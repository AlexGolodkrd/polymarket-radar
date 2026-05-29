"""Risk + admin HTTP API.

Extracted from arb_server.py in audit-28d (27.05.2026) + audit-28b cont 5
(28.05.2026). Owns:
    GET  /api/risk_status         — live risk snapshot from `risk` module
    GET  /api/network_status      — outbound IP + country + allowed-list
    POST /api/scan                — manual scan trigger (fire-and-forget)
    POST /api/approve             — add title to scan whitelist
    POST /api/reject              — add title to scan blacklist
    POST /api/analytics/reset     — truncate analytics jsonl + reset state
    POST /api/kill                — trip killswitch (requires {confirm:'YES'})
    POST /api/risk_resume         — clear kill + active pause
    POST /api/dryfire             — manually trigger dry-fire on a deal title

Auth model: nginx basic auth in production. /api/kill ALSO supports an
optional X-Admin-Token header check via ADMIN_KILL_TOKEN env (defense
in depth — if radar is ever exposed without nginx, kill stays authed).
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any

from flask import Blueprint, jsonify, request

import risk as risk_mod

bp = Blueprint('radar_admin', __name__)

log = logging.getLogger('arb_server')


# Phase 9uu (29.04.2026) — bounds-check to prevent unbounded growth of the
# operator-facing whitelist/blacklist sets. A loop of 1M unique titles
# would otherwise eat memory unnoticed.
APPROVE_LIST_HARD_CAP = 2000
TITLE_MAX_LEN = 500


def _admin_token() -> str:
    """Read ADMIN_KILL_TOKEN lazily.

    Priority order:
      1. `arb_server.ADMIN_KILL_TOKEN` — legacy tests use
         `mock.patch.object(arb_server, 'ADMIN_KILL_TOKEN', ...)`. Honour
         that path so the extraction is transparent.
      2. Env var ADMIN_KILL_TOKEN — production/normal path. arb_server
         primes its module-level constant from this env on import, so
         in real use the two paths agree.
    """
    try:
        from arb_server import ADMIN_KILL_TOKEN as _t
        return (_t or '').strip()
    except Exception:
        return os.environ.get('ADMIN_KILL_TOKEN', '').strip()


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
            pass
    return jsonify(risk_mod.network_status())


@bp.route('/api/scan', methods=['POST'])
def api_scan() -> Any:
    """Trigger a manual scan in a daemon thread. Fire-and-forget — caller
    polls /api/deals or /api/scan_health for progress."""
    from arb_server import run_scan
    threading.Thread(target=run_scan, daemon=True).start()
    return jsonify({"status": "scan_started"})


@bp.route('/api/approve', methods=['POST'])
def api_approve() -> Any:
    """Add a market title to the scan-time whitelist."""
    from arb_server import scan_lock, whitelist
    payload = request.get_json(silent=True) or {}
    title = payload.get('title')
    if not isinstance(title, str):
        return jsonify({"status": "bad_request"}), 400
    title = title.strip()[:TITLE_MAX_LEN]
    if not title:
        return jsonify({"status": "empty_title"}), 400
    with scan_lock:
        if len(whitelist) >= APPROVE_LIST_HARD_CAP:
            return jsonify({"status": "list_full",
                            "limit": APPROVE_LIST_HARD_CAP}), 429
        whitelist.add(title)
    return jsonify({"status": "approved"})


@bp.route('/api/reject', methods=['POST'])
def api_reject() -> Any:
    """Add a market title to the scan-time blacklist."""
    from arb_server import scan_lock, blacklist
    payload = request.get_json(silent=True) or {}
    title = payload.get('title')
    if not isinstance(title, str):
        return jsonify({"status": "bad_request"}), 400
    title = title.strip()[:TITLE_MAX_LEN]
    if not title:
        return jsonify({"status": "empty_title"}), 400
    with scan_lock:
        if len(blacklist) >= APPROVE_LIST_HARD_CAP:
            return jsonify({"status": "list_full",
                            "limit": APPROVE_LIST_HARD_CAP}), 429
        blacklist.add(title)
    return jsonify({"status": "rejected"})


@bp.route('/api/analytics/reset', methods=['POST'])
def api_analytics_reset() -> Any:
    """Phase 17 (01.05.2026) — operator-requested clean baseline.

    Truncates analytics_events.jsonl, dryrun.jsonl, paper_results.jsonl,
    analytics_state.json, price_history.jsonl AND resets in-memory analytics
    state. Used after deploying new code so paper-trade collection starts
    from zero (old buggy data doesn't poison metrics)."""
    import analytics
    here = os.path.dirname(os.path.abspath(__file__))
    # api/admin.py → radar/api → radar → Scripts → repo_root
    repo_root = os.path.normpath(os.path.join(here, '..', '..', '..'))
    exec_dir = os.path.join(repo_root, 'Executions')
    targets = [
        'analytics_events.jsonl',
        'analytics_state.json',
        'dryrun.jsonl',
        'paper_results.jsonl',
        'price_history.jsonl',
    ]
    reset = []
    for fname in targets:
        path = os.path.join(exec_dir, fname)
        if os.path.exists(path):
            try:
                with open(path, 'w', encoding='utf-8'):
                    pass
                reset.append(fname)
            except OSError as e:
                log.warning("analytics reset %s failed: %s", fname, e)
    try:
        if hasattr(analytics, 'reset_state'):
            analytics.reset_state()
    except Exception:
        pass
    return jsonify({'reset': reset, 'count': len(reset)})


@bp.route('/api/kill', methods=['POST'])
def api_kill() -> Any:
    """Trip the kill switch.

    Body MUST include {confirm: 'YES'} — server-side double-confirm
    guard against misclicked dev curl. Phase 9uu: optional X-Admin-Token
    header check on top of nginx basic auth (defense in depth).
    """
    token = _admin_token()
    if token:
        provided = request.headers.get('X-Admin-Token', '')
        import hmac
        if not hmac.compare_digest(provided, token):
            return jsonify({'status': 'unauthorized',
                            'reason': 'X-Admin-Token header missing or wrong'}), 401
    body = request.get_json(silent=True) or {}
    if body.get('confirm') != 'YES':
        return jsonify({'status': 'error',
                        'reason': 'must POST {"confirm": "YES", "reason": "..."} '
                                  'to confirm kill — guards against accidental clicks'}), 400
    reason = str(body.get('reason') or 'manual_dashboard')[:200]
    info = risk_mod.kill(reason=reason)
    return jsonify({'status': 'killed', 'flag': info})


@bp.route('/api/risk_resume', methods=['POST'])
def api_risk_resume() -> Any:
    """Clear the kill switch and any active pause. Operator-only —
    used after investigating a reconcile mismatch or daily-limit pause."""
    body = request.get_json(silent=True) or {}
    if body.get('confirm') != 'YES':
        return jsonify({'status': 'error',
                        'reason': 'must POST {"confirm": "YES"} to confirm resume'}), 400
    was_killed = risk_mod.unkill(reason=body.get('reason') or 'manual_resume')
    s = risk_mod.get_state()
    s.paused_until_unix = None
    s.paused_reason = None
    risk_mod.save_state(s)
    return jsonify({'status': 'resumed', 'was_killed': was_killed})


@bp.route('/api/dryfire', methods=['POST'])
def api_dryfire() -> Any:
    """Manually trigger a dry-fire on a deal by title. Useful for ad-hoc
    testing — auto-fire already handles new arbs, but a manual trigger
    lets the user re-fire to re-evaluate realistic slippage on demand."""
    from arb_server import scan_lock, scan_data, fire_arb, _DRY_RUN_WALLETS
    body = request.get_json(silent=True) or {}
    title = body.get('title')
    if not title:
        return jsonify({'status': 'error', 'reason': 'title required'}), 400
    with scan_lock:
        deals = list(scan_data.get('deals') or [])
    matches = [d for d in deals if d.get('title') == title]
    if not matches:
        return jsonify({'status': 'error',
                        'reason': f'no deal matches title {title!r}'}), 404
    fired = []
    for d in matches:
        try:
            r = fire_arb(d, wallets=_DRY_RUN_WALLETS, dry_run=True)
            fired.append({'arb_id': r.arb_id, 'structure': r.deal_structure,
                          'leg_count': len(r.legs), 'aborted': r.aborted_reason})
        except Exception as e:
            fired.append({'error': str(e)})
    return jsonify({'status': 'ok', 'fired': fired})
