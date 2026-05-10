/**
 * SX Bet taker-fill builder — Phase TS-2.
 *
 * Mirrors Python `Scripts/executor/builders.py:build_sx_order`. The fire
 * path is taker-fill: caller already knows market_hash + outcome + max
 * acceptable price, builder fetches matchable maker orders, greedy-matches
 * enough capacity, builds the OrderFill body, signs EIP-712.
 *
 * Phase TS-2 keeps this PURE (no actual HTTP fetch — caller injects a
 * fetcher for tests, or the firer plugs in the real `undici` client in
 * TS-3). Same convention as Python's `fetcher` callable.
 */

import { keccak256, type Hex } from "viem";
import { privateKeyToAccount } from 'viem/accounts';

import {
  SX_DOMAIN,
  SX_FILL_TYPES,
  SX_FILL_URL,
  SX_USDC_DECIMALS,
} from '../types/eip712.js';
import type { BuiltOrder } from '../types/deal.js';
import type { Wallet } from '../types/wallet.js';

/**
 * Raw SX maker order shape returned by GET /orders. We only use a few
 * fields; the rest are passed through opaquely.
 */
export interface SxMakerOrder {
  orderHash: Hex;
  percentageOdds: string; // 1e20-scaled maker odds
  isMakerBettingOutcomeOne: boolean;
  /** Old API field — present in legacy responses (Phase 19v26 fallback) */
  orderSizeFillable?: string;
  /** New API fields (Phase 19v27) — fillable = totalBetSize - fillAmount */
  totalBetSize?: string;
  fillAmount?: string;
  /** Phase 19v27: skip non-ACTIVE orders */
  orderStatus?: string;
}

export interface SxMatchableOrder {
  orderHash: Hex;
  makerPct: number; // 0..1
  takerPrice: number; // 0..1, what taker pays per $1 contract
  fillableUsdc: number;
  raw: SxMakerOrder;
}

/**
 * Phase 19v26 + v27 size parser. SX changed the API twice in May 2026:
 *   - v26: response shape `data.orders[]` → `data[]`
 *   - v27: `orderSizeFillable` field removed → use
 *          `totalBetSize - fillAmount`. Plus `orderStatus !== 'ACTIVE'`
 *          filter.
 *
 * Old TS port mirrors Python parser exactly so live API breaks affect
 * both implementations identically.
 */
function fillableSize(order: SxMakerOrder): number {
  if (order.orderStatus !== undefined && order.orderStatus !== 'ACTIVE') {
    return 0;
  }
  // Forward-compat: if old field is present, prefer it.
  if (order.orderSizeFillable !== undefined && order.orderSizeFillable !== null) {
    return Number(order.orderSizeFillable) / 10 ** SX_USDC_DECIMALS;
  }
  // Default to the new field arithmetic.
  const total = Number(order.totalBetSize ?? 0);
  const filled = Number(order.fillAmount ?? 0);
  return Math.max(0, (total - filled)) / 10 ** SX_USDC_DECIMALS;
}

/**
 * Filter live SX maker orders to those a taker on `takerOutcome` can
 * fill (i.e. on the OPPOSITE outcome). Same predicate as Python.
 */
export function filterMatchableOrders(
  orders: SxMakerOrder[],
  takerOutcome: 1 | 2,
): SxMatchableOrder[] {
  const out: SxMatchableOrder[] = [];
  for (const o of orders) {
    const isMakerOne = !!o.isMakerBettingOutcomeOne;
    const wantsOpposite =
      takerOutcome === 1 ? !isMakerOne : isMakerOne;
    if (!wantsOpposite) continue;

    const makerPct = Number(o.percentageOdds) / 1e20;
    if (!(makerPct > 0 && makerPct < 1)) continue;

    const fillable = fillableSize(o);
    if (fillable <= 0) continue;

    out.push({
      orderHash: o.orderHash,
      makerPct,
      takerPrice: 1 - makerPct, // taker pays (1 − maker odds) per $1 face
      fillableUsdc: fillable,
      raw: o,
    });
  }
  return out;
}

export interface SxMatchResult {
  matched: Array<{ orderHash: Hex; takerPrice: number; takerAmountUsdc: number }>;
  filledUsdc: number;
  avgPrice: number | null;
  partial: boolean;
  shortfallUsdc: number;
  bestPrice: number | null;
  worstPrice: number | null;
}

/**
 * Greedy match — sort orders by best taker price (lowest first), fill
 * `targetSizeUsdc`, stop when cumulative fillable >= target OR next
 * order's price exceeds `maxTakerPrice`.
 */
export function matchOrders(
  matchable: SxMatchableOrder[],
  targetSizeUsdc: number,
  maxTakerPrice: number,
): SxMatchResult {
  const sorted = [...matchable].sort((a, b) => a.takerPrice - b.takerPrice);
  const matched: SxMatchResult['matched'] = [];
  let filled = 0;
  let costWeighted = 0;
  let bestPrice: number | null = null;
  let worstPrice: number | null = null;

  for (const o of sorted) {
    if (filled >= targetSizeUsdc) break;
    if (o.takerPrice > maxTakerPrice) break;

    const remaining = targetSizeUsdc - filled;
    const take = Math.min(remaining, o.fillableUsdc);
    matched.push({
      orderHash: o.orderHash,
      takerPrice: o.takerPrice,
      takerAmountUsdc: Math.round(take * 1e6) / 1e6,
    });
    filled += take;
    costWeighted += o.takerPrice * take;
    if (bestPrice === null || o.takerPrice < bestPrice) bestPrice = o.takerPrice;
    if (worstPrice === null || o.takerPrice > worstPrice) worstPrice = o.takerPrice;
  }

  const avgPrice = filled > 0 ? costWeighted / filled : null;
  return {
    matched,
    filledUsdc: Math.round(filled * 1e6) / 1e6,
    avgPrice: avgPrice !== null ? Math.round(avgPrice * 1e6) / 1e6 : null,
    partial: filled < targetSizeUsdc - 0.000001,
    shortfallUsdc: Math.round(Math.max(0, targetSizeUsdc - filled) * 1e6) / 1e6,
    bestPrice,
    worstPrice,
  };
}

/** Inputs to `buildSxOrder`. */
export interface BuildSxOrderInput {
  marketHash: Hex;
  outcome: 1 | 2;
  /** Expected price taker pays per $1 face. */
  takerPrice: number;
  /** Total stake in USDC dollars. */
  sizeUsdc: number;
  wallet: Wallet;
  /** Pre-fetched maker orders (caller does the HTTP). */
  orders: SxMakerOrder[];
  expirationSecs?: number;
  /** Default 0.005 (0.5¢) slippage tolerance, mirroring Python. */
  slippageTolerance?: number;
  /** Override expiry timestamp (for tests). */
  expirationOverride?: bigint;
  /** Override salt (for tests). */
  salt?: Hex;
  /** Test-only signing key. */
  privateKey?: Hex;
}

export interface SxFillBody {
  marketHash: Hex;
  taker: Hex;
  takerOutcome: 1 | 2;
  fillAmount: string;
  orderHashes: Hex[];
  takerAmounts: string[];
  expiry: string;
  salt: Hex;
  takerSig: Hex | '';
}

/** Generate 32 bytes of cryptorandom hex (mirrors Python uuid4().hex). */
function freshSaltHex(): Hex {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return `0x${Array.from(bytes)
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('')}` as Hex;
}

/**
 * Build SX Bet OrderFill payload. Phase 19v19 invariant: `worstOdds`
 * must be the slippage CAP (max_taker), not the observed worst matched
 * maker price — forward-looking, not backward. Mirrors Python sign path.
 */
export async function buildSxOrder(
  input: BuildSxOrderInput,
): Promise<BuiltOrder<SxFillBody> & { match: SxMatchResult; partial: boolean }> {
  const {
    marketHash,
    outcome,
    takerPrice,
    sizeUsdc,
    wallet,
    orders,
    expirationSecs = 60,
    slippageTolerance = 0.005,
    privateKey,
  } = input;

  if (outcome !== 1 && outcome !== 2) {
    throw new Error(`outcome must be 1 or 2, got ${outcome as number}`);
  }
  if (!(takerPrice > 0 && takerPrice < 1)) {
    throw new Error(`takerPrice out of range: ${takerPrice}`);
  }
  if (sizeUsdc < 1.0) {
    throw new Error(`size below SX min $1: ${sizeUsdc}`);
  }

  const matchable = filterMatchableOrders(orders, outcome);
  const maxTakerPrice = takerPrice + slippageTolerance;
  const match = matchOrders(matchable, sizeUsdc, maxTakerPrice);

  const fillAmountInt = BigInt(Math.round(match.filledUsdc * 10 ** SX_USDC_DECIMALS));
  const expiry =
    input.expirationOverride !== undefined
      ? input.expirationOverride
      : BigInt(Math.floor(Date.now() / 1000) + expirationSecs);

  const body: SxFillBody = {
    marketHash,
    taker: wallet.ethAddress,
    takerOutcome: outcome,
    fillAmount: fillAmountInt.toString(),
    orderHashes: match.matched.map((m) => m.orderHash),
    takerAmounts: match.matched.map((m) =>
      BigInt(Math.round(m.takerAmountUsdc * 10 ** SX_USDC_DECIMALS)).toString(),
    ),
    expiry: expiry.toString(),
    salt: input.salt ?? freshSaltHex(),
    takerSig: '',
  };

  let signedOk = false;
  if (privateKey && wallet.canSign && match.matched.length > 0) {
    // worstOdds: 1e20-scaled like percentageOdds. Phase 19v19: use the
    // CAP (maxTakerPrice → maker pct = 1 - maxTakerPrice), not the
    // observed worst matched price.
    const worstOddsScaled = BigInt(
      Math.round((1 - maxTakerPrice) * 1e20),
    ).toString();
    const account = privateKeyToAccount(privateKey);
    const sig = await account.signTypedData({
      domain: SX_DOMAIN,
      types: SX_FILL_TYPES,
      primaryType: 'Details',
      message: {
        action: 'N/A',
        market: marketHash,
        betting: outcome === 1 ? 'Outcome 1' : 'Outcome 2',
        stake: fillAmountInt.toString(),
        worstOdds: worstOddsScaled,
        executor: wallet.ethAddress,
      },
    });
    body.takerSig = sig;
    signedOk = true;
  }

  // Deterministic JSON of the unsigned body — mirrors Python sort_keys=True.
  const signPayload = canonicalJsonBytes(body);

  return {
    platform: 'sx_bet',
    body,
    wouldPostUrl: SX_FILL_URL,
    signed: signedOk,
    expectedPrice: takerPrice,
    expectedSizeUsdc: sizeUsdc,
    signPayload,
    match,
    partial: match.partial,
  };
}

function canonicalJsonBytes(body: SxFillBody): Uint8Array {
  const sorted: Record<string, unknown> = {};
  for (const k of Object.keys(body).sort()) {
    sorted[k] = (body as unknown as Record<string, unknown>)[k];
  }
  return new TextEncoder().encode(JSON.stringify(sorted));
}

export function payloadDigest(payload: Uint8Array): Hex {
  return keccak256(payload);
}
