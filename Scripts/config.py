"""Centralised runtime configuration for the plan-kapkan radar.

Single source of truth for every env-driven knob the radar reads. Replaces
the previous pattern of `os.environ.get('FOO', 'default')` scattered
across 19 files and ~125 call sites.

Why centralise:
    1. Type-safety — env strings are coerced to int/float/bool via pydantic
       validators; bogus values fail fast at startup, not 6 hours into a
       scan loop with a NoneType cryptic traceback.
    2. Discoverability — `grep "class RadarConfig" Scripts/config.py`
       lists every tunable in 80 lines, vs grepping `os.environ` in 19
       files.
    3. Hot-reload friendly — when we move to runtime config later, the
       only thing that changes is `RadarConfig.from_env()` → polling.
    4. Documented defaults — every field has a docstring with prior-art
       (which PR introduced it, what value tripped 429 / phantom etc.).

Usage:
    from config import config
    # config is a module-level singleton, instantiated once at import.
    if config.fire_cooldown_s > 0:
        ...

If you need a fresh instance (e.g. tests that monkeypatch env):
    cfg = RadarConfig()  # re-reads os.environ

NEVER mutate `config` at runtime; create a new instance instead. Mutation
defeats the type validators and races with readers across threads.
"""
from __future__ import annotations

import os
from typing import Optional

try:
    from pydantic import Field, field_validator
    from pydantic_settings import BaseSettings, SettingsConfigDict
    _PYDANTIC_AVAILABLE = True
except ImportError:
    # Pydantic v2 not installed yet — fall back to a thin dataclass so the
    # radar boots in environments where deps haven't been refreshed. The
    # fallback path mirrors only the most-used fields; if a field isn't
    # here, callers get the legacy os.environ.get behaviour via the
    # `_env` helper at the bottom of this module.
    _PYDANTIC_AVAILABLE = False


def _env(key: str, default: str = '') -> str:
    """Raw env lookup with logging-free fallback. Use ONLY in legacy
    code paths; new code must read `config.X`."""
    return os.environ.get(key, default)


if _PYDANTIC_AVAILABLE:

    class RadarConfig(BaseSettings):
        """Every env-driven knob lives here. Add a field with a sensible
        default + a docstring referencing the originating PR or skill.

        Defaults reflect Phase audit-27.05 (27.05.2026) baseline.
        """

        model_config = SettingsConfigDict(
            env_file='Credentials.env',
            env_file_encoding='utf-8',
            extra='ignore',          # tolerate unknown vars (legacy)
            case_sensitive=False,
        )

        # ── Runtime mode ──────────────────────────────────────────
        dry_run: bool = Field(
            default=True,
            description="If False (DRY_RUN=0), executor POSTs real orders. "
                        "Operator gates this manually post-graduation.",
        )
        log_level: str = Field(
            default='INFO',
            description="Python root-logger level. INFO in prod, DEBUG only when chasing a bug.",
        )

        # ── Platform toggles ──────────────────────────────────────
        enable_poly: bool = Field(default=True, description="Polymarket fetch + eval")
        enable_limitless: bool = Field(default=True, description="Limitless fetch + eval")
        enable_sx: bool = Field(default=True, description="SX Bet fetch + eval")
        enable_kalshi: bool = Field(
            default=False,
            description="Kalshi disabled by default since PR #177 — geo-blocked from non-US VPS",
        )

        # ── Polymarket ────────────────────────────────────────────
        poly_main_pages: int = Field(
            default=10,
            ge=1, le=30,
            description="Pages of /events to fetch per main scan (500/page → 5000 events)",
        )
        poly_chunk_pages: int = Field(default=2, ge=1, description="Pages per chunk for progressive scan UI")

        # ── Limitless ─────────────────────────────────────────────
        limitless_main_pages: int = Field(
            default=25,
            ge=1, le=100,
            description="Reduced 40 → 25 in PR #179 to tame 429s on our VPS IP",
        )
        limitless_page_size: int = Field(default=25, ge=1, le=100, description="API max = 100")
        limitless_chunk_pages: int = Field(default=4, ge=1)
        limitless_page_concurrent: int = Field(
            default=8, ge=1, le=20,
            description="Only used if async_fetch=True. 8 was last safe value before 429.",
        )
        limitless_ob_concurrent: int = Field(default=12, ge=1, le=50)

        async_fetch: bool = Field(
            default=False,
            description="HARD-LEARNED: keep False on current VPS IP. Even 8-concurrent trips Limitless 429.",
        )

        # ── Cross-platform ────────────────────────────────────────
        cross_platform_enabled: bool = Field(default=True)
        cross_platform_threshold: float = Field(default=0.96, gt=0.0, lt=1.0)

        # ── Executor / firing ─────────────────────────────────────
        executor_url: str = Field(
            default='http://executor-ts:5051',
            description="TS executor service URL. Empty = use legacy Python in-process.",
        )
        max_per_trade_usd: float = Field(default=5.0, gt=0.0, description="Per-leg cap (PR #28 fix: per-leg not sum)")
        min_net_per_arb_usd: float = Field(default=0.50, ge=0.0, description="Threshold for fire (net of fees)")
        slippage_tolerance: float = Field(default=0.005, ge=0.0, le=0.5)
        depth_recheck_enabled: bool = Field(default=True)

        # ── Dedup / cooldown ──────────────────────────────────────
        fire_cooldown_s: int = Field(
            default=1800,
            ge=0, le=86400,
            description="Phase audit-27.05 — TTL for _fired_arb_keys. 30 min default; "
                        "kills the 18-fires-in-1h re-detection loop captured on screenshot.",
        )
        close_grace_scans: int = Field(
            default=10,
            ge=1, le=100,
            description="Phase audit-27.05 — analytics consecutive-miss grace. Bumped 3→10 "
                        "(p50 scan_tick=30s → 5 min grace) to suppress dashboard 18-row spam.",
        )

        # ── Persistence paths ─────────────────────────────────────
        executions_dir: Optional[str] = Field(
            default=None,
            description="Override the Executions/ root. Tests use tmp_path; prod stays default.",
        )

        # ── Risk (limits.py loads these too) ──────────────────────
        daily_loss_limit_usd: float = Field(default=35.0, gt=0.0)
        losing_trades_per_hour_limit: int = Field(default=5, ge=1)

        # ── Operational / admin ───────────────────────────────────
        admin_kill_token: str = Field(default='', description="Required by /api/kill when set")
        github_token: str = Field(
            default='',
            description="repo:write PAT for deploy.yml + session agent PR creation. "
                        "Rotate every 90d; should be a fine-grained PAT scoped to this repo only.",
        )

        # ── Telegram alerts ───────────────────────────────────────
        telegram_bot_token: str = Field(default='')
        telegram_chat_id: str = Field(default='')
        arb_alert_min_net_usd: float = Field(default=10.0, ge=0.0)

        # ── Validators ────────────────────────────────────────────
        @field_validator('log_level')
        @classmethod
        def _log_level_uppercase(cls, v: str) -> str:
            return v.upper() if v else 'INFO'

        @field_validator('executor_url')
        @classmethod
        def _strip_executor_url(cls, v: str) -> str:
            return (v or '').strip()

else:
    # Pydantic not installed — minimal duck-typed config that supports the
    # most-used attributes. Reading any field that isn't here falls back
    # to os.environ.get() via __getattr__.
    class RadarConfig:  # type: ignore[no-redef]
        """Fallback config without pydantic. Coerces strings to int/bool
        for known fields; everything else returns the raw env string."""

        def __init__(self) -> None:
            self.dry_run = _env('DRY_RUN', '1').strip() != '0'
            self.log_level = _env('LOG_LEVEL', 'INFO').upper() or 'INFO'

            self.enable_poly = _env('ENABLE_POLY', '1') == '1'
            self.enable_limitless = _env('ENABLE_LIMITLESS', '1') == '1'
            self.enable_sx = _env('ENABLE_SX', '1') == '1'
            self.enable_kalshi = _env('ENABLE_KALSHI', '0') == '1'

            self.poly_main_pages = int(_env('POLY_MAIN_PAGES', '10'))
            self.poly_chunk_pages = int(_env('POLY_CHUNK_PAGES', '2'))

            self.limitless_main_pages = int(_env('LIMITLESS_MAIN_PAGES', '25'))
            self.limitless_page_size = int(_env('LIMITLESS_PAGE_SIZE', '25'))
            self.limitless_chunk_pages = int(_env('LIMITLESS_CHUNK_PAGES', '4'))
            self.limitless_page_concurrent = int(_env('LIMITLESS_PAGE_CONCURRENT', '8'))
            self.limitless_ob_concurrent = int(_env('LIMITLESS_OB_CONCURRENT', '12'))

            self.async_fetch = _env('ASYNC_FETCH', '').strip() == '1'

            self.cross_platform_enabled = _env('CROSS_PLATFORM_ENABLED', '1') == '1'
            self.cross_platform_threshold = float(_env('CROSS_PLATFORM_THRESHOLD', '0.96'))

            self.executor_url = _env('EXECUTOR_URL', 'http://executor-ts:5051').strip()
            self.max_per_trade_usd = float(_env('MAX_PER_TRADE_USD', '5'))
            self.min_net_per_arb_usd = float(_env('MIN_NET_PER_ARB_USD', '0.50'))
            self.slippage_tolerance = float(_env('SLIPPAGE_TOLERANCE', '0.005'))
            self.depth_recheck_enabled = _env('DEPTH_RECHECK_ENABLED', '1') == '1'

            self.fire_cooldown_s = int(_env('FIRE_COOLDOWN_S', '1800'))
            self.close_grace_scans = int(_env('CLOSE_GRACE_SCANS', '10'))

            self.executions_dir = _env('EXECUTIONS_DIR', '') or None

            self.daily_loss_limit_usd = float(_env('DAILY_LOSS_LIMIT_USD', '35'))
            self.losing_trades_per_hour_limit = int(_env('LOSING_TRADES_PER_HOUR_LIMIT', '5'))

            self.admin_kill_token = _env('ADMIN_KILL_TOKEN', '')
            self.github_token = _env('GITHUB_TOKEN', '')

            self.telegram_bot_token = _env('TELEGRAM_BOT_TOKEN', '')
            self.telegram_chat_id = _env('TELEGRAM_CHAT_ID', '')
            self.arb_alert_min_net_usd = float(_env('ARB_ALERT_MIN_NET_USD', '10'))


# Module-level singleton. Imported as: `from config import config`
config: RadarConfig = RadarConfig()


def reload() -> RadarConfig:
    """Re-instantiate from current os.environ. Tests use this when they
    monkeypatch env vars and need the config to reflect the change."""
    global config
    config = RadarConfig()
    return config
