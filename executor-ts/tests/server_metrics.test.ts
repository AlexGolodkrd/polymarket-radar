/**
 * server.ts — /metrics fire-counter breakdown (phase audit-2, 11.05.2026).
 *
 * Operator's pain: production showed `fires.by_outcome.error: 7` with
 * zero context — no log access without docker-exec. Verifies the new
 * error_reasons / aborted_reasons maps populate correctly, that the
 * `aborted` bucket is separate from `success`, and that bounded growth
 * works (no map explosion under a flood of unique error strings).
 */
import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest';
import type { FastifyInstance } from 'fastify';

// We need to import server.ts and exercise its /metrics + /fire flow.
// Use vi.mock to stub the actual `fireArb` so we control the outcomes
// without standing up wallets / poly RPC / etc. The mock returns
// different shapes per test to exercise success / aborted / error paths.

const mockFireArb = vi.fn();
vi.mock('../src/executor/atomic.js', () => ({
  fireArb: (...args: unknown[]) => mockFireArb(...args),
}));
// Stub killswitch to always "not killed" so the kill path doesn't
// pre-empt our outcome buckets in these tests.
vi.mock('../src/risk/killswitch.js', () => ({
  isKilled: () => false,
  kill: async () => ({ killed: true }),
  unkill: () => true,
  status: () => ({ killed: false }),
}));
// Stub risk snapshot (not under test here).
vi.mock('../src/risk/limits.js', () => ({
  snapshot: async () => ({ canFire: true, dailyLoss: 0 }),
  checkCanFire: () => ({ canFire: true }),
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
// Stub WS manager so buildServer doesn't try to enumerate sockets.
vi.mock('../src/ws/ws_manager.js', () => ({
  setSockets: () => {},
  getAllPolySockets: () => [],
  getAllLimitlessSockets: () => [],
  stopAll: () => {},
}));
// Stub fills (referenced by /metrics).
vi.mock('../src/executor/fills.js', () => ({
  registry: {
    metrics: () => ({ pending: 0, byOrderId: 0, bySlug: 0 }),
    expireStale: () => {},
  },
}));

const SAMPLE_FIRE_REQ = {
  arbId: 'test-arb-1',
  dealTitle: 'Test',
  structure: 'cross_platform',
  entries: [{ platform: 'polymarket', side: 'BUY', expectedPrice: 0.45, expectedSizeUsdc: 10 }],
  dryRun: true,
};

describe('/metrics fire counters with error reason breakdown', () => {
  let app: FastifyInstance;
  beforeEach(async () => {
    mockFireArb.mockReset();
    vi.resetModules();
    const { buildServer } = await import('../src/server.js');
    app = buildServer() as unknown as FastifyInstance;
  });
  afterEach(async () => {
    await app.close();
  });

  it('counts success in by_outcome.success, no error/aborted reasons', async () => {
    mockFireArb.mockResolvedValue({
      arbId: 'a', abortedReason: null, legs: [], expectedCost: 10,
      expectedPayout: 11, simPnl: 1, legCount: 1, legStatusCounts: { 'dry-fired': 1 },
      partialLegCount: 0, worstPartialShortfallUsdc: 0, fireMode: 'taker',
      dryRun: true, firedAt: Date.now(), dealTitle: 'T', dealStructure: 'X',
    });
    const r = await app.inject({ method: 'POST', url: '/fire', payload: SAMPLE_FIRE_REQ });
    expect(r.statusCode).toBe(200);
    const m = await app.inject({ method: 'GET', url: '/metrics' });
    const body = m.json();
    expect(body.fires.by_outcome.success).toBe(1);
    expect(body.fires.by_outcome.error).toBe(0);
    expect(body.fires.by_outcome.aborted).toBe(0);
    expect(body.fires.error_reasons).toEqual({});
    expect(body.fires.aborted_reasons).toEqual({});
  });

  it('separates aborted (with abortedReason) from success', async () => {
    mockFireArb.mockResolvedValueOnce({
      arbId: 'a', abortedReason: 'min_net_guard: net=$0.40 < $0.50', legs: [],
      expectedCost: 10, expectedPayout: 10.4, simPnl: 0.4, legCount: 1,
      legStatusCounts: { aborted: 1 }, partialLegCount: 0,
      worstPartialShortfallUsdc: 0, fireMode: 'taker', dryRun: true,
      firedAt: Date.now(), dealTitle: 'T', dealStructure: 'X',
    });
    await app.inject({ method: 'POST', url: '/fire', payload: SAMPLE_FIRE_REQ });
    const m = await app.inject({ method: 'GET', url: '/metrics' });
    const body = m.json();
    expect(body.fires.by_outcome.success).toBe(0);
    expect(body.fires.by_outcome.aborted).toBe(1);
    // Aborted-reason category is the prefix before the first colon.
    expect(body.fires.aborted_reasons['min_net_guard']).toBe(1);
    expect(body.fires.last_aborted_reason).toContain('min_net_guard');
  });

  it('categorizes thrown errors into error_reasons by Error class + head', async () => {
    mockFireArb
      .mockRejectedValueOnce(new TypeError('polymarket leg requires tokenId'))
      .mockRejectedValueOnce(new TypeError('polymarket leg requires tokenId'))
      .mockRejectedValueOnce(new RangeError('size 0 below MIN_TICK'));
    await app.inject({ method: 'POST', url: '/fire', payload: SAMPLE_FIRE_REQ });
    await app.inject({ method: 'POST', url: '/fire', payload: SAMPLE_FIRE_REQ });
    await app.inject({ method: 'POST', url: '/fire', payload: SAMPLE_FIRE_REQ });
    const m = await app.inject({ method: 'GET', url: '/metrics' });
    const body = m.json();
    expect(body.fires.by_outcome.error).toBe(3);
    // Two TypeErrors with same message head → same bucket
    const reasons = body.fires.error_reasons;
    const keys = Object.keys(reasons);
    expect(keys.some((k) => k.includes('TypeError'))).toBe(true);
    expect(keys.some((k) => k.includes('RangeError'))).toBe(true);
    // Same-class same-head should aggregate to 2
    const typeErrCount = Object.entries(reasons)
      .filter(([k]) => k.startsWith('TypeError'))
      .reduce((acc, [, v]) => acc + (v as number), 0);
    expect(typeErrCount).toBe(2);
    expect(body.fires.last_error_message).toBeTruthy();
  });

  it('counts malformed request body separately, never invokes fireArb', async () => {
    const r = await app.inject({
      method: 'POST',
      url: '/fire',
      payload: { arbId: '', entries: [] },
    });
    expect(r.statusCode).toBe(400);
    expect(mockFireArb).not.toHaveBeenCalled();
    const m = await app.inject({ method: 'GET', url: '/metrics' });
    const body = m.json();
    expect(body.fires.by_outcome.malformed).toBe(1);
  });

  it('caps error_reasons map size with __overflow__ bucket', async () => {
    // Push 60 unique error messages — bucket cap is 50.
    for (let i = 0; i < 60; i++) {
      mockFireArb.mockRejectedValueOnce(new Error(`unique error number ${i}`));
      await app.inject({ method: 'POST', url: '/fire', payload: SAMPLE_FIRE_REQ });
    }
    const m = await app.inject({ method: 'GET', url: '/metrics' });
    const body = m.json();
    expect(body.fires.by_outcome.error).toBe(60);
    const keys = Object.keys(body.fires.error_reasons);
    // 50 unique buckets + 1 overflow bucket = 51 total
    expect(keys.length).toBeLessThanOrEqual(51);
    expect(keys).toContain('__overflow__');
    expect(body.fires.error_reasons['__overflow__']).toBeGreaterThan(0);
  });
});
