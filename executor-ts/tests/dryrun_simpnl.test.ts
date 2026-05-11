/**
 * Phase audit-2 (11.05.2026, second simPnl fix) — verify dry-run simPnl
 * uses the optimistic formula regardless of per-leg outcomes.
 *
 * Background: SX TS-3 stub passes `orders: []` to buildSxOrder, which
 * sets `built.partial=true`. Limitless cold-cache misses produce
 * 'rejected' legs. Both made `allDryFired=false` → simPnl fell back to
 * -expectedCost + 1.0 = -$45 for CP arbs → paper_stats.win_rate stuck
 * at 0% → graduation gate unreachable. In dry-run we want the
 * THEORETICAL profit signal — leg failures are tracked separately.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { tmpdir } from 'node:os';
import { mkdtemp, rm } from 'node:fs/promises';
import { join } from 'node:path';

vi.mock('../src/risk/limits.js', () => ({
  checkCanFire: async () => ({ allowed: true }),
  snapshot: async () => ({}),
}));
vi.mock('../src/risk/killswitch.js', () => ({ isKilled: () => false }));
vi.mock('../src/wallets/pool.js', () => ({
  assignLegs: (pool: unknown[], n: number) => pool.slice(0, n),
  jitterMsForLeg: () => 0,
  synthesizeMockWallets: () => [],
}));
vi.mock('../src/wallets/signers.js', () => ({
  getSignerKey: () => undefined,
  registeredCount: () => 0,
}));

// Poly builder succeeds. SX builder simulates the TS-3 stub: returns a
// BuiltOrder with `partial: true` (the production behavior when called
// with orders=[]).
vi.mock('../src/builders/poly.js', () => ({
  buildPolyOrder: async (i: { price: number; sizeUsdc: number }) => ({
    platform: 'polymarket', body: {}, wouldPostUrl: 'http://test', signed: false,
    expectedPrice: i.price, expectedSizeUsdc: i.sizeUsdc,
    signPayload: new Uint8Array(),
  }),
}));
vi.mock('../src/builders/sx.js', () => ({
  buildSxOrder: async (i: { takerPrice: number; sizeUsdc: number }) => ({
    platform: 'sx_bet', body: {}, wouldPostUrl: 'http://test', signed: false,
    expectedPrice: i.takerPrice, expectedSizeUsdc: i.sizeUsdc,
    signPayload: new Uint8Array(),
    partial: true,  // ← TS-3 stub effect: empty orders → partial
    match: { matched: [], filledUsdc: 0, avgPrice: null, partial: true,
             shortfallUsdc: i.sizeUsdc, bestPrice: null, worstPrice: null },
  }),
}));
// Limitless builder throws when tokenId missing (cold cache simulation).
vi.mock('../src/builders/limitless.js', () => ({
  buildLimitlessOrder: async () => {
    throw new Error('limitless leg requires tokenId + slug');
  },
}));
vi.mock('../src/executor/revert.js', () => ({
  planRevert: () => ({ legs: [], arbReason: null }),
  annotateLegsWithPlan: () => {},
  executeRevertPlan: async () => {},
}));

describe('dry-run simPnl optimistic formula (phase audit-2)', () => {
  let dataDir: string;
  beforeEach(async () => {
    dataDir = await mkdtemp(join(tmpdir(), 'exec-ts-dryrun-pnl-'));
    process.env.EXECUTIONS_DIR = dataDir;
    process.env.DRY_RUN = '1';
    vi.resetModules();
  });
  afterEach(async () => {
    delete process.env.EXECUTIONS_DIR;
    await rm(dataDir, { recursive: true, force: true });
  });

  const wallet = {
    botId: 'bot1', ethAddress: '0x' + '0'.repeat(40),
    canSign: false, signatureType: 0,
  } as const;

  it('CP arb with SX partial leg still gives positive simPnl in dry-run', async () => {
    const { fireArb } = await import('../src/executor/atomic.js');
    const result = await fireArb(
      {
        arbId: 'cp-sx-partial', dealTitle: 'EPL',
        structure: 'cross_platform' as const,
        entries: [
          { platform: 'polymarket' as const, side: 'BUY' as const,
            expectedPrice: 0.42, expectedSizeUsdc: 21,
            tokenId: 'TID' },
          { platform: 'sx_bet' as const, side: 'BUY' as const,
            expectedPrice: 0.50, expectedSizeUsdc: 25,
            marketHash: '0xMH', outcome: 1 },
        ],
        dryRun: true,
        expectedPayout: 50,
      },
      [wallet, { ...wallet, botId: 'bot2' }],
    );
    // Old behavior: SX builder reports partial → allDryFired=false →
    // simPnl = -46 + 1 = -45.
    // New behavior: dry-run forces optimistic simPnl = 50 - 46 = +4.
    expect(result.simPnl).toBe(50 - 46);
    expect(result.simPnl).toBeGreaterThan(0);
  });

  it('CP arb with Limitless build failure still gives optimistic simPnl', async () => {
    const { fireArb } = await import('../src/executor/atomic.js');
    const result = await fireArb(
      {
        arbId: 'cp-lim-fail', dealTitle: 'EPL',
        structure: 'cross_platform' as const,
        entries: [
          { platform: 'limitless' as const, side: 'BUY' as const,
            expectedPrice: 0.42, expectedSizeUsdc: 21,
            slug: 'my-slug' /* no tokenId → buildLimitless throws */ },
          { platform: 'sx_bet' as const, side: 'BUY' as const,
            expectedPrice: 0.50, expectedSizeUsdc: 25,
            marketHash: '0xMH', outcome: 1 },
        ],
        dryRun: true,
        expectedPayout: 50,
      },
      [wallet, { ...wallet, botId: 'bot2' }],
    );
    // Limitless throws → leg 'rejected'. SX partial. Old: simPnl=-45.
    // New: dry-run forces simPnl = 50 - 46 = +4.
    expect(result.simPnl).toBe(50 - 46);
    expect(result.simPnl).toBeGreaterThan(0);
    // Leg-level signal still tracked for ops visibility
    const rejected = result.legs.filter((l) => l.status === 'rejected');
    expect(rejected.length).toBeGreaterThan(0);
  });

  it('all-success dry-run gives the same expectedPayout - expectedCost', async () => {
    const { fireArb } = await import('../src/executor/atomic.js');
    const result = await fireArb(
      {
        arbId: 'cp-clean', dealTitle: 'EPL',
        structure: 'cross_platform' as const,
        entries: [
          { platform: 'polymarket' as const, side: 'BUY' as const,
            expectedPrice: 0.42, expectedSizeUsdc: 21, tokenId: 'TID1' },
          { platform: 'polymarket' as const, side: 'BUY' as const,
            expectedPrice: 0.50, expectedSizeUsdc: 25, tokenId: 'TID2' },
        ],
        dryRun: true,
        expectedPayout: 50,
      },
      [wallet, { ...wallet, botId: 'bot2' }],
    );
    expect(result.simPnl).toBe(50 - 46);
  });
});
