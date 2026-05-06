"""Phase 19v26 (06.05.2026) — SX API breaking change + Poly NEAR re-fetch.

Operator screenshot showed:
- Polymarket panel: NEAR=2 but visible NEAR table has 0 Polymarket rows
- SX Bet panel: NEAR=0 always, deals=0 forever — "never seen ANY SX deal"

Two distinct root causes:

1. **SX Bet API breaking change** — sometime ~May 2026, the
   /orders endpoint started rejecting `?maker=true` with HTTP 400
   (`maker must be a valid address`). The radar still used
   `marketHashes=<hash>&maker=true` everywhere, so EVERY SX
   orderbook fetch returned 400 → sx_res empty → 0 SX markets
   ever entered the NEAR pool → user never saw a single SX deal.
   The response shape also changed: `data.orders[]` → `data[]`
   directly. Both fixed; both async + sync paths handle either
   shape for forward compat.

2. **Polymarket cache decay between scan and api_deals**.
   classify_pools accepts events with `has_real` per-leg gate at
   scan time using fresh `running_clob_res`. Between scans,
   poly_clob_cache is replaced; if api_deals fires during the
   window where cache is partially repopulated, the candidate's
   tokens may be missing → `_poly_per_market` falls back to
   `implied` source → `_best_near_structure` rejects as
   `all_legs_implied`. Result: pool_poly_near=2 but visible=0.
   Fix: in near_summary, if any required token is missing from
   the cache, sync-fetch it just-in-time (capped at 8 tokens to
   prevent stalling /api/near).
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


# ── Bug #1: SX `maker=true` removed ───────────────────────────────

def test_sync_fetch_sx_no_maker_param():
    """`_fetch_sx_orders` must not include `&maker=true` in the URL."""
    import inspect
    import arb_server
    src = inspect.getsource(arb_server._fetch_sx_orders)
    # Strip comments (which legitimately quote the broken pattern)
    code_only = '\n'.join(line for line in src.split('\n')
                           if not line.lstrip().startswith('#'))
    # Old broken pattern absent in EXECUTABLE code
    assert 'maker=true' not in code_only
    # URL still present
    assert 'api.sx.bet/orders?marketHashes' in code_only


def test_async_fetch_sx_no_maker_param():
    """Same fix in async path."""
    import inspect
    from async_fetchers import fetch_sx_orders_async
    src = inspect.getsource(fetch_sx_orders_async)
    code_only = '\n'.join(line for line in src.split('\n')
                           if not line.lstrip().startswith('#'))
    assert 'maker=true' not in code_only
    assert 'api.sx.bet/orders?marketHashes' in code_only


def test_sync_fetch_sx_handles_new_response_shape():
    """Parser accepts `data: [...]` (new) AND `data: {orders: [...]}` (old)."""
    import inspect
    import arb_server
    src = inspect.getsource(arb_server._fetch_sx_orders)
    # Both shapes handled
    assert 'isinstance(raw, list)' in src
    assert 'isinstance(raw, dict)' in src


def test_async_fetch_sx_handles_new_response_shape():
    import inspect
    from async_fetchers import fetch_sx_orders_async
    src = inspect.getsource(fetch_sx_orders_async)
    assert 'isinstance(raw, list)' in src
    assert 'isinstance(raw, dict)' in src


# ── Bug #2: Polymarket NEAR re-fetch on cache miss ────────────────

def test_near_summary_refetches_missing_tokens():
    """`near_summary` poly loop sync-fetches /book if cache lacks the
    candidate's tokens — closes the pool→visible mismatch."""
    import inspect
    import arb_server
    src = inspect.getsource(arb_server.near_summary)
    # New cache-miss handling present
    assert '_missing_tids' in src
    assert '_fetch_clob(tid)' in src
    # Cap at 8 tokens
    assert 'len(_missing_tids) <= 8' in src or 'cap' in src.lower()


def test_near_summary_uses_local_clob_copy():
    """Doesn't mutate the shared poly_clob_cache."""
    import inspect
    import arb_server
    src = inspect.getsource(arb_server.near_summary)
    assert 'clob_for_pm = dict(' in src
