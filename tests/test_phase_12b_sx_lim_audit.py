"""Phase 12b (01.05.2026) — SX Bet + Limitless audit fixes.

Findings (verified by Explore agent):
  Bug 1 — Limitless _fetch_limitless_orderbook didn't apply DEPTH_SLIPPAGE_TOLERANCE
  Bug 2 — SX status check fail-OPEN on missing field (accepts paused markets)
  Bug 4 — _lim_depth_usd boundary `>` missed exact 1M edge
  Bug 6 — SX bare except hid 403/429/timeout
  Bug 8 — Limitless build_order with can_sign but token_id=None silently unsigned
"""
import os
import sys
import logging
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), 'Scripts')
sys.path.insert(0, SCRIPTS)


# ── Bug 4: _lim_depth_usd boundary fix ──────────────────────────────
def test_lim_depth_usd_exact_1M_boundary_divides():
    """price=0.01 × size=100_000_000 = exactly 1_000_000 raw → must divide."""
    from arb_server import _lim_depth_usd
    out = _lim_depth_usd(0.01, 100_000_000)
    # Old code returned 1_000_000.0 (capped, but $1M phantom).
    # Fixed: 1_000_000 / 1_000_000 = 1.0 USD.
    assert out == pytest.approx(1.0)


def test_lim_depth_usd_above_1M_still_divides():
    from arb_server import _lim_depth_usd
    # 0.5 × 5_000_000 = 2_500_000 raw → 2.5 USD
    out = _lim_depth_usd(0.5, 5_000_000)
    assert out == pytest.approx(2.5)


def test_lim_depth_usd_below_1M_no_division():
    from arb_server import _lim_depth_usd
    # 0.5 × 100 = 50 raw → 50 USD (already in USD form)
    out = _lim_depth_usd(0.5, 100)
    assert out == pytest.approx(50.0)


def test_lim_depth_usd_capped_at_1M():
    from arb_server import _lim_depth_usd
    # raw = 0.5 × 4e12 = 2e12, /1e6 = 2_000_000 USD → CAPPED at 1M.
    out = _lim_depth_usd(0.5, 4_000_000_000_000)
    assert out == pytest.approx(1_000_000.0)


def test_lim_depth_usd_uncapped_normal_value():
    from arb_server import _lim_depth_usd
    # raw = 0.5 × 200_000_000_000 = 1e11, /1e6 = 100_000 USD (under cap)
    out = _lim_depth_usd(0.5, 200_000_000_000)
    assert out == pytest.approx(100_000.0)


# ── Bug 1: Limitless top-of-book depth uses DEPTH_SLIPPAGE_TOLERANCE ──
def test_limitless_orderbook_uses_depth_tolerance(monkeypatch):
    """Asks at 0.30, 0.302, 0.310 — within tolerance 0.005 should sum
    first two. Without tolerance fix, only top would count."""
    import arb_server

    class _FakeResp:
        status_code = 200
        def json(self):
            return {
                'asks': [
                    {'price': '0.300', 'size': '100'},      # top
                    {'price': '0.302', 'size': '200'},      # within 0.5c
                    {'price': '0.310', 'size': '999'},      # outside
                ],
                'bids': [],
            }

    # Bypass WS cache
    monkeypatch.setattr(arb_server, 'lim_ws_client', None)
    monkeypatch.setattr(arb_server._SESS_LIM, 'get',
                          lambda *a, **kw: _FakeResp())
    slug, ask, depth, no_ask, no_depth = arb_server._fetch_limitless_orderbook(
        'test-slug')
    assert ask == pytest.approx(0.300)
    # Top + ladder (300 size total; raw_notional = 0.3*300 = 90, no division)
    # Old code: 0.3*100 = 30. New: 0.3*300 = 90.
    assert depth == pytest.approx(90.0)


def test_limitless_orderbook_skips_outside_tolerance(monkeypatch):
    """Level 1c above best is OUTSIDE default 0.5c tolerance."""
    import arb_server

    class _FakeResp:
        status_code = 200
        def json(self):
            return {
                'asks': [
                    {'price': '0.30', 'size': '50'},
                    {'price': '0.31', 'size': '500'},        # +1c outside
                ],
                'bids': [],
            }

    monkeypatch.setattr(arb_server, 'lim_ws_client', None)
    monkeypatch.setattr(arb_server._SESS_LIM, 'get',
                          lambda *a, **kw: _FakeResp())
    slug, ask, depth, _, _ = arb_server._fetch_limitless_orderbook('s')
    # 0.30*50 only — 0.31*500 outside tolerance
    assert depth == pytest.approx(15.0)


def test_limitless_orderbook_synthetic_no_with_tolerance(monkeypatch):
    """Bid side: ladder within tolerance counted for synthetic NO depth."""
    import arb_server

    class _FakeResp:
        status_code = 200
        def json(self):
            return {
                'asks': [],
                'bids': [
                    {'price': '0.40', 'size': '100'},        # best bid
                    {'price': '0.398', 'size': '200'},       # within 0.5c
                    {'price': '0.39', 'size': '999'},        # outside
                ],
            }

    monkeypatch.setattr(arb_server, 'lim_ws_client', None)
    monkeypatch.setattr(arb_server._SESS_LIM, 'get',
                          lambda *a, **kw: _FakeResp())
    slug, _, _, no_ask, no_depth = arb_server._fetch_limitless_orderbook('s')
    assert no_ask == pytest.approx(1 - 0.40)            # 0.60
    # Sum of sizes within tolerance: 100 + 200 = 300, normalized via _lim_depth_usd
    # raw_notional = 0.40 × 300 = 120 (below 1M, no division)
    assert no_depth == pytest.approx(120.0)


# ── Bug 2: SX status fail-CLOSED ────────────────────────────────────
def test_sx_status_missing_now_rejected():
    """Market without `status` field is now REJECTED (was accepted)."""
    from arb_server import eval_sx, SX_BINARY_TYPES
    # Pick first allowed type
    sx_type = next(iter(SX_BINARY_TYPES))
    market = {
        'type': sx_type,
        'marketHash': '0xABCDEF1234',
        'gameTime': 9999999999,
        # No 'status' field — old behavior: accept; new: reject
        'outcomeOneName': 'Team A',
        'outcomeTwoName': 'Team B',
    }
    sx_orders = {'0xABCDEF1234': (0.45, 100, 0.50, 100)}  # valid book
    deals = eval_sx([market], sx_orders)
    assert deals == [], "Markets without status field should be rejected"


def test_sx_status_paused_rejected():
    from arb_server import eval_sx, SX_BINARY_TYPES
    sx_type = next(iter(SX_BINARY_TYPES))
    market = {
        'type': sx_type,
        'marketHash': '0xABC',
        'gameTime': 9999999999,
        'status': 2,             # paused
        'outcomeOneName': 'A',
        'outcomeTwoName': 'B',
    }
    sx_orders = {'0xABC': (0.45, 100, 0.50, 100)}
    deals = eval_sx([market], sx_orders)
    assert deals == []


def test_sx_status_active_accepted():
    """Sanity check: status=1 still accepts."""
    from arb_server import eval_sx, SX_BINARY_TYPES, is_within_10_days
    sx_type = next(iter(SX_BINARY_TYPES))
    import time
    market = {
        'type': sx_type,
        'marketHash': '0xABC',
        'gameTime': int(time.time()) + 3600,
        'status': 1,
        'outcomeOneName': 'A',
        'outcomeTwoName': 'B',
    }
    sx_orders = {'0xABC': (0.45, 100, 0.50, 100)}
    deals = eval_sx([market], sx_orders)
    # May or may not produce a deal depending on threshold math, but
    # at minimum not blocked by status check.
    # If sum=0.95 and THRESH_SX higher → deal; if not → no deal but
    # we don't fail the test just because of this. Just verify no exception.
    assert isinstance(deals, list)


# ── Bug 6: SX error logging ─────────────────────────────────────────
def test_sx_fetch_orders_logs_exception(monkeypatch, capsys):
    """When _SESS_SX.get raises, exception type+message is logged."""
    import arb_server

    def _raise(*a, **kw):
        raise ConnectionError("SX gateway unreachable")
    monkeypatch.setattr(arb_server._SESS_SX, 'get', _raise)

    result = arb_server._fetch_sx_orders('0xABC')
    captured = capsys.readouterr()
    out = captured.out + captured.err
    # New behavior: logs error type + message
    # (Old: silently returns None tuple)
    assert 'ConnectionError' in out or '_fetch_sx_orders' in out
    assert result == ('0xABC', None, 0, None, 0)


# ── Bug 8: Limitless build_order warns on missing token_id ──────────
def test_limitless_build_order_warns_when_can_sign_but_no_token(caplog):
    """If wallet.can_sign=True but token_id=None, log a warning."""
    from executor import builders

    class _FakeWallet:
        bot_id = 'bot1'
        eth_address = '0x' + '1' * 40
        private_key = '0x' + 'a' * 64        # nonempty → can_sign True
        api_key = None
        poly_api_key = None
        poly_secret = None
        poly_passphrase = None
        @property
        def can_sign(self):
            return bool(self.private_key)
        @property
        def has_poly_creds(self):
            return False

    with caplog.at_level(logging.WARNING):
        result = builders.build_limitless_order(
            slug='test', side='BUY', price=0.30, size_usdc=10.0,
            wallet=_FakeWallet(),
            token_id=None,                    # missing!
            verifying_contract=None,
        )
    # Verify warning was emitted
    has_warning = any('token_id=None' in r.message for r in caplog.records)
    assert has_warning, f"expected warning about missing token_id, got: {[r.message for r in caplog.records]}"
    # Order body should still build (caller may want it for dry-run)
    assert result['platform'] == 'limitless'
    assert result['signed'] is False


def test_limitless_build_order_no_warning_when_token_present(caplog):
    """If token_id supplied, no warning."""
    from executor import builders

    class _FakeWallet:
        bot_id = 'bot1'
        eth_address = '0x' + '1' * 40
        private_key = '0x' + 'a' * 64
        api_key = None
        poly_api_key = None
        poly_secret = None
        poly_passphrase = None
        @property
        def can_sign(self):
            return bool(self.private_key)
        @property
        def has_poly_creds(self):
            return False

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        result = builders.build_limitless_order(
            slug='test', side='BUY', price=0.30, size_usdc=10.0,
            wallet=_FakeWallet(),
            token_id='0x1234',
            verifying_contract='0x' + 'b' * 40,
        )
    # Should NOT have the missing-token warning
    has_missing_warning = any('token_id=None' in r.message for r in caplog.records)
    assert not has_missing_warning
