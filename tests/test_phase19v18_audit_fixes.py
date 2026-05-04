"""Phase 19v18 (05.05.2026) — fifth-pass audit fixes.

Three parallel agents audited eval logic, executor/risk paths, and
WS/async layer. ~30 findings; this PR fixes the verified critical /
high-severity ones with focused regression tests.

  1. arb_server.calc_fee — Kalshi-specific variance formula applied to
     ALL platforms → Polymarket / Limitless / SX fees under-reported
     4-20× → inflated `net` lets losers slip past `net > 0` filter.
  2. arb_server._resolve_lim_end_date — `tz=_tz` (module) not `_tz.utc`
     → TypeError silently swallowed → `end_date=None` for Limitless
     events with numeric ms deadline.
  3. arb_server.near_summary SX path — same `tz=_tz` typo.
  4. risk/state.py RECONCILE_TOLERANCE_USD — $0.01 trips kill switch on
     normal slippage; bumped to $1.00.
  5. poly_user_ws — server `{"error":"unauthorized"}` infinite reconnect
     → CF ban risk; now sets long-cooldown flag and stops.
  6. poly_ws backoff index — `_connect_attempts=0` after success +
     disconnect → `BACKOFF_SCHEDULE[-1]` (Python negative wrap) =
     30s instead of 1s. Clamp to [0, len-1].
  7. poly_ws._on_message — KeyError in single delta crashed whole batch;
     now isolated per-event.
  8. limitless_ws._handle_orderbook — server pushes for unsubscribed
     slugs repopulated `books` cache, defeating v14 cleanup.
  9. async_fetchers — every `asyncio.run()` left an httpx client
     dangling, leaking FDs / TCP sockets. Now `_run_and_close()`
     wrapper drains per-loop clients before loop teardown.
"""
import asyncio
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


# ── Bug #1 + #2: calc_fee per-platform ────────────────────────────

def test_calc_fee_kalshi_uses_variance():
    """Kalshi: 7c × 100 contracts × 0.5 × 0.5 = $1.75."""
    from arb_server import calc_fee
    fee = calc_fee(0.5, 100, 0.07, platform='Kalshi')
    assert abs(fee - 1.75) < 1e-6


def test_calc_fee_polymarket_uses_notional():
    """Polymarket: 2.5% × 100 × 0.5 = $1.25 (notional, NOT variance)."""
    from arb_server import calc_fee
    fee = calc_fee(0.5, 100, 0.025, platform='Polymarket')
    assert abs(fee - 1.25) < 1e-6


def test_calc_fee_polymarket_high_price():
    """Polymarket at p=0.95: 2.5% × 100 × 0.95 = $2.375. Old variance
    formula gave $0.119 (95% understated). Fix verified."""
    from arb_server import calc_fee
    fee = calc_fee(0.95, 100, 0.025, platform='Polymarket')
    assert abs(fee - 2.375) < 1e-6


def test_calc_fee_limitless_notional():
    """Limitless flat % on notional."""
    from arb_server import calc_fee
    fee = calc_fee(0.6, 100, 0.005, platform='Limitless')
    assert abs(fee - 0.30) < 1e-6   # 0.5% × 100 × 0.6


def test_calc_fee_sx_notional():
    """SX Bet flat % on notional."""
    from arb_server import calc_fee
    fee = calc_fee(0.45, 100, 0.02, platform='SX Bet')
    assert abs(fee - 0.90) < 1e-6   # 2% × 100 × 0.45


def test_calc_fee_unknown_platform_defaults_notional():
    """Unknown / cross-platform → notional (safer default)."""
    from arb_server import calc_fee
    fee = calc_fee(0.5, 100, 0.025, platform=None)
    assert abs(fee - 1.25) < 1e-6


# ── Bug #3 + #4: tz typo fixed ────────────────────────────────────

def test_resolve_lim_end_date_handles_numeric_ms():
    """`_resolve_lim_end_date` must return ISO string for numeric ms
    deadline, not None."""
    from arb_server import _resolve_lim_end_date
    # 2026-05-04T11:00:00Z = 1778230800000 ms
    ev = {'deadline': 1778230800000}
    iso = _resolve_lim_end_date(ev)
    assert iso is not None, "numeric deadline should yield ISO string"
    assert iso.startswith('2026-')


def test_resolve_lim_end_date_handles_seconds():
    """Numeric deadline in seconds (not ms) also handled."""
    from arb_server import _resolve_lim_end_date
    ev = {'deadline': 1778230800}
    iso = _resolve_lim_end_date(ev)
    assert iso is not None
    assert iso.startswith('2026-')


# ── Bug #5: reconcile tolerance bumped ─────────────────────────────

def test_reconcile_tolerance_above_normal_slippage():
    """Tolerance must comfortably exceed normal slippage on a $50 leg."""
    from risk import state
    assert state.RECONCILE_TOLERANCE_USD >= 0.50


# ── Bug #6: poly_user_ws auth backoff ─────────────────────────────

def test_poly_user_ws_auth_error_handler_present():
    """Source-level: `_handle_event` detects auth-class errors and
    stops the supervisor instead of infinite-reconnecting."""
    import inspect
    import poly_user_ws
    src = inspect.getsource(poly_user_ws.PolyUserWS._handle_event)
    assert "ev.get('error')" in src or 'ev.get("error")' in src
    assert 'unauthor' in src.lower() or '401' in src or '403' in src
    assert '_auth_failed_at' in src or 'self.stop()' in src


# ── Bug #7: poly_ws backoff clamp ─────────────────────────────────

def test_poly_ws_backoff_idx_clamped():
    """Source guard: backoff index uses `max(0, ...)` to prevent the
    -1 index that returns the LAST element of BACKOFF_SCHEDULE."""
    import inspect
    import poly_ws
    src = inspect.getsource(poly_ws.PolyMarketWS._run_forever)
    assert 'BACKOFF_SCHEDULE' in src
    assert 'max(0,' in src or 'max(0, ' in src


# ── Bug #8: poly_ws price_change KeyError isolated ────────────────

def test_poly_ws_event_dispatch_isolates_errors():
    """A single bad event must not crash the whole batch."""
    import inspect
    import poly_ws
    src = inspect.getsource(poly_ws.PolyMarketWS._on_message)
    # Look for try/except around _handle_event call
    assert 'self._handle_event(ev)' in src
    # New defensive code path
    assert 'try:' in src and '_handle_event' in src


def test_poly_ws_handles_malformed_event_dict():
    """End-to-end: a non-dict event in the list must not raise."""
    import poly_ws
    cli = poly_ws.PolyMarketWS()
    # Simulate batch with one good event, one None, one string
    import json
    msg = json.dumps([
        {'event_type': 'book', 'asset_id': 'tA', 'asks': []},
        None,
        'malformed',
        {'event_type': 'book', 'asset_id': 'tB', 'asks': []},
    ])
    # _on_message needs (ws, msg). ws is unused. Should not raise.
    cli._on_message(None, msg)


# ── Bug #9: limitless_ws drops pushes for unsubscribed slugs ──────

def test_limitless_ws_filters_pushes_by_desired():
    """`_handle_orderbook` must short-circuit if slug not in `_desired`."""
    import inspect
    import limitless_ws
    src = inspect.getsource(limitless_ws.LimitlessWS._handle_orderbook)
    # Filter must exist
    assert 'self._desired' in src
    assert 'slug not in self._desired' in src \
        or 'if slug in self._desired' in src.replace(' not in ', ' in ')


# ── Bug #10: async_fetchers _run_and_close wrapper ─────────────────

def test_async_fetchers_run_and_close_wrapper_exists():
    """`_run_and_close()` exists and is used by every sync wrapper."""
    import async_fetchers
    assert callable(getattr(async_fetchers, '_run_and_close', None))
    assert callable(getattr(async_fetchers, 'close_clients_for_loop', None))


def test_async_fetchers_wrappers_use_run_and_close():
    """Source-level guard — every sync wrapper calls `_run_and_close`,
    not bare `asyncio.run` (except inside `_run_and_close` itself)."""
    import inspect
    import async_fetchers
    fn_names = [
        'run_fetch_clob_batch',
        'run_fetch_poly_markets_batch',
        'run_fetch_limitless_pages',
        'run_fetch_poly_events_pages',
        'run_fetch_sx_markets',
        'run_fetch_sx_orders_batch',
        'run_async_batch',
    ]
    for name in fn_names:
        fn = getattr(async_fetchers, name, None)
        if fn is None:
            continue
        src = inspect.getsource(fn)
        assert '_run_and_close' in src, \
            f"{name} should use _run_and_close (got: bare asyncio.run)"


def test_close_clients_for_loop_drains_only_target_loop():
    """`close_clients_for_loop(id)` only closes clients keyed by that id."""
    import async_fetchers
    # Simulate 2 fake entries
    async_fetchers._ASYNC_CLIENTS.clear()
    class _FakeClient:
        def __init__(self):
            self.closed = False
        async def aclose(self):
            self.closed = True
    a = _FakeClient(); b = _FakeClient()
    async_fetchers._ASYNC_CLIENTS[('poly', 1111)] = a
    async_fetchers._ASYNC_CLIENTS[('poly', 2222)] = b
    asyncio.run(async_fetchers.close_clients_for_loop(1111))
    assert a.closed is True
    assert b.closed is False
    assert ('poly', 1111) not in async_fetchers._ASYNC_CLIENTS
    assert ('poly', 2222) in async_fetchers._ASYNC_CLIENTS
    async_fetchers._ASYNC_CLIENTS.clear()
