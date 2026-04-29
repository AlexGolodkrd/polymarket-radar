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

class TestStructureAThresholdGuard(unittest.TestCase):
    """Same threshold-series test for structure A (ALL_YES) on Limitless.
    A phantom 104% bug is mirror-symmetric: a series of overlapping 'above N'
    YES tokens whose sum < 1 looks like a real ALL_YES arb but isn't —
    multiple YES tokens win simultaneously, sum-identity breaks."""

    def setUp(self):
        self._orig_fetch = arb_server._fetch_limitless_market_meta
        arb_server._fetch_limitless_market_meta = lambda slug: {
            'yes_token': '0x' + '1'*40, 'no_token': '0x' + '2'*40,
            'verifying_contract': '0x' + '3'*40, 'volume': 1000,
        }

    def tearDown(self):
        arb_server._fetch_limitless_market_meta = self._orig_fetch

    def test_threshold_yes_sum_below_threshold_is_skipped(self):
        # Threshold-series + sum_yes 0.95 < 0.988 — pre-fix this would have
        # been an "ALL_YES arb" with phantom $5+ profit. Must be dropped.
        events = [{
            'title': 'BTC closing price above ___ on Dec 31 2026',
            'markets': [
                {'slug': 's0', 'title': 'Above 90k'},
                {'slug': 's1', 'title': 'Above 100k'},
                {'slug': 's2', 'title': 'Above 110k'},
            ],
            'deadline': 9999999999, 'slug': 'parent',
        }]
        lim_res = {
            's0': (0.30, 1000, 0.70, 1000),
            's1': (0.32, 1000, 0.68, 1000),
            's2': (0.33, 1000, 0.67, 1000),
        }  # sum_yes = 0.95
        deals = arb_server.eval_limitless(events, lim_res)
        all_yes = [d for d in deals if d.get('arb_structure') == 'all_yes']
        self.assertEqual(len(all_yes), 0,
                         "ALL_YES on threshold-series 'above ___' must be dropped")


class TestStructureCReciprocalSafety(unittest.TestCase):
    """Structure C (YES_NO_PAIR per market) audit: it pairs YES+NO of the
    SAME binary market. By construction these are reciprocal — exactly
    one wins $1 — so structure C is immune to the threshold-series bug.

    These tests confirm the implementation does NOT mix YES_X with NO_Y
    of a different market (which WOULD be a phantom arb)."""

    def setUp(self):
        self._orig_fetch = arb_server._fetch_limitless_market_meta
        arb_server._fetch_limitless_market_meta = lambda slug: {
            'yes_token': f'0xYES_{slug}', 'no_token': f'0xNO_{slug}',
            'verifying_contract': '0x' + '3'*40, 'volume': 1000,
        }

    def tearDown(self):
        arb_server._fetch_limitless_market_meta = self._orig_fetch

    def test_yes_no_pair_uses_same_slug_per_market(self):
        # Even on a threshold-series event, structure C is allowed
        # because each pair is reciprocal within one market.
        events = [{
            'title': 'BTC above ___ EOY',
            'markets': [
                {'slug': 'sA', 'title': 'Above 90k'},
                {'slug': 'sB', 'title': 'Above 100k'},
            ],
            'deadline': 9999999999, 'slug': 'parent',
        }]
        lim_res = {
            'sA': (0.45, 1000, 0.50, 1000),  # sum 0.95 — C-arb candidate
            'sB': (0.50, 1000, 0.49, 1000),  # sum 0.99 — also candidate
        }
        deals = arb_server.eval_limitless(events, lim_res)
        c_deals = [d for d in deals if d.get('arb_structure') == 'yes_no_pair']
        # Each leg must point at the SAME slug as its partner — never mixed
        for d in c_deals:
            entries = d.get('entries', [])
            self.assertEqual(len(entries), 2)
            slugs = {e.get('slug') for e in entries}
            self.assertEqual(len(slugs), 1,
                             f'YES_NO_PAIR mixed slugs across legs: {slugs}')

    def test_yes_no_pair_token_ids_match_market(self):
        events = [{
            'title': 'Categorical event',
            'markets': [{'slug': 'mkt1', 'title': 'Outcome 1'}],
            'deadline': 9999999999, 'slug': 'parent',
        }]
        lim_res = {'mkt1': (0.40, 1000, 0.55, 1000)}
        deals = arb_server.eval_limitless(events, lim_res)
        c_deals = [d for d in deals if d.get('arb_structure') == 'yes_no_pair']
        if c_deals:
            entries = c_deals[0]['entries']
            yes_leg = next(e for e in entries if e['side'] == 'YES')
            no_leg  = next(e for e in entries if e['side'] == 'NO')
            # Same slug; YES uses yes_token, NO uses no_token of THAT slug
            self.assertEqual(yes_leg['slug'], no_leg['slug'])
            self.assertEqual(yes_leg['token_id'], '0xYES_mkt1')
            self.assertEqual(no_leg['token_id'],  '0xNO_mkt1')


class TestStructureToggles(unittest.TestCase):
    """Phase 9p — ENABLE_STRUCT_A/B/C env switches let the operator
    disable individual arb structures during paper-trading bring-up."""

    def setUp(self):
        self._orig_a = arb_server.ENABLE_STRUCT_A
        self._orig_b = arb_server.ENABLE_STRUCT_B
        self._orig_c = arb_server.ENABLE_STRUCT_C
        self._orig_fetch = arb_server._fetch_limitless_market_meta
        arb_server._fetch_limitless_market_meta = lambda slug: {
            'yes_token': '0x' + '1'*40, 'no_token': '0x' + '2'*40,
            'verifying_contract': '0x' + '3'*40, 'volume': 1000,
        }

    def tearDown(self):
        arb_server.ENABLE_STRUCT_A = self._orig_a
        arb_server.ENABLE_STRUCT_B = self._orig_b
        arb_server.ENABLE_STRUCT_C = self._orig_c
        arb_server._fetch_limitless_market_meta = self._orig_fetch

    def _category_event_with_arb(self):
        """Categorical 3-way event with sums that trigger A, B, and C."""
        events = [{
            'title': 'Football: Team Alpha vs Team Beta',
            'markets': [
                {'slug': 'a', 'title': 'Alpha wins'},
                {'slug': 'b', 'title': 'Draw'},
                {'slug': 'c', 'title': 'Beta wins'},
            ],
            'deadline': 9999999999, 'slug': 'parent',
        }]
        # sum_yes = 0.95 (A-arb), sum_no = 1.95 < 2*0.988 (B-arb),
        # YES+NO per market 0.30+0.65=0.95 (C-arb on each child)
        lim_res = {
            'a': (0.30, 1000, 0.65, 1000),
            'b': (0.32, 1000, 0.65, 1000),
            'c': (0.33, 1000, 0.65, 1000),
        }
        return events, lim_res

    def test_disable_b_drops_all_no_keeps_others(self):
        events, lim_res = self._category_event_with_arb()
        arb_server.ENABLE_STRUCT_A = True
        arb_server.ENABLE_STRUCT_B = False
        arb_server.ENABLE_STRUCT_C = True
        deals = arb_server.eval_limitless(events, lim_res)
        kinds = {d.get('arb_structure') for d in deals}
        self.assertNotIn('all_no', kinds,
                         "ALL_NO must be skipped when ENABLE_STRUCT_B=0")

    def test_disable_c_drops_pairs_keeps_a_and_b(self):
        events, lim_res = self._category_event_with_arb()
        arb_server.ENABLE_STRUCT_A = True
        arb_server.ENABLE_STRUCT_B = True
        arb_server.ENABLE_STRUCT_C = False
        deals = arb_server.eval_limitless(events, lim_res)
        kinds = {d.get('arb_structure') for d in deals}
        self.assertNotIn('yes_no_pair', kinds,
                         "YES_NO_PAIR must be skipped when ENABLE_STRUCT_C=0")

    def test_only_a_enabled(self):
        """Operator's exact request — keep only structure A during paper
        trading. B and C must produce zero deals on this event."""
        events, lim_res = self._category_event_with_arb()
        arb_server.ENABLE_STRUCT_A = True
        arb_server.ENABLE_STRUCT_B = False
        arb_server.ENABLE_STRUCT_C = False
        deals = arb_server.eval_limitless(events, lim_res)
        kinds = {d.get('arb_structure') for d in deals}
        self.assertNotIn('all_no', kinds)
        self.assertNotIn('yes_no_pair', kinds)
        # 'binary' is the standalone-market variant of C — also off
        self.assertNotIn('binary', kinds)


class TestNoCORSWildcard(unittest.TestCase):
    """Phase 9p — global Access-Control-Allow-Origin: * was removed.
    Combined with same-origin frontend, wildcard CORS would let any third
    party in the user's browser POST to /api/kill via cached basic-auth."""
    def test_after_request_does_not_inject_cors_wildcard(self):
        from flask import Flask
        # Re-importing arb_server triggered side effects already; instead
        # poke its module to be sure no after_request handler injects
        # the wildcard header.
        funcs = arb_server.app.after_request_funcs.get(None, [])
        for f in funcs:
            # Build a synthetic response and run the handler
            from flask import Response
            r = Response("ok")
            f(r)
            self.assertNotEqual(
                r.headers.get('Access-Control-Allow-Origin'), '*',
                f'after_request handler {f.__name__!r} still emits '
                f'wildcard CORS — security risk')


if __name__ == '__main__':
    unittest.main(verbosity=2)
