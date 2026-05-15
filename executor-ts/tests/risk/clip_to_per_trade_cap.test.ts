/**
 * Per-trade cap CLIPS rather than aborts. Replaces the old hard-block
 * behavior: radar's sizing was getting thrown out wholesale when the
 * profit-maximizing stake exceeded the operator's risk envelope.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { clipToPerTradeCap } from '../../src/risk/limits.js';
import { MAX_PER_TRADE_USD } from '../../src/risk/state.js';

describe('clipToPerTradeCap', () => {
  beforeEach(() => {
    // Sanity: tests assume the default cap. We don't override env here —
    // any cap change would still let the proportionality assertions hold.
    void MAX_PER_TRADE_USD;
  });

  it('returns unchanged when total stake under cap', () => {
    const entries = [
      { expectedSizeUsdc: 0.5 },
      { expectedSizeUsdc: 0.4 },
    ];
    const cap = MAX_PER_TRADE_USD * entries.length;
    expect(0.9).toBeLessThanOrEqual(cap);

    const r = clipToPerTradeCap(entries);
    expect(r.clipped).toBe(false);
    expect(r.ratio).toBe(1.0);
    expect(r.clippedTotalStakeUsd).toBeCloseTo(0.9, 6);
    expect(entries[0]!.expectedSizeUsdc).toBe(0.5);
    expect(entries[1]!.expectedSizeUsdc).toBe(0.4);
  });

  it('scales every leg proportionally when total stake over cap', () => {
    // Build stakes that exceed whatever cap is configured, using a
    // factor of 20× per leg so MAX=1 (live) and MAX=55 (default test
    // env) both trigger clipping.
    const factor = 20;
    const e1Original = MAX_PER_TRADE_USD * factor;
    const e2Original = MAX_PER_TRADE_USD * (factor * 0.668); // skew the split
    const entries = [
      { expectedSizeUsdc: e1Original },
      { expectedSizeUsdc: e2Original },
    ];
    const totalOriginal = e1Original + e2Original;
    const cap = MAX_PER_TRADE_USD * 2;
    const r = clipToPerTradeCap(entries);

    expect(r.clipped).toBe(true);
    expect(r.capUsd).toBe(cap);
    expect(r.originalTotalStakeUsd).toBeCloseTo(totalOriginal, 4);
    expect(r.clippedTotalStakeUsd).toBeCloseTo(cap, 6);
    // Each leg should keep its share of the total.
    expect(entries[0]!.expectedSizeUsdc).toBeCloseTo(cap * (e1Original / totalOriginal), 6);
    expect(entries[1]!.expectedSizeUsdc).toBeCloseTo(cap * (e2Original / totalOriginal), 6);
    // Ratio applies uniformly.
    expect(entries[0]!.expectedSizeUsdc / e1Original).toBeCloseTo(r.ratio, 6);
    expect(entries[1]!.expectedSizeUsdc / e2Original).toBeCloseTo(r.ratio, 6);
  });

  it('mutates entries in place (downstream sees clipped values)', () => {
    // Stake = 20× cap so we always exceed it.
    const e1 = { expectedSizeUsdc: MAX_PER_TRADE_USD * 20 };
    const entries = [e1];
    clipToPerTradeCap(entries);
    expect(e1.expectedSizeUsdc).toBeLessThanOrEqual(MAX_PER_TRADE_USD);
  });

  it('zero-stake input is no-op (empty arb defensive)', () => {
    const entries = [{ expectedSizeUsdc: 0 }, { expectedSizeUsdc: 0 }];
    const r = clipToPerTradeCap(entries);
    expect(r.clipped).toBe(false);
    expect(r.ratio).toBe(1.0);
    expect(r.clippedTotalStakeUsd).toBe(0);
  });

  it('single-leg arb at cap exactly does not clip', () => {
    const entries = [{ expectedSizeUsdc: MAX_PER_TRADE_USD }];
    const r = clipToPerTradeCap(entries);
    expect(r.clipped).toBe(false);
    expect(entries[0]!.expectedSizeUsdc).toBe(MAX_PER_TRADE_USD);
  });

  it('3-leg arb scales all three identically', () => {
    // 9× cap on each leg → 27× cap total → 9× over the 3-leg cap. Clip
    // ratio = (cap*3) / (cap*9*3) = 1/9 → each leg lands at cap/3.
    const per = MAX_PER_TRADE_USD * 9;
    const entries = [
      { expectedSizeUsdc: per },
      { expectedSizeUsdc: per },
      { expectedSizeUsdc: per },
    ];
    const cap = MAX_PER_TRADE_USD * 3;
    const r = clipToPerTradeCap(entries);
    expect(r.clipped).toBe(true);
    for (const e of entries) {
      expect(e.expectedSizeUsdc).toBeCloseTo(cap / 3, 6);
    }
  });
});
