"""Phase audit-2 (11.05.2026) — pipeline_timing instrumentation tests.

Verifies the timing logger writes rows correctly and the aggregate()
function returns sensible percentiles. These tests are pure-Python and
do not require docker / TS executor / network.

Operator's pain: needs to know median pipeline latency per stage for
any CP fire ("сколько при любом виде CP сделки времени тратится").
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


def test_log_fire_timing_writes_row(monkeypatch, tmp_path):
    """Row has all expected fields with correct ms math."""
    from executor import pipeline_timing
    monkeypatch.setattr(pipeline_timing, 'PIPELINE_TIMINGS_PATH',
                        str(tmp_path / 'pipeline_timings.jsonl'))
    deal = {
        'arb_id': 'test-1',
        'platform': 'Polymarket+SX Bet',
        'arb_structure': 'cross_platform',
        'entries': [{}, {}, {}],
    }
    pipeline_timing.log_fire_timing(
        arb_id='test-1', deal=deal,
        first_seen_ts=1000.0,
        dispatch_start_ts=1003.0,
        dispatch_end_ts=1003.250,
        response_status='ok',
    )
    with open(pipeline_timing.PIPELINE_TIMINGS_PATH) as f:
        row = json.loads(f.readline())
    assert row['arb_id'] == 'test-1'
    assert row['platform'] == 'Polymarket+SX Bet'
    assert row['structure'] == 'cross_platform'
    assert row['leg_count'] == 3
    assert row['scan_to_dispatch_ms'] == 3000.0   # (1003 - 1000) * 1000
    assert row['dispatch_http_ms'] == 250.0       # (1003.250 - 1003) * 1000
    assert row['total_pipeline_ms'] == 3250.0
    assert row['response_status'] == 'ok'


def test_log_fire_timing_null_first_seen(monkeypatch, tmp_path):
    """When first_seen_ts is None, scan_to_dispatch + total are null."""
    from executor import pipeline_timing
    monkeypatch.setattr(pipeline_timing, 'PIPELINE_TIMINGS_PATH',
                        str(tmp_path / 'pipeline_timings.jsonl'))
    pipeline_timing.log_fire_timing(
        arb_id='test-2',
        deal={'platform': 'Polymarket', 'entries': []},
        first_seen_ts=None,
        dispatch_start_ts=2000.0,
        dispatch_end_ts=2000.1,
        response_status='ok',
    )
    with open(pipeline_timing.PIPELINE_TIMINGS_PATH) as f:
        row = json.loads(f.readline())
    assert row['scan_to_dispatch_ms'] is None
    assert row['total_pipeline_ms'] is None
    assert row['dispatch_http_ms'] == 100.0   # still computed


def test_log_fire_timing_never_raises(monkeypatch, tmp_path):
    """Failure to write must NOT propagate — timing instrumentation
    must never break the fire path. We point at an unwritable path
    and verify the call returns silently."""
    from executor import pipeline_timing
    # Point at a path that can't be created (parent doesn't exist
    # AND we can't create it because the would-be parent is a file).
    bad_parent = tmp_path / 'not_a_dir.txt'
    bad_parent.write_text('blocking file')
    bad_path = bad_parent / 'pipeline_timings.jsonl'
    monkeypatch.setattr(pipeline_timing, 'PIPELINE_TIMINGS_PATH', str(bad_path))
    # Should NOT raise
    pipeline_timing.log_fire_timing(
        arb_id='test-3',
        deal={'platform': 'X', 'entries': []},
        first_seen_ts=None,
        dispatch_start_ts=0.0, dispatch_end_ts=0.001,
        response_status='ok',
    )


def test_aggregate_empty_returns_zero(monkeypatch, tmp_path):
    """No file = zero count, no stages — never crashes."""
    from executor import pipeline_timing
    monkeypatch.setattr(pipeline_timing, 'PIPELINE_TIMINGS_PATH',
                        str(tmp_path / 'nonexistent.jsonl'))
    out = pipeline_timing.aggregate(window_n=100)
    assert out['count'] == 0
    assert out['stages'] == {}


def test_aggregate_percentiles_sane(monkeypatch, tmp_path):
    """5 rows with dispatch_http_ms = [100, 200, 300, 400, 500]:
    p50=300, p90=500 (nearest-rank), p99=500. Mean=300."""
    from executor import pipeline_timing
    monkeypatch.setattr(pipeline_timing, 'PIPELINE_TIMINGS_PATH',
                        str(tmp_path / 'pipeline_timings.jsonl'))
    for i, ms in enumerate([100, 200, 300, 400, 500]):
        pipeline_timing.log_fire_timing(
            arb_id=f'r{i}',
            deal={'platform': 'Polymarket+SX Bet',
                  'arb_structure': 'cross_platform', 'entries': []},
            first_seen_ts=1000.0,
            dispatch_start_ts=1000.0,
            dispatch_end_ts=1000.0 + ms / 1000.0,
            response_status='ok',
        )
    out = pipeline_timing.aggregate(window_n=100)
    assert out['count'] == 5
    stage = out['stages']['dispatch_http_ms']
    assert stage['count'] == 5
    assert stage['p50'] == 300.0
    assert stage['min'] == 100.0
    assert stage['max'] == 500.0
    assert stage['mean'] == 300.0
    assert out['by_response_status'] == {'ok': 5}
    assert out['by_platform'] == {'Polymarket+SX Bet': 5}


def test_aggregate_segments_by_status(monkeypatch, tmp_path):
    """ok and http_error rows go to separate buckets in by_response_status."""
    from executor import pipeline_timing
    monkeypatch.setattr(pipeline_timing, 'PIPELINE_TIMINGS_PATH',
                        str(tmp_path / 'pipeline_timings.jsonl'))
    for status in ['ok', 'ok', 'http_error', 'exception:ConnectionError', 'ok']:
        pipeline_timing.log_fire_timing(
            arb_id='r', deal={'platform': 'P', 'entries': []},
            first_seen_ts=None,
            dispatch_start_ts=0.0, dispatch_end_ts=0.001,
            response_status=status,
        )
    out = pipeline_timing.aggregate(window_n=100)
    assert out['by_response_status']['ok'] == 3
    assert out['by_response_status']['http_error'] == 1
    assert out['by_response_status']['exception:ConnectionError'] == 1


def test_aggregate_window_n_truncates(monkeypatch, tmp_path):
    """Window applies tail-N: only the last `window_n` rows are aggregated."""
    from executor import pipeline_timing
    monkeypatch.setattr(pipeline_timing, 'PIPELINE_TIMINGS_PATH',
                        str(tmp_path / 'pipeline_timings.jsonl'))
    # Write 10 rows; aggregate over last 3 should only see rows 7,8,9
    for i in range(10):
        pipeline_timing.log_fire_timing(
            arb_id=f'r{i}', deal={'platform': 'P', 'entries': []},
            first_seen_ts=None,
            dispatch_start_ts=0.0, dispatch_end_ts=(i + 1) / 1000.0,
            response_status='ok',
        )
    out = pipeline_timing.aggregate(window_n=3)
    assert out['count'] == 3
    stage = out['stages']['dispatch_http_ms']
    # Last 3 rows: 8, 9, 10 ms
    assert stage['min'] == 8.0
    assert stage['max'] == 10.0


def test_aggregate_handles_corrupt_lines(monkeypatch, tmp_path):
    """Lines that fail JSON parsing are skipped silently, rest aggregated."""
    from executor import pipeline_timing
    path = tmp_path / 'pipeline_timings.jsonl'
    monkeypatch.setattr(pipeline_timing, 'PIPELINE_TIMINGS_PATH', str(path))
    pipeline_timing.log_fire_timing(
        arb_id='ok', deal={'platform': 'P', 'entries': []},
        first_seen_ts=None, dispatch_start_ts=0, dispatch_end_ts=0.1,
        response_status='ok',
    )
    # Append corrupt junk
    with open(path, 'a') as f:
        f.write('NOT JSON\n')
        f.write('{"partial":\n')   # invalid
        f.write('\n')               # blank line
    pipeline_timing.log_fire_timing(
        arb_id='ok2', deal={'platform': 'P', 'entries': []},
        first_seen_ts=None, dispatch_start_ts=0, dispatch_end_ts=0.2,
        response_status='ok',
    )
    out = pipeline_timing.aggregate(window_n=100)
    assert out['count'] == 2


def test_get_first_seen_ts_returns_none_for_unknown():
    """analytics.get_first_seen_ts returns None for keys not in
    open-deals tracker. This is the helper called by _fire_arb_via_ts."""
    import analytics
    assert analytics.get_first_seen_ts('::unknown::::') is None


def test_get_first_seen_ts_returns_value_when_tracked(monkeypatch):
    """When a deal is in _open_deals, get_first_seen_ts returns its
    first_seen_ts. Verified via update_from_scan injection."""
    import analytics
    # Use a unique key so we don't collide with anything real
    fake_key = '::pipeline_timing_test::cross_platform::cross_platform'
    with analytics._lock:
        analytics._open_deals[fake_key] = {
            'opened_ts': 1234.0,
            'first_seen_ts': 1234.5,
            'last_seen_ts': 1234.0,
            'consecutive_scans_seen': 1,
            'misses': 0,
            'snapshot': {},
        }
    try:
        assert analytics.get_first_seen_ts(fake_key) == 1234.5
    finally:
        with analytics._lock:
            analytics._open_deals.pop(fake_key, None)
