/**
 * SX Bet maker-orderbook fetcher.
 *
 * Mirrors Python `Scripts/arb_server.py::_fetch_sx_orders`. Returns the
 * raw `SxMakerOrder[]` so `buildSxOrder` can match against live makers.
 * Without this, every cross-platform arb that touches SX got its leg
 * rejected with `built.signed=false` because matchOrders received an
 * empty array → 0 fill → not signed.
 *
 * API contract (current as of 2026-05):
 *   GET https://api.sx.bet/orders?marketHashes={hash}
 *
 * Response shape (v27, May 2026): `{data: SxMakerOrder[]}` — orders
 * include `totalBetSize`, `fillAmount`, `orderStatus`, `percentageOdds`,
 * `isMakerBettingOutcomeOne`, `orderHash`.
 *
 * **MANDATORY proxy:** this fetch participates in the signing pipeline
 * (matchOrders output is signed into the fillBody), so per operator
 * directive on 2026-05-15 it MUST route through the residential proxy
 * on every call. Defaults to `getDispatcher('sx', botId)` which returns
 * a per-bot sticky-session ProxyAgent. If no proxy is configured the
 * call falls through to direct fetch — same behavior as POST helpers.
 */
import { HttpError } from '../lib/http_client.js';
import type { SxMakerOrder } from '../builders/sx.js';

const SX_ORDERS_URL = 'https://api.sx.bet/orders';
const DEFAULT_TIMEOUT_MS = 2_000;

export interface FetchSxOrdersOpts {
  marketHash: string;
  timeoutMs?: number;
  /** Wallet/bot id used to resolve the per-bot residential proxy dispatcher.
   *  Strongly recommended — see file-header note on mandatory proxy. */
  botId?: string;
  /** Pre-resolved dispatcher (overrides botId lookup; used by tests). */
  dispatcher?: import('undici').Dispatcher;
}

export interface SxOrdersResponse {
  data?: SxMakerOrder[];
  /** Some legacy envelopes have `data.orders[]` — handled below. */
  orders?: SxMakerOrder[];
  [k: string]: unknown;
}

/**
 * GET /orders?marketHashes=<hash>. Returns array (possibly empty) on
 * success; throws HttpError on 4xx/5xx so caller can surface the reason
 * back to the arb result.
 */
export async function fetchSxMakerOrders(
  opts: FetchSxOrdersOpts,
): Promise<SxMakerOrder[]> {
  const { marketHash, timeoutMs = DEFAULT_TIMEOUT_MS, botId } = opts;
  if (!marketHash) {
    throw new HttpError('marketHash required', null, 'api.sx.bet', '/orders', 0);
  }
  // Resolve residential proxy dispatcher. Caller can override (tests
  // pass a fake), otherwise lookup by (platform, botId) so this fetch
  // shares the same sticky exit IP as the matching POST.
  let dispatcher = opts.dispatcher;
  if (!dispatcher) {
    const { getDispatcher } = await import('../lib/proxy_pool.js');
    dispatcher = getDispatcher('sx', botId);
  }
  const url = `${SX_ORDERS_URL}?marketHashes=${encodeURIComponent(marketHash)}`;
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), timeoutMs);
  try {
    const fetchOpts: Parameters<typeof fetch>[1] & { dispatcher?: unknown } = {
      method: 'GET',
      headers: { Accept: 'application/json' },
      signal: ac.signal,
    };
    if (dispatcher) fetchOpts.dispatcher = dispatcher;
    const resp = await fetch(url, fetchOpts);
    const rawBody = await resp.text();
    if (!resp.ok) {
      throw new HttpError(
        `HTTP ${resp.status} from api.sx.bet/orders`,
        resp.status,
        'api.sx.bet',
        '/orders',
        1,
        rawBody,
      );
    }
    let parsed: SxOrdersResponse;
    try {
      parsed = JSON.parse(rawBody) as SxOrdersResponse;
    } catch (e) {
      throw new HttpError(
        `non-JSON response from /orders: ${(e as Error).message}`,
        resp.status,
        'api.sx.bet',
        '/orders',
        1,
        rawBody.slice(0, 200),
      );
    }
    // Phase 19v26: API used to wrap in `data.orders[]`; current returns
    // `data[]`. Handle both for resilience to future flips.
    const list =
      (Array.isArray(parsed.data) ? parsed.data : null) ??
      (Array.isArray(parsed.orders) ? parsed.orders : null) ??
      ((parsed.data as unknown as { orders?: SxMakerOrder[] })?.orders ?? []);
    return Array.isArray(list) ? list : [];
  } finally {
    clearTimeout(timer);
  }
}
