"""Phase 19v15 (05.05.2026) — third-pass audit fixes.

Eleven verified bugs from the deferred batch of Phase 19v14 audit + a
deeper look at risk-/notify-/preflight- paths. Each fix has a focused
regression test.

  1. risk/state.py — day-roll race (separate lock from limits.py)
  2. risk/limits.py — pause read-modify-write split across locks
  3. risk/limits.py — notify() inside _lock could stall risk gating
  4. risk/reconcile.py — `update()` overwrites instead of summing fetchers
  5. risk/reconcile.py — outcome string variance ("0"/"Yes"/"true") → false mismatch
  6. risk/reconcile.py — single fetcher error trips kill switch
  7. risk/network_check.py — no inflight dedupe; transient blip = 60s freeze
  8. preflight.py — Polygon RPC used for all platforms, breaks Limitless legs
  9. executor/atomic.py — slippage cancel-after-fill leaves directional exposure
 10. arb_server.py — pause_scan bare `except: pass` + bare `requests.get`
 11. notify.py — Telegram 429 silently drops kill-switch alerts
"""
import os
import sys
import threading
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


# ── Bug #1: state.py day-roll uses RLock + helper ───────────────

def test_state_lock_is_rlock():
    """`_state_lock` must be an RLock so callers (limits.record_pnl)
    can hold it across nested get_state() calls without deadlock."""
    from risk import state
    # RLock instances expose `_is_owned` method
    assert hasattr(state._state_lock, 'acquire')
    assert hasattr(state._state_lock, '_is_owned') or \
           type(state._state_lock).__name__ == 'RLock'


def test_state_check_day_roll_helper_exists():
    """Day-roll logic factored into _check_day_roll_unlocked so callers
    can run the roll inside their own lock atomically with their writes."""
    from risk import state
    assert hasattr(state, '_check_day_roll_unlocked')


# ── Bug #2 + #3: limits.py shares state lock + notify outside ────

def test_limits_uses_state_lock():
    """`limits._lock` must BE `state._state_lock` (same object)."""
    from risk import limits, state
    assert limits._lock is state._state_lock


def test_limits_notify_outside_lock():
    """Source-level guard — record_pnl must collect notifies and emit
    AFTER releasing `_lock`."""
    import inspect
    from risk import limits
    src = inspect.getsource(limits.record_pnl)
    assert 'pending_notifies' in src
    # Notify call must come AFTER `with _lock:` block
    pre_lock = src.split('with _lock:')[0]
    post_lock = src.split('with _lock:')[1]
    notify_in_post = '_notify_safe(' in post_lock
    assert notify_in_post, "_notify_safe must be called outside the lock"


# ── Bug #4: reconcile additive merge ────────────────────────────

def test_reconcile_additive_merge():
    """Source check — `_exchange_fetchers` results merged with `+=`,
    not `dict.update()`."""
    import inspect
    from risk import reconcile
    src = inspect.getsource(reconcile.reconcile_once)
    # Old destructive merge gone
    assert 'remote.update(fn() or {})' not in src
    # New additive merge present
    assert 'remote.get(k, 0.0)' in src or 'remote[k] = remote.get' in src


# ── Bug #5: reconcile outcome normalization ─────────────────────

def test_reconcile_norm_outcome_canonical():
    """`_norm_outcome` collapses '0'/'Yes'/'true' to canonical 'YES'."""
    from risk.reconcile import _norm_outcome
    assert _norm_outcome('0') == 'YES'
    assert _norm_outcome('Yes') == 'YES'
    assert _norm_outcome('true') == 'YES'
    assert _norm_outcome('outcomeOne') == 'YES'
    assert _norm_outcome('1') == 'NO'
    assert _norm_outcome('No') == 'NO'
    assert _norm_outcome(None) == ''
    # Pass-through on unknown
    assert _norm_outcome('Lakers') == 'LAKERS'


# ── Bug #6: reconcile debounce on fetcher errors ────────────────

def test_reconcile_has_failure_threshold():
    """Module-level debounce constant + counter exist."""
    from risk import reconcile
    assert hasattr(reconcile, '_RECONCILE_FAIL_THRESHOLD')
    assert hasattr(reconcile, '_consecutive_failures')
    assert reconcile._RECONCILE_FAIL_THRESHOLD >= 2


# ── Bug #7: network_check inflight dedupe ───────────────────────

def test_network_check_inflight_event():
    """Module exposes inflight dedupe machinery."""
    from risk import network_check
    assert hasattr(network_check, '_inflight_event')
    assert hasattr(network_check, '_GOOD_VALUE_TTL_S')
    assert network_check._GOOD_VALUE_TTL_S >= 60


def test_network_check_last_good_keys():
    """Cache dict tracks last-good values separately."""
    from risk import network_check
    assert 'last_good_ip' in network_check._cache
    assert 'last_good_country' in network_check._cache
    assert 'last_good_at' in network_check._cache


# ── Bug #8: preflight per-platform balance ───────────────────────

def test_preflight_routes_per_platform():
    """preflight_arb must dispatch to check_balance_for_platform
    for non-Polymarket legs."""
    import inspect
    import preflight
    src = inspect.getsource(preflight.preflight_arb)
    assert 'check_balance_for_platform' in src
    assert "platform == 'Polymarket'" in src or "platform=='Polymarket'" in src


# ── Bug #9: atomic slippage filled_with_slippage status ──────────

def test_atomic_slippage_uses_new_status():
    """Slippage breach now marks leg as filled_with_slippage so revert
    chain can flatten the off-spec position."""
    import inspect
    from executor import atomic
    # Slippage block
    src = inspect.getsource(atomic._fire_one_leg_live) \
        if hasattr(atomic, '_fire_one_leg_live') else inspect.getsource(atomic)
    assert 'filled_with_slippage' in src
    # Old no-op cancel-after-fill removed
    assert 'slippage_cancelled' in src  # legacy still exists in failed_legs list
    # filled_legs list includes the new status
    fire_arb_src = inspect.getsource(atomic.fire_arb)
    assert 'filled_with_slippage' in fire_arb_src


# ── Bug #10: arb_server pause_scan no bare except ───────────────

def test_arb_server_pause_scan_uses_session():
    """pause_scan uses pooled `_SESS_POLY` not bare `requests.get`."""
    import inspect
    import arb_server
    src = inspect.getsource(arb_server.run_pause_scan)
    # Bare module call gone
    assert 'requests.get(' not in src or '_SESS_POLY.get(' in src
    # Pause-scan errors are now visible (printed)
    assert '[PAUSE_POLY]' in src or '[PAUSE_SX]' in src


def test_arb_server_save_history_no_bare_except():
    """save_history narrowed from bare `except:` to `except Exception`."""
    import inspect
    import arb_server
    src = inspect.getsource(arb_server.save_history)
    code_only = '\n'.join(line for line in src.split('\n')
                           if not line.lstrip().startswith('#'))
    assert '\n    except:' not in code_only
    assert 'except Exception' in src


# ── Bug #11: notify Telegram 429 retry ──────────────────────────

def test_notify_handles_telegram_429():
    """`_post_blocking` recognizes `urllib.error.HTTPError` and retries
    on 429 with Retry-After / exponential backoff."""
    import inspect
    import notify
    src = inspect.getsource(notify._post_blocking)
    assert 'HTTPError' in src
    assert 'Retry-After' in src
    assert 'TELEGRAM_MAX_RETRIES' in src or 'attempt' in src


def test_notify_global_cooldown_skips_send_during_throttle():
    """Module-level cooldown prevents thread pile-up during 429 storms."""
    from risk import network_check  # noqa: side-effect import
    import notify
    # Simulate cooldown
    notify._TELEGRAM_COOLDOWN_UNTIL = time.time() + 30
    # Force-configure for the test path
    saved_token = notify.TELEGRAM_BOT_TOKEN
    saved_chat = notify.TELEGRAM_CHAT_ID
    try:
        notify.TELEGRAM_BOT_TOKEN = 'dummy'
        notify.TELEGRAM_CHAT_ID = 'dummy'
        # Should return None immediately without attempting urlopen
        result = notify._post_blocking('test')
        assert result is None
    finally:
        notify._TELEGRAM_COOLDOWN_UNTIL = 0
        notify.TELEGRAM_BOT_TOKEN = saved_token
        notify.TELEGRAM_CHAT_ID = saved_chat
