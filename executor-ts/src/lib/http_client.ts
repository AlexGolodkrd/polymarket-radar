/**
 * Shared HTTP client for executor-ts. Mirrors the Python radar's
 * pattern of a per-host requests.Session with keep-alive + retry +
 * circuit-breaker integration.
 *
 * Phase TS-5a: uses Node's native fetch (undici under the hood) with
 * AbortController for timeouts. Does NOT pull undici as an explicit
 * dependency — Node 20+ fetch is already undici-backed and the API
 * is stable. Adds:
 *   - Per-request timeout (default 2s, mirrors Python PER_ORDER_TIMEOUT_S)
 *   - One retry on transient errors (5xx / network) with 200ms jitter
 *   - Structured error: {code, status, body, host, path, attempt}
 *   - Optional circuit-breaker hook (passed in by caller, see
 *     ../circuit_breaker.ts when ported)
 *
 * Used by fire/poly_post.ts, fire/sx_post.ts, fire/lim_post.ts.
 * Deliberately framework-free (no axios / undici-explicit) to keep
 * the executor-ts dependency surface tiny.
 */

const DEFAULT_TIMEOUT_MS = 2_000;
const DEFAULT_RETRY_TIMEOUT_MS = 1_500;

/**
 * Structured HTTP error. Carries enough context for the caller to
 * decide: retry locally, surface to atomic.fire_arb for slippage
 * abort, or trip a circuit breaker.
 */
export class HttpError extends Error {
  override readonly name = 'HttpError';
  constructor(
    message: string,
    readonly status: number | null,
    readonly host: string,
    readonly path: string,
    readonly attempt: number,
    readonly body: string | null = null,
    readonly cause?: unknown,
  ) {
    super(message);
  }

  /** True iff worth retrying (transient: network or 5xx). */
  isTransient(): boolean {
    if (this.status === null) return true; // network error
    return this.status >= 500 && this.status < 600;
  }

  toJSON() {
    return {
      name: this.name,
      message: this.message,
      status: this.status,
      host: this.host,
      path: this.path,
      attempt: this.attempt,
      body: this.body?.slice(0, 500) ?? null,
    };
  }
}

export interface PostOptions {
  /** Full URL or relative path (resolved against `baseUrl` if given). */
  url: string;
  /** Request body — JSON-serialized if object, sent as-is if string. */
  body: unknown;
  /** Extra headers (Content-Type:application/json added automatically). */
  headers?: Record<string, string>;
  /** Per-request timeout in ms. Default 2000 (Python parity). */
  timeoutMs?: number;
  /** Retry on transient (5xx / network). Default 1. */
  retries?: number;
  /** Stable host name for circuit-breaker grouping. */
  host?: string;
  /** Optional circuit-breaker callback: returns false → abort before request. */
  circuitOpen?: () => boolean;
  /** Optional circuit-breaker callback: report success/failure. */
  reportOutcome?: (ok: boolean, status: number | null) => void;
}

export interface PostResponse<T = unknown> {
  status: number;
  headers: Headers;
  body: T;
  rawBody: string;
  attempt: number;
  durationMs: number;
}

/**
 * POST with timeout + 1 retry on transient errors + circuit-breaker
 * hook. Mirrors Python `requests.Session.post` semantics for the
 * executor's fire path.
 */
export async function postJson<T = unknown>(
  opts: PostOptions,
): Promise<PostResponse<T>> {
  const {
    url,
    body,
    headers = {},
    timeoutMs = DEFAULT_TIMEOUT_MS,
    retries = 1,
    host = new URL(url).host,
    circuitOpen,
    reportOutcome,
  } = opts;
  const path = new URL(url).pathname;

  if (circuitOpen && circuitOpen()) {
    throw new HttpError(
      `circuit-breaker open for ${host}`,
      null,
      host,
      path,
      0,
    );
  }

  const payload = typeof body === 'string' ? body : JSON.stringify(body);
  let lastErr: HttpError | null = null;

  for (let attempt = 1; attempt <= retries + 1; attempt++) {
    const ac = new AbortController();
    // Use shorter retry timeout to fail fast on a stuck retry
    const t = attempt === 1 ? timeoutMs : DEFAULT_RETRY_TIMEOUT_MS;
    const timer = setTimeout(() => ac.abort(), t);
    const start = Date.now();
    try {
      const resp = await fetch(url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...headers,
        },
        body: payload,
        signal: ac.signal,
      });
      clearTimeout(timer);
      const rawBody = await resp.text();
      const durationMs = Date.now() - start;

      if (!resp.ok) {
        const err = new HttpError(
          `HTTP ${resp.status} from ${host}${path}`,
          resp.status,
          host,
          path,
          attempt,
          rawBody,
        );
        if (reportOutcome) reportOutcome(false, resp.status);
        if (err.isTransient() && attempt <= retries) {
          // Retry with jitter
          await sleep(150 + Math.random() * 100);
          lastErr = err;
          continue;
        }
        throw err;
      }

      // 2xx — parse JSON if possible
      let parsed: T;
      try {
        parsed = rawBody ? (JSON.parse(rawBody) as T) : (null as T);
      } catch {
        parsed = rawBody as unknown as T;
      }
      if (reportOutcome) reportOutcome(true, resp.status);
      return {
        status: resp.status,
        headers: resp.headers,
        body: parsed,
        rawBody,
        attempt,
        durationMs,
      };
    } catch (e) {
      clearTimeout(timer);
      // Network error / abort
      const status = (e as { status?: number }).status ?? null;
      const err =
        e instanceof HttpError
          ? e
          : new HttpError(
              `network error: ${(e as Error).message}`,
              status,
              host,
              path,
              attempt,
              null,
              e,
            );
      if (reportOutcome) reportOutcome(false, status);
      if (err.isTransient() && attempt <= retries) {
        await sleep(150 + Math.random() * 100);
        lastErr = err;
        continue;
      }
      throw err;
    }
  }

  // Unreachable but TS demands it
  throw lastErr ?? new HttpError('exhausted retries', null, host, path, retries + 1);
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}
