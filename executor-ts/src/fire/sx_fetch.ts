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
 * **No proxy** — this is a read-only orderbook fetch. Per operator
 * clarification on 2026-05-15, residential proxy is reserved for the
 * actual order POST (`POST /orders/fill`) that puts capital at risk.
 * The maker book is public data — fetching it from the VPS direct IP
 * doesn't expose a wallet (no L2 auth, no signed body), so there's no
 * IP↔wallet correlation to break. Saves residential bandwidth too.
 *
 * If a test needs to override the dispatcher (e.g. inject a fake to
 * simulate proxy errors), pass `opts.dispatcher`. Production code
 * does NOT pass one — fetch goes direct.
 */
import { HttpError } from '../lib/http_client.js';
import type { SxMakerOrder } from '../builders/sx.js';

const SX_ORDERS_URL = 'https://api.sx.bet/orders';
const DEFAULT_TIMEOUT_MS = 2_000;

export interface FetchSxOrdersOpts {
  marketHash: string;
  timeoutMs?: number;
  /** Pre-resolved dispatcher (tests only). Production: leave undefined
   *  so the fetch goes direct from the VPS. */
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
  const { marketHash, timeoutMs = DEFAULT_TIMEOUT_MS } = opts;
  if (!marketHash) {
    throw new HttpError('marketHash required', null, 'api.sx.bet', '/orders', 0);
  }
  // Public orderbook → direct from VPS. Only the matching POST that
  // signs+places the taker fill goes through the residential proxy.
  const dispatcher = opts.dispatcher;
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
