"""Risk state — single source of truth.

Persists to Executions/risk_state.json so a radar restart mid-day keeps
the daily-loss accumulator. JSON keeps it human-readable for ops.
"""
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

# ── Defaults (from feedback_risk_params.md) ────────────────────────
MAX_PER_TRADE_USD = 55.0
DAILY_LOSS_LIMIT_USD = 35.0
LOSING_TRADES_PER_HOUR = 5
PAUSE_AFTER_HOURLY_LIMIT_S = 3600    # 1 hour pause after 5 losing trades

# Reconcile params
RECONCILE_INTERVAL_S = 60
# Phase 19v18 (05.05.2026) — bump tolerance from $0.01 to $1.00.
# Rationale: local positions store `expected_size_usdc` (build-time),
# remote returns `shares * avgPrice` (post-fill). Slippage of even
# 0.5¢ on a $50 stake yields a $0.50 mismatch — far above $0.01,
# tripping the reconcile-failure debounce within 3 cycles → kill
# switch fires on every active fire. $1 tolerance covers normal
# slippage; real positional divergence (lost / extra fills) will
# always exceed $1 because the smallest leg is $5+ stake.
RECONCILE_TOLERANCE_USD = 1.00

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_THIS_DIR, '..', '..'))
EXECUTIONS_DIR = os.path.join(_REPO_ROOT, 'Executions')
STATE_PATH = os.path.join(EXECUTIONS_DIR, 'risk_state.json')


@dataclass
class RiskState:
    """Live risk-tracker. All fields are simple JSON-roundtrippable types."""
    # Daily P&L (resets at 00:00 UTC)
    daily_date_utc: str = ''                # 'YYYY-MM-DD'
    daily_pnl_usd: float = 0.0              # net realized P&L for the day

    # Rolling losing-trade window (hourly limit)
    # List of (timestamp_unix, pnl_usd) for trades within last hour.
    recent_trades: list = field(default_factory=list)

    # Pause state — when set, executor refuses new fires until then.
    # `paused_until_unix=None` and `paused_reason=None` = not paused.
    paused_until_unix: Optional[float] = None
    paused_reason: Optional[str] = None     # human-readable

    # Position reconciliation last-status
    last_reconcile_unix: Optional[float] = None
    last_reconcile_ok: Optional[bool] = None
    last_reconcile_msg: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


# Phase 19v15 (05.05.2026) — RLock so callers (limits.record_pnl,
# check_can_fire) can hold this lock across their read-modify-write
# AND still call get_state() / save_state() without deadlocking.
# Previously state._state_lock and limits._lock were independent → the
# day-roll fired in get_state() could wipe daily_pnl_usd between
# limits.record_pnl's read and write at midnight UTC, hiding losses.
_state_lock = threading.RLock()
_state: Optional[RiskState] = None


def _utc_today_str() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%d')


def _ensure_dir():
    os.makedirs(EXECUTIONS_DIR, exist_ok=True)


def load_state() -> RiskState:
    """Load from disk if present; otherwise return a fresh state.
    Daily counters reset if the saved date is yesterday or older."""
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
            s = RiskState(**data)
        except Exception as e:
            log.warning("risk state load failed (%s) — starting fresh", e)
            s = RiskState()
    else:
        s = RiskState()
    today = _utc_today_str()
    if s.daily_date_utc != today:
        # New UTC day — reset daily counters but keep pause-until/recent-trades
        # so a fresh-day start doesn't suddenly unblock a 5-losing pause.
        s.daily_date_utc = today
        s.daily_pnl_usd = 0.0
    # Always trim recent_trades to last hour
    cutoff = time.time() - 3600
    s.recent_trades = [(t, p) for (t, p) in s.recent_trades if t >= cutoff]
    return s


def save_state(s: RiskState = None):
    """Atomic write to STATE_PATH (write-then-rename)."""
    if s is None:
        s = get_state()
    _ensure_dir()
    tmp = STATE_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(s.to_dict(), f, indent=2)
    os.replace(tmp, STATE_PATH)


def _check_day_roll_unlocked(s: RiskState) -> bool:
    """Phase 19v15 — apply day-roll if needed. Caller MUST hold _state_lock.
    Returns True if a roll happened (caller should save_state).
    """
    today = _utc_today_str()
    if s.daily_date_utc != today:
        log.info("UTC day rolled %s → %s — resetting daily P&L",
                 s.daily_date_utc, today)
        s.daily_date_utc = today
        s.daily_pnl_usd = 0.0
        # Clear daily-loss pause but keep hourly-loss pause if still active
        if s.paused_reason and 'daily' in (s.paused_reason or '').lower():
            s.paused_until_unix = None
            s.paused_reason = None
        return True
    return False


def get_state() -> RiskState:
    """Lazy-init singleton. Thread-safe.

    Phase 19v15 (05.05.2026) — day-roll uses _check_day_roll_unlocked so
    callers can wrap `get_state() + record_pnl-style mutation` in their
    own `with state._state_lock:` block without losing the roll-then-add
    atomicity at midnight UTC.
    """
    global _state
    with _state_lock:
        if _state is None:
            _state = load_state()
        if _check_day_roll_unlocked(_state):
            save_state(_state)
        return _state


def reset_for_test():
    """Wipe singleton so tests can start clean. NOT for production use."""
    global _state
    with _state_lock:
        _state = None
