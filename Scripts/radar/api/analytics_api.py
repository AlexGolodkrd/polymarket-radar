"""Analytics-domain HTTP API.

Extracted from arb_server.py in audit-28d (27.05.2026). Owns:
    GET /api/analytics              — aggregate stats over a period
    GET /api/analytics/history      — per-trade list, paginated, filtered
    GET /api/portfolio_positions    — open + resolved positions from fire_filled events

All handlers are pure functions of:
    - the analytics module (in-memory state + analytics_events.jsonl file)
    - Flask request query params
No coupling to arb_server globals (scan_data, _fired_arb_keys, etc.).
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

from flask import Blueprint, jsonify, request

import analytics

bp = Blueprint('radar_analytics_api', __name__)


@bp.route('/api/analytics')
def api_analytics() -> Any:
    """GET /api/analytics?period=day|week|month|all"""
    period = (request.args.get('period') or 'month').lower()
    if period not in ('day', 'week', 'month', 'all'):
        period = 'month'
    return jsonify(analytics.aggregate(period))


@bp.route('/api/analytics/history')
def api_analytics_history() -> Any:
    """GET /api/analytics/history — per-trade history, newest first, paginated.

    Query: period=day|week|month|all, limit (cap 1000), offset, platform,
    structure, min_net.
    """
    period = (request.args.get('period') or 'all').lower()
    if period not in ('day', 'week', 'month', 'all'):
        period = 'all'
    try:
        limit = max(1, min(int(request.args.get('limit', '100')), 1000))
    except (TypeError, ValueError):
        limit = 100
    try:
        offset = max(0, int(request.args.get('offset', '0')))
    except (TypeError, ValueError):
        offset = 0
    try:
        min_net = float(request.args.get('min_net', '0'))
    except (TypeError, ValueError):
        min_net = 0.0
    platform = request.args.get('platform') or None
    structure = request.args.get('structure') or None
    return jsonify(analytics.history(
        period=period, limit=limit, offset=offset,
        platform=platform, structure=structure, min_net=min_net,
    ))


# ── /api/portfolio_positions ─────────────────────────────────────

def _parse_end_date(ed: Any) -> Optional[datetime]:
    """Return a UTC datetime or None. Accepts ISO-8601 (Z or +00:00),
    Unix seconds, or pre-formatted 'May 17, 2026' (best-effort)."""
    if ed is None:
        return None
    if isinstance(ed, (int, float)):
        try:
            return datetime.fromtimestamp(float(ed), tz=timezone.utc)
        except (OSError, ValueError, OverflowError):
            return None
    if isinstance(ed, str):
        s = ed.strip()
        if not s:
            return None
        try:
            return datetime.fromisoformat(s.replace('Z', '+00:00'))
        except ValueError:
            pass
        for fmt in ('%B %d, %Y', '%b %d, %Y', '%Y-%m-%d'):
            try:
                return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


_ID_FIELDS = ('slug', 'market_hash', 'outcome_index',
              'condition_id', 'token_id',
              'token_id_yes', 'token_id_no',
              'neg_risk', 'sport_type', 'side')


@bp.route('/api/portfolio_positions')
def api_portfolio_positions() -> Any:
    """Phase audit-4 (15.05.2026) — open + resolved positions, per (title, platform, side).

    Reads `fire_filled` events from analytics_events.jsonl. SPLITS by
    end_date: future / unknown → open, past → resolved. Each position
    carries per-leg identifiers so the dashboard JS can resolve live
    state per platform and compute Real P&L client-side.
    """
    by_key: dict = defaultdict(lambda: {
        'total_size_usdc': 0.0,
        'fill_prices': [],
        'arb_ids': [],
        'first_ts': None,
        'last_ts': None,
        'end_date': None,
        'ids': {},
        'arb_structure': None,
    })
    if os.path.exists(analytics.EVENTS_PATH):
        with open(analytics.EVENTS_PATH, 'r', encoding='utf-8') as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                if ev.get('type') != 'fire_filled':
                    continue
                arb_id = ev.get('arb_id')
                ts = ev.get('ts')
                ev_end_date = ev.get('end_date')
                ev_structure = ev.get('arb_structure')
                for leg in (ev.get('legs') or []):
                    if (leg.get('fill_size_usdc') or 0) <= 0:
                        continue
                    platform = leg.get('platform', '?')
                    note = leg.get('note') or ''
                    slug = leg.get('slug') or ''
                    key = (ev.get('title') or '?', platform, note or slug)
                    entry = by_key[key]
                    entry['total_size_usdc'] += float(leg.get('fill_size_usdc') or 0)
                    if leg.get('fill_price') is not None:
                        entry['fill_prices'].append(float(leg['fill_price']))
                    entry['arb_ids'].append(arb_id)
                    if entry['first_ts'] is None or (ts and ts < entry['first_ts']):
                        entry['first_ts'] = ts
                    if entry['last_ts'] is None or (ts and ts > entry['last_ts']):
                        entry['last_ts'] = ts
                    if entry['end_date'] is None and ev_end_date:
                        entry['end_date'] = ev_end_date
                    if entry['arb_structure'] is None and ev_structure:
                        entry['arb_structure'] = ev_structure
                    for field in _ID_FIELDS:
                        val = leg.get(field)
                        if val is not None and field not in entry['ids']:
                            entry['ids'][field] = val

    now_utc = datetime.now(timezone.utc)
    open_positions: list[dict[str, Any]] = []
    resolved_positions: list[dict[str, Any]] = []
    for (title, platform, note), v in by_key.items():
        avg_price = (sum(v['fill_prices']) / len(v['fill_prices'])) if v['fill_prices'] else None
        end_dt = _parse_end_date(v['end_date'])
        pos: dict[str, Any] = {
            'title': title,
            'platform': platform,
            'side': v['ids'].get('side') or note,
            'total_size_usdc': round(v['total_size_usdc'], 4),
            'avg_fill_price': round(avg_price, 4) if avg_price is not None else None,
            'contracts': (round(v['total_size_usdc'] / avg_price, 2)
                          if avg_price else None),
            'fire_count': len(v['arb_ids']),
            'first_ts': v['first_ts'],
            'last_ts': v['last_ts'],
            'arb_ids': v['arb_ids'],
            'end_date': v['end_date'],
            'arb_structure': v['arb_structure'],
            'ids': v['ids'],
        }
        # Open = end_date in the future, OR no end_date known (defensive
        # default — don't accidentally hide a position with bad metadata).
        if end_dt is None or end_dt > now_utc:
            open_positions.append(pos)
        else:
            resolved_positions.append(pos)
    open_positions.sort(key=lambda p: (p['title'], p['platform'], p['side']))
    resolved_positions.sort(
        key=lambda p: (p.get('end_date') or '', p['title']), reverse=True)

    return jsonify({
        'open': {
            'count': len(open_positions),
            'total_cost_usdc': round(sum(p['total_size_usdc'] for p in open_positions), 4),
            'positions': open_positions,
        },
        'resolved': {
            'count': len(resolved_positions),
            'total_cost_usdc': round(sum(p['total_size_usdc'] for p in resolved_positions), 4),
            'positions': resolved_positions,
        },
        # Backwards-compatible flat fields — clients that only read
        # `count`/`total_cost_usdc`/`positions` (pre-audit-4) see the
        # UNION (open + resolved) so they don't break mid-deploy.
        'count': len(open_positions) + len(resolved_positions),
        'total_cost_usdc': round(
            sum(p['total_size_usdc'] for p in open_positions)
            + sum(p['total_size_usdc'] for p in resolved_positions), 4),
        'positions': open_positions + resolved_positions,
    })
