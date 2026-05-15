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
  order: LimitlessOrderStruct & { signature: Hex | '' };
  orderType: 'GTC' | 'GTD' | 'FOK' | 'FAK';
  marketSlug: string;
  ownerId?: number;
  clientOrderId?: string;
}

function toScaledWei(amount: number): bigint {
  return BigInt(Math.round(amount * 1_000_000));
}

function freshSalt(): bigint {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
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
    feeRateBps = 0,
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

  const usdcWei = toScaledWei(sizeUsdc);
  const contracts = sizeUsdc / price;
  const contractsWei = toScaledWei(contracts);

  // Phase 19v23 maker/taker semantics (parity with Polymarket Phase 19v19):
  //   BUY  (side=0): makerAmount=USDC, takerAmount=CTF
  //   SELL (side=1): makerAmount=CTF,  takerAmount=USDC
  const isBuy = side === 'BUY';
  const makerAmount = isBuy ? usdcWei : contractsWei;
  const takerAmount = isBuy ? contractsWei : usdcWei;

  const sigType = wallet.signatureType ?? 0;
  if (sigType !== 0 && sigType !== 1 && sigType !== 2) {
    throw new Error(`signatureType=${sigType} invalid (must be 0, 1, or 2)`);
  }

  const expiration =
    input.expirationOverride !== undefined
      ? input.expirationOverride
      : BigInt(Math.floor(Date.now() / 1000) + expirationSecs);

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

  const body: LimitlessOrderBody = {
    order: { ...order, signature },
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
