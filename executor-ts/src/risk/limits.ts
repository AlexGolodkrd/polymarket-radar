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

/**
 * Pre-fire risk check. Idempotent — pure read of state.
 *
 * Per-trade size cap (`MAX_PER_TRADE_USD × legCount`) is intentionally
 * NOT in this gate — see `clipToPerTradeCap` instead. The radar picks a
 * profit-maximizing stake based on liquidity/depth; the operator-set cap
 * is a risk envelope, not a viability check. Aborting when stake > cap
 * threw away genuine arbs (e.g. radar wanted $41.71, cap was $2 → abort
 * with $0 P&L). Clipping the stake down to fit the cap preserves the
 * arb at reduced size — operator's permission is the authority.
 */
export async function checkCanFire(legCount: number, _totalStakeUsd: number): Promise<CheckResult> {
  void legCount; void _totalStakeUsd; // kept for ABI compat with callers / tests
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

export interface ClipResult {
  /** True if any clipping happened (totalStake exceeded the per-trade cap). */
  clipped: boolean;
  /** What the cap is for this leg count: `MAX_PER_TRADE_USD × legCount`. */
  capUsd: number;
  /** Sum of `expectedSizeUsdc` BEFORE clipping. */
  originalTotalStakeUsd: number;
  /** Sum of `expectedSizeUsdc` AFTER clipping (≤ capUsd). */
  clippedTotalStakeUsd: number;
  /** Scaling factor applied to each leg (1.0 if no clip). */
  ratio: number;
}

/** Per-platform minimum order size in USDC. All three platforms enforce $1
 *  at the builder/exchange layer (poly.ts uses LegSpec.minOrderSizeUsdc
 *  defaulting to $1; limitless.ts and sx.ts throw at <$1). The leg may
 *  carry a per-market override via `minOrderSizeUsdc`. */
const DEFAULT_PLATFORM_MIN_USDC = 1.0;

export interface FloorResult {
  /** True if any leg was raised to its platform min. */
  floored: boolean;
  /** Sum of additional stake added across all legs (cap may be exceeded). */
  extraStakeUsd: number;
  /** Sum of `expectedSizeUsdc` AFTER flooring. */
  finalTotalStakeUsd: number;
  /** Number of legs that were below their platform min before flooring. */
  legsFloored: number;
}

/**
 * Raise any leg's `expectedSizeUsdc` to the platform minimum if it
 * fell below after clipping. Without this, a $1/leg cap on a 2-leg
 * cross-platform arb with skewed prices (e.g. 80¢ + 20¢) would clip to
 * $1.60 + $0.40 — and the $0.40 leg gets rejected by the exchange's
 * $1 min, breaking the arb. We accept that the total stake may exceed
 * `MAX_PER_TRADE_USD × legCount` in this corner case, because operator
 * directive is "min-floor чтобы сделки не абортились" — taking a
 * slightly-larger-than-cap real trade beats taking nothing.
 *
 * Mutates entries in place. Should be called AFTER `clipToPerTradeCap`.
 */
export function applyPlatformMinFloor<
  T extends { expectedSizeUsdc: number; minOrderSizeUsdc?: number },
>(entries: T[]): FloorResult {
  let extra = 0;
  let count = 0;
  for (const e of entries) {
    const min = e.minOrderSizeUsdc ?? DEFAULT_PLATFORM_MIN_USDC;
    if (e.expectedSizeUsdc < min) {
      extra += min - e.expectedSizeUsdc;
      e.expectedSizeUsdc = min;
      count++;
    }
  }
  return {
    floored: count > 0,
    extraStakeUsd: extra,
    finalTotalStakeUsd: entries.reduce((s, l) => s + l.expectedSizeUsdc, 0),
    legsFloored: count,
  };
}

/**
 * Scale each leg's `expectedSizeUsdc` down proportionally if the total
 * stake exceeds `MAX_PER_TRADE_USD × legCount`. The radar sizes for
 * profit-maximization given depth; the operator's cap is the authority
 * on capital at risk per arb. When the two conflict, we honor the cap
 * but still fire (smaller slice of the same arb) instead of aborting.
 *
 * Mutates the entries' `expectedSizeUsdc` field in place. Returns a
 * report that callers can surface to logs / paper-results.
 *
 * Notes:
 *   - `expectedPayout` should be scaled by the same ratio by the caller
 *     (payout is roughly linear in stake; we don't touch it here so the
 *     caller can choose whether to recompute or trust radar's value).
 *   - We do NOT enforce platform minimum order size here — if the clip
 *     pushes a leg below e.g. Polymarket's $1 floor, the builder will
 *     fail and the leg is rejected. That's a downstream concern.
 */
export function clipToPerTradeCap<T extends { expectedSizeUsdc: number }>(
  entries: T[],
): ClipResult {
  const legCount = entries.length;
  const capUsd = MAX_PER_TRADE_USD * legCount;
  const originalTotalStakeUsd = entries.reduce((s, l) => s + l.expectedSizeUsdc, 0);

  if (originalTotalStakeUsd <= capUsd || originalTotalStakeUsd === 0) {
    return {
      clipped: false,
      capUsd,
      originalTotalStakeUsd,
      clippedTotalStakeUsd: originalTotalStakeUsd,
      ratio: 1.0,
    };
  }

  const ratio = capUsd / originalTotalStakeUsd;
  for (const e of entries) e.expectedSizeUsdc *= ratio;
  const clippedTotalStakeUsd = entries.reduce((s, l) => s + l.expectedSizeUsdc, 0);

  return {
    clipped: true,
    capUsd,
    originalTotalStakeUsd,
    clippedTotalStakeUsd,
    ratio,
  };
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
