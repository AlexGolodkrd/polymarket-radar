/**
 * Polymarket CLOB V2 L2 HMAC header builder.
 *
 * Port of `Scripts/executor/builders.py:build_poly_hmac_headers` (Phase 9f).
 * Produces the 5 headers required by every L2-protected endpoint:
 *   POLY_ADDRESS    — EIP-55 checksum wallet address
 *   POLY_TIMESTAMP  — unix seconds (must be within ±60s of server clock)
 *   POLY_API_KEY    — operator-provisioned UUID
 *   POLY_PASSPHRASE — operator-provisioned secret
 *   POLY_SIGNATURE  — base64-url(HMAC-SHA256(secret, prehash))
 *
 *   prehash = timestamp + method.upper() + path + (body || '')
 *
 * py-clob-client convention:
 *   - api_secret is base64-URL-encoded (NOT standard base64) — we decode
 *     it with the URL-safe alphabet ('-_' instead of '+/')
 *   - signature output is also URL-safe base64
 *
 * Endpoints requiring L2 auth (per docs as of 2026):
 *   POST   /order                 — submit signed order (V2 actually uses
 *                                   ECDSA recovery from order.signature,
 *                                   but the server still inspects these
 *                                   headers as a defense-in-depth check)
 *   DELETE /order/{orderID}       — cancel a live order (REQUIRED)
 *   DELETE /orders                — cancel all (REQUIRED)
 *   GET    /balance-allowance     — read on-chain balance + allowances
 *   POST   /orders/scoring        — reward eligibility check
 *
 * Phase TS-6 (11.05.2026) — wires the runtime cancel path in
 * atomic.fireLeg's timeout branch (without L2 HMAC the order sits on
 * Poly's book until natural-expire, leaving residual exposure).
 */
import { createHmac } from 'node:crypto';
import { getAddress } from 'viem';

export interface PolyL2Creds {
  apiKey: string;
  apiSecret: string;
  passphrase: string;
}

export interface BuildL2HeadersInput {
  method: string;
  path: string;
  /** Stringified JSON body for POST, empty string for GET/DELETE. */
  body: string;
  apiKey: string;
  apiSecret: string;
  passphrase: string;
  ethAddress: string;
  /** Optional override for testing — defaults to current unix seconds. */
  ts?: number;
}

/**
 * Build the 6-header L2 auth bundle. Content-Type is included for
 * convenience even though it's not strictly part of the L2 protocol —
 * callers post JSON bodies on every endpoint that requires these headers.
 *
 * Throws nothing — falls back gracefully on malformed inputs:
 *   - bad base64 secret → use raw UTF-8 bytes (server will reject, fail-loud)
 *   - non-EIP-55 address → use raw string (Polymarket may or may not accept)
 */
export function buildPolyL2Headers(input: BuildL2HeadersInput): Record<string, string> {
  const ts = input.ts ?? Math.floor(Date.now() / 1000);
  const prehash = `${ts}${input.method.toUpperCase()}${input.path}${input.body || ''}`;

  // Decode the URL-safe base64 secret. Buffer.from with 'base64' accepts
  // both '+/' and '-_' alphabets in Node 18+. Pad to multiple of 4 to
  // handle inputs missing trailing '='.
  let secretBytes: Buffer;
  try {
    const padded = padBase64(input.apiSecret);
    secretBytes = Buffer.from(padded, 'base64');
    // Buffer.from with bad base64 returns an empty buffer instead of
    // throwing — guard against that by checking length is plausible.
    if (secretBytes.length === 0 && input.apiSecret.length > 0) {
      secretBytes = Buffer.from(input.apiSecret, 'utf-8');
    }
  } catch {
    secretBytes = Buffer.from(input.apiSecret, 'utf-8');
  }

  const sigBytes = createHmac('sha256', secretBytes)
    .update(prehash, 'utf-8')
    .digest();

  // Output is URL-safe base64 (Python's base64.urlsafe_b64encode).
  const sigB64 = sigBytes
    .toString('base64')
    .replace(/\+/g, '-')
    .replace(/\//g, '_');

  // EIP-55 checksum normalization — Phase 19v13 parity.
  let addr = input.ethAddress;
  try {
    addr = getAddress(input.ethAddress);
  } catch {
    // Keep raw — server may still accept lowercase, or fail-loud which
    // is the desired fail-fast for misconfiguration.
  }

  return {
    POLY_ADDRESS: addr,
    POLY_TIMESTAMP: String(ts),
    POLY_API_KEY: input.apiKey,
    POLY_PASSPHRASE: input.passphrase,
    POLY_SIGNATURE: sigB64,
    'Content-Type': 'application/json',
  };
}

/**
 * Pad a base64 string to a multiple of 4 characters by appending '='.
 * Buffer.from('base64') tolerates missing padding in Node ≥18 but
 * earlier behaviors varied; defensive padding keeps the prehash
 * decoding deterministic regardless of runtime version.
 */
function padBase64(s: string): string {
  const remainder = s.length % 4;
  if (remainder === 0) return s;
  return s + '='.repeat(4 - remainder);
}
