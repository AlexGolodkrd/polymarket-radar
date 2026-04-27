"""Unit tests for the Phase 7 SX Bet executor (PR #18).

Covers fetch_sx_matchable_orders + match_sx_orders + build_sx_order +
the partial-fill propagation through atomic.fire_arb.
"""
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'Scripts'))

from executor import builders
from executor.builders import (
    fetch_sx_matchable_orders, match_sx_orders, build_sx_order,
    _opposite_side_filter, WalletStub,
)
from executor.atomic import fire_arb


# ── Helpers ─────────────────────────────────────────────────────────
def _maker_order(*, on_outcome_one: bool, pct: float, fillable_usdc: float,
                 order_hash: str = '0xabc'):
    """Build a minimal /orders response shape."""
    return {
        'orderHash': order_hash,
        'isMakerBettingOutcomeOne': on_outcome_one,
        'percentageOdds': str(int(round(pct * 1e20))),
        'orderSizeFillable': str(int(round(fillable_usdc * 1e6))),
        'salt': 'test',
        'expiry': '9999999999',
    }


def _orders_response(orders):
    return {'status': 'success', 'data': {'orders': orders}}


def _wallet():
    return WalletStub(bot_id='bot1', eth_address='0x' + 'a' * 40)


# ── Side filter ─────────────────────────────────────────────────────
class TestOppositeSideFilter(unittest.TestCase):
    def test_taker_one_needs_maker_two(self):
        self.assertTrue(_opposite_side_filter(taker_outcome=1, is_maker_one=False))
        self.assertFalse(_opposite_side_filter(taker_outcome=1, is_maker_one=True))

    def test_taker_two_needs_maker_one(self):
        self.assertTrue(_opposite_side_filter(taker_outcome=2, is_maker_one=True))
        self.assertFalse(_opposite_side_filter(taker_outcome=2, is_maker_one=False))


# ── fetch_sx_matchable_orders ──────────────────────────────────────
class TestFetchMatchable(unittest.TestCase):
    def test_filters_to_opposite_side(self):
        # Taker on outcome 1, only orders with isMakerBettingOutcomeOne=False match
        same_side = _maker_order(on_outcome_one=True, pct=0.55, fillable_usdc=100)
        opp_side = _maker_order(on_outcome_one=False, pct=0.40, fillable_usdc=50,
                                order_hash='0x1')
        fetcher = lambda: _orders_response([same_side, opp_side])
        out = fetch_sx_matchable_orders('0xmh', taker_outcome=1, fetcher=fetcher)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['order_hash'], '0x1')
        # taker_price for opp side: 1 - 0.40 = 0.60
        self.assertAlmostEqual(out[0]['taker_price'], 0.60, places=6)
        self.assertAlmostEqual(out[0]['fillable_usdc'], 50.0, places=6)

    def test_skips_invalid_pct(self):
        bad = _maker_order(on_outcome_one=False, pct=0, fillable_usdc=10)
        fetcher = lambda: _orders_response([bad])
        self.assertEqual(fetch_sx_matchable_orders('0xmh', 1, fetcher=fetcher), [])

    def test_skips_zero_size(self):
        bad = _maker_order(on_outcome_one=False, pct=0.5, fillable_usdc=0)
        fetcher = lambda: _orders_response([bad])
        self.assertEqual(fetch_sx_matchable_orders('0xmh', 1, fetcher=fetcher), [])

    def test_handles_api_error(self):
        fetcher = lambda: {'status': 'error'}
        self.assertEqual(fetch_sx_matchable_orders('0xmh', 1, fetcher=fetcher), [])

    def test_handles_fetcher_exception(self):
        def boom(): raise RuntimeError('network')
        self.assertEqual(fetch_sx_matchable_orders('0xmh', 1, fetcher=boom), [])


# ── match_sx_orders ─────────────────────────────────────────────────
class TestMatchOrders(unittest.TestCase):
    def test_full_fill_one_order(self):
        matchable = [
            {'order_hash': '0x1', 'taker_price': 0.45,
             'fillable_usdc': 100.0, 'maker_pct': 0.55, 'raw_order': {}},
        ]
        m = match_sx_orders(matchable, target_size_usdc=50.0, max_taker_price=0.50)
        self.assertEqual(len(m['matched']), 1)
        self.assertAlmostEqual(m['filled_usdc'], 50.0)
        self.assertFalse(m['partial'])
        self.assertEqual(m['shortfall_usdc'], 0.0)
        self.assertAlmostEqual(m['avg_price'], 0.45)

    def test_full_fill_multiple_orders_sorted_by_price(self):
        # Worst price first in input — algorithm should sort and pick best first
        matchable = [
            {'order_hash': '0x_worst', 'taker_price': 0.48,
             'fillable_usdc': 100.0, 'maker_pct': 0.52, 'raw_order': {}},
            {'order_hash': '0x_best', 'taker_price': 0.42,
             'fillable_usdc': 30.0, 'maker_pct': 0.58, 'raw_order': {}},
            {'order_hash': '0x_mid', 'taker_price': 0.45,
             'fillable_usdc': 30.0, 'maker_pct': 0.55, 'raw_order': {}},
        ]
        m = match_sx_orders(matchable, target_size_usdc=50.0, max_taker_price=0.50)
        # Best ($30 @ 0.42) + mid ($20 @ 0.45) = $50
        self.assertEqual(len(m['matched']), 2)
        self.assertEqual(m['matched'][0]['order_hash'], '0x_best')
        self.assertEqual(m['matched'][1]['order_hash'], '0x_mid')
        self.assertAlmostEqual(m['filled_usdc'], 50.0)
        self.assertAlmostEqual(m['matched'][1]['taker_amount_usdc'], 20.0)

    def test_partial_fill_when_capacity_short(self):
        matchable = [
            {'order_hash': '0x1', 'taker_price': 0.45,
             'fillable_usdc': 30.0, 'maker_pct': 0.55, 'raw_order': {}},
        ]
        m = match_sx_orders(matchable, target_size_usdc=50.0, max_taker_price=0.50)
        self.assertTrue(m['partial'])
        self.assertAlmostEqual(m['shortfall_usdc'], 20.0)
        self.assertAlmostEqual(m['filled_usdc'], 30.0)

    def test_slippage_cap_stops_matching(self):
        matchable = [
            {'order_hash': '0x_good', 'taker_price': 0.45,
             'fillable_usdc': 20.0, 'maker_pct': 0.55, 'raw_order': {}},
            {'order_hash': '0x_too_expensive', 'taker_price': 0.55,
             'fillable_usdc': 100.0, 'maker_pct': 0.45, 'raw_order': {}},
        ]
        m = match_sx_orders(matchable, target_size_usdc=50.0, max_taker_price=0.50)
        # Only the $20 good order matches — the second is over the cap
        self.assertEqual(len(m['matched']), 1)
        self.assertTrue(m['partial'])
        self.assertEqual(m['matched'][0]['order_hash'], '0x_good')

    def test_empty_matchable(self):
        m = match_sx_orders([], target_size_usdc=50.0, max_taker_price=0.50)
        self.assertEqual(m['matched'], [])
        self.assertTrue(m['partial'])
        self.assertEqual(m['shortfall_usdc'], 50.0)
        self.assertIsNone(m['avg_price'])


# ── build_sx_order ──────────────────────────────────────────────────
class TestBuildSxOrder(unittest.TestCase):
    def test_full_match_returns_complete_body(self):
        orders = [
            _maker_order(on_outcome_one=True, pct=0.58, fillable_usdc=100,
                         order_hash='0xA'),
        ]
        fetcher = lambda: _orders_response(orders)
        out = build_sx_order('0xmh', outcome=2, taker_price=0.45, size_usdc=20,
                             wallet=_wallet(), fetcher=fetcher)
        self.assertEqual(out['platform'], 'sx_bet')
        self.assertFalse(out['partial_fill'])
        self.assertEqual(out['body']['orderHashes'], ['0xA'])
        self.assertEqual(len(out['body']['takerAmounts']), 1)
        # 20 USDC * 1e6 = 20_000_000
        self.assertEqual(out['body']['takerAmounts'][0], '20000000')
        self.assertEqual(out['body']['takerOutcome'], 2)
        # Match block
        self.assertEqual(out['sx_match']['matched_orders'], 1)
        self.assertEqual(out['sx_match']['available_orders'], 1)
        self.assertAlmostEqual(out['sx_match']['avg_fill_price'], 0.42, places=2)

    def test_partial_fill_flag_set(self):
        orders = [
            _maker_order(on_outcome_one=True, pct=0.58, fillable_usdc=10,
                         order_hash='0xA'),
        ]
        fetcher = lambda: _orders_response(orders)
        out = build_sx_order('0xmh', outcome=2, taker_price=0.45, size_usdc=50,
                             wallet=_wallet(), fetcher=fetcher)
        self.assertTrue(out['partial_fill'])
        self.assertGreater(out['sx_match']['shortfall_usdc'], 0)

    def test_no_matching_orders_means_partial_with_zero_filled(self):
        # All orders on same side as taker — none fillable
        orders = [
            _maker_order(on_outcome_one=False, pct=0.60, fillable_usdc=100),
        ]
        fetcher = lambda: _orders_response(orders)
        out = build_sx_order('0xmh', outcome=2, taker_price=0.45, size_usdc=20,
                             wallet=_wallet(), fetcher=fetcher)
        self.assertTrue(out['partial_fill'])
        self.assertEqual(out['body']['orderHashes'], [])
        self.assertEqual(out['sx_match']['filled_usdc'], 0.0)

    def test_assertions_validate_input(self):
        with self.assertRaises(AssertionError):
            build_sx_order('0xmh', outcome=3, taker_price=0.5, size_usdc=10,
                           wallet=_wallet(), fetcher=lambda: _orders_response([]))
        with self.assertRaises(AssertionError):
            build_sx_order('0xmh', outcome=1, taker_price=0, size_usdc=10,
                           wallet=_wallet(), fetcher=lambda: _orders_response([]))
        with self.assertRaises(AssertionError):
            build_sx_order('0xmh', outcome=1, taker_price=0.5, size_usdc=0.5,
                           wallet=_wallet(), fetcher=lambda: _orders_response([]))


# ── End-to-end via fire_arb ─────────────────────────────────────────
class TestFireArbWithSxPartial(unittest.TestCase):
    """Ensure that an SX Bet partial-fill propagates through fire_arb and
    aborts the arb (partial leg = no longer arb)."""

    def setUp(self):
        # Patch the network fetch in builders so the test is hermetic.
        # We simulate one leg fully filling, the other partial.
        self._patches = []
        # Patch dryrun_log paths to a temp dir
        from executor import dryrun_log
        from risk import state as _risk_state, killswitch as _risk_ks
        import tempfile
        self._tmpdir = tempfile.mkdtemp()
        for mod, attr, val in [
            (dryrun_log, 'EXECUTIONS_DIR', self._tmpdir),
            (dryrun_log, 'DRYRUN_LOG_PATH', os.path.join(self._tmpdir, 'dryrun.jsonl')),
            (dryrun_log, 'PAPER_RESULTS_PATH', os.path.join(self._tmpdir, 'paper.jsonl')),
            (_risk_state, 'EXECUTIONS_DIR', self._tmpdir),
            (_risk_state, 'STATE_PATH', os.path.join(self._tmpdir, 'risk_state.json')),
            (_risk_ks, 'EXECUTIONS_DIR', self._tmpdir),
            (_risk_ks, 'KILL_FLAG_PATH', os.path.join(self._tmpdir, '.killed')),
            (_risk_ks, 'KILL_LOG_PATH', os.path.join(self._tmpdir, 'killswitch.jsonl')),
        ]:
            p = mock.patch.object(mod, attr, val); p.start(); self._patches.append(p)
        # Disable schedule_realistic_eval (would spawn threads)
        p = mock.patch.object(dryrun_log, 'schedule_realistic_eval', lambda *a, **k: None)
        p.start(); self._patches.append(p)
        _risk_state.reset_for_test()

    def tearDown(self):
        for p in self._patches: p.stop()
        from risk import state as _risk_state
        _risk_state.reset_for_test()
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _sx_deal(self):
        return {
            'title': 'NBA TeamA vs TeamB',
            'platform': 'SX Bet',
            'arb_structure': 'binary',
            'total_cents': 95.0, 'spread_cents': 0.5,
            'min_liq': 1000, 'slip_pct': 0.2,
            'market_hash': '0xmh',
            'entries': [
                {'name': 'A', 'price': 0.48, 'stake': 10.0, 'contracts': 20.0,
                 'outcome_index': 1, 'source': 'sx_ob', 'liquidity': 1000,
                 'fee': 0.4, 'coeff': 1/0.48, 'share_pct': 50},
                {'name': 'B', 'price': 0.47, 'stake': 10.0, 'contracts': 20.0,
                 'outcome_index': 2, 'source': 'sx_ob', 'liquidity': 1000,
                 'fee': 0.4, 'coeff': 1/0.47, 'share_pct': 50},
            ],
        }

    def test_partial_leg_aborts_arb(self):
        # Leg 1 (outcome=1) needs maker on outcome 2 with enough capacity
        # Leg 2 (outcome=2) needs maker on outcome 1 — we make it short
        def _fetcher_factory():
            def fetcher():
                # build_sx_order calls this for both legs since both use 0xmh
                return _orders_response([
                    # Plenty for leg 1 (taker outcome=1 needs maker_one=False)
                    _maker_order(on_outcome_one=False, pct=0.52,
                                 fillable_usdc=100, order_hash='0x_for_leg1'),
                    # Tiny capacity for leg 2 (taker outcome=2 needs maker_one=True)
                    _maker_order(on_outcome_one=True, pct=0.53,
                                 fillable_usdc=2, order_hash='0x_for_leg2'),
                ])
            return fetcher
        with mock.patch.object(builders, 'fetch_sx_matchable_orders',
                               side_effect=lambda mh, outcome, fetcher=None:
                                   builders.fetch_sx_matchable_orders.__wrapped__(mh, outcome, fetcher=_fetcher_factory())
                                   if hasattr(builders.fetch_sx_matchable_orders, '__wrapped__')
                                   else None):
            pass  # We'll patch directly instead
        # Cleaner approach: monkey-patch the module-level requests inside builders.
        # But we have a `fetcher=` parameter — feed it through atomic._build_leg by
        # patching the builder we expose to the rest of the executor.
        original = builders.build_sx_order
        captured_fetcher = _fetcher_factory()
        def patched_build(market_hash, outcome, taker_price, size_usdc, wallet,
                          expiration_secs=60, slippage_tolerance=0.005, fetcher=None):
            return original(market_hash, outcome, taker_price, size_usdc, wallet,
                            expiration_secs=expiration_secs,
                            slippage_tolerance=slippage_tolerance,
                            fetcher=captured_fetcher)
        with mock.patch.object(builders, 'build_sx_order', side_effect=patched_build):
            result = fire_arb(self._sx_deal(), wallets=[], dry_run=True)

        # Both legs were attempted
        self.assertEqual(len(result.legs), 2)
        # Leg 2 should be partial
        partials = [l for l in result.legs if l.status == 'partial']
        self.assertEqual(len(partials), 1)
        self.assertEqual(partials[0].leg_idx, 1)
        # Arb is aborted
        self.assertIsNotNone(result.aborted_reason)
        self.assertIn('partial_fill_arb_broken', result.aborted_reason)


if __name__ == '__main__':
    unittest.main(verbosity=2)
