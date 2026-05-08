/**
 * Centralizes filesystem paths for `Executions/` artefacts so atomic.ts,
 * paper.ts, and risk.ts can't accidentally drift apart on where they
 * read/write. Mirrors Python `Scripts/executor/dryrun_log.py:_BASE_DIR`
 * and the `Executions/` host bind-mount in docker-compose.
 */
import { join } from 'node:path';
import { mkdir } from 'node:fs/promises';
import { existsSync } from 'node:fs';

/**
 * Default to `/app/Executions` (container mount), fall back to
 * `${cwd}/Executions` for local dev. Override via env `EXECUTIONS_DIR`
 * for tests or non-standard layouts.
 */
export const EXECUTIONS_DIR =
  process.env.EXECUTIONS_DIR ??
  (existsSync('/app/Executions') ? '/app/Executions' : join(process.cwd(), 'Executions'));

export const DRYRUN_PATH = join(EXECUTIONS_DIR, 'dryrun.jsonl');
export const PAPER_RESULTS_PATH = join(EXECUTIONS_DIR, 'paper_results.jsonl');
export const POSITIONS_PATH = join(EXECUTIONS_DIR, 'positions.jsonl');
export const KILLED_FLAG_PATH = join(EXECUTIONS_DIR, '.killed');
export const RISK_STATE_PATH = join(EXECUTIONS_DIR, 'risk_state.json');

/** Idempotent create of the data directory; safe to call repeatedly. */
export async function ensureDataDir(): Promise<void> {
  await mkdir(EXECUTIONS_DIR, { recursive: true });
}
