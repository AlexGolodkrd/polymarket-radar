"""Flask Blueprint registration for the radar's HTTP API.

Phase audit-28d (27.05.2026) — gradual extraction of endpoint handlers
from the `arb_server.py` monolith into per-concern blueprints. The
monolith calls `register_api_blueprints(app)` at startup; each
blueprint is independently testable.

Pattern for new blueprints:
    from flask import Blueprint
    bp = Blueprint('myname', __name__)

    @bp.route('/api/something')
    def handler():
        return ...

Then add `app.register_blueprint(bp)` inside `register_api_blueprints`.

Blueprints currently extracted:
    - version: /api/version

Blueprints planned (see docs/ARCHITECTURE.md audit-28d):
    - analytics: /api/analytics, /api/history, /api/portfolio_positions
    - admin:     /api/kill, /api/unkill
    - deals:     /api/deals, /api/near, /api/recent_deals, /api/active_deals
    - stats:     /api/scan_health, /api/ts_metrics, /api/pipeline_timings
"""
from __future__ import annotations

from flask import Flask

from radar.api.admin import bp as admin_bp
from radar.api.analytics_api import bp as analytics_bp
from radar.api.deals import bp as deals_bp
from radar.api.paper import bp as paper_bp
from radar.api.stats import bp as stats_bp
from radar.api.version import bp as version_bp
from radar.api.wallets import bp as wallets_bp
from radar.api.ws_health import bp as ws_health_bp


def register_api_blueprints(app: Flask) -> None:
    """Register every blueprint owned by `radar.api`. Idempotent — calling
    twice is a Flask error, so we guard via `app.blueprints`."""
    _blueprints = (
        ('radar_version', version_bp),
        ('radar_analytics_api', analytics_bp),
        ('radar_deals', deals_bp),
        ('radar_admin', admin_bp),
        ('radar_paper', paper_bp),
        ('radar_stats', stats_bp),
        ('radar_ws_health', ws_health_bp),
        ('radar_wallets', wallets_bp),
    )
    for name, bp in _blueprints:
        if name not in app.blueprints:
            app.register_blueprint(bp)
