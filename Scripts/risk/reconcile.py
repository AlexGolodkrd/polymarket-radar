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
                key = (row.get('platform'), row.get('market_id'), row.get('outcome'))
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
            remote.update(fn() or {})
        except Exception as e:
            fetcher_errors.append(f'{fn.__name__}: {e}')

    mismatches = _diff_positions(local, remote)
    ok = (not mismatches) and (not fetcher_errors)

    if not ok:
        msg = (f'mismatch: {len(mismatches)} keys differ' if mismatches
               else f'fetcher errors: {fetcher_errors}')
        log.warning("RECONCILE FAILED — %s", msg)
        # Halt: trip kill switch (operator must explicitly resume after
        # investigating). This matches "Расхождение → паника, остановка"
        # in the original plan.
        killswitch.kill(reason=f'reconcile_mismatch: {msg}')
        _append(RECONCILE_LOG, {
            'event': 'mismatch', 'mismatches': mismatches,
            'fetcher_errors': fetcher_errors, 'ts': started,
        })
    else:
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
