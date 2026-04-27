"""Unit tests for the Phase 4 wallets package."""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'Scripts'))

import wallets
from wallets.config import Wallet, WalletPool, MIN_USDC_PER_BOT
from wallets.stores import LocalEnvStore, load_pool
from wallets.coordinator import assign_legs, can_fire_pool, _eligible
from wallets import rebalance


def _make_pool(balances):
    """Synthetic pool from a {bot_id: usdc} dict."""
    return WalletPool(wallets=[
        Wallet(bot_id=k, eth_address=f'0x{k}'.ljust(42, '0'),
               store_name='test', can_sign=False, last_known_usdc=v)
        for k, v in balances.items()
    ])


# ── LocalEnvStore ──────────────────────────────────────────────────
class TestLocalEnvStore(unittest.TestCase):
    def setUp(self):
        self.f = tempfile.NamedTemporaryFile('w', suffix='.env', delete=False, encoding='utf-8')
        self.f.write('BOT1_ETH_ADDRESS=0xaaaa\n')
        self.f.write('BOT1_PRIVATE_KEY=0xkey1\n')
        self.f.write('BOT3_ETH_ADDRESS=0xcccc\n')   # bot2 missing on purpose
        self.f.write('# comment\n')
        self.f.write('BOT4_ETH_ADDRESS="0xdddd"\n') # quoted value
        self.f.close()
        # Don't let real env vars override
        self._env_patches = []
        for k in ('BOT1_ETH_ADDRESS', 'BOT1_PRIVATE_KEY', 'BOT3_ETH_ADDRESS',
                  'BOT4_ETH_ADDRESS'):
            if k in os.environ:
                self._env_patches.append(mock.patch.dict(os.environ, {k: ''}))
                self._env_patches[-1].start()

    def tearDown(self):
        for p in self._env_patches: p.stop()
        os.unlink(self.f.name)

    def test_addresses_only_loads_present(self):
        s = LocalEnvStore(env_path=self.f.name)
        addrs = s.addresses()
        self.assertIn('bot1', addrs)
        self.assertNotIn('bot2', addrs)
        self.assertEqual(addrs['bot1'], '0xaaaa')
        self.assertEqual(addrs['bot4'], '0xdddd')   # quotes stripped

    def test_has_key(self):
        s = LocalEnvStore(env_path=self.f.name)
        # has_key needs the address loaded first
        s.addresses()
        self.assertTrue(s.has_key('bot1'))
        self.assertFalse(s.has_key('bot3'))   # has address but no key

    def test_load_pool_empty_when_no_env(self):
        # nonexistent env file — pool should be empty, not crash
        s = LocalEnvStore(env_path='/nonexistent/file')
        self.assertEqual(s.addresses(), {})


# ── Coordinator ────────────────────────────────────────────────────
class TestCoordinator(unittest.TestCase):
    def test_eligible_filters_by_balance(self):
        pool = _make_pool({'bot1': 100, 'bot2': 30, 'bot3': 80})
        eligible = _eligible(pool)   # default MIN_USDC_PER_BOT = 60
        self.assertEqual({w.bot_id for w in eligible}, {'bot1', 'bot3'})

    def test_can_fire_pool_ok(self):
        pool = _make_pool({f'bot{i}': 100 for i in range(1, 7)})
        ok, reason = can_fire_pool(pool, legs_count=3)
        self.assertTrue(ok); self.assertIsNone(reason)

    def test_can_fire_pool_insufficient_eligible(self):
        # 6 bots but only 2 above threshold
        pool = _make_pool({'bot1': 100, 'bot2': 100, **{f'bot{i}': 0 for i in range(3, 7)}})
        ok, reason = can_fire_pool(pool, legs_count=3)
        self.assertFalse(ok)
        self.assertIn('insufficient_eligible_bots', reason)

    def test_assign_legs_distinct_bots(self):
        """Anti-detection rule from feedback memory: never aggregate
        multiple legs of one arb in one wallet."""
        pool = _make_pool({f'bot{i}': 100 for i in range(1, 7)})
        assigned = assign_legs(pool, legs_count=3)
        self.assertEqual(len(assigned), 3)
        # Each leg goes to a distinct bot
        self.assertEqual(len({w.bot_id for w in assigned}), 3)

    def test_assign_legs_empty_pool(self):
        # Empty pool — return [], executor falls back to mock stub
        pool = WalletPool(wallets=[])
        self.assertEqual(assign_legs(pool, legs_count=3), [])

    def test_assign_legs_prefers_low_balance(self):
        """Sort by balance ascending so the bots most needing throughput
        aren't starved."""
        pool = _make_pool({'bot1': 200, 'bot2': 80, 'bot3': 100, 'bot4': 70})
        assigned = assign_legs(pool, legs_count=2)
        # bot4 (70) and bot2 (80) are the two lowest eligible
        self.assertEqual([w.bot_id for w in assigned], ['bot4', 'bot2'])


# ── Rebalance ──────────────────────────────────────────────────────
class TestRebalance(unittest.TestCase):
    def setUp(self):
        # Isolate the rebalance log + cooldown state per test
        self._tmpdir = tempfile.mkdtemp()
        self._patches = [
            mock.patch.object(rebalance, 'EXECUTIONS_DIR', self._tmpdir),
            mock.patch.object(rebalance, 'REBALANCE_LOG',
                              os.path.join(self._tmpdir, 'rebalance.jsonl')),
        ]
        for p in self._patches: p.start()
        rebalance._pair_last_rebalance.clear()

    def tearDown(self):
        for p in self._patches: p.stop()
        rebalance._pair_last_rebalance.clear()
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_no_rebalance_when_balanced(self):
        pool = _make_pool({f'bot{i}': 150 for i in range(1, 7)})
        ps = rebalance.propose_rebalances(pool)
        self.assertEqual(ps, [])

    def test_proposes_when_low_and_high(self):
        pool = _make_pool({
            'bot1': 30,    # low — needs funds
            'bot2': 250,   # high — has surplus
            'bot3': 150, 'bot4': 150, 'bot5': 150, 'bot6': 150,
        })
        ps = rebalance.propose_rebalances(pool)
        self.assertEqual(len(ps), 1)
        self.assertEqual(ps[0].from_bot, 'bot2')
        self.assertEqual(ps[0].to_bot, 'bot1')
        self.assertGreater(ps[0].amount_usdc, 0)
        self.assertLess(ps[0].amount_usdc, 250 - 130)  # below excess

    def test_skips_dust_transfers(self):
        # bot1 just barely below threshold, bot2 just barely above reserve
        pool = _make_pool({
            'bot1': 59,      # very small need
            'bot2': 132,     # only $2 above reserve = dust
            **{f'bot{i}': 100 for i in range(3, 7)},
        })
        ps = rebalance.propose_rebalances(pool)
        # Should skip — transferable / 2 = $1 < $5 dust threshold
        self.assertEqual(ps, [])

    def test_cooldown_prevents_thrashing(self):
        pool = _make_pool({
            'bot1': 30, 'bot2': 250,
            **{f'bot{i}': 150 for i in range(3, 7)},
        })
        # Mark a recent rebalance for the (bot1, bot2) pair
        rebalance._pair_last_rebalance[('bot1', 'bot2')] = time.time()
        ps = rebalance.propose_rebalances(pool)
        self.assertEqual(ps, [])

    def test_history_reads_log(self):
        rebalance._append_log({'event': 'test', 'note': 'a'})
        rebalance._append_log({'event': 'test', 'note': 'b'})
        h = rebalance.rebalance_history()
        self.assertEqual(len(h), 2)
        self.assertEqual(h[1]['note'], 'b')

    def test_dryrun_execute_writes_log_doesnt_transfer(self):
        pool = _make_pool({'bot1': 30, 'bot2': 250, **{f'bot{i}': 150 for i in range(3, 7)}})
        # Add can_sign so we go past the source-key check… still dry-run logs
        pool.by_id('bot2').can_sign = True
        ps = rebalance.auto_rebalance_check(pool, execute=False)
        self.assertEqual(len(ps), 1)
        self.assertFalse(ps[0].executed)


# ── load_pool integration ──────────────────────────────────────────
class TestLoadPool(unittest.TestCase):
    def test_unknown_backend_falls_back(self):
        # Should warn and fall through to local — empty pool, no crash
        with mock.patch.dict(os.environ, {'WALLET_BACKEND': 'mars'}):
            # Need to also point local store at a non-existent file
            with mock.patch.object(LocalEnvStore, '_find_env', return_value=None):
                pool = load_pool()
        self.assertEqual(len(pool.wallets), 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
