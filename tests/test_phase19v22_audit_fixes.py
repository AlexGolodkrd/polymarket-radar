"""Phase 19v22 (05.05.2026) — ninth-pass audit fixes (post-merge sanity).

After merging all 8 prior PRs (#85-#93) to main and deploying to VPS,
a final audit on the merged tree found 3 verified bugs:

1. arb_server.filter_kalshi — missing past-resolve adaptive grace gate
   (parity gap with Polymarket / Limitless / SX)
2. polymarket_approve.py — NEGRISK_ADAPTER spender never granted
   pUSD allowance + CTF setApprovalForAll
3. executor/atomic.fire_arb — release_reservations() never called
   (dead code from v16) → reservations stuck on 15s TTL
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


def test_filter_kalshi_has_past_resolve_gate():
    import inspect
    import arb_server
    src = inspect.getsource(arb_server.filter_kalshi)
    assert 'kalshi_skip_past_resolve' in src
    assert 'compute_adaptive_grace_minutes' in src


def test_filter_kalshi_diag_initializes_past_resolve_key():
    from arb_server import filter_kalshi
    diag = {}
    filter_kalshi([], diag=diag)
    assert 'kalshi_skip_past_resolve' in diag
    assert diag['kalshi_skip_past_resolve'] == 0


def test_filter_kalshi_drops_past_resolve_event():
    """End-to-end: an event whose `close_time` is 2h ago must be
    dropped (parity with Limitless 8.7c phantom case)."""
    from datetime import datetime, timezone, timedelta
    from arb_server import filter_kalshi
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    open_t = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    ev = {
        'event_ticker': 'KALSHI-TEST',
        'title': 'BTC above 100k 11AM ET',
        'markets': [
            {'ticker': 'M1', 'title': 'BTC above 100k',
             'close_time': past, 'open_time': open_t},
            {'ticker': 'M2', 'title': 'BTC below 100k',
             'close_time': past, 'open_time': open_t},
        ],
    }
    diag = {}
    cands, _tickers = filter_kalshi([ev], diag=diag)
    # Past-resolve gate either drops at past_resolve OR earlier 10-day
    # window check — both prevent the phantom from entering candidates.
    assert cands == []


def test_polymarket_approve_includes_negrisk_adapter():
    """`_approve_exchanges` must iterate over THREE spenders, including
    NEGRISK_ADAPTER (separate from EXCHANGE_NEGRISK)."""
    import inspect
    import polymarket_approve
    src = inspect.getsource(polymarket_approve._approve_exchanges)
    assert 'NEGRISK_ADAPTER' in src
    assert "'negRisk_adapter'" in src or '"negRisk_adapter"' in src


def test_atomic_release_reservations_wired():
    """fire_arb must call release_reservations(assigned) before returning."""
    import inspect
    from executor import atomic
    src = inspect.getsource(atomic.fire_arb)
    assert 'release_reservations' in src
    # Should pass the `assigned` list specifically
    assert 'release_reservations(assigned)' in src
