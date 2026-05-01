"""Phase 10 #51 (30.04.2026) — top-of-book depth tests.

User report: liquidity reported per leg was SUM across all orderbook
levels, inflating min_liq 5-10x. Stake sized against that inflated number
would partial-fill (top-of-book exhausted) → walk the book → average
price exceeds SLIPPAGE_TOLERANCE → arb broken.

Fix: count USD notional only at exact best ask price (or within tiny
tolerance for floating-point fuzz). Limitless was already correct via
_lim_depth_usd; this PR brings Polymarket / Kalshi / SX Bet / poly_ws
to the same standard.
"""
import os
import sys
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), 'Scripts')
sys.path.insert(0, SCRIPTS)


def test_top_of_book_dict_shape_polymarket():
    """Polymarket /book?token_id returns {asks:[{price,size},...]}.
    Best ask 0.30 has $50 sitting; next level 0.31 has $200; old code
    summed 0.30*50 + 0.31*200 = $77 depth. New code returns $15
    (USD notional at exact best = 0.30 × 50 contracts = $15).
    """
    from arb_server import _top_of_book_depth_usd
    asks = [
        {'price': '0.31', 'size': '200'},
        {'price': '0.30', 'size': '50'},
        {'price': '0.33', 'size': '500'},
    ]
    best, depth = _top_of_book_depth_usd(asks)
    assert best == pytest.approx(0.30)
    assert depth == pytest.approx(0.30 * 50)        # $15, not $15+$62+$165


def test_top_of_book_multiple_levels_at_best_sum():
    """If two market makers both sit at 0.30 with $50 and $30, depth
    counts BOTH (same best ask price)."""
    from arb_server import _top_of_book_depth_usd
    asks = [
        {'price': 0.30, 'size': 50},
        {'price': 0.30, 'size': 30},
        {'price': 0.31, 'size': 999},
    ]
    best, depth = _top_of_book_depth_usd(asks)
    assert best == pytest.approx(0.30)
    assert depth == pytest.approx(0.30 * 80)        # $24


def test_top_of_book_kalshi_tuple_shape_size_in_usd():
    """Kalshi orderbook_fp.yes_dollars: [[price, size_usd], ...]
    Size is already dollar notional → don't multiply by price."""
    from arb_server import _top_of_book_depth_usd
    levels = [
        [0.30, 1000.0],     # best
        [0.31, 5000.0],     # walk
        [0.32, 10000.0],    # walk further
    ]
    best, depth = _top_of_book_depth_usd(
        levels, tuple_idx_price=0, tuple_idx_size=1, size_is_usd=True)
    assert best == pytest.approx(0.30)
    assert depth == pytest.approx(1000.0)            # NOT 1000+5000+10000


def test_top_of_book_empty_returns_none_zero():
    from arb_server import _top_of_book_depth_usd
    assert _top_of_book_depth_usd([]) == (None, 0.0)
    assert _top_of_book_depth_usd(None) == (None, 0.0)


def test_top_of_book_skips_malformed_levels():
    """Bad price/size entries don't crash — they're just skipped."""
    from arb_server import _top_of_book_depth_usd
    asks = [
        {'price': 'not-a-number', 'size': 100},
        {'price': 0.30, 'size': 50},
        {'price': 0.30, 'size': 'bad'},
        {'price': 0, 'size': 100},                  # zero price excluded
        {'price': 0.30, 'size': 0},                 # zero size excluded
    ]
    best, depth = _top_of_book_depth_usd(asks)
    assert best == pytest.approx(0.30)
    assert depth == pytest.approx(0.30 * 50)


def test_top_of_book_unsorted_input_still_picks_best():
    """asks may arrive unsorted — function must sort internally."""
    from arb_server import _top_of_book_depth_usd
    asks = [
        {'price': 0.50, 'size': 100},
        {'price': 0.20, 'size': 30},                # lowest = best
        {'price': 0.40, 'size': 200},
    ]
    best, depth = _top_of_book_depth_usd(asks)
    assert best == pytest.approx(0.20)
    assert depth == pytest.approx(0.20 * 30)         # $6


def test_top_of_book_slippage_tolerance_includes_close_levels():
    """With slippage_tolerance=0.005, levels within 0.5c of best count too."""
    from arb_server import _top_of_book_depth_usd
    asks = [
        {'price': 0.300, 'size': 50},
        {'price': 0.302, 'size': 100},               # within 0.5c
        {'price': 0.310, 'size': 999},               # outside
    ]
    best, depth = _top_of_book_depth_usd(asks, slippage_tolerance=0.005)
    assert best == pytest.approx(0.300)
    expected = 0.300 * 50 + 0.302 * 100
    assert depth == pytest.approx(expected)


def test_polymarket_fetch_clob_uses_top_of_book(monkeypatch):
    """End-to-end: _fetch_clob now returns top-of-book depth, not summed."""
    import arb_server
    fake_response_data = {
        'asks': [
            {'price': '0.30', 'size': '50'},
            {'price': '0.31', 'size': '500'},        # walking-the-book
            {'price': '0.40', 'size': '9999'},
        ]
    }

    class _FakeResp:
        def json(self): return fake_response_data

    def _fake_get(*a, **kw):
        return _FakeResp()
    monkeypatch.setattr(arb_server._SESS_POLY, 'get', _fake_get)

    # Phase 10 Task A: 5-tuple now (token_id, ask, ask_depth, bid, bid_depth)
    res = arb_server._fetch_clob('TOKEN_X')
    assert len(res) == 5
    token_id, best, depth = res[0], res[1], res[2]
    assert best == pytest.approx(0.30)
    # Old buggy depth would be 0.30*50 + 0.31*500 + 0.40*9999 = ~$4170
    # New correct depth = 0.30 * 50 = $15
    assert depth == pytest.approx(15.0)
    assert depth < 100, f"depth {depth} suspiciously high — bug regressed?"


def test_kalshi_fetch_orderbook_uses_top_of_book(monkeypatch):
    import arb_server

    class _FakeResp:
        def json(self):
            return {
                'orderbook_fp': {
                    'yes_dollars': [[0.45, 800.0], [0.46, 5000.0]],
                    'no_dollars':  [[0.55, 1200.0], [0.57, 8000.0]],
                }
            }

    def _fake_get(*a, **kw):
        return _FakeResp()
    monkeypatch.setattr(arb_server._SESS_KALSHI, 'get', _fake_get)

    ticker, yes_ask, yes_depth, no_ask, no_depth = arb_server._fetch_kalshi_ob(
        'KXEMOJI-1234')
    assert yes_ask == pytest.approx(0.45)
    assert yes_depth == pytest.approx(800.0)         # USD notional, top only
    assert no_ask == pytest.approx(0.55)
    assert no_depth == pytest.approx(1200.0)
    # Old buggy: yes_depth=800+5000=5800, no_depth=1200+8000=9200
    assert yes_depth < 1500
    assert no_depth < 1500


def test_sx_bet_fetch_orders_uses_top_of_book(monkeypatch):
    """SX Bet maker book: takers fill OPPOSITE side. Top-of-book taker
    depth = sum of maker orders at the SINGLE best maker price."""
    import arb_server

    class _FakeResp:
        def json(self):
            return {
                'status': 'success',
                'data': {
                    'orders': [
                        # Maker on outcomeOne @ 0.45 (= taker_outcomeTwo at 0.55), $300
                        {'percentageOdds': str(int(0.45 * 1e20)),
                         'orderSizeFillable': str(int(300 * 1e6)),
                         'isMakerBettingOutcomeOne': True},
                        # Maker on outcomeOne @ 0.45 (same best), $200 — sums w/ above
                        {'percentageOdds': str(int(0.45 * 1e20)),
                         'orderSizeFillable': str(int(200 * 1e6)),
                         'isMakerBettingOutcomeOne': True},
                        # Maker on outcomeOne @ 0.40 (worse for taker @ 0.60), excluded
                        {'percentageOdds': str(int(0.40 * 1e20)),
                         'orderSizeFillable': str(int(5000 * 1e6)),
                         'isMakerBettingOutcomeOne': True},
                        # Maker on outcomeTwo @ 0.50 (= taker_outcomeOne at 0.50), $400
                        {'percentageOdds': str(int(0.50 * 1e20)),
                         'orderSizeFillable': str(int(400 * 1e6)),
                         'isMakerBettingOutcomeOne': False},
                    ]
                }
            }

    def _fake_get(*a, **kw):
        return _FakeResp()
    monkeypatch.setattr(arb_server._SESS_SX, 'get', _fake_get)

    market_hash, best1, depth1, best2, depth2 = arb_server._fetch_sx_orders(
        '0xMARKETHASH')
    # Taker on outcomeOne pays 1 - 0.50 = 0.50, depth from makers_two: $400 × 0.50 = $200
    assert best1 == pytest.approx(0.50)
    assert depth1 == pytest.approx(200.0)
    # Taker on outcomeTwo pays 1 - 0.45 = 0.55, depth from makers_one at top:
    #   ($300 + $200) × 0.55 = $275 (NOT including the $5000 @ 0.40 maker)
    assert best2 == pytest.approx(0.55)
    assert depth2 == pytest.approx(275.0)
    # Old buggy total would have included the $5000 maker: 5500 × 0.55 = $3025
    assert depth2 < 500, f"SX depth {depth2} regressed — including walked book"


def test_poly_ws_calc_book_top_of_book():
    """poly_ws._calc_book is staticmethod; check it directly."""
    from poly_ws import PolyMarketWS as PolyWS
    asks = [
        {'price': '0.30', 'size': '50'},
        {'price': '0.31', 'size': '500'},
        {'price': '0.30', 'size': '20'},             # tied at best — sums
    ]
    best, depth = PolyWS._calc_book(asks)
    assert best == pytest.approx(0.30)
    assert depth == pytest.approx(0.30 * 70)         # $21


def test_poly_ws_calc_book_empty():
    from poly_ws import PolyMarketWS as PolyWS
    assert PolyWS._calc_book([]) == (None, 0.0)
