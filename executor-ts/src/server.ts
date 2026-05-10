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
import Fastify from 'fastify';
import type { FireRequest } from './types/deal.js';
import { fireArb } from './executor/atomic.js';
import { snapshot as riskSnapshot } from './risk/limits.js';
import { isKilled, kill, unkill, status as killStatus } from './risk/killswitch.js';
import { loadWalletsFromEnv } from './wallets/pool.js';
import { registry as fillRegistry } from './executor/fills.js';
import { PolyUserWS } from './ws/poly_user_ws.js';
import { LimitlessUserWS } from './ws/limitless_user_ws.js';
import type { Wallet } from './types/wallet.js';

const PORT = Number(process.env.EXECUTOR_PORT ?? '5051');
const HOST = process.env.EXECUTOR_HOST ?? '0.0.0.0';

let _wallets: Wallet[] = [];
// Phase TS-5b1 (10.05.2026) — one Polymarket user-channel WS per wallet
// that has L2 creds. Instances without creds are skipped (PolyUserWS.start
// is a no-op). updateMarkets() is called by atomic.ts before firing each
// poly leg so we pre-subscribe and the trade event arrives in <250ms
// instead of waiting on the 5s dead-man.
let _polyUserSockets: PolyUserWS[] = [];
// Phase TS-5b2 (10.05.2026) — one Limitless Socket.IO user-channel WS per
// wallet that has limitlessApiKey. Subscribes to `orderEvent` and bridges
// fills into the same fillRegistry. Without an API key the instance is a
// no-op (start() returns immediately) — radar still works in dry-run.
let _limitlessUserSockets: LimitlessUserWS[] = [];

// v36-fix (09.05.2026): no explicit return-type annotation — Fastify
// infers a complex generic that doesn't match the plain `FastifyInstance`
// alias when logger transport is conditionally set. Let TS infer.
export function buildServer() {
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
    poly_user_ws: _polyUserSockets.map((ws) => ws.getMetrics()),
    limitless_user_ws: _limitlessUserSockets.map((ws) => ws.getMetrics()),
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
  app.addHook('onClose', async () => {
    clearInterval(janitor);
    for (const ws of _polyUserSockets) ws.stop();
    for (const ws of _limitlessUserSockets) ws.stop();
  });

  return app;
}

export async function startServer() {
  _wallets = loadWalletsFromEnv();
  // Phase TS-5b1 — one PolyUserWS per wallet. No-op without poly L2 creds.
  _polyUserSockets = _wallets.map(
    (w) => new PolyUserWS({ wallet: w, verbose: process.env.LOG_LEVEL === 'debug' }),
  );
  for (const ws of _polyUserSockets) ws.start();
  // Phase TS-5b2 — one LimitlessUserWS per wallet. No-op without limitlessApiKey.
  _limitlessUserSockets = _wallets.map(
    (w) =>
      new LimitlessUserWS({ wallet: w, verbose: process.env.LOG_LEVEL === 'debug' }),
  );
  for (const ws of _limitlessUserSockets) ws.start();

  const app = buildServer();
  await app.listen({ host: HOST, port: PORT });
  app.log.info(
    {
      host: HOST,
      port: PORT,
      wallets: _wallets.length,
      polyUserSockets: _polyUserSockets.length,
      polyUserSocketsWithCreds: _wallets.filter(
        (w) => !!(w.polyApiKey && w.polySecret && w.polyPassphrase),
      ).length,
      limitlessUserSockets: _limitlessUserSockets.length,
      limitlessUserSocketsWithCreds: _wallets.filter((w) => !!w.limitlessApiKey).length,
      dryRun: (process.env.DRY_RUN ?? '1') !== '0',
    },
    'executor-ts ready',
  );
  return app;
}

/** Lookup PolyUserWS by botId. Used by atomic.ts to pre-subscribe before fire. */
export function getPolyUserWS(botId: string): PolyUserWS | undefined {
  return _polyUserSockets.find((ws) => ws.getMetrics().botId === botId);
}

/** Lookup LimitlessUserWS by botId. Symmetry with getPolyUserWS. */
export function getLimitlessUserWS(botId: string): LimitlessUserWS | undefined {
  return _limitlessUserSockets.find((ws) => ws.getMetrics().botId === botId);
}

// Entry point — only run if invoked directly (not when imported in tests).
if (import.meta.url === `file://${process.argv[1]}`) {
  startServer().catch((err) => {
    console.error('startup failure:', err);
    process.exit(1);
  });
}
