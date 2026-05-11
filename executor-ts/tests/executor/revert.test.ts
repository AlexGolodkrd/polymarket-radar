/**
 * Tests for src/executor/revert.ts — pure planner, no HTTP.
 *
 * Decision matrix the planner must implement:
 *   all 'filled'               → no revert (success)
 *   all 'rejected'/'aborted'   → no revert (no exposure)
 *   mix filled + rejected      → revert every 'filled'
 *   mix filled + timeout       → revert every 'filled'
 *   mix slipped + filled       → revert filled AND slipped (slip itself
 *                                is live exposure at wrong price)
 *   single leg 'dry-fired'     → no revert (TS-3 paper path)
 *
 * Each test constructs a minimal ArbFireResult, calls planRevert /
 * annotateLegsWithPlan, asserts:
 *   - plan.legs (which leg indices to flatten)
 *   - plan.arbReason (high-level reason string)
 *   - mutated leg.revertStatus / leg.revertReason after annotation
 */
import { describe, expect, it } from 'vitest';
import { planRevert, annotateLegsWithPlan } from '../../src/executor/revert.js';
import type { ArbFireResult, LegResult } from '../../src/executor/paper.js';

const makeResult = (
  legs: Partial<LegResult>[],
  overrides: Partial<ArbFireResult> = {},
): ArbFireResult => ({
  arbId: 'test-arb',
  dealTitle: 't',
  dealStructure: 'all_yes',
  expectedCost: 0.97,
  expectedPayout: 1.0,
  simPnl: 0.03,
  legCount: legs.length,
  legStatusCounts: {} as ArbFireResult['legStatusCounts'],
  partialLegCount: 0,
  worstPartialShortfallUsdc: 0,
  abortedReason: null,
  fireMode: 'taker',
  dryRun: false,
  firedAt: 0,
  legs: legs.map((l, i) => ({
    legIdx: i,
    platform: 'polymarket',
    status: 'filled',
    expectedPrice: 0.5,
    expectedSizeUsdc: 10,
    ...l,
  })) as LegResult[],
  ...overrides,
});

describe('planRevert', () => {
  it('all legs filled → no revert', () => {
    const r = makeResult([{ status: 'filled' }, { status: 'filled' }, { status: 'filled' }]);
    const plan = planRevert(r);
    expect(plan.legs).toEqual([]);
    expect(plan.arbReason).toBeNull();
  });

  it('all legs rejected → no revert (no exposure)', () => {
    const r = makeResult([{ status: 'rejected' }, { status: 'rejected' }]);
    const plan = planRevert(r);
    expect(plan.legs).toEqual([]);
    expect(plan.arbReason).toBeNull();
  });

  it('all legs aborted → no revert', () => {
    const r = makeResult([{ status: 'aborted' }, { status: 'aborted' }]);
    const plan = planRevert(r);
    expect(plan.legs).toEqual([]);
  });

  it('all legs dry-fired → no revert (TS-3 paper path)', () => {
    const r = makeResult([
      { status: 'dry-fired' },
      { status: 'dry-fired' },
      { status: 'dry-fired' },
    ]);
    const plan = planRevert(r);
    expect(plan.legs).toEqual([]);
    expect(plan.arbReason).toBeNull();
  });

  it('2 filled + 1 rejected → revert both filled', () => {
    const r = makeResult([
      { status: 'filled', legIdx: 0 },
      { status: 'filled', legIdx: 1 },
      { status: 'rejected', legIdx: 2 },
    ]);
    const plan = planRevert(r);
    const idxs = plan.legs.map((e) => e.legIdx).sort();
    expect(idxs).toEqual([0, 1]);
    expect(plan.arbReason).toMatch(/partial fill/);
    expect(plan.arbReason).toMatch(/legs 2/);
  });

  it('1 filled + 1 timeout → revert filled, reason mentions timeout', () => {
    const r = makeResult([
      { status: 'filled', legIdx: 0 },
      { status: 'timeout', legIdx: 1 },
    ]);
    const plan = planRevert(r);
    expect(plan.legs).toHaveLength(1);
    expect(plan.legs[0]!.legIdx).toBe(0);
    expect(plan.legs[0]!.reason).toMatch(/timeout/);
  });

  it('slipped + filled mixed with rejected sibling → revert BOTH', () => {
    const r = makeResult([
      { status: 'slipped', legIdx: 0 },
      { status: 'filled', legIdx: 1 },
      { status: 'rejected', legIdx: 2 },
    ]);
    const plan = planRevert(r);
    expect(plan.legs.map((e) => e.legIdx).sort()).toEqual([0, 1]);
    // The slipped leg's reason should mention 'slipped fill'.
    const slippedEntry = plan.legs.find((e) => e.legIdx === 0)!;
    expect(slippedEntry.reason).toMatch(/slipped/);
    // The plain-filled leg's reason is 'partial fill'.
    const filledEntry = plan.legs.find((e) => e.legIdx === 1)!;
    expect(filledEntry.reason).toMatch(/partial fill/);
  });

  it('slipped alone (no other broken legs) → still no revert (live exposure but arb succeeded modulo bad fill)', () => {
    // Edge case: if every leg is 'slipped' but none broke the arb, the
    // planner returns empty — because brokenLegs.length === 0 path runs.
    // Caller's expected behavior: treat this as success-with-warning and
    // leave to risk module to handle next scan.
    const r = makeResult([{ status: 'slipped' }, { status: 'filled' }]);
    const plan = planRevert(r);
    expect(plan.legs).toEqual([]);
  });
});

describe('annotateLegsWithPlan', () => {
  it('marks live legs as pending, others as none', () => {
    const r = makeResult([
      { status: 'filled', legIdx: 0 },
      { status: 'filled', legIdx: 1 },
      { status: 'timeout', legIdx: 2 },
    ]);
    const plan = planRevert(r);
    annotateLegsWithPlan(r, plan);
    expect(r.legs[0]!.revertStatus).toBe('pending');
    expect(r.legs[1]!.revertStatus).toBe('pending');
    expect(r.legs[2]!.revertStatus).toBe('none');
    expect(r.legs[0]!.revertReason).toMatch(/partial fill/);
  });

  it('all-filled success → every leg revertStatus=none', () => {
    const r = makeResult([{ status: 'filled' }, { status: 'filled' }]);
    const plan = planRevert(r);
    annotateLegsWithPlan(r, plan);
    expect(r.legs.every((l) => l.revertStatus === 'none')).toBe(true);
  });

  it('preserves pre-existing revertStatus if planner did not touch leg', () => {
    // Defensive case: if a later phase already set revertStatus='sold',
    // annotateLegsWithPlan shouldn't downgrade it back to 'none'.
    const r = makeResult([{ status: 'filled' }]);
    r.legs[0]!.revertStatus = 'sold';
    annotateLegsWithPlan(r, { legs: [], arbReason: null });
    expect(r.legs[0]!.revertStatus).toBe('sold');
  });
});
