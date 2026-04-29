"""Phase 9tt — safety-layer regression tests.

Two latent crashes audited 29.04.2026:
  1. _next_utc_midnight crashed on month-end (ValueError day out of range)
  2. is_killed() global declaration was inside except — works in modern
     Python but PEP-8 anti-pattern; moved to top.

Both bugs sat undiscovered because they live in fire_arb's fail-closed
gate which (a) had no tests and (b) only triggers on date arithmetic
edge cases or filesystem errors.
"""
import os
import sys
import time
import unittest
from datetime import datetime, timezone, timedelta
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'Scripts'))


class TestNextUtcMidnightHandlesMonthEnd(unittest.TestCase):
    """_next_utc_midnight must NOT raise on the 31st of a month, on
    Feb 28/29, or on any day where now.day+1 doesn't exist."""

    def _at(self, year, month, day, hour=12):
        """Patch datetime.now(timezone.utc) inside risk.limits."""
        from risk import limits
        fake_now = datetime(year, month, day, hour, 0, 0, tzinfo=timezone.utc)
        return mock.patch.object(limits, 'datetime',
                                 wraps=datetime,
                                 **{'now.return_value': fake_now})

    def test_31st_of_january(self):
        from risk.limits import _next_utc_midnight
        # Without the fix: day=31+1=32 → ValueError
        with mock.patch('risk.limits.datetime') as fake_dt:
            fake_dt.now.return_value = datetime(2026, 1, 31, 12, 0, 0,
                                                 tzinfo=timezone.utc)
            ts = _next_utc_midnight()
        self.assertGreater(ts, 0, '_next_utc_midnight crashed on Jan 31')

    def test_28_feb_non_leap(self):
        from risk.limits import _next_utc_midnight
        # Feb 28 in non-leap year, day+1=29 → ValueError
        with mock.patch('risk.limits.datetime') as fake_dt:
            fake_dt.now.return_value = datetime(2026, 2, 28, 12, 0, 0,
                                                 tzinfo=timezone.utc)
            ts = _next_utc_midnight()
        self.assertGreater(ts, 0, '_next_utc_midnight crashed on Feb 28')

    def test_30_april(self):
        from risk.limits import _next_utc_midnight
        # April has 30 days; day+1=31 → ValueError
        with mock.patch('risk.limits.datetime') as fake_dt:
            fake_dt.now.return_value = datetime(2026, 4, 30, 23, 30, 0,
                                                 tzinfo=timezone.utc)
            ts = _next_utc_midnight()
        self.assertGreater(ts, 0)

    def test_31_dec_year_boundary(self):
        from risk.limits import _next_utc_midnight
        # Dec 31 — day+1 fails AND month+1=13 fails too
        with mock.patch('risk.limits.datetime') as fake_dt:
            fake_dt.now.return_value = datetime(2026, 12, 31, 23, 59, 0,
                                                 tzinfo=timezone.utc)
            ts = _next_utc_midnight()
        self.assertGreater(ts, 0)

    def test_returns_seconds_until_next_midnight(self):
        """Sanity: at noon UTC, next midnight is ~12 hours away."""
        from risk.limits import _next_utc_midnight
        with mock.patch('risk.limits.datetime') as fake_dt:
            fake_dt.now.return_value = datetime(2026, 4, 15, 12, 0, 0,
                                                 tzinfo=timezone.utc)
            with mock.patch('risk.limits.time.time',
                            return_value=time.mktime(
                                datetime(2026, 4, 15, 12, 0, 0).timetuple())):
                ts = _next_utc_midnight()
        # 12:00 → midnight = 43200 seconds
        diff = ts - time.mktime(datetime(2026, 4, 15, 12, 0, 0).timetuple())
        self.assertAlmostEqual(diff, 43200, delta=5)


class TestKillswitchFailClosedOnFsError(unittest.TestCase):
    """When the filesystem call raises (permission denied, disk error),
    is_killed() MUST return True (fail-closed). The global declaration
    placement bug previously had a chance of UnboundLocalError before
    Python 3 normalized the semantics — Phase 9tt moves it to the top
    of the function regardless."""

    def setUp(self):
        from risk import killswitch
        self.killswitch = killswitch
        # Reset the throttle timer so logging fires
        killswitch._last_kill_check_error = 0.0

    def test_fs_error_returns_true(self):
        with mock.patch('risk.killswitch.os.path.exists',
                        side_effect=OSError('disk error')):
            result = self.killswitch.is_killed()
        self.assertTrue(result, 'is_killed must fail-CLOSED on fs error')

    def test_fs_error_records_timestamp(self):
        before = time.time()
        with mock.patch('risk.killswitch.os.path.exists',
                        side_effect=PermissionError('denied')):
            self.killswitch.is_killed()
        # _last_kill_check_error must be set (means the error path executed)
        self.assertGreaterEqual(self.killswitch._last_kill_check_error,
                                before)

    def test_fs_error_logs_only_once_per_minute(self):
        # First call logs; subsequent calls within 60s do NOT
        with mock.patch('risk.killswitch.os.path.exists',
                        side_effect=OSError('e1')):
            with mock.patch.object(self.killswitch.log,
                                    'warning') as mock_log:
                self.killswitch.is_killed()
                self.killswitch.is_killed()
                self.killswitch.is_killed()
        self.assertEqual(mock_log.call_count, 1,
                         'Should throttle to 1 log per 60s')


if __name__ == '__main__':
    unittest.main(verbosity=2)
