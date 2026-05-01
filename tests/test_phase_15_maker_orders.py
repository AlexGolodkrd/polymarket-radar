"""Phase 15 (01.05.2026) — maker order builder + maker mode selector + supervisor.

Tests cover:
  15a — build_poly_maker_order (price 1 tick inside spread, fallback when
        spread < 1 tick)
  15b — maker_supervise (fill / timeout / adverse selection)
  15c — select_fire_mode (sum_cents → mode)
"""
import os
import sys
import threading
import time
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), 'Scripts')
sys.path.insert(0, SCRIPTS)


# ── 15a: build_poly_maker_order ─────────────────────────────────────
def _make_wallet():
    from executor.builders import WalletStub
    return WalletStub(bot_id='bot1', eth_address='0x' + '1' * 40)


def test_maker_order_buy_at_bid_plus_tick():
    """BUY maker: price = best_bid + tick_size."""
    from executor.builders import build_poly_maker_order
    res = build_poly_maker_order(
        token_id='T', side='BUY',
        best_ask=0.40, best_bid=0.35,
        size_usdc=10.0, wallet=_make_wallet(),
        tick_size=0.01,
    )
    assert res['is_maker'] is True
    assert res['maker_price'] == pytest.approx(0.36)
    assert res['will_revert_to_taker'] is False


def test_maker_order_sell_at_ask_minus_tick():
    """SELL maker: price = best_ask - tick_size."""
    from executor.builders import build_poly_maker_order
    res = build_poly_maker_order(
        token_id='T', side='SELL',
        best_ask=0.40, best_bid=0.35,
        size_usdc=10.0, wallet=_make_wallet(),
        tick_size=0.01,
    )
    assert res['is_maker'] is True
    assert res['maker_price'] == pytest.approx(0.39)


def test_maker_order_falls_back_when_spread_too_tight():
    """spread < tick_size → no room for maker, return taker fallback."""
    from executor.builders import build_poly_maker_order
    res = build_poly_maker_order(
        token_id='T', side='BUY',
        best_ask=0.40, best_bid=0.395,    # 0.5c spread, tick=1c → too tight
        size_usdc=10.0, wallet=_make_wallet(),
        tick_size=0.01,
    )
    assert res['is_maker'] is False
    assert res['will_revert_to_taker'] is True
    assert 'spread_too_tight' in res['maker_failure_reason']


def test_maker_order_falls_back_when_no_spread_data():
    """best_ask=None → invalid spread, taker fallback."""
    from executor.builders import build_poly_maker_order
    res = build_poly_maker_order(
        token_id='T', side='BUY',
        best_ask=None, best_bid=None,
        size_usdc=10.0, wallet=_make_wallet(),
    )
    assert res['will_revert_to_taker'] is True
    assert res['maker_failure_reason'] == 'invalid_spread'


def test_maker_order_returns_compatible_shape():
    """Maker order has same standard keys as taker order."""
    from executor.builders import build_poly_maker_order
    res = build_poly_maker_order(
        token_id='T', side='BUY',
        best_ask=0.40, best_bid=0.30,
        size_usdc=10.0, wallet=_make_wallet(),
    )
    for k in ('platform', 'body', 'order', 'sign_payload', 'would_post_url',
              'expected_price', 'expected_size_usdc', 'signed', 'eip712'):
        assert k in res, f"missing key {k}"
    assert res['platform'] == 'polymarket'


# ── 15c: select_fire_mode ───────────────────────────────────────────
def test_select_fire_mode_disabled_returns_taker(monkeypatch):
    from executor import atomic
    monkeypatch.setattr(atomic, 'MAKER_MODE_ENABLED', False)
    assert atomic.select_fire_mode({'sum_cents': 90}) == 'taker'


def test_select_fire_mode_wide_arb_picks_maker(monkeypatch):
    from executor import atomic
    monkeypatch.setattr(atomic, 'MAKER_MODE_ENABLED', True)
    assert atomic.select_fire_mode({'sum_cents': 88}) == 'maker'
    assert atomic.select_fire_mode({'sum_cents': 91}) == 'maker'


def test_select_fire_mode_medium_arb_picks_hybrid(monkeypatch):
    from executor import atomic
    monkeypatch.setattr(atomic, 'MAKER_MODE_ENABLED', True)
    assert atomic.select_fire_mode({'sum_cents': 94}) == 'maker_then_taker'
    assert atomic.select_fire_mode({'sum_cents': 95}) == 'maker_then_taker'


def test_select_fire_mode_tight_arb_picks_taker(monkeypatch):
    from executor import atomic
    monkeypatch.setattr(atomic, 'MAKER_MODE_ENABLED', True)
    assert atomic.select_fire_mode({'sum_cents': 96}) == 'taker'
    assert atomic.select_fire_mode({'sum_cents': 96.5}) == 'taker'


# ── 15b: maker_supervise ────────────────────────────────────────────
def test_maker_supervise_fill_returns_filled():
    """When the registration's event is set within deadline → 'filled'."""
    from executor import atomic

    class _Reg:
        event = threading.Event()

    reg = _Reg()
    # Set event in 0.1s
    threading.Timer(0.1, reg.event.set).start()

    result = atomic.maker_supervise(reg, expected_price=0.30, deadline_s=2.0)
    assert result == 'filled'


def test_maker_supervise_timeout_returns_timeout():
    """No fill within deadline → 'timeout'."""
    from executor import atomic

    class _Reg:
        event = threading.Event()
    reg = _Reg()

    result = atomic.maker_supervise(reg, expected_price=0.30, deadline_s=1.0)
    assert result == 'timeout'


def test_maker_supervise_adverse_selection_cancels():
    """If other_source_check returns price drifted > tolerance → cancel."""
    from executor import atomic

    class _Reg:
        event = threading.Event()
    reg = _Reg()

    # Other source reports price moved 2c away (>1c tolerance)
    def _check():
        return 0.32           # we expected 0.30, drift 0.02 > 0.01 default
    result = atomic.maker_supervise(
        reg, expected_price=0.30,
        other_source_check=_check,
        deadline_s=2.0,
    )
    assert result == 'adverse_selection'


def test_maker_supervise_no_drift_no_cancel(monkeypatch):
    """Other source close to expected → don't cancel, time out normally."""
    from executor import atomic

    class _Reg:
        event = threading.Event()
    reg = _Reg()

    def _check():
        return 0.301      # small drift, within tolerance
    result = atomic.maker_supervise(
        reg, expected_price=0.30,
        other_source_check=_check,
        deadline_s=1.0,
    )
    assert result == 'timeout'


def test_maker_supervise_handles_check_exceptions():
    """If other_source_check raises, supervise continues — defensive path."""
    from executor import atomic

    class _Reg:
        event = threading.Event()
    reg = _Reg()

    def _broken():
        raise RuntimeError('check broken')
    threading.Timer(0.1, reg.event.set).start()
    # Should not crash — error caught, eventually fill arrives
    result = atomic.maker_supervise(
        reg, expected_price=0.30,
        other_source_check=_broken,
        deadline_s=2.0,
    )
    assert result == 'filled'
