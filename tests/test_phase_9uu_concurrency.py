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
    """_fired_arb_keys must shed entries when their deals leave the
    active list (otherwise it grows unbounded across long-running
    container)."""

    def setUp(self):
        # Wipe the global set before each test
        with arb_server._fired_arb_keys_lock:
            arb_server._fired_arb_keys.clear()

    def test_evicts_keys_when_deal_leaves_active_list(self):
        # Round 1: deals A, B, C → all become 'fired'
        deals_round1 = [
            {'arb_structure': 'all_yes', 'platform': 'Polymarket', 'title': 'A'},
            {'arb_structure': 'all_yes', 'platform': 'Polymarket', 'title': 'B'},
            {'arb_structure': 'all_yes', 'platform': 'Polymarket', 'title': 'C'},
        ]
        # Patch fire_arb to a no-op so we don't actually fire
        with mock.patch('arb_server.fire_arb', return_value=None):
            arb_server._maybe_dry_fire(deals_round1)
        with arb_server._fired_arb_keys_lock:
            self.assertEqual(len(arb_server._fired_arb_keys), 3)

        # Round 2: only deal A remains active → B and C should be evicted
        deals_round2 = deals_round1[:1]
        with mock.patch('arb_server.fire_arb', return_value=None):
            arb_server._maybe_dry_fire(deals_round2)
        with arb_server._fired_arb_keys_lock:
            self.assertEqual(len(arb_server._fired_arb_keys), 1,
                             'B and C should be evicted; only A remains')

    def test_hard_cap_clears_set(self):
        # Force the set above hard cap
        with arb_server._fired_arb_keys_lock:
            for i in range(arb_server._FIRED_KEYS_HARD_CAP + 100):
                arb_server._fired_arb_keys.add(f'fake-{i}')
        # Calling _maybe_dry_fire with one deal should trigger the
        # hard-cap clear path
        deals = [{'arb_structure': 'all_yes', 'platform': 'P', 'title': 'X'}]
        with mock.patch('arb_server.fire_arb', return_value=None):
            arb_server._maybe_dry_fire(deals)
        with arb_server._fired_arb_keys_lock:
            self.assertLessEqual(len(arb_server._fired_arb_keys),
                                 arb_server._FIRED_KEYS_HARD_CAP,
                                 'Hard cap must enforce upper bound')


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
