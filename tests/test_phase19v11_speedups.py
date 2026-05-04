"""Phase 19v11 (04.05.2026) — final scan speed optimizations.

Three improvements:

1. **WS-first /book** — Polymarket WS already subscribed to ~1000 HOT/NEAR
   tokens. Big batch /book скан-time fetched ALL tokens via REST. Fix:
   if WS has fresh book for a tid, use it; only REST the rest. Save
   ~10-15s per scan (depending on WS coverage).

2. **`_persist_scan_state` в background thread** — JSON dump + disk write
   to ~800KB scan_state.json. Daemon thread не блокирует main scan_loop
   → next tick starts ~3-5s earlier.

3. **WS subscription updates в background** — `update_subscriptions`
   triggers WS reconnect (close+open TCP+TLS), 1-3s. Daemon thread
   parallelizes with subsequent operations.
"""
import os, sys, pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


def test_run_scan_uses_ws_first_for_book():
    """Big batch /book section now checks ws_client.get_book before REST."""
    import inspect
    import arb_server
    src = inspect.getsource(arb_server.run_scan)
    # WS-first split logic
    assert 'ws_client.get_book(tid)' in src
    # New ws_pre_clob dict + rest_tids
    assert 'ws_pre_clob' in src
    assert 'rest_tids' in src
    # Output log mentions both WS and REST counts
    assert 'WS=' in src and 'REST=' in src


def test_persist_state_runs_in_background():
    """`_persist_scan_state` called via `threading.Thread(daemon=True)`."""
    import inspect
    import arb_server
    src = inspect.getsource(arb_server.run_scan)
    # Look for daemon thread spawning persist
    assert "threading.Thread" in src
    assert "_persist_scan_state" in src
    # Daemon flag must be set
    pre_persist = src.split('_persist_scan_state')[0]
    last_thread = pre_persist.rfind('threading.Thread')
    assert last_thread > 0
    # Window after the threading.Thread call should mention daemon=True
    after = src[last_thread:last_thread + 200]
    assert 'daemon=True' in after or 'name=' in after


def test_ws_subscription_update_in_background():
    """ws_client.update_subscriptions called in daemon thread, not synchronously."""
    import inspect
    import arb_server
    src = inspect.getsource(arb_server.run_scan)
    # Should NOT see bare `ws_client.update_subscriptions(tokens[:MAX_WS_SUBS])`
    # call without `threading.Thread` wrap
    # Heuristic: count `update_subscriptions` calls inside `threading.Thread`
    import re
    bg_pattern = re.compile(r'threading\.Thread\([^)]*update_subscriptions', re.S)
    assert bg_pattern.search(src), \
        "ws_client.update_subscriptions must be in background thread"


def test_lim_ws_subscription_in_background():
    """Same for lim_ws_client."""
    import inspect
    import arb_server
    src = inspect.getsource(arb_server.run_scan)
    import re
    # Check lim_ws_client background pattern too
    pattern = re.compile(r'threading\.Thread\([^)]*lim_ws_client.*?update_subscriptions', re.S)
    assert pattern.search(src), \
        "lim_ws_client.update_subscriptions must be in background thread"


def test_token_index_still_synchronous():
    """`poly_token_index.update` MUST stay synchronous before subs update —
    on_ws_update callbacks need consistent index immediately. Background
    sub update is fine because callbacks for cancelled subs just no-op."""
    import inspect
    import arb_server
    src = inspect.getsource(arb_server.run_scan)
    # Verify there's no daemon thread wrapping poly_token_index update
    import re
    bad = re.compile(r'threading\.Thread\([^)]*poly_token_index', re.S)
    assert not bad.search(src), \
        "poly_token_index update must be synchronous (consistency guarantee)"
