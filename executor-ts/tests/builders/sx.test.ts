/**
 * SX Bet v2 OrderFill builder — golden signature + invariants.
 *
 * Phase audit-5 (15.05.2026) — protocol rewrite. The previous test
 * targeted the dead v1 struct (Details with `executor` field,
 * verifyingContract `0xBE9F69...`, body with orderHashes/takerAmounts).
 * v2 introduces nested `Details { ..., fills: FillObject }`, a flat
 * server-matched POST body, and a new verifyingContract derived from
 * the `EIP712FillHasher` address on SX Network mainnet.
 *
 * Golden signature was generated locally with viem using the same
 * fixture key (`0x11...11`) and parameters as the test (deterministic
 * fillSalt=12345678901234567890, takerPrice=0.4, slippage=0.005,
 * sizeUsdc=5, outcome=1). Re-run sx_golden_compute.mjs if the spec
 * changes again.
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

// Computed via tools/sx_golden_compute.mjs against the v2 docs example
// (domain { name: "SX Bet", version: "6.0", chainId: 4162,
//   verifyingContract: 0x845a2Da2D70fEDe8474b1C8518200798c60aC364 }).
const GOLDEN_SX_V2 =
  '0xaf7c1eb78d3bfbb5408a9d16213b4efe38817369fde9ebd3b5a11c7a34126d11' +
  '1b8a1f4d3ff7362a2128f257ec96356722f64a7acedddda85da16567b99d4c1e1b';

describe('buildSxOrder v2 — golden parity', () => {
  it('signs Details{fills} identically to docs example shape', async () => {
    const built = await buildSxOrder({
      marketHash: MARKET,
      outcome: 1,
      takerPrice: 0.4,
      sizeUsdc: 5,
      wallet: W,
      slippageTolerance: 0.005,
      fillSalt: '12345678901234567890',
      privateKey: PRIV,
    });

    expect(built.signed).toBe(true);
    expect(built.body.takerSig).toBe(GOLDEN_SX_V2);
    expect(built.body.stakeWei).toBe('5000000');
    // takerPrice 0.4 + slippage 0.005 = 0.405 → minMakerPct = 0.595
    expect(built.body.desiredOdds).toBe('59500000000000000000');
    expect(built.body.oddsSlippage).toBe(0);
    expect(built.body.isTakerBettingOutcomeOne).toBe(true);
    expect(built.body.fillSalt).toBe('12345678901234567890');
    expect(built.body.message).toBe('N/A');
    expect(built.body.market).toBe(MARKET);
    expect(built.body.taker).toBe(SIGNER);
    expect(built.body.baseToken.toLowerCase()).toBe(
      '0x6629ce1cf35cc1329ebb4f63202f3f197b3f050b',
    );
  });

  it('targets POST /orders/fill/v2 URL', async () => {
    const built = await buildSxOrder({
      marketHash: MARKET,
      outcome: 1,
      takerPrice: 0.5,
      sizeUsdc: 3,
      wallet: W,
    });
    expect(built.wouldPostUrl).toBe('https://api.sx.bet/orders/fill/v2');
    expect(built.platform).toBe('sx_bet');
  });
});

describe('buildSxOrder v2 — invariants', () => {
  it('rejects out-of-range outcome', async () => {
    await expect(
      buildSxOrder({
        marketHash: MARKET,
        // biome-ignore lint/suspicious/noExplicitAny: testing runtime guard
        outcome: 3 as any,
        takerPrice: 0.5,
        sizeUsdc: 5,
        wallet: W,
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
      }),
    ).rejects.toThrow(/size below SX min/);
  });

  it('emits empty signature when wallet.canSign=false', async () => {
    const dryW: Wallet = { ...W, canSign: false };
    const built = await buildSxOrder({
      marketHash: MARKET,
      outcome: 1,
      takerPrice: 0.5,
      sizeUsdc: 3,
      wallet: dryW,
      privateKey: PRIV,
    });
    expect(built.signed).toBe(false);
    expect(built.body.takerSig).toBe('');
  });

  it('flips isTakerBettingOutcomeOne for outcome 2', async () => {
    const built = await buildSxOrder({
      marketHash: MARKET,
      outcome: 2,
      takerPrice: 0.5,
      sizeUsdc: 3,
      wallet: W,
      privateKey: PRIV,
    });
    expect(built.body.isTakerBettingOutcomeOne).toBe(false);
  });
});

// Legacy match helpers kept for the optional pre-flight liquidity check.
// They no longer feed the v2 firing path — server picks makers itself —
// but the unit tests remain to lock in the math in case a caller wants
// to use them for sizing decisions ahead of the POST.
describe('SX legacy matchable filter + greedy match', () => {
  it('filters opposite-outcome maker orders only', () => {
    const orders: SxMakerOrder[] = [
      {
        orderHash: ('0x' + '01'.repeat(32)) as `0x${string}`,
        percentageOdds: '50000000000000000000',
        isMakerBettingOutcomeOne: false,
        totalBetSize: '5000000',
        fillAmount: '0',
        orderStatus: 'ACTIVE',
      },
      {
        orderHash: ('0x' + '02'.repeat(32)) as `0x${string}`,
        percentageOdds: '50000000000000000000',
        isMakerBettingOutcomeOne: true,
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
        orderStatus: 'CANCELLED',
      },
    ];
    expect(filterMatchableOrders(orders, 1)).toHaveLength(0);
  });

  it('greedy-matches in best-price order', () => {
    const matchable = [
      { orderHash: ('0x' + '05'.repeat(32)) as `0x${string}`,
        makerPct: 0.6, takerPrice: 0.4, fillableUsdc: 3, raw: {} as SxMakerOrder },
      { orderHash: ('0x' + '06'.repeat(32)) as `0x${string}`,
        makerPct: 0.55, takerPrice: 0.45, fillableUsdc: 5, raw: {} as SxMakerOrder },
    ];
    const r = matchOrders(matchable, 5, 0.5);
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
    const r = matchOrders(matchable, 10, 0.5);
    expect(r.matched).toHaveLength(1);
    expect(r.partial).toBe(true);
    expect(r.shortfallUsdc).toBe(8);
  });
});
