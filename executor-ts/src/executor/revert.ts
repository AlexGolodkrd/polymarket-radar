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
