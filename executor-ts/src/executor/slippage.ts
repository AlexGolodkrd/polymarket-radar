/**
 * Slippage evaluation — pure decision logic.
 *
 * Mirrors Python `Scripts/executor/atomic.py` slippage check (≈line 240):
 * a fill counts as success only if its price is within `tolerance` of the
 * expected price. Anything worse is "slipped" and the leg must be reverted
 * (market-sold) to flatten the would-be arb position.
 *
 * Why pure decision logic instead of inline arithmetic in atomic.ts:
 *   - testable in isolation (no exchange mocks, no async)
 *   - one source of truth for the tolerance threshold
 *   - easy to extend later (e.g. asymmetric tolerance: tighter on the
 *     buy side, looser on sell — Phase TS-5f territory)
 *
 * SLIPPAGE_TOLERANCE source of truth: env `SLIPPAGE_TOLERANCE` (default
 * 0.005 = 50 bps = 0.5¢ on $1 notional). Matches Python's identical
 * env-driven default. Phase 9eee bumped from 0.001 → 0.005 because
 * 0.001 was over-cancelling normal fills inside healthy spreads.
 */

export const DEFAULT_SLIPPAGE_TOLERANCE = Number(
  process.env.SLIPPAGE_TOLERANCE ?? '0.005',
);

export interface SlippageDecision {
  /** True iff |fill - expected| ≤ tolerance — leg counts as successful. */
  within: boolean;
  /** Absolute delta in price units (e.g. 0.012 = 1.2¢). */
  deltaAbs: number;
  /** Signed delta: positive = worse than expected on a BUY, better on a SELL. */
  deltaSigned: number;
  /** Tolerance used for this decision (echo back for logging). */
  toleranceUsed: number;
  /** What atomic.ts should do with this leg. */
  recommended: 'keep' | 'revert';
}

/**
 * Decide whether a fill price is acceptable.
 *
 * @param expectedPrice — what the order was placed at (e.g. 0.55)
 * @param fillPrice     — what actually matched on-chain / on-exchange
 * @param tolerance     — max acceptable absolute deviation (default 0.005)
 *
 * Note: `deltaSigned = fillPrice - expectedPrice` so positive deltas mean
 * the fill came in at a HIGHER price. For a BUY, higher fill = worse for
 * us; for a SELL, higher fill = better. atomic.ts doesn't currently use
 * the signed value, but it's surfaced for diagnostics and future
 * asymmetric-tolerance work.
 */
/**
 * FP epsilon for the `<= tolerance` comparison. Without this, e.g.
 * `(0.505 - 0.5) === 0.005000000000000004` and a strict `<=` rejects
 * the inclusive boundary by a handful of ULPs. 1e-9 is 6+ orders of
 * magnitude smaller than any sane real tolerance (smallest used in
 * practice is 0.001 = 10⁶ × 1e-9), so this can never widen tolerance
 * in any meaningful way — it only restores semantic correctness on
 * the boundary.
 */
const FP_EPSILON = 1e-9;

export function evaluateSlippage(
  expectedPrice: number,
  fillPrice: number,
  tolerance: number = DEFAULT_SLIPPAGE_TOLERANCE,
): SlippageDecision {
  const deltaSigned = fillPrice - expectedPrice;
  const deltaAbs = Math.abs(deltaSigned);
  const within = deltaAbs <= tolerance + FP_EPSILON;
  return {
    within,
    deltaAbs,
    deltaSigned,
    toleranceUsed: tolerance,
    recommended: within ? 'keep' : 'revert',
  };
}
