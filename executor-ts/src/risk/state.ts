/**
 * Risk state — daily P&L accumulator + paused_until + reconcile status.
 *
 * Mirrors Python `Scripts/risk/state.py`. Single source of truth for
 * risk gates; persisted to `Executions/risk_state.json` so it survives
 * container restarts.
 *
 * NOT mutex-protected at the JS layer because the Fastify Node process
 * is single-threaded by design (radar's parallel HTTP I/O is event-loop,
 * not threaded). If this is ever ported to a multi-worker setup, swap
 * the in-memory state for atomic file rename.
 */
import { readFile, writeFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { RISK_STATE_PATH, ensureDataDir } from '../lib/paths.js';

// Env-overridable — mirrors Scripts/risk/state.py. Operator can tighten
// caps in Credentials.env (e.g. MAX_PER_TRADE_USD=5 for first live runs)
// without rebuilding the image.
const envNum = (k: string, d: number) => {
  const v = process.env[k];
  if (v == null || v === '') return d;
  const n = Number(v);
  return Number.isFinite(n) ? n : d;
};
export const MAX_PER_TRADE_USD = envNum('MAX_PER_TRADE_USD', 55.0);
export const DAILY_LOSS_LIMIT_USD = envNum('DAILY_LOSS_LIMIT_USD', 35.0);
export const LOSING_TRADES_PER_HOUR = envNum('LOSING_TRADES_PER_HOUR', 5);
export const PAUSE_AFTER_HOURLY_LIMIT_S = envNum('PAUSE_AFTER_HOURLY_LIMIT_S', 3600);
export const RECONCILE_INTERVAL_S = envNum('RECONCILE_INTERVAL_S', 60);
export const RECONCILE_TOLERANCE_USD = envNum('RECONCILE_TOLERANCE_USD', 1.0);

export interface RecentTrade {
  ts: number;
  pnlUsd: number;
}

export interface RiskState {
  dailyDateUtc: string; // YYYY-MM-DD
  dailyPnlUsd: number;
  recentTrades: RecentTrade[]; // rolling 1h window
  pausedUntilUnix: number | null;
  pausedReason: string | null;
  lastReconcileUnix: number | null;
  lastReconcileOk: boolean;
  lastReconcileMsg: string;
}

let _state: RiskState | null = null;

function todayUtc(): string {
  return new Date().toISOString().slice(0, 10);
}

export async function loadState(): Promise<RiskState> {
  if (_state) return _state;
  await ensureDataDir();
  if (existsSync(RISK_STATE_PATH)) {
    try {
      const raw = await readFile(RISK_STATE_PATH, 'utf-8');
      const parsed = JSON.parse(raw) as Partial<RiskState>;
      _state = {
        dailyDateUtc: parsed.dailyDateUtc ?? todayUtc(),
        dailyPnlUsd: parsed.dailyPnlUsd ?? 0,
        recentTrades: parsed.recentTrades ?? [],
        pausedUntilUnix: parsed.pausedUntilUnix ?? null,
        pausedReason: parsed.pausedReason ?? null,
        lastReconcileUnix: parsed.lastReconcileUnix ?? null,
        lastReconcileOk: parsed.lastReconcileOk ?? true,
        lastReconcileMsg: parsed.lastReconcileMsg ?? 'never run',
      };
    } catch {
      _state = freshState();
    }
  } else {
    _state = freshState();
  }
  return rolloverDayIfNeeded(_state);
}

function freshState(): RiskState {
  return {
    dailyDateUtc: todayUtc(),
    dailyPnlUsd: 0,
    recentTrades: [],
    pausedUntilUnix: null,
    pausedReason: null,
    lastReconcileUnix: null,
    lastReconcileOk: true,
    lastReconcileMsg: 'never run',
  };
}

function rolloverDayIfNeeded(s: RiskState): RiskState {
  const today = todayUtc();
  if (s.dailyDateUtc !== today) {
    s.dailyDateUtc = today;
    s.dailyPnlUsd = 0;
    // Don't clear paused_until — operator may have set it; expires by ts
  }
  return s;
}

export async function saveState(s: RiskState): Promise<void> {
  _state = s;
  await ensureDataDir();
  // Atomic write: write to .tmp then rename. Mirrors Python's write
  // pattern; protects against half-written JSON on crash.
  const tmp = `${RISK_STATE_PATH}.tmp`;
  await writeFile(tmp, JSON.stringify(s, null, 2));
  // Node's rename is atomic on POSIX; on Windows it's also atomic for
  // same-volume renames which is what we have here.
  const { rename } = await import('node:fs/promises');
  await rename(tmp, RISK_STATE_PATH);
}

export function trimRecentTrades(s: RiskState, nowSec: number): RiskState {
  const oneHourAgo = nowSec - 3600;
  s.recentTrades = s.recentTrades.filter((t) => t.ts >= oneHourAgo);
  return s;
}

/** Reset for tests. */
export function _resetForTest(): void {
  _state = null;
}
