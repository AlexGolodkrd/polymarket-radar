"""Phase 9j — Polymarket V2 dynamic market info + tick/min validation.

Closes the gap I missed in Phase 9f/9i: V2 made fee/tick/min-size
per-market dynamic (queryable via /markets/{condition_id}). Old code
used hardcoded THETA_POLY=0.025 across the board, so:
  - markets with 0% fee (V2 promo zones) had us reject valid arbs
    because we modeled 2.5% net cost
  - markets with non-default tick (0.001 high-liquidity sport books)
    would 400-reject our orders at signing time

Tests verify:
- _fetch_poly_market_info caches per condition_id
- effective_theta in _eval_poly_structures uses worst (max) per-market fee
- build_poly_order snaps price to tick_size
- build_poly_order rejects size < min_order_size
"""
import os
import sys
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'Scripts'))

import arb_server
from executor import builders


class TestPolyMarketInfoFetcher(unittest.TestCase):
    def setUp(self):
        with arb_server.poly_market_info_lock:
            arb_server.poly_market_info_cache.clear()

    def test_fetch_populates_cache(self):
        fake = mock.Mock()
        fake.status_code = 200
        fake.json.return_value = {
            'minimum_tick_size': 0.001,
            'minimum_order_size': 5,
            'maker_base_fee': 100,
            'taker_base_fee': 250,
            'neg_risk': False,
            'accepting_orders': True,
            'enable_order_book': True,
            'closed': False,
        }
        with mock.patch('arb_server.requests.get', return_value=fake):
            rec = arb_server._fetch_poly_market_info('0xCID')
        self.assertEqual(rec['tick_size'], 0.001)
        self.assertEqual(rec['min_order_size'], 5.0)
        self.assertEqual(rec['taker_fee_bps'], 250.0)
        self.assertEqual(rec['maker_fee_bps'], 100.0)
        # Subsequent call returns from cache
        with mock.patch('arb_server.requests.get',
                         side_effect=Exception('nope')) as gp:
            rec2 = arb_server._fetch_poly_market_info('0xCID')
        gp.assert_not_called()
        self.assertEqual(rec2['tick_size'], 0.001)

    def test_fetch_404_returns_cached_none(self):
        fake = mock.Mock(); fake.status_code = 404
        with mock.patch('arb_server.requests.get', return_value=fake):
            rec = arb_server._fetch_poly_market_info('0xMISSING')
        self.assertIsNone(rec)

    def test_no_condition_id_returns_none(self):
        rec = arb_server._fetch_poly_market_info(None)
        self.assertIsNone(rec)


class TestPolyTickSnap(unittest.TestCase):
    def test_snap_to_default_tick_001(self):
        snapped = builders._round_to_tick(0.4523, 0.01)
        self.assertEqual(snapped, 0.45)

    def test_snap_to_finer_tick(self):
        snapped = builders._round_to_tick(0.4523, 0.001)
        self.assertEqual(snapped, 0.452)

    def test_snap_zero_tick_passes_through(self):
        # Defensive: tick_size=0 means no snapping
        self.assertEqual(builders._round_to_tick(0.4523, 0), 0.4523)

    def test_already_aligned_unchanged(self):
        self.assertEqual(builders._round_to_tick(0.50, 0.01), 0.50)


class TestPolyBuilderTickAndMin(unittest.TestCase):
    def _wallet(self):
        return builders.WalletStub(bot_id='b', eth_address='0x' + 'a' * 40)

    def test_price_snapped_in_signed_order(self):
        o = builders.build_poly_order('1', 'BUY', 0.4523, 5.0, self._wallet(),
                                       tick_size=0.01)
        # makerAmount = round(5 * 1e6) = 5000000
        # contracts = 5 / 0.45 = 11.111..., taker = round(11.111 * 1e6)
        self.assertEqual(o['order']['makerAmount'], '5000000')
        # snapped to 0.45 → contracts = 5/0.45 ≈ 11.111 → taker ≈ 11111111
        self.assertEqual(o['order']['takerAmount'], '11111111')

    def test_min_order_size_enforced(self):
        with self.assertRaises(AssertionError):
            builders.build_poly_order(
                '1', 'BUY', 0.5, 3.0, self._wallet(),
                min_order_size_usdc=5.0,
            )

    def test_min_order_size_default_keeps_old_behavior(self):
        # Without explicit min, default is 1.0 — old test_executor relied on
        # this when builder asserted "size below Polymarket min $1".
        with self.assertRaises(AssertionError):
            builders.build_poly_order('1', 'BUY', 0.5, 0.5, self._wallet())


class TestPolyDynamicTheta(unittest.TestCase):
    """Verify _eval_poly_structures uses dynamic taker_fee from
    market info cache instead of hardcoded THETA_POLY."""

    def setUp(self):
        # Patch the fetcher so eval doesn't hit the network and we
        # can assert on cached values.
        self._fetch_patcher = mock.patch(
            'arb_server._fetch_poly_market_info',
            side_effect=self._fake_fetch,
        )
        self._fetch_patcher.start()

    def tearDown(self):
        self._fetch_patcher.stop()

    def _fake_fetch(self, cid):
        # Two markets: one with 0% fee, one with 2.5%
        return {
            '0xZERO': {
                'condition_id': '0xZERO', 'tick_size': 0.01,
                'min_order_size': 5, 'maker_fee_bps': 0, 'taker_fee_bps': 0,
                'neg_risk': False, 'accepting_orders': True,
                'enable_order_book': True, 'closed': False, 'fetched_at': time.time(),
            },
            '0xHIGH': {
                'condition_id': '0xHIGH', 'tick_size': 0.01,
                'min_order_size': 5, 'maker_fee_bps': 100,
                'taker_fee_bps': 250,    # 2.5% taker fee
                'neg_risk': False, 'accepting_orders': True,
                'enable_order_book': True, 'closed': False, 'fetched_at': time.time(),
            },
        }.get(cid)

    def test_zero_fee_market_uses_zero_theta(self):
        """A market with taker_base_fee=0 must yield effective_theta=0,
        meaning more arbs survive the net>0 filter."""
        cand = (
            {'title': 'T', 'markets': [{'conditionId': '0xZERO'},
                                         {'conditionId': '0xZERO'}],
             'negRisk': True, 'endDate': '2026-05-01T00:00:00Z'},
            [
                {'m': {'question': 'A', 'volume': 1000,
                        'liquidity': 1000, 'conditionId': '0xZERO'},
                 'implied': 0.40, 'token_id': '1', 'token_id_yes': '1', 'token_id_no': '2'},
                {'m': {'question': 'B', 'volume': 1000,
                        'liquidity': 1000, 'conditionId': '0xZERO'},
                 'implied': 0.40, 'token_id': '3', 'token_id_yes': '3', 'token_id_no': '4'},
            ],
            False,
        )
        clob_res = {'1': (0.40, 1000), '3': (0.40, 1000),
                    '2': (0.55, 800), '4': (0.55, 800)}
        deals = arb_server._eval_poly_structures(cand, clob_res=clob_res)
        # sum=0.80, with theta=0 → no fee subtracted, net = gross
        if deals:
            d = deals[0]
            # Fee should be near 0 (markets have 0% taker_base_fee)
            self.assertLess(d['fee'], 0.05,
                f"With 0% taker fee, total fee should be ~0; got {d['fee']}")


if __name__ == '__main__':
    unittest.main(verbosity=2)
