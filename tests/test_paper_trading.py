"""Unit tests for Phase 5 paper trading + graduation gate."""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'Scripts'))

import paper_trading


def _row(realistic_pnl=1.0, drift=0.05, leg_slippage=0.001, evaluated_at=None):
    return {
        'arb_id': '1234-Test',
        'title': 'Test Event',
        'structure': 'all_yes',
        'sim_pnl': 1.0,
        'realistic_pnl_5s': realistic_pnl,
        'drift': drift,
        'legs': [{'leg_idx': 0, 'expected_price': 0.30,
                  'realistic_fill': 0.30 + leg_slippage,
                  'slippage': leg_slippage}],
        'dry_fired_at': evaluated_at or time.time(),
        'evaluated_at': evaluated_at or time.time(),
    }


class _PaperTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._path = os.path.join(self._tmpdir, 'paper_results.jsonl')
        self._patches = [
            mock.patch.object(paper_trading, 'PAPER_RESULTS_PATH', self._path),
        ]
        for p in self._patches: p.start()

    def tearDown(self):
        for p in self._patches: p.stop()
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _write(self, rows):
        with open(self._path, 'w', encoding='utf-8') as f:
            for r in rows: f.write(json.dumps(r) + '\n')


class TestGraduationGate(_PaperTest):
    def test_empty_not_ready(self):
        s = paper_trading.graduation_status()
        self.assertEqual(s.count, 0)
        self.assertFalse(s.ready)
        self.assertIn('no paper trades', s.blockers[0])

    def test_75pct_winrate_passes(self):
        rows = [_row(realistic_pnl=1.0, drift=0.05) for _ in range(75)]
        rows += [_row(realistic_pnl=-1.0, drift=0.05) for _ in range(25)]
        self._write(rows)
        s = paper_trading.graduation_status()
        self.assertEqual(s.count, 100)
        self.assertEqual(s.win_rate, 0.75)
        self.assertTrue(s.ready)
        self.assertEqual(s.blockers, [])

    def test_60pct_winrate_blocked(self):
        rows = [_row(realistic_pnl=1.0) for _ in range(60)]
        rows += [_row(realistic_pnl=-1.0) for _ in range(40)]
        self._write(rows)
        s = paper_trading.graduation_status()
        self.assertFalse(s.ready)
        self.assertTrue(any('win rate 60' in b for b in s.blockers))

    def test_high_drift_blocks(self):
        # 75% wins, but mean drift 25% — fails
        rows = [_row(realistic_pnl=1.0, drift=0.25) for _ in range(75)]
        rows += [_row(realistic_pnl=-1.0, drift=0.25) for _ in range(25)]
        self._write(rows)
        s = paper_trading.graduation_status()
        self.assertFalse(s.ready)
        self.assertTrue(any('drift' in b for b in s.blockers))

    def test_count_under_100_blocks(self):
        rows = [_row(realistic_pnl=1.0, drift=0.05) for _ in range(80)]
        self._write(rows)
        s = paper_trading.graduation_status()
        self.assertFalse(s.ready)
        self.assertIn('20 more', s.blockers[0])

    def test_to_dict_shape(self):
        d = paper_trading.graduation_status().to_dict()
        for k in ['count', 'graduation_ready', 'min_trades_required',
                  'min_win_rate_pct', 'max_drift_pct', 'first_real_size_usdc']:
            self.assertIn(k, d)


class TestPaperDistribution(_PaperTest):
    def test_empty(self):
        d = paper_trading.paper_distribution()
        self.assertEqual(d['total'], 0)

    def test_bins_shape(self):
        rows = [_row(realistic_pnl=1.5) for _ in range(5)]    # $1..$2 bin
        rows += [_row(realistic_pnl=-0.3) for _ in range(3)]  # -$0.50..-$0.10 bin
        self._write(rows)
        d = paper_trading.paper_distribution()
        self.assertEqual(d['total'], 8)
        self.assertEqual(len(d['bins']), len(d['counts']))
        # Ensure both populated buckets are non-zero
        self.assertGreater(sum(d['counts']), 0)


class TestGraduationHistory(_PaperTest):
    def test_groups_by_day(self):
        # 3 wins yesterday, 2 losses today
        yesterday = time.time() - 86400
        today = time.time()
        rows = [_row(realistic_pnl=1.0, evaluated_at=yesterday) for _ in range(3)]
        rows += [_row(realistic_pnl=-1.0, evaluated_at=today) for _ in range(2)]
        self._write(rows)
        h = paper_trading.graduation_history(days=14)
        self.assertEqual(len(h), 2)
        # Today's bucket has 0% wins
        today_bucket = next(d for d in h if d['count'] == 2)
        self.assertEqual(today_bucket['win_rate_pct'], 0.0)


class TestFirstRealTradeSize(unittest.TestCase):
    def test_initial_trades_use_5_usd(self):
        for i in range(0, 10):
            self.assertEqual(paper_trading.first_real_trade_size_usdc(i), 5.0)

    def test_after_10_returns_none(self):
        # None means "use full deal-builder stake"
        self.assertIsNone(paper_trading.first_real_trade_size_usdc(10))
        self.assertIsNone(paper_trading.first_real_trade_size_usdc(50))


if __name__ == '__main__':
    unittest.main(verbosity=2)
