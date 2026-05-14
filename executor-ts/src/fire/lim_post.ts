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
  /** X-API-Key header value — operator-provisioned per bot. */
  apiKey: string;
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
    url = LIMITLESS_ORDER_URL,
    timeoutMs = 2_000,
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

  return await postJson<LimOrderResult>({
    url,
    body,
    host: 'api.limitless.exchange',
    timeoutMs,
    retries: 1,
    headers: {
      Accept: 'application/json',
      'X-API-Key': apiKey,
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
    url = `${LIMITLESS_ORDER_URL}/${orderId}`,
    timeoutMs = 2_000,
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

  const headers = {
    Accept: 'application/json',
    'X-API-Key': apiKey,
  };

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
