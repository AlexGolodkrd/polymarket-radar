/**
 * Paper-trading log + post-hoc realistic-fill evaluator.
 *
 * Mirrors Python `Scripts/executor/dryrun_log.py`. Writes:
 *   - `Executions/dryrun.jsonl`       — one row per leg (kind=leg) and
 *                                       one row per arb decision (kind=arb)
 *   - `Executions/paper_results.jsonl`— evaluator row written ~5s after
 *                                       each arb fire, with realistic
 *                                       fill prices fetched from the
 *                                       same exchange APIs the firer
 *                                       would have hit
 *
 * Phase TS-3: dry-run only. Real paper-trading evaluator (orderbook
 * re-fetch with realistic slippage) is out of scope here; we mirror
 * Python's `realistic_fill: null, reason: "rejected"` shape until TS-5.
 */
import { appendFile } from 'node:fs/promises';
import type { LegSpec } from '../types/deal.js';
import type { BuiltOrder } from '../types/deal.js';
import { DRYRUN_PATH, PAPER_RESULTS_PATH, ensureDataDir } from '../lib/paths.js';

export type ArbStatus = 'dry-fired' | 'rejected' | 'aborted';

export interface LegResult {
  legIdx: number;
  platform: string;
  status: ArbStatus;
  expectedPrice: number;
  expectedSizeUsdc: number;
  fillPrice?: number | null;
  fillSizeUsdc?: number | null;
  botId?: string;
  error?: string | null;
  elapsedMs?: number | null;
  extra?: Record<string, unknown>;
}

export interface ArbFireResult {
  arbId: string;
  dealTitle: string;
  dealStructure: string;
  expectedCost: number;
  expectedPayout: number;
  simPnl: number;
  legCount: number;
  legStatusCounts: Record<ArbStatus, number>;
  partialLegCount: number;
  worstPartialShortfallUsdc: number;
  abortedReason: string | null;
  fireMode: 'taker' | 'maker';
  dryRun: boolean;
  firedAt: number;
  legs: LegResult[];
}

/**
 * Append a single leg-level decision row. Matches the Python
 * `dryrun.jsonl` schema 1:1 so analytics aggregations work over the
 * union of files written by either runtime during cutover.
 */
export async function logOrderDecision(
  arbId: string,
  legIdx: number,
  built: BuiltOrder<unknown>,
  legSpec: LegSpec,
  botId: string,
): Promise<void> {
  await ensureDataDir();
  const row = {
    kind: 'leg',
    arb_id: arbId,
    leg_idx: legIdx,
    platform: built.platform,
    expected_price: built.expectedPrice,
    expected_size_usdc: built.expectedSizeUsdc,
    bot_id: botId,
    would_post_url: built.wouldPostUrl,
    body: (built as { body?: unknown }).body,
    ts: Date.now() / 1000,
    spec_outcome: legSpec.tokenId ?? legSpec.marketHash ?? legSpec.slug,
  };
  await appendFile(DRYRUN_PATH, `${JSON.stringify(row)}\n`);
}

/** Append the arb-level summary row (one per fireArb call). */
export async function logArbDecision(result: ArbFireResult): Promise<void> {
  await ensureDataDir();
  const row = {
    kind: 'arb',
    arb_id: result.arbId,
    title: result.dealTitle,
    structure: result.dealStructure,
    expected_cost: result.expectedCost,
    expected_payout: result.expectedPayout,
    sim_pnl: result.simPnl,
    dry_run: result.dryRun,
    aborted_reason: result.abortedReason,
    leg_count: result.legCount,
    leg_status_counts: result.legStatusCounts,
    partial_leg_count: result.partialLegCount,
    worst_partial_shortfall_usdc: result.worstPartialShortfallUsdc,
    fired_at: result.firedAt,
    fire_mode: result.fireMode,
  };
  await appendFile(DRYRUN_PATH, `${JSON.stringify(row)}\n`);
}

/**
 * Schedule a post-hoc realistic-fill evaluator. Phase TS-3 stub: writes
 * a placeholder paper_results row with `realistic_fill: null, reason:
 * "rejected"` for each leg, mirroring the Python behavior when the
 * executor refuses to fire (e.g. cross-platform rejected by depth
 * recheck). Real evaluator (refetch /book, compute slippage) lands in
 * TS-5 alongside fill confirmation.
 */
export async function schedulePaperEvaluation(
  result: ArbFireResult,
  delayMs = 5000,
): Promise<void> {
  setTimeout(async () => {
    try {
      await ensureDataDir();
      const allRejected = result.legs.every((l) => l.status !== 'dry-fired');
      const row = {
        arb_id: result.arbId,
        title: result.dealTitle,
        structure: result.dealStructure,
        sim_pnl: result.simPnl,
        realistic_pnl_5s: result.simPnl,
        drift: 0,
        legs: result.legs.map((l) => ({
          leg_idx: l.legIdx,
          ...(l.status === 'dry-fired'
            ? { expected_price: l.expectedPrice, realistic_fill: null, slippage: null }
            : { realistic_fill: null, reason: 'rejected' }),
        })),
        dry_fired_at: result.firedAt,
        evaluated_at: Date.now() / 1000,
        all_rejected: allRejected,
      };
      await appendFile(PAPER_RESULTS_PATH, `${JSON.stringify(row)}\n`);
    } catch (err) {
      // Paper logging failure must NEVER take down the executor —
      // mirrors Python's silent skip on append errors.
      // eslint-disable-next-line no-console
      console.error('[paper] schedule eval failed:', err);
    }
  }, delayMs);
}

/**
 * Read paper_results.jsonl and compute graduation stats. Stub for now
 * (real evaluator + drift calc lands in TS-5). Returns minimal shape
 * so the Python analytics page can read either source uniformly.
 */
export async function paperStats(_windowN = 50): Promise<{
  count: number;
  ready: boolean;
  blockers: string[];
}> {
  return {
    count: 0,
    ready: false,
    blockers: ['TS-3 stub — real evaluator in TS-5'],
  };
}
