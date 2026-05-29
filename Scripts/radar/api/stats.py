"""Stats / observability HTTP API.

Extracted from arb_server.py in audit-28b cont (27.05.2026). Owns:
    GET /api/exchange_rtt       — exchange latency shadow probe (GET RTT)
    GET /api/pipeline_timings   — per-stage latency percentiles from jsonl
    GET /api/circuit_breakers   — current breaker states (CLOSED/OPEN/HALF_OPEN)
    GET /api/cp_pairing_diag    — cross-platform fuzzy-match funnel
    GET /api/scan_health        — scan loop liveness + pool counts (PII-free)
    GET /api/ts_metrics         — proxy to TS executor /metrics (port 5051)

All read-only, no shared mutable state. Each handler lazy-imports
its underlying module to avoid circular dependencies.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from flask import Blueprint, jsonify, request

bp = Blueprint('radar_stats', __name__)


@bp.route('/api/exchange_rtt')
def api_exchange_rtt() -> Any:
    """Phase audit-2 — GET RTT against each exchange every 60s as a
    lower bound for real-mode POST latency. The response.note field
    warns that this excludes server-side processing time (+100-300ms)."""
    try:
        import exchange_latency_probe as _rtt_probe
        return jsonify(_rtt_probe.stats())
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@bp.route('/api/pipeline_timings')
def api_pipeline_timings() -> Any:
    """Phase audit-2 — per-stage latency p50/p90/p99 from
    Executions/pipeline_timings.jsonl. Breakdown by response_status
    (ok / http_error / exception:*) so a flood of TS errors doesn't
    poison success-path percentiles.

    Query: ?window=N (default 200, cap 5000).
    """
    try:
        n = int(request.args.get('window', '200'))
    except (TypeError, ValueError):
        n = 200
    n = max(1, min(n, 5000))
    try:
        from executor import pipeline_timing
        return jsonify(pipeline_timing.aggregate(window_n=n))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@bp.route('/api/circuit_breakers')
def api_circuit_breakers() -> Any:
    """Per-host circuit breaker states for ops visibility.

    Returns:
        {breakers: [{host, state, failures_count, opened_at, ...}], count: N}
    Or {breakers: [], count: 0, note: ...} if the circuit_breaker module
    isn't loaded.
    """
    try:
        import circuit_breaker
    except ImportError:
        return jsonify({
            'breakers': [],
            'count': 0,
            'note': 'circuit_breaker module not available',
        })
    try:
        breakers = circuit_breaker.all_breakers()
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    out: list[dict[str, Any]] = []
    for name, cb in (breakers or {}).items():
        try:
            state = cb._state.name if hasattr(cb._state, 'name') else str(cb._state)
        except Exception:
            state = 'unknown'
        out.append({
            'host': name,
            'state': state,
            'failures_count': getattr(cb, '_failure_count', 0),
            'opened_at': getattr(cb, '_opened_at', None),
        })
    return jsonify({'breakers': out, 'count': len(out)})


@bp.route('/api/cp_pairing_diag')
def api_cp_pairing_diag() -> Any:
    """Phase audit — SZ-4 blind-spot fix. Cross-platform fuzzy-match
    funnel counts: pool sizes → matched pairs → same-platform rejects
    → settlement-timing rejects → built deals."""
    try:
        import cross_platform as _cp
        return jsonify(_cp.get_pairing_diag())
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@bp.route('/api/scan_health')
def api_scan_health() -> Any:
    """Public scan health snapshot (PII-free).

    Phase audit (11.05.2026) — closes blind-spot SZ-2 from PROJECT_AUDIT.
    Operator + maintaining agent probes this without basic-auth to
    answer: "is the scanner alive and how busy is it right now?"

    Returns last_scan_iso, age_sec, scanning/progress/error, scan_tick_ms
    percentiles + per-platform breakdown, plus a whitelist of pool count
    stats. Strategy/PII fields (deals, token IDs, market hashes) are
    intentionally excluded.

    Query: ?series=1 — include raw chronological samples (~+500B).
    """
    from arb_server import (
        scan_lock, scan_data,
        _scan_tick_stats, _scan_breakdown_stats,
    )
    with scan_lock:
        last_iso = scan_data.get('last_scan')
        stats = dict(scan_data.get('stats') or {})
        scanning = bool(scan_data.get('scanning'))
        progress = scan_data.get('progress')
        error = scan_data.get('error')
    age_sec: float | None = None
    if last_iso:
        try:
            last_dt = datetime.fromisoformat(
                last_iso.replace('Z', '+00:00')
            )
            age_sec = round(
                (datetime.now(timezone.utc) - last_dt).total_seconds(), 1
            )
        except (ValueError, TypeError):
            age_sec = None
    safe_stats = {
        k: v for k, v in stats.items() if k in {
            'arb_found',
            'cross_platform_count',
            'pool_poly_hot', 'pool_poly_near',
            'pool_kalshi_hot', 'pool_kalshi_near',
            'pool_sx_hot', 'pool_sx_near',
            'pool_lim_hot', 'pool_lim_near',
            'poly_events', 'lim_events',
        }
    }
    want_series = request.args.get('series', '0') == '1'
    return jsonify({
        'last_scan_iso': last_iso,
        'last_scan_age_sec': age_sec,
        'scanning': scanning,
        'progress': progress,
        'error': error,
        'scan_tick_ms': _scan_tick_stats(include_series=want_series),
        'scan_breakdown_ms': _scan_breakdown_stats(include_series=want_series),
        **safe_stats,
    })


@bp.route('/api/ts_metrics')
def api_ts_metrics() -> Any:
    """Phase TS-5-audit (11.05.2026) — public proxy to TS executor /metrics.

    Closes TS-5 audit blind-spot #4: TS executor's :5051 /metrics is only
    reachable inside the docker network. This proxy exposes it on the
    public dashboard. The body is PII-free by design (TS executor never
    serialises key material — keys live in a module-scoped Map).

    Behaviour:
        - reachable → TS executor's body verbatim with that status code
        - unreachable → 503 with {error, reachable: false, ts_url}
    """
    import requests as _req
    ts_url = os.environ.get('EXECUTOR_URL', 'http://executor-ts:5051').rstrip('/')
    try:
        r = _req.get(f'{ts_url}/metrics', timeout=3)
        try:
            body = r.json()
        except Exception:
            body = {'_raw': r.text[:500]}
        return jsonify(body), r.status_code
    except _req.exceptions.ConnectionError:
        return jsonify({
            'error': 'TS executor unreachable',
            'reachable': False,
            'ts_url': ts_url,
        }), 503
    except Exception as e:
        return jsonify({
            'error': f'proxy failed: {e!r}',
            'reachable': False,
            'ts_url': ts_url,
        }), 503
