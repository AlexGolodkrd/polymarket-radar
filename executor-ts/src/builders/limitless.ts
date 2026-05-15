/**
 * Limitless Exchange (Base) order builder — Phase TS-2.
 *
 * Mirrors Python `Scripts/executor/builders.py:build_limitless_order`.
 * Same EIP-712 structure as Polymarket but on Base mainnet (chainId 8453)
 * with a different verifyingContract, fixed 'name'/'version', and the
 * V1 Order shape (with expiration/nonce/feeRateBps fields).
 */

import { keccak256, type Hex } from "viem";
import { privateKeyToAccount } from 'viem/accounts';

import {
  LIMITLESS_CHAIN_ID,
  LIMITLESS_DEFAULT_EXCHANGE,
  LIMITLESS_DOMAIN_NAME,
  LIMITLESS_DOMAIN_VERSION,
  LIMITLESS_ORDER_TYPES,
  LIMITLESS_ORDER_URL,
} from '../types/eip712.js';
import type { BuiltOrder } from '../types/deal.js';
import type { Wallet } from '../types/wallet.js';

const ZERO_ADDR = '0x0000000000000000000000000000000000000000' as const;

export interface BuildLimitlessOrderInput {
  /** Market slug (Limitless API path identifier). */
  slug: string;
  /** Outcome token id from market metadata. */
  tokenId: string;
  side: 'BUY' | 'SELL';
  /** Price in [0, 1]. Caller responsible for tick alignment. */
  price: number;
  /** Stake in USDC dollars (Base USDC, 6dp). */
  sizeUsdc: number;
  wallet: Wallet;
  /** verifyingContract per market. Falls back to LIMITLESS_DEFAULT_EXCHANGE. */
  verifyingContract?: Hex;
  expirationSecs?: number;
  feeRateBps?: number;
  orderType?: 'GTC' | 'GTD' | 'FOK' | 'FAK';
  ownerId?: number;
  clientOrderId?: string;
  salt?: bigint;
  expirationOverride?: bigint;
  privateKey?: Hex;
  /** Tick-aware contract amount multiple (default 1000 for 0.001-tick
   *  markets). Snaps `takerAmount` (contracts side) DOWN to a multiple
   *  of this so `price × contracts` is always integer in 1e6 USDC. */
  contractMultiple?: bigint;
}

export interface LimitlessOrderStruct {
  salt: bigint;
  maker: Hex;
  signer: Hex;
  taker: Hex;
  tokenId: bigint;
  makerAmount: bigint;
  takerAmount: bigint;
  expiration: bigint;
  nonce: bigint;
  feeRateBps: bigint;
  side: 0 | 1;
  signatureType: 0 | 1 | 2;
}

export interface LimitlessOrderBody {
  order: LimitlessOrderStruct & { signature: Hex | ''; price: number };
  orderType: 'GTC' | 'GTD' | 'FOK' | 'FAK';
  marketSlug: string;
  ownerId?: number;
  clientOrderId?: string;
}

function toScaledWei(amount: number): bigint {
  return BigInt(Math.round(amount * 1_000_000));
}

function freshSalt(): bigint {
  // Phase audit-13 (15.05.2026) — Limitless DB stores salt as
  // Postgres BIGINT (int64). A uint128 random salt overflows with
  // "value '...' is out of range for type bigint". Generate 7 bytes
  // (56 bits, max ~7.2e16) — well inside int64 — and leave a comfortable
  // sign-bit + spread margin. EIP-712 type still uses uint256, but the
  // signed value is the same as the body-serialized integer; server
  // parses + verifies + then writes to DB.
  const bytes = crypto.getRandomValues(new Uint8Array(7));
  let n = 0n;
  for (const b of bytes) n = (n << 8n) | BigInt(b);
  return n;
}

export async function buildLimitlessOrder(
  input: BuildLimitlessOrderInput,
): Promise<BuiltOrder<LimitlessOrderBody>> {
  const {
    slug,
    tokenId,
    side,
    price,
    sizeUsdc,
    wallet,
    verifyingContract,
    // 120s default — cold SOCKS5+TLS handshake + POST round-trip + any
    // server-side queueing routinely measures 1-3s on the first fire of
    // a session. A 60s window cut it too close; if the order isn't
    // matched within ~30s the cold-call to deleteLimOrder also needs
    // expiration headroom. Operator can override via env if tighter
    // semantics are required for a specific deploy.
    expirationSecs = Number(process.env.LIMITLESS_EXPIRATION_SECS ?? '120'),
    // Phase audit-12 (15.05.2026) — server rejects with
    //   "feeRateBps[0] is out of user's band"
    // when this doesn't match the wallet's current rank fee. The rank's
    // feeRateBps is at GET /profiles/{address}.rank.feeRateBps
    // (Bronze=300, higher ranks lower). Default 300 covers a brand-new
    // wallet; operator overrides via LIMITLESS_FEE_RATE_BPS env when
    // their rank advances.
    feeRateBps = Number(process.env.LIMITLESS_FEE_RATE_BPS ?? '300'),
    orderType = 'GTC',
    ownerId,
    clientOrderId,
    privateKey,
  } = input;

  if (side !== 'BUY' && side !== 'SELL') {
    throw new Error(`side must be BUY|SELL, got ${side as string}`);
  }
  if (!(price > 0 && price < 1)) {
    throw new Error(`price out of range: ${price}`);
  }
  if (sizeUsdc < 1.0) {
    throw new Error(`size below Limitless min $1: ${sizeUsdc}`);
  }

  // Phase audit-12 (15.05.2026) — Limitless V2 enforces a tick-amount
  // invariant: `price * contracts_wei` must be an integer in 1e6 USDC
  // units. Server error: `Order amounts tick violation: price(0.56) *
  // contracts(1785714) = 999999.84 is not a whole (integer) number.`
  // Server hint: snap contracts to a multiple of 1000 for 0.001-tick
  // markets. We snap DOWN so we never over-quote stake.
  //
  // Default contract multiple: 1000 (covers 0.001 tickSize, which is the
  // current Limitless default). If a market uses a coarser/finer tick,
  // override via input.contractMultiple.
  const isBuy = side === 'BUY';
  const contractMultiple = input.contractMultiple ?? 1000n;
  const rawContractsWei = toScaledWei(sizeUsdc / price);
  const contractsWei =
    (rawContractsWei / contractMultiple) * contractMultiple; // floor to multiple
  // Recompute USDC side from the snapped contracts so the product is
  // exact: usdcWei = contractsWei * price (in 1e6 USDC units, integer).
  const priceScaledTo1e6 = BigInt(Math.round(price * 1_000_000));
  const usdcWei = (contractsWei * priceScaledTo1e6) / 1_000_000n;

  // Phase 19v23 maker/taker semantics (parity with Polymarket Phase 19v19):
  //   BUY  (side=0): makerAmount=USDC, takerAmount=CTF
  //   SELL (side=1): makerAmount=CTF,  takerAmount=USDC
  const makerAmount = isBuy ? usdcWei : contractsWei;
  const takerAmount = isBuy ? contractsWei : usdcWei;

  const sigType = wallet.signatureType ?? 0;
  if (sigType !== 0 && sigType !== 1 && sigType !== 2) {
    throw new Error(`signatureType=${sigType} invalid (must be 0, 1, or 2)`);
  }

  // Phase audit-11 (15.05.2026) — Limitless V2 server: "Order expiration
  // is not currently supported. Please sign orders without expiration."
  // EIP-712 type still has the `expiration` field (contract requirement),
  // but server accepts 0 as "no expiration". Sign with 0 always; the
  // expirationOverride hatch remains only for tests that exercise the
  // signing path with a non-zero value.
  const expiration =
    input.expirationOverride !== undefined
      ? input.expirationOverride
      : 0n;

  const order: LimitlessOrderStruct = {
    salt: input.salt ?? freshSalt(),
    maker: wallet.ethAddress,
    signer: wallet.ethAddress,
    taker: ZERO_ADDR,
    tokenId: BigInt(tokenId),
    makerAmount,
    takerAmount,
    expiration,
    nonce: 0n,
    feeRateBps: BigInt(feeRateBps),
    side: isBuy ? 0 : 1,
    signatureType: sigType,
  };

  let signature: Hex | '' = '';
  let signedOk = false;
  if (privateKey && wallet.canSign) {
    const account = privateKeyToAccount(privateKey);
    const domain = {
      name: LIMITLESS_DOMAIN_NAME,
      version: LIMITLESS_DOMAIN_VERSION,
      chainId: LIMITLESS_CHAIN_ID,
      verifyingContract: (verifyingContract ?? LIMITLESS_DEFAULT_EXCHANGE) as Hex,
    } as const;
    signature = await account.signTypedData({
      domain,
      types: LIMITLESS_ORDER_TYPES,
      primaryType: 'Order',
      message: order,
    });
    signedOk = true;
  }

  // Phase audit-10 (15.05.2026) — `price` lives INSIDE the order
  // object, not at the body top-level. Earlier audit-9 placed it at
  // the body level based on the bare error string "GTC order must
  // have a price"; the canonical Limitless docs put it inside `order`
  // alongside makerAmount/takerAmount.
  const body: LimitlessOrderBody = {
    order: { ...order, signature, price },
    orderType,
    marketSlug: slug,
    ...(ownerId !== undefined ? { ownerId } : {}),
    ...(clientOrderId !== undefined ? { clientOrderId } : {}),
  };

  // Deterministic JSON of unsigned order — Python parity.
  const signPayload = canonicalJsonBytes(order);

  return {
    platform: 'limitless',
    body,
    wouldPostUrl: LIMITLESS_ORDER_URL,
    signed: signedOk,
    expectedPrice: price,
    expectedSizeUsdc: sizeUsdc,
    signPayload,
    order,
  };
}

function canonicalJsonBytes(order: LimitlessOrderStruct): Uint8Array {
  const sorted: Record<string, string> = {};
  for (const k of Object.keys(order).sort()) {
    const v = (order as unknown as Record<string, bigint | string>)[k];
    if (v === undefined) continue;
    sorted[k] = typeof v === 'bigint' ? v.toString() : (v as string);
  }
  return new TextEncoder().encode(JSON.stringify(sorted));
}

export function payloadDigest(payload: Uint8Array): Hex {
  return keccak256(payload);
}
