"""Paper-trading HTTP API.

Extracted from arb_server.py in audit-28b cont (27.05.2026). Owns:
    GET /api/paper_stats
    GET /api/graduation
    GET /api/paper_distribution
    GET /api/paper_skip_reasons
    GET /api/graduation_history

All five are pure read-only delegations to the `paper_trading` module
+ the legacy `paper_stats()` aggregator that lives on arb_server. No
shared mutable state — safe to delegate via blueprint.
"""
from __future__ import annotations

from typing import Any

from flask import Blueprint, jsonify, request

import paper_trading

bp = Blueprint('radar_paper', __name__)


def _paper_stats_window(window_n: int) -> dict[str, Any]:
    """Lazy proxy to arb_server.paper_stats() — that function lives in
    the monolith for now; lazy import avoids circular dep at module load."""
    try:
        from arb_server import paper_stats
        return paper_stats(window_n=window_n)
    except Exception as e:
        return {'error': f'paper_stats unavailable: {e}'}


@bp.route('/api/paper_stats')
def api_paper_stats() -> Any:
    """Rolling stats from Executions/paper_results.jsonl. Used by the
    dashboard's paper-trade panel and the Phase 5 graduation gate."""
    try:
        n = int(request.args.get('window', '100'))
    except (TypeError, ValueError):
        n = 100
    return jsonify(_paper_stats_window(n))


@bp.route('/api/graduation')
def api_graduation() -> Any:
    """Graduation gate status — count, win rate, drift, blockers,
    ready flag. Dashboard renders the 🎓 banner from this."""
    return jsonify(paper_trading.graduation_status().to_dict())


@bp.route('/api/paper_distribution')
def api_paper_distribution() -> Any:
    """P&L histogram bins for the Analytics tab chart."""
    try:
        n = int(request.args.get('window', '500'))
    except (TypeError, ValueError):
        n = 500
    return jsonify(paper_trading.paper_distribution(window_n=n))


@bp.route('/api/paper_skip_reasons')
def api_paper_skip_reasons() -> Any:
    """Phase audit (11.05.2026) — SZ-3 blind-spot fix. Distribution of
    skip reasons across the last `window` paper-trade rows so the
    operator can spot when one platform dominates aborts."""
    try:
        n = int(request.args.get('window', '500'))
    except (TypeError, ValueError):
        n = 500
    return jsonify(paper_trading.paper_skip_reasons(window_n=n))


@bp.route('/api/graduation_history')
def api_graduation_history() -> Any:
    """Daily rolling win rate / drift for the last N days — time-series
    so the operator sees the trajectory toward graduation."""
    try:
        days = int(request.args.get('days', '14'))
    except (TypeError, ValueError):
        days = 14
    return jsonify({'days': paper_trading.graduation_history(days=days)})
