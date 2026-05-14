"""Phase audit-3 (12.05.2026) — async prefetch consume-time bug.

When ASYNC_FETCH=1, SX/Limitless prefetch runs in BG pool in parallel
with Polymarket processing. By the time the main scan reaches the SX
block, the future is already resolved. The naive pattern

    t_sx = time.time()
    result = future.result()   # instant
    t_sx = time.time() - t_sx  # only microseconds elapsed

reports sx_ms ≈ 0 in /api/scan_health.scan_breakdown_ms.

Fix in arb_server.py: capture `_sx_submit_ts` at submit; rewind `t_sx`
to that timestamp after consuming the future. Final subtraction then
gives submit→consume wall-clock — a meaningful "how long was this
stage's work happening" metric (vs. literally µs of dict lookup).
"""
import time
from concurrent.futures import ThreadPoolExecutor


def _slow_bg_work():
    """Mimics SX fetch — does real work for ~50ms."""
    time.sleep(0.05)
    return ('markets', 200, None)


def test_bug_naive_consume_reports_near_zero():
    """Reproduces the bug: section-elapsed measurement of a finished
    future gives near-zero ms, which is what operator saw in production
    when ASYNC_FETCH=1."""
    pool = ThreadPoolExecutor(max_workers=1)
    fut = pool.submit(_slow_bg_work)
    # Simulate main scan being slow on Polymarket — future finishes early.
    time.sleep(0.1)
    assert fut.done(), "test setup: future should be resolved"

    # === Naive (buggy) pattern ===
    t_sx = time.time()
    _ = fut.result(timeout=1)
    t_sx_elapsed_ms = (time.time() - t_sx) * 1000.0

    pool.shutdown(wait=True)
    # Consume of a done future is microseconds — rounds to 0 in ms.
    assert t_sx_elapsed_ms < 5.0, (
        f"naive consume should be near-zero, got {t_sx_elapsed_ms}ms")


def test_fix_submit_ts_rewind_reports_actual_wallclock():
    """The fix: rewind t_sx to the submit timestamp. Final subtraction
    yields submit→consume wall-clock, which reflects the actual BG
    work duration when the BG worker started immediately on submit."""
    pool = ThreadPoolExecutor(max_workers=1)
    _sx_submit_ts = time.time()
    fut = pool.submit(_slow_bg_work)
    time.sleep(0.1)
    assert fut.done()

    # === Fixed pattern ===
    t_sx = time.time()             # would-be naive baseline
    _ = fut.result(timeout=1)
    if _sx_submit_ts is not None:  # rewind step from arb_server.py
        t_sx = _sx_submit_ts
    t_sx_elapsed_ms = (time.time() - t_sx) * 1000.0

    pool.shutdown(wait=True)
    # ≥100ms since submit (50ms BG work + 100ms outer sleep).
    assert t_sx_elapsed_ms >= 100.0, (
        f"rewound t_sx should report ≥100ms, got {t_sx_elapsed_ms}ms")


def test_sync_path_unaffected_when_submit_ts_none():
    """If ASYNC_FETCH=0, `_sx_submit_ts` stays None and the rewind step
    is skipped. The section-elapsed measurement (which IS the actual
    sync fetch duration) is preserved."""
    _sx_submit_ts = None
    fut = None  # no BG future in sync mode

    t_sx = time.time()
    time.sleep(0.03)  # sync SX fetch happens here
    # Rewind condition (matches arb_server.py): only if both future
    # consumed AND submit_ts captured.
    if fut is not None and _sx_submit_ts is not None:
        t_sx = _sx_submit_ts
    t_sx_elapsed_ms = (time.time() - t_sx) * 1000.0

    # Sync mode: still reports the actual sleep duration (~30ms).
    assert 25.0 <= t_sx_elapsed_ms < 100.0, (
        f"sync-path measurement should be ~30ms, got {t_sx_elapsed_ms}ms")


def test_record_scan_tick_accepts_nonzero_sx_ms():
    """End-to-end: after the fix, _record_scan_tick receives a non-zero
    sx_ms and surfaces it through _scan_breakdown_stats."""
    import os
    import sys
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.join(os.path.dirname(here), 'Scripts'))
    import arb_server

    with arb_server._scan_tick_lock:
        arb_server._scan_tick_durations_ms.clear()
        arb_server._scan_breakdown_buffer.clear()

    # Simulate one fixed-up tick: t_sx = 4.5s (typical real value, was 0)
    arb_server._record_scan_tick(40.0, stages={
        'poly_ms': 12000.0, 'lim_ms': 20000.0, 'sx_ms': 4500.0})
    out = arb_server._scan_breakdown_stats()
    assert 'sx_ms' in out['stages']
    assert out['stages']['sx_ms']['last'] == 4500.0
    assert out['last']['sx_ms'] == 4500.0
