/**
 * Atomic arb firer — TS port of Python `Scripts/executor/atomic.py`.
 *
 * Distributes a deal's legs across the wallet pool, fires them in
 * parallel via Promise.all (target <100ms), implements:
 *   - per-leg timeout (Python PER_ORDER_TIMEOUT_S=2s)
 *   - slippage check (|fill_price - expected| > 0.005 → cancel)
 *   - dead-man switch (no fill confirms within 5s → cancel all)
 *   - reversal (if some legs filled and others rejected, sell filled
 *     at market to flatten)
 *
 * Phase TS-3 ships **dry-run only** — no real POSTs. Caller receives
 * the same ArbFireResult shape Python writes to dryrun.jsonl. Real
 * HTTP firing wires in TS-5 alongside fill confirmation.
 */
import type { FireRequest, LegSpec, BuiltOrder } from '../types/deal.js';
import type { Wallet } from '../types/wallet.js';
import { buildPolyOrder } from '../builders/poly.js';
import { buildSxOrder } from '../builders/sx.js';
import { buildLimitlessOrder } from '../builders/limitless.js';
import { assignLegs, jitterMsForLeg } from '../wallets/pool.js';
import { checkCanFire } from '../risk/limits.js';
import { isKilled } from '../risk/killswitch.js';
import {
  type ArbFireResult,
  type LegResult,
  logOrderDecision,
  logArbDecision,
  schedulePaperEvaluation,
} from './paper.js';
import { planRevert, annotateLegsWithPlan } from './revert.js';

const DRY_RUN_DEFAULT = (process.env.DRY_RUN ?? '1') !== '0';
const SLIPPAGE_TOLERANCE = Number(process.env.SLIPPAGE_TOLERANCE ?? '0.005');
const MIN_NET_PER_ARB_USD = Number(process.env.MIN_NET_PER_ARB_USD ?? '0.50');
const PER_LEG_TIMEOUT_MS = Number(process.env.PER_ORDER_TIMEOUT_S ?? '2') * 1000;

/**
 * Build the platform-specific BuiltOrder for one leg. Pure dispatch
 * over LegSpec.platform — each builder is itself pure (no I/O), the
 * one exception being SX Bet which needs maker orders fetched first.
 * Phase TS-3 stubs SX with empty orders — TS-5 wires real fetcher.
 */
async function buildLeg(spec: LegSpec, wallet: Wallet): Promise<BuiltOrder<unknown>> {
  switch (spec.platform) {
    case 'polymarket': {
      if (!spec.tokenId) throw new Error(`polymarket leg requires tokenId`);
      return await buildPolyOrder({
        tokenId: spec.tokenId,
        side: spec.side,
        price: spec.expectedPrice,
        sizeUsdc: spec.expectedSizeUsdc,
        wallet,
        ...(spec.negRisk !== undefined ? { negRisk: spec.negRisk } : {}),
        ...(spec.orderType ? { orderType: spec.orderType } : {}),
      });
    }
    case 'limitless': {
      if (!spec.tokenId || !spec.slug) {
        throw new Error('limitless leg requires tokenId + slug');
      }
      return await buildLimitlessOrder({
        slug: spec.slug,
        tokenId: spec.tokenId,
        side: spec.side,
        price: spec.expectedPrice,
        sizeUsdc: spec.expectedSizeUsdc,
        wallet,
        ...(spec.verifyingContract ? { verifyingContract: spec.verifyingContract } : {}),
        ...(spec.orderType ? { orderType: spec.orderType } : {}),
      });
    }
    case 'sx_bet': {
      if (!spec.marketHash || spec.outcome === undefined) {
        throw new Error('sx_bet leg requires marketHash + outcome');
      }
      // Phase TS-3 stub: empty orders array → match=empty → partial.
      // TS-5 plugs in real `undici` GET /orders fetch with circuit
      // breaker and Phase 19v26+v27 size parsing.
      return await buildSxOrder({
        marketHash: spec.marketHash,
        outcome: spec.outcome,
        takerPrice: spec.expectedPrice,
        sizeUsdc: spec.expectedSizeUsdc,
        wallet,
        orders: [],
      });
    }
    case 'kalshi':
      throw new Error('kalshi disabled (US-only KYC)');
    default:
      throw new Error(`unknown platform: ${spec.platform as string}`);
  }
}

/**
 * Fire one leg in dry-run mode: build → log → simulate "rejected" or
 * "dry-fired" status. In real-mode (Phase TS-5) this becomes a real
 * POST with timeout, slippage check, and fill-event awaiting.
 */
async function fireLeg(
  arbId: string,
  legIdx: number,
  spec: LegSpec,
  wallet: Wallet,
  dryRun: boolean,
): Promise<LegResult> {
  const startedAt = Date.now();
  // Anti-detection jitter (matches Python coordinator behavior).
  await new Promise((r) => setTimeout(r, jitterMsForLeg(legIdx)));
  try {
    const built = await buildLeg(spec, wallet);
    await logOrderDecision(arbId, legIdx, built, spec, wallet.botId);

    if (dryRun) {
      // Phase TS-3 default path: log the decision, mark as dry-fired.
      // The paper-eval step (5s later) will refetch and evaluate
      // realistic fill, mirroring Python.
      return {
        legIdx,
        platform: spec.platform,
        status: 'dry-fired',
        expectedPrice: built.expectedPrice,
        expectedSizeUsdc: built.expectedSizeUsdc,
        botId: wallet.botId,
        elapsedMs: Date.now() - startedAt,
        extra: {
          signed: built.signed,
          would_post_url: built.wouldPostUrl,
        },
      };
    }

    // Real-mode firing not yet implemented (TS-5).
    return {
      legIdx,
      platform: spec.platform,
      status: 'rejected',
      expectedPrice: built.expectedPrice,
      expectedSizeUsdc: built.expectedSizeUsdc,
      botId: wallet.botId,
      error: 'real-mode firing not yet wired (Phase TS-5)',
      elapsedMs: Date.now() - startedAt,
    };
  } catch (err) {
    return {
      legIdx,
      platform: spec.platform,
      status: 'rejected',
      expectedPrice: spec.expectedPrice,
      expectedSizeUsdc: spec.expectedSizeUsdc,
      botId: wallet.botId,
      error: err instanceof Error ? err.message : String(err),
      elapsedMs: Date.now() - startedAt,
    };
  }
}

/**
 * Top-level entry: fire an arb across N legs. Returns ArbFireResult
 * compatible with Python schema (one row in dryrun.jsonl).
 *
 * Pre-fire risk checks:
 *   1. kill switch (fail-CLOSED)
 *   2. risk.checkCanFire(legCount, totalStake)
 *
 * On success-path: builds all legs, fires Promise.all with per-leg
 * timeout, computes statuses, writes dryrun.jsonl, schedules paper eval.
 *
 * Aborted reason (string) is set when pre-fire gate denies. legs is
 * empty in that case (matches Python's `legs: []` when aborted before
 * fire).
 */
export async function fireArb(
  req: FireRequest,
  walletPool: Wallet[],
  dryRun: boolean = DRY_RUN_DEFAULT,
): Promise<ArbFireResult> {
  const firedAt = Date.now() / 1000;
  const legCount = req.entries.length;
  const totalStake = req.entries.reduce((s, l) => s + l.expectedSizeUsdc, 0);
  const expectedPayout = 1.0; // placeholder, mirrors Python schema
  const expectedCost = totalStake;

  // Pre-fire gates ----------------------------------------------------
  if (isKilled()) {
    return makeAbortedResult(req, firedAt, dryRun, 'kill switch active', expectedCost, expectedPayout);
  }
  const can = await checkCanFire(legCount, totalStake);
  if (!can.allowed) {
    return makeAbortedResult(req, firedAt, dryRun, can.reason ?? 'risk gate', expectedCost, expectedPayout);
  }

  // Min-net guard (Phase 19v6 — mosquito reject) ---------------------
  // We don't have profit numbers from the request directly; the radar
  // pre-filters this before POSTing /fire. If the operator wants to
  // double-check it on the executor side, populate it on FireRequest
  // (TODO TS-3 follow-up).

  // Wallet assignment -------------------------------------------------
  let wallets: Wallet[];
  try {
    wallets = assignLegs(walletPool, legCount);
  } catch (err) {
    return makeAbortedResult(
      req, firedAt, dryRun,
      `wallet assignment: ${err instanceof Error ? err.message : String(err)}`,
      expectedCost, expectedPayout,
    );
  }

  // Parallel fire with per-leg timeout --------------------------------
  const legPromises = req.entries.map((spec, i) => {
    const wallet = wallets[i];
    if (!wallet) {
      return Promise.resolve<LegResult>({
        legIdx: i,
        platform: spec.platform,
        status: 'rejected',
        expectedPrice: spec.expectedPrice,
        expectedSizeUsdc: spec.expectedSizeUsdc,
        error: 'no wallet assigned (pool exhausted)',
      });
    }
    const fire = fireLeg(req.arbId, i, spec, wallet, dryRun);
    const timeout = new Promise<LegResult>((resolve) =>
      setTimeout(() => resolve({
        legIdx: i,
        platform: spec.platform,
        status: 'rejected',
        expectedPrice: spec.expectedPrice,
        expectedSizeUsdc: spec.expectedSizeUsdc,
        botId: wallet.botId,
        error: `per-leg timeout ${PER_LEG_TIMEOUT_MS}ms`,
      }), PER_LEG_TIMEOUT_MS),
    );
    return Promise.race([fire, timeout]);
  });
  const legs = await Promise.all(legPromises);

  // Aggregate ---------------------------------------------------------
  const statusCounts: Record<string, number> = {};
  for (const l of legs) {
    statusCounts[l.status] = (statusCounts[l.status] ?? 0) + 1;
  }
  const allDryFired = legs.length > 0 && legs.every((l) => l.status === 'dry-fired');
  const simPnl = allDryFired ? expectedPayout - expectedCost : -expectedCost + 1.0;
  // Note: expectedPayout=1 is the placeholder Python uses; sim_pnl
  // formula matches `Scripts/executor/dryrun_log.py` to keep paper
  // analytics aggregations consistent across runtimes.

  const result: ArbFireResult = {
    arbId: req.arbId,
    dealTitle: req.dealTitle,
    dealStructure: req.structure,
    expectedCost,
    expectedPayout,
    simPnl,
    legCount,
    legStatusCounts: statusCounts as ArbFireResult['legStatusCounts'],
    partialLegCount: 0,
    worstPartialShortfallUsdc: 0,
    abortedReason: null,
    fireMode: 'taker',
    dryRun,
    firedAt,
    legs,
  };

  // Phase TS-5c — revert decision planning (pure, no HTTP).
  // In dry-run all legs are 'dry-fired' so the planner returns empty.
  // In real-mode (TS-5a/5c.2), mixed 'filled'/'slipped'/'timeout'/'rejected'
  // statuses trigger the planner, which annotates revertStatus on each leg
  // so dryrun.jsonl carries the decision trail for forensics.
  const revertPlan = planRevert(result);
  annotateLegsWithPlan(result, revertPlan);
  if (revertPlan.legs.length > 0) {
    result.revertPlanReason = revertPlan.arbReason;
  }

  // Persist + schedule paper eval ------------------------------------
  await logArbDecision(result);
  await schedulePaperEvaluation(result);

  // Slippage / mosquito reject signals (Phase 19v6 parity, post-fire)
  if (Math.abs(expectedPayout - expectedCost) < MIN_NET_PER_ARB_USD && allDryFired) {
    // We don't actively cancel here in TS-3 (real-mode is TS-5), but
    // the warning gets surfaced in result.legs via the paper evaluator.
  }
  void SLIPPAGE_TOLERANCE; // referenced; used in TS-5 real-mode path

  return result;
}

function makeAbortedResult(
  req: FireRequest,
  firedAt: number,
  dryRun: boolean,
  reason: string,
  expectedCost: number,
  expectedPayout: number,
): ArbFireResult {
  return {
    arbId: req.arbId,
    dealTitle: req.dealTitle,
    dealStructure: req.structure,
    expectedCost,
    expectedPayout,
    simPnl: 0,
    legCount: req.entries.length,
    legStatusCounts: { 'aborted': req.entries.length } as ArbFireResult['legStatusCounts'],
    partialLegCount: 0,
    worstPartialShortfallUsdc: 0,
    abortedReason: reason,
    fireMode: 'taker',
    dryRun,
    firedAt,
    legs: [],
  };
}
