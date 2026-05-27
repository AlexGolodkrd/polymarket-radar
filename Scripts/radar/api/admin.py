"""Risk + network HTTP API.

Extracted from arb_server.py in audit-28d (27.05.2026). Owns:
    GET /api/risk_status      — live risk snapshot from `risk` module
    GET /api/network_status   — outbound IP + country + allowed-list check

Both are read-only and pure delegations to the `risk` package. The
HEAVY admin endpoints (/api/kill, /api/unkill, /api/reset) are NOT
moved in this pass — they touch the killswitch file and require
ADMIN_KILL_TOKEN authorisation. They stay in arb_server.py for now and
move in a follow-up PR with their auth wrapper.
"""
from __future__ import annotations

from typing import Any

from flask import Blueprint, jsonify, request

import risk as risk_mod

bp = Blueprint('radar_admin', __name__)


@bp.route('/api/risk_status')
def api_risk_status() -> Any:
    """Live risk snapshot — daily P&L, pause state, kill flag, last reconcile.
    Polled by the dashboard every few seconds for the risk panel."""
    return jsonify(risk_mod.snapshot())


@bp.route('/api/network_status')
def api_network_status() -> Any:
    """Network safety (Layer 3) — outbound IP + country + ALLOWED_COUNTRIES gate.
    `force=1` query param bypasses the IP cache."""
    force = request.args.get('force') == '1'
    if force:
        try:
            risk_mod.get_current_ip_country(force_refresh=True)
        except Exception:
            # network probe is best-effort; don't fail the endpoint
            pass
    return jsonify(risk_mod.network_status())
