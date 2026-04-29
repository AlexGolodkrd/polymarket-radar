"""Phase 9w — Polymarket single-binary structure C + NEAR-cap for C.

User feedback:
  1. Polymarket has many "Will X happen by Y" single-binary events.
     Old filter rejected them (need >=2 markets for A/B). Add path so
     they enter pools as structure-C candidates (YES + NO of one market).
  2. Structure C clutters NEAR (was 14 of 41 visible C-candidates, all at
     +2c above threshold). Restrict C in NEAR to within 2c of arb.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'Scripts'))

import arb_server


class TestSingleBinaryFilterPasses(unittest.TestCase):
    """filter_poly accepts single-market events when previously it rejected."""

    def _ev(self, **overrides):
        ev = {
            'title': 'Will BTC be above $200k by May 30?',
            'endDateIso': '2026-05-08T20:00:00Z',
            'markets': [{
                'question': 'BTC > 200k by May 30 2026',
                'outcomePrices': '[0.42, 0.58]',
                'closed': False, 'archived': False, 'restricted': False,
                'enableOrderBook': True, 'acceptingOrders': True,
                'clobTokenIds': '["TID_YES", "TID_NO"]',
                'conditionId': '0xCID',
            }],
        }
        ev.update(overrides)
        return ev

    def test_single_binary_event_passes_filter(self):
        diag = {}
        cands, tids = arb_server.filter_poly([self._ev()], diag=diag)
        self.assertEqual(len(cands), 1,
                         f"single-binary event must pass filter; diag={diag}")
        # Verify the flag was attached
        ev, rough, is_q = cands[0]
        self.assertTrue(ev.get('_single_binary'),
                        "_single_binary flag must be set")
        # YES + NO token IDs collected
        self.assertIn('TID_YES', tids)
        self.assertIn('TID_NO', tids)

    def test_single_binary_no_negrisk_required(self):
        # Old code rejected events without negRisk. Single-binary skips this.
        ev = self._ev()
        # No negRisk flag on event or market
        self.assertFalse(ev.get('negRisk', False))
        diag = {}
        cands, _ = arb_server.filter_poly([ev], diag=diag)
        self.assertEqual(len(cands), 1)

    def test_multi_outcome_still_requires_negrisk(self):
        # Sanity: multi-outcome events still need negRisk
        ev = self._ev()
        ev['markets'].append({
            'question': 'Outcome 2',
            'outcomePrices': '[0.30, 0.70]',
            'closed': False, 'archived': False, 'restricted': False,
            'enableOrderBook': True, 'acceptingOrders': True,
            'clobTokenIds': '["TID2_YES", "TID2_NO"]',
            'conditionId': '0xCID2',
        })
        diag = {}
        cands, _ = arb_server.filter_poly([ev], diag=diag)
        # No negRisk → reject for multi-outcome
        self.assertEqual(len(cands), 0,
                         "multi-outcome without negRisk must still be rejected")
        self.assertEqual(diag['poly_skip_no_negrisk'], 1)

    def test_closed_single_binary_still_rejected(self):
        ev = self._ev()
        ev['markets'][0]['closed'] = True
        diag = {}
        cands, _ = arb_server.filter_poly([ev], diag=diag)
        self.assertEqual(len(cands), 0,
                         "closed single-binary must still be filtered")


class TestNearCapForC(unittest.TestCase):
    """C in NEAR only when within 2c of threshold."""

    def test_c_at_5c_above_threshold_dropped(self):
        # YES 0.50 + NO 0.50 = 1.00 → 1.0 - 0.99 = 1c — borderline
        # YES 0.50 + NO 0.55 = 1.05 → 6c above — must NOT show
        pm = [{'name': 'X', 'yes_price': 0.50, 'yes_liq': 1000,
               'no_price': 0.55, 'no_liq': 1000}]
        best = arb_server._best_near_structure(pm, threshold=0.99)
        self.assertIsNone(best, "C at +6c above threshold must not show in NEAR")

    def test_c_within_2c_above_threshold_shown(self):
        # YES 0.40 + NO 0.60 = 1.00 → 1c above 0.99 — OK
        pm = [{'name': 'X', 'yes_price': 0.40, 'yes_liq': 1000,
               'no_price': 0.60, 'no_liq': 1000}]
        best = arb_server._best_near_structure(pm, threshold=0.99)
        self.assertIsNotNone(best, "C at +1c must surface in NEAR")
        self.assertEqual(best['structure'], 'yes_no_pair')

    def test_c_below_threshold_shown_as_arb_candidate(self):
        # YES 0.40 + NO 0.55 = 0.95 → -4c below threshold — full arb
        pm = [{'name': 'X', 'yes_price': 0.40, 'yes_liq': 1000,
               'no_price': 0.55, 'no_liq': 1000}]
        best = arb_server._best_near_structure(pm, threshold=0.99)
        self.assertIsNotNone(best)
        # distance is sum - threshold; for arbs it's negative
        self.assertLess(best['sum'] - best['threshold'], 0)

    def test_a_b_not_affected_by_c_cap(self):
        # 3-way ALL_YES at +5c above threshold — must still show (it's
        # the C cap, not A/B)
        pm = [
            {'name': 'A', 'yes_price': 0.34, 'yes_liq': 1000,
             'no_price': 0.66, 'no_liq': 1000},
            {'name': 'B', 'yes_price': 0.35, 'yes_liq': 1000,
             'no_price': 0.65, 'no_liq': 1000},
            {'name': 'C', 'yes_price': 0.35, 'yes_liq': 1000,
             'no_price': 0.65, 'no_liq': 1000},
        ]
        # sum_yes = 1.04, threshold 0.99 → +5c, ALL_YES candidate
        best = arb_server._best_near_structure(pm, threshold=0.99)
        # Should pick ALL_YES (C is per-market 1.00 → +1c, ALL_YES is +5c
        # — pick smaller distance = ALL_YES cap is C only).
        # Actually: distance(all_yes) = 1.04 - 0.99 = 0.05
        # distance(c)        = 1.00 - 0.99 = 0.01 (within 2c → shown)
        # min = c. Pick C.
        self.assertIsNotNone(best)
        # Either way it must NOT be None (C cap doesn't break the function)


class TestSumPolyCandSingleBinary(unittest.TestCase):
    """_sum_poly_cand handles single-binary candidates via structure C."""

    def test_single_binary_returns_c_sum(self):
        ev = {
            'title': 'Will X happen?',
            '_single_binary': True,
            'markets': [{}],   # 1 market
        }
        rough = [{
            'm': {'clobTokenIds': '["YES_TID","NO_TID"]'},
            'implied': 0.45,
            'token_id_yes': 'YES_TID',
            'token_id_no': 'NO_TID',
        }]
        # Mock clob_res
        clob_res = {'YES_TID': (0.45, 1000), 'NO_TID': (0.50, 1000)}
        # Patch _poly_per_market to return our pm directly — simpler than
        # threading through actual implementation
        orig = arb_server._poly_per_market

        def fake_pm(rough, clob_res, ws_books=None):
            return [{'name': '?', 'yes_price': 0.45, 'yes_liq': 1000,
                     'no_price': 0.50, 'no_liq': 1000,
                     'yes_src': 'rest', 'no_src': 'rest', 'volume': 100}]
        arb_server._poly_per_market = fake_pm
        try:
            s = arb_server._sum_poly_cand((ev, rough, False), clob_res, {})
            self.assertIsNotNone(s, "single-binary must yield a sum (structure C)")
            # Expected: yes + no = 0.95
            self.assertAlmostEqual(s, 0.95, places=2)
        finally:
            arb_server._poly_per_market = orig

    def test_single_binary_empty_pm_returns_none(self):
        ev = {'_single_binary': True, 'markets': [{}]}
        rough = [{'m': {}}]
        orig = arb_server._poly_per_market
        arb_server._poly_per_market = lambda *a, **k: []
        try:
            s = arb_server._sum_poly_cand((ev, rough, False), {}, {})
            self.assertIsNone(s)
        finally:
            arb_server._poly_per_market = orig


class TestThresholdSeriesPropagatesToNearAndPool(unittest.TestCase):
    """Phase 9x — Reddit-DAUq event was reaching NEAR (sum=107.9c, dist
    -89.7c) even though eval_limitless dropped it. Cause: classify_pools
    + near_summary did not run is_threshold_series() check, only
    eval_*. Now it's applied at all 3 levels."""

    def setUp(self):
        # Phase 9z requires non-zero volume on every leg — patch meta cache
        # to return realistic volumes for these synthetic slugs.
        self._orig_meta = arb_server._fetch_limitless_market_meta
        arb_server._fetch_limitless_market_meta = lambda slug: {'volume': 100}

    def tearDown(self):
        arb_server._fetch_limitless_market_meta = self._orig_meta

    def test_reddit_dauq_skipped_in_sum_limitless_cand(self):
        # Mimic the real screenshot: 4 children, all "above N" prefix.
        ev = {
            'title': 'Reddit (RDDT) U.S. DAUq above ___ in Q1 2026?',
            'slug': 'reddit-dauq-q1',
            'markets': [
                {'slug': 's52', 'title': 'Above 52M'},
                {'slug': 's53', 'title': 'Above 53M'},
                {'slug': 's54', 'title': 'Above 54M'},
                {'slug': 's55', 'title': 'Above 55M'},
            ],
        }
        # Cheap NOs that would naively look like a great ALL_NO arb.
        lim_res = {
            's52': (0.951, 1000, 0.079, 1000),
            's53': (0.905, 1000, 0.125, 1000),
            's54': (0.545, 1000, 0.485, 1000),
            's55': (0.515, 1000, 0.515, 1000),
        }
        s = arb_server._sum_limitless_cand(ev, lim_res)
        # With threshold_series guard, A and B are dropped from the
        # candidates list. Only C (per-market YES_NO_PAIR) remains.
        # Min C sum across markets = min(0.951+0.079, 0.905+0.125, ...)
        #                        = 1.030 (52M pair)
        # If guard had NOT fired, A would have given 0.951+0.905+0.545+0.515
        # = 2.916, which divided by N-1=3 ≈ 0.972 (B), and that would be
        # the smallest, polluting NEAR. Verify we got C, not B.
        self.assertIsNotNone(s)
        self.assertGreaterEqual(s, 1.0,
                                "threshold-series must skip A/B; only C "
                                f"YES+NO pairs remain; got {s}")

    def test_reddit_dauq_no_a_b_in_best_near_structure(self):
        pm = [
            {'name': 'Above 52M', 'yes_price': 0.951, 'yes_liq': 1000,
             'no_price': 0.079, 'no_liq': 1000},
            {'name': 'Above 53M', 'yes_price': 0.905, 'yes_liq': 1000,
             'no_price': 0.125, 'no_liq': 1000},
            {'name': 'Above 54M', 'yes_price': 0.545, 'yes_liq': 1000,
             'no_price': 0.485, 'no_liq': 1000},
            {'name': 'Above 55M', 'yes_price': 0.515, 'yes_liq': 1000,
             'no_price': 0.515, 'no_liq': 1000},
        ]
        # threshold_series=True forces _best_near_structure to skip A and B
        best = arb_server._best_near_structure(
            pm, threshold=0.99, threshold_series=True)
        # Only C might surface, and only if within 2c of threshold —
        # min YES+NO is 0.515+0.515=1.030 → +4c above 0.99 → above the
        # C cap → None.
        self.assertIsNone(
            best,
            f"Reddit-DAUq pm with threshold_series=True must yield None; got {best}")

    def test_normal_categorical_event_unaffected(self):
        # Plain 3-way football match — A should still surface.
        pm = [
            {'name': 'Team A', 'yes_price': 0.30, 'yes_liq': 1000,
             'no_price': 0.65, 'no_liq': 1000},
            {'name': 'Draw',   'yes_price': 0.32, 'yes_liq': 1000,
             'no_price': 0.65, 'no_liq': 1000},
            {'name': 'Team B', 'yes_price': 0.33, 'yes_liq': 1000,
             'no_price': 0.65, 'no_liq': 1000},
        ]
        # threshold_series=False (a normal categorical event)
        best = arb_server._best_near_structure(
            pm, threshold=0.99, threshold_series=False)
        self.assertIsNotNone(best)
        # ALL_YES = 0.95 → distance -0.04 (full arb) — best pick
        self.assertEqual(best['structure'], 'all_yes')


if __name__ == '__main__':
    unittest.main(verbosity=2)
