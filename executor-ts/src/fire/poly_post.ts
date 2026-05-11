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
  });
}

/**
 * DELETE /order/{id} cancel — uses L2 HMAC headers, NOT signature
 * (V2 cancel auth model). This is used by the revert path when a
 * partial fill needs to be cleaned up.
 *
 * Phase TS-5a stub: implementation returns the URL + headers shape
 * but caller is expected to wire L2 HMAC via builder.buildPolyCancel
 * (TS port of builders.py:build_poly_cancel — not yet ported).
 */
export const POLY_CANCEL_URL = `${POLY_API_BASE}/order`;
