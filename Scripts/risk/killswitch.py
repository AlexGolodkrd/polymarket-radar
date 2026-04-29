"""Kill switch — file-flag based, watchdog-friendly.

Two principles from the user feedback (27.04.2026):
    - UI must require *double* confirmation (modal "Точно остановить?" → second click)
    - Kill cancels PENDING orders only — open positions are NEVER closed.

Why a file flag instead of in-process state? A separate watchdog process
(Phase 4 will spin one up via systemd / Windows Task Scheduler) polls the
flag every second. If the main radar process has hung or crashed, the
watchdog still sees the flag and can cancel pending orders directly via
each exchange's REST API. This makes the kill switch survive a hung
Python interpreter.

Phase 3 implements:
    - The flag file at Executions/.killed
    - is_killed(), kill(reason), unkill()
    - cancel_all_pending() hook — Phase 4 wires it to real cancel APIs;
      here it logs intent so the wiring is visible/testable
"""
import json
import logging
import os
import threading
import time
from typing import Optional

log = logging.getLogger(__name__)

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_THIS_DIR, '..', '..'))
EXECUTIONS_DIR = os.path.join(_REPO_ROOT, 'Executions')
KILL_FLAG_PATH = os.path.join(EXECUTIONS_DIR, '.killed')
KILL_LOG_PATH = os.path.join(EXECUTIONS_DIR, 'killswitch.jsonl')

_lock = threading.Lock()
# Optional cancel-pending callbacks registered by Phase 4 wallet manager.
# Each callback runs once on kill and should be idempotent + non-blocking.
_cancel_callbacks: list = []


def _ensure_dir():
    os.makedirs(EXECUTIONS_DIR, exist_ok=True)


def _append_log(event: dict):
    _ensure_dir()
    event = {**event, 'ts': time.time()}
    with open(KILL_LOG_PATH, 'a', encoding='utf-8') as f:
        f.write(json.dumps(event, default=str) + '\n')


# ── Public API ──────────────────────────────────────────────────────
def is_killed() -> bool:
    """Check kill flag. Phase 9i (28.04.2026) fix: **fail-closed** —
    if the filesystem call raises (permission denied, disk error, mount
    point dropped, etc.) we ASSUME killed and refuse to fire.

    Original `os.path.exists()` returns False on permission errors,
    which made the kill switch fail-OPEN — operator hits STOP, fs
    permissions hiccup, executor never sees the kill, fires anyway.
    For a safety mechanism that's the wrong default."""
    # Phase 9tt — declare `global` at the function top, NOT inside the
    # except block. Python's `global` statement lexically applies to the
    # whole function, but having it after a non-trivial code path is a
    # PEP-8 anti-pattern that some linters flag and that humans
    # misinterpret. Declared up-front: this function ALSO assigns
    # _last_kill_check_error, so it must be global throughout.
    global _last_kill_check_error
    try:
        return os.path.exists(KILL_FLAG_PATH)
    except Exception as e:
        # Log once per process — don't spam (called every fire_arb).
        if (not _last_kill_check_error
                or time.time() - _last_kill_check_error > 60):
            log.warning("is_killed() filesystem error — assuming KILLED "
                        "(fail-closed): %s", e)
            _last_kill_check_error = time.time()
        return True


_last_kill_check_error = 0.0


def kill(reason: str = 'manual') -> dict:
    """Trip the kill switch. Idempotent — calling on an already-killed
    state returns the existing flag info."""
    _ensure_dir()
    with _lock:
        already = is_killed()
        info = {'reason': reason, 'set_at': time.time(), 'pid': os.getpid()}
        if not already:
            with open(KILL_FLAG_PATH, 'w', encoding='utf-8') as f:
                json.dump(info, f, indent=2)
            log.warning("KILL SWITCH ACTIVATED — reason=%s", reason)
            _append_log({'event': 'kill', **info})
            # Run cancel-pending callbacks (Phase 4 wires real ones)
            for cb in list(_cancel_callbacks):
                try:
                    cb(reason)
                except Exception as e:
                    log.warning("cancel callback failed: %s", e)
            # Phase 8: Telegram alert. Lazy import so circular deps are
            # impossible; notify is a no-op if env not configured.
            try:
                import sys, os as _os
                sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
                import notify
                notify.send(
                    f'*KILL SWITCH ACTIVATED*\n`reason: {reason}`\n'
                    f'New fires blocked. Existing positions NOT closed.',
                    level='crit', dedupe_key='killswitch_active',
                )
            except Exception as e:
                log.warning("notify on kill failed: %s", e)
        else:
            log.info("kill() called but already killed")
        return _read_flag() or info


def unkill(reason: str = 'manual_resume') -> bool:
    """Clear the kill flag. Returns True if it was previously set.
    Note: unkill does NOT auto-resume trading — paused_until in risk
    state may still be active (e.g. daily loss limit). Operator must
    also clear those (or let them expire)."""
    with _lock:
        was = is_killed()
        if was:
            try:
                os.remove(KILL_FLAG_PATH)
            except FileNotFoundError:
                pass
            log.info("kill switch CLEARED — reason=%s", reason)
            _append_log({'event': 'unkill', 'reason': reason})
            # Phase 8: notify on resume too — operator wants to confirm
            # the unkill was processed.
            try:
                import sys, os as _os
                sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
                import notify
                notify.send(
                    f'*Kill switch cleared*\n`reason: {reason}`\nTrading may resume.',
                    level='success', dedupe_key=f'unkill:{reason}:{int(time.time()//60)}',
                )
                # Clear the kill dedupe so a future kill triggers a fresh alert
                with notify._last_sent_lock:
                    notify._last_sent.pop('killswitch_active', None)
            except Exception as e:
                log.warning("notify on unkill failed: %s", e)
        return was


def _read_flag() -> Optional[dict]:
    if not is_killed(): return None
    try:
        with open(KILL_FLAG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {'corrupted': True}


def status() -> dict:
    info = _read_flag()
    return {
        'killed': info is not None,
        'flag_info': info,
        'flag_path': KILL_FLAG_PATH,
        'callbacks_registered': len(_cancel_callbacks),
    }


# ── Hooks (Phase 4 wallet manager will register real callbacks) ────
def register_cancel_callback(cb):
    """Phase 4: wallet manager registers a callback that POSTs DELETE
    /order to each exchange for every pending order_id from the fills
    registry. Phase 3 leaves this list empty."""
    _cancel_callbacks.append(cb)


def clear_cancel_callbacks():
    """Test helper — let unit tests reset between runs."""
    _cancel_callbacks.clear()
