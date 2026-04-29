"""Phase 9y — top-of-book depth + child-name in C-structure NEAR rows.

User screenshot showed `min_liq=$7,659,000` for a Limitless market that
in reality had ~$50 of top-of-book liquidity. Cause: depth was `sum of
price*size across ALL orderbook levels`, not the best ask only.
build_deal then sized legs against fake-deep liquidity, would slip
catastrophically on fill.

Also: structure-C rows in NEAR showed only the parent event title,
making it impossible to copy a child market name to search on Limitless.
"""
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'Scripts'))

import arb_server


class TestTopOfBookDepth(unittest.TestCase):
    """_fetch_limitless_orderbook now reports depth at best price only."""

    def setUp(self):
        # Bypass WS cache by patching the module-level lim_ws_client
        self._orig_ws = arb_server.lim_ws_client
        arb_server.lim_ws_client = None

    def tearDown(self):
        arb_server.lim_ws_client = self._orig_ws

    def _mock_ob(self, asks, bids):
        fake = mock.Mock()
        fake.status_code = 200
        fake.json.return_value = {'asks': asks, 'bids': bids}
        return fake

    def test_depth_is_top_of_book_not_sum_of_levels(self):
        # 5 ask levels — old code summed all 5*price*size = $1500 phantom;
        # new code reports only best ask = price 0.40 × size 100 = $40.
        asks = [
            {'price': 0.40, 'size': 100},
            {'price': 0.45, 'size': 200},
            {'price': 0.50, 'size': 300},
            {'price': 0.55, 'size': 200},
            {'price': 0.60, 'size': 100},
        ]
        bids = [{'price': 0.35, 'size': 50}]
        with mock.patch('arb_server.requests.get',
                        return_value=self._mock_ob(asks, bids)):
            slug, ya, dy, na, dn = arb_server._fetch_limitless_orderbook('s1')
        self.assertEqual(ya, 0.40, 'best ask should be 0.40')
        # depth_yes = 0.40 * 100 = $40 — NOT the $370 sum-of-levels number
        self.assertAlmostEqual(dy, 40.0, places=2,
                               msg=f'depth_yes must be top-of-book ($40), got {dy}')

    def test_huge_orderbook_does_not_inflate_depth(self):
        # 1000 small orders that would have summed to $50000 fake depth
        asks = [{'price': 0.40 + i*0.0001, 'size': 5} for i in range(1000)]
        with mock.patch('arb_server.requests.get',
                        return_value=self._mock_ob(asks, [])):
            slug, ya, dy, na, dn = arb_server._fetch_limitless_orderbook('s1')
        # Only top-of-book counts → 0.40 * 5 = $2
        self.assertAlmostEqual(dy, 2.0, places=2,
                               msg=f'large orderbook must not inflate depth, got {dy}')

    def test_no_depth_uses_best_yes_bid_top(self):
        asks = [{'price': 0.40, 'size': 100}]
        bids = [
            {'price': 0.35, 'size': 50},
            {'price': 0.30, 'size': 200},
            {'price': 0.25, 'size': 500},
        ]
        with mock.patch('arb_server.requests.get',
                        return_value=self._mock_ob(asks, bids)):
            slug, ya, dy, na, dn = arb_server._fetch_limitless_orderbook('s1')
        # NO synthesised from best YES bid 0.35 → no_ask = 0.65
        self.assertAlmostEqual(na, 0.65, places=2)
        # Depth from best bid only: 0.35 * 50 = $17.5
        self.assertAlmostEqual(dn, 17.5, places=2,
                               msg=f'no-depth must be top-of-book bid, got {dn}')


class TestCStructureChildNameInNear(unittest.TestCase):
    """Structure-C rows now include the child market name in title."""

    def test_best_near_structure_c_carries_market_name(self):
        pm = [
            {'name': 'Both teams to score?', 'yes_price': 0.49, 'yes_liq': 100,
             'no_price': 0.50, 'no_liq': 100},
        ]
        best = arb_server._best_near_structure(pm, threshold=0.99)
        self.assertIsNotNone(best)
        self.assertEqual(best['structure'], 'yes_no_pair')
        self.assertEqual(best.get('market_name'), 'Both teams to score?')

    def test_a_structure_does_not_carry_market_name(self):
        # Multi-outcome event — A structure picked. No market_name needed
        # (parent title already identifies the event).
        pm = [
            {'name': 'X1', 'yes_price': 0.30, 'yes_liq': 100,
             'no_price': 0.70, 'no_liq': 100},
            {'name': 'X2', 'yes_price': 0.32, 'yes_liq': 100,
             'no_price': 0.68, 'no_liq': 100},
            {'name': 'X3', 'yes_price': 0.33, 'yes_liq': 100,
             'no_price': 0.67, 'no_liq': 100},
        ]
        best = arb_server._best_near_structure(pm, threshold=0.99)
        self.assertIsNotNone(best)
        # ALL_YES = 0.95 → arb. No market_name on A.
        if best['structure'] == 'all_yes':
            self.assertNotIn('market_name', best)


class TestAnyVolumeZeroExclusion(unittest.TestCase):
    """Phase 9z — exclude event from NEAR if ANY child has volume=0.
    Volume = lifetime traded notional from /markets/{slug}.volume,
    cached via _fetch_limitless_market_meta. More reliable than
    depth/orderbook because Limitless returns indicative prices on
    stale markets even when no one has actually traded."""

    def setUp(self):
        self._meta = {}
        self._orig = arb_server._fetch_limitless_market_meta
        arb_server._fetch_limitless_market_meta = lambda slug: self._meta.get(slug, {})

    def tearDown(self):
        arb_server._fetch_limitless_market_meta = self._orig

    def test_one_dead_leg_in_2way_blocks_a_b_keeps_c_on_alive(self):
        # 2-way event with G2 dead, Astralis alive.
        # A (ALL_YES with N=2) needs both alive → blocked.
        # B (ALL_NO N>=3) doesn't apply for N=2 anyway.
        # C on Astralis (yes+no=1.05) still works — sum returned.
        ev = {
            'title': 'Astralis vs G2',
            'markets': [
                {'slug': 'astralis'}, {'slug': 'g2'},
            ],
        }
        lim_res = {
            'astralis': (0.50, 100, 0.55, 100),
            'g2':       (0.55, 100, 0.49, 100),
        }
        self._meta = {'astralis': {'volume': 118}, 'g2': {'volume': 0}}
        s = arb_server._sum_limitless_cand(ev, lim_res)
        # Should return Astralis C-pair sum = 1.05
        self.assertIsNotNone(s,
            "C on alive leg must still classify even when sibling leg dead")
        self.assertAlmostEqual(s, 1.05, places=2)

    def test_all_legs_dead_returns_none(self):
        ev = {
            'title': 'Dead event',
            'markets': [
                {'slug': 'a'}, {'slug': 'b'},
            ],
        }
        lim_res = {
            'a': (0.50, 100, 0.55, 100),
            'b': (0.55, 100, 0.49, 100),
        }
        self._meta = {'a': {'volume': 0}, 'b': {'volume': 0}}
        s = arb_server._sum_limitless_cand(ev, lim_res)
        self.assertIsNone(s,
            "no leg with volume → no candidate at all")

    def test_all_legs_with_volume_passes(self):
        ev = {
            'title': 'Astralis vs G2',
            'markets': [{'slug': 'a'}, {'slug': 'b'}],
        }
        lim_res = {
            'a': (0.45, 100, 0.50, 100),
            'b': (0.50, 100, 0.45, 100),
        }
        self._meta = {'a': {'volume': 200}, 'b': {'volume': 50}}
        s = arb_server._sum_limitless_cand(ev, lim_res)
        self.assertIsNotNone(s, "all-volume-positive must classify normally")

    def test_dead_leg_in_multi_outcome_blocks_a_and_b_only(self):
        # Multi-outcome with one dead leg: A and B blocked (need all alive),
        # C still works on the 3 alive legs → sum is min C-pair.
        ev = {
            'title': 'US GDP growth in Q1 2026?',
            'markets': [{'slug': 's1'}, {'slug': 's2'},
                        {'slug': 's3'}, {'slug': 's4'}],
        }
        lim_res = {
            's1': (0.30, 100, 0.65, 100),
            's2': (0.32, 100, 0.65, 100),
            's3': (0.33, 100, 0.65, 100),
            's4': (0.99, 100, 0.01, 100),
        }
        self._meta = {
            's1': {'volume': 100}, 's2': {'volume': 100},
            's3': {'volume': 100}, 's4': {'volume': 0},
        }
        s = arb_server._sum_limitless_cand(ev, lim_res)
        # min C-pair across alive (s1, s2, s3) = 0.30+0.65 = 0.95
        self.assertIsNotNone(s,
            "C on alive legs must still surface when a sibling is dead")
        self.assertAlmostEqual(s, 0.95, places=2)

    def test_re_enters_when_volume_returns_for_a(self):
        # Single-leg-dead doesn't block C — but ALL_YES is blocked. When
        # volume on the dead leg appears, ALL_YES sum becomes available.
        ev = {'title': 'X', 'markets': [{'slug': 'a'}, {'slug': 'b'}]}
        lim_res = {
            'a': (0.45, 100, 0.50, 100),
            'b': (0.50, 100, 0.45, 100),
        }
        self._meta = {'a': {'volume': 200}, 'b': {'volume': 0}}
        # Only C contributes (b dead) → min C = a's pair = 0.95
        s_dead = arb_server._sum_limitless_cand(ev, lim_res)
        self.assertAlmostEqual(s_dead, 0.95, places=2)
        # All alive → A also contributes 0.45+0.50 = 0.95 (same sum)
        # But min over A and C still = 0.95
        self._meta = {'a': {'volume': 200}, 'b': {'volume': 50}}
        s_alive = arb_server._sum_limitless_cand(ev, lim_res)
        self.assertIsNotNone(s_alive)


if __name__ == '__main__':
    unittest.main(verbosity=2)
