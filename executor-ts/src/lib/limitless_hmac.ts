/**
 * Limitless Exchange HMAC-SHA256 request signing.
 *
 * Phase TS-5f (14.05.2026) — discovered via empirical key issue + live
 * docs check that Limitless V2 uses HMAC, not bearer X-API-Key. Old
 * code paths attached `X-API-Key: <key>` which 401s on every
 * authenticated endpoint (POST /orders, DELETE /orders/{id}, GET
 * /positions, subscribe_order_events on WS).
 *
 * See .claude/skills/limitless-hmac-auth/SKILL.md for the contract,
 * common failure modes, and integration map.
 *
 * Canonical message (newline-separated):
 *
 *     <ISO-8601 timestamp>\n<METHOD>\n<path?query>\n<body>
 *
 * Signature:
 *
 *     base64(HMAC-SHA256(base64.decode(secret), message))
 */
import { createHmac } from 'crypto';

export interface LmtsSignedHeaders {
  'lmts-api-key': string;
  'lmts-timestamp': string;
  'lmts-signature': string;
}

/**
 * Sign a Limitless API request with HMAC-SHA256 and return the 3
 * headers the server expects.
 *
 * IMPORTANT: `body` must be the EXACT JSON string that will be sent
 * on the wire. If you stringify with different key ordering or
 * whitespace between sign-time and send-time, the signature breaks.
 * For GET requests, pass `''`.
 *
 * `path` must include query string. Example: '/orders?market=btc-100k'.
 */
export function signLmtsRequest(
  tokenId: string,
  secret: string,
  method: string,
  path: string,
  body: string = '',
): LmtsSignedHeaders {
  const ts = new Date().toISOString();
  const msg = `${ts}\n${method}\n${path}\n${body}`;
  const sig = createHmac('sha256', Buffer.from(secret, 'base64'))
    .update(msg)
    .digest('base64');
  return {
    'lmts-api-key': tokenId,
    'lmts-timestamp': ts,
    'lmts-signature': sig,
  };
}

/**
 * Convenience: extract just the path+query from a full URL for the
 * HMAC message. Server signs over the path portion only, not the
 * scheme/host.
 */
export function pathForSigning(url: string): string {
  // Parse via URL constructor — fails on relative URLs, which is fine
  // because callers always pass absolute URLs (POST endpoint URLs are
  // imported from types/eip712.ts as constants).
  const u = new URL(url);
  return `${u.pathname}${u.search}`;
}
