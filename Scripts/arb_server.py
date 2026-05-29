"""
Arbitrage Radar v7 — 3 platforms (Poly, Kalshi, SX Bet) + Polymarket WebSocket.

Main scan: 300 Poly + 200 Kalshi + 200 SX Bet (fast ~35s) — REST.
Pause scan: extra pages — REST background.
HOT/NEAR pool architecture:
    HOT  = sum < threshold              (already an arb)
    NEAR = threshold <= sum < +NEAR_BUFFER  (one tick away from arb)
    COLD = sum >= threshold + NEAR_BUFFER   (ignored until next main scan)
Polymarket HOT+NEAR    → WebSocket push (instant)
Kalshi    HOT+NEAR    → REST micro-scan every KALSHI_MICRO_INTERVAL s
SX Bet    HOT+NEAR    → REST micro-scan every SX_MICRO_INTERVAL s (live sport)

Rate-limit safeguards:
    - WS subs capped at MAX_WS_SUBS (default 200)
    - WS reconnect backoff 1->2->4->8->30s
    - WS heartbeat: PING every 10s, watchdog drops conn after 30s silence
    - REST micro-scan only the HOT+NEAR pool, never the full universe
"""
import sys, io, os, json, re, time, threading
import logging
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as _CFTimeoutError

# Phase audit-2 (12.05.2026) — ASYNC_FETCH default REMOVED permanently.
# Iteration history:
#   #179: ON, page=20+ob=30  →  CB:limitless OPEN every minute
#   #180: hotfix OFF (sync)  →  stable but slow (~40s lim_ms)
#   #181: ON, page=8+ob=12   →  STILL CB cycling every 4-7 min
# Conclusion: Limitless rate limit doesn't tolerate ANY level of
# concurrent fetches from one IP. Even 8 parallel page requests
# trip it under sustained load (the operator's vps_vpn_drop_detector
# bot logged a CB OPEN ~every 5 min over a 2h window).
# Reverting to sync-only by default. The async path stays in the
# code (operator can `ASYNC_FETCH=1` if they ever get whitelisted
# or move to a higher rate-limit tier). LIMITLESS_PAGE_CONCURRENT
# and LIMITLESS_OB_CONCURRENT remain configurable for that case.
# (no setdefault — env unset = '' = sync path, the safe default)
# Phase audit (11.05.2026) — BUG-A3 root cause. Unconditionally wrapping
# sys.stdout in TextIOWrapper closes pytest's captured tmpfile (the new
# wrapper takes ownership of `sys.stdout.buffer`; subsequent reads via
# pytest's `tmpfile.seek(0)` fail with `ValueError: I/O operation on
# closed file`). Skip the UTF-8 wrap under pytest (test stdout doesn't
# need cyrillic console output) and when stdout has no .buffer attribute
# (some CI runners + pytest captures emit text-mode SpooledTemporaryFile).
if hasattr(sys.stdout, 'buffer') and 'pytest' not in (sys.argv[0] if sys.argv else ''):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# Phase 19v14 (05.05.2026) — module-level logger. Several error-handling
# paths reference `log.debug` / `log.warning` (kalshi fail, cross_platform
# error, persist contention, analytics reset failure). Without this the
# first error on each path raises `NameError: name 'log' is not defined`,
# which is then swallowed by an outer `except Exception` and silently
# distorts the actual failure reason.
log = logging.getLogger(__name__)
# Phase audit (11.05.2026) — BUG-A3 fix. Adding StreamHandler() at module
# import time captures the CURRENT sys.stderr; pytest's capture system
# swaps stderr between tests, and the cached handle becomes "closed" →
# `ValueError: I/O operation on closed file` during test collection. We
# now skip handler installation under pytest (pytest's caplog/captures
# handle output) and let gunicorn / root logger handle production output.
import sys as _sys_check
_under_pytest = (
    'pytest' in (_sys_check.argv[0] if _sys_check.argv else '') or
    'PYTEST_CURRENT_TEST' in os.environ
)
if not log.handlers and not _under_pytest:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
    log.addHandler(_h)
log.setLevel(logging.INFO)

from flask import Flask, jsonify, send_file
import requests

# Make Scripts/ importable when run from project root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from poly_ws import PolyMarketWS
from poly_user_ws import PolyUserWS
from limitless_ws import LimitlessWS
import analytics
from executor import fire_arb as _fire_arb_python, paper_stats
from executor.builders import WalletStub
import risk as risk_mod
import wallets as wallets_mod
import paper_trading

# Phase TS-3 (08.05.2026) — optional TypeScript executor switch.
# When `EXECUTOR_URL` env is set (e.g. http://executor-ts:5051),
# fire_arb() POSTs the deal to the TS executor instead of running the
# Python executor in-process. Fallback to Python in-process if env is
# unset OR the HTTP call fails (so the radar keeps working when the
# Node service is down).
#
# Phase audit-2 (11.05.2026) — default changed from '' to 'http://executor-ts:5051'.
# Operator's Credentials.env didn't include EXECUTOR_URL → radar fell back
# to Python executor → Python can't dispatch cross-platform deals
# (platform='Polymarket+SX Bet' doesn't match `if platform == 'Polymarket'`
# in _build_leg) → all CP legs rejected → 100% paper-trade rejection,
# win_rate=0%, fills=0 on TS executor /metrics. Result: 3 hours of dry-run
# data was useless for measuring fill viability.
#
# The default points at the docker-compose service name `executor-ts:5051`
# which resolves correctly inside the radar container. Operator can still
# override via env (set EXECUTOR_URL='' to force Python path for debugging).
_EXECUTOR_URL = os.environ.get('EXECUTOR_URL', 'http://executor-ts:5051').rstrip('/')


def _alert_allowance_errors(resp_json):
    """Phase audit-14 (15.05.2026) — scan a fire response's leg_details
    for the platform-specific on-chain allowance error strings and ping
    the operator with a structured Telegram alert containing the
    spender address they need to approve.

    Two patterns covered:
      - SX: `TAKER_INSUFFICIENT_BASE_TOKEN_ALLOWANCE` (chain 4162,
        spender = TokenTransferProxy, single global address)
      - Limitless: `Insufficient collateral allowance for this order.`
        (chain 8453, per-market venue.exchange; we surface the slug so
        the operator can look up the right contract if needed, or just
        approve via the Limitless UI on that market)

    Dedupe key is per-platform so the operator gets ONE ping per outage
    window, not one per fire. Cleared automatically on next radar restart.
    """
    try:
        import notify
        if not notify.is_configured():
            return
    except Exception:
        return
    legs = (resp_json or {}).get('leg_details') or []
    sx_hit = False
    lim_hit_slug = None
    for leg in legs:
        err = str(leg.get('error') or '')
        platform = str(leg.get('platform') or '')
        if 'TAKER_INSUFFICIENT_BASE_TOKEN_ALLOWANCE' in err and platform == 'sx_bet':
            sx_hit = True
        elif 'Insufficient collateral allowance' in err and platform == 'limitless':
            lim_hit_slug = leg.get('slug') or '(unknown market)'
    if sx_hit:
        notify.send(
            '⚠️ *SX USDC approve needed*\n'
            'Wallet: `0xD7301352011f65D100d968e0De49fa4981aE3686`\n'
            'Chain: SX Network (4162)\n'
            'USDC: `0x6629Ce1Cf35Cc1329ebB4F63202F3f197b3F050B`\n'
            'Spender (TokenTransferProxy): '
            '`0x38aef22152BC8965bf0af7Cf53586e4b0C4E9936`\n'
            'Easiest: place a $1 manual bet on sx.bet — '
            'MetaMask will prompt approve.',
            level='warn',
            dedupe_key='sx_allowance_needed',
        )
    if lim_hit_slug is not None:
        notify.send(
            '⚠️ *Limitless USDC approve needed*\n'
            f'Market: `{lim_hit_slug}`\n'
            'Wallet: `0xD7301352011f65D100d968e0De49fa4981aE3686`\n'
            'Chain: Base (8453)\n'
            'USDC: `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`\n'
            'NegRisk family exchange: '
            '`0xe3E00BA3a9888d1DE4834269f62ac008b4BB5C47`\n'
            'Adapter: `0x6151EF8368b6316c1aa3C68453EF083ad31E712D`\n'
            'Easiest: open the market on limitless.exchange and '
            'place a small bet — MetaMask will prompt approve.',
            level='warn',
            dedupe_key='limitless_allowance_needed',
        )


def _fire_arb_via_ts(deal, wallets=None, dry_run=True, **kwargs):
    """Forward `deal` to the TypeScript executor via POST /fire.

    Translates Python's `deal` dict into FireRequest shape (lowercase
    field names, leg specs from `deal.entries`), POSTs, parses the
    ArbFireResult JSON. Returns a python-side compat object whose
    attributes match `Scripts/executor/atomic.ArbFireResult`.

    Falls back to in-process Python executor on any HTTP error so a
    transient TS executor outage doesn't pause the radar.

    Phase audit-2 (11.05.2026) — every dispatch writes a row to
    `Executions/pipeline_timings.jsonl` so the operator can measure
    median pipeline latency (scan→dispatch→response) for any CP fire.
    See `executor.pipeline_timing` for schema + /api/pipeline_timings.
    """
    import requests
    # Pre-build the arb_id (used by both success and failure logging)
    arb_id = (deal.get('arb_id') or deal.get('id')
              or f"py-{int(time.time()*1000)}")
    # Look up first-seen timestamp for scan_to_dispatch_ms.
    # Preference: deal._pipeline_seen_ts stamped by _maybe_dry_fire (always
    # set on dispatch). Fall back to analytics open-deals tracker (works
    # for subsequent fires of the same deal). None only when both are
    # absent (test isolation, callers that bypass _maybe_dry_fire).
    first_seen_ts = deal.get('_pipeline_seen_ts')
    if first_seen_ts is None:
        try:
            first_seen_ts = analytics.get_first_seen_ts(analytics.deal_key(deal))
        except Exception:
            first_seen_ts = None
    dispatch_start_ts = time.time()
    try:
        # Translate dict shape — Python's deal uses 'entries' with
        # legacy field names; TS expects {arbId, dealTitle, structure,
        # entries: [{platform, tokenId, marketHash, side, expectedPrice,
        # expectedSizeUsdc, ...}]}.
        entries_ts = []
        for e in deal.get('entries', []):
            # Side translation — cross_platform.py stores side per outcome
            # perspective ('YES'/'NO' = which side of a binary contract we
            # selected); TS executor builders expect transaction perspective
            # ('BUY'/'SELL' = direction of the order on that side). For arb
            # firing both legs are ALWAYS buys (we acquire contracts on each
            # side; nobody sells YES to enter an arb). Per-platform deals
            # don't set 'side' so default 'BUY' carries through.
            #
            # Phase audit-3 (15.05.2026) — without this translation, every
            # Limitless leg of a CP arb rejected with "side must be
            # BUY|SELL, got YES" in `buildLeg`. Operator caught it on the
            # first real-mode CP fire when leg_details came back with the
            # error.
            raw_side = (e.get('side') or 'BUY').upper()
            ts_side = 'BUY' if raw_side in ('YES', 'NO', 'BUY') else 'SELL'
            spec = {
                'platform': (e.get('platform') or '').lower().replace(' bet', '_bet'),
                'side': ts_side,
                'expectedPrice': float(e.get('price') or e.get('expected_price') or 0),
                'expectedSizeUsdc': float(e.get('stake') or e.get('expected_size_usdc') or 0),
            }
            # Phase audit-2 (11.05.2026) — Python uses snake_case for these
            # leg fields (set by cross_platform._leg_platform_ids); TS
            # uses camelCase per its FireRequest schema. Mapping kept
            # explicit so renames on either side fail loudly. Old code
            # had `marketHash` on Python side (never populated) so SX
            # legs always lacked the field — root cause of 100% errored
            # fires we observed today.
            for k_py, k_ts in (
                ('token_id', 'tokenId'),
                ('market_hash', 'marketHash'),
                ('outcome_index', 'outcome'),
                ('slug', 'slug'),
                ('verifying_contract', 'verifyingContract'),
                ('neg_risk', 'negRisk'),
                ('tick_size', 'tickSize'),
                ('condition_id', 'conditionId'),
            ):
                if e.get(k_py) is not None:
                    spec[k_ts] = e[k_py]
            entries_ts.append(spec)
        # Phase audit-2 (11.05.2026) — pass expectedPayout so TS can compute
        # simPnl correctly. Without this TS uses a hardcoded $1 placeholder,
        # giving simPnl < 0 for CP arbs and pinning paper_stats.win_rate=0%.
        # Order of preference:
        #   1. deal.payout_target (per-platform builds set this explicitly:
        #      ALL_YES=1, ALL_NO with N outcomes=N-1, YN_PAIR=1)
        #   2. for CP deals (which don't set payout_target), the leg face
        #      value from entries[0]['contracts'] — same across legs
        #      because to_radar_deal_format sets contracts=actual_face on
        #      every leg
        #   3. fallback 1.0 (pre-fix behavior; safe for ALL_YES per-platform)
        payout_target = deal.get('payout_target')
        if payout_target is None:
            first = (deal.get('entries') or [{}])[0]
            payout_target = float(first.get('contracts') or 1.0)
        body = {
            'arbId': arb_id,
            'dealTitle': deal.get('title', '?'),
            'structure': deal.get('arb_structure') or deal.get('structure') or 'unknown',
            'entries': entries_ts,
            'dryRun': bool(dry_run),
            'expectedPayout': float(payout_target),
        }
        r = requests.post(
            f'{_EXECUTOR_URL}/fire', json=body, timeout=10,
        )
        r.raise_for_status()
        resp_json = r.json()
        dispatch_end_ts = time.time()
        # Log timing row for /api/pipeline_timings aggregation.
        try:
            from executor import pipeline_timing
            pipeline_timing.log_fire_timing(
                arb_id=arb_id, deal=deal,
                first_seen_ts=first_seen_ts,
                dispatch_start_ts=dispatch_start_ts,
                dispatch_end_ts=dispatch_end_ts,
                response_status='ok',
                executor_kind='ts',
            )
        except Exception:
            pass
        # Phase audit-14 (15.05.2026) — operator asked for a Telegram
        # alert when a fire fails because of an on-chain allowance
        # miss. Scan the response's leg_details for the platform-
        # specific allowance error strings and surface a structured
        # alert with the spender address the operator needs to approve.
        # Dedupe by alert key so we don't flood the operator's chat
        # with one ping per fire.
        try:
            _alert_allowance_errors(resp_json)
        except Exception:
            pass
        # Phase audit-15 (15.05.2026) — record a real entered trade in
        # analytics_events.jsonl when the fire actually put on a position
        # (any leg status='filled' with non-zero size). Powers the
        # dashboard's new `filled` metric so the operator sees REAL
        # trades, not radar predictions.
        try:
            from analytics import record_fire_filled
            record_fire_filled(arb_id, deal, resp_json.get('leg_details') or [])
        except Exception:
            pass
        return resp_json
    except Exception as exc:
        dispatch_end_ts = time.time()
        # Classify failure so /api/pipeline_timings can segment percentiles.
        exc_name = type(exc).__name__
        status = (f'http_error' if isinstance(exc, requests.HTTPError)
                  else f'exception:{exc_name}')
        try:
            from executor import pipeline_timing
            pipeline_timing.log_fire_timing(
                arb_id=arb_id, deal=deal,
                first_seen_ts=first_seen_ts,
                dispatch_start_ts=dispatch_start_ts,
                dispatch_end_ts=dispatch_end_ts,
                response_status=status,
                executor_kind='ts',
            )
        except Exception:
            pass
        # Fall back to in-process executor — never block on TS outage.
        try:
            return _fire_arb_python(deal, wallets=wallets, dry_run=dry_run, **kwargs)
        except Exception as exc2:
            return {
                'arb_id': arb_id,
                'aborted_reason': f'ts-bridge {exc!r}; fallback {exc2!r}',
                'dry_run': dry_run,
                'leg_count': len(deal.get('entries', [])),
                'leg_status_counts': {'aborted': len(deal.get('entries', []))},
            }


def fire_arb(deal, wallets=None, dry_run=True, **kwargs):
    """Phase TS-3 dispatcher: TS executor if EXECUTOR_URL set, else Python.

    Same signature as the original `executor.fire_arb` so existing call
    sites work unchanged.
    """
    if _EXECUTOR_URL:
        return _fire_arb_via_ts(deal, wallets=wallets, dry_run=dry_run, **kwargs)
    return _fire_arb_python(deal, wallets=wallets, dry_run=dry_run, **kwargs)


app = Flask(__name__)

# Phase audit-28d (27.05.2026) — register extracted blueprints from
# `radar.api.*`. Currently:  /api/version. As more endpoints are
# extracted from this monolith they get added to register_api_blueprints
# in radar/api/__init__.py, with no further changes needed here.
try:
    from radar.api import register_api_blueprints
    register_api_blueprints(app)
except Exception as _e:  # pragma: no cover  (don't kill boot if blueprint import fails)
    print(f"[radar.api] blueprint registration failed: {_e}", flush=True)

# ── Phase 2: dry-run executor — auto-fire deals when they enter HOT ─
# Phase audit-28a (27.05.2026) — fire-dedup data structure EXTRACTED to
# `radar/dedup.py`. The class `FireDedup` owns the TTL store; the
# module-level `fire_dedup` singleton is what _maybe_dry_fire below uses.
#
# For backward compat with tests that touched the raw globals
# (test_phase_9i, test_phase_9uu_concurrency), the legacy names are
# re-exported as aliases pointing at the singleton's internals. New
# code MUST use the `fire_dedup` API instead.
#
# History of this state slice:
#   Phase 9i  (28.04.2026): two-phase commit — fire-out-of-lock pattern.
#   Phase 9uu (29.04.2026): added "evict when not in active deals"
#                            (buggy — caused 27.05 re-fire loop).
#   Phase audit-27.05      : REPLACED with TTL eviction.
#   Phase audit-28a        : EXTRACTED into `radar/dedup.py`.
from radar.dedup import fire_dedup as _fire_dedup, _arb_fire_key
_fired_arb_keys: dict = _fire_dedup._raw_dict       # legacy alias
_fired_arb_keys_lock: threading.Lock = _fire_dedup._raw_lock  # legacy alias
_FIRED_KEYS_HARD_CAP: int = _fire_dedup.hard_cap     # legacy alias
FIRE_COOLDOWN_S: int = _fire_dedup.cooldown_s        # legacy alias

# Phase audit-28b cont 8 (28.05.2026) — `_last_visible_near_count` +
# `_last_near_rejection_stats` migrated to `radar.eval.pools` (where
# near_summary lives now). arb_server.py exposes them via module-level
# __getattr__ below so legacy callers (api.deals) and tests still see
# `arb_server.X` semantics.

# Phase 19v13 (04.05.2026) — protect _persist_scan_state from concurrent
# daemon-thread writes. Try-acquire non-blocking; skip if previous still
# in flight. Prevents corrupt scan_state.json under high scan tick rate.
_persist_state_lock = threading.Lock()

# Phase audit-28a — `_arb_fire_key` was extracted to radar/dedup.py and
# re-imported above. The legacy module-level alias remains so existing
# call sites (and tests grepping for it) keep working.

# Phase 4: load wallet pool from configured backend at startup.
# If Credentials.env has no BOT*_ETH_ADDRESS entries, the pool stays empty
# and atomic.py falls back to a single mock stub (still dry-run safe).
# When the user fills in addresses, the real 6-bot pool is used.
_wallet_pool = wallets_mod.load_pool()
_DRY_RUN_WALLETS = [
    WalletStub(bot_id=w.bot_id, eth_address=w.eth_address,
               private_key=None)
    for w in _wallet_pool.wallets
]
# Live-fire gate (read once at module import — radar restart picks up
# any change to DRY_RUN in Credentials.env). When DRY_RUN=0:
#   - auto-firer uses real `_wallet_pool.wallets` (signing keys present)
#   - executor receives dry_run=False so it actually POSTs orders
# When DRY_RUN=1 (default) the path stays paper: stubs + dry-run flag.
# Operator's manual /api/dryfire endpoint stays forced dry-run regardless.
_LIVE_FIRE = os.environ.get('DRY_RUN', '1').strip() == '0'
_FIRE_WALLETS = _wallet_pool.wallets if _LIVE_FIRE else _DRY_RUN_WALLETS
_FIRE_DRY_RUN = not _LIVE_FIRE
print(f"[fire-mode] DRY_RUN={os.environ.get('DRY_RUN','1')} → "
      f"live={_LIVE_FIRE} wallets={'real(' + str(len(_FIRE_WALLETS)) + ')' if _LIVE_FIRE else 'stubs(no-keys)'}",
      flush=True)

def _maybe_dry_fire(deals):
    """Auto-fire (dry-run) any deal not previously fired this session.
    Called after every main scan and after every WS-driven re-eval.

    Phase 9i (28.04.2026): two-phase commit fixes a TOCTOU race +
    serialization issue. Old code held _fired_arb_keys_lock across the
    fire_arb call which:
      (a) blocked any other thread (scan_loop, WS callbacks) for the
          full 5s dead-man timeout in real mode
      (b) had a check-then-add gap if anyone snuck a release into the
          inner fire_arb path
    New approach: reserve all keys atomically under lock, fire without
    lock, parallel calls now safe."""
    if not deals:
        return
    # Phase audit-28a (27.05.2026) — delegated to FireDedup. The
    # singleton handles TTL eviction, hard-cap drop-oldest, and atomic
    # reserve-then-return. Tests that touch `_fired_arb_keys` directly
    # still work because that name is an alias to the singleton's dict.
    to_fire = _fire_dedup.reserve(list(deals))
    # Fire outside the lock — slow path doesn't block other threads.
    # Phase 9kkk (30.04.2026): also send Telegram alert on high-value
    # arbs (net >= ARB_ALERT_MIN_NET_USD = $10 by default), de-duped per
    # arb. This is independent of fire success — operator still gets
    # notified even if wallet pool is short of legs.
    try:
        from notify import alert_high_value_arb as _notify_arb
    except ImportError:
        _notify_arb = None
    for key, d in to_fire:
        # Telegram alert — fire-and-forget, daemon thread, dedupe inside
        if _notify_arb is not None:
            try:
                _notify_arb(d)
            except Exception as e:
                print(f"[DRYFIRE] notify error {key}: {e}")
        # Phase audit-2 (11.05.2026) — stamp pipeline-timing anchor.
        # `_fire_arb_via_ts` reads this to compute scan_to_dispatch_ms,
        # falling back to analytics.get_first_seen_ts(deal_key) when
        # absent. analytics is updated by a separate thread, so on the
        # very first dry-fire after a deal appears the open-deals
        # tracker doesn't have the key yet → scan_to_dispatch_ms was
        # null in /api/pipeline_timings even after many fires. With
        # this stamp the metric is always populated; on first fire it
        # essentially reflects "Python build + dispatch overhead".
        d['_pipeline_seen_ts'] = time.time()
        try:
            # Live vs dry decided at radar startup from DRY_RUN env. The
            # function name `_maybe_dry_fire` is legacy — we now fire for
            # real when DRY_RUN=0 (and the configured wallets have signing
            # keys). Operator's manual /api/dryfire button stays forced
            # dry-run regardless of env (different code path below).
            fire_arb(d, wallets=_FIRE_WALLETS, dry_run=_FIRE_DRY_RUN)
        except Exception as e:
            print(f"[FIRE] error firing {key}: {e}")

# Removed permissive `Access-Control-Allow-Origin: *` (Phase 9p, 28.04.2026).
# With same-origin frontend (dashboard.html → const API = '') we don't need
# CORS at all. The old wildcard let any third-party site read live deals
# data and (when combined with cached basic-auth credentials in the user's
# browser) potentially POST to /api/kill, /api/dryfire, etc. from a
# malicious page. Modern fetch() defaults to same-origin and works fine.
# If a future cross-origin client legitimately needs API access, add
# explicit allowlist here — never wildcard.

# ── Config ──────────────────────────────────────────────────────
BALANCE = 100.0

# Phase 19v6 (03.05.2026) — MIN_LEG_LIQ_USD filter: reject deals where
# the smallest-leg orderbook liquidity is below this threshold. Eliminates
# "mosquito arbs" at detection.
# Phase 19v8 (03.05.2026) — lowered default $10 → $5 to surface more
# borderline candidates in NEAR (operator wanted more visibility on
# Sunday low-activity hours when nothing reaches $10). $5 still rejects
# absolute mosquitos ($0-1 phantom MM inventory) but lets through small
# but tradeable arbs.
MIN_LEG_LIQ_USD = float(os.environ.get('MIN_LEG_LIQ_USD', '5'))
THETA_POLY      = 0.025   # Polymarket taker fee ~2.5%
THETA_KALSHI    = 0.07    # Kalshi taker fee ~7%
THETA_SX        = 0.02    # SX Bet taker fee ~2%
# Limitless Exchange (Base L2): NO platform fee, only gas (~$0.01 per leg).
# We model 0.5% as conservative buffer covering gas + slippage on $50 trade
# (gas $0.04 across 4 legs = 0.08% on $50, so 0.5% leaves room for spread).
THETA_LIMITLESS = 0.005
THRESH_POLY      = 0.97   # legacy fallback when no per-market info available;
                          # actual threshold is now DYNAMIC per arb (see
                          # compute_poly_threshold) — Polymarket V2 has
                          # per-market dynamic taker_fee_bps, so a single
                          # static threshold over-counts on 0-fee markets and
                          # under-counts on 2.5-3% markets.
THRESH_KALSHI    = 0.93   # 93c — covers ~7% taker fee with margin
THRESH_SX        = 0.97   # 97c — covers ~2% taker fee with margin
# Limitless: signed feeRateBps=300 (Bronze rank), effectiveFeeBps=0 per
# server response on live test 2026-05-15 (promo for new accounts). Net
# fee impact is 0% currently; if Limitless removes the promo this should
# drop to ~0.96 (= 1 - 0.03 fee - 0.005 slippage - buffer). Verify via
# Limitless POST response `execution.effectiveFeeBps` and update if it
# stops returning 0.
# Phase 9l (28.04.2026): bumped from 0.99 → 0.988 for extra cushion
# (matches the +0.002 safety buffer we added to dynamic Poly thresholds).
# 0.988 = 1.2¢ minimum margin per $1 = covers ~$0.005 gas + slippage
# safely + 0.5¢ buffer against drift between scan and fire.
THRESH_LIMITLESS = 0.988

# ── Phase 19v31 (06.05.2026) — quality_ok env overrides ─────────────
# Lets the operator tune the tight-margin gate (sum ≥ N¢ → require
# higher liquidity / lower slippage) without a code redeploy. Defaults
# match the post-9gg behavior so flipping these envs is a strict opt-in.
#
#   QUALITY_TIGHT_CUTOFF_CENTS — sum threshold above which the tight
#                                gate engages. Default 95.0 → arbs
#                                with ≥95¢ sum (≤5¢ margin) face the
#                                stricter min_liq / slip_pct check.
#                                Lowering it (e.g. 90.0) widens the
#                                gate so more deals get filtered.
#                                Raising it (e.g. 99.0) means only
#                                ultra-tight 1¢-margin arbs are gated.
#
#   QUALITY_TIGHT_MIN_LIQ      — min liquidity (USD) required on the
#                                worst leg of a Polymarket tight arb.
#                                Default 600. Lower → more deals show
#                                up but may have unfillable legs.
#
#   QUALITY_LIM_TIGHT_MIN_LIQ  — same for Limitless. Default 130
#                                (Limitless markets are typically
#                                thinner so threshold is lower).
#
#   QUALITY_TIGHT_MAX_SLIP     — max slip_pct (0..1 fraction) on the
#                                worst leg. Default 0.3 (30%). This
#                                is the absolute price drift the
#                                executor's depth-recheck would tolerate
#                                before aborting; same number across
#                                Polymarket and Limitless because the
#                                executor's slippage logic is identical.
QUALITY_TIGHT_CUTOFF_CENTS = float(
    os.environ.get('QUALITY_TIGHT_CUTOFF_CENTS', '95.0'))
QUALITY_TIGHT_MIN_LIQ = float(
    os.environ.get('QUALITY_TIGHT_MIN_LIQ', '600'))
QUALITY_LIM_TIGHT_MIN_LIQ = float(
    os.environ.get('QUALITY_LIM_TIGHT_MIN_LIQ', '130'))
QUALITY_TIGHT_MAX_SLIP = float(
    os.environ.get('QUALITY_TIGHT_MAX_SLIP', '0.3'))

# ── Dynamic Polymarket threshold (Phase 9k) ─────────────────────────
# Break-even THRESH per (theta, N_legs) so we don't reject valid arbs on
# 0%-fee markets (V2 promo) nor accept loss-making arbs on 2.5%+ markets.
#
# Cost components on $1 of capital:
#   fee_total      = theta × 1            (Polymarket charges taker fee on
#                                          every leg's filled stake;
#                                          stakes sum to capital)
#   slippage_total ≈ 0.003 × 1            (per-leg slip, conservative cap)
#   safety_buffer  = 0.005 × 1            (drift between scan and fire)
#
# Required margin to break even = fee_total + slippage_total + safety
#                              = theta + 0.008
# Therefore THRESH = 1 - (theta + 0.008)
#
# Floor 0.95 (never below — even hard-capped if API misreports fee).
# Cap   0.995 (never above — spread that tight is noise, not arb).
POLY_DYNAMIC_THRESH_FLOOR  = 0.948    # Phase 9l: -0.002 from 0.95 for extra cushion
POLY_DYNAMIC_THRESH_CAP    = 0.993    # Phase 9l: -0.002 from 0.995
POLY_SLIPPAGE_RESERVE      = 0.003    # per arb (not per leg — conservative)
# Phase 9l (28.04.2026): bumped safety buffer from 0.005 → 0.007 at user
# request. Effect: every dynamic threshold is now 0.002 lower, giving us
# an extra 0.2% margin cushion against unexpected drift / cache-stale
# fee / liquidity drop between scan and fire. Examples:
#   0%   fee: 0.992 → 0.990
#   1%   fee: 0.982 → 0.980
#   2.5% fee: 0.967 → 0.965
#   4%   fee: 0.952 → 0.950
# Trade-off: ~0.2% fewer arbs accepted, but every accepted one has bigger
# safety margin against the 5-10% promah scenarios we identified.
POLY_SAFETY_BUFFER         = 0.0    # Phase 9kkk #47 (30.04.2026) — operator
                                    # request: «с порога 97 убери страхующее
                                    # значение». Was 0.007 (Phase 9l). Now
                                    # threshold = 1 - (fee + slippage_reserve).
                                    # Only direct fee+slippage compensation,
                                    # no extra cushion. Other platforms
                                    # (Kalshi/SX/Limitless) keep their static
                                    # thresholds with built-in margins.


# compute_poly_threshold extracted to radar.eval.polymarket
# (audit-28b cont 6, 28.05.2026). Re-export for legacy callers + tests
# that reference arb_server.compute_poly_threshold directly.
from radar.eval.polymarket import compute_poly_threshold  # noqa: E402,F401
SCAN_INTERVAL = 90
MICRO_INTERVAL = 5             # legacy — kept as fallback only
KALSHI_MICRO_INTERVAL = 5      # REST poll for Kalshi HOT+NEAR pool
SX_MICRO_INTERVAL = 3          # REST poll for SX Bet HOT+NEAR pool (live sport)

# Per-platform enable toggles. Set ENABLE_KALSHI=0 / ENABLE_SX=0 in env to
# skip those platforms entirely — no fetches, no eval, no micro-loop.
# Useful when focusing capacity on one platform (e.g. Polymarket-only mode
# while Kalshi/SX are inaccessible from current jurisdiction).
#
# Phase audit-2 (12.05.2026) — Kalshi geo-blocks operator's IP (US-only
# KYC) so all kalshi fetches were silently timing out and contributing
# ~10-30s of dead air to every scan tick. Default flipped to OFF. The
# env var is still honored for future US-KYC deployments, but the
# default is now "skip Kalshi entirely". Operator's directive after
# observing 95s scan_tick_ms p50.
ENABLE_KALSHI = os.environ.get('ENABLE_KALSHI', '0') != '0'
ENABLE_SX = os.environ.get('ENABLE_SX', '1') != '0'
# Phase 9r: Polymarket also gets a kill switch — some hosting providers
# (notably the Frankfurt CloudFront edge) face TLS-handshake hangs against
# gamma-api.polymarket.com that exceed our request timeout, locking the
# scan loop. Operator can disable Polymarket entirely while keeping
# Limitless live.
ENABLE_POLY = os.environ.get('ENABLE_POLY', '1') != '0'

# Phase 9p (29.04.2026): per-structure on/off switches.
# Operator can disable B (ALL_NO) and C (YES_NO_PAIR) independently
# during paper-trading bring-up to focus on the simplest, most-mature
# A (ALL_YES) signal first. ENABLE_STRUCT_A=0 effectively disables the
# whole detector so it's kept on by default; B and C are also on by
# default so behaviour is unchanged unless env explicitly opts out.
ENABLE_STRUCT_A = os.environ.get('ENABLE_STRUCT_A', '1') != '0'
ENABLE_STRUCT_B = os.environ.get('ENABLE_STRUCT_B', '1') != '0'
ENABLE_STRUCT_C = os.environ.get('ENABLE_STRUCT_C', '1') != '0'
# Limitless Exchange (Base L2 prediction market) — added 28.04.2026.
# Same CLOB/EIP-712 architecture as Polymarket, no KYC, no platform fee.
ENABLE_LIMITLESS = os.environ.get('ENABLE_LIMITLESS', '1') != '0'
LIMITLESS_API_KEY = os.environ.get('LIMITLESS_API_KEY', '').strip()  # for trade-side ops; reads work without key

# Polymarket main-scan pages. Each page = 500 events. 4 pages = 2000 events
# per scan. Default was 2 pages; bumped because skipping Kalshi/SX frees
# ~25s of fetch budget per scan that we can spend on more Poly coverage.
POLY_MAIN_PAGES = int(os.environ.get('POLY_MAIN_PAGES', '10'))
# Limitless main-scan pages. The API caps `limit` at 25 (verified 28.04.2026
# — server returns HTTP 400 for limit>25). To cover ~1000 markets we need
# 40 pages of 25. With 100ms polite gap → full fetch ~4s, well under our
# scan budget. Bumped from 10×100 to 40×25 after the cap was discovered.
# Phase audit-2 (12.05.2026) — reduced from 40 → 25. Operator's overnight
# observation: only 2-3 unique CP fixtures emerged across hours of scan,
# meaning pages 26-40 yielded near-zero arb candidates. 25 pages × 25
# events/page = 625 events covered — already past the ~250-500 active
# Limitless market count typical even on busy hours. Env-overridable
# upward if a future market expansion changes the picture.
LIMITLESS_MAIN_PAGES = int(os.environ.get('LIMITLESS_MAIN_PAGES', '25'))
LIMITLESS_PAGE_SIZE = int(os.environ.get('LIMITLESS_PAGE_SIZE', '25'))   # API max
LIMITLESS_PAGE_DELAY_S = float(os.environ.get('LIMITLESS_PAGE_DELAY_S', '0.1'))
# Phase audit-2 (12.05.2026) — async-path concurrency knobs (PR #181 after
# #179 hit rate limits + #180 reverted). The async path is much faster
# (~2-3s vs ~20s for 25 pages) but at max_concurrent=20 hit Limitless 429
# within minutes. Conservative defaults below stay below the rate-limit
# threshold while still using HTTP/2 multiplexing.
#
#   LIMITLESS_PAGE_CONCURRENT — parallel GET requests for /markets/active
#                                page fetcher. 8 = empirical safe ceiling
#                                (verified during PR #181 testing).
#   LIMITLESS_OB_CONCURRENT   — parallel GET requests for /markets/{slug}/
#                                orderbook batch. Lower than page fetcher
#                                because orderbook endpoint is per-slug and
#                                we hit it for HUNDREDS of slugs per scan.
LIMITLESS_PAGE_CONCURRENT = int(
    os.environ.get('LIMITLESS_PAGE_CONCURRENT', '8'))
LIMITLESS_OB_CONCURRENT = int(
    os.environ.get('LIMITLESS_OB_CONCURRENT', '12'))
# Phase 9qq (29.04.2026) — Progressive scan output. Push partial deals
# / NEAR / quarantine / stats to scan_data after every N fetched pages
# instead of waiting for the entire scan to finish. Without this, the UI
# looked dead for 60-90s during a full MAIN cycle (10 Poly pages + 40
# Lim pages + 200-250 orderbooks). With chunk=2, the user sees the first
# results within ~6-12s of scan start and watches them fill in.
POLY_CHUNK_PAGES = int(os.environ.get('POLY_CHUNK_PAGES', '2'))
# Phase audit-2 (12.05.2026) — chunk size 2 → 4. Each chunk costs ~1s of
# overhead (batch_fetch setup + eval_limitless + _push_partial + log).
# With 25 pages and chunks of 2 = 13 chunks × 1s = 13s overhead alone.
# At chunks of 4 = 7 chunks × 1s = 7s — saves ~6s while still giving the
# operator partial UI updates every ~3s during the Limitless phase.
LIMITLESS_CHUNK_PAGES = int(os.environ.get('LIMITLESS_CHUNK_PAGES', '4'))
LIMITLESS_MICRO_INTERVAL = int(os.environ.get('LIMITLESS_MICRO_INTERVAL', '5'))
LIMITLESS_API_BASE = 'https://api.limitless.exchange'
MAX_WORKERS = int(os.environ.get('MAX_WORKERS', '30'))
# Phase 9pp baseline = 30. Restored after Phase 9fff.0 false alarm.
# Operator correctly noted: hangs only appeared after Limitless WS was
# re-enabled. With ENABLE_LIMITLESS_WS=0 the issue should not surface.
# If it does, the cause is NOT Cloudflare throttling at high concurrency
# (we ran 30 fine before today) — it's something newer in the code path.
TIMEOUT = 5
NEAR_BUFFER = 0.03             # 3c (Phase 9kkk #45/46, 30.04.2026) — operator request
                               # "сделай порог попадания в near ближе к deals".
                               # Was 7c. Matches C_NEAR_MAX_DISTANCE so all 3
                               # structures (A/B/C) use the same buffer.
MAX_WS_SUBS = 1000             # Polymarket WS cap. Doubled from 500 to fit YES+NO
                               # tokens both subscribed (Phase 1 — ALL_NO / YES_NO_PAIR).
SX_PAGE_SIZE = 100             # SX Bet API rejects pageSize > 100 (HTTP 400)
SX_MAX_PAGES_MAIN = 10         # 10 * 100 = up to 1000 markets in main scan
SX_MAX_PAGES_PAUSE = 5         # 5 * 100 = up to 500 markets in pause scan

# SX Bet market types that are *binary and exhaustive* (outcomeOne+outcomeTwo
# cover all possible outcomes — perfect for arbitrage). Discovered live via
# `GET /markets/active`.
#
# Phase audit-16 (15.05.2026) — operator caught a real CP-arb bug:
#   • OUT: type=52 ("Soccer Draw No Bet, W/L only") was treated as binary,
#     but it ISN'T — on a draw the stake is REFUNDED, not paid as the
#     opposing team's win. So pairing Lim Brentford YES (binary) with
#     SX type=52 outcome 2 (Crystal Palace) produced sum_prices < $1
#     that LOOKED like an arb but left Draw uncovered (Limitless lost
#     in Draw with no SX recovery). Verified live: -$3.85 on Draw
#     against +$1.02 / +$1.09 in the two win outcomes for our 2× $1
#     SX + $3.85 Lim hedge of Brentford-CP. Removed from set.
#   • IN:  type=1  (Soccer 1X2 binary "Team / Not Team") was excluded
#     thinking it was 3-way; it actually IS true binary YES/NO on a
#     single statement ("Team A wins?"). outcome 2 "Not Team A" covers
#     (Team B wins) OR (Draw) — perfect for CP-arb pairing with
#     Limitless "Team A YES".
SX_BINARY_TYPES = {
    1,   # Soccer 1X2 binary (Team / Not Team) — TRUE binary, covers draw on NO side
    2,   # Soccer Total Over/Under
    3,   # Soccer Spread/Handicap
    21,  # Basketball 1st Period Total
    28,  # Hockey Total
    29,  # MMA Total
    45,  # Basketball 2nd Period Total
    46,  # Basketball 3rd Period Total
    # 52 — REMOVED, Soccer Draw No Bet (Draw refunds, NOT true binary)
    53,  # Basketball 1st Half Spread
    63,  # Basketball 1st Half Moneyline
    64,  # Basketball 1st Period Spread
    65,  # Basketball 2nd Period Spread
    66,  # Basketball 3rd Period Spread
    77,  # Basketball 1st Half Total
    165, # Tennis Sets Total
    166, # Tennis Games Total
    201, # Tennis Period Spread
    202, # Tennis 1st Set Moneyline
    203, # Tennis 2nd Set Moneyline
    204, # Basketball 3rd Period Moneyline
    226, # Hockey Moneyline (the original, kept)
    236, # Baseball 1st 5 Innings Total
    342, # Hockey Spread
    866, # Tennis Sets Spread
    1536,# E-Sports Total
    1618,# Baseball 1st 5 Innings Moneyline
    # Phase 16 (01.05.2026) — Q2: expanded sport coverage per operator
    # request "всё на SX кроме политики". SX Bet does host occasional
    # politics markets but they're rare and use 3-way (DNB-like) types
    # we can't cleanly arb. Sport types added below cover NBA/NFL/NHL/
    # MLB/Tennis/MMA/Soccer/E-sports moneyline + period spreads + totals.
    11,  # Soccer Both Teams To Score Yes/No
    50,  # Hockey 1st Period Moneyline
    81,  # Soccer 1st Half Total
    83,  # Soccer 1st Half Spread
    220, # NFL Moneyline
    223, # NFL Spread
    224, # NFL Total
    227, # NBA Moneyline
    230, # MLB Moneyline
    232, # MLB Total
    342, # Hockey Spread (already above; kept)
    374, # Soccer Total Goals (binary fancy)
    1117,# E-Sports Spread
    1346,# Soccer Both Halves Goal Yes/No
}
# SX_EXCLUDED_TYPES — types we explicitly KNOW are politics or
# multi-outcome (3+ way) and excluded from binary arb pipeline.
# Operator decision (01.05.2026): block politics, allow 3-way sport.
# 3-way sport currently blocked too — Phase 17 (separate pipeline)
# was de-prioritized after PRs #128-#148 showed cross-platform handles
# 3-way via Polymarket+SX pairing without needing a SX-internal
# 3-way orderbook query.
SX_EXCLUDED_TYPES = {
    # Phase audit-16 (15.05.2026) — type=1 moved OUT of excluded into
    # SX_BINARY_TYPES; it really is true binary "Team A wins?" YES/NO
    # with draw resolved as the NO side. Old comment "needs 3-way
    # pipeline" was wrong — the binary version IS the correct one.
    #
    # Add types here only when:
    #   • DNB-style (Draw refunds — e.g. former type=52 entry now removed)
    #   • Politics (operator policy: skip elections + government events)
    #   • Multi-outcome (3+ way with no clean binary projection)
    52,  # Soccer Draw No Bet — Draw refunds, NOT pure binary
    # Politics types we've observed on SX Bet (election outcomes etc):
    # Note: SX Bet hosts very few politics markets and our scan filters
    # by type, so these don't reach arb pipeline anyway. Listed for
    # documentation — also operator title-blacklist captures politics
    # by event title pattern.
}
WINDOW_DAYS = 13               # accept events ending within this many days
                               # (reverted 28.04.2026 from 30 → 10 for capital
                               # efficiency, then 29.04 → 13 to widen Polymarket
                               # NEAR pool — most Polymarket events resolve >10
                               # days out so 10-day cutoff was killing 97.5% of
                               # them. 13 days hits the sweet spot.)
WINDOW_PAST_DAYS = 2           # also keep events that ended up to this many days ago

DEADLINE_RE = re.compile(
    r'\b(by|before)\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|'
    r'january|february|march|april|june|july|august|september|october|november|december|'
    r'20\d{2}|end of|q[1-4])', re.IGNORECASE)

# "Other" outcome detector. Multi-outcome events with a hidden "Other" /
# "None of the above" option are vulnerable arbs: if we buy YES on A,B,C
# only and "Other" actually wins, every leg loses. We quarantine such deals
# (still show in UI for analysis, but block the executor from firing them).
# Pattern covers EN + RU phrasing seen across Polymarket / Limitless titles.
#
# Phase 9kkk (30.04.2026) — operator-found bug 30.04.2026:
# 3 events leaked into NEAR pool despite having an Other outcome:
#   - West Virginia Democratic Senate Primary Winner
#   - Nebraska Governor Republican Primary Winner
#   - NE-02 Democratic Primary Winner
# Each had a child market with `groupItemTitle='Other'` AND `question`
# starting with "Will another candidate/person be...". Two issues:
#   (a) filter_poly's `m.get('question') or m.get('groupItemTitle')`
#       short-circuits on truthy question → groupItemTitle='Other' missed.
#       FIX: pass BOTH fields to has_other_outcome (see filter_poly fix).
#   (b) OTHER_RE didn't match "another candidate / another person /
#       another option / another nominee" — added below.
OTHER_RE = re.compile(
    r'\b(other|any other|none of the above|other team|other candidate|other player|'
    r'another\s+(?:candidate|player|person|team|option|nominee|contender|entrant)|'
    r'someone\s+else|will\s+a\s+different|'
    r'прочее|другое|неопределен|любой другой|'
    r'(?:другой|иной)\s+(?:кандидат|игрок|вариант))\b',
    re.IGNORECASE)


def has_other_outcome(names):
    """True if any name matches the 'Other' pattern — see OTHER_RE comment.
    Used by both filter_poly and eval_limitless to flag deals as quarantine.

    Phase 9kkk (30.04.2026): also checks against `groupItemTitle == 'Other'`
    exact match as a safety net — Polymarket sometimes leaves the question
    in a misleading form while explicitly tagging the GT.
    """
    for n in names:
        if not n:
            continue
        s = str(n).strip()
        # Direct exact-match safety net for the most common label
        if s.lower() in ('other', 'другое', 'иное', 'остальные'):
            return True
        if OTHER_RE.search(s):
            return True
    return False


# ── threshold-series detector (Phase 9o, 28.04.2026) ────────────────
# CRITICAL guard against the Reddit-DAUq-104%-ROI bug:
# Multi-outcome events on Limitless / Polymarket sometimes encode a series
# of OVERLAPPING threshold markets ("above 65M", "above 70M", "above 75M",
# ...) under one negRisk parent. They look like multi-outcome events but
# their YES tokens are NOT mutually exclusive — if reality is 72M then YES
# above-65M AND YES above-70M both win, and NO above-75M AND NO above-80M
# both win. That breaks the core assumption of ALL_YES (exactly one YES
# wins, sum_yes ≈ $1) and ALL_NO (exactly N-1 NOs win, sum_no ≈ $N-1):
# the sum identity becomes meaningless and the radar would report a phantom
# "104% ROI" arb that, in reality, can lose the entire stake.
#
# This regex flags such events at the parent-title level so eval_limitless
# / eval_poly skip ALL_YES and ALL_NO for them. YES_NO_PAIR per child
# market is still valid (each binary market individually pays $1, regardless
# of how the parent series is structured).
# THRESHOLD_SERIES_RE + is_threshold_series extracted to
# radar.eval.polymarket (audit-28b cont 6, 28.05.2026). Re-export for
# legacy callers + tests that mock.patch.object on these names.
from radar.eval.polymarket import (  # noqa: E402,F401
    THRESHOLD_SERIES_RE, is_threshold_series,
)

HEADERS = {"Accept": "application/json"}

# ── State ───────────────────────────────────────────────────────
# Phase clean-quarantine-2 (11.05.2026) — `quarantine` field removed.
# Detection drops "Other"-outcome events upfront now (see eval_poly /
# eval_limitless guards on `is_q`/`is_quarantine`). The empty list this
# field carried after that change was misleading: a perpetual `[]` in
# scan_data + UI checks that the field exists. Drop entirely.
scan_data = {"last_scan": None, "scanning": False, "deals": [], "stats": {}, "error": None, "ws": {}}
whitelist = set()
blacklist = set()
scan_lock = threading.Lock()
candidates_global = {"poly": [], "kalshi": [], "sx": []}
cand_lock = threading.Lock()

# HOT / NEAR pools per platform.
# Each pool item is the same shape we already use in eval_*: a candidate tuple/list.
pools = {
    'poly':   {'hot': [], 'near': []},
    'kalshi': {'hot': [], 'near': []},
    'sx':     {'hot': [], 'near': []},
    'lim':    {'hot': [], 'near': []},
}
pools_lock = threading.Lock()

# Reverse index: Polymarket token_id -> candidate, used by WS callback to know
# which event to re-evaluate when a price_change arrives.
poly_token_index = {}
poly_token_index_lock = threading.Lock()

# Limitless reverse slug-index. Phase 9d (28.04.2026) — same role as
# poly_token_index: when WS pushes an orderbookUpdate on a slug, we
# look up the parent event in O(1) and re-evaluate immediately instead
# of waiting for the 5s micro-loop tick. Critical for Limitless's
# 30-min crypto-oracle markets where prices move fast in the last
# minutes before resolution. Maps BOTH child slugs (negRisk groups)
# and event-level slugs (standalone binary) to the parent event dict.
lim_slug_index = {}
lim_slug_index_lock = threading.Lock()


# Limitless per-slug metadata cache. Phase 9c (28.04.2026) — without this,
# atomic._build_leg cannot construct a real Limitless order (`tokenId`
# uint256 is required by the EIP-712 Order type; `verifyingContract` is
# required by the EIP-712 domain and varies per market venue).
#
# Shape: {slug: {'yes_token': str, 'no_token': str, 'verifying_contract':
#                str, 'volume': float, 'is_other': bool, 'fetched_at': float}}
#
# Tokens + venue.exchange are stable for a given slug (CTF condition is
# immutable once deployed) so we cache forever within a process. Volume
# changes — refresh every 5 min so HOT-pool sorting can prefer liquid markets.
lim_meta_cache = {}
lim_meta_lock = threading.Lock()
LIM_META_REFRESH_S = 300   # 5 min — only volume needs refresh; tokens stay
# Phase 9uu (29.04.2026) — hard cap to prevent unbounded growth.
# Audit: cache had TTL but no size limit; over weeks of running every slug
# ever seen accumulated. Cap at 5000 — far more than any realistic active
# market universe (Limitless typically has <2000 active markets).
LIM_META_CACHE_MAX = 5000

# Polymarket V2 per-market info cache. V2 migration moved fee/tick/min-size
# from hardcoded constants to dynamic per-market values queryable via the
# REST API. Without this, our deal builder used THETA_POLY=2.5% across the
# board — but in V2 many markets charge 0% taker fee, so we were
# REJECTING valid arbs (overpessimistic net) and signing orders with the
# wrong tick size (server would reject).
#
# Shape: {condition_id: {tick_size, min_order_size,
#                        maker_fee_bps, taker_fee_bps, neg_risk,
#                        accepting_orders, fetched_at}}
poly_market_info_cache = {}
poly_market_info_lock = threading.Lock()
POLY_MARKET_INFO_REFRESH_S = 600    # 10 min — fee changes are rare
POLY_MARKET_INFO_CACHE_MAX = 5000   # Phase 9uu — hard cap, see LIM_META_CACHE_MAX

# Last full REST clob_res cached so WS-driven re-eval can fall back to old asks
# for tokens of the same event that haven't been pushed yet.
# Also reused by /api/near to render NEAR snapshot without re-fetching.
poly_clob_cache = {}
poly_clob_cache_lock = threading.Lock()
kalshi_res_cache = {}
sx_res_cache = {}
lim_res_cache = {}
res_cache_lock = threading.Lock()

# Polymarket WS client (initialized in __main__).
ws_client = None
# Phase TS-5c (12.05.2026) — symmetric to LIMITLESS_WS_REQUIRED. When set,
# scan does NOT fall back to REST `clob.polymarket.com/book` on cache miss;
# the token_id is simply skipped for that tick. Eliminates Polymarket's
# occasional Cloudflare 403/429 (BUG_CATALOG 6.3) under heavy /book load.
# When WS is disconnected we still fall through to REST (graceful).
POLYMARKET_WS_REQUIRED = os.environ.get('POLYMARKET_WS_REQUIRED', '0') != '0'

# Polymarket user-channel WS clients — Phase 9f. One per bot wallet that
# has poly L2 creds. Maintained as a list so iteration in update_markets /
# kill / reconcile is straightforward.
poly_user_ws_clients: list = []

# Limitless WS client (initialized in __main__ when ENABLE_LIMITLESS=1).
# Same pattern as ws_client: idle until first scan classifies a HOT/NEAR pool,
# then `update_subscriptions(slugs)` triggers connect + subscribe.
lim_ws_client = None
LIMITLESS_MAX_WS_SUBS = int(os.environ.get('LIMITLESS_MAX_WS_SUBS', '250'))
# Phase TS-5a (12.05.2026) — WS-only mode for Limitless. When set, scan
# does NOT fall back to REST `/markets/{slug}/orderbook` on cache miss;
# the slug is simply skipped for that tick. Lets operator eliminate the
# Limitless rate-limit pressure (Phase 9ddd / Phase audit-2 PR #182 saga).
# When WS is disconnected we still fall through to REST regardless of
# this flag, so a WS outage degrades gracefully rather than blacking
# out the radar.
LIMITLESS_WS_REQUIRED = os.environ.get('LIMITLESS_WS_REQUIRED', '0') != '0'

# ── Helpers ─────────────────────────────────────────────────────
# Phase audit-28b cont (27.05.2026) — calc_fee extracted to radar.fees.
# Re-export for backward compat with all call sites.
from radar.fees import calc_fee  # noqa: F401,E402  re-export

def is_deadline(names):
    if len(names) < 2: return False
    return sum(1 for n in names if DEADLINE_RE.search(n)) >= len(names) * 0.5

def is_within_window(date_str=None, timestamp=None, max_days=None, past_days=None):
    """True if the event ends within `max_days` ahead OR ended within
    `past_days` behind (still resolving). Defaults read from module config
    (WINDOW_DAYS / WINDOW_PAST_DAYS), so call sites stay short."""
    if max_days is None: max_days = WINDOW_DAYS
    if past_days is None: past_days = WINDOW_PAST_DAYS
    now = datetime.now(timezone.utc)
    try:
        if timestamp:
            dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        elif date_str:
            if date_str.endswith('Z'): date_str = date_str[:-1] + '+00:00'
            elif len(date_str) == 10: date_str += 'T00:00:00+00:00'
            dt = datetime.fromisoformat(date_str)
            if not dt.tzinfo: dt = dt.replace(tzinfo=timezone.utc)
        else: return False

        diff = (dt - now).total_seconds()
        return -86400 * past_days <= diff <= 86400 * max_days
    except Exception:
        return False

# Back-compat shim — older code paths and external callers may still use this name
# Phase 14a (01.05.2026) — Gap 2 fix: adaptive grace shared helper.
# Extracted from filter_poly (lines 3030-3062) so SX Bet and Limitless can
# also reject post-resolve "zombie" events with the same logic. Without
# this, SX/Lim can produce phantom arbs for hours after match end (operator
# saw 56-min phantoms on Polymarket pre-Phase-9kkk).
def compute_adaptive_grace_minutes(duration_seconds=None, title=None):
    """Pick grace window based on event duration. Used to filter post-resolve
    zombie events. Mirrors Polymarket grace policy:
        ≤ 10 min  → 1 min   (5-min crypto)
        ≤ 1 h    → 5 min   (hourly events)
        ≤ 24 h   → 30 min  (daily — weather, daily polls)
        > 24 h   → 60 min  (multi-day — UMA dispute window)
    Falls back to title-pattern heuristic when duration unknown.
    """
    if duration_seconds is not None and duration_seconds > 0:
        if duration_seconds <= 600:
            return 1
        if duration_seconds <= 3600:
            return 5
        if duration_seconds <= 86400:
            return 30
        return 60
    # Title heuristic fallback
    title_lower = (title or '').lower()
    intraday_signals = (' 5min', '-5min', '5-min',
                        ' 1min', '-1min', '1-min',
                        'minutely', 'every 5 min', '5min crypto')
    import re as _re
    is_intraday_ampm = bool(_re.search(
        r'\b\d{1,2}(am|pm)(-\d{1,2}(am|pm))?\s*et\b', title_lower))
    if any(s in title_lower for s in intraday_signals) or is_intraday_ampm:
        return 1
    if 'highest temperature' in title_lower or 'lowest temperature' in title_lower:
        return 30
    return 30                         # safer default


def is_within_10_days(date_str=None, timestamp=None):
    return is_within_window(date_str=date_str, timestamp=timestamp)

# ── Fetchers ────────────────────────────────────────────────────
# Phase 9rr (29.04.2026) — connection-pooled HTTP sessions per host.
# Was: each _fetch_* opened a fresh TLS connection (~200ms TLS handshake +
# ~80ms request = 280ms/call). Across 600 orderbook fetches per main scan,
# that's 168s of just TLS overhead. With `requests.Session` + a sized
# HTTPAdapter, urllib3 keeps idle connections in a pool keyed by (scheme,
# host, port) and reuses them — so subsequent calls on the same host pay
# only the request RTT (~30-80ms). On 600 calls this drops the budget from
# ~120-150s to 25-45s.
#
# Per-host session lets each backend's connection failures stay isolated
# (a hung Polymarket TLS won't poison Limitless calls). Timeout is split
# (connect=3s, read=8s) so a stalled read CAN'T silently sit forever the
# way a single `timeout=5` could when SSL_read blocked in C.
#
# Pool size = MAX_WORKERS so we never starve a worker waiting for a
# connection slot.
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

def _make_session(pool_size: int, *, use_proxy: bool = False):
    """Build a per-platform requests.Session.

    `use_proxy=True` routes every request through the residential proxy
    in `PROXY_URL_DEFAULT` (operator's standing rule: anything in the
    signing/trading pipeline must go through residential). Falls back to
    direct (proxy-less) when env is unset, so dev machines without a
    proxy still work.

    Translates `socks5://` → `socks5h://` so DNS resolution happens at
    the proxy exit, not at the VPS (avoids DNS leak).
    """
    s = requests.Session()
    # Retry=0: we use our own batch_fetch deadline + chunked progressive
    # output; an internal retry storm would just compound timeouts.
    adapter = HTTPAdapter(
        pool_connections=pool_size,
        pool_maxsize=pool_size,
        max_retries=Retry(total=0, connect=0, read=0,
                          status=0, redirect=0, other=0,
                          raise_on_status=False),
        pool_block=False,
    )
    s.mount('https://', adapter)
    s.mount('http://', adapter)
    if use_proxy:
        proxy_url = (os.environ.get('PROXY_URL_DEFAULT') or '').strip()
        if proxy_url:
            # socks5:// → socks5h:// for remote DNS. Other schemes pass through.
            if proxy_url.startswith('socks5://'):
                proxy_url = 'socks5h://' + proxy_url[len('socks5://'):]
            s.proxies = {'https': proxy_url, 'http': proxy_url}
    return s

# Per-backend sessions (lazy init at first call from any worker).
# All radar-side sessions run DIRECT from the VPS. Operator's standing
# rule (clarified 2026-05-15): residential proxy is reserved for the
# physical order-placement POST in executor-ts (`postPolyOrder` /
# `postLimOrder` / `postSxFill`). Reading public orderbooks /
# market metadata / listings doesn't expose a wallet — there's no
# L2 auth on those GETs — so no IP↔wallet correlation to break.
# Routing read-only scan traffic through residential burns the
# limited bandwidth without security benefit.
_SESS_POLY = _make_session(MAX_WORKERS)
_SESS_LIM = _make_session(MAX_WORKERS)
_SESS_KALSHI = _make_session(MAX_WORKERS)
_SESS_SX = _make_session(MAX_WORKERS)
# (connect_timeout, read_timeout) — connect is the TCP+TLS handshake;
# read is bytes-flowing-from-server. Tuple form is mandatory because a
# single-int timeout in `requests` does NOT consistently fire when
# OpenSSL's SSL_read blocks in C (we observed scans hung past 8 minutes
# on what should have been a 5s requests-level timeout).
_FETCH_TIMEOUT = (3.0, 8.0)


# ── Phase 10 #51 (30.04.2026) — top-of-book depth ─────────────────
# Operator-found bug: `liquidity` reported per leg was the SUM across ALL
# orderbook levels, not just the best ask. This inflated `min_liq` 5-10x:
# a market showing "$3,865 depth" might actually have only $200 at the best
# ask and $3,665 sitting 1-3c above. With Polymarket V2 partial-fills, that
# means a $55 stake submitted at expected best price (e.g. 30c) fills $30
# at 30c then walks the book to 31c, 33c — average price hits 31.5c, blowing
# past `SLIPPAGE_TOLERANCE=0.001` and breaking the arb.
#
# For arb (limit order at expected price P): only depth AT exactly P matters.
# Anything beyond P+slippage_tolerance is a different trade; counting it as
# fillable is over-optimistic and produces phantom "low-risk" sizing.
#
# Limitless was already correct (top-of-book only via _lim_depth_usd).
# This helper makes Polymarket / Kalshi / SX Bet / poly_ws consistent.
def _top_of_book_depth_usd(asks_or_levels, slippage_tolerance: float = 0.0,
                            price_key: str = 'price', size_key: str = 'size',
                            tuple_idx_price: int = 0, tuple_idx_size: int = 1,
                            size_is_usd: bool = False):
    """Return (best_ask, top_of_book_depth_usd) from a list of asks.

    Accepts either dict-shape (`{'price','size'}` — Polymarket /book) or
    tuple-shape (`[price, size]` — Kalshi orderbook_fp). Caller picks via
    the key/idx params.

    `size_is_usd`: if True, `size` field is already a dollar notional and
    should NOT be multiplied by price. Used for Kalshi `*_dollars` levels.
    Default False = treat size as contract count (Polymarket V2 /book).

    Counts only USD notional sitting at the best ask price (or within
    `slippage_tolerance` of it — default 0 = strict exact match). If multiple
    levels share that best price (after sort), they sum together.

    Returns (None, 0.0) for empty / malformed input — never raises.
    """
    if not asks_or_levels:
        return None, 0.0
    parsed = []
    for a in asks_or_levels:
        try:
            if isinstance(a, dict):
                p = float(a.get(price_key, 999))
                s = float(a.get(size_key, 0))
            else:                                          # list/tuple
                p = float(a[tuple_idx_price])
                s = float(a[tuple_idx_size])
            if p <= 0 or s <= 0:
                continue
            parsed.append((p, s))
        except Exception:
            continue
    if not parsed:
        return None, 0.0
    parsed.sort(key=lambda x: x[0])              # ascending by price
    best = parsed[0][0]
    cutoff = best + slippage_tolerance + 1e-9
    depth_usd = 0.0
    for p, s in parsed:
        if p > cutoff:
            break
        depth_usd += s if size_is_usd else (p * s)
    return best, depth_usd


# Phase 11 (01.05.2026) — Task F: depth-within-slippage-tolerance.
# Operator request: "сейчас если best ask 34c with $50, на 34.5c есть ещё $300 —
# мы это $300 не считаем". Yes — Phase 10 #51 made depth strictly top-of-book.
# Task F relaxes the strictness to match SLIPPAGE_TOLERANCE (atomic.py, raised
# from 0.001 to 0.005). MMs stack orders 0.05-0.5c apart on liquid sport books;
# strict top-of-book under-counts 5-10x.
#
# Default 0.005 = consistent with raised SLIPPAGE_TOLERANCE: if executor allows
# fills to drift up to 0.5c, depth must reflect the same window — otherwise
# we'd see "abundant depth" but cancel half the fills via Task B slippage trigger.
DEPTH_SLIPPAGE_TOLERANCE = float(
    os.environ.get('DEPTH_SLIPPAGE_TOLERANCE', '0.005'))


# _fetch_clob extracted to radar.fetchers.polymarket
# (audit-28b cont 9, 29.05.2026). Re-export below.
from radar.fetchers.polymarket import _fetch_clob  # noqa: F401,E402

# _fetch_kalshi_ob + _fetch_sx_orders + _fetch_sx_3way_outcomes extracted
# to radar.fetchers.{kalshi,sx} (audit-28b cont 11, 29.05.2026).
from radar.fetchers.kalshi import _fetch_kalshi_ob  # noqa: F401,E402
from radar.fetchers.sx import (  # noqa: F401,E402
    _fetch_sx_orders, _fetch_sx_3way_outcomes,
)


# ── Limitless Exchange (Phase 9, 28.04.2026) ────────────────────────
# CLOB-based prediction market on Base L2 (api.limitless.exchange).
# Architecture mirrors Polymarket: YES/NO shares, $1 collateral, EIP-712
# signed orders, negRisk-style multi-outcome groups. We fetch markets +
# orderbook via REST and treat the data the same way as Polymarket
# downstream (filter → classify_pools → eval → fire). Key differences:
#   - No platform fee (only Base gas ~$0.01) → tighter THRESH_LIMITLESS=0.99
#   - Smaller volume than Polymarket (~$3M vs $110M daily) but proportionally
#     less competition, so spreads stay wider.
# _lim_depth_usd + _fetch_limitless_orderbook + _fetch_limitless_market_meta
# extracted to radar.fetchers.limitless (audit-28b cont 10, 29.05.2026).
from radar.fetchers.limitless import (  # noqa: F401,E402
    _lim_depth_usd, _fetch_limitless_orderbook, _fetch_limitless_market_meta,
)


def _safe_int_ts(v) -> int:
    """Parse a timestamp field safely. Polymarket /markets sometimes returns
    `accepting_order_timestamp` as an ISO 8601 string ('2026-04-26T23:04:30Z')
    instead of int seconds. Defensive — if parse fails, return 0 (book always
    open by default rather than crashing the cache populate)."""
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    try:
        return int(v)
    except (ValueError, TypeError):
        # Try ISO8601 parse
        try:
            from datetime import datetime
            s = str(v).replace('Z', '+00:00')
            return int(datetime.fromisoformat(s).timestamp())
        except Exception:
            return 0


# _batch_fetch_poly_market_info + _read_poly_fee_bps + _fetch_poly_market_info
# extracted to radar.fetchers.polymarket (audit-28b cont 9, 29.05.2026).
# Re-exports below preserve every caller and `mock.patch.object` site.
from radar.fetchers.polymarket import (  # noqa: F401,E402
    _batch_fetch_poly_market_info, _fetch_poly_market_info, _read_poly_fee_bps,
)


def batch_fetch(fn, ids):
    """Fan-out per-id calls onto MAX_WORKERS threads. Phase 9qq.4:
    `pool.shutdown(wait=False, cancel_futures=True)` so that a frozen
    pool actually releases. Previously `with ThreadPoolExecutor()` at
    block-exit blocked on shutdown(wait=True), waiting for hung workers
    forever — even though `as_completed(timeout=)` had raised.

    Bug chain:
      9qq.2 — outer deadline check; never fired (as_completed blocks).
      9qq.3 — as_completed(timeout=); fires, but `with` context manager
              shutdown waited for hung threads anyway.
      9qq.4 — explicit try/finally + shutdown(wait=False, cancel_futures=True).

    Hung worker threads are not killed (Python has no thread.kill); they
    continue holding sockets in the background. They'll die when the
    process restarts. In practice each scan leaks at most MAX_WORKERS=30
    zombie threads against a stuck endpoint, which the OS cleans up via
    socket TIMEOUT (a few minutes). Acceptable trade-off vs scan-forever.

    Budget: max(45s, 3s × len(ids) / MAX_WORKERS)."""
    results = {}
    if not ids: return results
    budget = max(45.0, 3.0 * len(ids) / max(1, MAX_WORKERS))
    fn_name = getattr(fn, '__name__', 'fn')
    t0 = time.time()
    pool = ThreadPoolExecutor(max_workers=MAX_WORKERS)
    try:
        futs = [pool.submit(fn, i) for i in ids]
        completed = 0
        try:
            for f in as_completed(futs, timeout=budget):
                try:
                    res = f.result(timeout=1.0)
                    results[res[0]] = res[1:]
                    completed += 1
                except Exception:
                    pass
        except (_CFTimeoutError, TimeoutError):
            pending = sum(1 for x in futs if not x.done())
            print(f"[batch_fetch:{fn_name}] timeout after "
                  f"{int(time.time() - t0)}s — "
                  f"{completed}/{len(ids)} done, {pending} pending dropped",
                  flush=True)
    finally:
        # Critical: do NOT wait for hung workers. They'll keep running
        # in the background until socket-level timeout finishes.
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except TypeError:  # Python <3.9: cancel_futures kw not supported
            pool.shutdown(wait=False)
    return results

# Phase audit-28b cont 2 (27.05.2026) - build_deal extracted to
# radar.build_deal. Re-export below preserves callers.
from radar.build_deal import build_deal  # noqa: F401,E402  re-export

# ── Evaluate Candidates ────────────────────────────────────────
# _poly_per_market + _attach_poly_v2_meta + _eval_poly_structures + eval_poly
# extracted to radar.eval.polymarket (audit-28b cont 6, 28.05.2026).
# Re-exports below preserve every caller / mock.patch site.
from radar.eval.polymarket import (  # noqa: E402,F401
    _poly_per_market, _attach_poly_v2_meta,
    _eval_poly_structures, eval_poly,
)



def eval_kalshi(cands, kalshi_res):
    """Evaluate all three arb structures for Kalshi events:
        A. ALL_YES — sum(yes_ask) < THRESH_KALSHI
        B. ALL_NO  — sum(no_ask)  < (N-1) * THRESH_KALSHI  (multi-outcome only)
        C. YES_NO_PAIR (per market): yes_ask + no_ask < THRESH_KALSHI
    """
    deals = []
    for cand in cands:
        ev, tickers = cand
        # Kalshi event-level close_time, fallback to per-market field below
        end_date = ev.get('close_time') or ev.get('expected_expiration_time')
        # Phase 9g coverage gate — track total outcomes vs priced
        total_outcomes_on_event = len(ev.get('markets') or [])
        per_market = []
        for m in ev.get('markets', []):
            t = m.get('ticker','')
            if t not in kalshi_res: continue
            yes_ask, yes_depth, no_ask, no_depth = kalshi_res[t]
            if yes_ask is None or yes_ask < 0.05 or yes_ask >= 1: continue
            per_market.append({
                'name': m.get('title', t), 'ticker': t,
                'yes_price': yes_ask, 'yes_liq': yes_depth,
                'no_price': no_ask if (no_ask and 0 < no_ask < 1) else None,
                'no_liq': no_depth or 0,
                # Per-market close_time (Kalshi sometimes has both)
                'end_date': m.get('close_time') or end_date,
            })
        if len(per_market) < 2: continue
        full_coverage = (len(per_market) == total_outcomes_on_event)

        # ── A. ALL_YES ──────────────────────────────────────────────
        # Coverage required — uncovered outcome winning kills the arb.
        yes_outcomes = [{'name': p['name'], 'price': p['yes_price'],
                         'liquidity': p['yes_liq'], 'source': 'kalshi_ob'}
                        for p in per_market]
        total_yes = sum(o['price'] for o in yes_outcomes)
        if (full_coverage and 0.50 <= total_yes < THRESH_KALSHI
                and any(o['price'] > 0.20 for o in yes_outcomes)):
            d = build_deal(ev.get('title','?'), 'Kalshi', yes_outcomes,
                           total_yes, THETA_KALSHI, THRESH_KALSHI)
            if d:
                d['arb_structure'] = 'all_yes'; d['end_date'] = end_date
                deals.append(d)

        # ── B. ALL_NO (N>=3) — coverage required ────────────────────
        no_raw = [p for p in per_market if p['no_price'] is not None]
        N = len(no_raw)
        if N >= 3 and N == total_outcomes_on_event:
            no_outcomes = [{'name': f"NO {p['name']}", 'price': p['no_price'],
                            'liquidity': p['no_liq'], 'source': 'kalshi_ob'}
                           for p in no_raw]
            total_no = sum(o['price'] for o in no_outcomes)
            no_threshold = (N - 1) * THRESH_KALSHI
            if total_no < no_threshold:
                # Phase 9i: payout_target=N-1 for ALL_NO (see build_deal docstring)
                d = build_deal(ev.get('title','?') + ' (ALL_NO)', 'Kalshi',
                               no_outcomes, total_no, THETA_KALSHI, no_threshold,
                               payout_target=float(N - 1))
                if d:
                    d['arb_structure'] = 'all_no'; d['payout_target'] = N - 1
                    d['end_date'] = end_date
                    deals.append(d)

        # ── C. YES_NO_PAIR ──────────────────────────────────────────
        for p in per_market:
            if p['no_price'] is None: continue
            pair_total = p['yes_price'] + p['no_price']
            if pair_total >= THRESH_KALSHI: continue
            pair_out = [
                {'name': f"YES {p['name']}", 'price': p['yes_price'],
                 'liquidity': p['yes_liq'], 'source': 'kalshi_ob'},
                {'name': f"NO {p['name']}", 'price': p['no_price'],
                 'liquidity': p['no_liq'], 'source': 'kalshi_ob'},
            ]
            d = build_deal(p['name'], 'Kalshi', pair_out, pair_total,
                           THETA_KALSHI, THRESH_KALSHI)
            if d:
                d['arb_structure'] = 'yes_no_pair'
                d['end_date'] = p.get('end_date')
                deals.append(d)
    return deals

def _sx_market_title(m: dict) -> str:
    """Pretty title that disambiguates Moneyline vs Total vs Spread for the
    same matchup. Uses outcomeOneName/outcomeTwoName which already carry
    Over/Under and ±line annotations."""
    league = m.get('leagueLabel', '')
    o1 = m.get('outcomeOneName', m.get('teamOneName', 'Team 1'))
    o2 = m.get('outcomeTwoName', m.get('teamTwoName', 'Team 2'))
    return f"{o1} vs {o2} ({league})" if league else f"{o1} vs {o2}"

# ── Phase 14b (01.05.2026): cross-platform PlatformOutcome builders ──
# Convert per-platform scan results into the unified PlatformOutcome shape
# that cross_platform.py expects. Three builders, one per platform.
# These are used ONLY when CROSS_PLATFORM_ENABLED=1; otherwise scan loop
# skips them entirely. Standalone helpers — pure functions, no side effects.

def _build_cp_outcomes_polymarket(pc, clob_res):
    """Polymarket per-event → list[PlatformOutcome]. One outcome per child
    market (binary or negRisk). Uses clob_ask sources only."""
    try:
        from cross_platform import PlatformOutcome
    except ImportError:
        return []
    out = []
    for cand in pc:
        try:
            ev = cand[0] if isinstance(cand, tuple) else cand.get('ev')
            rough = cand[1] if isinstance(cand, tuple) and len(cand) > 1 else None
            if not isinstance(ev, dict) or not rough:
                continue
            title = ev.get('title') or '?'
            end_date = ev.get('endDate') or ev.get('endTime')
            for o in rough:
                yes_tid = o.get('token_id_yes') or o.get('token_id')
                no_tid = o.get('token_id_no')
                yes_ask, ask_depth, yes_bid, bid_depth = (None, 0, None, 0)
                no_ask, no_depth = (None, 0)
                if yes_tid and yes_tid in clob_res:
                    res = clob_res[yes_tid]
                    if len(res) >= 4:
                        yes_ask, ask_depth, yes_bid, bid_depth = res[:4]
                if no_tid and no_tid in clob_res:
                    res = clob_res[no_tid]
                    if len(res) >= 2:
                        no_ask, no_depth = res[:2]
                # Synthetic NO from YES bid (Phase 12 Task A)
                no_src = 'clob_ask'
                if no_ask is None and yes_bid is not None and 0 < yes_bid < 1:
                    no_ask = 1 - yes_bid
                    no_depth = bid_depth or 0
                    no_src = 'clob_synthetic'
                outcome_name = (o.get('m', {}).get('groupItemTitle')
                                or o.get('m', {}).get('question') or 'OUT')
                # Phase audit-2 (11.05.2026) — carry platform-specific
                # identifiers in `extras` so `_leg_platform_ids` in
                # cross_platform.py can stamp them onto leg dicts, which
                # `_fire_arb_via_ts` then forwards to the TS executor.
                # Without these, TS `buildLeg` threw "polymarket leg
                # requires tokenId" on every CP fire.
                cond_id = o.get('m', {}).get('conditionId')
                poly_info = (poly_market_info_cache.get(cond_id)
                              if cond_id else None) or {}
                extras = {
                    'token_id_yes': yes_tid,
                    'token_id_no': no_tid,
                    'condition_id': cond_id,
                    'neg_risk': poly_info.get('neg_risk') or False,
                    'tick_size': poly_info.get('tick_size') or 0.01,
                }
                out.append(PlatformOutcome(
                    platform='Polymarket',
                    event_id=str(cond_id or yes_tid or '?'),
                    outcome_name=outcome_name,
                    yes_price=yes_ask, yes_depth=ask_depth or 0,
                    yes_source='clob_ask' if yes_ask else 'implied',
                    no_price=no_ask, no_depth=no_depth or 0,
                    no_source=no_src if no_ask else 'implied',
                    end_date=end_date, title=title,
                    extras=extras,
                ))
        except Exception:
            continue
    return out


def _build_cp_outcomes_limitless(events, lim_res):
    """Limitless events → list[PlatformOutcome]."""
    try:
        from cross_platform import PlatformOutcome
    except ImportError:
        return []
    out = []
    for ev in events:
        try:
            title = ev.get('title') or ev.get('proxyTitle') or '?'
            end_date = ev.get('deadline') or ev.get('expirationTimestamp')
            children = ev.get('markets') or [ev]
            for c in children:
                slug = c.get('slug') or c.get('address')
                if not slug or slug not in lim_res:
                    continue
                yes_ask, yes_depth, no_ask, no_depth = lim_res[slug]
                outcome_name = c.get('title') or c.get('proxyTitle') or 'OUT'
                # Phase audit-2 (11.05.2026) — pull yes_token/no_token/
                # verifying_contract from lim_meta_cache (filter_limitless
                # populated it at scan time). Without these the TS
                # executor throws "limitless leg requires tokenId + slug".
                #
                # Phase audit-3 (15.05.2026) — on cache miss, fetch
                # on-demand. The first real-mode fire revealed that some
                # CP-eligible markets aren't in `lim_meta_cache` at fire
                # time (eval_limitless populates it only for markets that
                # entered the HOT/NEAR pool, but CP detection runs on a
                # broader pool from `_build_cp_outcomes_limitless`). Result:
                # the Limitless leg's token_id was never set on the deal,
                # TS rejected with "limitless leg requires tokenId + slug",
                # arb died with no real position taken.
                with lim_meta_lock:
                    meta = lim_meta_cache.get(slug)
                if not isinstance(meta, dict):
                    # On-demand fetch — also populates the cache for future hits.
                    meta = _fetch_limitless_market_meta(slug) or {}
                extras = {'slug': slug}
                if isinstance(meta, dict):
                    extras['token_id_yes'] = meta.get('yes_token')
                    extras['token_id_no'] = meta.get('no_token')
                    extras['verifying_contract'] = meta.get('verifying_contract')
                # Phase audit-2 (11.05.2026) — format Limitless deadline
                # to ISO 8601. `ev.get('deadline')` is unix ms (numeric or
                # numeric string). Old `str(end_date)` left "1779103800000"
                # which dashboard's fmtDate can't parse → showed "—" in
                # the "Резолв" column for every Limitless+SX deal.
                # Polymarket already gives ISO; SX is formatted in
                # _build_cp_outcomes_sx; this aligns Limitless.
                end_date_iso = None
                if end_date is not None:
                    try:
                        # epoch ms (int or numeric string) → ISO UTC
                        ts_ms = int(float(end_date))
                        if ts_ms > 0:
                            end_date_iso = datetime.fromtimestamp(
                                ts_ms / 1000.0, tz=timezone.utc).isoformat()
                    except (TypeError, ValueError):
                        # Already-formatted string (rare) — pass through
                        end_date_iso = str(end_date) if end_date else None
                out.append(PlatformOutcome(
                    platform='Limitless', event_id=slug,
                    outcome_name=outcome_name,
                    yes_price=yes_ask, yes_depth=yes_depth or 0,
                    yes_source='lim_clob' if yes_ask else 'implied',
                    no_price=no_ask, no_depth=no_depth or 0,
                    no_source='lim_clob' if no_ask else 'implied',
                    end_date=end_date_iso,
                    title=title,
                    extras=extras,
                ))
        except Exception:
            continue
    return out


def _build_cp_outcomes_sx(markets, sx_res):
    """SX Bet markets → list[PlatformOutcome] (each market has 2 outcomes)."""
    try:
        from cross_platform import PlatformOutcome
    except ImportError:
        return []
    out = []
    for m in markets:
        try:
            mh = m.get('marketHash')
            if not mh or mh not in sx_res:
                continue
            best1, depth1, best2, depth2 = sx_res[mh]
            if best1 is None or best2 is None:
                continue
            title = _sx_market_title(m)
            game_ts = m.get('gameTime')
            end_date = (datetime.fromtimestamp(game_ts, tz=timezone.utc).isoformat()
                        if isinstance(game_ts, (int, float)) and game_ts > 0
                        else None)
            # SX is binary — 2 outcomes per market. Convention: outcome1=YES on
            # outcomeOneName, outcome2=NO on it (= YES on outcomeTwoName).
            # Phase audit-2 (11.05.2026) — carry market_hash + outcome
            # names in extras so `_leg_platform_ids` can map side → index.
            extras = {
                'market_hash': mh,
                'outcome_one_name': m.get('outcomeOneName'),
                'outcome_two_name': m.get('outcomeTwoName'),
            }
            out.append(PlatformOutcome(
                platform='SX Bet', event_id=mh,
                outcome_name=m.get('outcomeOneName', 'Team A'),
                yes_price=best1, yes_depth=depth1 or 0, yes_source='sx_ob',
                no_price=best2, no_depth=depth2 or 0, no_source='sx_ob',
                end_date=end_date, title=title,
                extras=extras,
            ))
        except Exception:
            continue
    return out


# Phase audit-28b cont (27.05.2026) — filter_sx extracted to
# radar.filters.sx. Re-export below preserves callers.
from radar.filters.sx import filter_sx  # noqa: F401,E402  re-export


# Phase 17 (01.05.2026) — SX 3-way (1X2) pipeline.
# Soccer 1X2 markets (type=1) have 3 outcomes: home/draw/away. Each is a
# separate maker-orderbook on SX. To find ALL_YES arb we sum 3 best taker
# prices and compare to threshold. If sum < THRESH_SX_3WAY → arb.
SX_THREE_WAY_TYPES = {1}        # type=1 soccer 1X2; expand if more types added
THRESH_SX_3WAY = 0.97 - 0.005 - 0.003   # taker fee + slippage reserve buffer


# _fetch_sx_3way_outcomes extracted to radar.fetchers.sx (cont 11 above).


def eval_sx_3way(sx_markets, sx_orders):
    """Evaluate 3-way 1X2 markets for ALL_YES arb. Currently STUB — full
    implementation pending SX 3-way orderbook semantics.

    Returns deals list (currently always empty until 3rd outcome data path
    is implemented). Operator-flagged but not blocking — type=1 stays
    excluded via SX_EXCLUDED_TYPES until this is wired.
    """
    deals = []
    for m in sx_markets:
        if m.get('type') not in SX_THREE_WAY_TYPES:
            continue
        # Future: fetch 3 outcomes' best taker prices, sum, compare to threshold.
        # For now: stub — log and skip.
        # When implemented, build_deal with 3 outcomes + structure='all_yes_3way'.
        pass
    return deals


def eval_sx(sx_markets, sx_orders):
    """One deal per market (by marketHash), not per event. A single match
    can have Moneyline + Total + Spread + Period markets — each is an
    independent binary arb opportunity, so we evaluate them separately.

    Phase 9kkk (30.04.2026) — SX filter parity with Polymarket:
      * status filter: drop closed/resolved/cancelled markets (was missing).
        SX Bet's `status` field is 1=open/2=closed/3=settled/4=resolved/cancelled.
      * 13-day window via is_within_window (was hardcoded shim 10).
      * type=1 (3-way soccer with Draw) — STILL excluded via SX_BINARY_TYPES;
        when we add 3-way pipeline, also ensure status filter survives.
    """
    deals = []
    seen_hashes = set()
    for m in sx_markets:
        if m.get('type') not in SX_BINARY_TYPES: continue
        mh = m.get('marketHash', '')
        if not mh or mh in seen_hashes: continue
        seen_hashes.add(mh)

        # Phase 9kkk: drop closed/resolved/cancelled markets.
        # SX Bet `status` = 1 (active) / 2 (paused/halted) / 3 (settled) /
        # 4 (resolved/cancelled). Anything except 1 is unfillable.
        # Phase 12b (01.05.2026) — Bug 2 fix: was "fail-OPEN on missing
        # status", now fail-CLOSED. SX Bet API never legitimately returns
        # markets without `status`. Old behavior accepted paused markets
        # → potential dry-fire on unfillable book.
        # Phase 19v9 (03.05.2026) — accept string 'ACTIVE' too: SX API
        # changed format to string status. (filter_sx fix mirrored here.)
        status = m.get('status')
        if status not in (1, 'ACTIVE', 'active'):
            continue
        # Also check `reportedDate` / `outcome` — if outcome != 0 it's settled.
        if m.get('outcome') is not None and m.get('outcome') != 0:
            continue

        # Phase 9kkk: use unified WINDOW_DAYS=13 instead of legacy 10-day shim.
        # is_within_10_days is an alias for is_within_window with default
        # 13-day cutoff (Phase 9v 29.04.2026).
        if not is_within_10_days(timestamp=m.get('gameTime')): continue

        # Phase 14a (01.05.2026) — Gap 2 fix: adaptive post-resolve grace.
        # Without this, SX market that ended 30 min ago can still produce
        # phantom arbs (orderbook lingers until 13-day cutoff). Same grace
        # policy as Polymarket filter_poly.
        game_ts = m.get('gameTime')
        if isinstance(game_ts, (int, float)) and game_ts > 0:
            now_ts = time.time()
            age_seconds = now_ts - game_ts
            if age_seconds > 0:                        # match has ended
                # SX doesn't expose start-time consistently; use title heuristic
                title = _sx_market_title(m)
                grace_min = compute_adaptive_grace_minutes(
                    duration_seconds=None, title=title)
                if (age_seconds / 60) > grace_min:
                    continue

        if mh not in sx_orders: continue
        best1, depth1, best2, depth2 = sx_orders[mh]
        if best1 is None or best2 is None: continue
        if best1 <= 0 or best2 <= 0: continue
        total = best1 + best2
        if total >= THRESH_SX: continue
        outcomes = [
            {'name': m.get('outcomeOneName', 'Team 1'), 'price': best1, 'liquidity': depth1, 'source': 'sx_ob'},
            {'name': m.get('outcomeTwoName', 'Team 2'), 'price': best2, 'liquidity': depth2, 'source': 'sx_ob'},
        ]
        deal = build_deal(_sx_market_title(m), 'SX Bet', outcomes, total, THETA_SX, THRESH_SX)
        if deal:
            # SX Bet markets are inherently binary (outcomeOne vs outcomeTwo).
            # All three arb structures collapse to the same shape here.
            deal['arb_structure'] = 'binary'
            # SX gameTime is unix-seconds; normalise to ISO-8601 for analytics
            game_ts = m.get('gameTime')
            if isinstance(game_ts, (int, float)) and game_ts > 0:
                deal['end_date'] = datetime.fromtimestamp(game_ts, tz=timezone.utc).isoformat()
            deals.append(deal)
    return deals


# Phase audit-28b cont (27.05.2026) — filter_limitless extracted to
# radar.filters.limitless. Re-export below preserves callers.
from radar.filters.limitless import filter_limitless  # noqa: F401,E402  re-export

# Phase audit-28b cont 7 (28.05.2026) — eval_limitless + _lim_quality_ok
# extracted to radar.eval.limitless. Re-exports preserve all call sites
# and `mock.patch.object(arb_server, 'eval_limitless', X)` patterns.
from radar.eval.limitless import (  # noqa: F401,E402
    _lim_quality_ok, eval_limitless,
)


# Phase audit-28b cont 8 (28.05.2026) — pool classification moved to
# radar.eval.pools. Re-exports below preserve every call site +
# `mock.patch.object(arb_server, '_sum_*', X)` patterns.
from radar.eval.pools import (  # noqa: F401,E402
    _sum_limitless_cand, _sum_poly_cand, _sum_kalshi_cand, _sum_sx_market,
    classify_pools, _best_near_structure, near_summary,
    C_NEAR_MAX_DISTANCE,
)


# ── Single-candidate re-eval (used by WS callback + classification) ──
def _poly_outcomes_from_cand(cand, clob_res, ws_books):
    """[Legacy YES-only] Reconstruct YES-side outcomes list. Kept for
    backwards-compat (NEAR summary, _sum_poly_cand). New code paths go
    through _poly_per_market which returns both YES and NO."""
    _ev, rough, _is_q = cand
    pm = _poly_per_market(rough, clob_res, ws_books)
    return [{'name': p['name'], 'price': p['yes_price'],
             'liquidity': p['yes_liq'], 'source': p['yes_src'],
             'volume': p['volume']} for p in pm]

def _eval_poly_one(cand, clob_res=None, ws_books=None):
    """Returns list of deals across all 3 arb structures (A/B/C). Empty list if
    none cross threshold. Pure function — no globals touched. Used by both
    eval_poly (batch) and the WS callback (single-token push)."""
    return _eval_poly_structures(cand, clob_res=clob_res, ws_books=ws_books)

# Pool classification + NEAR summary moved to radar.eval.pools (audit-28b cont 8).

def rebuild_poly_token_index(poly_pool):
    """token_id -> candidate, for WS callback reverse lookup.
    Maps both YES and NO tokens so a price update on either side triggers
    re-evaluation of all 3 arb structures (A/B/C)."""
    idx = {}
    for cand in poly_pool['hot'] + poly_pool['near']:
        _ev, rough, _ = cand
        for o in rough:
            yes_tid = o.get('token_id_yes') or o.get('token_id')
            no_tid = o.get('token_id_no')
            if yes_tid: idx[yes_tid] = cand
            if no_tid: idx[no_tid] = cand
    return idx


def rebuild_lim_slug_index(lim_pool):
    """slug -> parent event, for WS callback reverse lookup. Maps:
      - For negRisk groups: each child slug → parent event dict
      - For standalone binary: event slug → same event dict
    So a single WS push on any slug surfaces the right event for re-eval.
    """
    idx = {}
    for ev in (lim_pool.get('hot', []) + lim_pool.get('near', [])):
        children = ev.get('markets') or []
        if children:
            for c in children:
                s = c.get('slug') or c.get('address')
                if s: idx[s] = ev
        else:
            s = ev.get('slug') or ev.get('address')
            if s: idx[s] = ev
    return idx

# ── WS push callback ────────────────────────────────────────────
# on_ws_update + on_lim_ws_update extracted to radar.ws.callbacks
# (audit-28b cont 12, 29.05.2026). Re-exported so WS clients keep
# binding via `arb_server.on_ws_update`.
from radar.ws.callbacks import on_ws_update, on_lim_ws_update  # noqa: F401,E402

# ── Filter Candidates ──────────────────────────────
# Phase audit-28b cont (27.05.2026) — `filter_poly` extracted to
# `radar.filters.polymarket`. Re-export below preserves callers.
from radar.filters.polymarket import filter_poly  # noqa: F401,E402  re-export



# Phase audit-28b (27.05.2026) — `filter_kalshi` extracted to
# `radar.filters.kalshi`. The function below is the same callable,
# re-exported so all existing call sites (`from arb_server import
# filter_kalshi` etc.) keep working. The body lives in the new module.
from radar.filters.kalshi import filter_kalshi  # noqa: F401,E402  re-export

# ═══════════════════════════════════════════════════════════════
# MAIN SCAN — 300 Poly + 200 Kalshi + 200 SX Bet = 700 events
# ═══════════════════════════════════════════════════════════════
RUN_SCAN_BUDGET_S = float(os.environ.get('RUN_SCAN_BUDGET_S', '120'))

# Phase audit-2 (11.05.2026) — scan-tick durations for /api/scan_health.
# Operator wants to know "how long does one full scan take" so the
# operator-facing pipeline timing reflects the dominant latency factor
# (scan HTTP polling), not just the executor dispatch. Bounded ring
# buffer; lock-free reads OK because we never resize after init.
from collections import deque as _scan_deque
_scan_tick_durations_ms: _scan_deque = _scan_deque(maxlen=50)
# Phase audit-2 (12.05.2026) — per-platform stage durations for one scan.
# Stored alongside the total tick so /api/scan_health can show "scan took
# 95s — poly=60, lim=20, sx=15" instead of just the total. Operator's
# pain: knowing TOTAL doesn't tell you which platform is slow.
_scan_breakdown_buffer: _scan_deque = _scan_deque(maxlen=50)
_scan_tick_lock = threading.Lock()


def _record_scan_tick(elapsed_s: float, stages: dict = None) -> None:
    """Push one scan duration + optional per-stage breakdown. Stages dict
    keys are arbitrary (e.g. 'poly_ms', 'lim_ms', 'sx_ms', 'eval_ms');
    values are floats in milliseconds. Stages missing from a given tick
    count as zero in the stats — defensive against partial scans (early
    bail on budget exceeded) where one platform never ran.

    Safe to call from run_scan completion.
    """
    try:
        with _scan_tick_lock:
            _scan_tick_durations_ms.append(round(elapsed_s * 1000.0, 1))
            if stages:
                _scan_breakdown_buffer.append({
                    k: round(float(v), 1) for k, v in stages.items()
                    if isinstance(v, (int, float)) and v >= 0
                })
            else:
                _scan_breakdown_buffer.append({})
    except Exception:
        pass


def _scan_tick_stats(include_series: bool = False) -> dict:
    """Snapshot of recent scan-tick durations (ms). Returns p50/p90/p99/
    mean/min/max/last/count from the ring buffer.

    Phase TS-5a sparkline (12.05.2026) — when `include_series=True`, the
    raw chronological values are returned under `series` (oldest→newest).
    This lets the dashboard (or any consumer) draw an inline sparkline
    showing the scan-time trend at a glance, without needing a separate
    endpoint. Default is False to keep /api/scan_health backwards-compatible
    when callers don't ask for it.
    """
    with _scan_tick_lock:
        vals = list(_scan_tick_durations_ms)
    if not vals:
        out = {'count': 0, 'p50': None, 'p90': None, 'p99': None,
                'mean': None, 'min': None, 'max': None, 'last': None}
        if include_series:
            out['series'] = []
        return out
    sv = sorted(vals)
    n = len(sv)
    def pct(p):
        k = max(0, min(n - 1, int(round((p / 100.0) * (n - 1)))))
        return sv[k]
    out = {
        'count': n,
        'p50': pct(50), 'p90': pct(90), 'p99': pct(99),
        'mean': round(sum(vals) / n, 1),
        'min': sv[0], 'max': sv[-1],
        'last': vals[-1],
    }
    if include_series:
        out['series'] = list(vals)
    return out


def _scan_breakdown_stats(include_series: bool = False) -> dict:
    """Per-stage p50/p99 across recent scan ticks. Returns:
        {
          'count': N,
          'stages': {
            'poly_ms': {p50, p90, p99, mean, last [, series]},
            'lim_ms':  {p50, p90, p99, mean, last [, series]},
            'sx_ms':   {p50, p90, p99, mean, last [, series]},
            ...
          },
          'last': {poly_ms: ..., lim_ms: ..., sx_ms: ...},
        }
    Stages that never appeared in any sample are omitted.

    Phase TS-5a sparkline (12.05.2026) — when `include_series=True`, each
    stage gets a `series` field with the chronological values for that
    stage (sparse: only ticks that recorded it). Use case: sparkline
    showing per-platform timing trend.
    """
    with _scan_tick_lock:
        rows = list(_scan_breakdown_buffer)
    if not rows:
        return {'count': 0, 'stages': {}, 'last': {}}
    # Union of stage keys observed across all rows
    all_keys = set()
    for r in rows:
        all_keys.update(r.keys())
    out_stages = {}
    for key in sorted(all_keys):
        vals = [r[key] for r in rows if key in r]
        if not vals:
            continue
        sv = sorted(vals)
        n = len(sv)
        def pct(p):
            k = max(0, min(n - 1, int(round((p / 100.0) * (n - 1)))))
            return sv[k]
        out_stages[key] = {
            'count': n,
            'p50': pct(50), 'p90': pct(90), 'p99': pct(99),
            'mean': round(sum(vals) / n, 1),
            'last': vals[-1] if vals else None,
        }
        if include_series:
            out_stages[key]['series'] = list(vals)
    return {
        'count': len(rows),
        'stages': out_stages,
        'last': dict(rows[-1]) if rows else {},
    }


def run_scan():
    with scan_lock:
        scan_data['scanning'] = True; scan_data['error'] = None
    stats = {'poly_events':0, 'kalshi_events':0, 'sx_markets':0,
             'poly_neg_risk':0, 'clob_fetched':0, 'kalshi_ob_fetched':0,
             'arb_found':0, 'scan_type': 'MAIN'}
    t0 = time.time()
    # Phase 9rr (29.04.2026) — wall-clock budget on the entire scan.
    # Even with batch_fetch deadlines and Session pooling, a backend
    # outage (Polymarket DDoS-protection cooldown, Limitless API rolling
    # restart, Cloudflare rate-limit) can stretch a single chunk past
    # its budget. Without an outer wall, scan_loop would never get to
    # call run_scan() again — UI stays "scanning…" forever. With the
    # wall, after RUN_SCAN_BUDGET_S we bail with whatever partial pools
    # we collected, return, and scan_loop's normal interval restarts
    # the cycle. Default 120s = generous but bounded.
    scan_deadline = t0 + RUN_SCAN_BUDGET_S
    def _budget_left():
        return max(0.0, scan_deadline - time.time())
    def _budget_exceeded():
        return time.time() > scan_deadline
    try:
        print(f"\n{'='*50}")
        print(f"[MAIN] Start {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC")

        # Phase 9qq (29.04.2026) — Progressive (chunked) scan.
        # Old flow: fetch ALL Polymarket pages → filter all → batch ALL
        # orderbooks → eval all → push to UI at end of run_scan(). Total
        # 60-90s before the user saw a single deal, even though the first
        # arbs were detectable after just 1-2 pages of the most-liquid
        # markets. New flow: process Polymarket and Limitless in chunks
        # of POLY_CHUNK_PAGES / LIMITLESS_CHUNK_PAGES pages. After each
        # chunk: filter → fetch orderbooks → eval → MERGE into running
        # totals → push partial scan_data['deals'/'quarantine'/'stats'].
        # UI auto-refresh sees results progressively.
        #
        # Correctness: each event is independent (eval_poly / eval_limitless
        # operate per-event), and chunks don't share events (each gamma
        # offset returns disjoint events). So the final aggregated result
        # is identical to the old single-shot path.
        running_poly_events = []
        running_pc = []
        running_clob_res = {}
        running_lim_events = []
        running_lim_res = {}
        running_deals = []
        # Phase clean-quarantine-2 (11.05.2026) — running_quarantine removed.
        # eval_poly / eval_limitless now drop "Other"-outcome events at
        # detection (continue instead of marking). The arrays that used
        # to collect quarantine deals always end up empty.

        def _push_partial(phase_label):
            """Update scan_data with running totals so UI sees progress.
            Reclassifies pools from running accumulators (cheap — pure
            Python, no network). Only writes scan_data + pools + caches;
            does NOT touch WS subscriptions (those churn at end-of-scan)."""
            partial_pools = classify_pools(
                running_pc, [], [], running_clob_res, {}, {},
                lim_events=running_lim_events, lim_res=running_lim_res,
                ws_books={},
            )
            stats['pool_poly_hot']  = len(partial_pools['poly']['hot'])
            stats['pool_poly_near'] = len(partial_pools['poly']['near'])
            stats['pool_lim_hot']   = len(partial_pools['lim']['hot'])
            stats['pool_lim_near']  = len(partial_pools['lim']['near'])
            stats['arb_found']      = len(running_deals)
            deals_sorted = sorted(running_deals,
                                  key=lambda d: d['net'], reverse=True)
            with scan_lock:
                scan_data['deals'] = deals_sorted
                scan_data['stats'] = dict(stats)
                scan_data['last_scan'] = datetime.now(timezone.utc).isoformat()
                scan_data['progress'] = phase_label
            with pools_lock:
                pools['poly'] = partial_pools['poly']
                pools['lim']  = partial_pools['lim']
            with poly_clob_cache_lock:
                poly_clob_cache.update(running_clob_res)
            with res_cache_lock:
                lim_res_cache.update(running_lim_res)

        # ───── Phase 18 (02.05.2026): parallel SX + Limitless prefetch ─────
        # Polymarket processing dominates wall time (60-120s due to per-token
        # /book fetches). SX (~3s) and Limitless (~1s with HTTP/2) can run in
        # parallel background threads while Poly chews through its chunks.
        # By the time we reach the SX/Lim sections below, the futures resolve
        # immediately — saving sequential 4-30s every scan.
        #
        # Critical: each platform writes ONLY to its own future-result; main
        # scan reads results when it gets to its section. No locks needed —
        # results are consumed once after the future is done.
        from concurrent.futures import ThreadPoolExecutor as _TPE
        _bg_pool = _TPE(max_workers=2, thread_name_prefix='prefetch')
        _sx_future = None
        _lim_future = None
        # Phase audit-3 (12.05.2026): capture submit timestamps so per-stage
        # scan_breakdown_ms reports wall-clock from submit→consume, not just
        # consume time (which is microseconds when BG worker finishes before
        # main scan reaches the section).
        _sx_submit_ts = None
        _lim_submit_ts = None
        if os.environ.get('ASYNC_FETCH') == '1':
            if ENABLE_SX:
                try:
                    from async_fetchers import run_fetch_sx_markets
                    _sx_submit_ts = time.time()
                    _sx_future = _bg_pool.submit(
                        run_fetch_sx_markets,
                        SX_PAGE_SIZE, SX_MAX_PAGES_MAIN)
                except Exception as e:
                    print(f"[SX] prefetch submit failed: {e}", flush=True)
            if ENABLE_LIMITLESS:
                try:
                    from async_fetchers import run_fetch_limitless_pages
                    _lim_submit_ts = time.time()
                    _lim_future = _bg_pool.submit(
                        run_fetch_limitless_pages,
                        LIMITLESS_PAGE_SIZE, LIMITLESS_MAIN_PAGES, 20)
                except Exception as e:
                    print(f"[LIM] prefetch submit failed: {e}", flush=True)

        # ───── Polymarket: chunked fetch+filter+eval ─────
        # Phase 9ii: no `end_date_max` (it filters umbrella endDate, not
        # child trading deadlines — see commit history). Plain offset
        # pagination, top-by-volume. We rely on is_within_window in
        # filter_poly to drop long-term events.
        t_poly = time.time()
        if ENABLE_POLY:
            # ── Phase 18 (02.05.2026): parallel /events fetcher ───────
            # Empirical (live test from VPS): 15 pages parallel via HTTP/2 in
            # ~0.5s, all 200 OK. Cloudflare limit 500/10s for /events; we use
            # max_concurrent=10 (5× headroom). Falls back to per-chunk
            # sequential fetch if async path unavailable / errors.
            _all_poly_events = None
            if os.environ.get('ASYNC_FETCH') == '1':
                try:
                    from async_fetchers import run_fetch_poly_events_pages
                    _all_poly_events = run_fetch_poly_events_pages(
                        page_size=500, max_pages=POLY_MAIN_PAGES,
                        max_concurrent=10,
                    )
                    print(f"[POLY] parallel fetch done: "
                          f"{len(_all_poly_events)} events", flush=True)
                except Exception as e:
                    print(f"[POLY] parallel fetch failed ({e}), "
                          f"fallback to sequential", flush=True)
                    _all_poly_events = None

            # ── Phase 19v4 (02.05.2026) — big batch /book RE-ENABLED ───
            # Phase 19v4 fixed root cause of _push_partial hang
            # (`classify_pools` parallel /markets via ThreadPoolExecutor).
            # Big batch /book itself worked fine (24s for 3000 tokens) —
            # was being unfairly blamed for chunk hangs. Now re-enabled.
            _all_clob = None
            if _all_poly_events is not None and os.environ.get('ASYNC_FETCH') == '1':
                try:
                    _t_pre = time.time()
                    _, _all_tids = filter_poly(_all_poly_events, diag=None)
                    if _all_tids:
                        with scan_lock:
                            scan_data['progress'] = (
                                f"polymarket fetching {len(_all_tids)} books…")
                        # Phase 19v11 (04.05.2026) — WS-first: skip REST для
                        # tokens с активным WS book (Polymarket WS уже
                        # subscribed на ~1000 HOT/NEAR tokens). На каждый
                        # WS hit save ~10-30ms × 1000 = 10-30с network +
                        # parsing time. Fall back to REST для cold tokens.
                        #
                        # Phase 19v13 (05.05.2026) — freshness guard: WS
                        # books for RESOLVED events go silent without a
                        # 'market_closed' notification (Polymarket gap),
                        # so a stale `best_ask` from a resolved 5-min
                        # crypto event would slip through as 'clob_ask'
                        # (the same source label REST data carries) and
                        # bypass the Phase 9kkk hotfix #7 stale-source
                        # filter. Only trust WS books with `ts` within
                        # WS_BOOK_FRESHNESS_SEC; otherwise fall back to
                        # REST (which is always live).
                        WS_BOOK_FRESHNESS_SEC = 45.0
                        _ws_now = time.time()
                        ws_pre_clob: dict = {}
                        rest_tids: list = list(_all_tids)
                        ws_stale_skipped = 0
                        if ws_client is not None:
                            ws_pre_clob, rest_tids = [], list(_all_tids)
                            ws_pre_clob = {}
                            new_rest = []
                            ws_skipped_required = 0  # Phase TS-5c
                            # Phase TS-5c: probe connected ONCE (avoid N calls
                            # to get_metrics per scan tick).
                            ws_connected_for_skip = False
                            if POLYMARKET_WS_REQUIRED:
                                try:
                                    ws_connected_for_skip = bool(
                                        ws_client.get_metrics().get('connected'))
                                except Exception:
                                    ws_connected_for_skip = False
                            for tid in _all_tids:
                                ws_book = ws_client.get_book(tid)
                                if ws_book and ws_book.get('best_ask') and 0 < ws_book['best_ask'] < 1:
                                    # Phase 19v13: freshness check against `ts`
                                    book_ts = ws_book.get('ts') or 0.0
                                    if (_ws_now - book_ts) > WS_BOOK_FRESHNESS_SEC:
                                        ws_stale_skipped += 1
                                        # Phase TS-5c: if required + connected, skip
                                        # entirely; don't fall through to REST.
                                        if ws_connected_for_skip:
                                            ws_skipped_required += 1
                                            continue
                                        new_rest.append(tid)
                                        continue
                                    # Synth scan-time tuple matching _fetch_clob shape
                                    ask = ws_book['best_ask']
                                    depth = ws_book.get('depth') or 0.0
                                    bid = ws_book.get('best_bid')
                                    bid_depth = ws_book.get('bid_depth') or 0.0
                                    ws_pre_clob[tid] = (ask, depth, bid, bid_depth)
                                else:
                                    # Phase TS-5c: same skip for cache-miss
                                    if ws_connected_for_skip:
                                        ws_skipped_required += 1
                                        continue
                                    new_rest.append(tid)
                            rest_tids = new_rest
                            if ws_skipped_required:
                                print(f"[POLY] WS_REQUIRED: skipped "
                                      f"{ws_skipped_required} tids without "
                                      f"fresh WS book (no REST fallback)",
                                      flush=True)
                        # Now REST batch only the tokens not covered by WS
                        rest_clob = {}
                        if rest_tids:
                            from async_fetchers import run_fetch_clob_batch
                            rest_clob = run_fetch_clob_batch(
                                rest_tids,
                                max_concurrent=60,
                                slippage_tolerance=DEPTH_SLIPPAGE_TOLERANCE,
                            )
                        # Merge WS + REST. WS wins on overlap (we built ws_pre_clob
                        # from disjoint tids, so this is just union).
                        _all_clob = {**rest_clob, **ws_pre_clob}
                        _ok = sum(1 for v in _all_clob.values() if v[0] is not None)
                        print(f"[POLY] big batch /book: {_ok}/{len(_all_clob)} "
                              f"tokens (WS={len(ws_pre_clob)}, REST={len(rest_tids)}, "
                              f"WS_stale={ws_stale_skipped}) "
                              f"in {time.time()-_t_pre:.2f}s", flush=True)
                        running_clob_res.update(_all_clob)
                        stats['clob_fetched'] = _ok
                except Exception as e:
                    print(f"[POLY] big batch /book FAILED ({e!r}), "
                          f"chunks will fetch /book individually", flush=True)
                    _all_clob = None

            for chunk_start in range(0, POLY_MAIN_PAGES, POLY_CHUNK_PAGES):
                chunk_events = []
                chunk_end = min(chunk_start + POLY_CHUNK_PAGES, POLY_MAIN_PAGES)
                if _all_poly_events is not None:
                    # Phase 18: slice pre-fetched events. Each "page" is 500
                    # events, so chunk = chunk_start*500 .. chunk_end*500.
                    chunk_lo = chunk_start * 500
                    chunk_hi = chunk_end * 500
                    chunk_events = _all_poly_events[chunk_lo:chunk_hi]
                else:
                    # Fallback: original sequential fetch
                    for page_idx in range(chunk_start, chunk_end):
                        offset = page_idx * 500
                        try:
                            r = _SESS_POLY.get(
                                f"https://gamma-api.polymarket.com/events?"
                                f"closed=false&active=true&limit=500&offset={offset}",
                                timeout=_FETCH_TIMEOUT,
                            )
                            page = r.json()
                            if not page: break
                            chunk_events.extend(page)
                        except Exception as e:
                            print(f"[POLY] page {page_idx}: {e}", flush=True)
                if not chunk_events:
                    break  # API ran out of events
                running_poly_events.extend(chunk_events)
                pc_chunk, tids_chunk = filter_poly(chunk_events, diag=stats)
                running_pc.extend(pc_chunk)
                if tids_chunk:
                    # Phase 19v4: prefer big-batch pre-fetched /book if
                    # available (single asyncio.run did all tokens at once).
                    # Chunk just slices the dict — instant. Missing tids
                    # (rare partial-fail) fall back to sync batch_fetch.
                    if _all_clob is not None:
                        clob_chunk = {tid: _all_clob[tid] for tid in tids_chunk
                                      if tid in _all_clob}
                        missing = [tid for tid in tids_chunk
                                   if tid not in _all_clob]
                        if missing:
                            fb = batch_fetch(_fetch_clob, missing)
                            clob_chunk.update(fb)
                            running_clob_res.update(fb)
                    else:
                        # Fallback: sync ThreadPoolExecutor (60-100s per scan)
                        clob_chunk = batch_fetch(_fetch_clob, tids_chunk)
                        running_clob_res.update(clob_chunk)
                    stats['clob_fetched'] = sum(
                        1 for v in running_clob_res.values()
                        if v[0] is not None)
                    chunk_deals = eval_poly(pc_chunk, clob_chunk)
                    # Phase clean-quarantine-2: eval_poly drops quarantined
                    # events upfront now. No more `is_quarantine` split.
                    running_deals.extend(chunk_deals)
                stats['poly_events'] = len(running_poly_events)
                stats['poly_neg_risk'] = len(running_pc)
                _push_partial(
                    f"polymarket {chunk_end}/{POLY_MAIN_PAGES} pages")
                print(f"[POLY] chunk {chunk_start}-{chunk_end}: "
                      f"+{len(chunk_events)} events, "
                      f"+{len(pc_chunk)} candidates, "
                      f"running deals={len(running_deals)}", flush=True)
                if _budget_exceeded():
                    print(f"[MAIN] scan budget exceeded "
                          f"({RUN_SCAN_BUDGET_S}s) — bailing in Polymarket "
                          f"with partial results", flush=True)
                    break
        poly_events = running_poly_events  # alias for downstream code below
        t_poly = time.time() - t_poly

        # Kalshi — skipped entirely if ENABLE_KALSHI=0
        t_kalshi = time.time()
        kalshi_events = []
        if ENABLE_KALSHI:
            try:
                # Phase 9uu: tuple timeout (connect, read) — single-int
                # timeouts can be ignored by SSL_read C-layer hangs.
                r = _SESS_KALSHI.get("https://api.elections.kalshi.com/trade-api/v2/events?status=open&limit=200&with_nested_markets=true", timeout=_FETCH_TIMEOUT, headers=HEADERS)
                data = r.json()
                kalshi_events.extend(data.get('events', []))
                cursor = data.get('cursor')
                for _ in range(4):
                    if not cursor: break
                    r = _SESS_KALSHI.get(f"https://api.elections.kalshi.com/trade-api/v2/events?status=open&limit=200&with_nested_markets=true&cursor={cursor}", timeout=_FETCH_TIMEOUT, headers=HEADERS)
                    data = r.json()
                    kalshi_events.extend(data.get('events', []))
                    cursor = data.get('cursor')
            except Exception as e: print(f"[KALSHI] {e}")
        t_kalshi = time.time() - t_kalshi

        # SX Bet — skipped entirely if ENABLE_SX=0
        t_sx = time.time()
        sx_markets = []
        sx_fetch_error = None
        sx_http_status = None
        if ENABLE_SX:
            # ── Phase 18 (02.05.2026): consume background prefetch ─────
            # _sx_future was submitted at the start of run_scan (in parallel
            # with Polymarket processing). By now it should already be done.
            if _sx_future is not None:
                try:
                    sx_markets, sx_http_status, sx_fetch_error = _sx_future.result(timeout=30)
                    # Phase audit-3 (12.05.2026): rewind t_sx to submit time so
                    # final subtraction at end of section reports actual BG
                    # wall-clock duration (not microsecond consume time).
                    if _sx_submit_ts is not None:
                        t_sx = _sx_submit_ts
                except Exception as e:
                    sx_fetch_error = f"prefetch_result_failed: {type(e).__name__}: {e}"
                    print(f"[SX] prefetch result failed: {e}", flush=True)
            if not sx_markets and not sx_fetch_error:
                # Sync fallback (also runs if ASYNC_FETCH=0)
                try:
                    r = _SESS_SX.get(f"https://api.sx.bet/markets/active?onlyMainLine=true&pageSize={SX_PAGE_SIZE}", timeout=_FETCH_TIMEOUT)
                    sx_http_status = r.status_code
                    data = r.json()
                    if data.get('status') == 'success':
                        sx_markets.extend(data.get('data', {}).get('markets', []))
                        next_key = data.get('data', {}).get('nextKey')
                        for _ in range(SX_MAX_PAGES_MAIN - 1):
                            if not next_key: break
                            r = _SESS_SX.get(f"https://api.sx.bet/markets/active?onlyMainLine=true&pageSize={SX_PAGE_SIZE}&paginationKey={next_key}", timeout=_FETCH_TIMEOUT)
                            data = r.json()
                            if data.get('status') == 'success':
                                sx_markets.extend(data.get('data', {}).get('markets', []))
                                next_key = data.get('data', {}).get('nextKey')
                    else:
                        # Surface non-success status for diagnostics
                        sx_fetch_error = f"status={data.get('status')} msg={str(data)[:120]}"
                except Exception as e:
                    sx_fetch_error = f"{type(e).__name__}: {e}"
                    print(f"[SX] {e}")
        t_sx = time.time() - t_sx

        # Limitless Exchange — skipped if ENABLE_LIMITLESS=0.
        # Phase 9qq (29.04.2026): chunked, after every LIMITLESS_CHUNK_PAGES
        # pages: collect slugs, batch-fetch orderbooks, eval, merge.
        # Phase 9kkk (30.04.2026): when ASYNC_FETCH=1, ALL pages are fetched
        # in parallel via HTTP/2 multiplexing FIRST, then chunked iteration
        # processes them locally (no more sequential REST round-trips).
        # Empirical: 40 pages × 1s sequential = 40s; parallel HTTP/2 = ~2-3s.
        t_lim = time.time()
        if ENABLE_LIMITLESS:
            # ── Phase 9kkk: parallel main-page fetcher ────────────────
            # Phase 18 (02.05.2026): consume background _lim_future first
            # if it was started; if not (fallback path), fetch synchronously
            # via async_fetchers.run_fetch_limitless_pages now.
            _all_lim_pages = None  # populated if async path succeeds
            if _lim_future is not None:
                try:
                    _all_lim_pages = _lim_future.result(timeout=30)
                    # Phase audit-3 (12.05.2026): rewind t_lim to submit ts so
                    # final subtraction reports BG wall-clock, not consume µs.
                    if _lim_submit_ts is not None:
                        t_lim = _lim_submit_ts
                    print(f"[LIM] parallel fetch (background) done: "
                          f"{len(_all_lim_pages)} events in "
                          f"{time.time()-t_lim:.2f}s", flush=True)
                except Exception as e:
                    print(f"[LIM] background fetch failed ({e}), "
                          f"will retry synchronously", flush=True)
                    _all_lim_pages = None
            if _all_lim_pages is None and os.environ.get('ASYNC_FETCH') == '1':
                try:
                    from async_fetchers import run_fetch_limitless_pages
                    # Phase audit-2 (12.05.2026) — concurrency reduced
                    # from 20 → LIMITLESS_PAGE_CONCURRENT (default 8) so
                    # we stay below Limitless rate limit. See module-top
                    # comment on PR #179 → #180 → #181 history.
                    _all_lim_pages = run_fetch_limitless_pages(
                        page_size=LIMITLESS_PAGE_SIZE,
                        max_pages=LIMITLESS_MAIN_PAGES,
                        max_concurrent=LIMITLESS_PAGE_CONCURRENT,
                    )
                    print(f"[LIM] parallel fetch done: "
                          f"{len(_all_lim_pages)} events in "
                          f"{time.time()-t_lim:.2f}s", flush=True)
                except Exception as e:
                    print(f"[LIM] parallel fetch failed ({e}), "
                          f"fallback to sequential", flush=True)
                    _all_lim_pages = None
            print(f"[LIM] starting chunk-eval loop "
                  f"({LIMITLESS_MAIN_PAGES} pages, "
                  f"chunks of {LIMITLESS_CHUNK_PAGES})", flush=True)
            for chunk_start in range(0, LIMITLESS_MAIN_PAGES,
                                     LIMITLESS_CHUNK_PAGES):
                chunk_events = []
                chunk_end = min(chunk_start + LIMITLESS_CHUNK_PAGES,
                                LIMITLESS_MAIN_PAGES)
                stop_outer = False
                if _all_lim_pages is not None:
                    # Phase 9kkk path: slice pre-fetched events.
                    # Each "page" in the chunk corresponds to LIMITLESS_PAGE_SIZE
                    # events. Take chunk_start*PAGE_SIZE .. chunk_end*PAGE_SIZE.
                    chunk_lo = chunk_start * LIMITLESS_PAGE_SIZE
                    chunk_hi = chunk_end * LIMITLESS_PAGE_SIZE
                    chunk_events = _all_lim_pages[chunk_lo:chunk_hi]
                    if not chunk_events:
                        stop_outer = True
                else:
                    # Fallback: original sequential REST loop
                    print(f"[LIM] fetching pages "
                          f"{chunk_start+1}-{chunk_end}…", flush=True)
                    for page_idx in range(chunk_start, chunk_end):
                        page_num = page_idx + 1  # API is 1-indexed
                        try:
                            r = _SESS_LIM.get(
                                f"{LIMITLESS_API_BASE}/markets/active?"
                                f"page={page_num}&limit={LIMITLESS_PAGE_SIZE}",
                                timeout=_FETCH_TIMEOUT,
                            )
                            if r.status_code != 200:
                                stop_outer = True; break
                            data = r.json()
                            items = data if isinstance(data, list) \
                                    else data.get('data') or data.get('markets') or []
                            if not items:
                                stop_outer = True; break
                            chunk_events.extend(items)
                            if len(items) < LIMITLESS_PAGE_SIZE:
                                stop_outer = True; break
                            if LIMITLESS_PAGE_DELAY_S > 0 \
                                    and page_idx + 1 < LIMITLESS_MAIN_PAGES:
                                time.sleep(LIMITLESS_PAGE_DELAY_S)
                        except Exception as e:
                            print(f"[LIMITLESS] page {page_num}: {e}")
                            stop_outer = True; break
                if not chunk_events:
                    if stop_outer: break
                    else: continue
                running_lim_events.extend(chunk_events)
                # Slugs for this chunk only.
                # Phase 9rr (29.04.2026) — pre-filter by volume>0.
                # /markets/active includes a `volume` field per market;
                # markets with volume=0 are dead (never traded), and the
                # eval_limitless / pool path drops them anyway via
                # `_fetch_limitless_market_meta(slug).volume == 0`. So
                # fetching their orderbook is wasted work — and the dead
                # backends are exactly the ones that hang requests.get.
                # Empirical: 50 events → ~95 child slugs total; after
                # volume>0 filter → 15-25 active. 70-80% reduction in
                # orderbook calls on the busiest chunks.
                chunk_slugs = []
                skipped_zero_vol = 0
                def _has_volume(m):
                    try: v = float(m.get('volume') or 0)
                    except Exception: v = 0
                    return v > 0
                for ev in chunk_events:
                    children = ev.get('markets') or []
                    if children:
                        for c in children:
                            s = c.get('slug') or c.get('address')
                            if not s: continue
                            if _has_volume(c):
                                chunk_slugs.append(s)
                            else:
                                skipped_zero_vol += 1
                    else:
                        s = ev.get('slug') or ev.get('address')
                        if not s: continue
                        if _has_volume(ev):
                            chunk_slugs.append(s)
                        else:
                            skipped_zero_vol += 1
                print(f"[LIM] chunk {chunk_start}-{chunk_end}: "
                      f"{len(chunk_events)} events, {len(chunk_slugs)} slugs"
                      f" (skipped {skipped_zero_vol} vol=0) → batch_fetch…",
                      flush=True)
                if chunk_slugs:
                    # Phase 9fff (29.04.2026) — async fetcher gated by env.
                    # When ASYNC_FETCH=1, use httpx.AsyncClient via
                    # async_fetchers.py — single thread, no GIL contention,
                    # no socketio reconnect storms.
                    # Default sync path remains (requests.Session) until
                    # we've A/B-measured the async path on the VPS.
                    if os.environ.get('ASYNC_FETCH') == '1':
                        try:
                            from async_fetchers import (
                                run_async_batch, fetch_limitless_orderbook_async)
                            # Phase audit-2 (12.05.2026) — concurrency
                            # capped at LIMITLESS_OB_CONCURRENT (default 12,
                            # was MAX_WORKERS=30). Per-slug orderbook
                            # fetch fires for HUNDREDS of slugs per scan,
                            # so this is the dominant rate-limit pressure.
                            lim_chunk_res = run_async_batch(
                                fetch_limitless_orderbook_async,
                                chunk_slugs,
                                max_concurrent=LIMITLESS_OB_CONCURRENT)
                        except ImportError:
                            print("[LIM] httpx not installed — falling back "
                                  "to sync batch_fetch", flush=True)
                            lim_chunk_res = batch_fetch(
                                _fetch_limitless_orderbook, chunk_slugs)
                    else:
                        lim_chunk_res = batch_fetch(
                            _fetch_limitless_orderbook, chunk_slugs)
                    running_lim_res.update(lim_chunk_res)
                    chunk_deals = eval_limitless(chunk_events, lim_chunk_res)
                    # Phase clean-quarantine-2: eval_limitless drops
                    # quarantined events upfront. No split.
                    running_deals.extend(chunk_deals)
                stats['lim_events'] = len(running_lim_events)
                stats['lim_slugs'] = len(running_lim_res)
                stats['lim_ob_fetched'] = sum(
                    1 for v in running_lim_res.values()
                    if v[0] is not None)
                _push_partial(
                    f"limitless {chunk_end}/{LIMITLESS_MAIN_PAGES} pages")
                print(f"[LIM] chunk {chunk_start}-{chunk_end}: "
                      f"+{len(chunk_events)} events, "
                      f"running deals={len(running_deals)}", flush=True)
                if stop_outer: break
                if _budget_exceeded():
                    print(f"[MAIN] scan budget exceeded "
                          f"({RUN_SCAN_BUDGET_S}s) — bailing in Limitless "
                          f"with partial results", flush=True)
                    break
        lim_events = running_lim_events  # alias for downstream code
        t_lim = time.time() - t_lim

        # ───── Final aggregation ─────
        # Polymarket / Limitless were processed in chunks above; their
        # deals + candidates are already in `running_*`. Below we run
        # Kalshi + SX as single-shot (they're disabled in production and
        # cap-bounded — Kalshi 1000 events, SX 1000 markets — fast).
        pc = running_pc
        poly_tids = []  # already fetched per-chunk into running_clob_res
        clob_res = running_clob_res
        lim_res = running_lim_res

        kc, kalshi_tks = filter_kalshi(kalshi_events, diag=stats)
        sx_ml_hashes = [m['marketHash'] for m in sx_markets
                        if m.get('type') in SX_BINARY_TYPES]
        stats['sx_binary_count'] = len(sx_ml_hashes)
        stats['sx_moneyline_count'] = sum(
            1 for m in sx_markets if m.get('type') == 226)
        stats['poly_events'] = len(poly_events)
        stats['kalshi_events'] = len(kalshi_events)
        stats['sx_markets'] = len(sx_markets)
        stats['lim_events'] = len(lim_events)
        stats['sx_http_status'] = sx_http_status
        stats['sx_fetch_error'] = sx_fetch_error
        print(f"[FETCH] Poly={len(poly_events)} ({t_poly:.1f}s) "
              f"Kalshi={len(kalshi_events)} ({t_kalshi:.1f}s) "
              f"SX={len(sx_markets)} ({t_sx:.1f}s) "
              f"Lim={len(lim_events)} ({t_lim:.1f}s) "
              f"sx_http={sx_http_status}")

        kalshi_res = batch_fetch(_fetch_kalshi_ob, kalshi_tks)

        # Phase 19v7 (03.05.2026) — async SX orders batch.
        # Sync `batch_fetch(_fetch_sx_orders, ...)` тратил 30-60с на 300-500
        # binary markets из-за GIL contention при JSON parsing. Async fan-out
        # через httpx + connection pool keepalive ожидаемо в 5-10× быстрее.
        # Same fallback pattern as big batch /book — error → sync.
        sx_res = None
        if os.environ.get('ASYNC_FETCH') == '1' and sx_ml_hashes:
            try:
                from async_fetchers import run_fetch_sx_orders_batch
                sx_res = run_fetch_sx_orders_batch(
                    list(sx_ml_hashes),
                    max_concurrent=30,
                    slippage_tolerance=DEPTH_SLIPPAGE_TOLERANCE,
                )
            except Exception as e:
                print(f"[SX] async orders batch failed ({e}), "
                      f"fallback to sync", flush=True)
                sx_res = None
        if sx_res is None:
            sx_res = batch_fetch(_fetch_sx_orders, sx_ml_hashes)

        stats['clob_fetched'] = sum(1 for v in clob_res.values()
                                    if v[0] is not None)
        stats['kalshi_ob_fetched'] = sum(1 for v in kalshi_res.values()
                                         if v[0] is not None)
        stats['lim_ob_fetched'] = sum(1 for v in lim_res.values()
                                      if v[0] is not None)

        # Combine: chunked deals (Poly+Lim already evaluated) + Kalshi/SX
        # Phase clean-quarantine-2 — running_quarantine removed.
        all_deals = list(running_deals)
        if ENABLE_KALSHI:
            all_deals += eval_kalshi(kc, kalshi_res)
        if ENABLE_SX:
            # Phase 14a (01.05.2026) — Gap 5: pre-filter SX through filter_sx
            # for parity with filter_poly/filter_limitless. Adaptive grace
            # rejects post-resolve markets, status check is belt-and-
            # suspenders against the version inside eval_sx.
            sx_filtered = filter_sx(sx_markets, diag=stats)
            all_deals += eval_sx(sx_filtered, sx_res)

        # Phase 14b (01.05.2026) — Cross-platform pairing layer.
        # Opt-in via env CROSS_PLATFORM_ENABLED=1. NOT default-on because
        # it adds work to every scan (event matching across platforms) and
        # operator should validate single-platform stability first.
        # Standalone module — does NOT modify per-platform deals above.
        try:
            import cross_platform as _cp
            if _cp.CROSS_PLATFORM_ENABLED:
                # Build PlatformOutcome lists from current scan results.
                # Polymarket from pc + clob_res, Limitless from lim_events
                # + lim_res, SX Bet from sx_markets + sx_res.
                cp_pool_poly = _build_cp_outcomes_polymarket(pc, clob_res)
                cp_pool_lim = _build_cp_outcomes_limitless(
                    lim_events or [], lim_res)
                cp_pool_sx = _build_cp_outcomes_sx(
                    sx_filtered if ENABLE_SX else [], sx_res)
                cross_deals = []
                # Pairwise: Poly×Lim, Poly×SX, Lim×SX
                for pool_a, pool_b in (
                    (cp_pool_poly, cp_pool_lim),
                    (cp_pool_poly, cp_pool_sx),
                    (cp_pool_lim, cp_pool_sx),
                ):
                    cross_deals.extend(_cp.find_cross_platform_arbs(
                        pool_a, pool_b,
                        min_confidence=float(os.environ.get(
                            'CP_MIN_CONFIDENCE', '0.75'))))
                radar_deals = [_cp.to_radar_deal_format(d) for d in cross_deals]
                all_deals += radar_deals
                stats['cross_platform_count'] = len(radar_deals)
        except Exception as e:
            log.warning("cross_platform layer error: %s", e)

        # Phase clean-quarantine-2: no `is_quarantine` filter — eval_*
        # functions drop those events upfront. all_deals == deals.
        deals = list(all_deals)
        deals.sort(key=lambda d: d['net'], reverse=True)

        stats['arb_found'] = len(deals)

        # Save candidates for micro-scan (legacy path, kept for safety)
        with cand_lock:
            candidates_global['poly'] = pc
            candidates_global['kalshi'] = kc
            candidates_global['sx'] = sx_markets

        # ── Classify into HOT/NEAR pools, update WS subscription set ──
        ws_books = {tid: ws_client.get_book(tid) for tid in (clob_res.keys() if ws_client else [])}
        ws_books = {k: v for k, v in ws_books.items() if v}
        new_pools = classify_pools(pc, kc, sx_markets, clob_res, kalshi_res, sx_res,
                                    lim_events=lim_events, lim_res=lim_res, ws_books=ws_books)
        with pools_lock:
            pools.update(new_pools)
        # Cache REST clob snapshot for WS-driven re-eval fallback + NEAR snapshot
        with poly_clob_cache_lock:
            poly_clob_cache.clear(); poly_clob_cache.update(clob_res)
        with res_cache_lock:
            kalshi_res_cache.clear(); kalshi_res_cache.update(kalshi_res)
            sx_res_cache.clear(); sx_res_cache.update(sx_res)
            lim_res_cache.clear(); lim_res_cache.update(lim_res)
        # Phase 19v11 (04.05.2026) — WS subscription updates в фоне.
        # `update_subscriptions` triggers WS reconnect (close+open) если
        # set изменился, что может занимать 1-3с TCP+TLS. Daemon thread
        # не блокирует scan_loop → следующий цикл сразу.
        # Index rebuild делаем в main thread (cheap pure Python, нужно
        # синхронно для subsequent on_ws_update callback consistency).
        poly_pool = new_pools['poly']
        new_idx = rebuild_poly_token_index(poly_pool) if ws_client else {}
        if ws_client is not None:
            with poly_token_index_lock:
                poly_token_index.clear(); poly_token_index.update(new_idx)
            tokens = collect_poly_tokens({'hot': poly_pool['hot'], 'near': poly_pool['near']})
            threading.Thread(
                target=lambda: ws_client.update_subscriptions(tokens[:MAX_WS_SUBS]),
                daemon=True, name='ws-poly-sub-update',
            ).start()
        # Limitless: same pattern.
        lim_pool = new_pools.get('lim') or {'hot': [], 'near': []}
        new_lim_idx = rebuild_lim_slug_index(lim_pool)
        with lim_slug_index_lock:
            lim_slug_index.clear(); lim_slug_index.update(new_lim_idx)

        if lim_ws_client is not None:
            lim_slugs_set = list(new_lim_idx.keys())
            threading.Thread(
                target=lambda: lim_ws_client.update_subscriptions(lim_slugs_set[:LIMITLESS_MAX_WS_SUBS]),
                daemon=True, name='ws-lim-sub-update',
            ).start()
        # Phase 9f: push HOT+NEAR Polymarket condition_ids to every per-wallet
        # user-channel WS so they can latch on `trade` events for our orders.
        if poly_user_ws_clients:
            poly_pool_now = new_pools['poly']
            condition_ids = []
            for cand in poly_pool_now['hot'] + poly_pool_now['near']:
                ev_obj = cand[0] if isinstance(cand, tuple) else cand.get('ev')
                if not isinstance(ev_obj, dict):
                    continue
                # Polymarket exposes either `condition_id` on each market
                # or a top-level event `id`. Collect from markets[].
                for m in (ev_obj.get('markets') or []):
                    cid = m.get('conditionId') or m.get('condition_id')
                    if cid: condition_ids.append(cid)
            for client in poly_user_ws_clients:
                client.update_markets(condition_ids[:MAX_WS_SUBS])
        stats['pool_poly_hot']    = len(new_pools['poly']['hot'])
        stats['pool_poly_near']   = len(new_pools['poly']['near'])
        stats['pool_kalshi_hot']  = len(new_pools['kalshi']['hot'])
        stats['pool_kalshi_near'] = len(new_pools['kalshi']['near'])
        stats['pool_sx_hot']      = len(new_pools['sx']['hot'])
        stats['pool_sx_near']     = len(new_pools['sx']['near'])
        stats['pool_lim_hot']     = len(new_pools['lim']['hot'])
        stats['pool_lim_near']    = len(new_pools['lim']['near'])

        elapsed = time.time() - t0
        # Phase audit-2 (12.05.2026) — per-platform stage breakdown.
        # `t_poly` / `t_sx` / `t_lim` are existing local floats already
        # populated by the platform sections above (used in [FETCH] log
        # line). Pass them to the timing buffer so /api/scan_health.scan_breakdown_ms
        # shows the operator WHICH platform is slow, not just the total.
        # Defensive: each var may be undefined if a section was skipped
        # (e.g. ENABLE_LIMITLESS=0, or budget bail), so use locals().get.
        _stage_ms = {}
        for var_name, key in (('t_poly', 'poly_ms'),
                                ('t_sx', 'sx_ms'),
                                ('t_lim', 'lim_ms')):
            v = locals().get(var_name)
            if isinstance(v, (int, float)) and v >= 0:
                _stage_ms[key] = v * 1000.0
        _record_scan_tick(elapsed, stages=_stage_ms)
        print(f"[MAIN] Done in {elapsed:.1f}s — {stats['arb_found']} arb found "
              f"| pools: poly H{stats['pool_poly_hot']}/N{stats['pool_poly_near']} "
              f"kalshi H{stats['pool_kalshi_hot']}/N{stats['pool_kalshi_near']} "
              f"sx H{stats['pool_sx_hot']}/N{stats['pool_sx_near']} "
              f"lim H{stats['pool_lim_hot']}/N{stats['pool_lim_near']}")
        if deals: save_history(deals)

    except Exception as e:
        print(f"[MAIN] Error: {e}")
        import traceback; traceback.print_exc()
        with scan_lock:
            scan_data['error'] = str(e)
            scan_data['scanning'] = False
            scan_data.pop('progress', None)
            return

    with scan_lock:
        scan_data['deals'] = deals
        scan_data['stats'] = stats
        scan_data['last_scan'] = datetime.now(timezone.utc).isoformat()
        scan_data['scanning'] = False
        # Phase 9qq: scan complete → clear progress label so UI knows
        # we're done (not "polymarket 8/10 pages" forever).
        scan_data.pop('progress', None)
        # First fresh scan after a restore — clear the "stale" flags
        # so the UI knows the snapshot is now live.
        scan_data.pop('restored_from_disk', None)
        scan_data.pop('restored_age_s', None)
    # Phase 19v11 (04.05.2026) — persist в фоне.
    # Phase 19v13 (04.05.2026) — race fix: ensure single persist thread
    # at a time. Without this, two threads could write same file
    # concurrently → corrupt JSON, restart loses state. Use try-acquire
    # non-blocking lock; if previous persist still running, skip this
    # tick (state will be written by the running thread or next tick).
    if _persist_state_lock.acquire(blocking=False):
        def _persist_with_lock():
            try:
                _persist_scan_state()
            finally:
                _persist_state_lock.release()
        threading.Thread(
            target=_persist_with_lock, daemon=True, name='persist-state'
        ).start()
    else:
        log.debug("persist_scan_state: previous write still running, skipping")
    # Phase 18: shut down the prefetch pool (daemon threads, won't hang).
    try:
        _bg_pool.shutdown(wait=False)
    except (NameError, AttributeError):
        pass        # _bg_pool not initialized (early bail) — fine
    # Auto-dry-fire new arbs from this main scan (Phase 2). Idempotent —
    # tracks already-fired keys, so the same deal isn't logged every 90s.
    _maybe_dry_fire(deals)

# ═══════════════════════════════════════════════════════════════
# PAUSE SCAN — Extra pages 
# ═══════════════════════════════════════════════════════════════
def run_pause_scan():
    """Fetch additional Poly/Kalshi/SX pages during pause.

    Phase 9t: ENABLE_POLY / ENABLE_SX gates added — same rationale as
    poly_micro_fallback_loop. Without these, the pause-scan kept hitting
    geo-blocked Polymarket / SX endpoints, which (because they tarpit
    TLS handshake) consumed CPU and held resources for full 10s timeout
    × 4 pages = 40s before finally erroring out."""
    t0 = time.time()
    extra_deals = []

    # Extra Polymarket pages — only if Polymarket is enabled
    if ENABLE_POLY:
        for offset in [300, 800, 1300]:
            try:
                # Phase 19v15 (05.05.2026) — pooled `_SESS_POLY` session
                # instead of bare `requests.get`. Bare module-level
                # `requests.get` opens a new TLS handshake every call,
                # bypassing the connection pool that Phase 9rr put on
                # `_SESS_POLY`. Under CF tarpit (~5s handshake) this
                # made pause_scan a no-op. Plus print the error so
                # operators see geo blocks / 429s.
                r = _SESS_POLY.get(
                    f"https://gamma-api.polymarket.com/events?closed=false&limit=500&active=true&offset={offset}",
                    timeout=_FETCH_TIMEOUT,
                )
                data = r.json()
                if not data: break
                pc, tids = filter_poly(data)
                if pc:
                    clob = batch_fetch(_fetch_clob, tids)
                    extra_deals.extend(eval_poly(pc, clob))
                if len(data) < 500: break
            except Exception as e:
                print(f"[PAUSE_POLY] offset={offset} {type(e).__name__}: {e}",
                      flush=True)
                break

    # Extra SX Bet pages — only if SX Bet is enabled
    if not ENABLE_SX:
        # Skip SX block entirely; merge what we already have and return
        if extra_deals:
            extra_deals.sort(key=lambda d: d['net'], reverse=True)
            with scan_lock:
                existing = scan_data.get('deals', [])
                existing_titles = {d['title'] for d in existing}
                for d in extra_deals:
                    if d['title'] not in existing_titles:
                        existing.append(d)
                existing.sort(key=lambda d: d['net'], reverse=True)
                scan_data['deals'] = existing
        return

    try:
        # Phase 9uu: pooled session
        r = _SESS_SX.get(f"https://api.sx.bet/markets/active?onlyMainLine=true&pageSize={SX_PAGE_SIZE}", timeout=_FETCH_TIMEOUT)
        data = r.json()
        next_key = data.get('data', {}).get('nextKey') if data.get('status') == 'success' else None
        pages = 0
        while next_key and pages < (SX_MAX_PAGES_PAUSE - 1):
            r = _SESS_SX.get(f"https://api.sx.bet/markets/active?onlyMainLine=true&pageSize={SX_PAGE_SIZE}&paginationKey={next_key}", timeout=_FETCH_TIMEOUT)
            data = r.json()
            if data.get('status') != 'success': break
            batch = data.get('data', {}).get('markets', [])
            ml_hashes = [m['marketHash'] for m in batch if m.get('type') in SX_BINARY_TYPES]
            if ml_hashes:
                sx_res = batch_fetch(_fetch_sx_orders, ml_hashes)
                extra_deals.extend(eval_sx(batch, sx_res))
            next_key = data.get('data', {}).get('nextKey')
            pages += 1
    except Exception as e:
        # Phase 19v15 (05.05.2026) — narrowed bare `except: pass` so
        # KeyboardInterrupt / SystemExit no longer get swallowed and
        # operators see SX rate-limit / TLS / CF block patterns.
        print(f"[PAUSE_SX] {type(e).__name__}: {e}", flush=True)

    # Merge
    if extra_deals:
        extra_deals.sort(key=lambda d: d['net'], reverse=True)
        with scan_lock:
            # Phase 19v16 (05.05.2026) — make a fresh COPY of the deals
            # list before mutating. Old code did `existing = scan_data.get('deals', [])`
            # which returned the live reference; if `run_scan` (or any
            # micro_loop) replaced `scan_data['deals']` between this fetch
            # and our `scan_data['deals'] = existing` write-back, the
            # appends landed on an orphan list AND wiped out the fresh
            # scan's results. Now we copy, mutate, then publish.
            existing = list(scan_data.get('deals', []))
            existing_titles = {d['title'] for d in existing}
            for d in extra_deals:
                if d['title'] not in existing_titles:
                    existing.append(d)
            existing.sort(key=lambda d: d['net'], reverse=True)
            scan_data['deals'] = existing
            stats = scan_data.setdefault('stats', {})
            stats['arb_found'] = len(existing)
        save_history(extra_deals, micro=True)

# ── Micro Scanners (per-platform, pool-scoped) ──────────────────
def _merge_platform_deals(new_deals, platform):
    """Replace this platform's deals/quarantine in scan_data with the new list,
    keeping deals from other platforms intact."""
    # Phase clean-quarantine-2: eval_* dropped quarantine events. new_deals
    # is already clean.
    with scan_lock:
        deals = [d for d in scan_data.get('deals', []) if d.get('platform') != platform]
        deals.extend(new_deals)
        deals.sort(key=lambda d: d['net'], reverse=True)
        scan_data['deals'] = deals
        if isinstance(scan_data.get('stats'), dict):
            scan_data['stats']['arb_found'] = len(deals)

def kalshi_micro_loop():
    """Refresh Kalshi HOT+NEAR pool every KALSHI_MICRO_INTERVAL seconds."""
    time.sleep(15)
    while True:
        try:
            # Phase 19v4 (02.05.2026): release scan_lock BEFORE sleep.
            # Old code held lock during 5s sleep → scan_loop starvation
            # (py-spy dump: scan_loop blocked at _push_partial line 3758
            # for 4+ minutes because micro_loops kept ping-ponging the
            # lock without yielding to scan_loop).
            with scan_lock:
                scanning = scan_data['scanning']
            if scanning:
                time.sleep(KALSHI_MICRO_INTERVAL); continue
            with pools_lock:
                pool = list(pools['kalshi']['hot']) + list(pools['kalshi']['near'])
            if pool:
                tks = [t for _, tickers in pool for t in tickers]
                k_res = batch_fetch(_fetch_kalshi_ob, tks)
                _merge_platform_deals(eval_kalshi(pool, k_res), 'Kalshi')
        except Exception as e:
            print(f"[KALSHI MICRO] Error: {e}")
        time.sleep(KALSHI_MICRO_INTERVAL)

def sx_micro_loop():
    """Refresh SX Bet HOT+NEAR pool every SX_MICRO_INTERVAL seconds (live sport)."""
    time.sleep(15)
    while True:
        try:
            with scan_lock:
                scanning = scan_data['scanning']
            if scanning:
                time.sleep(SX_MICRO_INTERVAL); continue
            with pools_lock:
                pool = list(pools['sx']['hot']) + list(pools['sx']['near'])
            if pool:
                ml_hashes = [m['marketHash'] for m in pool if m.get('type') in SX_BINARY_TYPES]
                sx_res = batch_fetch(_fetch_sx_orders, ml_hashes)
                _merge_platform_deals(eval_sx(pool, sx_res), 'SX Bet')
        except Exception as e:
            print(f"[SX MICRO] Error: {e}")
        time.sleep(SX_MICRO_INTERVAL)


def limitless_micro_loop():
    """Refresh Limitless HOT+NEAR pool every LIMITLESS_MICRO_INTERVAL seconds.
    Same pattern as kalshi_micro_loop / sx_micro_loop — re-fetches orderbooks
    of the in-pool slugs and re-evaluates, so a price flick into arb territory
    surfaces in <5s without waiting for the 90s main scan. WebSocket would
    cut this to <100ms but is left as Phase 2 of the Limitless integration."""
    time.sleep(20)
    while True:
        try:
            with scan_lock:
                scanning = scan_data['scanning']
            if scanning:
                time.sleep(LIMITLESS_MICRO_INTERVAL); continue
            with pools_lock:
                pool = list(pools['lim']['hot']) + list(pools['lim']['near'])
            if pool:
                slugs = []
                for ev in pool:
                    children = ev.get('markets') or []
                    if children:
                        for c in children:
                            s = c.get('slug') or c.get('address')
                            if s: slugs.append(s)
                    else:
                        s = ev.get('slug') or ev.get('address')
                        if s: slugs.append(s)
                # Phase 9fff: async path when feature-flag on
                if os.environ.get('ASYNC_FETCH') == '1':
                    try:
                        from async_fetchers import (
                            run_async_batch, fetch_limitless_orderbook_async)
                        lim_res = run_async_batch(
                            fetch_limitless_orderbook_async, slugs,
                            max_concurrent=MAX_WORKERS)
                    except ImportError:
                        lim_res = batch_fetch(_fetch_limitless_orderbook, slugs)
                else:
                    lim_res = batch_fetch(_fetch_limitless_orderbook, slugs)
                _merge_platform_deals(eval_limitless(pool, lim_res), 'Limitless')
        except Exception as e:
            print(f"[LIM MICRO] Error: {e}")
        time.sleep(LIMITLESS_MICRO_INTERVAL)

def analytics_loop():
    """Periodically snapshot scan_data['deals'] into analytics so we get
    open/close lifecycle events without instrumenting every write site."""
    time.sleep(10)
    while True:
        try:
            with scan_lock:
                deals_snapshot = list(scan_data.get('deals') or [])
            analytics.update_from_scan(deals_snapshot)
            # Phase audit (11.05.2026) — BUG-B2: also snapshot NEAR pool so
            # we get forensic history of which markets sat at threshold and
            # for how long, independent of whether they ever became arbs.
            try:
                with poly_clob_cache_lock:
                    clob = dict(poly_clob_cache)
                with res_cache_lock:
                    ka = dict(kalshi_res_cache)
                    sx = dict(sx_res_cache)
                    lim = dict(lim_res_cache)
                ws_books = {}
                if ws_client is not None:
                    for tid in clob.keys():
                        b = ws_client.get_book(tid)
                        if b:
                            ws_books[tid] = b
                near_items = near_summary(clob_res=clob, kalshi_res=ka,
                                          sx_res=sx, lim_res=lim,
                                          ws_books=ws_books)
                analytics.update_from_near_scan(near_items)
            except Exception as ne:
                print(f"[ANALYTICS NEAR] Error: {ne}")
        except Exception as e:
            print(f"[ANALYTICS] Error: {e}")
        time.sleep(10)

def poly_micro_fallback_loop():
    """Fallback REST poll for Polymarket HOT+NEAR pool — runs ONLY when WS is
    disconnected (no msgs in last 30s). Keeps Polymarket fresh during outages.

    Phase 9t: also gated by ENABLE_POLY — without this gate the loop
    would silently keep hitting gamma-api.polymarket.com even when the
    operator has set ENABLE_POLY=0 due to geo-block / TLS-tarpit."""
    if not ENABLE_POLY:
        print("[POLY FALLBACK] ENABLE_POLY=0 — fallback loop disabled")
        return
    time.sleep(20)
    while True:
        try:
            ws_dead = True
            if ws_client is not None:
                m = ws_client.get_metrics()
                age = m.get('last_msg_age_sec')
                if age is not None and age < 30:
                    ws_dead = False
            if ws_dead:
                with pools_lock:
                    pool = list(pools['poly']['hot']) + list(pools['poly']['near'])
                if pool:
                    tids = [o.get('token_id') for _, rough, _ in pool for o in rough if o.get('token_id')]
                    clob = batch_fetch(_fetch_clob, tids)
                    _merge_platform_deals(eval_poly(pool, clob), 'Polymarket')
        except Exception as e:
            print(f"[POLY FALLBACK] Error: {e}")
        time.sleep(MICRO_INTERVAL)

def save_history(deals, micro=False):
    # Phase 19v15 (05.05.2026) — narrowed bare `except: pass` so
    # KeyboardInterrupt is no longer swallowed and disk-full / permission
    # errors are visible in stderr instead of silent data loss.
    try:
        hdir = os.path.join(os.path.dirname(__file__), '..', 'Executions')
        os.makedirs(hdir, exist_ok=True)
        with open(os.path.join(hdir, 'price_history.jsonl'), 'a', encoding='utf-8') as f:
            f.write(json.dumps({
                "time": datetime.now(timezone.utc).isoformat(), "micro": micro,
                "deals": [{"title":d["title"],"platform":d["platform"],"sum":d["total_cents"],"net":d["net"]} for d in deals[:10]]
            }) + "\n")
    except Exception as e:
        print(f"[save_history] {type(e).__name__}: {e}", flush=True)


# ── scan_data warm-cache ───────────────────────────────────────────
# The MAIN scan can take 30-60s on a cold container start. Until it
# finishes, scan_data is empty and the UI shows a "Запуск сканирования…"
# spinner that visually looks like a hang. Persist the last-completed
# scan_data to disk so a restarted container immediately serves the
# previous snapshot — the loop then overwrites it on the first fresh
# pass. Stale (>24h) state is dropped.
SCAN_STATE_PATH = os.path.join(os.path.dirname(__file__), '..',
                               'Executions', 'scan_state.json')
SCAN_STATE_MAX_AGE_S = 24 * 3600


def _persist_scan_state():
    """Atomically write the current scan_data snapshot to disk.
    Best-effort — failures are logged but never raise."""
    try:
        os.makedirs(os.path.dirname(SCAN_STATE_PATH), exist_ok=True)
        # Phase 19v16 (05.05.2026) — DEEP-copy under lock. Old code did
        # `payload = dict(scan_data)` (shallow) — `payload['deals']` was
        # still the SAME list reference. After lock release, json.dump
        # iterated 800KB of deals outside the lock while scan_loop /
        # on_ws_update / _merge_platform_deals mutated the same list →
        # `RuntimeError: dictionary/list changed size during iteration`
        # OR a partial JSON file. Use json.dumps inside the lock for an
        # atomic snapshot — single pass, no concurrent mutation possible.
        with scan_lock:
            # Strip volatile fields BEFORE serializing (so we don't carry
            # WS metrics that downstream readers expect to be live).
            snapshot = {k: v for k, v in scan_data.items()
                         if k not in ('scanning', 'error', 'ws',
                                       'ws_limitless', 'near_count')}
            serialized = json.dumps(snapshot, ensure_ascii=False, default=str)
        tmp = SCAN_STATE_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(serialized)
        os.replace(tmp, SCAN_STATE_PATH)
    except Exception as e:
        print(f"[persist] {e}")


def _restore_scan_state():
    """On startup, repopulate scan_data from the persisted snapshot if it
    exists and is recent enough — so /api/deals does not return an empty
    payload while the first run_scan() is still in flight."""
    try:
        if not os.path.exists(SCAN_STATE_PATH):
            return
        age = time.time() - os.path.getmtime(SCAN_STATE_PATH)
        if age > SCAN_STATE_MAX_AGE_S:
            print(f"[restore] scan_state {age/3600:.1f}h old — skipping")
            return
        with open(SCAN_STATE_PATH, 'r', encoding='utf-8') as f:
            payload = json.load(f)
        # Mark as restored so the UI/operators know this is not live.
        # First successful run_scan() overwrites it (re-persists without
        # this flag).
        payload['restored_from_disk'] = True
        payload['restored_age_s'] = round(age, 1)
        with scan_lock:
            scan_data.update(payload)
        n_deals = len(payload.get('deals') or [])
        print(f"[restore] loaded {n_deals} deals from cache "
              f"(age {age:.0f}s) — UI will show last snapshot until "
              f"first scan completes")
    except Exception as e:
        print(f"[restore] {e}")


def scan_loop():
    time.sleep(2)
    while True:
        run_scan()
        threading.Thread(target=run_pause_scan, daemon=True).start()
        time.sleep(SCAN_INTERVAL)

# ── Routes ──────────────────────────────────────────────────────
@app.route('/')
def index():
    """Phase 9eee.1 — disable HTML caching for the dashboard.

    Browser was holding a cached copy of dashboard.html with broken JS
    after each deploy until user hit Ctrl+Shift+R. With these headers
    every reload fetches fresh HTML; static assets inside (CSS/JS) get
    proper ETag handling automatically by Flask's send_file."""
    resp = send_file(os.path.join(os.path.dirname(__file__), 'dashboard.html'))
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp

# /api/deals + /api/near + /api/recent_near extracted to radar.api.deals
# (audit-28b cont 5, 28.05.2026). Blueprint registered at boot.
#
# /api/scan + /api/approve + /api/reject + /api/analytics/reset extracted
# to radar.api.admin (audit-28b cont 5). Blueprint registered at boot.


def _raw_near_pool_count() -> int:
    """Helper used by radar.api.deals::api_deals via lazy import — counts
    raw NEAR pool entries (pre-`_best_near_structure` filter). Kept here
    because it touches `pools_lock` + `pools` (module-scoped state)."""
    with pools_lock:
        return (len(pools['poly']['near'])
                + len(pools['kalshi']['near'])
                + len(pools['sx']['near'])
                + len(pools.get('lim', {'near': []})['near']))


# Phase audit-28d (27.05.2026) — /api/portfolio_positions extracted to
# radar.api.analytics_api. -140 dead lines removed from this file.

# Phase audit-28d (27.05.2026) — /api/analytics and /api/analytics/history
# extracted to radar.api.analytics_api. Blueprint registered at boot.

# ── Phase 2: paper trading dashboard endpoints ───────────────────
# ── Phase 5: paper trading + graduation gate endpoints ───────────
# /api/lim_ws_health + /api/poly_ws_health extracted to
# radar.api.ws_health (audit-28b cont 4, 28.05.2026). Blueprint
# registered via register_api_blueprints() at boot.

# /api/wallets + /api/rebalance/proposals extracted to
# radar.api.wallets (audit-28b cont 4). Blueprint registered at boot.


# ── Phase 19v35 (09.05.2026) — public read-only recent-deals endpoint ─
# Operator's pain: nginx basic auth protects /api/analytics, /api/scan_state,
# /api/deals_history etc. for production safety. The agent maintaining
# this radar (and any external read-only observer) couldn't verify deal
# flow without operator running `docker exec` dumps every time. v35 adds
# a single PUBLIC endpoint that returns the last N analytics events with
# all PII fields stripped (token IDs, wallet addresses, market hashes,
# signatures, API keys, salts). Sensitive economic fields (sum_cents,
# net, roi, grade) ARE exposed because they're already visible on the
# dashboard's public landing page anyway.
#
# IMPORTANT: this endpoint MUST be whitelisted in nginx (auth_basic off)
# for the path /api/recent_deals — otherwise the basic auth wrapper
# still blocks it. See docs/PUBLIC_AUDIT_ENDPOINT.md for the nginx
# config snippet.
ALLOWED_DEAL_FIELDS = frozenset({
    # Time + identity
    'type', 'ts', 'key', 'arb_id',
    # Market structure
    'title', 'platform', 'arb_structure', 'cross_structure', 'structure',
    # Economics (already public on the dashboard)
    'sum_cents', 'total_cents', 'threshold_cents',
    'net', 'net_cents',
    'gross', 'gross_pct', 'fee', 'fee_pct',
    'roi', 'adj', 'adj_roi',
    'slip_pct', 'slip_cost',
    # Quality
    'grade', 'min_liq', 'balance_used', 'theta',
    'confidence',
    # Calendar
    'end_date',
    # NB: explicitly NOT in allowlist —
    #   token_id / token_id_yes / token_id_no  (Polymarket CTF IDs)
    #   marketHash / market_hash               (SX Bet)
    #   slug                                    (Limitless market slug)
    #   wallet / address / signer / maker      (any address)
    #   signature / sig / takerSig             (EIP-712 sigs)
    #   salt                                    (order entropy — could correlate)
    #   poly_api_key / api_secret              (L2 creds)
    #   verifying_contract                      (revealed by side-channel?)
    #   conditionId                             (Polymarket parent)
    #   body / order                            (full POST body — has it all)
    #   entries / legs                          (each leg has token + price + stake)
})


# Phase audit-28d (27.05.2026) — /api/active_deals and /api/recent_deals
# extracted to radar.api.deals. The blueprint version uses a streaming
# read instead of the tail-with-seek optimization; for typical jsonl
# file sizes (<10MB) this is fine. The tail-with-seek will return in a
# follow-up perf PR.


# ── Phase 19v33 (08.05.2026) — version endpoint for deploy verification ─
# Phase deploy-fix-2 found that v29-v32 PR fixes were merging into main
# but NEVER running on production: Dockerfile uses `COPY Scripts/`, so
# `docker restart` (without --build) kept serving the old image. Fixed
# by switching to `docker compose up --build` in deploy.yml. To make
# sure that class of silent staleness can never recur, we now stamp the
# git commit into the image at build time and expose it here. The CI
# workflow asserts that `/api/version` returns the expected sha after
# deploy — any mismatch fails the run loudly rather than silently
# leaving stale code running.
# Phase audit-28d (27.05.2026) — `/api/version` extracted to
# `radar.api.version`. Blueprint registered below at app boot via
# `register_api_blueprints(app)` (see end of this module).


# ── Phase 3: risk management endpoints ───────────────────────────
# Phase audit-28d (27.05.2026) — /api/risk_status and /api/network_status
# extracted to radar.api.admin. Heavy admin endpoints (/api/kill,
# /api/unkill, /api/reset) stay here pending an auth-aware extraction.


# /api/scan_health + /api/ts_metrics extracted to radar.api.stats
# (audit-28b cont 5, 28.05.2026). Blueprint registered at boot.
#
# /api/kill + /api/risk_resume + /api/dryfire extracted to radar.api.admin
# (audit-28b cont 5). Backward-compat shim: legacy tests reference
# `arb_server.ADMIN_KILL_TOKEN` / `APPROVE_LIST_HARD_CAP` / `TITLE_MAX_LEN`
# directly (mock.patch.object). Re-export those module attrs so the
# extraction is transparent to existing test infrastructure.
ADMIN_KILL_TOKEN = os.environ.get('ADMIN_KILL_TOKEN', '').strip()
from radar.api.admin import APPROVE_LIST_HARD_CAP, TITLE_MAX_LEN  # noqa: E402,F401


# on_lim_ws_update extracted to radar.ws.callbacks (re-exported above).


def on_poly_fill(event):
    """Polymarket user-channel `trade` event → fills.registry.consume.

    Phase 9f bridge — same role as on_lim_fill but for Polymarket. Polymarket
    pushes `trade` lifecycle events (MATCHED → MINED → CONFIRMED). We latch
    on MATCHED (status='MATCHED' or 'matched') because that's when the
    on-chain match exists; CONFIRMED comes later when the tx is mined and
    only useful for reconcile, not for atomic-wake.

    Event shape per Polymarket docs:
      {event_type:'trade', id, taker_order_id, maker_orders[],
       market, asset_id, side, size, price, status, timestamp, ...}
    `taker_order_id` is the order_id WE sent in our POST. Match by that.
    """
    if not event:
        return
    typ = (event.get('event_type') or event.get('type') or '').lower()
    if typ != 'trade':
        return
    status = (event.get('status') or '').upper()
    # Latch on MATCHED only — CONFIRMED arrives later (mined). Both have the
    # same fill price/size, so consuming on MATCHED is the right speed/safety.
    if status not in ('MATCHED', 'CONFIRMED'):
        return

    order_id = (event.get('taker_order_id') or event.get('order_id')
                or event.get('orderId'))
    market = event.get('market')        # condition_id
    try:
        fill_price = float(event.get('price', 0) or 0) or None
    except Exception:
        fill_price = None
    try:
        fill_size = float(event.get('size', 0) or 0)
    except Exception:
        fill_size = None

    result = {
        'fill_price': fill_price,
        'fill_size_usdc': fill_size,
        'status': status,
        'asset_id': event.get('asset_id'),
        'condition_id': market,
        'raw': event,
    }
    try:
        from executor import fills as _fills_mod
    except Exception:
        return

    consumed = None
    if order_id:
        consumed = _fills_mod.registry.consume_by_order_id(
            'polymarket', str(order_id), result)
    if consumed is None and market:
        # Fallback: SETTLEMENT events that don't carry our orderId, key by
        # condition_id (we register slug=condition_id for poly legs).
        consumed = _fills_mod.registry.consume_by_slug(
            'polymarket', market, result)

    if consumed:
        print(f"[POLY FILL] {status} → arb {consumed.arb_id} leg {consumed.leg_idx} "
              f"(price={fill_price})")


def on_lim_fill(event):
    """Authenticated `orderEvent` push from Limitless WS.

    Phase 9e (28.04.2026): the bridge from Limitless WS to executor's
    fills.registry. atomic._fire_one_leg_live registers a future on
    (platform='limitless', order_id=...) and waits on its Event. When
    this callback fires, we look up the registration and set the Event
    so atomic wakes immediately instead of waiting for the 5s dead-man.

    Two event shapes per docs:
      - source='OME': matching-engine fill, has takerOrderId / orderId,
        marketId/slug, price, remainingSize.
      - source='SETTLEMENT': on-chain settlement, has takerOrderId,
        marketSlug, txHash, makerMatches[].

    We try by orderId first, fall back to slug (FIFO across same-slug
    legs of one arb). Either way fills.registry pops the registration
    and sets its Event.
    """
    if not event:
        return
    src = event.get('source', '?')
    typ = event.get('type', '?')

    # Build a normalised result dict for atomic to consume
    result = {
        'fill_price': float(event.get('price', 0) or 0) or None,
        'fill_size_usdc': None,
        'remaining_size': event.get('remainingSize'),
        'source': src,
        'type': typ,
        'tx_hash': event.get('txHash'),
        'raw': event,
    }
    # remainingSize > 0 means partial fill — still wake atomic but keep
    # status info in `result` so it can decide reversal later.
    try:
        from executor import fills as _fills_mod
    except Exception:
        return

    order_id = (event.get('orderId') or event.get('takerOrderId')
                or event.get('order_id'))
    slug = event.get('marketSlug') or event.get('slug')

    consumed = None
    if order_id:
        consumed = _fills_mod.registry.consume_by_order_id('limitless', str(order_id), result)
    if consumed is None and slug:
        consumed = _fills_mod.registry.consume_by_slug('limitless', slug, result)

    if consumed:
        print(f"[LIM FILL] {src}/{typ} → arb {consumed.arb_id} leg {consumed.leg_idx} "
              f"(price={result['fill_price']})")
    else:
        # Not our fill — every market push lands here too. Quiet at INFO level.
        pass


def executor_atomic_dry_run():
    """Helper for startup banner — returns True if executor is in dry-run mode.

    Defined BEFORE `_bootstrap_radar` because `_bootstrap_radar` calls it at
    module import time under gunicorn (the `if not _skip_bootstrap`
    block below). Earlier placement after the bootstrap call caused
    `NameError: 'executor_atomic_dry_run' is not defined` to swallow the
    startup telegram notification — radar still worked, but the operator
    got no `Mode: LIVE` ping after flipping `DRY_RUN=0`.
    """
    try:
        from executor.atomic import DRY_RUN
        return DRY_RUN
    except Exception:
        return True


def _bootstrap_radar():
    """Phase 9ccc — bootstrap function callable from BOTH dev (`__main__`)
    and gunicorn `--preload` paths. Was previously inline in the
    `if __name__ == '__main__':` block which gunicorn does NOT execute
    (gunicorn imports the module, never invokes __main__). Result: under
    gunicorn the WS clients + scan_loop never started, dashboard sat
    permanently with empty data.

    Called once at module-import time (after class/func defs) so both
    `python arb_server.py` and `gunicorn arb_server:app` end up with a
    fully-running radar."""
    global ws_client, lim_ws_client
    # Start Polymarket WS client (idle until first scan populates pools)
    ws_client = PolyMarketWS(on_update=on_ws_update, max_subs=MAX_WS_SUBS, verbose=True)
    ws_client.start()
    # Phase 9ddd (29.04.2026) — Limitless WS made OPTIONAL via env flag.
    # Reason: python-socketio's reconnect loop can hold the GIL during
    # disconnect cascades on flaky Limitless TCP (we measured 4341ms
    # max TLS handshake), starving the scan thread → 761s "hangs".
    # ENABLE_LIMITLESS_WS=0 keeps Limitless DATA flowing (REST polling
    # via micro_loop every LIMITLESS_MICRO_INTERVAL=5s) but avoids the
    # GIL contention. Lose: real-time push (5s latency vs 200ms via WS).
    # Win: stable scans, no hangs.
    # Default: 0 (REST-only) until dr-manhattan migration replaces
    # python-socketio with async client (Phase 9eee+).
    enable_lim_ws = os.environ.get('ENABLE_LIMITLESS_WS', '0') != '0'
    if ENABLE_LIMITLESS and enable_lim_ws:
        # API key is optional for public market data, REQUIRED for authenticated
        # channels (orderEvent / positions). LimitlessWS gracefully skips
        # auth-only subscriptions if api_key is empty — public stream still works.
        # Phase TS-5f.3 — pass HMAC secret if configured (post-14.05.2026
        # Limitless V2 needs it for `subscribe_order_events` /
        # `subscribe_positions` channels). Public market data subscribes
        # work without auth, so the constructor accepts both None.
        _lim_api_secret = os.environ.get('LIMITLESS_API_SECRET', '').strip() or None
        lim_ws_client = LimitlessWS(
            on_update=on_lim_ws_update,
            max_subs=LIMITLESS_MAX_WS_SUBS,
            verbose=False,
            api_key=LIMITLESS_API_KEY or None,
            api_secret=_lim_api_secret,
            on_fill=on_lim_fill,
        )
        lim_ws_client.start()
    elif ENABLE_LIMITLESS:
        print("[Limitless] WS DISABLED (ENABLE_LIMITLESS_WS=0) — REST-only "
              "polling mode. Trade-off: 5s update latency vs 200ms via WS, "
              "but no GIL contention from socketio reconnect loop.",
              flush=True)

    # Phase 9f: Polymarket user-channel WS, one per wallet that has L2 creds.
    # Skips wallets without poly_api_key/secret/passphrase silently.
    for w in _wallet_pool.wallets:
        if getattr(w, 'has_poly_creds', False):
            client = PolyUserWS(wallet=w, on_fill=on_poly_fill, verbose=False)
            client.start()
            poly_user_ws_clients.append(client)

    # Initialize analytics (loads persisted state, if any)
    analytics.init()

    # Warm cache — load the previous scan_data snapshot so /api/deals is
    # not empty while the first cold run_scan() is still in flight.
    _restore_scan_state()

    threading.Thread(target=scan_loop, daemon=True).start()
    # Phase audit-2 (11.05.2026) — exchange RTT shadow probe. Daemon
    # thread polls a no-auth GET on each exchange every 60s; the radar
    # uses the results as a lower-bound estimate for real-mode POST
    # latency. Surfaced via /api/exchange_rtt. Does not interfere with
    # main fetch loops — uses its own connection.
    try:
        import exchange_latency_probe as _rtt_probe
        _rtt_probe.start()
    except Exception as _e:
        print(f"[BOOT] exchange_latency_probe start failed (non-fatal): {_e}",
              flush=True)
    if ENABLE_KALSHI:
        threading.Thread(target=kalshi_micro_loop, daemon=True).start()
    if ENABLE_SX:
        threading.Thread(target=sx_micro_loop, daemon=True).start()
    if ENABLE_LIMITLESS:
        threading.Thread(target=limitless_micro_loop, daemon=True).start()
    threading.Thread(target=poly_micro_fallback_loop, daemon=True).start()
    threading.Thread(target=analytics_loop, daemon=True).start()

    # Phase 3: position reconciliation runs every 60s, halts on mismatch.
    # Phase 9f: register the Polymarket fetcher (GET /data/positions with
    # L2 HMAC auth). Each wallet that has poly creds adds its own fetcher;
    # the loop merges them into a single remote view. Without creds we
    # silently skip — local-only reconcile is still safe for paper-trade.
    from risk import reconcile as _reconcile

    def _make_poly_fetcher(w):
        # Phase 19v25 (05.05.2026) — route GET /data/positions per-bot
        # SOCKS5 via poly_l2_http.l2_request. Each bot has a pinned
        # exit IP (port = POLY_PROXY_PORT_BASE + bot_index) so
        # Polymarket sees consistent geo per wallet.
        #
        # Phase audit (11.05.2026) — guard the import. poly_l2_http
        # never made it from feature/ts-5a-real-http-fires branch onto
        # main (only existed in stash). Without the guard, ANY wallet
        # with poly creds triggers ImportError during _bootstrap_radar
        # → entire reconciliation layer fails silently. Gracefully skip
        # poly reconciliation; local-only reconcile still safe for
        # paper-trade and real-mode with fill-confirmation via WS.
        try:
            from poly_l2_http import l2_request as _l2
        except ImportError:
            print(f"  [RECONCILE poly fetcher {w.bot_id}] poly_l2_http "
                  "not available — skipping remote position fetch")
            return None
        def fetch():
            try:
                r = _l2(method='GET', path='/data/positions',
                         wallet=w, timeout=_FETCH_TIMEOUT)
                if r.status_code != 200:
                    return {}
                data = r.json() or []
                out = {}
                for p in (data if isinstance(data, list)
                          else data.get('positions') or []):
                    market = (p.get('conditionId') or p.get('condition_id')
                              or p.get('market'))
                    outcome = p.get('outcome') or p.get('outcomeIndex')
                    size = p.get('size') or p.get('amount') or 0
                    if market and outcome is not None:
                        try:
                            out[('Polymarket', market, outcome)] = float(size)
                        except Exception: pass
                return out
            except Exception as e:
                print(f"[RECONCILE poly fetcher {w.bot_id}] {e}")
                return {}
        return fetch

    poly_fetchers_registered = 0
    for w in _wallet_pool.wallets:
        if getattr(w, 'has_poly_creds', False):
            fetcher = _make_poly_fetcher(w)
            if fetcher is not None:
                _reconcile.register_exchange_fetcher(fetcher)
                poly_fetchers_registered += 1
    if poly_fetchers_registered > 0:
        print(f"  Reconcile: Polymarket fetcher registered ({poly_fetchers_registered} wallet(s))")

    # Phase 9e: register the Limitless fetcher (WS-positions-cache primary,
    # REST /portfolio fallback). This is the FIRST live exchange fetcher
    # since cancel/positions on Limitless authenticate via X-API-Key, no
    # private key required.
    if ENABLE_LIMITLESS and lim_ws_client is not None:
        from risk import reconcile as _reconcile

        def _fetch_limitless_positions_for_reconcile():
            """Use the WS-pushed positions cache when fresh (<60s old),
            otherwise fall back to REST. Returns the same shape every
            other reconcile fetcher does: dict keyed by
            (platform, market_id, outcome) → size_usdc."""
            age = lim_ws_client.positions_age_s() if lim_ws_client else None
            if age is not None and age < 60:
                return lim_ws_client.get_positions_snapshot()
            # REST fallback. Only attempt when api_key present (auth).
            if not LIMITLESS_API_KEY:
                return {}
            try:
                # Try /portfolio/{eth_address}; on shape change, return {}
                # rather than raising — reconcile treats fetcher errors
                # as "remote unknown" and we don't want to halt the loop
                # over a docs-changed endpoint.
                addr = next(
                    (w.eth_address for w in _wallet_pool.wallets if w.eth_address),
                    None,
                )
                if not addr:
                    return {}
                # Phase TS-5f.2 (14.05.2026) — HMAC-signed auth.
                # Trading-scope tokens reject the legacy X-API-Key.
                portfolio_url = f"{LIMITLESS_API_BASE}/portfolio/{addr}"
                lim_secret = os.environ.get('LIMITLESS_API_SECRET', '').strip() or None
                try:
                    from limitless_hmac import lmts_headers_or_legacy
                    auth_headers = lmts_headers_or_legacy(
                        LIMITLESS_API_KEY, lim_secret, 'GET',
                        portfolio_url, '')
                except ImportError:
                    auth_headers = {'X-API-Key': LIMITLESS_API_KEY}
                r = _SESS_LIM.get(
                    portfolio_url,
                    headers=auth_headers,
                    timeout=_FETCH_TIMEOUT,
                )
                if r.status_code != 200:
                    return {}
                positions = r.json() or []
                out = {}
                for p in (positions if isinstance(positions, list)
                          else positions.get('positions') or []):
                    slug = p.get('marketSlug') or p.get('slug')
                    outcome = p.get('outcome') or p.get('outcomeIndex')
                    size = p.get('size') or p.get('amount') or 0
                    if slug and outcome is not None:
                        try:
                            out[('Limitless', slug, outcome)] = float(size)
                        except Exception: pass
                return out
            except Exception as e:
                print(f"[RECONCILE limitless fetcher] {e}")
                return {}

        _reconcile.register_exchange_fetcher(
            _fetch_limitless_positions_for_reconcile)
        print("  Reconcile: Limitless fetcher registered (WS cache + REST fallback)")

    risk_mod.start_reconcile_loop()

    print("=" * 60)
    print("  ARBITRAGE RADAR v7 — http://localhost:5050")
    poly_total = POLY_MAIN_PAGES * 500
    kalshi_str = "1000 events" if ENABLE_KALSHI else "DISABLED"
    sx_str = "up to 1000 markets" if ENABLE_SX else "DISABLED"
    lim_total = LIMITLESS_MAIN_PAGES * 100
    lim_str = f"up to {lim_total} markets" if ENABLE_LIMITLESS else "DISABLED"
    print(f"  Poly ({poly_total}) + Kalshi ({kalshi_str}) + SX Bet ({sx_str}) + Limitless ({lim_str})")
    print(f"  HOT/NEAR pools (buffer={NEAR_BUFFER:.2f})")
    print(f"  Polymarket WS: max {MAX_WS_SUBS} subs, ping every 10s")
    if ENABLE_LIMITLESS:
        print(f"  Limitless WS:  max {LIMITLESS_MAX_WS_SUBS} subs, ping every 15s")
    if ENABLE_KALSHI:
        print(f"  Kalshi REST micro: every {KALSHI_MICRO_INTERVAL}s on HOT+NEAR")
    if ENABLE_SX:
        print(f"  SX Bet REST micro: every {SX_MICRO_INTERVAL}s on HOT+NEAR (live sport)")
    print(f"  Risk: max ${risk_mod.MAX_PER_TRADE_USD:.0f}/trade, "
          f"daily loss limit -${risk_mod.DAILY_LOSS_LIMIT_USD:.0f}, "
          f"{risk_mod.LOSING_TRADES_PER_HOUR} losing/h → 1h pause")
    if risk_mod.is_killed():
        print("  ⚠ KILL SWITCH ACTIVE (Executions/.killed exists) — fires blocked")
    # Phase 4: wallet pool status
    n = len(_wallet_pool.wallets)
    sig = sum(1 for w in _wallet_pool.wallets if w.can_sign)
    print(f"  Wallets: {n} bot(s) loaded ({sig} can sign), backend={_wallet_pool.backend}"
          + (" — empty pool, executor falls back to mock stub" if n == 0 else ""))
    # Network safety (Layer 3) — fetch own IP/country at startup so the
    # operator sees immediately if VPS is on the wrong network.
    if risk_mod.ALLOWED_COUNTRIES:
        ip, country, err = risk_mod.get_current_ip_country(force_refresh=True)
        if err:
            print(f"  ⚠ Network: ALLOWED={','.join(sorted(risk_mod.ALLOWED_COUNTRIES))} "
                  f"— check FAILED ({err[:60]}). Fires will be blocked until network recovers.")
        elif country in risk_mod.ALLOWED_COUNTRIES:
            print(f"  Network: ALLOWED={','.join(sorted(risk_mod.ALLOWED_COUNTRIES))} "
                  f"| current IP {ip} ({country}) → ✓ allowed")
        else:
            print(f"  ⚠ Network: ALLOWED={','.join(sorted(risk_mod.ALLOWED_COUNTRIES))} "
                  f"| current IP {ip} ({country}) → ✗ DISALLOWED. Fires WILL be blocked.")
    else:
        print(f"  Network: ALLOWED_COUNTRIES not set — geo check DISABLED "
              f"(safe for local dev; set on VPS, e.g. ALLOWED_COUNTRIES=GE)")
    print("============================================================")

    # Phase 8: notify operator on startup so they know radar booted (esp.
    # after a crash/restart). Telegram envvars optional; no-op if unset.
    try:
        import notify
        if notify.is_configured():
            sig_count = sum(1 for w in _wallet_pool.wallets if w.can_sign)
            startup_msg = (
                f'*Radar started*\n'
                f'Mode: `{"DRY_RUN" if executor_atomic_dry_run() else "LIVE"}`\n'
                f'Platforms: Poly={poly_total}'
                + (' Limitless+SX ON' if ENABLE_SX else ' (SX disabled)')
                + f'\nWallets: {len(_wallet_pool.wallets)}/6 ({sig_count} can sign)'
                + (f'\nNetwork: {",".join(sorted(risk_mod.ALLOWED_COUNTRIES))}'
                   if risk_mod.ALLOWED_COUNTRIES else '\nNetwork check: DISABLED')
            )
            notify.send(startup_msg, level='success', dedupe_key='radar_startup')
    except Exception as _e:
        print(f"  (telegram startup notify skipped: {_e})")

    # End of _bootstrap_radar body — `app.run` is NOT here. Dev path
    # below calls bootstrap then app.run; WSGI path calls bootstrap on
    # import, gunicorn handles its own listener.
    return


# Phase 9ccc — auto-bootstrap on import. Under gunicorn `--preload` the
# WSGI loader imports this module ONCE in the master process before
# fork()ing workers; our bootstrap runs there, then workers inherit the
# already-running ws_client / scan_loop threads via fork.
# Skip when running under pytest / unittest discovery (sys.argv[0] tells).
import sys as _sys
_skip_bootstrap = (
    'unittest' in (_sys.argv[0] if _sys.argv else '') or
    'pytest' in (_sys.argv[0] if _sys.argv else '') or
    any('test' in _a for _a in _sys.argv) or
    os.environ.get('SKIP_BOOTSTRAP') == '1'   # opt-out for tests
)
if not _skip_bootstrap and __name__ != '__main__':
    # Under WSGI (gunicorn). Run bootstrap NOW (idempotent at module level).
    _bootstrap_radar()


if __name__ == '__main__':
    _bootstrap_radar()
    app.run(host='0.0.0.0', port=5050, debug=False, threaded=True)
