/**
 * Real HTTP POST to Polymarket CLOB V2.
 *
 * Wraps `executor-ts/src/builders/poly.ts` output (BuiltOrder<PolyOrderBody>)
 * with the actual `fetch` call. Behavior mirrors Python
 * `Scripts/executor/atomic.py:_fire_one_leg` for platform=='polymarket':
 *   - 2s per-order timeout
 *   - 1 retry on 5xx / network
 *   - Circuit-breaker friendly (host-keyed)
 *   - Returns parsed orderId on 2xx, throws structured HttpError on 4xx/5xx
 *
 * Phase TS-5a: this module makes REAL POSTs — only call it from a
 * code path that is gated on DRY_RUN=0 elsewhere. By design, the
 * builder still produces the correct body in DRY_RUN=1 mode (signed
 * if a private key is provided), and `fireArb` writes a paper-trade
 * row instead of calling this module.
 */
import type { Hex } from 'viem';
import {
  HttpError,
  postJson,
  type PostResponse,
} from '../lib/http_client.js';
import { POLY_API_BASE, POLY_CLOB_URL } from '../types/eip712.js';
import type { PolyOrderBody } from '../builders/poly.js';

/** Subset of the Polymarket /order success response we actually use. */
export interface PolyOrderResult {
  /** Order ID returned by Polymarket — needed to listen for fills. */
  orderID: string;
  /** Status: 'matched' | 'live' | 'unmatched' (etc.) */
  status?: string;
  /** Filled amount in USDC (6dp wei as string). */
  takingAmount?: string;
  /** Order hash for on-chain reference. */
  transactionHash?: string;
  /** Echo of POSTed signature so caller can verify. */
  signature?: Hex;
  /** Any other fields server returns we don't strictly need. */
  [k: string]: unknown;
}

export interface PolyPostInput {
  body: PolyOrderBody;
  /** Override URL (default https://clob.polymarket.com/order). */
  url?: string;
  /** Per-request timeout. Default 2000ms (Python parity). */
  timeoutMs?: number;
  /** Optional circuit-breaker hooks. */
  circuitOpen?: () => boolean;
  reportOutcome?: (ok: boolean, status: number | null) => void;
  /**
   * Phase TS-5d — wallet identifier (e.g. 'bot1'..'bot6') used to
   * resolve the residential ProxyAgent. The (platform, botId) pair
   * gets a sticky exit IP so Polymarket sees a consistent IP per
   * derived L2 identity. Pass undefined to disable (direct VPS IP).
   */
  botId?: string;
}

/**
 * POST a signed Polymarket V2 order. Returns the parsed order result
 * on 2xx; throws HttpError on 4xx/5xx (with structured detail so the
 * atomic engine can decide: revert filled legs, abort arb, etc.).
 *
 * The builder MUST have populated `body.order.signature` (an empty
 * string here would be a server reject 'INVALID_SIGNATURE'). This is
 * caller's responsibility — we don't re-validate to keep this module
 * a pure HTTP shim.
 */
export async function postPolyOrder(
  input: PolyPostInput,
): Promise<PostResponse<PolyOrderResult>> {
  const {
    body,
    url = POLY_CLOB_URL,
    timeoutMs = 2_000,
    circuitOpen,
    reportOutcome,
    botId,
  } = input;

  if (!body.order.signature) {
    throw new HttpError(
      'cannot POST unsigned order — builder.signed=false',
      null,
      'clob.polymarket.com',
      '/order',
      0,
    );
  }

  // Phase TS-5d — resolve residential proxy dispatcher (undefined if no
  // proxy configured → direct fetch, current behavior).
  const { getDispatcher } = await import('../lib/proxy_pool.js');
  const dispatcher = getDispatcher('polymarket', botId);

  return await postJson<PolyOrderResult>({
    url,
    body,
    host: 'clob.polymarket.com',
    timeoutMs,
    retries: 1,
    headers: {
      // POLY_BUILDER_* HMAC headers are deprecated for V2 (see Python
      // builders.py:115). The order.signature alone authenticates the
      // request via on-chain ECDSA recovery.
      Accept: 'application/json',
    },
    ...(circuitOpen ? { circuitOpen } : {}),
    ...(reportOutcome ? { reportOutcome } : {}),
    ...(dispatcher ? { dispatcher } : {}),
  });
}

export const POLY_CANCEL_URL = `${POLY_API_BASE}/order`;

import { buildPolyL2Headers, type PolyL2Creds } from '../lib/poly_hmac.js';

export interface PolyCancelInput {
  /** Order ID returned from POST /order — the live order to cancel. */
  orderId: string;
  /** L2 auth creds — operator-provisioned per bot. */
  creds: PolyL2Creds;
  /** Wallet ethAddress for the POLY_ADDRESS header (EIP-55 normalized). */
  ethAddress: string;
  /** Override URL for tests (default https://clob.polymarket.com/order). */
  url?: string;
  timeoutMs?: number;
  circuitOpen?: () => boolean;
  reportOutcome?: (ok: boolean, status: number | null) => void;
}

export interface PolyCancelResult {
  /** Echo of input — server confirms which order was cancelled. */
  canceled?: string[];
  /** Orders the server couldn't cancel (e.g., already filled). */
  not_canceled?: Record<string, string>;
  [k: string]: unknown;
}

/**
 * Phase TS-6 (11.05.2026) — DELETE /order with L2 HMAC.
 *
 * Called from atomic.fireLeg's timeout branch (and the revert path) to
 * clean up live Polymarket orders that haven't matched within the
 * dead-man window. Without this, partial-fill scenarios leave residual
 * orders on the book that count against the wallet's open-order limit
 * and can fill later at adverse prices.
 *
 * Body shape: Polymarket's DELETE endpoint accepts a JSON body
 * `{orderID: string}`. We treat it as a JSON DELETE (Node's fetch
 * supports method: 'DELETE' + body, but some servers reject; if Poly
 * does, fall back to POST /orders/cancel — both routes exist per docs).
 *
 * The HMAC headers depend on body content (prehash includes body), so
 * we build the body string first, then compute headers against it.
 */
export async function deletePolyOrder(
  input: PolyCancelInput,
): Promise<PostResponse<PolyCancelResult>> {
  const {
    orderId,
    creds,
    ethAddress,
    url = POLY_CANCEL_URL,
    timeoutMs = 2_000,
    circuitOpen,
    reportOutcome,
  } = input;

  if (!orderId) {
    throw new HttpError(
      'cannot cancel with empty orderId',
      null,
      'clob.polymarket.com',
      '/order',
      0,
    );
  }
  if (!creds.apiKey || !creds.apiSecret || !creds.passphrase) {
    throw new HttpError(
      'cannot cancel without full L2 creds (apiKey + apiSecret + passphrase)',
      null,
      'clob.polymarket.com',
      '/order',
      0,
    );
  }

  const bodyObj = { orderID: orderId };
  const bodyStr = JSON.stringify(bodyObj);
  const path = new URL(url).pathname;

  const headers = buildPolyL2Headers({
    method: 'DELETE',
    path,
    body: bodyStr,
    apiKey: creds.apiKey,
    apiSecret: creds.apiSecret,
    passphrase: creds.passphrase,
    ethAddress,
  });

  // postJson is POST-only; for DELETE we use a small inline fetch.
  // We can't reuse postJson here because it hardcodes method: 'POST'.
  // Instead implement a minimal DELETE with the same timeout/retry/CB
  // semantics by manually wiring the AbortController + retry loop.
  // For simplicity, retries=1 hardcoded (matches postJson default).
  const host = 'clob.polymarket.com';

  if (circuitOpen && circuitOpen()) {
    throw new HttpError(
      `circuit-breaker open for ${host}`,
      null,
      host,
      path,
      0,
    );
  }

  for (let attempt = 1; attempt <= 2; attempt++) {
    const ac = new AbortController();
    const timer = setTimeout(() => ac.abort(), timeoutMs);
    const start = Date.now();
    try {
      const resp = await fetch(url, {
        method: 'DELETE',
        headers,
        body: bodyStr,
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

      let parsed: PolyCancelResult;
      try {
        parsed = rawBody ? (JSON.parse(rawBody) as PolyCancelResult) : {};
      } catch {
        parsed = { _rawText: rawBody } as unknown as PolyCancelResult;
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
