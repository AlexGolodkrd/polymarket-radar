/**
 * Real HTTP POST to SX Bet `/orders/fill`.
 *
 * SX Bet uses a taker-fill model — the body contains an array of
 * pre-signed maker order hashes that the taker is committing to fill,
 * plus the taker's EIP-712 signature over a Details struct. See
 * `builders/sx.ts` for the body shape.
 *
 * Mirrors Python `atomic.py:_fire_one_leg` for platform=='sx_bet'.
 */
import {
  HttpError,
  postJson,
  type PostResponse,
} from '../lib/http_client.js';
import { SX_FILL_URL } from '../types/eip712.js';
import type { SxFillBody } from '../builders/sx.js';

/** SX fill response shape (subset). */
export interface SxOrderResult {
  status: 'success' | 'failure';
  data?: {
    /** Taker's filled order hashes (echo of input). */
    fillHash?: string;
    /** Filled amount per maker in 1e6 USDC units. */
    fillAmount?: string;
    /** SX-side server timestamp. */
    timestamp?: number;
  };
  /** Error message on failure. */
  error?: string;
  [k: string]: unknown;
}

export interface SxPostInput {
  body: SxFillBody;
  url?: string;
  timeoutMs?: number;
  circuitOpen?: () => boolean;
  reportOutcome?: (ok: boolean, status: number | null) => void;
}

export async function postSxFill(
  input: SxPostInput,
): Promise<PostResponse<SxOrderResult>> {
  const {
    body,
    url = SX_FILL_URL,
    timeoutMs = 2_000,
    circuitOpen,
    reportOutcome,
  } = input;

  if (!body.takerSig) {
    throw new HttpError(
      'cannot POST unsigned fill — builder.signed=false',
      null,
      'api.sx.bet',
      '/orders/fill',
      0,
    );
  }
  if (!body.orderHashes || body.orderHashes.length === 0) {
    throw new HttpError(
      'cannot POST fill with empty orderHashes (no matchable makers)',
      null,
      'api.sx.bet',
      '/orders/fill',
      0,
    );
  }

  return await postJson<SxOrderResult>({
    url,
    body,
    host: 'api.sx.bet',
    timeoutMs,
    retries: 1,
    headers: { Accept: 'application/json' },
    ...(circuitOpen ? { circuitOpen } : {}),
    ...(reportOutcome ? { reportOutcome } : {}),
  });
}
