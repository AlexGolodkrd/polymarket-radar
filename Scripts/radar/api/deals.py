"""Deals-domain HTTP API.

Extracted from arb_server.py in audit-28d (27.05.2026) + audit-28b cont 5
(28.05.2026). Owns:
    GET /api/active_deals     — live snapshot of currently-open deals
    GET /api/recent_deals     — sanitized tail of analytics_events.jsonl
    GET /api/deals            — current scan state (deals/stats/pools) for UI
    GET /api/near             — full NEAR pool snapshot (basic-auth)
    GET /api/recent_near      — public PII-stripped NEAR snapshot

Public-safe: every field on the wire in /api/recent_deals + /api/recent_near
is whitelisted (`ALLOWED_DEAL_FIELDS` / `ALLOWED_NEAR_FIELDS`). Token IDs,
wallet addresses, market hashes, signatures, salts, per-leg detail are
stripped. Operator-side endpoints (/api/deals, /api/near) carry the
raw payloads — nginx basic auth is what protects them.
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


# ── /api/deals — current scan state (operator UI, basic-auth) ────────
@bp.route('/api/deals')
def api_deals() -> Any:
    """Phase 9u (29.04.2026) — non-blocking lock acquire with stale fallback.

    Under heavy WS traffic the scan_lock contends; if we can't acquire
    within 2s we return the previous serialized snapshot (`stale: True`)
    so /api/deals never blocks the dashboard for >2s. Phase 19v16 fixes
    a pre-existing race: jsonify previously iterated `scan_data['deals']`
    OUTSIDE the lock — when run_scan mutated the list concurrently this
    raised 500s with "dictionary changed size during iteration". Now we
    serialize under the lock to a JSON string (atomic snapshot) then
    attach live-only fields (WS metrics, NEAR badge count) outside.
    """
    from arb_server import (
        scan_lock, scan_data, ws_client, lim_ws_client,
        _raw_near_pool_count,
    )
    # NEAR diag stats moved to radar.eval.pools in audit-28b cont 8.
    # Read directly so we see live updates after each near_summary() call.
    from radar.eval import pools as _pools_mod
    _last_visible_near_count = _pools_mod._last_visible_near_count
    _last_near_rejection_stats = _pools_mod._last_near_rejection_stats
    acquired = scan_lock.acquire(timeout=2.0)
    if acquired:
        try:
            payload = json.loads(json.dumps(scan_data, default=str))
        finally:
            scan_lock.release()
        api_deals._last_payload = payload  # stash for next contended caller
    else:
        payload = dict(getattr(api_deals, '_last_payload', None) or {})
        payload['stale'] = True
    if ws_client is not None:
        payload['ws'] = ws_client.get_metrics()
    if lim_ws_client is not None:
        payload['ws_limitless'] = lim_ws_client.get_metrics()
    # Phase 9vv (29.04.2026) — NEAR badge must use cached last-rendered
    # count (post-`_best_near_structure` filter), not raw pool count, so
    # badge matches the visible table rows.
    payload['near_count'] = (
        _last_visible_near_count if _last_visible_near_count is not None
        else _raw_near_pool_count()
    )
    if _last_near_rejection_stats:
        payload['near_diag'] = dict(_last_near_rejection_stats)
    return jsonify(payload)


# ── /api/near — full NEAR snapshot (operator UI) ─────────────────────
@bp.route('/api/near')
def api_near() -> Any:
    """Operator-facing NEAR snapshot. Recomputes near_summary() against the
    current orderbook caches + Polymarket WS books. Carries the raw items
    (no field whitelist) — relies on nginx basic auth for protection."""
    from arb_server import (
        poly_clob_cache, poly_clob_cache_lock,
        res_cache_lock, kalshi_res_cache, sx_res_cache, lim_res_cache,
        ws_client, near_summary, NEAR_BUFFER,
    )
    with poly_clob_cache_lock:
        clob = dict(poly_clob_cache)
    with res_cache_lock:
        ka = dict(kalshi_res_cache)
        sx = dict(sx_res_cache)
        lim = dict(lim_res_cache)
    ws_books: dict[str, Any] = {}
    if ws_client is not None:
        for tid in clob.keys():
            b = ws_client.get_book(tid)
            if b:
                ws_books[tid] = b
    items = near_summary(clob_res=clob, kalshi_res=ka, sx_res=sx,
                          lim_res=lim, ws_books=ws_books)
    return jsonify({
        'count': len(items),
        'buffer_cents': round(NEAR_BUFFER * 100, 1),
        'items': items,
    })


# Phase audit-extras (11.05.2026) — fields safe to expose publicly on
# /api/recent_near. near_summary's natural output is already mostly
# PII-free (no token_ids, no slugs, no marketHashes), but we whitelist
# explicitly so any addition to near_summary is opt-IN to public exposure.
ALLOWED_NEAR_FIELDS = frozenset({
    'platform', 'arb_structure',
    'title',
    'sum_cents', 'distance_cents', 'threshold_cents',
    'outcomes_count', 'min_price_cents', 'max_price_cents',
    'min_liquidity',
    'end_date',
    # NB: 'search_query' is excluded — may leak raw market_name slug
    # patterns we don't want indexable by scrapers.
})


@bp.route('/api/recent_near')
def api_recent_near() -> Any:
    """Public PII-stripped NEAR pool snapshot. Companion to /api/recent_deals.

    nginx whitelists this path identically to /api/recent_deals so the
    maintaining agent can see WHY a deal is hovering near threshold
    (theta, distance_cents, threshold_cents) without basic auth.
    """
    from arb_server import (
        poly_clob_cache, poly_clob_cache_lock,
        res_cache_lock, kalshi_res_cache, sx_res_cache, lim_res_cache,
        ws_client, near_summary, NEAR_BUFFER,
    )
    with poly_clob_cache_lock:
        clob = dict(poly_clob_cache)
    with res_cache_lock:
        ka = dict(kalshi_res_cache)
        sx = dict(sx_res_cache)
        lim = dict(lim_res_cache)
    ws_books: dict[str, Any] = {}
    if ws_client is not None:
        for tid in clob.keys():
            b = ws_client.get_book(tid)
            if b:
                ws_books[tid] = b
    items = near_summary(clob_res=clob, kalshi_res=ka, sx_res=sx,
                          lim_res=lim, ws_books=ws_books)
    sanitized = [
        {k: v for k, v in it.items() if k in ALLOWED_NEAR_FIELDS}
        for it in items
    ]
    return jsonify({
        'count': len(sanitized),
        'buffer_cents': round(NEAR_BUFFER * 100, 1),
        'items': sanitized,
    })
