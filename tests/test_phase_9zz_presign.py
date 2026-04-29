"""Phase 9zz — pre-signing cache tests.

Pre-signing skips the ~50ms/leg inline EIP-712 signing during fire by
caching signed bodies during NEAR-pool detection. Tests verify:
  1. Cache hit/miss accounting
  2. TTL expiry drops bundles
  3. Single-use semantics (consume removes the entry)
  4. Multi-structure bundles work
  5. Stats expose hit-rate
"""
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'Scripts'))

from executor import presign


class TestCacheBasics(unittest.TestCase):
    def setUp(self):
        presign.clear_cache()

    def test_miss_returns_none(self):
        self.assertIsNone(presign.consume_presigned('nonexistent', 'all_yes'))

    def test_cand_id_for_deal_is_deterministic(self):
        deal = {'platform': 'Polymarket', 'title': 'Foo', 'arb_structure': 'all_yes'}
        a = presign.cand_id_for_deal(deal)
        b = presign.cand_id_for_deal(deal)
        self.assertEqual(a, b)

    def test_clear_cache_resets(self):
        # Manually inject a bundle
        bundle = presign.PreSignedBundle(
            cand_id='X', deal_title='X', platform='polymarket',
            signed_orders={'all_yes': [{'fake': 1}]},
            expires_at=time.time() + 100,
        )
        with presign._cache_lock:
            presign._cache['X'] = bundle
        n = presign.clear_cache()
        self.assertEqual(n, 1)
        self.assertIsNone(presign.consume_presigned('X', 'all_yes'))


class TestCacheConsume(unittest.TestCase):
    def setUp(self):
        presign.clear_cache()
        # Reset stats
        with presign._stats_lock:
            for k in presign._stats:
                presign._stats[k] = 0

    def test_hit_returns_orders_and_pops(self):
        bundle = presign.PreSignedBundle(
            cand_id='cand-1', deal_title='T', platform='polymarket',
            signed_orders={'all_yes': [{'leg': 0}, {'leg': 1}, {'leg': 2}]},
            expires_at=time.time() + 100,
        )
        with presign._cache_lock:
            presign._cache['cand-1'] = bundle
        # First consume → hit
        orders = presign.consume_presigned('cand-1', 'all_yes')
        self.assertEqual(len(orders), 3)
        # Second consume on SAME id → miss (single-use semantics)
        again = presign.consume_presigned('cand-1', 'all_yes')
        self.assertIsNone(again, 'consume must POP — no double-fire')

    def test_expired_bundle_dropped(self):
        bundle = presign.PreSignedBundle(
            cand_id='cand-2', deal_title='T', platform='polymarket',
            signed_orders={'all_yes': [{}]},
            expires_at=time.time() - 1,  # already expired
        )
        with presign._cache_lock:
            presign._cache['cand-2'] = bundle
        result = presign.consume_presigned('cand-2', 'all_yes')
        self.assertIsNone(result)
        # Was it cleaned out?
        with presign._cache_lock:
            self.assertNotIn('cand-2', presign._cache)

    def test_wrong_structure_returns_miss(self):
        bundle = presign.PreSignedBundle(
            cand_id='cand-3', deal_title='T', platform='polymarket',
            signed_orders={'all_yes': [{}]},
            expires_at=time.time() + 100,
        )
        with presign._cache_lock:
            presign._cache['cand-3'] = bundle
        # Asking for all_no on a bundle with only all_yes → miss
        self.assertIsNone(presign.consume_presigned('cand-3', 'all_no'))


class TestCacheStats(unittest.TestCase):
    def setUp(self):
        presign.clear_cache()
        with presign._stats_lock:
            for k in presign._stats:
                presign._stats[k] = 0

    def test_stats_track_hit_miss(self):
        # 3 hits, 2 misses
        for i in range(3):
            with presign._cache_lock:
                presign._cache[f'h-{i}'] = presign.PreSignedBundle(
                    cand_id=f'h-{i}', deal_title='T', platform='polymarket',
                    signed_orders={'all_yes': [{}]},
                    expires_at=time.time() + 100,
                )
            presign.consume_presigned(f'h-{i}', 'all_yes')
        for i in range(2):
            presign.consume_presigned(f'm-{i}', 'all_yes')
        s = presign.get_stats()
        self.assertEqual(s['cache_hits'], 3)
        self.assertEqual(s['cache_misses'], 2)
        self.assertEqual(s['hit_rate_pct'], 60.0)


class TestEvictExpired(unittest.TestCase):
    def setUp(self):
        presign.clear_cache()

    def test_evict_removes_expired_only(self):
        # 1 valid + 2 expired
        with presign._cache_lock:
            presign._cache['valid'] = presign.PreSignedBundle(
                cand_id='valid', deal_title='V', platform='polymarket',
                signed_orders={'all_yes': [{}]},
                expires_at=time.time() + 100,
            )
            presign._cache['exp1'] = presign.PreSignedBundle(
                cand_id='exp1', deal_title='E1', platform='polymarket',
                signed_orders={'all_yes': [{}]},
                expires_at=time.time() - 1,
            )
            presign._cache['exp2'] = presign.PreSignedBundle(
                cand_id='exp2', deal_title='E2', platform='polymarket',
                signed_orders={'all_yes': [{}]},
                expires_at=time.time() - 5,
            )
        n = presign.evict_expired()
        self.assertEqual(n, 2)
        with presign._cache_lock:
            self.assertIn('valid', presign._cache)
            self.assertNotIn('exp1', presign._cache)


if __name__ == '__main__':
    unittest.main(verbosity=2)
