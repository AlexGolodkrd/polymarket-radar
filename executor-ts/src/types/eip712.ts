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
 * SX Bet OrderFill v2 — chainId 4162 (SX Network mainnet), version 6.0.
 *
 * Phase audit-5 (15.05.2026) — protocol changed shape entirely. The v1
 * struct (signed Details { action, market, betting, stake, worstOdds,
 * executor }, body with orderHashes + takerAmounts + expiry) was
 * deprecated. The v2 protocol moves the maker-matching to the server
 * (taker no longer chooses orderHashes) and re-shapes both the body
 * and the EIP-712 message:
 *
 *   - Domain `name` is now "SX Bet" (was "SX Bet Order Fill")
 *   - `verifyingContract` is now the EIP712FillHasher contract
 *     (`0x845a2Da2D70fEDe8474b1C8518200798c60aC364`) — read from
 *     `GET /metadata` as `EIP712FillHasher`. The old address
 *     `0xBE9F69DaB...` is dead.
 *   - The signed message is `Details` with a nested `FillObject`
 *     containing the real fill parameters; all Details-level
 *     human-readable fields use literal "N/A" placeholders.
 *   - URL is `/orders/fill/v2` (NOT `/v1/orders/fill/v2`).
 *
 * Live-verified 2026-05-15 against `docs.sx.bet/developers/filling-orders.md`.
 */
export const SX_DOMAIN = {
  name: 'SX Bet',
  version: '6.0',
  chainId: 4162,
  verifyingContract: '0x845a2Da2D70fEDe8474b1C8518200798c60aC364',
} as const;

export const SX_FILL_TYPES = {
  Details: [
    { name: 'action', type: 'string' },
    { name: 'market', type: 'string' },
    { name: 'betting', type: 'string' },
    { name: 'stake', type: 'string' },
    { name: 'worstOdds', type: 'string' },
    { name: 'worstReturning', type: 'string' },
    { name: 'fills', type: 'FillObject' },
  ],
  FillObject: [
    { name: 'stakeWei', type: 'string' },
    { name: 'marketHash', type: 'string' },
    { name: 'baseToken', type: 'string' },
    { name: 'desiredOdds', type: 'string' },
    { name: 'oddsSlippage', type: 'uint256' },
    { name: 'isTakerBettingOutcomeOne', type: 'bool' },
    { name: 'fillSalt', type: 'uint256' },
    { name: 'beneficiary', type: 'address' },
    { name: 'beneficiaryType', type: 'uint8' },
    { name: 'cashOutTarget', type: 'bytes32' },
  ],
} as const;

export const SX_FILL_URL = 'https://api.sx.bet/orders/fill/v2';
export const SX_USDC_DECIMALS = 6;
// USDC on SX Network chainId 4162 (per `GET /metadata.addresses.4162.USDC`).
// This is the `baseToken` field in every USDC-denominated fill body.
export const SX_USDC_BASE_TOKEN =
  '0x6629Ce1Cf35Cc1329ebB4F63202F3f197b3F050B' as const;
