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

// Phase audit-3 (15.05.2026) — 8s default to absorb cold SOCKS5+TLS
// handshake on first fire (200-800ms for SOCKS5, +200-400ms for TLS).
// Old 2s default kept timing out residential-proxy traffic before the
// actual exchange POST could even start. Same default used in
// `atomic.ts:PER_LEG_TIMEOUT_MS` so neither layer pre-empts the other.
const DEFAULT_TIMEOUT_MS = Number(process.env.PER_ORDER_TIMEOUT_S ?? '8') * 1000;
const DEFAULT_RETRY_TIMEOUT_MS = Math.max(1_500, DEFAULT_TIMEOUT_MS * 0.6);

/**
 * BigInt-safe JSON.stringify. Polymarket / Limitless order structs use
 * BigInt for uint256 fields (tokenId, makerAmount, etc.); plain
 * JSON.stringify throws `TypeError: Do not know how to serialize a BigInt`.
 * Exchanges accept these as decimal strings.
 *
 * Exported so the per-platform POST helpers can serialize ONCE (needed
 * for HMAC signing — body bytes must match what gets posted).
 */
export function jsonStringifyBigIntSafe(value: unknown): string {
  return JSON.stringify(value, (_k, v) =>
    typeof v === 'bigint' ? v.toString() : v,
  );
}

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
  /**
   * Phase TS-5d (14.05.2026) — residential proxy dispatcher. Pre-resolved
   * via `proxy_pool.getDispatcher(platform, botId)`. When undefined we
   * use Node's default global dispatcher (direct from VPS IP). When set
   * the request goes through the ProxyAgent — exit IP comes from the
   * residential proxy, sticky-per-bot. See
   * .claude/skills/residential-proxy-routing/SKILL.md for the contract.
   */
  dispatcher?: import('undici').Dispatcher;
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
    dispatcher,
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

  const payload = typeof body === 'string' ? body : jsonStringifyBigIntSafe(body);
  let lastErr: HttpError | null = null;

  for (let attempt = 1; attempt <= retries + 1; attempt++) {
    const ac = new AbortController();
    // Use shorter retry timeout to fail fast on a stuck retry
    const t = attempt === 1 ? timeoutMs : DEFAULT_RETRY_TIMEOUT_MS;
    const timer = setTimeout(() => ac.abort(), t);
    const start = Date.now();
    try {
      // Phase TS-5d — pass undici dispatcher (ProxyAgent) when provided.
      // Node's fetch type signature doesn't include `dispatcher` (it's
      // undici-specific), but Node 20+ fetch IS undici-backed and
      // honors this field at runtime.
      const fetchOpts: Parameters<typeof fetch>[1] & { dispatcher?: unknown } = {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...headers,
        },
        body: payload,
        signal: ac.signal,
      };
      if (dispatcher) {
        fetchOpts.dispatcher = dispatcher;
      }
      const resp = await fetch(url, fetchOpts);
      clearTimeout(timer);
      const rawBody = await resp.text();
      const durationMs = Date.now() - start;

      if (!resp.ok) {
        // Phase audit-3 (15.05.2026) — include a truncated response body
        // in the error message so the cause shows up in fire-reject logs
        // and dryrun.jsonl leg_details. Before this, operator only saw
        // "HTTP 400 from api.limitless.exchange/orders" with no hint
        // which field the server rejected.
        //
        // Phase audit-4 (15.05.2026) — try to extract a structured
        // `error`/`message`/`reason` field from JSON bodies first; fall
        // back to raw text for non-JSON. Exchanges that return well-
        // shaped errors (Polymarket V2 returns `{error, errorMsg}`,
        // Limitless `{message}`, SX `{error}`) get a clean one-liner
        // instead of a 300-char JSON blob.
        let bodySnippet = '';
        if (rawBody) {
          let extracted: string | null = null;
          try {
            const parsed = JSON.parse(rawBody) as Record<string, unknown>;
            // Order matters: structured arrays of {field, message}
            // beat generic "error: Bad Request" envelopes. Limitless
            // V2 returns the validator detail as `message: Array<{field,
            // message}>` plus `error: "Bad Request"` — taking `error`
            // first hid every per-field reason (Phase audit-7).
            const candidates = ['message', 'errorMsg', 'reason', 'detail', 'error'];
            for (const k of candidates) {
              const v = parsed[k];
              if (typeof v === 'string' && v.length > 0) {
                extracted = v;
                break;
              }
              if (Array.isArray(v) && v.length > 0) {
                // Array of strings → join
                if (typeof v[0] === 'string') {
                  extracted = (v as string[]).join('; ');
                  break;
                }
                // Array of validator objects {field, message, ...} →
                // pretty-print each entry so the operator sees the
                // actual field that failed.
                if (typeof v[0] === 'object' && v[0] !== null) {
                  extracted = (v as Array<Record<string, unknown>>)
                    .map((it) => {
                      const f = typeof it['field'] === 'string' ? it['field'] : '';
                      const m = typeof it['message'] === 'string' ? it['message'] : JSON.stringify(it);
                      return f ? `${f}: ${m}` : m;
                    })
                    .join('; ');
                  break;
                }
              }
            }
          } catch {
            // non-JSON body — fall through to raw truncation
          }
          bodySnippet = extracted
            ? ` body="${extracted.replace(/\s+/g, ' ').slice(0, 280)}"`
            : ` body=${rawBody.replace(/\s+/g, ' ').slice(0, 300)}`;
        }

        // Phase audit-4 (15.05.2026) — operator-visible categorization.
        // Cloudflare 403/429 looks different from exchange-level 400/422.
        // Pre-tag so leg.error reads "[CF-BLOCK] HTTP 403 ..." in dryrun.
        let prefix = '';
        const cfMitigated = resp.headers.get('cf-mitigated');
        const cfRay = resp.headers.get('cf-ray');
        if (cfMitigated || (cfRay && resp.status === 403)) {
          prefix = '[CF-BLOCK] ';
        } else if (resp.status === 429) {
          prefix = '[RATE-LIMIT] ';
        }
        const err = new HttpError(
          `${prefix}HTTP ${resp.status} from ${host}${path}${bodySnippet}`,
          resp.status,
          host,
          path,
          attempt,
          rawBody,
        );
        if (reportOutcome) reportOutcome(false, resp.status);

        // 429 → exponential backoff retry (1 extra attempt). Honors
        // Retry-After if present, else 500-1500ms.
        if (resp.status === 429 && attempt <= retries) {
          const retryAfterHeader = resp.headers.get('retry-after');
          const retryAfterMs = retryAfterHeader
            ? Math.min(5000, Number(retryAfterHeader) * 1000 || 0)
            : 500 + Math.random() * 1000;
          await sleep(retryAfterMs);
          lastErr = err;
          continue;
        }
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
