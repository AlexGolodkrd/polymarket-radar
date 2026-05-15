/**
 * SX Bet taker-fill builder — v2 protocol (Phase audit-5, 15.05.2026).
 *
 * v2 architecture: the SERVER picks matching maker orders; the taker
 * signs a flat fill intent (market, stake, worstOdds, slippage) and
 * the server walks its own orderbook. We no longer need to fetch
 * `/orders` + greedy-match + pass orderHashes; just sign desired-odds
 * + cap and POST.
 *
 * The legacy helpers (filterMatchableOrders, matchOrders, fillableSize,
 * fetchSxMakerOrders) remain exported so anything that wants a
 * pre-flight liquidity check can still walk the book — but the firing
 * path skips them.
 *
 * Verified against `docs.sx.bet/developers/filling-orders.md` 2026-05-15.
 */

import { keccak256, type Hex } from "viem";
import { privateKeyToAccount } from 'viem/accounts';

import {
  SX_DOMAIN,
  SX_FILL_TYPES,
  SX_FILL_URL,
  SX_USDC_BASE_TOKEN,
  SX_USDC_DECIMALS,
  ZERO_BYTES32,
} from '../types/eip712.js';
import type { BuiltOrder } from '../types/deal.js';
import type { Wallet } from '../types/wallet.js';

const ZERO_ADDR = '0x0000000000000000000000000000000000000000' as const;

/**
 * Raw SX maker order shape returned by GET /orders. Kept for the
 * optional pre-flight liquidity check; the firing path no longer
 * consults these in v2.
 */
export interface SxMakerOrder {
  orderHash: Hex;
  percentageOdds: string;
  isMakerBettingOutcomeOne: boolean;
  orderSizeFillable?: string;
  totalBetSize?: string;
  fillAmount?: string;
  orderStatus?: string;
}

export interface SxMatchableOrder {
  orderHash: Hex;
  makerPct: number;
  takerPrice: number;
  fillableUsdc: number;
  raw: SxMakerOrder;
}

function fillableSize(order: SxMakerOrder): number {
  if (order.orderStatus !== undefined && order.orderStatus !== 'ACTIVE') {
    return 0;
  }
  if (order.orderSizeFillable !== undefined && order.orderSizeFillable !== null) {
    return Number(order.orderSizeFillable) / 10 ** SX_USDC_DECIMALS;
  }
  const total = Number(order.totalBetSize ?? 0);
  const filled = Number(order.fillAmount ?? 0);
  return Math.max(0, (total - filled)) / 10 ** SX_USDC_DECIMALS;
}

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
      takerPrice: 1 - makerPct,
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

/** Inputs to `buildSxOrder` (v2). */
export interface BuildSxOrderInput {
  marketHash: Hex;
  outcome: 1 | 2;
  /** Expected price taker pays per $1 face (0..1). */
  takerPrice: number;
  /** Total stake in USDC dollars. */
  sizeUsdc: number;
  wallet: Wallet;
  /** Default 0.005 (0.5¢) slippage tolerance baked into desiredOdds. */
  slippageTolerance?: number;
  /** Optional override of fillSalt (tests). uint256 decimal string. */
  fillSalt?: string;
  /** Test-only signing key. */
  privateKey?: Hex;
  /** Optional override of baseToken (defaults to SX USDC). */
  baseToken?: Hex;
}

/** Flat POST body shape for `/orders/fill/v2`. */
export interface SxFillBody {
  market: string;
  baseToken: Hex;
  isTakerBettingOutcomeOne: boolean;
  stakeWei: string;
  desiredOdds: string;
  oddsSlippage: number;
  taker: Hex;
  takerSig: Hex | '';
  fillSalt: string;
  message: string;
}

/** Generate a 32-byte cryptorandom uint256 as decimal string (SX expects
 *  `fillSalt` as uint256 decimal, not hex). */
function freshSaltDecimal(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  let n = 0n;
  for (const b of bytes) n = (n << 8n) | BigInt(b);
  return n.toString();
}

/**
 * Build SX Bet OrderFill v2 payload + signature.
 *
 * The signing wallet commits to: this market, this taker outcome, at
 * worst these odds (`desiredOdds`) with at most `oddsSlippage`%
 * additional tolerance. Server picks makers to satisfy. If no makers
 * are available within tolerance, server returns 4xx and we surface
 * the body in `leg.error`.
 *
 * `slippageTolerance` (a price-cents fraction we've used historically)
 * is baked into `desiredOdds` so `oddsSlippage` stays at 0 — keeps the
 * fill semantics under our control rather than the server's heuristic.
 */
export async function buildSxOrder(
  input: BuildSxOrderInput,
): Promise<BuiltOrder<SxFillBody>> {
  const {
    marketHash,
    outcome,
    takerPrice,
    sizeUsdc,
    wallet,
    slippageTolerance = 0.005,
    privateKey,
    baseToken = SX_USDC_BASE_TOKEN,
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

  // worst maker odds = 1 - (taker price + slippage), 1e20-scaled.
  const maxTakerPrice = Math.min(0.999, takerPrice + slippageTolerance);
  const minMakerPct = 1 - maxTakerPrice;
  const desiredOdds = BigInt(Math.round(minMakerPct * 1e20)).toString();
  const oddsSlippage = 0;

  const stakeWei = BigInt(Math.round(sizeUsdc * 10 ** SX_USDC_DECIMALS)).toString();
  const fillSalt = input.fillSalt ?? freshSaltDecimal();
  const isTakerBettingOutcomeOne = outcome === 1;

  // body.market = "N/A" per docs example
  // (docs.sx.bet/developers/filling-orders.md, 2026-05). The real
  // marketHash lives inside the signed FillObject — server extracts it
  // from the EIP-712 message hash, not from the body's market field.
  // First live attempt sent body.market=marketHash and the server
  // responded HTTP 400 body="Bad Request"; switching to the literal
  // "N/A" matches the documented body shape exactly.
  const body: SxFillBody = {
    market: 'N/A',
    baseToken,
    isTakerBettingOutcomeOne,
    stakeWei,
    desiredOdds,
    oddsSlippage,
    taker: wallet.ethAddress,
    takerSig: '',
    fillSalt,
    message: 'N/A',
  };

  let signedOk = false;
  if (privateKey && wallet.canSign) {
    const account = privateKeyToAccount(privateKey);
    const message = {
      action: 'N/A',
      market: marketHash,
      betting: 'N/A',
      stake: 'N/A',
      worstOdds: 'N/A',
      worstReturning: 'N/A',
      fills: {
        stakeWei,
        marketHash,
        baseToken,
        desiredOdds,
        oddsSlippage: BigInt(oddsSlippage),
        isTakerBettingOutcomeOne,
        fillSalt: BigInt(fillSalt),
        beneficiary: ZERO_ADDR,
        beneficiaryType: 0,
        cashOutTarget: ZERO_BYTES32,
      },
    } as const;
    const sig = await account.signTypedData({
      domain: SX_DOMAIN,
      types: SX_FILL_TYPES,
      primaryType: 'Details',
      message,
    });
    body.takerSig = sig;
    signedOk = true;
  }

  const signPayload = canonicalJsonBytes(body);

  return {
    platform: 'sx_bet',
    body,
    wouldPostUrl: SX_FILL_URL,
    signed: signedOk,
    expectedPrice: takerPrice,
    expectedSizeUsdc: sizeUsdc,
    signPayload,
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
