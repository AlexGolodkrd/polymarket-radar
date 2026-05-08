/**
 * Kill switch — file-flag based, fail-CLOSED.
 *
 * If `Executions/.killed` exists, fire is blocked. Mirrors Python
 * `Scripts/risk/killswitch.py`. Watchdog process polls the same file.
 *
 * Fail-closed semantics: if the FS check itself errors (e.g. /app
 * mount disappeared), `isKilled()` returns true. We'd rather refuse
 * to fire on transient FS issues than silently ignore the kill flag.
 */
import { existsSync, readFileSync, writeFileSync, unlinkSync } from 'node:fs';
import { KILLED_FLAG_PATH, ensureDataDir } from '../lib/paths.js';

export interface KillSwitchStatus {
  killed: boolean;
  reason: string | null;
  setAt: number | null;
}

export function isKilled(): boolean {
  try {
    return existsSync(KILLED_FLAG_PATH);
  } catch {
    return true; // fail-CLOSED on FS errors
  }
}

export function status(): KillSwitchStatus {
  if (!isKilled()) return { killed: false, reason: null, setAt: null };
  try {
    const raw = readFileSync(KILLED_FLAG_PATH, 'utf-8');
    const parsed = JSON.parse(raw) as { reason?: string; set_at?: number };
    return {
      killed: true,
      reason: parsed.reason ?? null,
      setAt: parsed.set_at ?? null,
    };
  } catch {
    return { killed: true, reason: 'flag exists, content unreadable', setAt: null };
  }
}

export async function kill(reason: string): Promise<KillSwitchStatus> {
  await ensureDataDir();
  const payload = {
    reason,
    set_at: Date.now() / 1000,
    pid: process.pid,
    set_by: 'executor-ts',
  };
  writeFileSync(KILLED_FLAG_PATH, JSON.stringify(payload, null, 2));
  return status();
}

export function unkill(): boolean {
  if (!isKilled()) return false;
  try {
    unlinkSync(KILLED_FLAG_PATH);
    return true;
  } catch {
    return false;
  }
}
