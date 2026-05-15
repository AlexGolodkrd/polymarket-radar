/**
 * Real HTTP POST to Limitless Exchange `/orders`.
 *
 * Same shape as Polymarket: signed EIP-712 Order in the body, server
 * verifies via ECDSA recovery + maker balance. Uses X-API-Key header
 * for L2 auth (operator-provisioned, NOT signed).
 */
import {
  HttpError,
  postJson,
  type PostResponse,
} from '../lib/http_client.js';
import { LIMITLESS_ORDER_URL } from '../types/eip712.js';
import type { LimitlessOrderBody } from '../builders/limitless.js';

export interface LimOrderResult {
  /** Server-assigned order ID. */
  id?: string;
  status?: 'open' | 'matched' | 'cancelled' | 'rejected';
  matchedAmount?: string;
  /** Echo of submitted signature for verification. */
  signature?: string;
  [k: string]: unknown;
}

export interface LimPostInput {
  body: LimitlessOrderBody;
  /**
   * Phase TS-5f (14.05.2026) — Limitless API token ID (formerly named
   * "apiKey", but in the new HMAC scheme it's just the public identifier
   * sent as `lmts-api-key` header). Kept the field name for backwards
   * compat with existing callers; semantically it's the tokenId.
   */
  apiKey: string;
  /**
   * Phase TS-5f — base64-encoded HMAC secret returned alongside the
   * token at creation. Required for signing. If undefined or empty,
   * we fall back to legacy `X-API-Key` header (will 401 against the
   * current Limitless API — but preserves the test path for callers
   * that haven't migrated yet).
   */
  apiSecret?: string;
  url?: string;
  timeoutMs?: number;
  circuitOpen?: () => boolean;
  reportOutcome?: (ok: boolean, status: number | null) => void;
  /** Phase TS-5d — wallet id for residential proxy sticky session. */
  botId?: string;
}

export async function postLimOrder(
  input: LimPostInput,
): Promise<PostResponse<LimOrderResult>> {
  const {
    body,
    apiKey,
    apiSecret,
    url = LIMITLESS_ORDER_URL,
    timeoutMs = Number(process.env.PER_ORDER_TIMEOUT_S ?? 8) * 1000,
    circuitOpen,
    reportOutcome,
    botId,
  } = input;

  if (!body.order.signature) {
    throw new HttpError(
      'cannot POST unsigned order — builder.signed=false',
      null,
      'api.limitless.exchange',
      '/orders',
      0,
    );
  }
  if (!apiKey) {
    throw new HttpError(
      'cannot POST without X-API-Key (Limitless requires L2 key)',
      null,
      'api.limitless.exchange',
      '/orders',
      0,
    );
  }

  // Phase TS-5d — residential proxy dispatcher (undefined if not configured).
  const { getDispatcher } = await import('../lib/proxy_pool.js');
  const dispatcher = getDispatcher('limitless', botId);

  // Phase TS-5f — HMAC-signed headers (when secret is provided). The
  // server requires the EXACT body string we send to be the one we
  // signed, so serialize ONCE here and pass via `body: jsonBody` (a
  // string), not the object — `postJson` would re-serialize the object
  // with arbitrary key ordering and break the signature.
  //
  // Phase audit-6 (15.05.2026) — Limitless V2 validators are mixed:
  // some order fields are @IsNumber, others @IsString. Empirically
  // verified via direct probe with mixed types:
  //   - makerAmount, takerAmount, nonce, feeRateBps → must be Number
  //   - expiration                                  → must be String
  //   - tokenId, salt                               → string (uint256)
  // Sending expiration as Number produced
  //   `{"message":[{"field":"order.expiration","message":"expiration must be a string"}]}`
  // even with everything else correct.
  const NUMERIC_ORDER_FIELDS = new Set([
    'makerAmount',
    'takerAmount',
    'nonce',
    'feeRateBps',
  ]);
  const jsonBody = JSON.stringify(body, (key, value) => {
    if (typeof value !== 'bigint') return value;
    if (NUMERIC_ORDER_FIELDS.has(key)) return Number(value);
    return value.toString();
  });
  const { pathForSigning, signLmtsRequest } = await import('../lib/limitless_hmac.js');
  let authHeaders: Record<string, string>;
  if (apiSecret) {
    const path = pathForSigning(url);
    authHeaders = { ...signLmtsRequest(apiKey, apiSecret, 'POST', path, jsonBody) };
  } else {
    // Legacy bearer path — preserved for any caller mid-migration. Will
    // 401 against the current Limitless API (see TS-5f skill); used in
    // tests that haven't been updated yet.
    authHeaders = { 'X-API-Key': apiKey };
  }

  return await postJson<LimOrderResult>({
    url,
    body: jsonBody,
    host: 'api.limitless.exchange',
    timeoutMs,
    retries: 1,
    headers: {
      Accept: 'application/json',
      ...authHeaders,
    },
    ...(circuitOpen ? { circuitOpen } : {}),
    ...(reportOutcome ? { reportOutcome } : {}),
    ...(dispatcher ? { dispatcher } : {}),
  });
}

/**
 * Phase TS-6.2 (11.05.2026) — Limitless DELETE /orders/{id}.
 *
 * Mirrors `Scripts/executor/builders.py:build_limitless_cancel`. Auth is
 * a single `X-API-Key` header — no HMAC, no signature, no body. Called
 * from atomic.fireLeg's timeout branch when a Limitless order doesn't
 * fill within the dead-man window; without this the order sits on the
 * book until natural-expire and can fill at adverse prices.
 *
 * The Limitless API also supports batch cancel (POST /orders/cancel-batch)
 * and market-wide cancel (DELETE /orders/all/{slug}); we ship single-
 * order cancel here since it's what the timeout path needs. Batch cancel
 * lands when the watchdog/killswitch port to TS (TS-5e+ territory).
 */
export interface LimCancelInput {
  orderId: string;
  apiKey: string;
  /** Phase TS-5f.3 — HMAC secret. When present we sign the DELETE with
   *  HMAC headers; when absent we fall back to legacy X-API-Key bearer
   *  (which 401s on Trading-scope tokens in current Limitless V2). */
  apiSecret?: string;
  /** Override URL for tests. Default https://api.limitless.exchange/orders/{id}. */
  url?: string;
  timeoutMs?: number;
  circuitOpen?: () => boolean;
  reportOutcome?: (ok: boolean, status: number | null) => void;
}

export interface LimCancelResult {
  /** Limitless server may return `{cancelled: true, orderId: "..."}` or
   * simply a 204. Both shapes are valid; we parse what's there. */
  cancelled?: boolean;
  orderId?: string;
  [k: string]: unknown;
}

export async function deleteLimOrder(
  input: LimCancelInput,
): Promise<PostResponse<LimCancelResult>> {
  const {
    orderId,
    apiKey,
    apiSecret,
    url = `${LIMITLESS_ORDER_URL}/${orderId}`,
    timeoutMs = Number(process.env.PER_ORDER_TIMEOUT_S ?? 8) * 1000,
    circuitOpen,
    reportOutcome,
  } = input;

  if (!orderId) {
    throw new HttpError(
      'cannot cancel Limitless order with empty orderId',
      null,
      'api.limitless.exchange',
      '/orders',
      0,
    );
  }
  if (!apiKey) {
    throw new HttpError(
      'cannot cancel without X-API-Key (Limitless requires L2 key)',
      null,
      'api.limitless.exchange',
      `/orders/${orderId}`,
      0,
    );
  }

  // postJson is POST-only; for DELETE we use a small inline retry/timeout
  // loop matching its semantics (1 retry on transient, 2s default).
  // Mirrors deletePolyOrder in poly_post.ts (same pattern).
  const host = 'api.limitless.exchange';
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

  // Phase TS-5f.3 — HMAC for DELETE. Body is empty string for cancel.
  const { pathForSigning, signLmtsRequest } = await import('../lib/limitless_hmac.js');
  const headers: Record<string, string> = {
    Accept: 'application/json',
  };
  if (apiSecret) {
    Object.assign(
      headers,
      signLmtsRequest(apiKey, apiSecret, 'DELETE', pathForSigning(url), ''),
    );
  } else {
    headers['X-API-Key'] = apiKey;
  }

  for (let attempt = 1; attempt <= 2; attempt++) {
    const ac = new AbortController();
    const timer = setTimeout(() => ac.abort(), timeoutMs);
    const start = Date.now();
    try {
      const resp = await fetch(url, {
        method: 'DELETE',
        headers,
        signal: ac.signal,
      });
      clearTimeout(timer);
      const rawBody = await resp.text();
      const durationMs = Date.now() - start;

      if (!resp.ok) {
        if (reportOutcome) reportOutcome(false, resp.status);
        const err = new HttpError(
          `HTTP ${resp.status} from ${host}${path}`,
          resp.status,
          host,
          path,
          attempt,
          rawBody,
        );
        if (err.isTransient() && attempt < 2) {
          await new Promise((r) => setTimeout(r, 150 + Math.random() * 100));
          continue;
        }
        throw err;
      }

      // 204 No Content path: server may return empty body on success.
      let parsed: LimCancelResult;
      if (!rawBody) {
        parsed = { cancelled: true, orderId };
      } else {
        try {
          parsed = JSON.parse(rawBody) as LimCancelResult;
        } catch {
          parsed = { _rawText: rawBody, cancelled: true, orderId } as
            unknown as LimCancelResult;
        }
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
      if (err.isTransient() && attempt < 2) {
        await new Promise((r) => setTimeout(r, 150 + Math.random() * 100));
        continue;
      }
      throw err;
    }
  }
  throw new HttpError('cancel exhausted retries', null, host, path, 2);
}
