"""Tests for scan_data warm-cache persistence (Phase 9n).

Why this exists: cold-start /api/deals returns an empty payload until
the first MAIN scan completes (30-90s). The dashboard then shows
"Запуск сканирования…" indefinitely. Persisting scan_data to disk and
restoring on startup makes the UI show the last-known snapshot
immediately, with a `restored_from_disk` flag so callers can tell
fresh from stale.
"""
import json
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'Scripts'))

import arb_server


class TestScanStatePersist(unittest.TestCase):
    def setUp(self):
        # Use a temp file so we don't trample any real state on the
        # developer's machine. arb_server reads SCAN_STATE_PATH at call
        # time (module-level binding), so monkey-patch.
        self._tmp = tempfile.NamedTemporaryFile(suffix='.json', delete=False)
        self._tmp.close()
        os.unlink(self._tmp.name)  # we want it absent at first
        self._orig_path = arb_server.SCAN_STATE_PATH
        arb_server.SCAN_STATE_PATH = self._tmp.name
        # Clean scan_data before each test
        with arb_server.scan_lock:
            arb_server.scan_data.clear()
            arb_server.scan_data.update({
                "last_scan": None, "scanning": False, "deals": [],
                "quarantine": [], "stats": {}, "error": None, "ws": {},
            })

    def tearDown(self):
        arb_server.SCAN_STATE_PATH = self._orig_path
        if os.path.exists(self._tmp.name):
            os.unlink(self._tmp.name)

    def test_persist_writes_atomic_file(self):
        with arb_server.scan_lock:
            arb_server.scan_data['deals'] = [
                {"title": "T1", "platform": "Polymarket", "net": 0.05,
                 "total_cents": 95.0},
            ]
            arb_server.scan_data['last_scan'] = '2026-04-28T20:00:00+00:00'
            arb_server.scan_data['stats'] = {'arb_found': 1}

        arb_server._persist_scan_state()

        self.assertTrue(os.path.exists(self._tmp.name))
        with open(self._tmp.name, 'r', encoding='utf-8') as f:
            payload = json.load(f)
        self.assertEqual(len(payload['deals']), 1)
        self.assertEqual(payload['deals'][0]['title'], 'T1')
        self.assertEqual(payload['stats']['arb_found'], 1)

    def test_persist_strips_volatile_fields(self):
        with arb_server.scan_lock:
            arb_server.scan_data['scanning'] = True
            arb_server.scan_data['error'] = 'boom'
            arb_server.scan_data['ws'] = {'subs': 42}
            arb_server.scan_data['deals'] = []

        arb_server._persist_scan_state()
        with open(self._tmp.name, 'r', encoding='utf-8') as f:
            payload = json.load(f)
        # Runtime-only fields must not be persisted
        for k in ('scanning', 'error', 'ws', 'ws_limitless', 'near_count'):
            self.assertNotIn(k, payload, f'{k} leaked into persisted state')

    def test_restore_marks_stale_with_age(self):
        # Write a snapshot directly, then restore
        with open(self._tmp.name, 'w', encoding='utf-8') as f:
            json.dump({
                'deals': [{'title': 'cached', 'platform': 'Polymarket',
                           'net': 0.03, 'total_cents': 97.0}],
                'last_scan': '2026-04-28T19:00:00+00:00',
                'stats': {'arb_found': 1},
            }, f)

        # Pretend it was written 5 minutes ago
        five_min_ago = time.time() - 300
        os.utime(self._tmp.name, (five_min_ago, five_min_ago))

        arb_server._restore_scan_state()
        with arb_server.scan_lock:
            self.assertEqual(len(arb_server.scan_data['deals']), 1)
            self.assertEqual(arb_server.scan_data['deals'][0]['title'], 'cached')
            self.assertTrue(arb_server.scan_data.get('restored_from_disk'))
            # Age must be roughly 300s (give or take a few)
            self.assertAlmostEqual(arb_server.scan_data['restored_age_s'],
                                   300, delta=10)

    def test_restore_skips_stale_state_over_24h(self):
        with open(self._tmp.name, 'w', encoding='utf-8') as f:
            json.dump({'deals': [{'title': 'ancient'}], 'stats': {}}, f)
        long_ago = time.time() - (25 * 3600)  # 25 hours
        os.utime(self._tmp.name, (long_ago, long_ago))

        arb_server._restore_scan_state()
        with arb_server.scan_lock:
            # scan_data must NOT be polluted with ancient cache
            self.assertEqual(arb_server.scan_data['deals'], [])
            self.assertNotIn('restored_from_disk', arb_server.scan_data)

    def test_restore_no_file_is_a_noop(self):
        # File doesn't exist — must not raise, must not change scan_data
        self.assertFalse(os.path.exists(self._tmp.name))
        arb_server._restore_scan_state()
        with arb_server.scan_lock:
            self.assertEqual(arb_server.scan_data['deals'], [])
            self.assertNotIn('restored_from_disk', arb_server.scan_data)

    def test_persist_restore_roundtrip_preserves_deals(self):
        sample_deals = [
            {"title": "EPL: Leeds vs Burnley", "platform": "Polymarket",
             "net": 0.012, "total_cents": 98.8, "arb_structure": "all_yes"},
            {"title": "BTC > 100k", "platform": "Limitless",
             "net": 0.025, "total_cents": 97.5, "arb_structure": "yn_pair"},
        ]
        with arb_server.scan_lock:
            arb_server.scan_data['deals'] = sample_deals
            arb_server.scan_data['stats'] = {'arb_found': 2,
                                              'pool_poly_hot': 5}

        arb_server._persist_scan_state()

        # Wipe in-memory state and restore — simulates a fresh process
        with arb_server.scan_lock:
            arb_server.scan_data.clear()
            arb_server.scan_data.update({"deals": [], "stats": {}})
        arb_server._restore_scan_state()

        with arb_server.scan_lock:
            self.assertEqual(len(arb_server.scan_data['deals']), 2)
            self.assertEqual(arb_server.scan_data['deals'][0]['title'],
                             'EPL: Leeds vs Burnley')
            self.assertEqual(arb_server.scan_data['stats']['arb_found'], 2)
            self.assertTrue(arb_server.scan_data.get('restored_from_disk'))


class TestFlaskThreaded(unittest.TestCase):
    def test_app_run_uses_threaded_true(self):
        """Ensure the production launch line keeps threaded=True. Without
        it, the dev WSGI server serializes requests and a long scan blocks
        /api/deals — that was the original 'Сервер недоступен' bug."""
        src_path = os.path.join(HERE, '..', 'Scripts', 'arb_server.py')
        with open(src_path, 'r', encoding='utf-8') as f:
            src = f.read()
        # Find the sole live app.run() line — the one not inside a comment
        live_lines = [ln for ln in src.splitlines()
                      if 'app.run(' in ln and not ln.lstrip().startswith('#')]
        self.assertEqual(len(live_lines), 1, live_lines)
        self.assertIn('threaded=True', live_lines[0])


if __name__ == '__main__':
    unittest.main(verbosity=2)
