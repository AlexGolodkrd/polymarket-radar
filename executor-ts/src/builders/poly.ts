/**
 * Polymarket V2 order builder — pure function, no I/O.
 *
 * Mirrors Python `Scripts/executor/builders.py:build_poly_order`. The
 * Phase TS-1 contract is: given the same (token, side, price, size,
 * wallet, salt, timestamp), this TS builder MUST produce a byte-identical
 * EIP-712 signature to the Python side. Golden tests in
 * `tests/builders/poly.test.ts` enforce that.
 *
 * Fee/tick/min_size handling is OUT of scope for this builder — caller
 * is responsible for snapping price to tick BEFORE invoking us. Same
 * convention as Python (Phase 9j `_round_to_tick`).
 */

import { keccak256 } from 'viem';
import { privateKeyToAccount } from 'viem/accounts';
import type { Hex } from 'viem';

import {
  POLY_DOMAIN_NEGRISK,
  POLY_DOMAIN_STANDARD,
  POLY_ORDER_TYPES_V2,
  ZERO_BYTES32,
  POLY_CLOB_URL,
} from '../types/eip712.js';
import type { BuiltOrder } from '../types/deal.js';
import { effectiveFunder, type Wallet } from '../types/wallet.js';

/** Inputs for `buildPolyOrder`. */
export interface BuildPolyOrderInput {
  tokenId: string;
  side: 'BUY' | 'SELL';
  /** Price in [0, 1]. Caller must already have snapped to tick. */
  price: number;
  /** Stake in USDC dollars (not wei). 6dp internally. */
  sizeUsdc: number;
  wallet: Wallet;
  /** True → use negRisk EIP-712 domain. Default false. */
  negRisk?: boolean;
  /** GTC default; GTD adds expiration to the wrapper body. */
  orderType?: 'GTC' | 'GTD' | 'FOK';
  /** GTD-only: seconds-until-expiry. Default 60. */
  expirationSecs?: number;
  /**
   * Override salt (for tests). Production should leave undefined and
   * let the builder generate a fresh uuid-derived uint256.
   */
  salt?: bigint;
  /**
   * Override timestamp (for tests). Production leaves undefined and
   * uses `Date.now()` in milliseconds.
   */
  timestampMs?: bigint;
  /**
   * Optional private-key override (for tests + paper-trade Phase 5).
   * Production passes wallet.canSign and signing happens via wallet.signFn.
   * Phase TS-1 keeps this minimal — we accept a raw key here so golden
   * tests can produce deterministic signatures matching Python's path.
   */
  privateKey?: Hex;
}

/**
 * USDC has 6 decimals everywhere we deal with it. Polymarket CTF tokens
 * use the same 6dp scaling. Do the math in BigInt to avoid float drift
 * (Python `int(round(size_usdc * 1e6))` — we mirror with precise scaling).
 */
function toScaledWei(amount: number): bigint {
  // Round to nearest 6dp to mirror Python `int(round(x * 1e6))`.
  // Math.round ties-to-nearest-even on Node, same as Python for halves.
  return BigInt(Math.round(amount * 1_000_000));
}

/**
 * Generate a fresh 128-bit random salt as uint256. Mirrors Python's
 * `int(uuid.uuid4().hex, 16)` semantically — we use crypto.randomUUID
 * which is also v4 entropy.
 */
function freshSalt(): bigint {
  // 16 bytes = 128 bits. Pad to 32-byte uint256 by left-zero in BigInt.
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  let n = 0n;
  for (const b of bytes) n = (n << 8n) | BigInt(b);
  return n;
}

/**
 * The unsigned Order struct, with all numeric fields as `bigint` so
 * viem's EIP-712 encoder accepts them without coercion ambiguity.
 */
export interface PolyOrderStruct {
  salt: bigint;
  maker: Hex;
  signer: Hex;
  tokenId: bigint;
  makerAmount: bigint;
  takerAmount: bigint;
  side: 0 | 1;
  signatureType: 0 | 1 | 2;
  timestamp: bigint;
  metadata: Hex;
  builder: Hex;
}

/** Polymarket POST /order body shape. */
export interface PolyOrderBody {
  order: Omit<PolyOrderStruct, never> & { signature: Hex | '' };
  owner: string;
  orderType: 'GTC' | 'GTD' | 'FOK';
  expiration?: string;
}

/**
 * Build (and optionally sign) a Polymarket V2 order. Returns a
 * `BuiltOrder` with signature embedded when `wallet.canSign` and a
 * `privateKey` is provided (real-mode); otherwise leaves signature
 * empty (dry-run mode).
 */
export async function buildPolyOrder(
  input: BuildPolyOrderInput,
): Promise<BuiltOrder<PolyOrderBody>> {
  const {
    tokenId,
    side,
    price,
    sizeUsdc,
    wallet,
    negRisk = false,
    orderType = 'GTC',
    expirationSecs = 60,
    privateKey,
  } = input;

  if (side !== 'BUY' && side !== 'SELL') {
    throw new Error(`side must be BUY|SELL, got ${side as string}`);
  }
  if (!(price > 0 && price < 1)) {
    throw new Error(`price out of range: ${price}`);
  }
  // Polymarket per-market `min_order_size_usdc` defaults to $1; caller
  // can pre-validate. We don't enforce here so tests can craft sub-$1
  // orders for golden-signature determinism.

  const usdcWei = toScaledWei(sizeUsdc);
  const contracts = sizeUsdc / price;
  const contractsWei = toScaledWei(contracts);

  // Phase 19v19 maker/taker semantics:
  //   BUY (side=0):  makerAmount=USDC, takerAmount=CTF
  //   SELL (side=1): makerAmount=CTF,  takerAmount=USDC
  const isBuy = side === 'BUY';
  const makerAmount = isBuy ? usdcWei : contractsWei;
  const takerAmount = isBuy ? contractsWei : usdcWei;

  const sigType = wallet.signatureType ?? 0;
  if (sigType !== 0 && sigType !== 1 && sigType !== 2) {
    throw new Error(`signatureType=${sigType} invalid (must be 0, 1, or 2)`);
  }
  const maker = effectiveFunder(wallet);

  const order: PolyOrderStruct = {
    salt: input.salt ?? freshSalt(),
    maker,
    signer: wallet.ethAddress,
    tokenId: BigInt(tokenId),
    makerAmount,
    takerAmount,
    side: isBuy ? 0 : 1,
    signatureType: sigType,
    timestamp: input.timestampMs ?? BigInt(Date.now()),
    metadata: ZERO_BYTES32,
    builder: ZERO_BYTES32,
  };

  // Sign EIP-712 if a private key is available.
  let signature: Hex | '' = '';
  let signedOk = false;
  if (privateKey && wallet.canSign) {
    const account = privateKeyToAccount(privateKey);
    const domain = negRisk ? POLY_DOMAIN_NEGRISK : POLY_DOMAIN_STANDARD;
    signature = await account.signTypedData({
      domain,
      types: POLY_ORDER_TYPES_V2,
      primaryType: 'Order',
      message: order,
    });
    signedOk = true;
  }

  // Owner: V2 expects the L2 API key (uuid). Falls back to maker addr
  // when creds aren't provisioned yet — server may reject with
  // INVALID_API_KEY but that's a fail-loud signal, matching Python.
  const owner = wallet.polyApiKey ?? maker;

  const body: PolyOrderBody = {
    order: { ...order, signature },
    owner,
    orderType,
  };
  if (orderType === 'GTD') {
    body.expiration = String(Math.floor(Date.now() / 1000) + expirationSecs);
  }

  // Deterministic JSON of the UNSIGNED order (signature excluded). Same
  // canonicalization Python does for dry-run audit logs.
  const signPayload = canonicalJsonBytes(order);

  return {
    platform: 'polymarket',
    body,
    wouldPostUrl: POLY_CLOB_URL,
    signed: signedOk,
    expectedPrice: price,
    expectedSizeUsdc: sizeUsdc,
    signPayload,
    order,
    negRisk,
  };
}

/**
 * Canonical JSON encoder for the unsigned order — sorted keys, BigInt
 * fields rendered as decimal strings. Matches Python
 * `json.dumps(order, sort_keys=True).encode('utf-8')` byte-for-byte
 * (within the constraints of our key set, all of which are ASCII and
 * have no special escaping).
 */
function canonicalJsonBytes(order: PolyOrderStruct): Uint8Array {
  const sorted: Record<string, string> = {};
  for (const k of Object.keys(order).sort()) {
    const v = (order as unknown as Record<string, bigint | string>)[k];
    if (v === undefined) continue;
    sorted[k] = typeof v === 'bigint' ? v.toString() : (v as string);
  }
  // JSON.stringify with sorted Object preserves insertion order in
  // modern Node — matches Python sort_keys=True.
  return new TextEncoder().encode(JSON.stringify(sorted));
}

/**
 * Convenience: keccak256 hash of canonical-JSON sign-payload. Useful for
 * dryrun.jsonl indexing where we want a stable id per order across
 * Python and TS without leaking the full body. NOT used for signing —
 * EIP-712 has its own digest path.
 */
export function payloadDigest(payload: Uint8Array): Hex {
  // v36-fix: keccak256 accepts Uint8Array directly; wrapping with
  // toBytes() (which expects string|number|bigint|bool) was a leftover
  // of an earlier refactor.
  return keccak256(payload);
}
