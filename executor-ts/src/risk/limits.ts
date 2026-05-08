/**
 * Risk gates — call before every fire. Mirrors Python
 * `Scripts/risk/limits.py:check_can_fire`.
 *
 * Returns `{allowed: false, reason}` for any tripped gate; firer must
 * abort the entire arb (not just one leg) on `allowed=false`.
 */
import {
  loadState,
  saveState,
  trimRecentTrades,
  MAX_PER_TRADE_USD,
  DAILY_LOSS_LIMIT_USD,
  LOSING_TRADES_PER_HOUR,
  PAUSE_AFTER_HOURLY_LIMIT_S,
} from './state.js';
import { isKilled, status as killStatus } from './killswitch.js';

export interface CheckResult {
  allowed: boolean;
  reason?: string;
}

/** Pre-fire risk check. Idempotent — pure read of state. */
export async function checkCanFire(legCount: number, totalStakeUsd: number): Promise<CheckResult> {
  // Layer 0 — kill switch. Fail-closed.
  if (isKilled()) {
    const k = killStatus();
    return { allowed: false, reason: `kill switch active: ${k.reason ?? 'no reason set'}` };
  }
  const s = await loadState();
  const now = Date.now() / 1000;
  trimRecentTrades(s, now);

  // Layer 1 — paused?
  if (s.pausedUntilUnix !== null && s.pausedUntilUnix > now) {
    const remainingS = Math.round(s.pausedUntilUnix - now);
    return {
      allowed: false,
      reason: `paused (${s.pausedReason ?? 'no reason'}) — ${remainingS}s remaining`,
    };
  }

  // Layer 2 — per-trade size cap. legCount × MAX_PER_TRADE_USD = abs cap.
  if (totalStakeUsd > MAX_PER_TRADE_USD * legCount) {
    return {
      allowed: false,
      reason: `stake $${totalStakeUsd.toFixed(2)} exceeds $${MAX_PER_TRADE_USD}×${legCount}`,
    };
  }

  // Layer 3 — daily loss limit (only checks if accumulator already
  // negative; firing more isn't blocked by zero pnl).
  if (s.dailyPnlUsd <= -DAILY_LOSS_LIMIT_USD) {
    return {
      allowed: false,
      reason: `daily loss limit hit: $${s.dailyPnlUsd.toFixed(2)} ≤ -$${DAILY_LOSS_LIMIT_USD}`,
    };
  }

  // Layer 4 — losing trades in last hour
  const losingLastHour = s.recentTrades.filter((t) => t.pnlUsd < 0).length;
  if (losingLastHour >= LOSING_TRADES_PER_HOUR) {
    return {
      allowed: false,
      reason: `${losingLastHour} losing trades in last hour ≥ ${LOSING_TRADES_PER_HOUR} limit`,
    };
  }

  return { allowed: true };
}

/**
 * Record a settled trade's P&L, possibly trip the hourly losing-trade
 * limit (which auto-pauses). Mirrors Python `record_pnl`.
 */
export async function recordPnl(pnlUsd: number, source: string): Promise<void> {
  const s = await loadState();
  const now = Date.now() / 1000;
  s.dailyPnlUsd += pnlUsd;
  s.recentTrades.push({ ts: now, pnlUsd });
  trimRecentTrades(s, now);

  // Auto-pause if hourly losing-trade limit just tripped.
  const losingLastHour = s.recentTrades.filter((t) => t.pnlUsd < 0).length;
  if (
    losingLastHour >= LOSING_TRADES_PER_HOUR &&
    (s.pausedUntilUnix === null || s.pausedUntilUnix < now)
  ) {
    s.pausedUntilUnix = now + PAUSE_AFTER_HOURLY_LIMIT_S;
    s.pausedReason = `auto-pause: ${losingLastHour} losing trades in 1h (source=${source})`;
  }
  await saveState(s);
}

export async function snapshot(): Promise<Record<string, unknown>> {
  const s = await loadState();
  const now = Date.now() / 1000;
  trimRecentTrades(s, now);
  const losingLastHour = s.recentTrades.filter((t) => t.pnlUsd < 0).length;
  const k = killStatus();
  return {
    daily_date_utc: s.dailyDateUtc,
    daily_loss_limit_usd: DAILY_LOSS_LIMIT_USD,
    daily_loss_remaining_usd: Math.max(0, DAILY_LOSS_LIMIT_USD + s.dailyPnlUsd),
    daily_pnl_usd: s.dailyPnlUsd,
    killed: k.killed,
    last_reconcile_msg: s.lastReconcileMsg,
    last_reconcile_ok: s.lastReconcileOk,
    last_reconcile_unix: s.lastReconcileUnix,
    losing_trades_last_hour: losingLastHour,
    losing_trades_per_hour_limit: LOSING_TRADES_PER_HOUR,
    max_per_trade_usd: MAX_PER_TRADE_USD,
    paused: s.pausedUntilUnix !== null && s.pausedUntilUnix > now,
    paused_reason: s.pausedReason,
    paused_remaining_s:
      s.pausedUntilUnix !== null && s.pausedUntilUnix > now
        ? Math.round(s.pausedUntilUnix - now)
        : 0,
    paused_until_unix: s.pausedUntilUnix,
  };
}
