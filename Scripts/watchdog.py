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


def _on_kill_detected(reason: str, pool):
    """Fired ONCE per kill transition (we track was_killed across iterations).

    Phase 4+ will fill this with real cancel-order logic per wallet:
        for w in pool.wallets:
            if not w.can_sign: continue
            cancel_polymarket_orders(w)
            cancel_sx_orders(w)
            log to Executions/watchdog.jsonl
    Phase 6 ships an empty stub so the container runs today.
    """
    log.warning("KILL DETECTED — reason=%s. Cancel hooks (Phase 4) would run here.",
                reason)
    sig = sum(1 for w in pool.wallets if w.can_sign)
    _heartbeat({
        'event': 'kill_detected',
        'reason': reason,
        'wallets_can_sign': sig,
        'wallets_total': len(pool.wallets),
        'note': ('Phase 6 stub — real cancel API calls land in Phase 4'
                 ' once wallet keys are loaded'),
    })


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
