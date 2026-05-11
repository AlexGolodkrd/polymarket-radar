/**
 * Tests for src/executor/slippage.ts — pure decision logic, no async.
 *
 * Slippage tolerance default 0.005 (50 bps). Asserts:
 *   - exact-match fill → within=true, delta=0
 *   - fill within tolerance → within=true, recommended='keep'
 *   - fill exactly AT tolerance → within=true (inclusive bound)
 *   - fill beyond tolerance → within=false, recommended='revert'
 *   - asymmetric direction (buy higher vs sell lower) — signed delta sign
 *   - explicit tolerance override
 */
import { describe, expect, it } from 'vitest';
import {
  evaluateSlippage,
  DEFAULT_SLIPPAGE_TOLERANCE,
} from '../../src/executor/slippage.js';

describe('evaluateSlippage', () => {
  it('exact-match fill → within, delta=0', () => {
    const r = evaluateSlippage(0.55, 0.55);
    expect(r.within).toBe(true);
    expect(r.deltaAbs).toBe(0);
    expect(r.deltaSigned).toBe(0);
    expect(r.recommended).toBe('keep');
  });

  it('fill 0.003 above expected → within tolerance', () => {
    const r = evaluateSlippage(0.55, 0.553);
    expect(r.within).toBe(true);
    expect(r.deltaAbs).toBeCloseTo(0.003);
    expect(r.deltaSigned).toBeCloseTo(0.003);
    expect(r.recommended).toBe('keep');
  });

  it('fill exactly AT tolerance (0.005) → within (inclusive bound)', () => {
    const r = evaluateSlippage(0.5, 0.505, 0.005);
    expect(r.within).toBe(true);
    expect(r.deltaAbs).toBeCloseTo(0.005);
    expect(r.recommended).toBe('keep');
  });

  it('fill 0.006 above expected → beyond tolerance → revert', () => {
    const r = evaluateSlippage(0.55, 0.556, 0.005);
    expect(r.within).toBe(false);
    expect(r.deltaAbs).toBeCloseTo(0.006);
    expect(r.recommended).toBe('revert');
  });

  it('fill 0.02 BELOW expected → still beyond tolerance → revert', () => {
    // Even if the delta is favorable to us (we paid less on a BUY), the
    // tolerance is symmetric — anything > tol triggers revert. The
    // rationale: a fill far from expected indicates we hit a price the
    // arb model didn't see, so the implied "edge" is suspect.
    const r = evaluateSlippage(0.55, 0.53);
    expect(r.within).toBe(false);
    expect(r.deltaAbs).toBeCloseTo(0.02);
    expect(r.deltaSigned).toBeCloseTo(-0.02);
    expect(r.recommended).toBe('revert');
  });

  it('explicit tolerance override (e.g. tight 0.001) shows it as toleranceUsed', () => {
    const r = evaluateSlippage(0.5, 0.501, 0.001);
    expect(r.toleranceUsed).toBe(0.001);
    expect(r.within).toBe(true);
  });

  it('explicit tolerance override (looser 0.02) accepts wide drift', () => {
    const r = evaluateSlippage(0.55, 0.565, 0.02);
    expect(r.within).toBe(true);
    expect(r.toleranceUsed).toBe(0.02);
  });

  it('DEFAULT_SLIPPAGE_TOLERANCE is 0.005 unless env override', () => {
    // Strict equality — sanity check the constant export doesn't drift.
    // If env SLIPPAGE_TOLERANCE was set at module load, this test will
    // log the value rather than fail (helps debug CI).
    if (process.env.SLIPPAGE_TOLERANCE === undefined) {
      expect(DEFAULT_SLIPPAGE_TOLERANCE).toBe(0.005);
    } else {
      expect(DEFAULT_SLIPPAGE_TOLERANCE).toBe(
        Number(process.env.SLIPPAGE_TOLERANCE),
      );
    }
  });
});
