/**
 * Tests for `executeRevertPlan` in src/executor/revert.ts — the
 * Phase TS-5c.2 addition that actually POSTs the SELL orders to
 * flatten exposure after a partial fill.
 *
 * Strategy: inject a fake `sellHandler` (the LegSellHandler callback)
 * that captures the spec it was called with and returns a synthetic
 * LegResult. This avoids hitting any real HTTP path while still
 * exercising the full plan-execution flow.
 *
 * Coverage:
 *   - SELL handler called for every live leg in the plan
 *   - sellSpec has opposite side + aggressive price + FOK orderType
 *   - sellSpec.expectedSizeUsdc uses fillSizeUsdc when available
 *     (flattens only the taken-on amount, not the originally planned)
 *   - handler returning 'filled' → revertStatus='sold'
 *   - handler returning 'slipped' → revertStatus='sold' (warn but OK)
 *   - handler returning 'rejected'/'timeout' → revertStatus='failed'
 *   - handler throwing → revertStatus='failed' with reason
 *   - idempotency: pre-existing 'sold' status skipped
 *   - empty plan → no-op (no handler calls)
 */
import { describe, expect, it } from 'vitest';
import {
  planRevert,
  executeRevertPlan,
  annotateLegsWithPlan,
  type LegSellHandler,
} from '../../src/executor/revert.js';
import type { ArbFireResult, LegResult } from '../../src/executor/paper.js';
import type { LegSpec } from '../../src/types/deal.js';
import type { Wallet } from '../../src/types/wallet.js';

const wallet = (id: string): Wallet => ({
  botId: id,
  ethAddress: '0x0000000000000000000000000000000000000001',
  canSign: false,
  signatureType: 0,
});

const polySpec = (over: Partial<LegSpec> = {}): LegSpec => ({
  platform: 'polymarket',
  side: 'BUY',
  expectedPrice: 0.55,
  expectedSizeUsdc: 10,
  tokenId: 'tok-1',
  ...over,
});

const result = (legs: Partial<LegResult>[]): ArbFireResult => ({
  arbId: 'arb-X',
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
    expectedPrice: 0.55,
    expectedSizeUsdc: 10,
    ...l,
  })) as LegResult[],
});

describe('executeRevertPlan', () => {
  it('no-op when plan is empty', async () => {
    const r = result([{ status: 'filled' }, { status: 'filled' }]);
    const plan = planRevert(r);
    annotateLegsWithPlan(r, plan);
    let called = 0;
    const handler: LegSellHandler = async () => {
      called++;
      throw new Error('should not be called');
    };
    await executeRevertPlan(r, plan, [polySpec(), polySpec()], [wallet('bot1'), wallet('bot2')], handler);
    expect(called).toBe(0);
  });

  it('builds opposite-side market spec with aggressive price + FOK', async () => {
    const r = result([
      { status: 'filled', fillPrice: 0.55, fillSizeUsdc: 9.5, expectedSizeUsdc: 10 },
      { status: 'timeout' },
    ]);
    const plan = planRevert(r);
    annotateLegsWithPlan(r, plan);
    const captured: LegSpec[] = [];
    const handler: LegSellHandler = async (spec) => {
      captured.push(spec);
      return {
        legIdx: 0,
        platform: 'polymarket',
        status: 'filled',
        expectedPrice: spec.expectedPrice,
        expectedSizeUsdc: spec.expectedSizeUsdc,
        fillPrice: spec.expectedPrice,
        fillSizeUsdc: spec.expectedSizeUsdc,
      };
    };
    await executeRevertPlan(
      r,
      plan,
      [polySpec({ side: 'BUY' }), polySpec({ side: 'BUY' })],
      [wallet('bot1'), wallet('bot2')],
      handler,
    );
    expect(captured.length).toBe(1);
    expect(captured[0]!.side).toBe('SELL'); // opposite of original BUY
    expect(captured[0]!.expectedPrice).toBe(0.01); // floor for aggressive SELL
    expect(captured[0]!.expectedSizeUsdc).toBe(9.5); // uses fillSizeUsdc not expectedSizeUsdc
    expect(captured[0]!.orderType).toBe('FOK');
  });

  it('uses expectedSizeUsdc fallback when fillSizeUsdc missing', async () => {
    const r = result([
      { status: 'slipped', fillPrice: 0.55, expectedSizeUsdc: 20 /* no fillSizeUsdc */ },
      { status: 'rejected' },
    ]);
    const plan = planRevert(r);
    annotateLegsWithPlan(r, plan);
    const captured: LegSpec[] = [];
    const handler: LegSellHandler = async (spec) => {
      captured.push(spec);
      return {
        legIdx: 0,
        platform: 'polymarket',
        status: 'filled',
        expectedPrice: spec.expectedPrice,
        expectedSizeUsdc: spec.expectedSizeUsdc,
      };
    };
    await executeRevertPlan(r, plan, [polySpec(), polySpec()], [wallet('bot1'), wallet('bot2')], handler);
    expect(captured[0]!.expectedSizeUsdc).toBe(20); // expectedSizeUsdc fallback
  });

  it('opposite side: SELL → BUY at ceiling 0.99', async () => {
    const r = result([
      { status: 'filled', fillPrice: 0.45, fillSizeUsdc: 5 },
      { status: 'timeout' },
    ]);
    const plan = planRevert(r);
    annotateLegsWithPlan(r, plan);
    const captured: LegSpec[] = [];
    const handler: LegSellHandler = async (spec) => {
      captured.push(spec);
      return {
        legIdx: 0,
        platform: 'polymarket',
        status: 'filled',
        expectedPrice: spec.expectedPrice,
        expectedSizeUsdc: spec.expectedSizeUsdc,
      };
    };
    await executeRevertPlan(
      r,
      plan,
      [polySpec({ side: 'SELL' }), polySpec({ side: 'SELL' })],
      [wallet('bot1'), wallet('bot2')],
      handler,
    );
    expect(captured[0]!.side).toBe('BUY');
    expect(captured[0]!.expectedPrice).toBe(0.99);
  });

  it('handler success → revertStatus=sold', async () => {
    const r = result([
      { status: 'filled', fillSizeUsdc: 5 },
      { status: 'rejected' },
    ]);
    const plan = planRevert(r);
    annotateLegsWithPlan(r, plan);
    const handler: LegSellHandler = async (spec) => ({
      legIdx: 0,
      platform: 'polymarket',
      status: 'filled',
      expectedPrice: spec.expectedPrice,
      expectedSizeUsdc: spec.expectedSizeUsdc,
      fillPrice: spec.expectedPrice,
      fillSizeUsdc: spec.expectedSizeUsdc,
    });
    await executeRevertPlan(r, plan, [polySpec(), polySpec()], [wallet('bot1'), wallet('bot2')], handler);
    expect(r.legs[0]!.revertStatus).toBe('sold');
    expect(r.legs[0]!.revertReason).toMatch(/flatten succeeded/);
  });

  it('handler slipped → revertStatus=sold with warning in reason', async () => {
    const r = result([
      { status: 'filled', fillSizeUsdc: 5 },
      { status: 'timeout' },
    ]);
    const plan = planRevert(r);
    annotateLegsWithPlan(r, plan);
    const handler: LegSellHandler = async (spec) => ({
      legIdx: 0,
      platform: 'polymarket',
      status: 'slipped',
      expectedPrice: spec.expectedPrice,
      expectedSizeUsdc: spec.expectedSizeUsdc,
      fillPrice: spec.expectedPrice + 0.1,
      fillSizeUsdc: spec.expectedSizeUsdc,
      extra: { slippage_delta_abs: 0.1 },
    });
    await executeRevertPlan(r, plan, [polySpec(), polySpec()], [wallet('bot1'), wallet('bot2')], handler);
    expect(r.legs[0]!.revertStatus).toBe('sold');
    expect(r.legs[0]!.revertReason).toMatch(/slipped/);
  });

  it('handler rejected/timeout → revertStatus=failed', async () => {
    const r = result([
      { status: 'filled', fillSizeUsdc: 5 },
      { status: 'rejected' },
    ]);
    const plan = planRevert(r);
    annotateLegsWithPlan(r, plan);
    const handler: LegSellHandler = async (spec) => ({
      legIdx: 0,
      platform: 'polymarket',
      status: 'rejected',
      expectedPrice: spec.expectedPrice,
      expectedSizeUsdc: spec.expectedSizeUsdc,
      error: 'orderbook empty',
    });
    await executeRevertPlan(r, plan, [polySpec(), polySpec()], [wallet('bot1'), wallet('bot2')], handler);
    expect(r.legs[0]!.revertStatus).toBe('failed');
    expect(r.legs[0]!.revertReason).toMatch(/sell rejected.*orderbook empty/);
  });

  it('handler throws → revertStatus=failed with reason', async () => {
    const r = result([
      { status: 'filled', fillSizeUsdc: 5 },
      { status: 'timeout' },
    ]);
    const plan = planRevert(r);
    annotateLegsWithPlan(r, plan);
    const handler: LegSellHandler = async () => {
      throw new Error('network down');
    };
    await executeRevertPlan(r, plan, [polySpec(), polySpec()], [wallet('bot1'), wallet('bot2')], handler);
    expect(r.legs[0]!.revertStatus).toBe('failed');
    expect(r.legs[0]!.revertReason).toMatch(/network down/);
  });

  it('idempotent: pre-existing sold status skipped (no double-sell)', async () => {
    const r = result([
      { status: 'filled', fillSizeUsdc: 5 },
      { status: 'rejected' },
    ]);
    const plan = planRevert(r);
    annotateLegsWithPlan(r, plan);
    // Simulate a previous attempt that succeeded
    r.legs[0]!.revertStatus = 'sold';
    r.legs[0]!.revertReason = 'previously sold';
    let called = 0;
    const handler: LegSellHandler = async () => {
      called++;
      throw new Error('should not be called');
    };
    await executeRevertPlan(r, plan, [polySpec(), polySpec()], [wallet('bot1'), wallet('bot2')], handler);
    expect(called).toBe(0);
    expect(r.legs[0]!.revertStatus).toBe('sold'); // unchanged
    expect(r.legs[0]!.revertReason).toBe('previously sold'); // unchanged
  });

  it('missing wallet at legIdx → revertStatus=failed', async () => {
    const r = result([
      { status: 'filled', fillSizeUsdc: 5 },
      { status: 'timeout' },
    ]);
    const plan = planRevert(r);
    annotateLegsWithPlan(r, plan);
    const handler: LegSellHandler = async () => {
      throw new Error('should not be called');
    };
    // Pass empty wallets array
    await executeRevertPlan(r, plan, [polySpec(), polySpec()], [], handler);
    expect(r.legs[0]!.revertStatus).toBe('failed');
    expect(r.legs[0]!.revertReason).toMatch(/no wallet.*spec/);
  });
});
