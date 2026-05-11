/**
 * Tests for the `expectFill` helper in src/executor/fills.ts — sugar
 * around `registry.register` + slippage check + structured outcome.
 *
 * The helper is what TS-5c.2 real-mode firing calls AFTER the real POST
 * returns with an orderId. Tests drive the registry by simulating the
 * WS bridge feeding events via consumeByOrderId.
 *
 * Coverage:
 *   - within tolerance → {kind:'filled', slippage.within=true}
 *   - beyond tolerance → {kind:'slipped', slippage.within=false}
 *   - no fill event before deadman → {kind:'timeout'}
 *   - explicit tolerance override propagates
 *   - returned slippage object carries delta/recommended for logging
 */
import { describe, expect, it } from 'vitest';
import { registry, expectFill } from '../../src/executor/fills.js';

describe('expectFill', () => {
  it('resolves filled when fill within tolerance', async () => {
    const promise = expectFill({
      arbId: 'arb-1',
      legIdx: 0,
      platform: 'polymarket',
      orderId: 'ord-A',
      expectedPrice: 0.55,
      deadmanMs: 200,
      slippageTolerance: 0.005,
    });
    // Drive the bridge: WS listener calls consumeByOrderId
    setTimeout(() => {
      registry.consumeByOrderId('polymarket', 'ord-A', {
        arbId: '',
        legIdx: 0,
        platform: 'polymarket',
        orderId: 'ord-A',
        fillPrice: 0.551,
        fillSizeUsdc: 5.51,
      });
    }, 10);
    const r = await promise;
    expect(r.kind).toBe('filled');
    if (r.kind !== 'filled') throw new Error('unreachable');
    expect(r.fillPrice).toBe(0.551);
    expect(r.fillSizeUsdc).toBe(5.51);
    expect(r.slippage.within).toBe(true);
    expect(r.slippage.recommended).toBe('keep');
  });

  it('resolves slipped when fill beyond tolerance', async () => {
    const promise = expectFill({
      arbId: 'arb-2',
      legIdx: 1,
      platform: 'polymarket',
      orderId: 'ord-B',
      expectedPrice: 0.55,
      deadmanMs: 200,
      slippageTolerance: 0.005,
    });
    setTimeout(() => {
      registry.consumeByOrderId('polymarket', 'ord-B', {
        arbId: '',
        legIdx: 1,
        platform: 'polymarket',
        orderId: 'ord-B',
        fillPrice: 0.58, // 0.03 above expected — way over tolerance
        fillSizeUsdc: 5.8,
      });
    }, 10);
    const r = await promise;
    expect(r.kind).toBe('slipped');
    if (r.kind !== 'slipped') throw new Error('unreachable');
    expect(r.slippage.within).toBe(false);
    expect(r.slippage.recommended).toBe('revert');
    expect(r.slippage.deltaAbs).toBeCloseTo(0.03);
  });

  it('returns timeout when no fill arrives before deadman', async () => {
    const r = await expectFill({
      arbId: 'arb-3',
      legIdx: 0,
      platform: 'limitless',
      orderId: 'never-fills',
      expectedPrice: 0.42,
      deadmanMs: 50,
    });
    expect(r.kind).toBe('timeout');
    if (r.kind !== 'timeout') throw new Error('unreachable');
    expect(r.reason).toMatch(/timeout/);
  });

  it('respects explicit tighter tolerance', async () => {
    const promise = expectFill({
      arbId: 'arb-4',
      legIdx: 0,
      platform: 'polymarket',
      orderId: 'ord-C',
      expectedPrice: 0.5,
      deadmanMs: 200,
      slippageTolerance: 0.0005, // very tight
    });
    setTimeout(() => {
      registry.consumeByOrderId('polymarket', 'ord-C', {
        arbId: '',
        legIdx: 0,
        platform: 'polymarket',
        orderId: 'ord-C',
        fillPrice: 0.503,
        fillSizeUsdc: 5,
      });
    }, 10);
    const r = await promise;
    // 0.003 > 0.0005 → slipped
    expect(r.kind).toBe('slipped');
  });
});
