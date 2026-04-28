"""Phase 9i — critical bug fixes from sub-agent audit (28.04.2026).

Six classes of issues discovered + Polymarket V2 migration polish:
1. MAX_PER_TRADE_USD applied as sum(legs) instead of per-leg → cuts P&L 3×
2. jitter_ms_for_leg defined but never invoked → all legs in ±1ms (detectable)
3. wallet round-robin puts 2 legs of same arb on one address (anti-detection)
4. _maybe_dry_fire held lock during fire (serialization + race window)
5. killswitch.is_killed() fails open on permission errors (UNSAFE)
6. ALL_NO gross math wrong (used 1−sum_no, should be (N-1)−sum_no)
+ V2: order_type='GTD' adds expiration to POST body
"""
import os
import sys
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'Scripts'))

import arb_server
from executor import builders, atomic, fills


# ── Fix 1: per-leg cap ──────────────────────────────────────────────
class TestPerLegCap(unittest.TestCase):
    """`_trade_cost_estimate` must return MAX leg stake, not sum."""

    def test_returns_max_leg_not_sum(self):
        from risk import limits
        deal = {'entries': [
            {'stake': 18.0}, {'stake': 18.0}, {'stake': 18.0},
        ]}
        # Old code: sum = 54 (passes $55 cap). New code: max = 18 (passes).
        # Both pass for THIS deal; the difference shows when stakes are
        # higher. Try $30/leg × 3 = sum 90 (would FAIL old) vs max 30 (PASS new).
        deal2 = {'entries': [{'stake': 30.0}] * 3}
        self.assertEqual(limits._trade_cost_estimate(deal), 18.0)
        self.assertEqual(limits._trade_cost_estimate(deal2), 30.0)

    def test_three_leg_50_passes_now(self):
        """3-leg arb with $50/leg: old sum=$150 blocked, new max=$50 passes."""
        from risk import limits, state as st
        deal = {'entries': [{'stake': 50.0}] * 3}
        # State will block on kill switch unless we mock — but the cap
        # check is BEFORE kill switch in the function order. We just want
        # to assert the per-leg semantics.
        max_leg = limits._trade_cost_estimate(deal)
        self.assertLessEqual(max_leg, st.MAX_PER_TRADE_USD,
            "3 legs of $50 should pass per-leg cap of $55")


# ── Fix 2: jitter actually fires ────────────────────────────────────
class TestJitterFires(unittest.TestCase):
    def test_jitter_called_for_each_leg(self):
        """fire_arb wraps each leg in a function that calls
        jitter_ms_for_leg(idx) before invoking the real fire path."""
        from wallets import coordinator
        # Replace jitter with a counter so we can assert it was called
        seen_calls = []
        original = coordinator.jitter_ms_for_leg
        coordinator.jitter_ms_for_leg = lambda i: (seen_calls.append(i) or 0)
        try:
            # Force a re-import of atomic so it picks up the patched coordinator
            # (atomic imports jitter_ms_for_leg lazily inside fire_arb).
            deal = {
                'platform': 'Polymarket',
                'title': 'Jitter test',
                'arb_structure': 'binary',
                'entries': [
                    {'name': 'A', 'price': 0.4, 'stake': 5.0, 'token_id': '1'},
                    {'name': 'B', 'price': 0.4, 'stake': 5.0, 'token_id': '2'},
                    {'name': 'C', 'price': 0.4, 'stake': 5.0, 'token_id': '3'},
                ],
            }
            wallets = [
                builders.WalletStub(bot_id='bot1', eth_address='0x' + 'a' * 40),
                builders.WalletStub(bot_id='bot2', eth_address='0x' + 'b' * 40),
                builders.WalletStub(bot_id='bot3', eth_address='0x' + 'c' * 40),
            ]
            atomic.fire_arb(deal, wallets=wallets, dry_run=True)
            # 3 legs → 3 jitter invocations
            self.assertEqual(sorted(seen_calls), [0, 1, 2])
        finally:
            coordinator.jitter_ms_for_leg = original


# ── Fix 3: distinct wallets enforced ────────────────────────────────
class TestDistinctWallets(unittest.TestCase):
    def test_round_robin_no_longer_used(self):
        """Old code: 3 legs + 2 wallets → [w0, w1, w0]. New: returns
        empty list when not enough distinct wallets."""
        wallets_pool = [
            builders.WalletStub(bot_id='bot1', eth_address='0x' + 'a' * 40),
            builders.WalletStub(bot_id='bot2', eth_address='0x' + 'b' * 40),
        ]
        assigned = atomic._assign_wallets(3, wallets_pool)
        self.assertEqual(assigned, [],
            "Should refuse to put 2 legs on one wallet")

    def test_distinct_wallets_returned(self):
        wallets_pool = [
            builders.WalletStub(bot_id=f'bot{i}', eth_address='0x' + str(i) * 40)
            for i in range(1, 5)
        ]
        assigned = atomic._assign_wallets(3, wallets_pool)
        self.assertEqual(len(assigned), 3)
        ids = {w.bot_id for w in assigned}
        self.assertEqual(len(ids), 3, "All assigned wallets must be distinct")

    def test_fire_arb_aborts_on_wallet_shortage(self):
        deal = {
            'platform': 'Polymarket',
            'title': 'Shortage',
            'arb_structure': 'all_yes',
            'entries': [
                {'name': 'A', 'price': 0.3, 'stake': 5.0, 'token_id': '1'},
                {'name': 'B', 'price': 0.3, 'stake': 5.0, 'token_id': '2'},
                {'name': 'C', 'price': 0.3, 'stake': 5.0, 'token_id': '3'},
            ],
        }
        wallets_pool = [
            builders.WalletStub(bot_id='bot1', eth_address='0x' + 'a' * 40),
        ]
        result = atomic.fire_arb(deal, wallets=wallets_pool, dry_run=True)
        self.assertIsNotNone(result.aborted_reason)
        self.assertIn('wallet_assignment_failed', result.aborted_reason or '')


# ── Fix 4: two-phase commit, no dupe-fire ───────────────────────────
class TestNoDupeFire(unittest.TestCase):
    def test_same_deal_fires_only_once(self):
        """Even if _maybe_dry_fire sees the same deal twice (e.g. from
        scan + WS push within one batch), fire_arb runs ONCE."""
        # Reset module-level state
        with arb_server._fired_arb_keys_lock:
            arb_server._fired_arb_keys.clear()

        deal = {
            'platform': 'Polymarket',
            'title': 'Dupe test',
            'arb_structure': 'all_yes',
            'is_quarantine': False,
            'entries': [
                {'name': 'A', 'price': 0.4, 'stake': 5.0, 'token_id': '1'},
                {'name': 'B', 'price': 0.4, 'stake': 5.0, 'token_id': '2'},
            ],
        }
        # Wrap fire_arb to count calls
        original_fire = arb_server.fire_arb
        call_count = []

        def counting_fire(*a, **kw):
            call_count.append(1)
            return original_fire(*a, **kw)

        arb_server.fire_arb = counting_fire
        try:
            arb_server._maybe_dry_fire([deal, deal, deal])
            # Even with 3 references to the same deal, only fires once
            self.assertEqual(len(call_count), 1)
        finally:
            arb_server.fire_arb = original_fire

    def test_concurrent_calls_no_dupe(self):
        """Two threads calling _maybe_dry_fire with overlapping deals
        must not both fire the same key."""
        import threading
        with arb_server._fired_arb_keys_lock:
            arb_server._fired_arb_keys.clear()
        deal = {
            'platform': 'Limitless',
            'title': 'Concurrent test',
            'arb_structure': 'all_yes',
            'is_quarantine': False,
            'entries': [
                {'name': 'A', 'price': 0.4, 'stake': 5.0, 'slug': 'a'},
                {'name': 'B', 'price': 0.4, 'stake': 5.0, 'slug': 'b'},
            ],
        }
        original_fire = arb_server.fire_arb
        call_count = []
        lock = threading.Lock()

        def counting_fire(*a, **kw):
            with lock:
                call_count.append(1)
            return original_fire(*a, **kw)

        arb_server.fire_arb = counting_fire
        try:
            t1 = threading.Thread(target=arb_server._maybe_dry_fire, args=([deal],))
            t2 = threading.Thread(target=arb_server._maybe_dry_fire, args=([deal],))
            t1.start(); t2.start()
            t1.join(); t2.join()
            self.assertEqual(len(call_count), 1,
                "Concurrent _maybe_dry_fire on same deal must fire ONCE")
        finally:
            arb_server.fire_arb = original_fire


# ── Fix 5: killswitch fail-closed ───────────────────────────────────
class TestKillswitchFailClosed(unittest.TestCase):
    def test_filesystem_error_returns_killed(self):
        from risk import killswitch
        with mock.patch('os.path.exists', side_effect=PermissionError("denied")):
            self.assertTrue(killswitch.is_killed(),
                "Permission error must be treated as KILLED (fail-closed)")

    def test_normal_path_unchanged(self):
        from risk import killswitch
        with mock.patch('os.path.exists', return_value=False):
            self.assertFalse(killswitch.is_killed())
        with mock.patch('os.path.exists', return_value=True):
            self.assertTrue(killswitch.is_killed())


# ── Fix 6: ALL_NO gross math ────────────────────────────────────────
class TestAllNoGrossMath(unittest.TestCase):
    def test_three_outcome_all_no_now_profitable(self):
        """N=3, sum_no=1.95 → payout=2 → gross = (2 - 1.95) * balance = 0.05*$55 = $2.75.
        Old code: gross = (1 - 1.95) * 55 = -$52.25 → net<=0 filter killed it."""
        outcomes = [
            {'name': 'NO_A', 'price': 0.65, 'liquidity': 10000, 'source': 'x'},
            {'name': 'NO_B', 'price': 0.65, 'liquidity': 10000, 'source': 'x'},
            {'name': 'NO_C', 'price': 0.65, 'liquidity': 10000, 'source': 'x'},
        ]
        total_no = 1.95
        no_threshold = 2 * 0.99  # 1.98
        d = arb_server.build_deal(
            'Test ALL_NO', 'Test', outcomes, total_no,
            theta=0.005, threshold=no_threshold, payout_target=2.0,
        )
        self.assertIsNotNone(d, "ALL_NO with payout_target=N-1 must produce deal")
        self.assertGreater(d['net'], 0)
        self.assertGreater(d['gross'], 0)

    def test_default_payout_target_unchanged(self):
        """For ALL_YES (no payout_target arg) behavior must be identical to before."""
        outcomes = [
            {'name': 'A', 'price': 0.30, 'liquidity': 1000, 'source': 'x'},
            {'name': 'B', 'price': 0.30, 'liquidity': 1000, 'source': 'x'},
            {'name': 'C', 'price': 0.30, 'liquidity': 1000, 'source': 'x'},
        ]
        d = arb_server.build_deal(
            'Test ALL_YES', 'Test', outcomes, 0.90,
            theta=0.005, threshold=0.99,
        )
        self.assertIsNotNone(d)
        # Margin per $1 = 1 - 0.90 = 0.10. With actual_balance scaled by
        # risk-cap (max_share=0.30/0.90=0.333; max_leg target=$55 → balance=$165
        # but capped at $100 default → BALANCE=100 OK). gross = 0.10*100 = 10.
        # After fee+slip might shrink — just check positive.
        self.assertGreater(d['gross'], 0)


# ── V2: GTD adds expiration to POST body ────────────────────────────
class TestPolyV2GTDExpiration(unittest.TestCase):
    def test_gtc_no_expiration(self):
        wallet = builders.WalletStub(bot_id='b', eth_address='0x' + 'a' * 40)
        o = builders.build_poly_order('123', 'BUY', 0.5, 5.0, wallet)
        self.assertEqual(o['body']['orderType'], 'GTC')
        self.assertNotIn('expiration', o['body'])

    def test_gtd_includes_expiration(self):
        wallet = builders.WalletStub(bot_id='b', eth_address='0x' + 'a' * 40)
        o = builders.build_poly_order(
            '123', 'BUY', 0.5, 5.0, wallet,
            order_type='GTD', expiration_secs=120,
        )
        self.assertEqual(o['body']['orderType'], 'GTD')
        self.assertIn('expiration', o['body'])
        ts = int(o['body']['expiration'])
        # within 5s window of expected
        self.assertLess(abs(ts - (int(time.time()) + 120)), 5)

    def test_v2_order_struct_unchanged_by_order_type(self):
        """`order_type` is a wrapper-level concern — the signed Order
        struct must still be V2 shape regardless."""
        wallet = builders.WalletStub(bot_id='b', eth_address='0x' + 'a' * 40)
        o_gtc = builders.build_poly_order('1', 'BUY', 0.5, 5.0, wallet)
        o_gtd = builders.build_poly_order('1', 'BUY', 0.5, 5.0, wallet,
                                           order_type='GTD')
        # Both have V2 fields, no V1 legacy in the signed order
        for o in (o_gtc, o_gtd):
            order = o['order']
            self.assertIn('timestamp', order)
            self.assertIn('metadata', order)
            self.assertIn('builder', order)
            for legacy in ('expiration', 'nonce', 'feeRateBps', 'taker'):
                self.assertNotIn(legacy, order,
                    f"V1 legacy field {legacy} leaked into V2 signed Order")


if __name__ == '__main__':
    unittest.main(verbosity=2)
