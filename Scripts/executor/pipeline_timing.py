"""Pipeline timing instrumentation for paper-trade fires.

Operator request (11.05.2026): "мне нужно ,чтобы ты на каждый тик пепер
трейдинга делал логи, чтобы выяснить медиану моего pipeline, я хочу
знать сколько при любом виде CP сделки времени тратится".

For every fire dispatched via `arb_server._fire_arb_via_ts`, write one
row to `Executions/pipeline_timings.jsonl` capturing the per-stage wall
time. `/api/pipeline_timings` aggregates the last N rows into median /
p50 / p99 per stage so the operator can spot bottlenecks (slow scan
loop, slow TS HTTP, etc.) without parsing JSONL by hand.

Stages tracked (Python-side, dry-run path):

    scan_to_dispatch_ms
        first_seen_ts (when the deal first appeared in radar's scan) →
        dispatch_start (just before POST /fire to TS executor).
        Captures how stale the price snapshot is by the time we fire.

    dispatch_http_ms
        dispatch_start → dispatch_end (the POST /fire roundtrip).
        Includes TS-side build + log + response — the executor's hot path.

    total_pipeline_ms
        first_seen_ts → dispatch_end. Total wall time from first
        sighting to fire completion. This is the primary number the
        operator asked about ("сколько при любом виде CP сделки").

We intentionally do NOT track per-stage TS-side timings yet (build /
post / fill on the TypeScript side) — that needs `executor-ts` changes
and a separate metric channel. Python-side `dispatch_http_ms` is a
useful upper bound for TS work.
"""
import json
import logging
import os
import statistics
import threading
import time
from typing import Optional

log = logging.getLogger(__name__)

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_THIS_DIR, '..', '..'))
EXECUTIONS_DIR = os.path.join(_REPO_ROOT, 'Executions')
PIPELINE_TIMINGS_PATH = os.path.join(EXECUTIONS_DIR, 'pipeline_timings.jsonl')

_log_lock = threading.Lock()


def _ensure_dir():
    os.makedirs(EXECUTIONS_DIR, exist_ok=True)


def log_fire_timing(
    arb_id: str,
    deal: dict,
    *,
    first_seen_ts: Optional[float],
    dispatch_start_ts: float,
    dispatch_end_ts: float,
    response_status: str,
    executor_kind: str = 'ts',
) -> None:
    """Append one timing row. Safe to call from any thread.

    `first_seen_ts` is the analytics-tracked moment the deal first
    appeared in scan output. Pass None if unknown (the row still gets
    written; only the scan_to_dispatch_ms column will be null).

    `response_status` is a short string suitable for filtering: 'ok',
    'http_error', 'fallback_python', 'exception:<Type>'. The /api
    endpoint segments percentiles by status so a flood of `http_error`
    fires doesn't poison the success-path medians.
    """
    try:
        _ensure_dir()
        now_dispatch_ms = (dispatch_end_ts - dispatch_start_ts) * 1000.0
        scan_to_dispatch_ms = (
            (dispatch_start_ts - first_seen_ts) * 1000.0
            if first_seen_ts is not None else None
        )
        total_pipeline_ms = (
            (dispatch_end_ts - first_seen_ts) * 1000.0
            if first_seen_ts is not None else None
        )
        row = {
            'arb_id': arb_id,
            'platform': deal.get('platform'),
            'structure': deal.get('arb_structure'),
            'cross_structure': deal.get('cross_structure'),
            'leg_count': len(deal.get('entries') or []),
            'first_seen_ts': first_seen_ts,
            'dispatch_start_ts': dispatch_start_ts,
            'dispatch_end_ts': dispatch_end_ts,
            'scan_to_dispatch_ms': (round(scan_to_dispatch_ms, 1)
                                     if scan_to_dispatch_ms is not None else None),
            'dispatch_http_ms': round(now_dispatch_ms, 1),
            'total_pipeline_ms': (round(total_pipeline_ms, 1)
                                   if total_pipeline_ms is not None else None),
            'response_status': response_status,
            'executor_kind': executor_kind,
            'ts': time.time(),
        }
        line = json.dumps(row, ensure_ascii=False)
        with _log_lock:
            with open(PIPELINE_TIMINGS_PATH, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
    except Exception as e:
        # Never let timing instrumentation break the fire path.
        log.warning("pipeline_timing write failed for %s: %s", arb_id, e)


def _percentile(sorted_values, pct: float) -> Optional[float]:
    """Nearest-rank percentile on a pre-sorted list. None for empty input."""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = max(0, min(len(sorted_values) - 1,
                    int(round((pct / 100.0) * (len(sorted_values) - 1)))))
    return sorted_values[k]


def aggregate(window_n: int = 200) -> dict:
    """Read the last `window_n` rows and return per-stage percentiles.

    Output shape (each stage object only has the keys with non-null values):
        {
          'count': 187,
          'window_n': 200,
          'stages': {
            'scan_to_dispatch_ms': {p50, p90, p99, mean, count},
            'dispatch_http_ms':    {p50, p90, p99, mean, count},
            'total_pipeline_ms':   {p50, p90, p99, mean, count}
          },
          'by_response_status': {'ok': 173, 'http_error': 14, ...},
          'by_platform': {'Polymarket+SX Bet': 89, ...},
          'by_structure': {'cross_platform': 187},
        }
    """
    if not os.path.exists(PIPELINE_TIMINGS_PATH):
        return {'count': 0, 'window_n': window_n, 'stages': {},
                'by_response_status': {}, 'by_platform': {}, 'by_structure': {}}
    rows = []
    try:
        with open(PIPELINE_TIMINGS_PATH, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        log.warning("pipeline_timings read failed: %s", e)
        return {'count': 0, 'window_n': window_n, 'stages': {},
                'by_response_status': {}, 'by_platform': {}, 'by_structure': {}}
    rows = rows[-window_n:]
    n = len(rows)
    if n == 0:
        return {'count': 0, 'window_n': window_n, 'stages': {},
                'by_response_status': {}, 'by_platform': {}, 'by_structure': {}}

    def _stage(field):
        vals = [r.get(field) for r in rows if isinstance(r.get(field), (int, float))]
        vals_sorted = sorted(vals)
        return {
            'count': len(vals),
            'p50': _percentile(vals_sorted, 50),
            'p90': _percentile(vals_sorted, 90),
            'p99': _percentile(vals_sorted, 99),
            'mean': round(statistics.mean(vals), 1) if vals else None,
            'min': round(vals_sorted[0], 1) if vals else None,
            'max': round(vals_sorted[-1], 1) if vals else None,
        }

    by_status: dict = {}
    by_platform: dict = {}
    by_structure: dict = {}
    for r in rows:
        s = r.get('response_status') or 'unknown'
        by_status[s] = by_status.get(s, 0) + 1
        p = r.get('platform') or 'unknown'
        by_platform[p] = by_platform.get(p, 0) + 1
        st = r.get('structure') or 'unknown'
        by_structure[st] = by_structure.get(st, 0) + 1

    return {
        'count': n,
        'window_n': window_n,
        'stages': {
            'scan_to_dispatch_ms': _stage('scan_to_dispatch_ms'),
            'dispatch_http_ms':    _stage('dispatch_http_ms'),
            'total_pipeline_ms':   _stage('total_pipeline_ms'),
        },
        'by_response_status': by_status,
        'by_platform': by_platform,
        'by_structure': by_structure,
    }
