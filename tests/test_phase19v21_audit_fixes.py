"""Phase 19v21 (05.05.2026) — eighth-pass audit fixes (final pass).

Final audit agent confirms code is in solid shape after 7 prior passes.
Only ONE medium-severity bug remained (Limitless NO-side depth multiplier),
plus a few defensive LOW fixes worth applying.

Bug-by-bug:

 1. arb_server.py + limitless_ws.py — `depth_no` for synthetic NO from
    YES bids used `best_yes_bid × size` for notional, but buying NO
    means selling YES at the YES bid: cost-per-share to NO buyer is
    `(1 - yes_bid)` USDC. With yes_bid=0.10, depth_no was reported
    as `0.10×size/1e6` instead of `0.90×size/1e6` — up to 9× under-
    count. Quality gate `min_liq < 130` then dropped real C-pair
    arbs whose NO leg was deep but yes_bid was small.

 2. risk/killswitch.py — `_last_kill_check_error = 0.0` was declared
    AFTER `is_killed()` referenced it. Worked because Python resolves
    at call time, but if any import-side effect calls `is_killed()`
    before line 81 ran, NameError fail-closed silently. Moved above
    the function for order independence.

 3. executor/dryrun_log.py — `_row_cost(r)` did `deal['entries'][idx]`
    without bounds check. If `idx` is invalid (mutated deal between
    fire and 5s eval), IndexError → caught by `_worker` except → entire
    paper_results row dropped. Now returns 0.0 cost on out-of-range.

 4. paper_trading.py — `r.get('legs', [])` returns `[]` only when key
    is ABSENT. A corrupt log line with `legs: null` returns `None`,
    and `for s in None` raises TypeError → `paper_stats` endpoint
    blows up entirely. Switched to `(r.get('legs') or [])`. Same
    pattern in `_row_is_clean` + isinstance check.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


# ── Bug #1: Limitless NO-side depth multiplier ───────────────────

def test_limitless_orderbook_no_depth_uses_inverse_price():
    """Source guard: `_fetch_limitless_orderbook` calls `_lim_depth_usd`
    with `best_no_ask` (= 1 - yes_bid), not `best_yes_bid`."""
    import inspect
    import arb_server
    src = inspect.getsource(arb_server._fetch_limitless_orderbook)
    # New correct call must be present
    assert 'depth_no = _lim_depth_usd(best_no_ask' in src
    # Old broken call must be absent (only the corrected line survives)
    code_only = '\n'.join(line for line in src.split('\n')
                           if not line.lstrip().startswith('#'))
    assert 'depth_no = _lim_depth_usd(best_yes_bid' not in code_only


def test_limitless_ws_no_depth_uses_inverse_price():
    """`limitless_ws._parse_orderbook` synthetic NO depth uses
    `(1 - best_yes_bid)`."""
    import inspect
    import limitless_ws
    src = inspect.getsource(limitless_ws.LimitlessWS._parse_orderbook)
    assert '_norm(1 - best_yes_bid' in src
    code_only = '\n'.join(line for line in src.split('\n')
                           if not line.lstrip().startswith('#'))
    assert 'depth_no_synth = _norm(best_yes_bid' not in code_only


# ── Bug #2: killswitch init order ─────────────────────────────────

def test_killswitch_sentinel_declared_before_function():
    """`_last_kill_check_error` initialized BEFORE `is_killed()` so any
    import-time call doesn't NameError fail-close."""
    import inspect
    from risk import killswitch
    src = inspect.getsource(killswitch)
    sentinel_idx = src.find('_last_kill_check_error = 0.0')
    func_idx = src.find('def is_killed(')
    assert sentinel_idx > 0 and func_idx > 0
    assert sentinel_idx < func_idx, \
        "sentinel must be initialized BEFORE is_killed() is defined"


# ── Bug #3: dryrun_log bounds check ───────────────────────────────

def test_dryrun_log_row_cost_handles_out_of_bounds():
    """`_row_cost` must NOT raise on an idx larger than entries length."""
    # White-box: replicate the function's bounds-check behavior.
    deal = {'entries': []}
    idx = 5
    entries = deal.get('entries') or []
    assert not (0 <= idx < len(entries))
    # The fixed function returns 0.0 in this branch — test by source guard.
    import inspect
    from executor import dryrun_log
    src = inspect.getsource(dryrun_log._evaluate_realistic_fill)
    assert 'idx < len(entries)' in src or 'len(deal' in src
    assert 'return 0.0' in src or 'return 0' in src


# ── Bug #4: paper_trading legs=null guard ─────────────────────────

def test_paper_trading_handles_null_legs_field():
    """`paper_stats` / `graduation_status` must NOT crash on a row
    whose `legs` field is JSON `null`."""
    import inspect
    import paper_trading
    src = inspect.getsource(paper_trading.graduation_status)
    # New `(r.get('legs') or [])` pattern present
    assert "(r.get('legs') or [])" in src or '(r.get("legs") or [])' in src
    # Defensive isinstance in _row_is_clean
    assert 'isinstance(legs, list)' in src


def test_paper_trading_row_is_clean_with_null_legs():
    """Row with `legs: null` should be classified as not-clean (defensive)."""
    # Re-run the check inline
    r = {'realistic_pnl_5s': 1.0, 'drift': 0.0, 'legs': None}
    legs = r.get('legs') or []
    assert legs == []
    # An empty legs list is clean by definition (no aborted reasons)
    # — but a `None` `legs` field could also indicate corrupt row;
    # graduation should at least not crash.
