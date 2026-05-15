/**
 * Phase audit-2 (11.05.2026) — verify TS fireArb uses radar-supplied
 * expectedPayout instead of the old hardcoded $1 placeholder.
 *
 * Without the fix, CP arbs (face $50-100/leg) produced simPnl =
 * 1 - 50 = -$49 → every paper_results row counted as a loss →
 * paper_stats.win_rate pinned at 0% → graduation gate unreachable.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { tmpdir } from 'node:os';
import { mkdtemp, rm, readFile } from 'node:fs/promises';
import { join } from 'node:path';

vi.mock('../src/risk/limits.js', () => ({
  checkCanFire: async () => ({ allowed: true }),
  snapshot: async () => ({}),
  // No-op clip + floor: pre-date the clip-not-abort change.
  clipToPerTradeCap: (entries: { expectedSizeUsdc: number }[]) => ({
    clipped: false,
    capUsd: Number.POSITIVE_INFINITY,
    originalTotalStakeUsd: entries.reduce((s, e) => s + e.expectedSizeUsdc, 0),
    clippedTotalStakeUsd: entries.reduce((s, e) => s + e.expectedSizeUsdc, 0),
    ratio: 1.0,
  }),
  applyPlatformMinFloor: (entries: { expectedSizeUsdc: number }[]) => ({
    floored: false,
    extraStakeUsd: 0,
    finalTotalStakeUsd: entries.reduce((s, e) => s + e.expectedSizeUsdc, 0),
    legsFloored: 0,
  }),
}));
vi.mock('../src/risk/killswitch.js', () => ({
  isKilled: () => false,
}));
vi.mock('../src/wallets/pool.js', () => ({
  assignLegs: (pool: unknown[], n: number) => pool.slice(0, n),
  jitterMsForLeg: () => 0,
  synthesizeMockWallets: () => [],
}));
vi.mock('../src/wallets/signers.js', () => ({
  getSignerKey: () => undefined,
  registeredCount: () => 0,
}));
// Builders return a successful dry-fired BuiltOrder (no real I/O).
const makeBuilt = (platform: string, price: number, size: number) => ({
  platform,
  body: {},
  wouldPostUrl: 'http://test',
  signed: false,
  expectedPrice: price,
  expectedSizeUsdc: size,
  signPayload: new Uint8Array(),
});
vi.mock('../src/builders/poly.js', () => ({
  buildPolyOrder: async (i: { price: number; sizeUsdc: number }) =>
    makeBuilt('polymarket', i.price, i.sizeUsdc),
}));
vi.mock('../src/builders/sx.js', () => ({
  buildSxOrder: async (i: { takerPrice: number; sizeUsdc: number }) =>
    makeBuilt('sx_bet', i.takerPrice, i.sizeUsdc),
}));
vi.mock('../src/builders/limitless.js', () => ({
  buildLimitlessOrder: async (i: { price: number; sizeUsdc: number }) =>
    makeBuilt('limitless', i.price, i.sizeUsdc),
}));
vi.mock('../src/executor/revert.js', () => ({
  planRevert: () => ({ legs: [], arbReason: null }),
  annotateLegsWithPlan: () => {},
  executeRevertPlan: async () => {},
}));

describe('TS fireArb expectedPayout (phase audit-2)', () => {
  let dataDir: string;
  beforeEach(async () => {
    dataDir = await mkdtemp(join(tmpdir(), 'exec-ts-payout-'));
    process.env.EXECUTIONS_DIR = dataDir;
    process.env.DRY_RUN = '1';
    vi.resetModules();
  });
  afterEach(async () => {
    delete process.env.EXECUTIONS_DIR;
    await rm(dataDir, { recursive: true, force: true });
  });

  it('uses radar-supplied expectedPayout for simPnl (CP arb case)', async () => {
    const { fireArb } = await import('../src/executor/atomic.js');
    const wallet = {
      botId: 'bot1', ethAddress: '0x' + '0'.repeat(40),
      canSign: false, signatureType: 0,
    };
    // CP arb: 2 legs at $21 + $25 stake. Face = $50.
    // Old behavior: simPnl = 1 - 46 = -45 (loss!)
    // Fixed behavior: simPnl = 50 - 46 = +4 (correct gross)
    const result = await fireArb(
      {
        arbId: 'cp-1',
        dealTitle: 'EPL Test',
        structure: 'cross_platform' as const,
        entries: [
          { platform: 'polymarket' as const, side: 'BUY' as const,
            expectedPrice: 0.42, expectedSizeUsdc: 21,
            tokenId: 'TID_POLY' },
          { platform: 'sx_bet' as const, side: 'BUY' as const,
            expectedPrice: 0.50, expectedSizeUsdc: 25,
            marketHash: '0xMH', outcome: 1 },
        ],
        dryRun: true,
        expectedPayout: 50,
      },
      [wallet, { ...wallet, botId: 'bot2' }],
    );
    expect(result.expectedPayout).toBe(50);
    expect(result.expectedCost).toBe(46);
    expect(result.simPnl).toBe(50 - 46);  // = +4, positive
  });

  it('falls back to 1.0 when expectedPayout absent (backward compat)', async () => {
    const { fireArb } = await import('../src/executor/atomic.js');
    const wallet = {
      botId: 'bot1', ethAddress: '0x' + '0'.repeat(40),
      canSign: false, signatureType: 0,
    };
    const result = await fireArb(
      {
        arbId: 'allyes-1',
        dealTitle: 'Per-Platform ALL_YES',
        structure: 'all_yes' as const,
        entries: [
          { platform: 'polymarket' as const, side: 'BUY' as const,
            expectedPrice: 0.30, expectedSizeUsdc: 0.30,
            tokenId: 'T1' },
          { platform: 'polymarket' as const, side: 'BUY' as const,
            expectedPrice: 0.65, expectedSizeUsdc: 0.65,
            tokenId: 'T2' },
        ],
        dryRun: true,
        // no expectedPayout — fallback to 1.0
      },
      [wallet, { ...wallet, botId: 'bot2' }],
    );
    expect(result.expectedPayout).toBe(1.0);
    // simPnl = 1.0 - 0.95 = +0.05
    expect(result.simPnl).toBeCloseTo(0.05, 5);
  });

  it('uses N-1 for ALL_NO per-platform (radar passes it explicitly)', async () => {
    const { fireArb } = await import('../src/executor/atomic.js');
    const wallet = {
      botId: 'bot1', ethAddress: '0x' + '0'.repeat(40),
      canSign: false, signatureType: 0,
    };
    // 3-outcome ALL_NO: payout when one outcome wins, two pay $1 = $2
    const result = await fireArb(
      {
        arbId: 'allno-1', dealTitle: 'ALL_NO', structure: 'all_no' as const,
        entries: [
          { platform: 'polymarket' as const, side: 'BUY' as const,
            expectedPrice: 0.60, expectedSizeUsdc: 0.60, tokenId: 'T1_NO' },
          { platform: 'polymarket' as const, side: 'BUY' as const,
            expectedPrice: 0.60, expectedSizeUsdc: 0.60, tokenId: 'T2_NO' },
          { platform: 'polymarket' as const, side: 'BUY' as const,
            expectedPrice: 0.60, expectedSizeUsdc: 0.60, tokenId: 'T3_NO' },
        ],
        dryRun: true,
        expectedPayout: 2,
      },
      [wallet, { ...wallet, botId: 'bot2' }, { ...wallet, botId: 'bot3' }],
    );
    expect(result.expectedPayout).toBe(2);
    // simPnl = 2.0 - 1.80 = +0.20
    expect(result.simPnl).toBeCloseTo(0.20, 5);
  });
});
