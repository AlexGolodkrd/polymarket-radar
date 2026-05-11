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
  });
}
