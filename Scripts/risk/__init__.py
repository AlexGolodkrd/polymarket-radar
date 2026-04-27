"""Risk management layer for the executor (Phase 3 — PR #14).

Module split — each file owns one concern, all wired together via state.py:

    state.py       — single source of truth for the live risk state:
                     daily P&L, rolling losing-trade window, paused_until,
                     killed flag, position log. Persists to
                     Executions/risk_state.json so a process restart picks
                     up mid-day limits without losing the day.
    limits.py      — record_pnl(), check_can_fire(): the only function the
                     executor needs to call before fire_arb. Returns
                     (allowed: bool, reason: Optional[str]).
    killswitch.py  — file-flag based kill switch (Executions/.killed). Two
                     processes can read it: the main radar (denies new
                     fires) and a watchdog (cancels pending orders even
                     if the radar has crashed).
    reconcile.py   — every 60s, fetch live positions from each exchange's
                     /positions endpoint and compare to local positions
                     log. Mismatch > $0.01 → halt + alert.

Parameter defaults match feedback memory `feedback_risk_params.md`:
    MAX_PER_TRADE_USD       = 55.0
    DAILY_LOSS_LIMIT_USD    = 35.0   (resets at 00:00 UTC)
    LOSING_TRADES_PER_HOUR  = 5      (rolling)
    PAUSE_AFTER_HOURLY_LIMIT_S = 3600
    NO concurrent-position cap, NO repeat-arb-per-event cap.

On any pause/kill: existing positions are NEVER closed automatically.
Only NEW fires are blocked. Reconcile and fill listeners keep running.
"""
from .state import (
    RiskState, get_state, save_state, load_state,
    MAX_PER_TRADE_USD, DAILY_LOSS_LIMIT_USD, LOSING_TRADES_PER_HOUR,
    PAUSE_AFTER_HOURLY_LIMIT_S,
)
from .limits import check_can_fire, record_pnl, snapshot
from .killswitch import is_killed, kill, unkill
from .reconcile import start_reconcile_loop, stop_reconcile_loop, last_reconcile_status

__all__ = [
    'RiskState', 'get_state', 'save_state', 'load_state',
    'MAX_PER_TRADE_USD', 'DAILY_LOSS_LIMIT_USD', 'LOSING_TRADES_PER_HOUR',
    'PAUSE_AFTER_HOURLY_LIMIT_S',
    'check_can_fire', 'record_pnl', 'snapshot',
    'is_killed', 'kill', 'unkill',
    'start_reconcile_loop', 'stop_reconcile_loop', 'last_reconcile_status',
]
