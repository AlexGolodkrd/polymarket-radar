"""Position reconciliation — every 60s, sync local positions with each
exchange's authoritative view. Mismatch > $0.01 → halt + alert.

Phase 3 builds the loop, the diff math, and the halt path. Real exchange
REST calls are gated on having wallet keys (Phase 4) — without keys we
emit a heartbeat row that says "skipped: no keys" so ops can see the loop
is alive. Once keys land, the same loop becomes authoritative.
"""
import json
import logging
import os
import threading
import time
from typing import Callable, Optional

from . import state as st
from . import killswitch

log = logging.getLogger(__name__)

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_THIS_DIR, '..', '..'))
EXECUTIONS_DIR = os.path.join(_REPO_ROOT, 'Executions')
POSITIONS_LOG = os.path.join(EXECUTIONS_DIR, 'positions.jsonl')
RECONCILE_LOG = os.path.join(EXECUTIONS_DIR, 'reconcile.jsonl')

_loop_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_last_status: dict = {}
_status_lock = threading.Lock()

# Phase 19v15 (05.05.2026) — outcome canonicalization for diff keys.
# Polymarket returns `outcome: "0"`/`"1"` from one endpoint and
# `"Yes"`/`"No"` from another; Limitless varies similarly. Without
# normalisation, local writes (executor) and remote reads (positions
# fetcher) keyed differently → false mismatch → kill switch trips.
_OUTCOME_NORM_MAP = {
    '0': 'YES', '1': 'NO',
    'yes': 'YES', 'no': 'NO',
    'true': 'YES', 'false': 'NO',
    'outcomeone': 'YES', 'outcometwo': 'NO',  # SX
}


def _norm_outcome(o) -> str:
    if o is None:
        return ''
    s = str(o).strip().lower()
    return _OUTCOME_NORM_MAP.get(s, s.upper())


# Reconcile-failure debounce: kill switch only fires after N consecutive
# bad runs. Single transient blip (CF 5xx, DNS hiccup) shouldn't halt
# trading globally — that requires manual unkill, blocking the user.
_consecutive_failures: int = 0
_RECONCILE_FAIL_THRESHOLD = 3


def _append(path: str, row: dict):
    os.makedirs(EXECUTIONS_DIR, exist_ok=True)
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(row, default=str) + '\n')


# ── Local positions reader ──────────────────────────────────────────
def _read_local_positions() -> dict:
    """Parse positions.jsonl and aggregate to (platform, market_id, outcome)
    → net_size_usdc. Phase 4 will write rows after every fill confirm;
    for now this is empty (no fills exist yet in dry-run)."""
    if not os.path.exists(POSITIONS_LOG):
        return {}
    positions = {}
    try:
        with open(POSITIONS_LOG, 'r', encoding='utf-8') as f:
            for line in f:
                row = json.loads(line)
                # Phase 19v15 — canonicalize outcome for join consistency.
                key = (row.get('platform'), row.get('market_id'),
                       _norm_outcome(row.get('outcome')))
                positions[key] = positions.get(key, 0.0) + float(row.get('size_usdc', 0))
    except Exception as e:
        log.warning("positions log parse failed: %s", e)
    return positions


# ── Exchange fetchers (Phase 4 fills these in with real keys) ───────
_exchange_fetchers: list = []


def register_exchange_fetcher(fn: Callable[[], dict]):
    """Phase 4: wallet manager registers `fetch_polymarket_positions(wallet)`,
    `fetch_sx_positions(wallet)`, etc. Each returns the same key shape as
    `_read_local_positions`. Phase 3 leaves the list empty."""
    _exchange_fetchers.append(fn)


def clear_exchange_fetchers():
    _exchange_fetchers.clear()


# ── Polymarket positions fetcher (Phase 10 #51, 30.04.2026) ───────
# Pulls every bot wallet's Polymarket positions via authenticated REST.
# Auth: L2 HMAC headers (POLY_ADDRESS / TIMESTAMP / API_KEY / PASSPHRASE
# / SIGNATURE). Without L2 creds (Phase 4 not yet provisioned) this
# returns {} silently — reconcile loop continues to heartbeat 'skipped'.
#
# To enable: derive L2 creds via Scripts/poly_derive_api_creds.py for
# each bot, then call register_polymarket_fetcher() at radar startup.
POLY_POSITIONS_URL = 'https://clob.polymarket.com/data/positions'


def fetch_polymarket_positions(wallets, *, http_get=None,
                                 timeout: float = 5.0) -> dict:
    """For each wallet with full L2 creds, GET /data/positions and merge
    into the canonical (platform, market_id, outcome) → size_usdc shape.

    Returns the same key shape as `_read_local_positions` so `_diff_positions`
    can compare directly.

    Each wallet is queried independently (sequential — at <=6 wallets it's
    not worth the parallelism overhead, and parallel would hammer Cloudflare).
    """
    out: dict = {}
    if http_get is None:
        import requests
        http_get = requests.get
    try:
        from executor.builders import build_poly_hmac_headers
    except ImportError:
        log.warning("executor.builders not importable — cannot fetch positions")
        return out

    for w in wallets:
        api_key = getattr(w, 'poly_api_key', None)
        secret = getattr(w, 'poly_secret', None)
        passphrase = getattr(w, 'poly_passphrase', None)
        addr = getattr(w, 'eth_address', None)
        if not (api_key and secret and passphrase and addr):
            continue
        try:
            path = '/data/positions'
            headers = build_poly_hmac_headers(
                method='GET', path=path, body='',
                api_key=api_key, api_secret=secret, passphrase=passphrase,
                eth_address=addr,
            )
            r = http_get(POLY_POSITIONS_URL, headers=headers, timeout=timeout)
            if r.status_code != 200:
                log.warning("polymarket positions for %s returned %d",
                            addr[:10], r.status_code)
                continue
            data = r.json() or {}
            positions = (data if isinstance(data, list) else
                          data.get('data') or data.get('positions') or [])
            for p in positions:
                cond = p.get('conditionId') or p.get('market')
                outcome = p.get('outcome') or p.get('outcomeIndex')
                size = float(p.get('size') or p.get('shares') or 0)
                price = float(p.get('avgPrice') or p.get('price') or 0)
                size_usdc = size * price
                if cond is None or outcome is None or size_usdc <= 0:
                    continue
                key = ('Polymarket', cond, _norm_outcome(outcome))
                out[key] = out.get(key, 0.0) + size_usdc
        except Exception as e:
            log.warning("polymarket positions fetch for %s failed: %s",
                         (addr or '?')[:10], e)
    return out


# ── Phase 17 (01.05.2026) — Limitless positions fetcher ────────────
# Auth: X-API-Key per wallet (simpler than Polymarket L2 HMAC).
# Endpoint: GET /portfolio (returns all open positions for the wallet).
LIMITLESS_PORTFOLIO_URL = 'https://api.limitless.exchange/portfolio'


def fetch_limitless_positions(wallets, *, http_get=None,
                                timeout: float = 5.0) -> dict:
    """For each wallet with X-API-Key, GET /portfolio and return canonical
    (platform, slug, outcome) → size_usdc shape for reconcile diff.
    Operator-flagged gap from PR #58 review."""
    out: dict = {}
    if http_get is None:
        import requests
        http_get = requests.get
    for w in wallets:
        api_key = getattr(w, 'api_key', None)
        addr = getattr(w, 'eth_address', None)
        if not (api_key and addr):
            continue
        try:
            headers = {'X-API-Key': api_key}
            r = http_get(LIMITLESS_PORTFOLIO_URL, headers=headers,
                          timeout=timeout, params={'address': addr})
            if r.status_code != 200:
                log.warning("limitless portfolio for %s returned %d",
                             addr[:10], r.status_code)
                continue
            data = r.json() or {}
            positions = (data.get('positions') or data.get('data') or
                          (data if isinstance(data, list) else []))
            for p in positions:
                slug = p.get('marketSlug') or p.get('slug')
                outcome = p.get('outcome') or p.get('side')
                size = float(p.get('size') or p.get('shares') or 0)
                price = float(p.get('avgPrice') or p.get('price') or 0)
                size_usdc = size * price
                if slug is None or outcome is None or size_usdc <= 0:
                    continue
                key = ('Limitless', slug, _norm_outcome(outcome))
                out[key] = out.get(key, 0.0) + size_usdc
        except Exception as e:
            log.warning("limitless positions fetch for %s failed: %s",
                         (addr or '?')[:10], e)
    return out


def register_limitless_fetcher(wallets):
    """Register Limitless reconcile fetcher if at least one wallet has
    X-API-Key configured."""
    eligible = [w for w in wallets if getattr(w, 'api_key', None)]
    if not eligible:
        log.info("limitless reconcile fetcher NOT registered "
                 "(no wallet has X-API-Key — set BOT{N}_LIMITLESS_API_KEY)")
        return False
    register_exchange_fetcher(lambda: fetch_limitless_positions(eligible))
    log.info("limitless reconcile fetcher registered for %d wallet(s)",
             len(eligible))
    return True


# ── Phase 17 (01.05.2026) — SX Bet positions fetcher ──────────────
# SX Bet doesn't have a public /portfolio endpoint (positions tracked
# on-chain via CTF token balances). For reconcile we'd need to query
# CTF.balanceOf(wallet, token_id) for each market we've fired on. This
# is heavier than REST polls — defer to lazy on-demand check at fill
# confirmation time. For now: register a no-op fetcher that returns {}
# silently so reconcile loop doesn't error on SX wallets.
def fetch_sx_positions(wallets, *, http_get=None) -> dict:
    """Stub — SX positions live on-chain (CTF 1155 balances). Real
    implementation would walk CTF.balanceOf for each fired market."""
    return {}


def register_sx_fetcher(wallets):
    """Register SX no-op fetcher (positions tracked on-chain only)."""
    if not wallets:
        return False
    register_exchange_fetcher(lambda: fetch_sx_positions(wallets))
    log.info("sx reconcile fetcher registered (no-op stub — positions "
             "tracked on-chain via CTF.balanceOf)")
    return True


def register_polymarket_fetcher(wallets):
    """Call this at radar startup once L2 creds for at least one wallet
    are present. Wraps `fetch_polymarket_positions` as a no-arg callable
    so reconcile loop can iterate uniformly.
    """
    eligible = [w for w in wallets
                if getattr(w, 'poly_api_key', None)
                and getattr(w, 'poly_secret', None)
                and getattr(w, 'poly_passphrase', None)]
    if not eligible:
        log.info("polymarket reconcile fetcher NOT registered "
                 "(no wallet has full L2 creds yet — run poly_derive_api_creds.py)")
        return False
    register_exchange_fetcher(lambda: fetch_polymarket_positions(eligible))
    log.info("polymarket reconcile fetcher registered for %d wallet(s)",
             len(eligible))
    return True


def _diff_positions(local: dict, remote: dict, tolerance: float = None) -> list:
    """Return list of mismatches — each element {key, local, remote, diff}."""
    if tolerance is None:
        tolerance = st.RECONCILE_TOLERANCE_USD
    keys = set(local) | set(remote)
    mismatches = []
    for k in keys:
        l = local.get(k, 0.0)
        r = remote.get(k, 0.0)
        if abs(l - r) > tolerance:
            mismatches.append({'key': str(k), 'local': l, 'remote': r,
                               'diff': l - r})
    return mismatches


def reconcile_once() -> dict:
    """Run a single reconcile pass. Returns a status dict and updates
    risk state's last_reconcile_* fields. If keys aren't loaded yet
    (Phase 3 default), records 'skipped' but the loop keeps running."""
    started = time.time()
    s = st.get_state()

    if not _exchange_fetchers:
        msg = 'skipped: no exchange fetchers registered (Phase 4 brings keys)'
        with _status_lock:
            global _last_status
            _last_status = {'ok': True, 'skipped': True, 'msg': msg, 'ts': started}
        s.last_reconcile_unix = started
        s.last_reconcile_ok = True
        s.last_reconcile_msg = msg
        st.save_state(s)
        _append(RECONCILE_LOG, {'event': 'heartbeat', 'msg': msg, 'ts': started})
        return _last_status

    local = _read_local_positions()
    remote: dict = {}
    fetcher_errors = []
    for fn in _exchange_fetchers:
        try:
            # Phase 19v15 (05.05.2026) — additive merge across fetchers.
            # Old `remote.update(...)` was destructive: if two fetchers
            # returned the same (platform, market_id, outcome) key (e.g.
            # operator registers per-wallet fetchers, or one wallet has
            # the same position split across two endpoints), the second
            # fetcher's value REPLACED the first instead of summing →
            # false-mismatch → false kill-switch trip.
            for k, v in (fn() or {}).items():
                remote[k] = remote.get(k, 0.0) + float(v or 0)
        except Exception as e:
            fetcher_errors.append(f'{fn.__name__}: {e}')

    mismatches = _diff_positions(local, remote)
    ok = (not mismatches) and (not fetcher_errors)

    # Phase 19v15 — debounce kill-switch on transient errors. A single
    # CF 502 / DNS blip on /data/positions used to trip kill globally,
    # halting trading until manual unkill. Now require N consecutive
    # bad runs (mismatches always trip immediately — they can mean real
    # money on the wrong side of the book).
    global _consecutive_failures
    if not ok:
        if mismatches:
            # Position mismatch is always serious — kill immediately
            _consecutive_failures = _RECONCILE_FAIL_THRESHOLD
        else:
            _consecutive_failures += 1

    if not ok:
        msg = (f'mismatch: {len(mismatches)} keys differ' if mismatches
               else f'fetcher errors ({_consecutive_failures}/'
                    f'{_RECONCILE_FAIL_THRESHOLD}): {fetcher_errors}')
        log.warning("RECONCILE FAILED — %s", msg)
        if _consecutive_failures >= _RECONCILE_FAIL_THRESHOLD:
            # Halt: trip kill switch (operator must explicitly resume after
            # investigating).
            killswitch.kill(reason=f'reconcile_mismatch: {msg}')
            _consecutive_failures = 0  # reset so unkill+next failure isn't immediate
        _append(RECONCILE_LOG, {
            'event': 'mismatch' if mismatches else 'fetcher_error',
            'mismatches': mismatches,
            'fetcher_errors': fetcher_errors,
            'consecutive_failures': _consecutive_failures,
            'ts': started,
        })
    else:
        _consecutive_failures = 0
        _append(RECONCILE_LOG, {
            'event': 'ok', 'local_keys': len(local), 'remote_keys': len(remote),
            'ts': started,
        })

    with _status_lock:
        _last_status = {
            'ok': ok, 'skipped': False, 'mismatches': mismatches,
            'fetcher_errors': fetcher_errors, 'ts': started,
        }
    s.last_reconcile_unix = started
    s.last_reconcile_ok = ok
    s.last_reconcile_msg = (f'{len(local)} local / {len(remote)} remote'
                             if ok else f'{len(mismatches)} mismatches')
    st.save_state(s)
    return _last_status


# ── Loop ────────────────────────────────────────────────────────────
def _loop():
    log.info("reconcile loop started (interval=%ds)", st.RECONCILE_INTERVAL_S)
    while not _stop_event.is_set():
        try:
            reconcile_once()
        except Exception as e:
            log.exception("reconcile_once raised: %s", e)
        # Sleep in small steps so stop_reconcile_loop responds quickly
        for _ in range(st.RECONCILE_INTERVAL_S):
            if _stop_event.is_set(): break
            time.sleep(1)
    log.info("reconcile loop stopped")


def start_reconcile_loop():
    global _loop_thread
    if _loop_thread and _loop_thread.is_alive():
        return
    _stop_event.clear()
    _loop_thread = threading.Thread(target=_loop, daemon=True, name='risk-reconcile')
    _loop_thread.start()


def stop_reconcile_loop():
    _stop_event.set()


def last_reconcile_status() -> dict:
    with _status_lock:
        return dict(_last_status)
