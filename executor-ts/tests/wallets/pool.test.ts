/**
 * Tests for src/wallets/pool.ts — focuses on the Phase TS-5b1.5 addition
 * `synthesizeMockWallets`. Real-env `loadWalletsFromEnv` is also covered
 * to assert the mock-wallet helper produces the same shape.
 *
 * Safety invariant under test:
 *   - mock wallets MUST have canSign=false (real-mode signing path is
 *     hard-gated by this — if it ever flipped to true, atomic.ts could
 *     attempt to sign with a non-existent private key and surface a
 *     confusing error in prod)
 *   - addresses MUST be valid 40-hex-char strings (viem rejects others
 *     at EIP-712 hashing time)
 *   - botIds MUST be `bot1`..`botN` so coordinator round-robin matches
 *     Python parity
 */
import { describe, expect, it } from 'vitest';
import {
  loadWalletsFromEnv,
  synthesizeMockWallets,
  assignLegs,
  _resetCursor,
} from '../../src/wallets/pool.js';

describe('synthesizeMockWallets', () => {
  it('returns exactly N wallets with deterministic addresses', () => {
    const ws = synthesizeMockWallets(6);
    expect(ws).toHaveLength(6);
    expect(ws.map((w) => w.botId)).toEqual([
      'bot1',
      'bot2',
      'bot3',
      'bot4',
      'bot5',
      'bot6',
    ]);
    // Addresses are deterministic — same call must produce same values.
    const ws2 = synthesizeMockWallets(6);
    expect(ws.map((w) => w.ethAddress)).toEqual(ws2.map((w) => w.ethAddress));
  });

  it('every mock wallet has canSign=false (safety invariant)', () => {
    const ws = synthesizeMockWallets(6);
    for (const w of ws) {
      expect(w.canSign).toBe(false);
    }
  });

  it('addresses are valid 40-hex-char strings (viem-acceptable)', () => {
    const ws = synthesizeMockWallets(6);
    const validHex = /^0x[0-9a-fA-F]{40}$/;
    for (const w of ws) {
      expect(w.ethAddress).toMatch(validHex);
    }
  });

  it('default count is 6 (matches Python BOT_COUNT)', () => {
    const ws = synthesizeMockWallets();
    expect(ws).toHaveLength(6);
  });

  it('mocks are visually distinguishable from real addresses (all zeros + index)', () => {
    const ws = synthesizeMockWallets(6);
    // Each address should be 0x + 39 zeros + 1 hex digit (the index).
    for (let i = 1; i <= 6; i++) {
      const w = ws[i - 1]!;
      expect(w.ethAddress.toLowerCase()).toBe(
        `0x${'0'.repeat(39)}${i.toString(16)}`,
      );
    }
  });

  it('mock pool integrates with assignLegs (no per-bot duplicates per arb)', () => {
    _resetCursor();
    const pool = synthesizeMockWallets(6);
    // Assigning 3 legs from a 6-bot pool must return 3 distinct bots.
    const assigned = assignLegs(pool, 3);
    const ids = assigned.map((w) => w.botId);
    expect(new Set(ids).size).toBe(3);
  });

  it('ALLOW_WALLET_REUSE=1 lets one wallet serve multiple legs', () => {
    _resetCursor();
    const prev = process.env.ALLOW_WALLET_REUSE;
    process.env.ALLOW_WALLET_REUSE = '1';
    try {
      const pool = synthesizeMockWallets(1);
      // 1 wallet + 3 legs would throw without reuse; with reuse, same bot fills all.
      const assigned = assignLegs(pool, 3);
      expect(assigned.length).toBe(3);
      const ids = assigned.map((w) => w.botId);
      expect(new Set(ids).size).toBe(1);
    } finally {
      if (prev === undefined) delete process.env.ALLOW_WALLET_REUSE;
      else process.env.ALLOW_WALLET_REUSE = prev;
    }
  });

  it('default (no env flag) keeps distinct-wallet requirement', () => {
    _resetCursor();
    const prev = process.env.ALLOW_WALLET_REUSE;
    delete process.env.ALLOW_WALLET_REUSE;
    try {
      const pool = synthesizeMockWallets(1);
      expect(() => assignLegs(pool, 2)).toThrow(/wallet pool too small/);
    } finally {
      if (prev !== undefined) process.env.ALLOW_WALLET_REUSE = prev;
    }
  });

  it('count=0 returns empty array (degenerate but defined)', () => {
    const ws = synthesizeMockWallets(0);
    expect(ws).toEqual([]);
  });
});

describe('loadWalletsFromEnv', () => {
  it('returns empty pool when no BOT*_ETH_ADDRESS set', () => {
    expect(loadWalletsFromEnv({} as NodeJS.ProcessEnv)).toEqual([]);
  });

  it('reads BOT1_ETH_ADDRESS and BOT3_ETH_ADDRESS (gaps allowed)', () => {
    const env = {
      BOT1_ETH_ADDRESS: '0x1111111111111111111111111111111111111111',
      BOT3_ETH_ADDRESS: '0x3333333333333333333333333333333333333333',
    } as unknown as NodeJS.ProcessEnv;
    const ws = loadWalletsFromEnv(env);
    expect(ws.map((w) => w.botId)).toEqual(['bot1', 'bot3']);
    expect(ws[0]!.canSign).toBe(false);
    expect(ws[1]!.canSign).toBe(false);
  });

  it('canSign=true only when BOT*_PRIVATE_KEY is also set', () => {
    const env = {
      BOT1_ETH_ADDRESS: '0x1111111111111111111111111111111111111111',
      BOT1_PRIVATE_KEY: '0xaaaa', // private key value is opaque here
      BOT2_ETH_ADDRESS: '0x2222222222222222222222222222222222222222',
      // no BOT2_PRIVATE_KEY → canSign=false
    } as unknown as NodeJS.ProcessEnv;
    const ws = loadWalletsFromEnv(env);
    expect(ws.find((w) => w.botId === 'bot1')!.canSign).toBe(true);
    expect(ws.find((w) => w.botId === 'bot2')!.canSign).toBe(false);
  });

  it('reads polyApiKey/secret/passphrase when present', () => {
    const env = {
      BOT1_ETH_ADDRESS: '0x1111111111111111111111111111111111111111',
      BOT1_POLY_API_KEY: 'key1',
      BOT1_POLY_SECRET: 'sec1',
      BOT1_POLY_PASSPHRASE: 'pass1',
    } as unknown as NodeJS.ProcessEnv;
    const ws = loadWalletsFromEnv(env);
    expect(ws[0]!.polyApiKey).toBe('key1');
    expect(ws[0]!.polySecret).toBe('sec1');
    expect(ws[0]!.polyPassphrase).toBe('pass1');
  });
});
