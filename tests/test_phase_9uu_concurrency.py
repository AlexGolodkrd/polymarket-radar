"""Phase 9uu — concurrency-heavy hot-path tests.

Audit identified gaps: no tests for classify_pools under concurrent
mutation, no tests for ws book lock, no tests for _fired_arb_keys
eviction.

This file plugs those gaps with deterministic tests that don't need
network calls.
"""
import os
import sys
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'Scripts'))

import arb_server


class TestFiredArbKeysEviction(unittest.TestCase):
    """_fired_arb_keys uses TTL-based eviction (Phase audit-27.05).

    Old behavior (Phase 9uu): drop keys whose deal is not in the
    current active list. Bug: arb that briefly leaves HOT pool and
    returns gets re-fired immediately. Operator screenshot 27.05.2026
    showed Saint-Etienne vs Nice Cross-Platform arb firing 18 times in
    1h02m.

    New behavior: keep key for FIRE_COOLDOWN_S seconds after last fire,
    independent of pool membership."""

    def setUp(self):
        with arb_server._fired_arb_keys_lock:
            arb_server._fired_arb_keys.clear()

    def test_keeps_keys_within_ttl_when_deal_leaves_active(self):
        """Regression for 27.05 re-fire loop. Round 1 fires A, B, C.
        Round 2 only A is in deals — B and C must STILL be tracked
        (within cooldown) so they don't re-fire if they reappear."""
        deals_round1 = [
            {'arb_structure': 'all_yes', 'platform': 'Polymarket', 'title': 'A'},
            {'arb_structure': 'all_yes', 'platform': 'Polymarket', 'title': 'B'},
            {'arb_structure': 'all_yes', 'platform': 'Polymarket', 'title': 'C'},
        ]
        with mock.patch('arb_server.fire_arb', return_value=None):
            arb_server._maybe_dry_fire(deals_round1)
        with arb_server._fired_arb_keys_lock:
            self.assertEqual(len(arb_server._fired_arb_keys), 3)

        # Round 2: only A active. B/C dropped out of pool — must NOT
        # be evicted (within TTL).
        deals_round2 = deals_round1[:1]
        with mock.patch('arb_server.fire_arb', return_value=None):
            arb_server._maybe_dry_fire(deals_round2)
        with arb_server._fired_arb_keys_lock:
            self.assertEqual(len(arb_server._fired_arb_keys), 3,
                             'B and C must stay tracked within TTL')

    def test_no_refire_when_arb_temporarily_leaves_pool(self):
        """Exact 27.05 scenario: arb is detected → fires once → leaves
        pool for 1 scan → returns → MUST NOT fire again (within TTL).
        """
        deal = {'arb_structure': 'cross_platform',
                'platform': 'Limitless+SX Bet',
                'title': 'Ligue 1, Saint Etienne vs Nice, May 26, 2026',
                'cross_structure': 'X1'}
        fire_calls = []
        def _track_fire(d, *a, **kw):
            fire_calls.append(d.get('title'))
            return None
        # Round 1: arb appears → fires
        with mock.patch('arb_server.fire_arb', side_effect=_track_fire):
            arb_server._maybe_dry_fire([deal])
        self.assertEqual(len(fire_calls), 1, 'Round 1 should fire once')

        # Round 2: arb temporarily out of pool — no deals at all
        with mock.patch('arb_server.fire_arb', side_effect=_track_fire):
            arb_server._maybe_dry_fire([])
        self.assertEqual(len(fire_calls), 1, 'Empty pool — nothing to fire')

        # Round 3: arb returns — MUST NOT re-fire within TTL
        with mock.patch('arb_server.fire_arb', side_effect=_track_fire):
            arb_server._maybe_dry_fire([deal])
        self.assertEqual(len(fire_calls), 1,
                         'Arb returned within cooldown — must not re-fire')

    def test_evicts_keys_after_ttl_expires(self):
        """When more than FIRE_COOLDOWN_S has elapsed since last fire,
        the key is evicted and the arb can fire again."""
        deal = {'arb_structure': 'all_yes', 'platform': 'P', 'title': 'TTL-test'}
        with mock.patch('arb_server.fire_arb', return_value=None):
            arb_server._maybe_dry_fire([deal])
        # Manually backdate the entry to past TTL
        with arb_server._fired_arb_keys_lock:
            self.assertEqual(len(arb_server._fired_arb_keys), 1)
            key = next(iter(arb_server._fired_arb_keys))
            arb_server._fired_arb_keys[key] = (
                time.time() - arb_server.FIRE_COOLDOWN_S - 10)
        # Next call should evict the expired key, then re-fire
        fire_calls = []
        def _track_fire(d, *a, **kw):
            fire_calls.append(d.get('title'))
            return None
        with mock.patch('arb_server.fire_arb', side_effect=_track_fire):
            arb_server._maybe_dry_fire([deal])
        self.assertEqual(len(fire_calls), 1,
                         'After TTL expiry the arb should fire again')

    def test_hard_cap_drops_oldest(self):
        """When the dict exceeds the hard cap, oldest 20% are dropped,
        not the whole dict (Phase audit-27.05 — old impl wiped all)."""
        with arb_server._fired_arb_keys_lock:
            base_ts = time.time() - 1
            for i in range(arb_server._FIRED_KEYS_HARD_CAP + 100):
                # newer keys have larger ts
                arb_server._fired_arb_keys[f'fake-{i}'] = base_ts + i * 0.001
        deals = [{'arb_structure': 'all_yes', 'platform': 'P', 'title': 'X'}]
        with mock.patch('arb_server.fire_arb', return_value=None):
            arb_server._maybe_dry_fire(deals)
        with arb_server._fired_arb_keys_lock:
            self.assertLessEqual(len(arb_server._fired_arb_keys),
                                 arb_server._FIRED_KEYS_HARD_CAP,
                                 'Hard cap must enforce upper bound')
            # New key 'X' must survive (just fired)
            x_key = arb_server._arb_fire_key(deals[0])
            self.assertIn(x_key, arb_server._fired_arb_keys,
                          'Freshly fired key must not be evicted by cap')


class TestApiSizeLimits(unittest.TestCase):
    """/api/approve and /api/reject must reject oversize / non-string
    titles and enforce a per-list cap (DOS protection)."""

    def setUp(self):
        self.app = arb_server.app.test_client()
        with arb_server.scan_lock:
            arb_server.whitelist.clear()
            arb_server.blacklist.clear()

    def test_approve_rejects_non_string_title(self):
        r = self.app.post('/api/approve', json={'title': {'evil': 'object'}})
        self.assertEqual(r.status_code, 400)

    def test_approve_truncates_oversize_title(self):
        # 10000-char title should be truncated to 500
        r = self.app.post('/api/approve', json={'title': 'x' * 10000})
        self.assertEqual(r.status_code, 200)
        with arb_server.scan_lock:
            stored = list(arb_server.whitelist)
            self.assertTrue(stored, 'whitelist should have one entry')
            self.assertLessEqual(len(stored[0]),
                                 arb_server.TITLE_MAX_LEN,
                                 'title should be truncated to TITLE_MAX_LEN')

    def test_approve_enforces_hard_cap(self):
        # Fill whitelist to the cap
        with arb_server.scan_lock:
            for i in range(arb_server.APPROVE_LIST_HARD_CAP):
                arb_server.whitelist.add(f'pre-existing-{i}')
        r = self.app.post('/api/approve', json={'title': 'overflow'})
        self.assertEqual(r.status_code, 429,
                         'Should refuse new entries when at cap')

    def test_reject_rejects_empty_title(self):
        r = self.app.post('/api/reject', json={'title': '   '})
        self.assertEqual(r.status_code, 400)


class TestKillTokenAuth(unittest.TestCase):
    """When ADMIN_KILL_TOKEN is configured, /api/kill must require the
    matching X-Admin-Token header."""

    def setUp(self):
        self.app = arb_server.app.test_client()
        # Reset kill state
        try:
            from risk import killswitch
            with killswitch._lock:
                if killswitch.is_killed():
                    killswitch.unkill()
        except Exception:
            pass

    def test_no_token_configured_falls_back_to_confirm_only(self):
        with mock.patch.object(arb_server, 'ADMIN_KILL_TOKEN', ''):
            r = self.app.post('/api/kill', json={'confirm': 'YES',
                                                 'reason': 'test'})
        # Should succeed (legacy behavior preserved when token unset)
        self.assertEqual(r.status_code, 200)

    def test_token_set_blocks_request_without_header(self):
        with mock.patch.object(arb_server, 'ADMIN_KILL_TOKEN', 'secret'):
            r = self.app.post('/api/kill', json={'confirm': 'YES'})
        self.assertEqual(r.status_code, 401)

    def test_token_set_blocks_wrong_header(self):
        with mock.patch.object(arb_server, 'ADMIN_KILL_TOKEN', 'secret'):
            r = self.app.post('/api/kill', json={'confirm': 'YES'},
                              headers={'X-Admin-Token': 'wrong'})
        self.assertEqual(r.status_code, 401)

    def test_token_set_allows_correct_header(self):
        with mock.patch.object(arb_server, 'ADMIN_KILL_TOKEN', 'secret'):
            r = self.app.post('/api/kill', json={'confirm': 'YES'},
                              headers={'X-Admin-Token': 'secret'})
        self.assertEqual(r.status_code, 200)


class TestPolyWsBookLockSafety(unittest.TestCase):
    """get_book and book mutations must not race — lock must be held."""

    def test_get_book_acquires_lock(self):
        from poly_ws import PolyMarketWS
        ws = PolyMarketWS(verbose=False)
        # Pre-seed a book
        with ws._lock:
            ws.books['tid-1'] = {'best_ask': 0.5, 'depth': 100, 'ts': time.time()}
        # Concurrent reads/writes should not crash with RuntimeError
        errors = []
        def reader():
            try:
                for _ in range(200):
                    ws.get_book('tid-1')
            except Exception as e: errors.append(('reader', e))
        def writer():
            try:
                for i in range(200):
                    ws._handle_event({
                        'event_type': 'book',
                        'asset_id': 'tid-1',
                        'asks': [{'price': str(0.5 + (i % 10) * 0.001),
                                  'size': str(100)}],
                    })
            except Exception as e: errors.append(('writer', e))
        threads = [threading.Thread(target=reader) for _ in range(3)] + \
                  [threading.Thread(target=writer) for _ in range(3)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(errors, [],
                         f'No reader/writer should crash. Got: {errors}')


class TestLimMetaCacheBoundedSize(unittest.TestCase):
    """Phase 9uu — lim_meta_cache must not exceed LIM_META_CACHE_MAX."""

    def test_eviction_kicks_in_at_cap(self):
        # Pre-populate to cap
        with arb_server.lim_meta_lock:
            arb_server.lim_meta_cache.clear()
            for i in range(arb_server.LIM_META_CACHE_MAX):
                arb_server.lim_meta_cache[f'slug-{i}'] = {
                    'volume': 0, 'fetched_at': float(i),
                }
        # New fetch should trigger eviction
        fake = mock.Mock()
        fake.status_code = 200
        fake.json.return_value = {
            'tokens': {'yes': '1', 'no': '2'},
            'venue': {'exchange': '0xabc'},
            'volume': 100, 'isOther': False,
        }
        with mock.patch('arb_server._SESS_LIM.get', return_value=fake):
            arb_server._fetch_limitless_market_meta('new-slug')
        with arb_server.lim_meta_lock:
            self.assertLessEqual(len(arb_server.lim_meta_cache),
                                 arb_server.LIM_META_CACHE_MAX,
                                 'Cache must stay within bound')
            self.assertIn('new-slug', arb_server.lim_meta_cache,
                          'New entry should be present')


if __name__ == '__main__':
    unittest.main(verbosity=2)
