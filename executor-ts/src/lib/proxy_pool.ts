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
import { Dispatcher, ProxyAgent } from 'undici';

export type ProxyPlatform = 'polymarket' | 'limitless' | 'sx';

/** Sentinel meaning "explicit direct, no proxy" (skips default URL). */
const NONE_SENTINEL = 'NONE';

/** Pattern used to template session-stickiness into the proxy
 *  username. Most residential providers accept `user-session-X`
 *  to bind a session to a sticky exit IP. Override via env. */
const DEFAULT_STICKY_PATTERN = '{platform}-{bot}';

const STICKY_PATTERN =
  process.env['PROXY_STICKY_SESSION_PATTERN'] || DEFAULT_STICKY_PATTERN;

/** Cache: `${platform}:${botId}` → ProxyAgent. Reused across requests
 *  so the underlying connection pool persists (sticky-session friendly,
 *  also amortizes TLS handshake cost). */
const _agents = new Map<string, ProxyAgent>();

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
    agent = new ProxyAgent(stickyUrl);
    _agents.set(key, agent);
  }
  return agent;
}

/** Test helper — clears the agent cache so tests can re-arm env vars. */
export function _resetForTests(): void {
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
    agents,
  };
}
