/**
 * Wallet types — mirror of Python `WalletStub` / `Wallet`.
 *
 * Phase 19v24 (05.05.2026) introduced V2 proxy-wallet topology in the
 * Python radar; we keep the same three-mode model here so the TS
 * executor can sign for EOA / POLY_PROXY / POLY_GNOSIS_SAFE wallets
 * without surprises.
 */

import type { Hex } from 'viem';

/**
 * Polymarket V2 signature type.
 *
 * - `0` (EOA)             — `maker = signer`, no separate funder.
 * - `1` (POLY_PROXY)      — Magic-derived proxy contract holds pUSD;
 *                           `maker = funder`, `signer = eth_address`.
 * - `2` (POLY_GNOSIS_SAFE)— Gnosis Safe holds pUSD;
 *                           `maker = funder` (safe addr), `signer = eth_address`.
 *
 * Full decision tree in `polymarket-v2-auth` skill (Python repo).
 */
export type PolySignatureType = 0 | 1 | 2;

/**
 * Per-bot wallet metadata. Loaded by stores (LocalEnvStore, AwsSecretsStore,
 * etc.) and consumed by builders. Private keys MUST stay inside
 * `signMessage` / `signTypedData` closures — never embed them on the
 * fired-order body.
 */
export interface Wallet {
  /** "bot1".."bot6" — coordinator uses this for round-robin assignment. */
  botId: string;

  /** EIP-55 checksum address that holds the private key (the SIGNER). */
  ethAddress: Hex;

  /** True iff a private-key signing function is wired in (false in dry-run). */
  canSign: boolean;

  /** Polymarket-specific: which signature topology the server should expect. */
  signatureType: PolySignatureType;

  /**
   * Address that actually holds pUSD / outcome tokens. For type 0 this
   * collapses to `ethAddress`; for types 1/2 it's the proxy/safe addr.
   * Used as `maker` in EIP-712 V2 orders and for balance/allowance reads.
   */
  funder?: Hex;

  /**
   * L2 API credentials for Polymarket REST (HMAC headers on POST /order
   * and DELETE cancels). Derived once from L1 EIP-712 signature, then
   * cached per-bot.
   */
  polyApiKey?: string;
  polySecret?: string;
  polyPassphrase?: string;

  /**
   * Limitless token ID (formerly "X-API-Key", but in V2 it's the public
   * identifier sent as `lmts-api-key` HMAC header). Kept the field name
   * for backwards compatibility with code that reads it.
   */
  limitlessApiKey?: string;
  /**
   * Phase TS-5f.4 (14.05.2026) — Limitless HMAC secret. Base64-encoded
   * raw bytes used as the HMAC-SHA256 key for signing REST requests and
   * the WS handshake. Required for Trading-scope tokens in real-mode;
   * legacy bearer (just limitlessApiKey alone) 401s.
   */
  limitlessApiSecret?: string;
}

/**
 * Helper: returns the address that should appear in the `maker` field
 * of a Polymarket V2 order (= funder for proxy types, = signer for EOA).
 */
export function effectiveFunder(w: Wallet): Hex {
  return w.funder ?? w.ethAddress;
}

/**
 * Helper: true iff the wallet uses a separate funder (= proxy or safe).
 * Mirrors Python `Wallet.is_proxy`.
 */
export function isProxy(w: Wallet): boolean {
  if (w.signatureType === 1 || w.signatureType === 2) return true;
  if (w.funder && w.funder.toLowerCase() !== w.ethAddress.toLowerCase()) return true;
  return false;
}
