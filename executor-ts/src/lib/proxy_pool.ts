/**
 * Residential proxy routing for order-placement HTTP requests.
 *
 * Phase TS-5d (14.05.2026) — required before flipping DRY_RUN=0.
 * Polymarket geoblocks/penalizes datacenter IPs (Cloudflare risk-score
 * on POST /order); Limitless rate-limits per-IP aggressively
 * (BUG_CATALOG 6.1 / the #179-#182 saga). The radar's VPS
 * 77.91.97.22 is exactly the kind of IP both penalize.
 *
 * Operator already derives L2 creds locally through a residential
 * connection (one-time per bot). This module makes the runtime
 * order-placement path do the same: every POST goes through a
 * residential ProxyAgent with a session sticky per bot wallet.
 *
 * Public contract:
 *
 *   getDispatcher(platform, botId)
 *     → ProxyAgent if PROXY_URL_* env present
 *     → undefined if no proxy configured (transparent fallback to
 *       direct fetch — preserves current behavior, zero-risk default)
 *
 * Failure mode:
 *
 *   When proxy connect fails, the caller's fetch will throw a
 *   network error. By default we do NOT silently fall through to
 *   direct IP — that would mask geoblock and produce wrong-IP
 *   signed-order mismatches. Operator must explicitly set
 *   PROXY_FALLBACK_TO_DIRECT=1 to allow that (testing only).
 *
 * See .claude/skills/residential-proxy-routing/SKILL.md for the
 * full design + env contract.
 */
import { Agent, Dispatcher, ProxyAgent } from 'undici';
// Phase TS-5g (14.05.2026) — SOCKS5 support. Operator's residential
// provider is pool.proxy.market with ports 10000-10999 (each port a
// different sticky exit IP). undici's native ProxyAgent only supports
// HTTP CONNECT; for SOCKS5 we use the `socks` package (low-level lib
// underneath socks-proxy-agent) and wire the resulting raw socket into
// an undici Agent via the `connect` factory.
//
// Phase audit-3 (15.05.2026) — was using `socksAgent.callback(...)`
// which is a Node-style API removed from socks-proxy-agent v8. Result:
// every POST through proxy threw TypeError → undici wrapped as
// "fetch failed". Now we call SocksClient.createConnection directly,
// then layer tls.connect for HTTPS targets. Stable, version-pinned,
// observable.
import { SocksClient } from 'socks';
import * as tls from 'tls';
import type { Socket } from 'net';

export type ProxyPlatform = 'polymarket' | 'limitless' | 'sx';

/** Sentinel meaning "explicit direct, no proxy" (skips default URL). */
const NONE_SENTINEL = 'NONE';

/** Pattern used to template session-stickiness into the proxy
 *  username. Most residential providers accept `user-session-X`
 *  to bind a session to a sticky exit IP. Override via env. */
const DEFAULT_STICKY_PATTERN = '{platform}-{bot}';

const STICKY_PATTERN =
  process.env['PROXY_STICKY_SESSION_PATTERN'] || DEFAULT_STICKY_PATTERN;

/** Cache: `${platform}:${botId}` → Dispatcher (ProxyAgent or Agent
 *  wrapping a SocksProxyAgent). Reused across requests so the
 *  underlying connection pool persists (sticky-session friendly,
 *  also amortizes TLS handshake cost). */
const _agents = new Map<string, Dispatcher>();

/** Phase TS-5d.1 (14.05.2026) — keepalive ticker. Residential proxies
 *  typically rotate the sticky exit IP if a session sits idle for
 *  30-60 seconds. After IP rotation, Polymarket would see a new IP
 *  for the bot's next order POST and flag it (sticky binding to L2
 *  creds is broken). We pre-empt this by pinging through every
 *  active (platform, botId) agent every `PROXY_KEEPALIVE_INTERVAL_S`
 *  seconds. Default 30s — well below the typical 60s rotation
 *  threshold of major residential providers (Bright Data, Smartproxy,
 *  Oxylabs). Set to 0 to disable (e.g. during unit tests). */
let _keepaliveTimer: NodeJS.Timeout | null = null;

/** Per-platform ping URLs used by the keepalive ticker. These hit a
 *  cheap unauthenticated endpoint on each platform — the only goal is
 *  to keep traffic flowing through the proxy session, so the provider
 *  doesn't rotate the exit IP. We don't care about the response body. */
const KEEPALIVE_URLS: Record<ProxyPlatform, string> = {
  polymarket: 'https://gamma-api.polymarket.com/sports',
  limitless: 'https://api.limitless.exchange/markets/active?page=1&limit=1',
  sx: 'https://api.sx.bet/leagues',
};

function getKeepaliveIntervalMs(): number {
  const raw = process.env['PROXY_KEEPALIVE_INTERVAL_S'];
  if (raw === undefined) return 30_000; // default 30s
  const n = Number.parseInt(raw, 10);
  if (Number.isNaN(n) || n <= 0) return 0; // disabled
  return n * 1000;
}

async function pingAgent(platform: ProxyPlatform, agent: ProxyAgent): Promise<void> {
  const url = KEEPALIVE_URLS[platform];
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), 5_000);
  try {
    // HEAD is ideal but some platforms 405 it; use GET with tiny payload.
    // Failures are logged but don't crash — next tick retries.
    const fetchOpts: Parameters<typeof fetch>[1] & { dispatcher?: unknown } = {
      method: 'GET',
      signal: ac.signal,
      dispatcher: agent,
    };
    await fetch(url, fetchOpts);
  } catch (e) {
    // Don't escalate — keepalive failures are operator-observable via
    // /api/ts_metrics but shouldn't kill the ticker. If the proxy is
    // truly broken, the next ORDER POST will surface it as HttpError.
    const reason = (e as Error)?.message || 'unknown';
    if (!reason.includes('AbortError')) {
      // Aborts from our own 5s ceiling are expected when proxy slow;
      // log other failures so operator can see flapping.
      // eslint-disable-next-line no-console
      console.warn(`[proxy_pool] keepalive ${platform} failed: ${reason}`);
    }
  } finally {
    clearTimeout(timer);
  }
}

function tickKeepalive(): void {
  // Fire one ping per (platform, botId) in parallel — undici agents
  // don't share connection pools, so each needs its own poke.
  for (const [key, agent] of _agents.entries()) {
    const [platform] = key.split(':') as [ProxyPlatform];
    void pingAgent(platform, agent);
  }
}

function ensureKeepaliveStarted(): void {
  if (_keepaliveTimer !== null) return;
  const intervalMs = getKeepaliveIntervalMs();
  if (intervalMs <= 0) return; // disabled via env
  _keepaliveTimer = setInterval(tickKeepalive, intervalMs);
  // Don't let the ticker block process shutdown.
  if (typeof (_keepaliveTimer as NodeJS.Timeout).unref === 'function') {
    _keepaliveTimer.unref();
  }
}

function stopKeepalive(): void {
  if (_keepaliveTimer !== null) {
    clearInterval(_keepaliveTimer);
    _keepaliveTimer = null;
  }
}

/** Resolve the env URL for a platform, with `_DEFAULT` fallback.
 *  Returns null when no proxy should be used. */
function resolveProxyUrl(platform: ProxyPlatform): string | null {
  const platformKey = `PROXY_URL_${platform.toUpperCase()}`;
  const v = process.env[platformKey];
  if (v === NONE_SENTINEL) return null; // explicit direct
  if (v && v.length > 0) return v;
  const fallback = process.env['PROXY_URL_DEFAULT'];
  if (!fallback || fallback === NONE_SENTINEL) return null;
  return fallback;
}

/** Apply the sticky-session pattern into the proxy URL's username so
 *  the provider routes this (platform, bot) pair through a consistent
 *  exit IP. Pattern token `{platform}` and `{bot}` are replaced. If
 *  the URL has no userinfo, returns it unchanged. */
function applySticky(rawUrl: string, platform: ProxyPlatform, botId: string): string {
  try {
    const u = new URL(rawUrl);
    if (!u.username) return rawUrl; // provider doesn't use userinfo auth
    const session = STICKY_PATTERN
      .replace('{platform}', platform)
      .replace('{bot}', botId);
    // Common provider pattern: user-session-<id>. We append the
    // session token to the existing username preserving the original
    // base username (which usually carries the provider account ID).
    const newUser = `${u.username}-session-${session}`;
    u.username = newUser;
    return u.toString();
  } catch {
    // Unparseable URL — return as-is and let undici surface the error.
    return rawUrl;
  }
}

/**
 * Returns a Dispatcher (ProxyAgent) to pass into `fetch(..., { dispatcher })`
 * — or `undefined` when no proxy is configured for this platform.
 *
 * Per (platform, botId) the same agent instance is returned across
 * calls, so the underlying TCP+TLS pool is reused and the sticky
 * session naturally persists.
 */
export function getDispatcher(
  platform: ProxyPlatform,
  botId?: string,
): Dispatcher | undefined {
  const url = resolveProxyUrl(platform);
  if (!url) return undefined;
  const effectiveBot = botId || 'shared';
  const key = `${platform}:${effectiveBot}`;
  let agent = _agents.get(key);
  if (!agent) {
    const stickyUrl = applySticky(url, platform, effectiveBot);
    agent = buildDispatcher(stickyUrl);
    _agents.set(key, agent);
    // Phase TS-5d.1 — first agent created in this process triggers the
    // keepalive ticker, which then services every agent created later
    // by iterating _agents on each tick.
    ensureKeepaliveStarted();
  }
  return agent;
}

/** Build the right Dispatcher for the URL scheme.
 *
 * - `http://` / `https://` → `ProxyAgent` (HTTP CONNECT, undici-native)
 * - `socks://` / `socks5://` / `socks5h://` / `socks4://` → undici `Agent`
 *   with a `connect` callback that establishes the upstream TCP through
 *   socks-proxy-agent first
 *
 * Phase TS-5g (14.05.2026) — added SOCKS5 path for operator's
 * pool.proxy.market provider (residential, ports 10000-10999, sticky
 * IP per port). */
function buildDispatcher(url: string): Dispatcher {
  const lower = url.toLowerCase();
  if (lower.startsWith('socks://') || lower.startsWith('socks5://') ||
      lower.startsWith('socks5h://') || lower.startsWith('socks4://') ||
      lower.startsWith('socks4a://')) {
    // Parse once at agent-construction time; reused per connect.
    const parsed = new URL(url);
    const socksType = lower.startsWith('socks4') ? 4 : 5;
    const proxyHost = parsed.hostname;
    const proxyPort = Number(parsed.port);
    const userId = parsed.username ? decodeURIComponent(parsed.username) : undefined;
    const password = parsed.password ? decodeURIComponent(parsed.password) : undefined;

    return new Agent({
      connect: (opts, callback) => {
        const isHttps = opts.protocol === 'https:';
        const destPort = Number(opts.port) || (isHttps ? 443 : 80);
        const target = `${opts.hostname}:${destPort}`;
        const t0 = Date.now();
        (async () => {
          try {
            const { socket: rawSocket } = await SocksClient.createConnection({
              proxy: {
                host: proxyHost,
                port: proxyPort,
                type: socksType as 4 | 5,
                ...(userId !== undefined ? { userId } : {}),
                ...(password !== undefined ? { password } : {}),
              },
              command: 'connect',
              destination: { host: opts.hostname as string, port: destPort },
              timeout: 8_000,
            });
            const dtSocks = Date.now() - t0;
            // For HTTPS we need to layer TLS on top of the raw socket
            // before undici can speak HTTP/1.1 + TLS to the target.
            // For HTTP we hand back the raw socket directly.
            if (isHttps) {
              const tlsSocket = tls.connect({
                socket: rawSocket,
                servername: opts.hostname as string,
                ALPNProtocols: ['http/1.1'],
              });
              tlsSocket.once('secureConnect', () => {
                const dtTotal = Date.now() - t0;
                // eslint-disable-next-line no-console
                console.log(
                  `[proxy] SOCKS5+TLS ok target=${target} socks=${dtSocks}ms total=${dtTotal}ms`,
                );
                callback(null, tlsSocket as unknown as Socket);
              });
              tlsSocket.once('error', (e: Error) => {
                // eslint-disable-next-line no-console
                console.log(
                  `[proxy] TLS error target=${target} err=${e.message}`,
                );
                callback(e, null);
              });
              tlsSocket.on('error', (e: Error) => {
                // eslint-disable-next-line no-console
                console.log(
                  `[proxy] post-handshake TLS error target=${target} err=${e.message}`,
                );
              });
            } else {
              // eslint-disable-next-line no-console
              console.log(
                `[proxy] SOCKS5 ok (plain) target=${target} dt=${dtSocks}ms`,
              );
              rawSocket.on('error', (e: Error) => {
                // eslint-disable-next-line no-console
                console.log(`[proxy] socket error target=${target} err=${e.message}`);
              });
              callback(null, rawSocket as Socket);
            }
          } catch (err) {
            const dt = Date.now() - t0;
            // eslint-disable-next-line no-console
            console.log(
              `[proxy] SOCKS5 connect FAILED target=${target} dt=${dt}ms ` +
              `err=${(err as Error).message}`,
            );
            callback(err as Error, null);
          }
        })().catch((err: unknown) => {
          // Defensive — async IIFE shouldn't reject because of inner try,
          // but if it does we still surface the error to undici.
          callback(err as Error, null);
        });
      },
    });
  }
  // Default — HTTP/HTTPS CONNECT proxy.
  return new ProxyAgent(url);
}

/** Test helper — clears the agent cache so tests can re-arm env vars. */
export function _resetForTests(): void {
  stopKeepalive();
  for (const agent of _agents.values()) {
    try {
      void agent.close();
    } catch {
      // best effort
    }
  }
  _agents.clear();
}

/** True iff `PROXY_FALLBACK_TO_DIRECT=1` — operator override for
 *  testing only. Real-money mode must keep this OFF. */
export function fallbackToDirectAllowed(): boolean {
  return process.env['PROXY_FALLBACK_TO_DIRECT'] === '1';
}

/** Diagnostic: returns the current cached (platform, botId) → URL
 *  shape for /api/ts_metrics surfacing. The URL is REDACTED — only
 *  host:port is shown, no credentials. */
export function getDiagnosticState(): {
  enabled: boolean;
  fallback_to_direct: boolean;
  keepalive_interval_s: number;
  keepalive_active: boolean;
  agents: Array<{ key: string; host: string | null }>;
} {
  const agents: Array<{ key: string; host: string | null }> = [];
  for (const key of _agents.keys()) {
    const [platform] = key.split(':') as [ProxyPlatform];
    const raw = resolveProxyUrl(platform);
    let host: string | null = null;
    if (raw) {
      try {
        const u = new URL(raw);
        host = `${u.hostname}:${u.port || (u.protocol === 'https:' ? '443' : '80')}`;
      } catch {
        host = '(unparseable)';
      }
    }
    agents.push({ key, host });
  }
  return {
    enabled:
      !!process.env['PROXY_URL_DEFAULT'] ||
      !!process.env['PROXY_URL_POLYMARKET'] ||
      !!process.env['PROXY_URL_LIMITLESS'] ||
      !!process.env['PROXY_URL_SX'],
    fallback_to_direct: fallbackToDirectAllowed(),
    keepalive_interval_s: getKeepaliveIntervalMs() / 1000,
    keepalive_active: _keepaliveTimer !== null,
    agents,
  };
}
