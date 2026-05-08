/**
 * Limitless Order builder — golden signature parity test.
 * Python golden generated 08.05.2026.
 */

import { describe, expect, it } from 'vitest';
import { buildLimitlessOrder } from '../../src/builders/limitless.js';
import type { Wallet } from '../../src/types/wallet.js';

const PRIV = `0x${'11'.repeat(32)}` as const;
const SIGNER = '0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A' as const;

const W: Wallet = {
  botId: 'bot1',
  ethAddress: SIGNER,
  canSign: true,
  signatureType: 0,
};

const FIXED_SALT = 99999999999999999999n;
const FIXED_TOKEN = '12345678901234567890';
const FIXED_EXPIRATION = 1740000000n;

const PYTHON_GOLDEN_LIMITLESS =
  '0x8c856a2c5193ef1df2f014cecb63b8c323dd5de22906e7ea81656cd77aec8b13' +
  '3170bbff7a21384316bfe4af76d9d354b605ec74f25b6a96dd9f55257ea754d61b';

describe('buildLimitlessOrder — golden parity with Python', () => {
  it('signs Order identically to Python eth_account path', async () => {
    const built = await buildLimitlessOrder({
      slug: 'test-market',
      tokenId: FIXED_TOKEN,
      side: 'BUY',
      price: 0.5,
      sizeUsdc: 10,
      wallet: W,
      salt: FIXED_SALT,
      expirationOverride: FIXED_EXPIRATION,
      privateKey: PRIV,
    });
    expect(built.signed).toBe(true);
    expect(built.body.order.signature).toBe(PYTHON_GOLDEN_LIMITLESS);
  });
});

describe('buildLimitlessOrder — invariants', () => {
  it('SELL flips maker/taker amounts (Phase 19v23)', async () => {
    const built = await buildLimitlessOrder({
      slug: 't',
      tokenId: FIXED_TOKEN,
      side: 'SELL',
      price: 0.5,
      sizeUsdc: 10,
      wallet: W,
      salt: FIXED_SALT,
      expirationOverride: FIXED_EXPIRATION,
    });
    expect(built.body.order.makerAmount).toBe(20_000_000n);
    expect(built.body.order.takerAmount).toBe(10_000_000n);
    expect(built.body.order.side).toBe(1);
  });

  it('rejects out-of-range price', async () => {
    await expect(
      buildLimitlessOrder({
        slug: 't',
        tokenId: FIXED_TOKEN,
        side: 'BUY',
        price: 1.5,
        sizeUsdc: 10,
        wallet: W,
      }),
    ).rejects.toThrow(/price out of range/);
  });

  it('rejects below-min size', async () => {
    await expect(
      buildLimitlessOrder({
        slug: 't',
        tokenId: FIXED_TOKEN,
        side: 'BUY',
        price: 0.5,
        sizeUsdc: 0.5,
        wallet: W,
      }),
    ).rejects.toThrow(/size below Limitless min/);
  });

  it('targets POST /orders URL', async () => {
    const built = await buildLimitlessOrder({
      slug: 'foo',
      tokenId: FIXED_TOKEN,
      side: 'BUY',
      price: 0.5,
      sizeUsdc: 5,
      wallet: W,
      salt: FIXED_SALT,
      expirationOverride: FIXED_EXPIRATION,
    });
    expect(built.wouldPostUrl).toBe('https://api.limitless.exchange/orders');
    expect(built.platform).toBe('limitless');
    expect(built.body.marketSlug).toBe('foo');
  });

  it('marketSlug is preserved in body', async () => {
    const built = await buildLimitlessOrder({
      slug: 'sol-above-dollar88753',
      tokenId: FIXED_TOKEN,
      side: 'BUY',
      price: 0.05,
      sizeUsdc: 5,
      wallet: W,
      salt: FIXED_SALT,
      expirationOverride: FIXED_EXPIRATION,
    });
    expect(built.body.marketSlug).toBe('sol-above-dollar88753');
  });
});
