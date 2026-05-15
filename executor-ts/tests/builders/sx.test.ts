/**
 * SX Bet OrderFill builder — golden signature parity tests.
 *
 * Python golden generated 08.05.2026 from `Scripts/executor/builders.py`
 * via `_sign_sx_order_fill`. The TS path through viem MUST produce
 * byte-identical signatures.
 *
 * Same fixture private key as poly.test.ts (0x11...11 — public test
 * vector, no real funds).
 */

import { describe, expect, it } from 'vitest';
import {
  buildSxOrder,
  filterMatchableOrders,
  matchOrders,
  type SxMakerOrder,
} from '../../src/builders/sx.js';
import type { Wallet } from '../../src/types/wallet.js';

const PRIV = `0x${'11'.repeat(32)}` as const;
const SIGNER = '0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A' as const;
const MARKET =
  '0xf764d133c782912dbe7f915a1733c63bf6bebd1f5b4b32e0960d7ae80f2c0285' as const;

const W: Wallet = {
  botId: 'bot1',
  ethAddress: SIGNER,
  canSign: true,
  signatureType: 0,
};

const PYTHON_GOLDEN_SX =
  '0xebd882f046cc197d4910d089d756f45fba111e542d92c732b421bbf9c6d49c41' +
  '1fed01ed8d367aa58bbf608a00038871709906363e03b5eb032586f18f15dffb1b';

describe('buildSxOrder — golden parity with Python', () => {
  it('signs OrderFill identically to Python eth_account path', async () => {
    // Provide enough maker depth on outcome 2 for a $5 taker on outcome 1
    // to fill completely at takerPrice 0.40 + slippage 0.005 = 0.405.
    // Maker pct = 1 - takerPrice = 0.6 → 6e19 scaled.
    const orders: SxMakerOrder[] = [
      {
        orderHash:
          '0x1111111111111111111111111111111111111111111111111111111111111111',
        percentageOdds: '60000000000000000000', // 0.6 maker → 0.4 taker
        isMakerBettingOutcomeOne: false, // outcome 2 — opposite of taker
        totalBetSize: '10000000', // $10 fillable
        fillAmount: '0',
        orderStatus: 'ACTIVE',
      },
    ];
    const built = await buildSxOrder({
      marketHash: MARKET,
      outcome: 1,
      takerPrice: 0.4,
      sizeUsdc: 5,
      wallet: W,
      orders,
      slippageTolerance: 0.005,
      privateKey: PRIV,
    });

    expect(built.signed).toBe(true);
    expect(built.body.takerSig).toBe(PYTHON_GOLDEN_SX);
    expect(built.body.fillAmount).toBe('5000000');
  });
});

describe('SX matchable filter + greedy match', () => {
  it('filters opposite-outcome maker orders only', () => {
    const orders: SxMakerOrder[] = [
      // Wants taker on 1, so we keep makers on 2.
      {
        orderHash: ('0x' + '01'.repeat(32)) as `0x${string}`,
        percentageOdds: '50000000000000000000', // 0.5
        isMakerBettingOutcomeOne: false, // outcome 2 — eligible
        totalBetSize: '5000000',
        fillAmount: '0',
        orderStatus: 'ACTIVE',
      },
      {
        orderHash: ('0x' + '02'.repeat(32)) as `0x${string}`,
        percentageOdds: '50000000000000000000',
        isMakerBettingOutcomeOne: true, // outcome 1 — same side, drop
        totalBetSize: '5000000',
        fillAmount: '0',
        orderStatus: 'ACTIVE',
      },
    ];
    const matchable = filterMatchableOrders(orders, 1);
    expect(matchable).toHaveLength(1);
    expect(matchable[0]!.orderHash).toBe('0x' + '01'.repeat(32));
  });

  it('skips inactive orders', () => {
    const orders: SxMakerOrder[] = [
      {
        orderHash: ('0x' + '03'.repeat(32)) as `0x${string}`,
        percentageOdds: '50000000000000000000',
        isMakerBettingOutcomeOne: false,
        totalBetSize: '5000000',
        fillAmount: '0',
        orderStatus: 'CANCELLED', // NOT active → drop
      },
    ];
    expect(filterMatchableOrders(orders, 1)).toHaveLength(0);
  });

  it('uses old orderSizeFillable field if present (Phase 19v26 forward-compat)', () => {
    const orders: SxMakerOrder[] = [
      {
        orderHash: ('0x' + '04'.repeat(32)) as `0x${string}`,
        percentageOdds: '50000000000000000000',
        isMakerBettingOutcomeOne: false,
        // Both old and new fields present — old wins.
        orderSizeFillable: '7000000',
        totalBetSize: '99999999',
        fillAmount: '0',
        orderStatus: 'ACTIVE',
      },
    ];
    const m = filterMatchableOrders(orders, 1);
    expect(m[0]!.fillableUsdc).toBe(7);
  });

  it('greedy-matches in best-price order', () => {
    const matchable = [
      { orderHash: ('0x' + '05'.repeat(32)) as `0x${string}`,
        makerPct: 0.6, takerPrice: 0.4, fillableUsdc: 3, raw: {} as SxMakerOrder },
      { orderHash: ('0x' + '06'.repeat(32)) as `0x${string}`,
        makerPct: 0.55, takerPrice: 0.45, fillableUsdc: 5, raw: {} as SxMakerOrder },
    ];
    const r = matchOrders(matchable, 5, 0.5);
    // Lower price first: 0x05 (3 USDC) then 0x06 (2 USDC of 5 available)
    expect(r.matched.map((m) => m.takerAmountUsdc)).toEqual([3, 2]);
    expect(r.filledUsdc).toBe(5);
    expect(r.partial).toBe(false);
  });

  it('stops when next price exceeds slippage cap', () => {
    const matchable = [
      { orderHash: ('0x' + '07'.repeat(32)) as `0x${string}`,
        makerPct: 0.6, takerPrice: 0.4, fillableUsdc: 2, raw: {} as SxMakerOrder },
      { orderHash: ('0x' + '08'.repeat(32)) as `0x${string}`,
        makerPct: 0.4, takerPrice: 0.6, fillableUsdc: 10, raw: {} as SxMakerOrder },
    ];
    const r = matchOrders(matchable, 10, 0.5);   // cap below second order
    expect(r.matched).toHaveLength(1);
    expect(r.partial).toBe(true);
    expect(r.shortfallUsdc).toBe(8);
  });
});

describe('buildSxOrder — invariants', () => {
  it('rejects out-of-range outcome', async () => {
    await expect(
      buildSxOrder({
        marketHash: MARKET,
        // biome-ignore lint/suspicious/noExplicitAny: testing runtime guard
        outcome: 3 as any,
        takerPrice: 0.5,
        sizeUsdc: 5,
        wallet: W,
        orders: [],
      }),
    ).rejects.toThrow(/outcome must be 1 or 2/);
  });

  it('rejects below-min size', async () => {
    await expect(
      buildSxOrder({
        marketHash: MARKET,
        outcome: 1,
        takerPrice: 0.5,
        sizeUsdc: 0.5,
        wallet: W,
        orders: [],
      }),
    ).rejects.toThrow(/size below SX min/);
  });

  it('emits empty signature when wallet.canSign=false', async () => {
    const dryW: Wallet = { ...W, canSign: false };
    const orders: SxMakerOrder[] = [
      {
        orderHash: ('0x' + '0a'.repeat(32)) as `0x${string}`,
        percentageOdds: '50000000000000000000',
        isMakerBettingOutcomeOne: false,
        totalBetSize: '10000000',
        fillAmount: '0',
        orderStatus: 'ACTIVE',
      },
    ];
    const built = await buildSxOrder({
      marketHash: MARKET,
      outcome: 1,
      takerPrice: 0.5,
      sizeUsdc: 3,
      wallet: dryW,
      orders,
      privateKey: PRIV,   // present, but canSign false → still no sign
    });
    expect(built.signed).toBe(false);
    expect(built.body.takerSig).toBe('');
  });

  it('targets POST /v1/orders/fill/v2 URL', async () => {
    const built = await buildSxOrder({
      marketHash: MARKET,
      outcome: 1,
      takerPrice: 0.5,
      sizeUsdc: 3,
      wallet: W,
      orders: [],
    });
    expect(built.wouldPostUrl).toBe('https://api.sx.bet/v1/orders/fill/v2');
    expect(built.platform).toBe('sx_bet');
  });
});
