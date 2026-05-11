/**
 * Position revert planner — given an arb-fire result with a mixed bag of
 * leg statuses, decides which filled legs need to be market-sold to keep
 * the bot's position flat.
 *
 * Mirrors Python `Scripts/executor/atomic.py:revert_filled_legs`. The
 * planner is **pure decision logic** — no HTTP, no signing. It produces
 * a `RevertPlan` that a later step (TS-5c.2, depends on TS-5a real-mode
 * POST helpers) executes.
 *
 * Rationale: if 2 of 3 legs of a 3-way arb fill at expected price but
 * the 3rd times out, we hold long exposure on those 2 legs without the
 * hedge. The mathematically correct response is to sell them at market
 * immediately, before adverse selection eats the position.
 *
 * Algorithm (mirrors Python):
 *   1. Find the partial-fill set: legs with status='filled' AND at least
 *      one leg in the arb is 'rejected'/'slipped'/'timeout'/'aborted'.
 *   2. If all legs filled successfully → revert nothing.
 *   3. If NO legs filled (every leg rejected/timed-out/aborted) → also
 *      revert nothing (we never took on exposure).
 *   4. Else: every 'filled' leg goes into the revert plan with reason
 *      pointing at the failing sibling leg(s).
 *
 * 'slipped' legs are themselves candidates for revert (we filled, but at
 * the wrong price, so we must flatten). 'timeout' / 'rejected' legs do
 * NOT go into the plan (no position to flatten — the order didn't take).
 */
import type { ArbFireResult, LegResult } from './paper.js';
import type { LegSpec } from '../types/deal.js';
import type { Wallet } from '../types/wallet.js';

export interface RevertLegEntry {
  legIdx: number;
  platform: string;
  reason: string;
  /** Echo of the original leg for the executor to build a market-sell against. */
  originalLeg: LegResult;
}

export interface RevertPlan {
  /** Empty if no revert needed. */
  legs: RevertLegEntry[];
  /** High-level reason — used for logging the arb-level decision. */
  arbReason: string | null;
}

/**
 * Predicate: a leg's exposure is "live" (must be flattened) iff it's
 * 'filled' (the success case) OR 'slipped' (filled at bad price — still
 * live, but at a worse cost basis).
 */
function legIsLive(l: LegResult): boolean {
  return l.status === 'filled' || l.status === 'slipped';
}

/**
 * Predicate: a leg's status counts as a "failure that breaks the arb",
 * which obligates the live legs to be reverted.
 */
function legBrokeTheArb(l: LegResult): boolean {
  return (
    l.status === 'rejected' ||
    l.status === 'timeout' ||
    l.status === 'aborted'
  );
}

/**
 * Build the revert plan for an arb result. Pure function — no side effects.
 *
 * Returns `{ legs: [], arbReason: null }` when no action needed (either
 * all-filled success path, or no-fill complete-fail path).
 */
export function planRevert(result: ArbFireResult): RevertPlan {
  const liveLegs = result.legs.filter(legIsLive);
  const brokenLegs = result.legs.filter(legBrokeTheArb);

  // Path A: nothing live → no flattening needed (we have no exposure).
  if (liveLegs.length === 0) {
    return { legs: [], arbReason: null };
  }

  // Path B: nothing broken → arb succeeded, all legs filled cleanly.
  // Hold all the legs (or, in Phase TS-5e, hand off to risk.position
  // tracker). No revert.
  if (brokenLegs.length === 0) {
    return { legs: [], arbReason: null };
  }

  // Path C: partial fill → revert every live leg.
  const brokenIdx = brokenLegs.map((l) => l.legIdx).join(',');
  const brokenStatuses = Array.from(
    new Set(brokenLegs.map((l) => l.status)),
  ).join('/');
  const arbReason =
    `partial fill: legs ${brokenIdx} ${brokenStatuses}, ` +
    `${liveLegs.length} live leg(s) to flatten`;

  const legs: RevertLegEntry[] = liveLegs.map((l) => ({
    legIdx: l.legIdx,
    platform: l.platform,
    reason: l.status === 'slipped'
      ? `slipped fill (delta > tolerance) — sibling legs ${brokenIdx} ${brokenStatuses}`
      : `partial fill — sibling legs ${brokenIdx} ${brokenStatuses}`,
    originalLeg: l,
  }));

  return { legs, arbReason };
}

/**
 * Mutate (in place) leg objects with the planner's verdict so the
 * persisted ArbFireResult row reflects the revert state in dryrun.jsonl.
 *
 * Each live leg gets `revertStatus: 'pending'` + matching reason. Other
 * legs get `revertStatus: 'none'` for consistency with the field's type.
 * Phase TS-5c.2 will flip 'pending' → 'sold' / 'failed' after the real
 * market-sell POST completes.
 */
export function annotateLegsWithPlan(
  result: ArbFireResult,
  plan: RevertPlan,
): void {
  const revertByIdx = new Map(plan.legs.map((e) => [e.legIdx, e]));
  for (const leg of result.legs) {
    const entry = revertByIdx.get(leg.legIdx);
    if (entry) {
      leg.revertStatus = 'pending';
      leg.revertReason = entry.reason;
    } else if (leg.revertStatus === undefined) {
      leg.revertStatus = 'none';
    }
  }
}

/**
 * Caller-injected handler that knows how to build + POST a single
 * SELL/BUY leg to the appropriate exchange. atomic.ts wires this to
 * its existing `fireLeg` so the revert path reuses the same plumbing
 * (signature gate, expectFill, slippage check, timeout).
 *
 * Pattern rationale: revert.ts must not import http_client + builders
 * (those are atomic.ts's concern) — passing a closure keeps the
 * dependency graph clean and lets tests inject a deterministic stub.
 */
export type LegSellHandler = (
  spec: LegSpec,
  wallet: Wallet,
  arbId: string,
  legIdx: number,
) => Promise<LegResult>;

/**
 * Aggressive price for a market-style revert. For a SELL we want to
 * hit the bid, so we quote at the floor; for a BUY (reverting a SELL)
 * we hit the ask at the ceiling. Polymarket's tick is 0.001 and orders
 * at exactly 0 / 1 are rejected, so we leave a small margin.
 */
const REVERT_SELL_FLOOR = 0.01;
const REVERT_BUY_CEILING = 0.99;

/**
 * Build an opposite-side LegSpec for the same outcome. Sizes use the
 * actual filled amount (if available) so we flatten exactly what was
 * taken on, not the originally-intended size.
 */
function makeOppositeSpec(orig: LegSpec, originalLeg: LegResult): LegSpec {
  const oppositeSide: 'BUY' | 'SELL' = orig.side === 'BUY' ? 'SELL' : 'BUY';
  const sellSize =
    typeof originalLeg.fillSizeUsdc === 'number' && originalLeg.fillSizeUsdc > 0
      ? originalLeg.fillSizeUsdc
      : originalLeg.expectedSizeUsdc;
  const aggressivePrice =
    oppositeSide === 'SELL' ? REVERT_SELL_FLOOR : REVERT_BUY_CEILING;
  return {
    ...orig,
    side: oppositeSide,
    expectedPrice: aggressivePrice,
    expectedSizeUsdc: sellSize,
    // FOK = fill-or-kill, closest existing orderType to "flatten now".
    // If the book is too thin to fully flatten, FOK fails loud and the
    // leg ends up revertStatus='failed' — operator must intervene.
    // This is intentionally conservative: partial revert would leave
    // residual exposure that's hard to track.
    orderType: 'FOK',
  };
}

/**
 * Execute a previously-planned revert. For each live leg in the plan,
 * builds an opposite-side market-aggressive order and POSTs it via the
 * caller-supplied handler. Mutates each leg's revertStatus in place:
 *   'sold'   — POST returned filled/slipped (flatten succeeded)
 *   'failed' — POST rejected or threw (operator must intervene)
 *
 * Idempotent: legs already marked 'sold' or 'failed' (e.g., from a
 * previous attempt) are skipped to avoid double-selling on retry.
 *
 * In dry-run mode, planRevert returns empty for an all-dry-fired arb,
 * so this function is a no-op end-to-end.
 */
export async function executeRevertPlan(
  result: ArbFireResult,
  plan: RevertPlan,
  originalSpecs: LegSpec[],
  wallets: Wallet[],
  sellHandler: LegSellHandler,
): Promise<void> {
  if (plan.legs.length === 0) return;
  for (const entry of plan.legs) {
    const leg = entry.originalLeg;
    // Idempotency: skip if already attempted.
    if (leg.revertStatus === 'sold' || leg.revertStatus === 'failed') {
      continue;
    }
    const wallet = wallets[entry.legIdx];
    const origSpec = originalSpecs[entry.legIdx];
    if (!wallet || !origSpec) {
      leg.revertStatus = 'failed';
      leg.revertReason = `no wallet/spec at legIdx=${entry.legIdx} for revert`;
      continue;
    }
    const sellSpec = makeOppositeSpec(origSpec, leg);
    try {
      const sellResult = await sellHandler(
        sellSpec,
        wallet,
        result.arbId,
        entry.legIdx,
      );
      if (sellResult.status === 'filled' || sellResult.status === 'slipped') {
        leg.revertStatus = 'sold';
        leg.revertReason =
          sellResult.status === 'slipped'
            ? `flatten succeeded but slipped (delta ${sellResult.extra?.slippage_delta_abs ?? '?'})`
            : 'flatten succeeded';
      } else {
        leg.revertStatus = 'failed';
        leg.revertReason = `sell ${sellResult.status}: ${sellResult.error ?? 'unknown'}`;
      }
    } catch (err) {
      leg.revertStatus = 'failed';
      leg.revertReason = `sell threw: ${err instanceof Error ? err.message : String(err)}`;
    }
  }
}
