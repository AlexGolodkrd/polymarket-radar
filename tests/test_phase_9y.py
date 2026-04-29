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


class TestAnyDepthZeroExclusion(unittest.TestCase):
    """Phase 9z — exclude event from NEAR if ANY child has zero orderbook
    depth on both yes and no sides. Replaces Phase 9v's all-dead rule.
    Depth-based (current orders) not volume-based (history) because
    arbitrage cares about whether we can actually trade right now."""

    def test_one_dead_leg_kills_pool_classification(self):
        # G2 vs Astralis-style: 2 outcomes, G2 has empty orderbook
        ev = {
            'title': 'Astralis vs G2',
            'markets': [
                {'slug': 'astralis', 'title': 'Astralis'},
                {'slug': 'g2', 'title': 'G2'},
            ],
        }
        # tuple = (yes_ask, yes_depth, no_ask, no_depth)
        lim_res = {
            'astralis': (0.50, 100, 0.55, 100),
            'g2':       (0.55, 0,   0.49, 0),    # both sides empty
        }
        s = arb_server._sum_limitless_cand(ev, lim_res)
        self.assertIsNone(s,
            "_sum_limitless_cand must return None when any leg has both "
            "yes_depth and no_depth = 0")

    def test_one_side_alive_passes(self):
        # If yes side has depth even when no doesn't, leg is alive
        ev = {
            'title': 'Astralis vs G2',
            'markets': [
                {'slug': 'a', 'title': 'A'},
                {'slug': 'b', 'title': 'B'},
            ],
        }
        lim_res = {
            'a': (0.45, 100, 0.50, 100),
            'b': (0.50, 50,  0.45, 0),  # only YES side has depth — still alive
        }
        s = arb_server._sum_limitless_cand(ev, lim_res)
        self.assertIsNotNone(s,
            "having depth on at least ONE side keeps leg alive")

    def test_dead_leg_in_multi_outcome_drops_event(self):
        # 4-outcome event, 1 leg dead — US-GDP-growth-style
        ev = {
            'title': 'US GDP growth in Q1 2026?',
            'markets': [
                {'slug': 's1'}, {'slug': 's2'}, {'slug': 's3'}, {'slug': 's4'},
            ],
        }
        lim_res = {
            's1': (0.30, 100, 0.65, 100),
            's2': (0.32, 100, 0.65, 100),
            's3': (0.33, 100, 0.65, 100),
            's4': (0.99, 0,   0.01, 0),  # dead leg
        }
        s = arb_server._sum_limitless_cand(ev, lim_res)
        self.assertIsNone(s,
            "any-depth-zero must drop multi-outcome event too")

    def test_re_enters_when_depth_returns(self):
        ev = {
            'title': 'Astralis vs G2',
            'markets': [
                {'slug': 'a'}, {'slug': 'b'},
            ],
        }
        # First scan: B has zero depth on both sides
        lim_res_dead = {
            'a': (0.45, 100, 0.50, 100),
            'b': (0.50, 0,   0.45, 0),
        }
        s1 = arb_server._sum_limitless_cand(ev, lim_res_dead)
        self.assertIsNone(s1)
        # Second scan: B got orderbook
        lim_res_alive = {
            'a': (0.45, 100, 0.50, 100),
            'b': (0.50, 50,  0.45, 50),
        }
        s2 = arb_server._sum_limitless_cand(ev, lim_res_alive)
        self.assertIsNotNone(s2,
            "event must re-enter pool once any depth appears")


if __name__ == '__main__':
    unittest.main(verbosity=2)
