"""Standalone watchdog process — polls Executions/.killed every second.

Why separate process? If the main radar (arb_server.py) has hung or
crashed, file-flag-based kill switch may already be tripped but pending
orders won't get cancelled because the radar can't run cancel logic.
The watchdog reads the same flag from a different process, so a frozen
Python interpreter in the radar container doesn't block kill execution.

Phase 6 implements:
  - The poll loop (1Hz)
  - Detection of new kill events vs already-handled
  - Stub call to wallet manager's cancel-pending hook (Phase 4 wires real
    cancel REST calls; Phase 6 leaves the hook empty so deployment is
    runnable today)
  - Heartbeat row to Executions/watchdog.jsonl every 5 minutes for ops

This script is meant to run as a sibling container in docker-compose.yml;
it can also run standalone (`python Scripts/watchdog.py`) for local dev.
"""
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

# Make sibling packages importable
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from risk import killswitch
import wallets as wallets_mod

# Limitless cancel uses API key (no signature) — works even when wallets
# can't sign. We pull it from env so the watchdog has the same view as
# arb_server. Optional: if unset, we skip Limitless cancellation (still
# logged for ops).
import requests as _requests   # alias keeps the import section tidy
LIMITLESS_API_KEY = os.environ.get('LIMITLESS_API_KEY', '').strip()
LIMITLESS_API_BASE = os.environ.get('LIMITLESS_API_BASE',
                                    'https://api.limitless.exchange').rstrip('/')

logging.basicConfig(level=logging.INFO,
                    format='[watchdog] %(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('watchdog')

POLL_INTERVAL_S = 1.0          # check the flag this often
HEARTBEAT_INTERVAL_S = 300     # log a heartbeat every N seconds

EXECUTIONS_DIR = killswitch.EXECUTIONS_DIR
HEARTBEAT_LOG = os.path.join(EXECUTIONS_DIR, 'watchdog.jsonl')


def _heartbeat(extra: dict = None):
    os.makedirs(EXECUTIONS_DIR, exist_ok=True)
    row = {
        'event': 'heartbeat',
        'ts': time.time(),
        'utc': datetime.now(timezone.utc).isoformat(),
        'killed': killswitch.is_killed(),
    }
    if extra:
        row.update(extra)
    with open(HEARTBEAT_LOG, 'a', encoding='utf-8') as f:
        f.write(json.dumps(row, default=str) + '\n')


def _cancel_limitless_pending():
    """Fetch this account's open Limitless orders and cancel-batch them.

    Limitless cancel auth is **API-key based** (no signature), so this works
    even before wallet private keys are configured — making it the first real
    cancellation path the watchdog can drive end-to-end.

    Returns dict with counts so the heartbeat row can show what happened.
    """
    if not LIMITLESS_API_KEY:
        return {'lim_skipped': 'no LIMITLESS_API_KEY in env'}
    # Phase TS-5f.3 (14.05.2026) — HMAC-signed auth. Trading-scope
    # tokens reject the legacy X-API-Key bearer.
    import json as _json
    import os as _os
    import sys as _sys
    # Soft-import the HMAC helper. watchdog is a separate process from
    # the radar so its sys.path may not include Scripts/ — fix up.
    _scripts_dir = _os.path.dirname(_os.path.abspath(__file__))
    if _scripts_dir not in _sys.path:
        _sys.path.insert(0, _scripts_dir)
    try:
        from limitless_hmac import lmts_headers_or_legacy as _sign
    except ImportError:
        _sign = None
    lim_secret = _os.environ.get('LIMITLESS_API_SECRET', '').strip() or None

    def _auth(method: str, url: str, body_str: str = '') -> dict:
        if _sign is not None:
            return _sign(LIMITLESS_API_KEY, lim_secret, method, url, body_str)
        return {'X-API-Key': LIMITLESS_API_KEY}

    try:
        # GET /orders/user — list every open order on this account
        list_url = f"{LIMITLESS_API_BASE}/orders/user"
        r = _requests.get(
            list_url,
            headers=_auth('GET', list_url, ''),
            timeout=10,
        )
        if r.status_code != 200:
            return {'lim_list_status': r.status_code, 'lim_list_err': r.text[:120]}
        data = r.json() or {}
        orders = data if isinstance(data, list) else (data.get('data') or data.get('orders') or [])
        ids = [o.get('id') or o.get('orderId') for o in orders
               if (o.get('id') or o.get('orderId'))]
        ids = [str(i) for i in ids if i]
        if not ids:
            return {'lim_open': 0, 'lim_cancelled': 0}
        # POST /orders/cancel-batch  body: {orderIds: [...]}
        cancel_url = f"{LIMITLESS_API_BASE}/orders/cancel-batch"
        # Serialize body ONCE — HMAC signs exact wire bytes.
        body_str = _json.dumps({'orderIds': ids}, separators=(',', ':'))
        rc = _requests.post(
            cancel_url,
            headers={**_auth('POST', cancel_url, body_str),
                     'Content-Type': 'application/json'},
            data=body_str,
            timeout=15,
        )
        return {
            'lim_open': len(ids),
            'lim_cancel_status': rc.status_code,
            'lim_cancelled': len(ids) if rc.status_code in (200, 202, 204) else 0,
        }
    except Exception as e:
        return {'lim_error': f"{type(e).__name__}: {str(e)[:120]}"}


def _cancel_polymarket_pending(pool):
    """For each wallet that has Polymarket L2 creds, dispatch DELETE /orders
    (cancel-all) with HMAC auth. Phase 9f — completes the watchdog → cancel
    chain for Polymarket without needing private-key signatures (HMAC uses
    api_key/secret/passphrase only).
    """
    if not pool or not pool.wallets:
        return {'poly_skipped': 'empty wallet pool'}
    try:
        from executor.builders import build_poly_hmac_headers, POLY_API_BASE
    except Exception as e:
        return {'poly_import_error': str(e)}

    cancelled = 0
    errors = []
    for w in pool.wallets:
        if not getattr(w, 'has_poly_creds', False):
            continue
        try:
            path = '/orders'
            headers = build_poly_hmac_headers(
                method='DELETE', path=path, body='',
                api_key=w.poly_api_key,
                api_secret=w.poly_secret,
                passphrase=w.poly_passphrase,
                eth_address=w.eth_address,
            )
            r = _requests.delete(f"{POLY_API_BASE}{path}",
                                 headers=headers, timeout=10)
            if r.status_code in (200, 202, 204):
                cancelled += 1
            else:
                errors.append(f"{w.bot_id}: HTTP {r.status_code}")
        except Exception as e:
            errors.append(f"{w.bot_id}: {type(e).__name__}: {str(e)[:80]}")
    return {
        'poly_wallets_cancelled': cancelled,
        'poly_errors': errors[:5] if errors else None,
    }


def _on_kill_detected(reason: str, pool):
    """Fired ONCE per kill transition (we track was_killed across iterations).

    Phase 9c (28.04.2026): wired Limitless cancellation (API-key auth).
    Phase 9f (28.04.2026): wired Polymarket cancellation (L2 HMAC auth —
    works without private keys). SX Bet still Phase 4 (signed orders).
    """
    log.warning("KILL DETECTED — reason=%s. Running cancel hooks.", reason)
    lim_result = _cancel_limitless_pending()
    poly_result = _cancel_polymarket_pending(pool)
    log.warning("Limitless cancel result: %s", lim_result)
    log.warning("Polymarket cancel result: %s", poly_result)

    sig = sum(1 for w in pool.wallets if w.can_sign)
    extras = {
        'event': 'kill_detected',
        'reason': reason,
        'wallets_can_sign': sig,
        'wallets_total': len(pool.wallets),
        'note': 'Limitless + Polymarket wired (auth-key based). SX still Phase 4.',
    }
    extras.update(lim_result)
    extras.update(poly_result)
    _heartbeat(extras)


def main():
    log.info("watchdog starting, polling %s every %.1fs",
             killswitch.KILL_FLAG_PATH, POLL_INTERVAL_S)

    # Load wallet pool once — watchdog uses the same backend as the radar
    # so cancel logic can authenticate per bot. If keys aren't loaded, the
    # watchdog still runs (just won't be able to cancel real orders).
    pool = wallets_mod.load_pool()
    log.info("wallet pool: %d bots loaded (%d can sign)",
             len(pool.wallets), sum(1 for w in pool.wallets if w.can_sign))

    was_killed = killswitch.is_killed()
    last_heartbeat = 0.0
    _heartbeat({'event': 'startup'})

    while True:
        try:
            now = time.time()
            current = killswitch.is_killed()
            if current and not was_killed:
                # Transition: not-killed → killed. Run cancel hook.
                flag = killswitch._read_flag() or {}
                _on_kill_detected(reason=flag.get('reason', 'unknown'), pool=pool)
            elif not current and was_killed:
                log.info("kill switch CLEARED")
                _heartbeat({'event': 'kill_cleared'})
            was_killed = current

            if now - last_heartbeat >= HEARTBEAT_INTERVAL_S:
                _heartbeat({'killed': current})
                last_heartbeat = now

            time.sleep(POLL_INTERVAL_S)
        except KeyboardInterrupt:
            log.info("watchdog stopping (SIGINT)")
            _heartbeat({'event': 'shutdown'})
            break
        except Exception as e:
            log.exception("watchdog loop error: %s — continuing", e)
            time.sleep(POLL_INTERVAL_S * 5)


if __name__ == '__main__':
    main()
