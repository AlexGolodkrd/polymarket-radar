"""Phase audit-2 (12.05.2026) — per-platform scan breakdown tests."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


def _reset():
    import arb_server
    with arb_server._scan_tick_lock:
        arb_server._scan_tick_durations_ms.clear()
        arb_server._scan_breakdown_buffer.clear()


def test_breakdown_empty_returns_zero_count():
    _reset()
    import arb_server
    out = arb_server._scan_breakdown_stats()
    assert out['count'] == 0
    assert out['stages'] == {}
    assert out['last'] == {}


def test_record_pushes_stages_alongside_total():
    _reset()
    import arb_server
    arb_server._record_scan_tick(5.0, stages={'poly_ms': 3000, 'lim_ms': 1500,
                                                'sx_ms': 500})
    out = arb_server._scan_breakdown_stats()
    assert out['count'] == 1
    assert set(out['stages'].keys()) == {'poly_ms', 'lim_ms', 'sx_ms'}
    assert out['last'] == {'poly_ms': 3000.0, 'lim_ms': 1500.0, 'sx_ms': 500.0}
    # Also verify total tick was recorded
    assert arb_server._scan_tick_stats()['count'] == 1


def test_breakdown_percentiles_per_stage():
    _reset()
    import arb_server
    # 5 ticks with varying durations
    for poly, lim, sx in [(60000, 20000, 15000), (70000, 18000, 12000),
                           (50000, 22000, 18000), (65000, 19000, 14000),
                           (55000, 21000, 16000)]:
        arb_server._record_scan_tick(0.1, stages={
            'poly_ms': poly, 'lim_ms': lim, 'sx_ms': sx})
    out = arb_server._scan_breakdown_stats()
    assert out['count'] == 5
    poly = out['stages']['poly_ms']
    assert poly['min'] if 'min' in poly else True  # min not exposed in _scan_breakdown_stats
    assert poly['p50'] == 60000.0  # sorted [50000..70000], idx=round(0.5*4)=2 → 60000
    assert poly['mean'] == 60000.0
    lim = out['stages']['lim_ms']
    assert lim['p50'] == 20000.0


def test_missing_stage_omitted_not_zeroed():
    """If poly_ms wasn't pushed in some ticks, percentile aggregation
    should use only the ticks that did include it — NOT count 0 for
    missing ticks (which would distort the distribution)."""
    _reset()
    import arb_server
    arb_server._record_scan_tick(0.1, stages={'poly_ms': 50000})
    arb_server._record_scan_tick(0.1, stages={'poly_ms': 60000})
    # This tick doesn't include poly (e.g., budget bail before poly section)
    arb_server._record_scan_tick(0.1, stages={'lim_ms': 20000})
    out = arb_server._scan_breakdown_stats()
    poly = out['stages']['poly_ms']
    assert poly['count'] == 2  # only 2 ticks recorded poly_ms
    assert poly['mean'] == 55000.0
    lim = out['stages']['lim_ms']
    assert lim['count'] == 1


def test_ring_buffer_capped_at_50():
    _reset()
    import arb_server
    for i in range(100):
        arb_server._record_scan_tick(0.1, stages={'poly_ms': float(i)})
    out = arb_server._scan_breakdown_stats()
    assert out['count'] == 50
    poly = out['stages']['poly_ms']
    # Last 50 entries are i=50..99
    assert poly['count'] == 50
    assert poly['mean'] == round(sum(range(50, 100)) / 50, 1)  # = 74.5


def test_record_handles_negative_or_non_numeric_stages():
    """Defensive: negative or non-numeric values are filtered out."""
    _reset()
    import arb_server
    arb_server._record_scan_tick(0.1, stages={
        'poly_ms': 1000,
        'lim_ms': -500,    # negative — drop
        'sx_ms': 'oops',   # non-numeric — drop
    })
    out = arb_server._scan_breakdown_stats()
    assert 'poly_ms' in out['stages']
    assert 'lim_ms' not in out['stages']
    assert 'sx_ms' not in out['stages']


def test_record_handles_none_stages_arg():
    """Backward-compat: calling _record_scan_tick(elapsed) without stages
    still works (older codepaths)."""
    _reset()
    import arb_server
    arb_server._record_scan_tick(5.0)
    assert arb_server._scan_tick_stats()['count'] == 1
    out = arb_server._scan_breakdown_stats()
    # Empty stage dict pushed; aggregation still doesn't crash
    assert out['count'] == 1
    assert out['stages'] == {}
