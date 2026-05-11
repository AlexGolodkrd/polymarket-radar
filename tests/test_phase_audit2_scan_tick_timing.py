"""Phase audit-2 (11.05.2026) — scan-tick timing in /api/scan_health.

Operator's pain: pipeline_timings shows executor dispatch (~30ms p99)
but that's only the post-detection slice. The dominant latency factor
is the scan tick itself (5-15 seconds typical) since deals can only
be detected after a full poll of Polymarket/Limitless/SX orderbooks.
Adding scan_tick_ms to /api/scan_health gives operators the FULL
latency picture for "time from 0 to executor response".
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


def test_scan_tick_stats_empty_returns_zero_count():
    import arb_server
    # Reset deque for test isolation
    with arb_server._scan_tick_lock:
        arb_server._scan_tick_durations_ms.clear()
    out = arb_server._scan_tick_stats()
    assert out['count'] == 0
    assert out['p50'] is None
    assert out['last'] is None


def test_record_scan_tick_pushes_ms():
    import arb_server
    with arb_server._scan_tick_lock:
        arb_server._scan_tick_durations_ms.clear()
    arb_server._record_scan_tick(5.5)   # 5.5s → 5500.0ms
    arb_server._record_scan_tick(7.2)   # 7.2s → 7200.0ms
    out = arb_server._scan_tick_stats()
    assert out['count'] == 2
    assert out['last'] == 7200.0
    assert out['min'] == 5500.0
    assert out['max'] == 7200.0


def test_scan_tick_percentiles_sane():
    import arb_server
    with arb_server._scan_tick_lock:
        arb_server._scan_tick_durations_ms.clear()
    # Push 10 durations: 1..10 seconds
    for s in range(1, 11):
        arb_server._record_scan_tick(float(s))
    out = arb_server._scan_tick_stats()
    assert out['count'] == 10
    # Nearest-rank percentile on 10-element sorted array
    # [1000..10000]. Python `round(0.5*9)=4` (banker's rounding to
    # even on .5) → sv[4]=5000ms. Operator-meaningful: p50 of small
    # samples is approximate either way.
    assert out['p50'] == 5000.0
    # round(0.9*9)=round(8.1)=8 → sv[8]=9000ms
    assert out['p90'] == 9000.0
    # round(0.99*9)=round(8.91)=9 → sv[9]=10000ms
    assert out['p99'] == 10000.0
    assert out['mean'] == 5500.0
    assert out['last'] == 10000.0


def test_scan_tick_deque_bounded_to_50():
    """Ring buffer prevents unbounded memory growth across long-running
    radar instances."""
    import arb_server
    with arb_server._scan_tick_lock:
        arb_server._scan_tick_durations_ms.clear()
    for s in range(100):
        arb_server._record_scan_tick(float(s))
    out = arb_server._scan_tick_stats()
    # maxlen=50 means only last 50 (values 50..99) are retained
    assert out['count'] == 50
    assert out['min'] == 50000.0   # 50s
    assert out['max'] == 99000.0   # 99s
    assert out['last'] == 99000.0


def test_record_scan_tick_never_raises():
    """Bad input must NOT crash the radar's hot path."""
    import arb_server
    # Pass a junk value — should swallow internally
    arb_server._record_scan_tick(None)
    arb_server._record_scan_tick(float('nan'))   # numeric but weird
    # Sanity: function still works after bad calls
    arb_server._record_scan_tick(3.0)
    out = arb_server._scan_tick_stats()
    assert out['count'] >= 1
