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
import { loadWalletsFromEnv, synthesizeMockWallets } from './wallets/pool.js';
import { registeredCount as registeredSignerCount } from './wallets/signers.js';
import { registry as fillRegistry } from './executor/fills.js';
import { PolyUserWS } from './ws/poly_user_ws.js';
import { LimitlessUserWS } from './ws/limitless_user_ws.js';
import { getDiagnosticState as getProxyDiagnosticState } from './lib/proxy_pool.js';
import {
  setSockets,
  getAllPolySockets,
  getAllLimitlessSockets,
  stopAll as stopAllSockets,
} from './ws/ws_manager.js';
import type { Wallet } from './types/wallet.js';

const PORT = Number(process.env.EXECUTOR_PORT ?? '5051');
const HOST = process.env.EXECUTOR_HOST ?? '0.0.0.0';

let _wallets: Wallet[] = [];
// Phase TS-5c.3 (11.05.2026) — singleton WS list moved to ws/ws_manager.ts
// to break the circular import (server → atomic → server) that would
// happen now that atomic.ts pre-subscribes markets via getPolyUserWS.

// Phase audit (11.05.2026) — SZ-1. Lightweight /fire counter so operator
// can grep `curl /metrics` and see whether TS executor is actually
// receiving fires + the success/error breakdown. Before this, the only
// signal was "fillRegistry.pending changed", which was indirect.
//
// Phase audit-2 (11.05.2026): added per-reason breakdown for `error` and
// `aborted` buckets. The bare counter said "error: 7" with no clue WHY —
// operator had to docker-exec the container and grep logs. Now /metrics
// returns an `error_reasons` map (categorized by Error class + first 80
// chars of message) and `aborted_reasons` map (categorized by the
// aborted_reason prefix before ':'). Last error/aborted text are kept
// for inspection so operator can copy-paste into bug reports without
// digging through log streams.
const _fireCounters = {
  total: 0,
  by_outcome: {
    success: 0,
    error: 0,
    killed: 0,
    no_wallets: 0,
    malformed: 0,
    aborted: 0,
  } as Record<string, number>,
  last_fire_ts: null as number | null,
  last_outcome: null as string | null,
  error_reasons: {} as Record<string, number>,
  aborted_reasons: {} as Record<string, number>,
  last_error_message: null as string | null,
  last_aborted_reason: null as string | null,
};

// Cap how much detail we keep — error messages from external libs can be
// kilobytes (full HTTP body), and operator wants a short categorization,
// not a stack trace dump.
const MAX_REASON_LEN = 80;
const MAX_BUCKETS = 50;

function _categorizeError(err: unknown): string {
  if (err instanceof Error) {
    const msg = (err.message || '').trim();
    // Take the first chunk up to the first colon (errors tend to be
    // "category: details") or first 80 chars whichever is shorter.
    // This collapses "TypeError: foo[bar] undefined at line N" and
    // "TypeError: foo[baz] undefined at line M" into one bucket.
    const colon = msg.indexOf(':');
    const head = colon > 0 ? msg.slice(0, colon) : msg.slice(0, MAX_REASON_LEN);
    return `${err.constructor.name}: ${head}`.slice(0, MAX_REASON_LEN);
  }
  return String(err).slice(0, MAX_REASON_LEN);
}

function _categorizeAborted(reason: string): string {
  // aborted_reason convention: "<category>: <details>" e.g.
  //   "min_net_guard: net=$0.40 < $0.50"
  //   "graduation_gate: not yet passed — ..."
  //   "preflight_failed: leg 0 fresh depth $..."
  // Collapse by prefix before ':' so the bucket count is bounded.
  const colon = reason.indexOf(':');
  return (colon > 0 ? reason.slice(0, colon) : reason).slice(0, MAX_REASON_LEN);
}

function _bumpBounded(map: Record<string, number>, key: string): void {
  if (map[key] === undefined && Object.keys(map).length >= MAX_BUCKETS) {
    // Hard cap so a flood of unique error texts can't bloat /metrics.
    map['__overflow__'] = (map['__overflow__'] ?? 0) + 1;
    return;
  }
  map[key] = (map[key] ?? 0) + 1;
}

function _trackFire(
  outcome: 'success' | 'error' | 'killed' | 'no_wallets' | 'malformed' | 'aborted',
  detail?: { error?: unknown; abortedReason?: string },
): void {
  _fireCounters.total += 1;
  _fireCounters.by_outcome[outcome] = (_fireCounters.by_outcome[outcome] ?? 0) + 1;
  _fireCounters.last_fire_ts = Date.now();
  _fireCounters.last_outcome = outcome;
  if (outcome === 'error' && detail?.error !== undefined) {
    const cat = _categorizeError(detail.error);
    _bumpBounded(_fireCounters.error_reasons, cat);
    _fireCounters.last_error_message =
      detail.error instanceof Error
        ? (detail.error.message ?? '').slice(0, 240)
        : String(detail.error).slice(0, 240);
  }
  if (outcome === 'aborted' && detail?.abortedReason) {
    const cat = _categorizeAborted(detail.abortedReason);
    _bumpBounded(_fireCounters.aborted_reasons, cat);
    _fireCounters.last_aborted_reason = detail.abortedReason.slice(0, 240);
  }
}

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
    // Phase TS-5d — count of botIds with registered private keys.
    // This is the COUNT only, never the keys themselves (signers module
    // hides them in a module-scoped Map).
    signers_registered: registeredSignerCount(),
    // Phase TS-5b1.5 — operator can see at a glance whether mock wallets
    // are in use (means real wallets aren't configured).
    using_mock_wallets:
      _wallets.length > 0 && _wallets.every((w) => !w.canSign),
    dry_run: (process.env.DRY_RUN ?? '1') !== '0',
    poly_user_ws: getAllPolySockets().map((ws) => ws.getMetrics()),
    limitless_user_ws: getAllLimitlessSockets().map((ws) => ws.getMetrics()),
    // Phase audit (11.05.2026) — SZ-1: per-outcome /fire counters so the
    // operator can see at a glance whether the TS executor is firing,
    // and what the success/error mix looks like without scraping logs.
    // Phase audit-2: error_reasons + aborted_reasons + last messages so
    // operator can diagnose WHY fires error without docker-exec'ing.
    fires: {
      total: _fireCounters.total,
      by_outcome: { ..._fireCounters.by_outcome },
      last_fire_ts: _fireCounters.last_fire_ts,
      last_outcome: _fireCounters.last_outcome,
      error_reasons: { ..._fireCounters.error_reasons },
      aborted_reasons: { ..._fireCounters.aborted_reasons },
      last_error_message: _fireCounters.last_error_message,
      last_aborted_reason: _fireCounters.last_aborted_reason,
    },
    // Phase TS-5d.2 (14.05.2026) — residential proxy state for the
    // dashboard panel. Surfaces whether the proxy is configured, the
    // keepalive ticker is running, and which (platform, botId) agents
    // are currently active. Credentials are redacted —
    // only host:port shown.
    //
    // NOTE: residential proxy is intended for ORDER PLACEMENT in
    // real-deposit mode. In dry-run no actual POST /order goes
    // through the proxy (only the keepalive pings, when enabled).
    proxy: getProxyDiagnosticState(),
  }));

  // ── /fire ────────────────────────────────────────────────────────
  app.post<{ Body: FireRequest }>('/fire', async (req, reply) => {
    const body = req.body;
    if (!body || !body.arbId || !Array.isArray(body.entries) || body.entries.length === 0) {
      _trackFire('malformed');
      return reply.code(400).send({ error: 'malformed FireRequest' });
    }
    if (isKilled()) {
      _trackFire('killed');
    }
    // Phase TS-5b1.5 — in dry-run we synthesize mock wallets at startup so
    // the pool is never empty. The 503 below now only triggers in real
    // mode (DRY_RUN=0) when no real wallets were configured. This prevents
    // dry-run /fire from silently returning 503 → radar fallback to Python
    // → TS executor never exercised in production.
    const isRealMode = (process.env.DRY_RUN ?? '1') === '0';
    if (_wallets.length === 0 && isRealMode) {
      _trackFire('no_wallets');
      return reply.code(503).send({
        error:
          'no real wallets loaded — set BOT*_ETH_ADDRESS in Credentials.env (real mode requires real wallets)',
      });
    }
    try {
      const result = await fireArb(body, _wallets, body.dryRun);
      // Phase audit-2: separate `aborted` bucket from `success`. fireArb
      // returns ArbFireResult with abortedReason set when min_net guard /
      // preflight / graduation_gate / wallet_assignment short-circuits.
      // Previously those counted as `success` even though no leg fired —
      // which is exactly what operator was confused about ("fires=success
      // but paper_stats win_rate=0%").
      if (result.abortedReason) {
        _trackFire('aborted', { abortedReason: result.abortedReason });
      } else {
        _trackFire('success');
      }
      return result;
    } catch (err) {
      _trackFire('error', { error: err });
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
    stopAllSockets();
  });

  return app;
}

export async function startServer() {
  _wallets = loadWalletsFromEnv();

  // Phase TS-5b1.5 (11.05.2026) — DRY_RUN mock-wallet synthesis.
  // Without this gate, an empty Credentials.env (no BOT*_ETH_ADDRESS)
  // means assignLegs() throws in atomic.ts → /fire returns 503 →
  // the radar's TS-3 dispatcher silently falls back to in-process
  // Python → TS executor container runs but is never exercised. By
  // synthesizing 6 mock wallets in dry-run we let the TS pipeline
  // run end-to-end on paper trades and surface real bugs.
  //
  // Mock wallets have canSign=false hardcoded — even an accidental
  // real-mode call cannot sign. We also explicitly DON'T synthesize
  // when DRY_RUN=0, so prod cannot accidentally fire with fake addrs.
  const isRealMode = (process.env.DRY_RUN ?? '1') === '0';
  if (_wallets.length === 0 && !isRealMode) {
    _wallets = synthesizeMockWallets();
    console.warn(
      `[startup] DRY_RUN=1 + no BOT*_ETH_ADDRESS configured — synthesized ${_wallets.length} mock wallets ` +
        `(canSign=false). TS executor will exercise the dry-run path end-to-end. ` +
        `Set BOT*_ETH_ADDRESS in Credentials.env to use real wallet pool.`,
    );
  } else if (_wallets.length === 0 && isRealMode) {
    console.error(
      '[startup] DRY_RUN=0 + no BOT*_ETH_ADDRESS — real fires will be 503-rejected. ' +
        'This is a safety guard: real-mode REQUIRES real wallet addresses.',
    );
  }

  // Phase TS-5b1/b2 — one WS client per wallet per platform. No-ops when
  // creds missing. Phase TS-5c.3 registers them via ws_manager so
  // atomic.ts can look them up without circular imports.
  const polySockets = _wallets.map(
    (w) => new PolyUserWS({ wallet: w, verbose: process.env.LOG_LEVEL === 'debug' }),
  );
  const limitlessSockets = _wallets.map(
    (w) =>
      new LimitlessUserWS({ wallet: w, verbose: process.env.LOG_LEVEL === 'debug' }),
  );
  setSockets(polySockets, limitlessSockets);
  for (const ws of polySockets) ws.start();
  for (const ws of limitlessSockets) ws.start();

  const app = buildServer();
  await app.listen({ host: HOST, port: PORT });
  app.log.info(
    {
      host: HOST,
      port: PORT,
      wallets: _wallets.length,
      polyUserSockets: polySockets.length,
      polyUserSocketsWithCreds: _wallets.filter(
        (w) => !!(w.polyApiKey && w.polySecret && w.polyPassphrase),
      ).length,
      limitlessUserSockets: limitlessSockets.length,
      limitlessUserSocketsWithCreds: _wallets.filter((w) => !!w.limitlessApiKey).length,
      dryRun: (process.env.DRY_RUN ?? '1') !== '0',
    },
    'executor-ts ready',
  );
  return app;
}

// Phase TS-5c.3 (11.05.2026) — getPolyUserWS / getLimitlessUserWS moved
// to ws/ws_manager.ts to break the server↔atomic circular dep. Import
// from there directly:
//   import { getPolyUserWS } from './ws/ws_manager.js';

// Entry point — only run if invoked directly (not when imported in tests).
if (import.meta.url === `file://${process.argv[1]}`) {
  startServer().catch((err) => {
    console.error('startup failure:', err);
    process.exit(1);
  });
}
