"""Deals-domain HTTP API.

Extracted from arb_server.py in audit-28d (27.05.2026). Owns:
    GET /api/active_deals     — live snapshot of currently-open deals
    GET /api/recent_deals     — sanitized tail of analytics_events.jsonl

Public-safe: every field on the wire is in `ALLOWED_DEAL_FIELDS`. Token
IDs, wallet addresses, market hashes, signatures, salts, per-leg detail
are stripped. The numbers that are already visible on the dashboard
(sum, net, roi, grade) are preserved.
"""
from __future__ import annotations

import json
import os
from collections import deque
from typing import Any

from flask import Blueprint, jsonify, request

import analytics

bp = Blueprint('radar_deals', __name__)


# Whitelist of fields safe to expose publicly. Add a new field here only
# after confirming it carries no per-wallet / per-order detail.
ALLOWED_DEAL_FIELDS = frozenset({
    # Time + identity
    'type', 'ts', 'key', 'arb_id',
    # Market structure
    'title', 'platform', 'arb_structure', 'cross_structure', 'structure',
    # Economics (already public on the dashboard)
    'sum_cents', 'total_cents', 'threshold_cents',
    'net', 'net_cents',
    'gross', 'gross_pct', 'fee', 'fee_pct',
    'roi', 'adj', 'adj_roi',
    'slip_pct', 'slip_cost',
    # Quality
    'grade', 'min_liq', 'balance_used', 'theta',
    'confidence',
    # Calendar
    'end_date',
    # Lifecycle
    'duration_sec',
    # NEAR-pool snapshots
    'distance_cents', 'outcomes_count', 'min_liquidity',
})


@bp.route('/api/active_deals')
def api_active_deals() -> Any:
    """Real-time arb lifecycle visibility — currently OPEN deals."""
    deals = analytics.live_deals_snapshot()
    sanitized: list[dict[str, Any]] = []
    for d in deals:
        snap = d.get('snapshot') or {}
        clean_snap = {k: v for k, v in snap.items() if k in ALLOWED_DEAL_FIELDS}
        sanitized.append({
            'key': d['key'],
            'opened_ts': d['opened_ts'],
            'first_seen_ts': d['first_seen_ts'],
            'last_seen_ts': d['last_seen_ts'],
            'age_sec': d['age_sec'],
            'consecutive_scans_seen': d['consecutive_scans_seen'],
            'misses': d['misses'],
            **clean_snap,
        })
    return jsonify({'count': len(sanitized), 'deals': sanitized})


@bp.route('/api/recent_deals')
def api_recent_deals() -> Any:
    """Sanitized tail of analytics_events.jsonl.

    Query: limit (default 50, cap 500), type (None | 'opened' | 'closed' | 'near_seen' | 'fire_filled').
    """
    try:
        limit = min(int(request.args.get('limit', 50)), 500)
    except (TypeError, ValueError):
        limit = 50
    type_filter = request.args.get('type')

    rows: deque[dict[str, Any]] = deque(maxlen=limit)
    path = analytics.EVENTS_PATH
    if not os.path.exists(path):
        return jsonify({'count': 0, 'rows': []})
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                if type_filter and ev.get('type') != type_filter:
                    continue
                clean = {k: v for k, v in ev.items() if k in ALLOWED_DEAL_FIELDS}
                rows.append(clean)
    except OSError as e:
        return jsonify({'error': f'read error: {e}'}), 500

    return jsonify({'count': len(rows), 'rows': list(rows)})
