"""Regression: collect_poly_tokens must stay defined + reachable.

Root cause guarded here: audit-28b cont 8 (#256) extracted classify_pools
into radar/eval/pools.py but dropped the adjacent helper collect_poly_tokens
without relocating it. The stale call site in arb_server.scan_loop
(`tokens = collect_poly_tokens(...)`, inside `if ws_client is not None:`)
became a dangling NameError that fired every prod tick — Polymarket WS subs
stuck at 0/N, empty poly pools, 0 Polymarket arbs since 29.05.2026.

These tests lock in:
  1. The function is importable from its home module (radar.eval.pools).
  2. It is re-exported into arb_server's namespace, so the bare-name call
     site resolves (this is the exact thing that NameError'd in prod).
  3. The flatten order + .get()-skip behaviour is preserved.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'Scripts'))

import arb_server
from radar.eval.pools import collect_poly_tokens


class TestCollectPolyTokensReachable(unittest.TestCase):
    """The dangling-NameError guard: the name must resolve where it's used."""

    def test_importable_from_pools_module(self):
        self.assertTrue(callable(collect_poly_tokens))

    def test_reexported_into_arb_server_namespace(self):
        # arb_server.scan_loop calls collect_poly_tokens by bare name; that
        # only resolves if it lives in arb_server's module globals via the
        # `from radar.eval.pools import (...)` block. This assertion is the
        # actual prod-regression guard.
        self.assertTrue(hasattr(arb_server, 'collect_poly_tokens'))
        self.assertIs(arb_server.collect_poly_tokens, collect_poly_tokens)


class TestCollectPolyTokensBehaviour(unittest.TestCase):
    """Flatten order + defensive .get() skipping."""

    def test_order_hot_yes_no_then_near_yes_no(self):
        poly_pool = {
            'hot': [
                ('ev_hot', [{'token_id_yes': 'HY1', 'token_id_no': 'HN1'}], None),
            ],
            'near': [
                ('ev_near', [
                    {'token_id_yes': 'NY1', 'token_id_no': 'NN1'},
                    {'token_id_yes': 'NY2'},  # no NO token → skipped on no_near
                ], None),
            ],
        }
        # Expected: HOT YES, HOT NO, NEAR YES (both), NEAR NO (only the one).
        self.assertEqual(
            collect_poly_tokens(poly_pool),
            ['HY1', 'HN1', 'NY1', 'NY2', 'NN1'],
        )

    def test_empty_pool_returns_empty_list(self):
        self.assertEqual(collect_poly_tokens({'hot': [], 'near': []}), [])

    def test_outcome_without_tokens_is_skipped(self):
        poly_pool = {
            'hot': [('ev', [{'some_other_field': 1}], None)],
            'near': [],
        }
        self.assertEqual(collect_poly_tokens(poly_pool), [])


if __name__ == '__main__':
    unittest.main()
