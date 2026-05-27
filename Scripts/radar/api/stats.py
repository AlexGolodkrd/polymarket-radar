"""Stats / observability HTTP API.

Extracted from arb_server.py in audit-28b cont (27.05.2026). Owns:
    GET /api/exchange_rtt       — exchange latency shadow probe (GET RTT)
    GET /api/pipeline_timings   — per-stage latency percentiles from jsonl
    GET /api/circuit_breakers   — current breaker states (CLOSED/OPEN/HALF_OPEN)
    GET /api/cp_pairing_diag    — cross-platform fuzzy-match funnel

All read-only, no shared mutable state. Each handler lazy-imports
its underlying module to avoid circular dependencies.
"""
from __future__ import annotations

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
