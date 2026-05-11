"""Phase audit-2 (11.05.2026) — verify Limitless end_date is ISO,
not raw epoch-ms string.

Operator's dashboard screenshot showed `—` in the "Резолв" column for
every Limitless+SX cross-platform row. Polymarket gives ISO 8601
("2026-05-13T23:00:00Z"); SX builder converts gameTime to ISO via
datetime.fromtimestamp.isoformat(); Limitless used `str(deadline)`
which left a raw "1779103800000" that dashboard's fmtDate can't parse.

The fix in _build_cp_outcomes_limitless coerces epoch-ms (int or
numeric string) to ISO UTC; only passes through when it's already
non-numeric.
"""
import os
import sys
from unittest.mock import MagicMock, patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


def _setup():
    """Importing arb_server triggers heavy startup. We only want the
    helper. Stub heavy deps before import."""
    pass


def test_limitless_deadline_epoch_ms_int_to_iso():
    """Most common Limitless shape: ev.deadline is an int (epoch ms)."""
    import arb_server  # noqa
    # Mock the lim_meta_cache (avoids lock setup detail)
    arb_server.lim_meta_cache.clear()
    out = arb_server._build_cp_outcomes_limitless(
        events=[{
            'title': 'EPL Man City vs Crystal Palace',
            'deadline': 1779103800000,   # epoch ms (int)
            'markets': [{'slug': 'epl-mc-cp-may13',
                          'title': 'Manchester City'}],
        }],
        lim_res={'epl-mc-cp-may13': (0.40, 100, 0.60, 100)},
    )
    assert len(out) == 1
    assert out[0].end_date == '2026-05-18T11:30:00+00:00'


def test_limitless_deadline_epoch_ms_string_to_iso():
    """Alternative shape we observed in production: ev.deadline is a
    numeric STRING. `str(end_date)` left it as-is — we now coerce."""
    import arb_server  # noqa
    arb_server.lim_meta_cache.clear()
    out = arb_server._build_cp_outcomes_limitless(
        events=[{
            'title': 'X', 'deadline': '1779103800000',
            'markets': [{'slug': 's1', 'title': 'A'}],
        }],
        lim_res={'s1': (0.40, 100, 0.60, 100)},
    )
    assert out[0].end_date == '2026-05-18T11:30:00+00:00'


def test_limitless_deadline_none_stays_none():
    """No deadline → end_date stays None (downstream UI handles)."""
    import arb_server  # noqa
    arb_server.lim_meta_cache.clear()
    out = arb_server._build_cp_outcomes_limitless(
        events=[{
            'title': 'X', 'deadline': None,
            'expirationTimestamp': None,
            'markets': [{'slug': 's2', 'title': 'A'}],
        }],
        lim_res={'s2': (0.40, 100, 0.60, 100)},
    )
    assert out[0].end_date is None


def test_limitless_deadline_falls_back_to_expirationTimestamp():
    """Some Limitless events use expirationTimestamp instead of deadline.
    Same epoch-ms semantic — must also be ISO-ified."""
    import arb_server  # noqa
    arb_server.lim_meta_cache.clear()
    out = arb_server._build_cp_outcomes_limitless(
        events=[{
            'title': 'X', 'deadline': None,
            'expirationTimestamp': 1779103800000,
            'markets': [{'slug': 's3', 'title': 'A'}],
        }],
        lim_res={'s3': (0.40, 100, 0.60, 100)},
    )
    assert out[0].end_date == '2026-05-18T11:30:00+00:00'
