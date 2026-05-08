/**
 * paper.ts — dryrun.jsonl + paper_results.jsonl writers.
 * Verifies the on-disk schema matches Python's so the analytics
 * aggregator can read either source uniformly during cutover.
 */
import { describe, expect, it, beforeEach, afterEach } from 'vitest';
import { tmpdir } from 'node:os';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { join } from 'node:path';

describe('paper logging', () => {
  let dataDir: string;
  beforeEach(async () => {
    dataDir = await mkdtemp(join(tmpdir(), 'exec-ts-paper-'));
    process.env.EXECUTIONS_DIR = dataDir;
  });
  afterEach(async () => {
    delete process.env.EXECUTIONS_DIR;
    await rm(dataDir, { recursive: true, force: true });
  });

  it('writes arb-level row matching Python schema', async () => {
    // Re-import after env is set so paths resolve to the tmpdir.
    const mod = await import(`../../src/executor/paper.js?t=${Date.now()}`);
    const result = {
      arbId: 'test-arb-1',
      dealTitle: 'Lakers vs Celtics',
      dealStructure: 'all_yes',
      expectedCost: 10,
      expectedPayout: 1,
      simPnl: -9,
      legCount: 2,
      legStatusCounts: { 'dry-fired': 2 } as Record<string, number>,
      partialLegCount: 0,
      worstPartialShortfallUsdc: 0,
      abortedReason: null,
      fireMode: 'taker' as const,
      dryRun: true,
      firedAt: 1.234,
      legs: [],
    };
    await mod.logArbDecision(result);
    const path = join(dataDir, 'dryrun.jsonl');
    const lines = (await readFile(path, 'utf-8')).trim().split('\n');
    expect(lines).toHaveLength(1);
    const row = JSON.parse(lines[0]!);
    expect(row.kind).toBe('arb');
    expect(row.arb_id).toBe('test-arb-1');
    expect(row.title).toBe('Lakers vs Celtics');
    expect(row.structure).toBe('all_yes');
    expect(row.dry_run).toBe(true);
    expect(row.leg_count).toBe(2);
    expect(row.leg_status_counts).toEqual({ 'dry-fired': 2 });
  });
});
