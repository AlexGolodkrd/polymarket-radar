"""Per-platform event filters — first deliverable of audit-28b (27.05.2026).

Currently extracted:
    radar.filters._helpers — shared date / deadline / grace helpers
    radar.filters.kalshi   — Kalshi event filter (smallest, disabled by default)

Pending (separate PRs, each behind a feature flag for safety):
    radar.filters.polymarket  — biggest filter, touches many caches
    radar.filters.limitless   — depends on Limitless quirks (lim_meta_cache etc.)
    radar.filters.sx          — depends on SX_BINARY_TYPES + has_other_outcome
"""
