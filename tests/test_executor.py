"""Unit tests for the Phase 2 executor package.

Run from repo root:
    python -m pytest tests/test_executor.py -v

Or stand-alone (no pytest):
    python tests/test_executor.py
"""
import json
import os
import sys
import time
import unittest
from unittest import mock

# Make Scripts/ importable
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'Scripts'))

from executor.builders import (
    build_poly_order, build_sx_order, build_kalshi_order, WalletStub,
)
from executor.atomic import fire_arb, ArbFireResult, _assign_wallets
from executor import dryrun_log
# Phase 3 risk modules — fire_arb now calls risk.check_can_fire so tests
# need to isolate the risk state so a stale .killed flag or persisted
# daily-loss counter from a previous run doesn't fail these tests.
from risk import state as _risk_state
from risk import killswitch as _risk_killswitch


# ── Fixtures ────────────────────────────────────────────────────────
def _wallet(bot_id='bot1'):
    return WalletStub(bot_id=bot_id, eth_address='0x' + 'a' * 40)

def _three_wallet_pool():
    return [_wallet(f'bot{i}') for i in range(1, 4)]

def _poly_deal(arb_structure='all_yes', n_legs=3, stake_per_leg=10.0):
    """Synthetic Polymarket deal that mimics what arb_server.build_deal produces.
    Default total stake = 3 × $10 = $30 — passes both Phase 3 gates:
        - $30 ≤ $55 per-trade cap
        - worst-case daily loss check: 0 - 30 = -30 ≥ -$35 daily limit

    Phase audit-28b cont (27.05.2026): added `net` (above $0.50 mosquito
    guard introduced in Phase 19v6) so the deal isn't rejected before
    leg dispatch happens.
    """
    return {
        'title': 'Test Event Winner',
        'platform': 'Polymarket',
        'arb_structure': arb_structure,
        'total_cents': 95.0,
        'spread_cents': 0.5,
        'min_liq': 5000,
        'slip_pct': 0.1,
        'net': 5.0,
        'entries': [
            {'name': f'Cand {i}', 'price': 0.30 + 0.05*i, 'stake': stake_per_leg,
             'contracts': 100.0, 'token_id': f'tok_{i}', 'token_id_yes': f'tok_{i}',
             'source': 'clob_ask', 'liquidity': 5000, 'fee': 0.5,
             'coeff': 1/(0.30+0.05*i), 'share_pct': 30}
            for i in range(n_legs)
        ],
    }

def _sx_deal():
    """Total stake $32 → passes per-trade cap and pre-trade daily check.

    Phase audit-28b cont (27.05.2026): `net` added so the deal clears
    the $0.50 mosquito guard before leg dispatch.
    """
    return {
        'title': 'TeamA vs TeamB (NBA)',
        'platform': 'SX Bet',
        'arb_structure': 'binary',
        'total_cents': 95.0, 'spread_cents': 0.5, 'min_liq': 1000, 'slip_pct': 0.2,
        'net': 5.0,
        'market_hash': '0xdead',
        'entries': [
            {'name': 'TeamA', 'price': 0.48, 'stake': 16.0, 'contracts': 33.0,
             'outcome_index': 1, 'source': 'sx_ob', 'liquidity': 1000, 'fee': 0.4,
             'coeff': 1/0.48, 'share_pct': 50},
            {'name': 'TeamB', 'price': 0.47, 'stake': 16.0, 'contracts': 33.0,
             'outcome_index': 2, 'source': 'sx_ob', 'liquidity': 1000, 'fee': 0.4,
             'coeff': 1/0.47, 'share_pct': 50},
        ],
    }


# ── Builders ────────────────────────────────────────────────────────
class TestPolyBuilder(unittest.TestCase):
    def test_basic_buy_order(self):
        o = build_poly_order('123', 'BUY', 0.45, 10.0, _wallet())
        self.assertEqual(o['platform'], 'polymarket')
        self.assertEqual(o['expected_price'], 0.45)
        self.assertEqual(o['expected_size_usdc'], 10.0)
        # V2 shape: body wraps order. Check the unsigned `order` accessor.
        order = o['order']
        self.assertEqual(order['tokenId'], '123')
        self.assertEqual(order['side'], '0')          # BUY
        self.assertEqual(order['signatureType'], '0')
        # makerAmount should be 10 * 1e6 = 10000000 USDC wei
        self.assertEqual(order['makerAmount'], '10000000')
        # takerAmount = contracts * 1e6 ~= (10/0.45) * 1e6 = 22222222
        self.assertEqual(order['takerAmount'], '22222222')
        # V2 fields present
        self.assertIn('timestamp', order)
        self.assertIn('metadata', order)
        self.assertIn('builder', order)

    def test_rejects_invalid_price(self):
        with self.assertRaises(AssertionError):
            build_poly_order('1', 'BUY', 0, 10.0, _wallet())
        with self.assertRaises(AssertionError):
            build_poly_order('1', 'BUY', 1.0, 10.0, _wallet())

    def test_rejects_below_min_size(self):
        with self.assertRaises(AssertionError):
            build_poly_order('1', 'BUY', 0.5, 0.5, _wallet())

    def test_timestamp_is_recent_ms(self):
        """V2 dropped `expiration` and `nonce`; uses ms timestamp instead."""
        o = build_poly_order('1', 'BUY', 0.5, 10.0, _wallet())
        # Should be a recent unix-ms timestamp
        ts = int(o['order']['timestamp'])
        now_ms = int(time.time() * 1000)
        self.assertLess(abs(ts - now_ms), 5000)   # within 5 seconds


class TestSxBuilder(unittest.TestCase):
    """Phase 7: build_sx_order now matches against live /orders. Tests pass
    a synthetic fetcher so no network is hit; partial-fill behaviour is
    covered in tests/test_sx_executor.py."""

    @staticmethod
    def _empty_fetcher():
        return {'status': 'success', 'data': {'orders': []}}

    def test_basic_no_orders_means_partial(self):
        o = build_sx_order('0xmh', 1, 0.45, 10.0, _wallet(),
                           fetcher=self._empty_fetcher)
        self.assertEqual(o['platform'], 'sx_bet')
        self.assertEqual(o['body']['marketHash'], '0xmh')
        self.assertEqual(o['body']['takerOutcome'], 1)
        # Empty book → partial fill, no matched orders
        self.assertTrue(o['partial_fill'])
        self.assertEqual(o['body']['orderHashes'], [])

    def test_rejects_bad_outcome(self):
        with self.assertRaises(AssertionError):
            build_sx_order('0x', 0, 0.5, 10.0, _wallet(),
                           fetcher=self._empty_fetcher)
        with self.assertRaises(AssertionError):
            build_sx_order('0x', 3, 0.5, 10.0, _wallet(),
                           fetcher=self._empty_fetcher)


class TestKalshiBuilderDisabled(unittest.TestCase):
    def test_returns_disabled_marker(self):
        o = build_kalshi_order(price=0.5, size_usdc=10.0)
        self.assertEqual(o['platform'], 'kalshi')
        self.assertIsNone(o['body'])
        self.assertIn('disabled_reason', o)


# ── Wallet assignment ───────────────────────────────────────────────
class TestWalletAssignment(unittest.TestCase):
    def test_distinct_wallets_only(self):
        """Phase 9i (28.04.2026): _assign_wallets refuses to put 2 legs
        on one wallet. Old code did round-robin; new returns empty list
        when not enough distinct eligible bots exist."""
        pool = _three_wallet_pool()
        # 3 wallets, 5 legs → not enough distinct → []
        assigned = _assign_wallets(5, pool)
        self.assertEqual(assigned, [],
            "5 legs on 3-wallet pool must refuse, not round-robin")

    def test_empty_pool_falls_back_to_mock(self):
        assigned = _assign_wallets(3, [])
        self.assertEqual(len(assigned), 3)
        self.assertTrue(all(w.bot_id == 'mock' for w in assigned))

    def test_one_leg_one_wallet(self):
        """Anti-detection rule from feedback memory: never aggregate legs in
        one wallet when the pool is large enough. With 3 wallets / 3 legs,
        each leg goes to a distinct bot."""
        pool = _three_wallet_pool()
        assigned = _assign_wallets(3, pool)
        self.assertEqual(len({w.bot_id for w in assigned}), 3)


# ── fire_arb (dry-run) ──────────────────────────────────────────────
class TestFireArbDryRun(unittest.TestCase):
    def setUp(self):
        # Reroute log paths to a temp dir to avoid polluting real Executions/
        self._tmpdir = os.path.join(HERE, '_tmp_executions')
        os.makedirs(self._tmpdir, exist_ok=True)
        self._patches = [
            mock.patch.object(dryrun_log, 'EXECUTIONS_DIR', self._tmpdir),
            mock.patch.object(dryrun_log, 'DRYRUN_LOG_PATH',
                              os.path.join(self._tmpdir, 'dryrun.jsonl')),
            mock.patch.object(dryrun_log, 'PAPER_RESULTS_PATH',
                              os.path.join(self._tmpdir, 'paper_results.jsonl')),
            # Disable the realistic-eval daemon so tests stay deterministic
            mock.patch.object(dryrun_log, 'schedule_realistic_eval', lambda *a, **k: None),
            # Phase 3: isolate risk state so a real .killed flag or
            # persisted daily-loss counter doesn't affect these tests.
            mock.patch.object(_risk_state, 'EXECUTIONS_DIR', self._tmpdir),
            mock.patch.object(_risk_state, 'STATE_PATH',
                              os.path.join(self._tmpdir, 'risk_state.json')),
            mock.patch.object(_risk_killswitch, 'EXECUTIONS_DIR', self._tmpdir),
            mock.patch.object(_risk_killswitch, 'KILL_FLAG_PATH',
                              os.path.join(self._tmpdir, '.killed')),
            mock.patch.object(_risk_killswitch, 'KILL_LOG_PATH',
                              os.path.join(self._tmpdir, 'killswitch.jsonl')),
        ]
        for p in self._patches: p.start()
        _risk_state.reset_for_test()

    def tearDown(self):
        for p in self._patches: p.stop()
        _risk_state.reset_for_test()
        # Clean up
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_polymarket_three_legs(self):
        deal = _poly_deal(n_legs=3)
        pool = _three_wallet_pool()
        res = fire_arb(deal, wallets=pool, dry_run=True)
        self.assertIsInstance(res, ArbFireResult)
        self.assertTrue(res.dry_run)
        self.assertEqual(len(res.legs), 3)
        self.assertTrue(all(l.status == 'dry-fired' for l in res.legs))
        # Each leg goes to a different bot (anti-detection)
        self.assertEqual(len({l.bot_id for l in res.legs}), 3)

    def test_sx_two_legs(self):
        """Phase 7: build_sx_order now matches against /orders. We patch the
        builder to a stub that returns a fully-matched body — full SX flow
        coverage lives in tests/test_sx_executor.py."""
        from executor import builders
        from executor import atomic
        deal = _sx_deal()

        def stub_build_sx(market_hash, outcome, taker_price, size_usdc, wallet,
                          expiration_secs=60, slippage_tolerance=0.005, fetcher=None):
            # Pretend a single matching maker order covered the full size
            return {
                'platform': 'sx_bet',
                'body': {
                    'marketHash': market_hash, 'taker': wallet.eth_address,
                    'takerOutcome': outcome,
                    'fillAmount': str(int(size_usdc * 1e6)),
                    'orderHashes': ['0xstub'],
                    'takerAmounts': [str(int(size_usdc * 1e6))],
                    'expiry': '9999999999', 'salt': 'test',
                },
                'sign_payload': b'{}',
                'would_post_url': builders.SX_FILL_URL,
                'expected_price': taker_price,
                'expected_size_usdc': size_usdc,
                'sx_match': {
                    'avg_fill_price': taker_price, 'best_price': taker_price,
                    'worst_price': taker_price, 'filled_usdc': size_usdc,
                    'shortfall_usdc': 0.0, 'partial_fill': False,
                    'matched_orders': 1, 'available_orders': 1,
                    'slippage_cap': slippage_tolerance,
                    'max_taker_price_accepted': taker_price + slippage_tolerance,
                },
                'partial_fill': False,
            }
        with mock.patch.object(builders, 'build_sx_order', side_effect=stub_build_sx):
            res = fire_arb(deal, wallets=_three_wallet_pool(), dry_run=True)
        self.assertEqual(len(res.legs), 2)
        self.assertEqual(res.deal_structure, 'binary')
        self.assertTrue(all(l.status == 'dry-fired' for l in res.legs))

    def test_kalshi_legs_marked_disabled(self):
        deal = _poly_deal(n_legs=2)
        deal['platform'] = 'Kalshi'
        res = fire_arb(deal, wallets=_three_wallet_pool(), dry_run=True)
        self.assertEqual(len(res.legs), 2)
        self.assertTrue(all(l.status == 'disabled' for l in res.legs))

    def test_real_mode_blocked(self):
        """Phase 2/3 must NOT actually fire. Real-mode returns early with
        an explicit aborted_reason until Phase 4/5 graduation passes.
        After Phase 3, risk gate also blocks pre-emptively, but for a deal
        that passes risk we want to see the real-mode-disabled reason."""
        res = fire_arb(_poly_deal(), wallets=_three_wallet_pool(), dry_run=False)
        self.assertEqual(len(res.legs), 0)
        # Either reason is acceptable — risk gate or real-mode lock
        self.assertIsNotNone(res.aborted_reason)

    def test_logs_written(self):
        fire_arb(_poly_deal(n_legs=3), wallets=_three_wallet_pool(), dry_run=True)
        path = os.path.join(self._tmpdir, 'dryrun.jsonl')
        self.assertTrue(os.path.exists(path))
        with open(path, encoding='utf-8') as f:
            lines = [json.loads(l) for l in f if l.strip()]
        kinds = [l['kind'] for l in lines]
        self.assertIn('arb', kinds)         # top-level summary
        self.assertEqual(kinds.count('leg'), 3)

    def test_risk_blocked_still_logs_decision(self):
        """Regression for the silent-block bug found 28.04.2026: when the risk
        gate denies a fire, the decision MUST still hit dryrun.jsonl so the
        operator sees WHY paper trades aren't accumulating.

        Phase audit-28b cont (27.05.2026): updated for per-leg cap. PR #28
        (Phase 9i) changed MAX_PER_TRADE_USD semantics from sum(legs) to
        max(leg). To trigger risk_blocked we now need a SINGLE leg above
        the cap ($55), not just total. $60/leg × 3 legs guarantees the
        per-leg gate trips."""
        deal = _poly_deal(n_legs=3, stake_per_leg=60.0)
        res = fire_arb(deal, wallets=_three_wallet_pool(), dry_run=True)
        self.assertEqual(len(res.legs), 0)
        self.assertIn('risk_blocked', res.aborted_reason)
        # Decision row must still be in the log
        path = os.path.join(self._tmpdir, 'dryrun.jsonl')
        self.assertTrue(os.path.exists(path), 'dryrun.jsonl must exist even when blocked')
        with open(path, encoding='utf-8') as f:
            lines = [json.loads(l) for l in f if l.strip()]
        arbs = [l for l in lines if l['kind'] == 'arb']
        self.assertEqual(len(arbs), 1)
        self.assertIn('risk_blocked', arbs[0]['aborted_reason'])
        self.assertEqual(arbs[0]['leg_count'], 0)   # no legs were fired


# ── paper_stats aggregator ──────────────────────────────────────────
class TestPaperStats(unittest.TestCase):
    def setUp(self):
        self._tmpdir = os.path.join(HERE, '_tmp_paper')
        os.makedirs(self._tmpdir, exist_ok=True)
        self._path = os.path.join(self._tmpdir, 'paper_results.jsonl')
        self._p = mock.patch.object(dryrun_log, 'PAPER_RESULTS_PATH', self._path)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _write(self, rows):
        with open(self._path, 'w', encoding='utf-8') as f:
            for r in rows: f.write(json.dumps(r) + '\n')

    def test_empty(self):
        s = dryrun_log.paper_stats()
        self.assertEqual(s['count'], 0)
        self.assertIsNone(s['win_rate_pct'])

    def test_win_rate(self):
        rows = [{'realistic_pnl_5s': 1.0, 'drift': 0.0, 'legs': []} for _ in range(7)]
        rows += [{'realistic_pnl_5s': -0.5, 'drift': 0.1, 'legs': []} for _ in range(3)]
        self._write(rows)
        s = dryrun_log.paper_stats(window_n=100)
        self.assertEqual(s['count'], 10)
        self.assertEqual(s['win_rate_pct'], 70.0)

    def test_graduation_gate(self):
        # 100 trades, 75% wins, 5% drift — should be graduation_ready
        rows = [{'realistic_pnl_5s': 1.0, 'drift': 0.05, 'legs': []} for _ in range(75)]
        rows += [{'realistic_pnl_5s': -1.0, 'drift': 0.05, 'legs': []} for _ in range(25)]
        self._write(rows)
        s = dryrun_log.paper_stats(window_n=100)
        self.assertTrue(s['graduation_ready'])

    def test_graduation_not_ready_low_winrate(self):
        rows = [{'realistic_pnl_5s': 1.0, 'drift': 0.05, 'legs': []} for _ in range(60)]
        rows += [{'realistic_pnl_5s': -1.0, 'drift': 0.05, 'legs': []} for _ in range(40)]
        self._write(rows)
        s = dryrun_log.paper_stats(window_n=100)
        self.assertFalse(s['graduation_ready'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
