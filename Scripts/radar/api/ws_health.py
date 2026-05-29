"""WS health blueprints — Limitless + Polymarket user-channel WS metrics.

Extracted from arb_server.py in audit-28b cont 4 (28.05.2026). Both
endpoints proxy `get_metrics()` from the WS client + add an `enabled`
gate so disabled platforms (no WS) return a soft 200 instead of 500.

Uses lazy import от arb_server для WS clients + env flags — нет
cyclic-deps at module load time.
"""
from __future__ import annotations

from typing import Any

from flask import Blueprint, jsonify

bp = Blueprint('radar_ws_health', __name__)


@bp.route('/api/lim_ws_health')
def api_lim_ws_health() -> Any:
    """Limitless WS metrics: connection, sub counts, handshake p50/p99,
    consecutive_failures, long_pause_count. Operator uses to decide if
    WS stable enough для `LIMITLESS_WS_REQUIRED=1`."""
    try:
        from arb_server import lim_ws_client, ENABLE_LIMITLESS, LIMITLESS_WS_REQUIRED
    except Exception as e:
        return jsonify({'enabled': False, 'reason': f'import failed: {e}'}), 500
    if lim_ws_client is None:
        return jsonify({
            'enabled': False,
            'reason': ('ENABLE_LIMITLESS_WS=0' if ENABLE_LIMITLESS
                       else 'ENABLE_LIMITLESS=0'),
            'required_mode': LIMITLESS_WS_REQUIRED,
        })
    try:
        metrics = lim_ws_client.get_metrics()
    except Exception as e:
        return jsonify({'enabled': True, 'error': str(e)}), 500
    payload: dict[str, Any] = {'enabled': True, 'required_mode': LIMITLESS_WS_REQUIRED}
    payload.update(metrics)
    return jsonify(payload)


@bp.route('/api/poly_ws_health')
def api_poly_ws_health() -> Any:
    """Polymarket WS metrics: connection, subs, msg/sec, reconnects.
    Operator uses to decide `POLYMARKET_WS_REQUIRED=1` (strict WS-only)."""
    try:
        from arb_server import ws_client, POLYMARKET_WS_REQUIRED
    except Exception as e:
        return jsonify({'enabled': False, 'reason': f'import failed: {e}'}), 500
    if ws_client is None:
        return jsonify({
            'enabled': False,
            'reason': 'ws_client not initialized (ENABLE_POLY=0 or bootstrap incomplete)',
            'required_mode': POLYMARKET_WS_REQUIRED,
        })
    try:
        metrics = ws_client.get_metrics()
    except Exception as e:
        return jsonify({'enabled': True, 'error': str(e)}), 500
    payload: dict[str, Any] = {'enabled': True, 'required_mode': POLYMARKET_WS_REQUIRED}
    payload.update(metrics)
    return jsonify(payload)
