/**
 * Min-floor logic: after clipping, raise any leg back to its platform
 * minimum (Polymarket/Limitless/SX all $1 by default). Without this,
 * an asymmetric clip on $1/leg cap (e.g. 80¢ vs 20¢ split → $1.60 +
 * $0.40) would have a sub-min leg → exchange rejects → arb breaks.
 */
import { describe, it, expect } from 'vitest';
import { applyPlatformMinFloor, clipToPerTradeCap } from '../../src/risk/limits.js';
import { MAX_PER_TRADE_USD } from '../../src/risk/state.js';

describe('applyPlatformMinFloor', () => {
  it('raises sub-min legs to $1 default, leaves others alone', () => {
    const entries = [
      { expectedSizeUsdc: 1.6 },
      { expectedSizeUsdc: 0.4 }, // below $1 default min
    ];
    const r = applyPlatformMinFloor(entries);
    expect(r.floored).toBe(true);
    expect(r.legsFloored).toBe(1);
    expect(r.extraStakeUsd).toBeCloseTo(0.6, 4); // raised 0.4 → 1.0
    expect(entries[0]!.expectedSizeUsdc).toBe(1.6); // untouched
    expect(entries[1]!.expectedSizeUsdc).toBe(1.0);
    expect(r.finalTotalStakeUsd).toBeCloseTo(2.6, 4);
  });

  it('respects per-leg minOrderSizeUsdc override', () => {
    // Some Polymarket markets ship a different min via the leg spec.
    const entries = [
      { expectedSizeUsdc: 0.5, minOrderSizeUsdc: 0.10 }, // override → no floor needed
      { expectedSizeUsdc: 0.5, minOrderSizeUsdc: 2.00 }, // override → must reach $2
    ];
    const r = applyPlatformMinFloor(entries);
    expect(r.floored).toBe(true);
    expect(r.legsFloored).toBe(1);
    expect(entries[0]!.expectedSizeUsdc).toBe(0.5); // 0.5 ≥ 0.10 min, untouched
    expect(entries[1]!.expectedSizeUsdc).toBe(2.0); // 0.5 → 2.0
    expect(r.extraStakeUsd).toBeCloseTo(1.5, 4);
  });

  it('no-op when all legs already ≥ min', () => {
    const entries = [
      { expectedSizeUsdc: 5 },
      { expectedSizeUsdc: 5 },
    ];
    const r = applyPlatformMinFloor(entries);
    expect(r.floored).toBe(false);
    expect(r.legsFloored).toBe(0);
    expect(r.extraStakeUsd).toBe(0);
    expect(entries[0]!.expectedSizeUsdc).toBe(5);
    expect(entries[1]!.expectedSizeUsdc).toBe(5);
  });

  it('clip → floor pipeline: stake can end above cap (operator-accepted)', () => {
    // 2-leg arb, radar wanted skewed 80¢:20¢ split with big size.
    // We use 100× cap as the bias so even MAX=55 default exercises the path.
    const cap2 = MAX_PER_TRADE_USD * 2;
    const entries = [
      { expectedSizeUsdc: cap2 * 100 * 0.8 },
      { expectedSizeUsdc: cap2 * 100 * 0.2 },
    ];
    const clip = clipToPerTradeCap(entries);
    expect(clip.clipped).toBe(true);
    // After clip: leg1 = cap2 × 0.8, leg2 = cap2 × 0.2.
    // For cap2=2 (live env), leg2=$0.40 — below $1 min, must floor.
    // For cap2=110 (test default), leg2=$22 — above $1, no floor.
    const floor = applyPlatformMinFloor(entries);
    if (MAX_PER_TRADE_USD * 0.2 * 2 < 1.0) {
      expect(floor.floored).toBe(true);
      // Total can exceed the cap when flooring occurred.
      expect(floor.finalTotalStakeUsd).toBeGreaterThanOrEqual(cap2);
    } else {
      expect(floor.floored).toBe(false);
    }
  });

  it('floors all legs when they all fell below min', () => {
    // 3-leg arb with tiny clipped stakes — all need flooring.
    const entries = [
      { expectedSizeUsdc: 0.30 },
      { expectedSizeUsdc: 0.30 },
      { expectedSizeUsdc: 0.30 },
    ];
    const r = applyPlatformMinFloor(entries);
    expect(r.legsFloored).toBe(3);
    for (const e of entries) expect(e.expectedSizeUsdc).toBe(1.0);
    expect(r.finalTotalStakeUsd).toBe(3.0);
    expect(r.extraStakeUsd).toBeCloseTo(2.1, 4);
  });
});
