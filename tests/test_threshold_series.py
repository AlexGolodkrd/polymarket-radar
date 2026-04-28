"""Phase 9o — threshold-series guard.

The Reddit-DAUq-104%-ROI bug: an event titled "Reddit (RDDT) U.S. DAUq
above ___ in Q1 2026?" with multiple "above N" child markets was reported
as a $104 net / 104% ROI ALL_NO arb. This is a phantom — overlapping
threshold YES/NO tokens are NOT mutually exclusive, so the (N-1)-of-N
payout assumption fails. is_threshold_series() must catch such events
and force eval_* to skip ALL_YES / ALL_NO (YES_NO_PAIR per market is fine).
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'Scripts'))

import arb_server


class TestIsThresholdSeries(unittest.TestCase):
    def test_explicit_placeholder_is_threshold(self):
        # Real-world Reddit-DAUq title that triggered the bug
        self.assertTrue(arb_server.is_threshold_series(
            "Reddit (RDDT) U.S. DAUq above ___ in Q1 2026?"))

    def test_above_with_number_is_threshold(self):
        self.assertTrue(arb_server.is_threshold_series(
            "Will BTC be above $100,000 by end of year"))
        self.assertTrue(arb_server.is_threshold_series(
            "ETH above 5000 in Q1"))

    def test_below_under_over_more_than_less_than(self):
        for t in [
            "Will inflation be below 3% in 2026",
            "Tesla under $200 by Friday",
            "Apple over 250 by Q2",
            "Will BTC be more than 100k",
            "Will ETH be less than 4000",
            "GDP growth at least 2%",
            "Unemployment at most 4%",
        ]:
            self.assertTrue(
                arb_server.is_threshold_series(t),
                f"should flag as threshold: {t!r}")

    def test_russian_threshold_phrasing(self):
        for t in [
            "Будет ли курс выше 100 рублей",
            "Цена ниже 50000 на закрытии",
        ]:
            self.assertTrue(
                arb_server.is_threshold_series(t),
                f"should flag as threshold: {t!r}")

    def test_categorical_event_is_NOT_threshold(self):
        # Mutually-exclusive outcomes — ALL_YES / ALL_NO valid here
        for t in [
            "Who will win the 2026 World Cup",
            "EPL: Leeds vs Burnley",
            "2024 Presidential Election Winner",
            "Best Picture Oscar 2026",
        ]:
            self.assertFalse(
                arb_server.is_threshold_series(t),
                f"must NOT flag categorical event: {t!r}")

    def test_children_with_same_above_prefix_is_threshold(self):
        # No "above ___" in parent, but every child starts with "Above N"
        children = ["Above 65M", "Above 70M", "Above 75M", "Above 80M"]
        self.assertTrue(arb_server.is_threshold_series(
            "Reddit DAUq Q1 2026", children))

    def test_children_with_mixed_prefixes_NOT_threshold(self):
        # Categorical names — sometimes contain digits but not threshold
        children = ["Team A", "Team B", "Draw"]
        self.assertFalse(arb_server.is_threshold_series(
            "Match outcome", children))

    def test_just_2_children_not_enough_for_secondary_signal(self):
        # We require at least 3 children for the children-prefix heuristic
        # to avoid false positives on plain binary markets.
        children = ["Above 100", "Above 200"]
        self.assertFalse(arb_server.is_threshold_series(
            "Some event", children))

    def test_empty_title_is_not_threshold(self):
        self.assertFalse(arb_server.is_threshold_series(""))
        self.assertFalse(arb_server.is_threshold_series(None))


class TestEvalLimitlessSkipsThresholdSeries(unittest.TestCase):
    """End-to-end: a Reddit-DAUq-style event must NOT produce a deal under
    structures A (ALL_YES) or B (ALL_NO), only under C (YES_NO_PAIR per
    market) — and even then only if a per-market arb exists individually."""

    def setUp(self):
        # Patch _fetch_limitless_market_meta — eval_limitless calls it for
        # token IDs; we don't need real values.
        self._orig_fetch = arb_server._fetch_limitless_market_meta
        arb_server._fetch_limitless_market_meta = lambda slug: {
            'yes_token': '0x' + '1'*40, 'no_token': '0x' + '2'*40,
            'verifying_contract': '0x' + '3'*40, 'volume': 1000,
        }

    def tearDown(self):
        arb_server._fetch_limitless_market_meta = self._orig_fetch

    def _build_event(self, title, child_titles, yes_prices, no_prices):
        children = [{'slug': f's{i}', 'title': t}
                    for i, t in enumerate(child_titles)]
        ev = {'title': title, 'markets': children,
              'deadline': 9999999999, 'slug': 'parent'}
        # lim_res maps slug → (yes_ask, yes_depth, no_ask, no_depth)
        lim_res = {f's{i}': (yes_prices[i], 1000, no_prices[i], 1000)
                   for i in range(len(child_titles))}
        return [ev], lim_res

    def test_threshold_series_event_yields_zero_all_no_deals(self):
        """The exact bug from the screenshot: 'above ___' parent +
        4 'above Nx' children. Sum NO ≈ 1.95 — would have triggered an
        ALL_NO 104% ROI phantom arb. Phase 9o must drop it."""
        events, lim_res = self._build_event(
            title="Reddit (RDDT) U.S. DAUq above ___ in Q1 2026?",
            child_titles=["Above 65M", "Above 70M", "Above 75M", "Above 80M"],
            # Cheap NOs that would naively look like a great ALL_NO arb
            yes_prices=[0.51, 0.51, 0.51, 0.51],
            no_prices=[0.49, 0.49, 0.49, 0.49],  # sum_no = 1.96 < 3*0.988
        )
        deals = arb_server.eval_limitless(events, lim_res)
        # Filter to the parent event's own structures only
        parent_deals = [d for d in deals
                        if 'Reddit' in d.get('title', '')]
        all_no_deals = [d for d in parent_deals
                        if d.get('arb_structure') == 'all_no']
        all_yes_deals = [d for d in parent_deals
                         if d.get('arb_structure') == 'all_yes']
        self.assertEqual(len(all_no_deals), 0,
                         "ALL_NO must be skipped on threshold-series events")
        self.assertEqual(len(all_yes_deals), 0,
                         "ALL_YES must be skipped on threshold-series events")

if __name__ == '__main__':
    unittest.main(verbosity=2)
