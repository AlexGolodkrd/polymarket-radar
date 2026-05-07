/**
 * EIP-712 typed-data shapes used by the executor builders.
 *
 * Each platform has its own (domain, primaryType, types) triple. We
 * keep them as exported constants so builders, signers, and tests all
 * reference the SAME source of truth — preventing the kind of EIP-712
 * regressions Python had in v17 / v19 / v23 / v24.
 */

/**
 * Polymarket CTF Exchange V2 — used by both standard and negRisk markets.
 * Differentiation between the two is encoded in `verifyingContract`,
 * which the server reads off the signed Order's domain. Hence we expose
 * both domain constants below; the builder picks one based on market type.
 */
export const POLY_DOMAIN_STANDARD = {
  name: 'Polymarket CTF Exchange',
  version: '2',
  chainId: 137,
  verifyingContract: '0xE111180000d2663C0091e4f400237545B87B996B',
} as const;

export const POLY_DOMAIN_NEGRISK = {
  name: 'Polymarket Neg Risk CTF Exchange',
  version: '2',
  chainId: 137,
  verifyingContract: '0xe2222d279d744050d28e00520010520000310F59',
} as const;

/**
 * V2 Order struct. Note the deliberate differences from V1 (covered in
 * Python `builders.py` Phase 9m comments):
 *   - dropped: expiration, nonce, feeRateBps, taker (always 0x0)
 *   - kept:    salt, maker, signer, tokenId, makerAmount, takerAmount,
 *              side, signatureType, timestamp(ms), metadata, builder
 *
 * `metadata` and `builder` are bytes32; we use zero by default
 * (no app metadata, no builder attribution).
 */
export const POLY_ORDER_TYPES_V2 = {
  Order: [
    { name: 'salt', type: 'uint256' },
    { name: 'maker', type: 'address' },
    { name: 'signer', type: 'address' },
    { name: 'tokenId', type: 'uint256' },
    { name: 'makerAmount', type: 'uint256' },
    { name: 'takerAmount', type: 'uint256' },
    { name: 'side', type: 'uint8' },
    { name: 'signatureType', type: 'uint8' },
    { name: 'timestamp', type: 'uint256' },
    { name: 'metadata', type: 'bytes32' },
    { name: 'builder', type: 'bytes32' },
  ],
} as const;

export const ZERO_BYTES32 =
  '0x0000000000000000000000000000000000000000000000000000000000000000' as const;

export const POLY_API_BASE = 'https://clob.polymarket.com';
export const POLY_CLOB_URL = `${POLY_API_BASE}/order`;

/**
 * Limitless CLOB (Base mainnet, chainId 8453). Domain name fixed,
 * verifyingContract comes from market metadata (different per venue).
 */
export const LIMITLESS_DOMAIN_NAME = 'Limitless CTF Exchange';
export const LIMITLESS_DOMAIN_VERSION = '1';
export const LIMITLESS_CHAIN_ID = 8453;
export const LIMITLESS_DEFAULT_EXCHANGE =
  '0xC5d563A36AE78145C45a50134d48A1215220f80a' as const;

export const LIMITLESS_ORDER_TYPES = {
  Order: [
    { name: 'salt', type: 'uint256' },
    { name: 'maker', type: 'address' },
    { name: 'signer', type: 'address' },
    { name: 'taker', type: 'address' },
    { name: 'tokenId', type: 'uint256' },
    { name: 'makerAmount', type: 'uint256' },
    { name: 'takerAmount', type: 'uint256' },
    { name: 'expiration', type: 'uint256' },
    { name: 'nonce', type: 'uint256' },
    { name: 'feeRateBps', type: 'uint256' },
    { name: 'side', type: 'uint8' },
    { name: 'signatureType', type: 'uint8' },
  ],
} as const;

export const LIMITLESS_API_BASE = 'https://api.limitless.exchange';
export const LIMITLESS_ORDER_URL = `${LIMITLESS_API_BASE}/orders`;

/**
 * SX Bet OrderFill — chainId 4162 (SX Network mainnet), version 6.0
 * domain. `worstOdds` is the slippage cap (1e20-scaled like
 * percentageOdds). Phase 19v19 fix: must be the CAP, not observed worst
 * matched-maker price (forward-looking, not backward).
 */
export const SX_DOMAIN = {
  name: 'SX Bet Order Fill',
  version: '6.0',
  chainId: 4162,
  verifyingContract: '0xBe9F69dab98C1Ddee5BF31a9b1f5DBe88869B5d4',
} as const;

export const SX_FILL_TYPES = {
  Details: [
    { name: 'action', type: 'string' },
    { name: 'market', type: 'string' },
    { name: 'betting', type: 'string' },
    { name: 'stake', type: 'string' },
    { name: 'worstOdds', type: 'string' },
    { name: 'executor', type: 'address' },
  ],
} as const;

export const SX_FILL_URL = 'https://api.sx.bet/orders/fill';
export const SX_USDC_DECIMALS = 6;
