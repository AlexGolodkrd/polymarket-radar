"""Unit tests for the Phase 3 risk package.

Run from repo root:
    python -m pytest tests/test_risk.py -v
    python -m unittest tests.test_risk -v
"""
import json
import os
import sys
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'Scripts'))

import risk
from risk import state as st
from risk import limits, killswitch, reconcile


def _deal(stake_per_leg=10.0, n_legs=3):
    return {
        'title': 'Test', 'platform': 'Polymarket', 'arb_structure': 'all_yes',
        'entries': [{'stake': stake_per_leg, 'price': 0.3, 'contracts': 30,
                     'token_id': f't{i}'} for i in range(n_legs)],
    }


class _RiskTest(unittest.TestCase):
    """Base class — every test runs in an isolated tmp dir to avoid leaking
    risk_state.json or .killed flags across tests."""
    def setUp(self):
        self._tmpdir = os.path.join(HERE, '_tmp_risk_' + self.id().rsplit('.',1)[1])
        os.makedirs(self._tmpdir, exist_ok=True)
        self._patches = [
            mock.patch.object(st, 'EXECUTIONS_DIR', self._tmpdir),
            mock.patch.object(st, 'STATE_PATH', os.path.join(self._tmpdir, 'risk_state.json')),
            mock.patch.object(killswitch, 'EXECUTIONS_DIR', self._tmpdir),
            mock.patch.object(killswitch, 'KILL_FLAG_PATH', os.path.join(self._tmpdir, '.killed')),
            mock.patch.object(killswitch, 'KILL_LOG_PATH', os.path.join(self._tmpdir, 'killswitch.jsonl')),
            mock.patch.object(reconcile, 'EXECUTIONS_DIR', self._tmpdir),
            mock.patch.object(reconcile, 'POSITIONS_LOG', os.path.join(self._tmpdir, 'positions.jsonl')),
            mock.patch.object(reconcile, 'RECONCILE_LOG', os.path.join(self._tmpdir, 'reconcile.jsonl')),
        ]
        for p in self._patches: p.start()
        st.reset_for_test()
        killswitch.clear_cancel_callbacks()
        reconcile.clear_exchange_fetchers()

    def tearDown(self):
        for p in self._patches: p.stop()
        st.reset_for_test()
        killswitch.clear_cancel_callbacks()
        reconcile.clear_exchange_fetchers()
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)


# ── Limits ──────────────────────────────────────────────────────────
class TestPerTradeCap(_RiskTest):
    def test_under_cap_allowed(self):
        ok, reason = limits.check_can_fire(_deal(stake_per_leg=10.0, n_legs=3))
        self.assertTrue(ok); self.assertIsNone(reason)

    def test_over_cap_blocked(self):
        # Phase 9i: cap is per-LEG ($55), not per-arb sum.
        # Use stake_per_leg=$60 to actually trip the per-leg cap.
        ok, reason = limits.check_can_fire(_deal(stake_per_leg=60.0, n_legs=3))
        self.assertFalse(ok)
        self.assertIn('per_leg_cap', reason)


class TestDailyLossLimit(_RiskTest):
    def test_under_limit_allowed(self):
        limits.record_pnl(-20.0)
        ok, reason = limits.check_can_fire(_deal(stake_per_leg=3.0, n_legs=3))
        self.assertTrue(ok)

    def test_at_limit_paused(self):
        limits.record_pnl(-35.0)   # exactly at limit
        ok, reason = limits.check_can_fire(_deal())
        self.assertFalse(ok)
        self.assertIn('paused', reason)

    def test_over_limit_paused(self):
        limits.record_pnl(-40.0)
        ok, reason = limits.check_can_fire(_deal())
        self.assertFalse(ok)

    def test_pretrade_check_blocks_borderline(self):
        """If we're at -$30 daily and next trade could lose up to $10,
        worst case = -$40 which crosses the -$35 limit. Refuse."""
        limits.record_pnl(-30.0)
        ok, reason = limits.check_can_fire(_deal(stake_per_leg=4.0, n_legs=3))  # cost $12
        self.assertFalse(ok)
        self.assertIn('pre_trade_daily_check', reason)


class TestHourlyLosingStreak(_RiskTest):
    def test_under_5_losing_allowed(self):
        for _ in range(4):
            limits.record_pnl(-1.0)
        ok, _ = limits.check_can_fire(_deal())
        self.assertTrue(ok)

    def test_5_losing_pauses_1h(self):
        for _ in range(5):
            limits.record_pnl(-1.0)
        snap = limits.snapshot()
        self.assertTrue(snap['paused'])
        self.assertGreater(snap['paused_remaining_s'], 3500)
        self.assertLess(snap['paused_remaining_s'], 3700)
        self.assertIn('hourly_losing_streak', snap['paused_reason'])

    def test_winning_trade_doesnt_count(self):
        for _ in range(10):
            limits.record_pnl(+0.5)
        ok, _ = limits.check_can_fire(_deal())
        self.assertTrue(ok)


# ── Kill switch ─────────────────────────────────────────────────────
class TestKillSwitch(_RiskTest):
    def test_initially_off(self):
        self.assertFalse(killswitch.is_killed())

    def test_kill_creates_flag(self):
        killswitch.kill('test_reason')
        self.assertTrue(killswitch.is_killed())
        st_ = killswitch.status()
        self.assertEqual(st_['flag_info']['reason'], 'test_reason')

    def test_unkill_clears_flag(self):
        killswitch.kill('x')
        was = killswitch.unkill()
        self.assertTrue(was)
        self.assertFalse(killswitch.is_killed())

    def test_kill_blocks_check_can_fire(self):
        killswitch.kill('block_test')
        ok, reason = limits.check_can_fire(_deal())
        self.assertFalse(ok)
        self.assertEqual(reason, 'kill_switch_active')

    def test_cancel_callback_runs_on_kill(self):
        called = []
        killswitch.register_cancel_callback(lambda r: called.append(r))
        killswitch.kill('with_callback')
        self.assertEqual(called, ['with_callback'])

    def test_kill_idempotent(self):
        called = []
        killswitch.register_cancel_callback(lambda r: called.append(r))
        killswitch.kill('first')
        killswitch.kill('second')   # already killed — callback NOT re-run
        self.assertEqual(len(called), 1)


# ── Reconcile ───────────────────────────────────────────────────────
class TestReconcile(_RiskTest):
    def test_diff_clean(self):
        local = {('p', 'm1', 1): 10.0}
        remote = {('p', 'm1', 1): 10.0}
        self.assertEqual(reconcile._diff_positions(local, remote), [])

    def test_diff_within_tolerance(self):
        local = {('p', 'm1', 1): 10.005}
        remote = {('p', 'm1', 1): 10.0}
        # 0.005 within 0.01 tolerance — no mismatch
        self.assertEqual(reconcile._diff_positions(local, remote), [])

    def test_diff_outside_tolerance(self):
        local = {('p', 'm1', 1): 10.0}
        remote = {('p', 'm1', 1): 8.0}
        ms = reconcile._diff_positions(local, remote)
        self.assertEqual(len(ms), 1)
        self.assertAlmostEqual(ms[0]['diff'], 2.0)

    def test_remote_only_key_is_mismatch(self):
        local = {}
        remote = {('p', 'mX', 2): 5.0}
        self.assertEqual(len(reconcile._diff_positions(local, remote)), 1)

    def test_reconcile_skipped_when_no_fetchers(self):
        # No exchange fetchers registered — Phase 3 default.
        s = reconcile.reconcile_once()
        self.assertTrue(s['ok'])
        self.assertTrue(s['skipped'])

    def test_reconcile_mismatch_trips_killswitch(self):
        reconcile.register_exchange_fetcher(lambda: {('p', 'm', 1): 5.0})
        # Local positions log empty → 0 vs 5 mismatch
        s = reconcile.reconcile_once()
        self.assertFalse(s['ok'])
        self.assertTrue(killswitch.is_killed())


# ── snapshot() shape ───────────────────────────────────────────────
class TestSnapshot(_RiskTest):
    def test_required_keys(self):
        snap = limits.snapshot()
        for k in ['killed', 'paused', 'daily_pnl_usd', 'daily_loss_limit_usd',
                  'losing_trades_last_hour', 'losing_trades_per_hour_limit',
                  'max_per_trade_usd']:
            self.assertIn(k, snap)

    def test_daily_loss_remaining_decreases(self):
        snap0 = limits.snapshot()
        self.assertEqual(snap0['daily_loss_remaining_usd'], 35.0)
        limits.record_pnl(-15.0)
        snap1 = limits.snapshot()
        self.assertEqual(snap1['daily_loss_remaining_usd'], 20.0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
