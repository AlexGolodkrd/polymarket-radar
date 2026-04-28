"""Risk limits — gating function called by the executor before every fire.

The single hot-path API is `check_can_fire(deal)`. Returns:
    (True, None)        — go ahead
    (False, "reason")   — denied (paused, killed, daily limit hit, etc.)

After a real trade resolves (Phase 4+), `record_pnl(pnl_usd)` updates the
daily accumulator + rolling-hour window, possibly tripping a pause.

All thresholds come from feedback_risk_params.md:
    - $55 max per trade
    - $35 daily loss limit (resets 00:00 UTC) → pause until next UTC midnight
    - 5 losing trades within 1 rolling hour → pause new trades for 1h
    - existing positions are NEVER closed on pause/kill
"""
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional, Tuple

from . import state as st
from . import killswitch

log = logging.getLogger(__name__)

_lock = threading.Lock()


def _notify_safe(text: str, level: str = 'info', dedupe_key: str = None):
    """Lazy-import notify and silently skip if not installed/configured.
    Used by limit-hit paths to alert the operator to Telegram."""
    try:
        import sys, os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import notify
        notify.send(text, level=level, dedupe_key=dedupe_key)
    except Exception as e:
        log.debug("notify suppressed: %s", e)


# ── Helpers ─────────────────────────────────────────────────────────
def _next_utc_midnight() -> float:
    now = datetime.now(timezone.utc)
    next_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    next_day = next_day.replace(day=now.day + 1) if now.hour or now.minute else next_day
    # Simpler: add 86400 - seconds_since_midnight
    secs_since_midnight = now.hour * 3600 + now.minute * 60 + now.second
    return time.time() + (86400 - secs_since_midnight)


def _trade_cost_estimate(deal: dict) -> float:
    """Approximate cost = sum of all leg stakes. Phase 2 deal shape:
    deal['entries'] is a list of {stake, price, contracts, ...}."""
    return sum(float(e.get('stake', 0)) for e in deal.get('entries', []))


def _losing_trades_in_last_hour(s: st.RiskState) -> int:
    cutoff = time.time() - 3600
    return sum(1 for (t, p) in s.recent_trades if t >= cutoff and p < 0)


# ── Hot-path: the executor calls this before every fire ─────────────
def check_can_fire(deal: dict) -> Tuple[bool, Optional[str]]:
    """Return (allowed, reason). Executor MUST call this before fire_arb.

    The order of checks matters — kill-switch first (most absolute),
    then per-trade size (cheapest), then pause (covers daily/hourly),
    then daily-limit pre-check (so a borderline trade can't push over).
    """
    if killswitch.is_killed():
        return False, 'kill_switch_active'

    # Network/geo check — application-level Layer 3 of VPN safety. Fails if
    # outbound IP is not in ALLOWED_COUNTRIES (e.g. VPS leaked from VPN to
    # bare provider IP). Disabled by default (empty ALLOWED_COUNTRIES); on
    # VPS set ALLOWED_COUNTRIES=GE (or whichever country you registered from).
    from . import network_check
    net_ok, net_reason = network_check.check_country_allowed()
    if not net_ok:
        _notify_safe(
            f'*Network check failed*\n`{net_reason}`\nFires blocked until recovery.',
            level='warn', dedupe_key='network_check_fail',
        )
        return False, net_reason

    cost = _trade_cost_estimate(deal)
    if cost > st.MAX_PER_TRADE_USD:
        return False, f'per_trade_cap_${st.MAX_PER_TRADE_USD:.0f}_exceeded_(${cost:.2f})'

    s = st.get_state()
    now = time.time()

    # Active pause?
    if s.paused_until_unix and s.paused_until_unix > now:
        remaining_min = (s.paused_until_unix - now) / 60
        return False, f'paused_{remaining_min:.1f}m_left ({s.paused_reason})'
    elif s.paused_until_unix and s.paused_until_unix <= now:
        # Pause expired — clear it
        with _lock:
            s.paused_until_unix = None
            s.paused_reason = None
            st.save_state(s)
        log.info("risk pause expired — resuming")

    # Pre-trade daily-loss check — would worst-case loss on THIS trade
    # cross the daily limit?
    #
    # The original implementation used worst_loss = cost (assumed 100% loss),
    # which is correct for naked directional bets but WRONG for arbitrage.
    # An arb pays out $1 × N (one outcome wins) regardless of which side
    # resolves, so the only actual loss vectors are:
    #   - Slippage during execution (~0.1-0.5c per leg, capped by SLIPPAGE_TOLERANCE)
    #   - Partial fill that we couldn't reverse (Phase 7 detects + aborts)
    #   - Resolution dispute / event mis-resolution (rare on Polymarket)
    # Realistically max loss is 5-15% of cost on a botched arb. We use
    # WORST_CASE_ARB_LOSS_PCT = 0.15 (15%) — conservative but not paralysing.
    # If `deal['net']` is missing or non-positive we fall back to the old
    # full-cost assumption (defensive — directional or already-bad deals).
    deal_net = float(deal.get('net') or 0)
    if deal_net > 0:
        WORST_CASE_ARB_LOSS_PCT = 0.15
        worst_loss = cost * WORST_CASE_ARB_LOSS_PCT
    else:
        worst_loss = cost  # not an arb — be pessimistic
    projected_daily = s.daily_pnl_usd - worst_loss
    if projected_daily < -st.DAILY_LOSS_LIMIT_USD:
        return False, (f'pre_trade_daily_check: worst-case '
                       f'-${worst_loss:.2f} would cross '
                       f'-${st.DAILY_LOSS_LIMIT_USD:.0f}_limit '
                       f'(today: ${s.daily_pnl_usd:.2f})')

    return True, None


# ── Recording outcomes ──────────────────────────────────────────────
def record_pnl(pnl_usd: float, source: str = 'real') -> dict:
    """Update daily P&L + rolling window after a trade resolves.

    Returns the updated risk snapshot. Pauses are triggered HERE so the
    next check_can_fire() denies the next attempt.

    Source tag: 'real' for live trades, 'paper' for Phase 5 paper-trades
    (which DO count toward graduation gate but should NOT trigger live
    pauses — they're informational).
    """
    s = st.get_state()
    now = time.time()
    with _lock:
        if source == 'real':
            s.daily_pnl_usd += pnl_usd
            s.recent_trades.append([now, pnl_usd])
            # Trim to last hour
            cutoff = now - 3600
            s.recent_trades = [(t, p) for (t, p) in s.recent_trades if t >= cutoff]

            # Daily-loss limit hit?
            if s.daily_pnl_usd <= -st.DAILY_LOSS_LIMIT_USD:
                s.paused_until_unix = _next_utc_midnight()
                s.paused_reason = (f'daily_loss_limit_hit '
                                   f'(${s.daily_pnl_usd:.2f} ≤ '
                                   f'-${st.DAILY_LOSS_LIMIT_USD:.0f})')
                log.warning("RISK: %s — paused until next UTC midnight", s.paused_reason)
                _notify_safe(
                    f'*DAILY LOSS LIMIT HIT*\n'
                    f'P&L today: `${s.daily_pnl_usd:.2f}` (limit: `-${st.DAILY_LOSS_LIMIT_USD:.0f}`)\n'
                    f'Trading paused until 00:00 UTC.',
                    level='crit', dedupe_key=f'daily_loss_{s.daily_date_utc}',
                )

            # Hourly-losing-trade limit hit?
            losing_count = _losing_trades_in_last_hour(s)
            if losing_count >= st.LOSING_TRADES_PER_HOUR:
                hourly_until = now + st.PAUSE_AFTER_HOURLY_LIMIT_S
                # If a daily pause is already further in the future, keep it.
                if (s.paused_until_unix or 0) < hourly_until:
                    s.paused_until_unix = hourly_until
                    s.paused_reason = (f'hourly_losing_streak '
                                       f'({losing_count} losing trades in last hour)')
                    log.warning("RISK: %s — paused 1h", s.paused_reason)
                    _notify_safe(
                        f'*Hourly losing streak*\n'
                        f'`{losing_count}` losing trades in last hour\n'
                        f'Trading paused 1 hour. Existing positions kept open.',
                        level='warn', dedupe_key=f'hourly_streak_{int(now//3600)}',
                    )
        st.save_state(s)
    return snapshot()


def snapshot() -> dict:
    """Read-only summary for /api/risk_status + dashboard."""
    s = st.get_state()
    now = time.time()
    paused = bool(s.paused_until_unix and s.paused_until_unix > now)
    return {
        'killed': killswitch.is_killed(),
        'paused': paused,
        'paused_reason': s.paused_reason if paused else None,
        'paused_until_unix': s.paused_until_unix if paused else None,
        'paused_remaining_s': max(0, (s.paused_until_unix or 0) - now) if paused else 0,

        'daily_date_utc': s.daily_date_utc,
        'daily_pnl_usd': round(s.daily_pnl_usd, 2),
        'daily_loss_limit_usd': st.DAILY_LOSS_LIMIT_USD,
        'daily_loss_remaining_usd': round(
            st.DAILY_LOSS_LIMIT_USD + min(0, s.daily_pnl_usd), 2),

        'losing_trades_last_hour': _losing_trades_in_last_hour(s),
        'losing_trades_per_hour_limit': st.LOSING_TRADES_PER_HOUR,

        'max_per_trade_usd': st.MAX_PER_TRADE_USD,

        'last_reconcile_ok': s.last_reconcile_ok,
        'last_reconcile_unix': s.last_reconcile_unix,
        'last_reconcile_msg': s.last_reconcile_msg,
    }
