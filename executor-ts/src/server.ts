/**
 * Fastify HTTP server — receives `POST /fire` from the Python detector
 * and orchestrates the executor pipeline. Mirrors the cross-process
 * contract documented in `docs/TS_REWRITE_PLAN.md` §3.
 *
 * Endpoints:
 *   POST /fire           — fire one arb (FireRequest body)
 *   GET  /version        — running git commit (Phase 19v33 parity)
 *   GET  /risk_status    — same shape as Python /api/risk_status
 *   POST /kill           — set kill switch (operator manual or watchdog)
 *   POST /unkill         — clear kill flag
 *   GET  /healthz        — k8s/docker healthcheck
 *   GET  /metrics        — fill registry pending count, recent fires
 *
 * Phase TS-3 ships in DRY_RUN mode (no real exchange POSTs) — the
 * Python detector can switch onto this executor by setting
 * EXECUTOR_URL=http://executor-ts:5051. When DRY_RUN=0 lands in TS-5,
 * the same endpoint will perform real fires.
 */
import Fastify, { type FastifyInstance } from 'fastify';
import type { FireRequest } from './types/deal.js';
import { fireArb } from './executor/atomic.js';
import { snapshot as riskSnapshot } from './risk/limits.js';
import { isKilled, kill, unkill, status as killStatus } from './risk/killswitch.js';
import { loadWalletsFromEnv } from './wallets/pool.js';
import { registry as fillRegistry } from './executor/fills.js';
import type { Wallet } from './types/wallet.js';

const PORT = Number(process.env.EXECUTOR_PORT ?? '5051');
const HOST = process.env.EXECUTOR_HOST ?? '0.0.0.0';

let _wallets: Wallet[] = [];

export function buildServer(): FastifyInstance {
  const app = Fastify({
    logger: {
      level: process.env.LOG_LEVEL ?? 'info',
      transport: process.env.NODE_ENV !== 'production'
        ? { target: 'pino-pretty', options: { colorize: true } }
        : undefined,
    },
    bodyLimit: 256 * 1024,
  });

  // ── /version ─────────────────────────────────────────────────────
  app.get('/version', async () => ({
    commit: process.env.GIT_COMMIT ?? 'unknown',
    commit_short: (process.env.GIT_COMMIT ?? 'unknown').slice(0, 8),
    build_time: process.env.BUILD_TIME ?? 'unknown',
    phase: 'TS-3',
    runtime: 'node-typescript',
    dry_run: (process.env.DRY_RUN ?? '1') !== '0',
  }));

  // ── /healthz ─────────────────────────────────────────────────────
  app.get('/healthz', async (_, reply) => {
    if (isKilled()) {
      // Kill switch is "alive but refusing fires" — return 200 with a
      // `degraded` flag rather than 503 so Docker doesn't mark the
      // container unhealthy and restart it (which would clear state).
      return reply.send({ status: 'degraded', reason: 'kill switch active' });
    }
    return { status: 'ok', wallets_loaded: _wallets.length };
  });

  // ── /risk_status ────────────────────────────────────────────────
  app.get('/risk_status', async () => await riskSnapshot());

  // ── /kill ────────────────────────────────────────────────────────
  app.post<{ Body: { reason?: string; confirm?: number } }>('/kill', async (req, reply) => {
    const { reason, confirm } = req.body ?? {};
    if (confirm !== 1) {
      return reply.code(400).send({ error: 'confirm: 1 required (anti-misclick)' });
    }
    return await kill(reason ?? 'manual kill');
  });

  app.post('/unkill', async () => ({ unkilled: unkill(), status: killStatus() }));

  // ── /metrics ─────────────────────────────────────────────────────
  app.get('/metrics', async () => ({
    fills: fillRegistry.metrics(),
    wallets: _wallets.length,
    can_sign: _wallets.filter((w) => w.canSign).length,
  }));

  // ── /fire ────────────────────────────────────────────────────────
  app.post<{ Body: FireRequest }>('/fire', async (req, reply) => {
    const body = req.body;
    if (!body || !body.arbId || !Array.isArray(body.entries) || body.entries.length === 0) {
      return reply.code(400).send({ error: 'malformed FireRequest' });
    }
    if (_wallets.length === 0) {
      return reply.code(503).send({ error: 'no wallets loaded — set BOT*_ETH_ADDRESS env vars' });
    }
    try {
      const result = await fireArb(body, _wallets, body.dryRun);
      return result;
    } catch (err) {
      app.log.error({ err, arbId: body.arbId }, 'fireArb failed');
      return reply.code(500).send({
        error: 'fireArb failed',
        message: err instanceof Error ? err.message : String(err),
        arbId: body.arbId,
      });
    }
  });

  // Fill registry janitor — every 10s purge stale registrations.
  const janitor = setInterval(() => fillRegistry.expireStale(), 10_000);
  app.addHook('onClose', async () => clearInterval(janitor));

  return app;
}

export async function startServer(): Promise<FastifyInstance> {
  _wallets = loadWalletsFromEnv();
  const app = buildServer();
  await app.listen({ host: HOST, port: PORT });
  app.log.info(
    { host: HOST, port: PORT, wallets: _wallets.length, dryRun: (process.env.DRY_RUN ?? '1') !== '0' },
    'executor-ts ready',
  );
  return app;
}

// Entry point — only run if invoked directly (not when imported in tests).
if (import.meta.url === `file://${process.argv[1]}`) {
  startServer().catch((err) => {
    console.error('startup failure:', err);
    process.exit(1);
  });
}
