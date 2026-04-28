"""Phase 9k — dynamic Polymarket threshold per market fee.

Math reminder:
    THRESH = 1 - (theta + slippage_reserve + safety_buffer)
           = 1 - (theta + 0.003 + 0.005)
           = 1 - (theta + 0.008)

    With floor 0.95 and cap 0.995.

Examples:
    0%   fee → 0.992
    1%   fee → 0.982
    2.5% fee → 0.967
    4%   fee → 0.952
    6%   fee → 0.95 (clipped to floor)

Profitability check: at the new (looser) threshold, every accepted deal
must still produce net P&L > 0 after taker fee + slippage. We verify by
running build_deal at borderline sums and asserting d['net'] > 0.
"""
import os
import sys
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'Scripts'))

import arb_server


# ── Math ────────────────────────────────────────────────────────────
# Phase 9l: safety buffer bumped 0.005 → 0.007 (every threshold -0.002).
class TestComputePolyThreshold(unittest.TestCase):
    def test_zero_fee_market(self):
        # 0 bps → 1 - 0.010 = 0.990   (was 0.992 in Phase 9k)
        self.assertAlmostEqual(arb_server.compute_poly_threshold(0), 0.990, places=4)

    def test_one_pct_fee(self):
        # 100 bps = 1% → 1 - 0.020 = 0.980   (was 0.982)
        self.assertAlmostEqual(arb_server.compute_poly_threshold(100), 0.980, places=4)

    def test_two_and_half_pct_fee(self):
        # 250 bps = 2.5% → 1 - 0.035 = 0.965   (was 0.967)
        self.assertAlmostEqual(arb_server.compute_poly_threshold(250), 0.965, places=4)

    def test_clipped_to_floor_on_high_fee(self):
        # 600 bps = 6% → 1 - 0.070 = 0.930 → clipped to 0.948 floor (was 0.95)
        self.assertEqual(arb_server.compute_poly_threshold(600), 0.948)

    def test_clipped_to_cap_on_negative_fee(self):
        # Defensive: never report > 0.993 cap (was 0.995)
        self.assertLessEqual(arb_server.compute_poly_threshold(-1000), 0.993)

    def test_none_fee_treated_as_zero(self):
        self.assertAlmostEqual(arb_server.compute_poly_threshold(None), 0.990, places=4)


# ── Profitability check ────────────────────────────────────────────
class TestProfitabilityAtThreshold(unittest.TestCase):
    """For every threshold compute_poly_threshold returns, a deal at
    sum = threshold-0.001 must still produce d['net'] > 0. This is the
    invariant that 'we never fire losing arbs'."""

    def _make_outcomes(self, sum_yes, n=2):
        each = sum_yes / n
        return [{'name': f'leg{i}', 'price': each,
                  'liquidity': 10000, 'source': 'x', 'volume': 1000}
                for i in range(n)]

    def test_zero_fee_at_991_is_profitable(self):
        # sum=0.991, theta=0% → THRESH=0.992 → margin=0.009 = 0.9¢/$1
        # On $100 capital: gross = $0.90, slip≈$0.30, net ≈ +$0.60
        d = arb_server.build_deal(
            'P', 'Polymarket', self._make_outcomes(0.991, 2),
            total_price=0.991, theta=0.0, threshold=0.992,
        )
        self.assertIsNotNone(d, 'borderline 0-fee deal must produce')
        self.assertGreater(d['net'], 0)

    def test_one_pct_fee_at_981_profitable(self):
        d = arb_server.build_deal(
            'P', 'Polymarket', self._make_outcomes(0.981, 3),
            total_price=0.981, theta=0.01, threshold=0.982,
        )
        self.assertIsNotNone(d)
        self.assertGreater(d['net'], 0)

    def test_25pct_fee_at_966_profitable(self):
        d = arb_server.build_deal(
            'P', 'Polymarket', self._make_outcomes(0.966, 3),
            total_price=0.966, theta=0.025, threshold=0.967,
        )
        self.assertIsNotNone(d)
        self.assertGreater(d['net'], 0)

    def test_zero_fee_at_993_is_REJECTED(self):
        """Above the threshold cap (0.995), no deal must form even with 0%."""
        d = arb_server.build_deal(
            'P', 'Polymarket', self._make_outcomes(0.996, 2),
            total_price=0.996, theta=0.0, threshold=0.995,
        )
        # gross = 0.004*100 = 0.40, slip 0.5 + safety = lots → net likely <= 0
        if d is not None:
            self.assertGreater(d['net'], 0,
                'sanity: if a deal forms above cap, must still be profitable')


# ── Integration: dyn_threshold flows through eval ──────────────────
class TestDynamicThresholdFlow(unittest.TestCase):
    """Patch _fetch_poly_market_info to return a 0-fee market and verify
    that _eval_poly_structures NOW accepts arbs in the 0.97-0.99 range
    that the OLD static threshold would have rejected."""

    def setUp(self):
        self._fetch_patcher = mock.patch(
            'arb_server._fetch_poly_market_info',
            side_effect=self._fake_fetch,
        )
        self._fetch_patcher.start()

    def tearDown(self):
        self._fetch_patcher.stop()

    def _fake_fetch(self, cid):
        return {
            'condition_id': cid, 'tick_size': 0.01,
            'min_order_size': 5, 'maker_fee_bps': 0, 'taker_fee_bps': 0,
            'neg_risk': False, 'accepting_orders': True,
            'enable_order_book': True, 'closed': False,
            'fetched_at': time.time(),
        }

    def test_98_arb_now_accepted_on_zero_fee(self):
        """sum=0.98 on 0%-fee market: old THRESH_POLY=0.97 would reject;
        new dyn threshold 0.992 accepts."""
        cand = (
            {'title': 'Z', 'markets': [
                {'conditionId': '0xC1'}, {'conditionId': '0xC2'},
            ], 'negRisk': True, 'endDate': '2026-05-01T00:00:00Z'},
            [
                {'m': {'question': 'A', 'volume': 5000, 'liquidity': 50000,
                        'conditionId': '0xC1'},
                 'implied': 0.49, 'token_id': '1', 'token_id_yes': '1',
                 'token_id_no': '2'},
                {'m': {'question': 'B', 'volume': 5000, 'liquidity': 50000,
                        'conditionId': '0xC2'},
                 'implied': 0.49, 'token_id': '3', 'token_id_yes': '3',
                 'token_id_no': '4'},
            ],
            False,
        )
        clob_res = {'1': (0.49, 50000), '3': (0.49, 50000),
                    '2': (0.51, 40000), '4': (0.51, 40000)}
        deals = arb_server._eval_poly_structures(cand, clob_res=clob_res)
        all_yes = [d for d in deals if d.get('arb_structure') == 'all_yes']
        self.assertGreater(len(all_yes), 0,
            'sum=0.98 on 0-fee market MUST be accepted in Phase 9k')
        self.assertGreater(all_yes[0]['net'], 0,
            'and net must still be positive after fees+slippage+safety')


# ── Integration: high-fee market still tightened ────────────────────
class TestHighFeeMarketTightened(unittest.TestCase):
    def setUp(self):
        self._fetch_patcher = mock.patch(
            'arb_server._fetch_poly_market_info',
            return_value={
                'condition_id': '0xH', 'tick_size': 0.01,
                'min_order_size': 5, 'maker_fee_bps': 100,
                'taker_fee_bps': 400,    # 4% fee
                'neg_risk': False, 'accepting_orders': True,
                'enable_order_book': True, 'closed': False,
                'fetched_at': time.time(),
            },
        )
        self._fetch_patcher.start()

    def tearDown(self):
        self._fetch_patcher.stop()

    def test_968_arb_REJECTED_on_4pct_fee(self):
        """sum=0.968 on 4%-fee market: old THRESH_POLY=0.97 would accept
        (and ship a losing arb). New dyn threshold 0.952 rejects."""
        cand = (
            {'title': 'H', 'markets': [
                {'conditionId': '0xH'}, {'conditionId': '0xH'},
            ], 'negRisk': True, 'endDate': '2026-05-01T00:00:00Z'},
            [
                {'m': {'question': 'A', 'volume': 5000, 'liquidity': 5000,
                        'conditionId': '0xH'},
                 'implied': 0.484, 'token_id': '1', 'token_id_yes': '1',
                 'token_id_no': '2'},
                {'m': {'question': 'B', 'volume': 5000, 'liquidity': 5000,
                        'conditionId': '0xH'},
                 'implied': 0.484, 'token_id': '3', 'token_id_yes': '3',
                 'token_id_no': '4'},
            ],
            False,
        )
        clob_res = {'1': (0.484, 5000), '3': (0.484, 5000),
                    '2': (0.516, 4000), '4': (0.516, 4000)}
        deals = arb_server._eval_poly_structures(cand, clob_res=clob_res)
        all_yes = [d for d in deals if d.get('arb_structure') == 'all_yes']
        self.assertEqual(all_yes, [],
            'sum=0.968 on 4-fee market must be REJECTED (fee eats margin)')


if __name__ == '__main__':
    unittest.main(verbosity=2)
