"""Phase TS-5a sparkline (12.05.2026) — opt-in raw series in /api/scan_health.

`_scan_tick_stats(include_series=True)` and `_scan_breakdown_stats(
include_series=True)` add a `series` field with chronological values for
each metric. `/api/scan_health?series=1` opts into this. Default off
keeps the response identical to the baseline for cheap-polling
consumers.

Use case: dashboard renders an inline SVG sparkline next to the
scan_tick stat card without needing a separate endpoint.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


def _reset():
    import arb_server
    with arb_server._scan_tick_lock:
        arb_server._scan_tick_durations_ms.clear()
        arb_server._scan_breakdown_buffer.clear()


def test_scan_tick_stats_default_no_series_field():
    """Backwards-compat: default call returns no `series` key at all."""
    _reset()
    import arb_server
    arb_server._record_scan_tick(5.0)
    out = arb_server._scan_tick_stats()
    assert 'series' not in out


def test_scan_tick_stats_include_series_returns_list():
    _reset()
    import arb_server
    arb_server._record_scan_tick(5.0)
    arb_server._record_scan_tick(6.0)
    arb_server._record_scan_tick(7.0)
    out = arb_server._scan_tick_stats(include_series=True)
    assert out['count'] == 3
    assert out['series'] == [5000.0, 6000.0, 7000.0]


def test_scan_tick_stats_empty_with_series_is_empty_list():
    _reset()
    import arb_server
    out = arb_server._scan_tick_stats(include_series=True)
    assert out['count'] == 0
    assert out['series'] == []


def test_scan_tick_stats_series_chronological_not_sorted():
    """Critical: the series must preserve INSERTION order (chronological)
    so sparkline renders the trend correctly. p50/min/max etc. operate on
    sorted values, but `series` must NOT be sorted."""
    _reset()
    import arb_server
    arb_server._record_scan_tick(10.0)
    arb_server._record_scan_tick(2.0)
    arb_server._record_scan_tick(15.0)
    out = arb_server._scan_tick_stats(include_series=True)
    # If sorted, would be [2000, 10000, 15000]. Chronological is the
    # insertion order:
    assert out['series'] == [10000.0, 2000.0, 15000.0]
    # And p50 still computed on the sorted view:
    assert out['p50'] == 10000.0


def test_scan_breakdown_default_no_series_field():
    _reset()
    import arb_server
    arb_server._record_scan_tick(5.0, stages={'poly_ms': 1000.0})
    out = arb_server._scan_breakdown_stats()
    assert 'series' not in out['stages']['poly_ms']


def test_scan_breakdown_include_series_per_stage():
    _reset()
    import arb_server
    arb_server._record_scan_tick(5.0, stages={'poly_ms': 1000.0,
                                                'lim_ms': 2000.0})
    arb_server._record_scan_tick(5.0, stages={'poly_ms': 1500.0,
                                                'lim_ms': 2500.0,
                                                'sx_ms': 500.0})
    out = arb_server._scan_breakdown_stats(include_series=True)
    poly = out['stages']['poly_ms']
    assert poly['series'] == [1000.0, 1500.0]
    lim = out['stages']['lim_ms']
    assert lim['series'] == [2000.0, 2500.0]
    sx = out['stages']['sx_ms']
    # sx only appeared in the second tick — series is sparse.
    assert sx['series'] == [500.0]


def test_api_scan_health_no_series_param_omits_field():
    _reset()
    import arb_server
    arb_server._record_scan_tick(5.0, stages={'poly_ms': 1000.0})
    with arb_server.app.test_client() as c:
        r = c.get('/api/scan_health')
        assert r.status_code == 200
        body = r.get_json()
        assert 'series' not in body['scan_tick_ms']
        assert 'series' not in body['scan_breakdown_ms']['stages']['poly_ms']


def test_api_scan_health_series_param_includes_field():
    _reset()
    import arb_server
    arb_server._record_scan_tick(5.0, stages={'poly_ms': 1000.0})
    arb_server._record_scan_tick(6.0, stages={'poly_ms': 1500.0})
    with arb_server.app.test_client() as c:
        r = c.get('/api/scan_health?series=1')
        assert r.status_code == 200
        body = r.get_json()
        assert body['scan_tick_ms']['series'] == [5000.0, 6000.0]
        assert body['scan_breakdown_ms']['stages']['poly_ms']['series'] \
            == [1000.0, 1500.0]


def test_existing_scan_health_tests_still_pass_through_series_param():
    """Sanity — adding `?series=1` doesn't change other response fields."""
    _reset()
    import arb_server
    arb_server._record_scan_tick(5.0)
    with arb_server.app.test_client() as c:
        r_no = c.get('/api/scan_health').get_json()
        r_yes = c.get('/api/scan_health?series=1').get_json()
    # Same top-level keys; same stat values except for the added series.
    assert set(r_no.keys()) == set(r_yes.keys())
    for k in ('count', 'p50', 'mean', 'last'):
        assert r_no['scan_tick_ms'][k] == r_yes['scan_tick_ms'][k]
