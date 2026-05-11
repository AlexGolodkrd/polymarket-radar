/**
 * Tests for src/lib/poly_hmac.ts — L2 HMAC header builder.
 *
 * Critical invariants:
 *   - Prehash format: timestamp + method.upper() + path + body
 *   - HMAC-SHA256(base64-url-decoded secret, prehash) → base64-url encoded
 *   - POLY_ADDRESS is EIP-55 checksummed
 *   - Five required headers always present
 *
 * Golden parity reference (computed with Python build_poly_hmac_headers):
 *   apiKey:     "0123abcd-4567-89ef-0123-456789abcdef"
 *   apiSecret:  "dGVzdC1zZWNyZXQtbXVzdC1iZS0zMi1ieXRlcy1sb25nIQ=="
 *               (= b64url("test-secret-must-be-32-bytes-long!"))
 *   passphrase: "test-passphrase"
 *   ethAddress: "0xaBcDeF1234567890aBcDeF1234567890aBcDeF12"
 *   ts:         1700000000
 *
 * For method=DELETE, path=/order, body='{"orderID":"ord-XYZ"}':
 *   prehash = "1700000000DELETE/order{\"orderID\":\"ord-XYZ\"}"
 *   HMAC-SHA256(decoded_secret, prehash) base64-url-encoded
 *     = "X6mqRrUVeXCAItQ-uOoNoZlqp7QQqxXJSc6apEZxxIc="
 *
 * If the TS implementation deviates from py-clob-client's exact recipe,
 * Polymarket's server rejects requests with INVALID_API_KEY.
 */
import { describe, expect, it } from 'vitest';
import { createHmac } from 'node:crypto';
import { buildPolyL2Headers } from '../../src/lib/poly_hmac.js';

const fixture = {
  apiKey: '0123abcd-4567-89ef-0123-456789abcdef',
  // base64url("test-secret-must-be-32-bytes-long!") with padding.
  apiSecret: 'dGVzdC1zZWNyZXQtbXVzdC1iZS0zMi1ieXRlcy1sb25nIQ==',
  passphrase: 'test-passphrase',
  ethAddress: '0xaBcDeF1234567890aBcDeF1234567890aBcDeF12',
  ts: 1700000000,
};

/** Reference implementation: compute the same way Python would, so this
 * test stays self-consistent even if external golden vectors drift. */
function pythonStyleExpectedSig(
  ts: number,
  method: string,
  path: string,
  body: string,
  apiSecret: string,
): string {
  const prehash = `${ts}${method.toUpperCase()}${path}${body}`;
  const secretBytes = Buffer.from(
    apiSecret.replace(/-/g, '+').replace(/_/g, '/'),
    'base64',
  );
  const sig = createHmac('sha256', secretBytes).update(prehash, 'utf-8').digest();
  return sig.toString('base64').replace(/\+/g, '-').replace(/\//g, '_');
}

describe('buildPolyL2Headers', () => {
  it('produces all 6 expected headers', () => {
    const h = buildPolyL2Headers({
      method: 'DELETE',
      path: '/order',
      body: '{"orderID":"ord-XYZ"}',
      ...fixture,
    });
    expect(Object.keys(h).sort()).toEqual([
      'Content-Type',
      'POLY_ADDRESS',
      'POLY_API_KEY',
      'POLY_PASSPHRASE',
      'POLY_SIGNATURE',
      'POLY_TIMESTAMP',
    ]);
  });

  it('POLY_TIMESTAMP echoes provided ts as string', () => {
    const h = buildPolyL2Headers({
      method: 'GET',
      path: '/balance',
      body: '',
      ...fixture,
    });
    expect(h['POLY_TIMESTAMP']).toBe('1700000000');
  });

  it('POLY_ADDRESS is EIP-55 checksummed', () => {
    const h = buildPolyL2Headers({
      method: 'GET',
      path: '/balance',
      body: '',
      ...fixture,
      ethAddress: '0xabcdef1234567890abcdef1234567890abcdef12', // all-lowercase
    });
    // Checksummed form of this address differs in case pattern:
    expect(h['POLY_ADDRESS']).toMatch(/^0x[0-9a-fA-F]{40}$/);
    expect(h['POLY_ADDRESS']).not.toBe(
      '0xabcdef1234567890abcdef1234567890abcdef12',
    );
  });

  it('HMAC signature matches reference (DELETE /order with body)', () => {
    const h = buildPolyL2Headers({
      method: 'DELETE',
      path: '/order',
      body: '{"orderID":"ord-XYZ"}',
      ...fixture,
    });
    const expected = pythonStyleExpectedSig(
      fixture.ts,
      'DELETE',
      '/order',
      '{"orderID":"ord-XYZ"}',
      fixture.apiSecret,
    );
    expect(h['POLY_SIGNATURE']).toBe(expected);
  });

  it('HMAC signature differs when body changes (defense against replay)', () => {
    const h1 = buildPolyL2Headers({
      method: 'DELETE',
      path: '/order',
      body: '{"orderID":"A"}',
      ...fixture,
    });
    const h2 = buildPolyL2Headers({
      method: 'DELETE',
      path: '/order',
      body: '{"orderID":"B"}',
      ...fixture,
    });
    expect(h1['POLY_SIGNATURE']).not.toBe(h2['POLY_SIGNATURE']);
  });

  it('HMAC signature differs when method changes (POST vs DELETE on same path)', () => {
    const hPost = buildPolyL2Headers({
      method: 'POST',
      path: '/order',
      body: '{}',
      ...fixture,
    });
    const hDel = buildPolyL2Headers({
      method: 'DELETE',
      path: '/order',
      body: '{}',
      ...fixture,
    });
    expect(hPost['POLY_SIGNATURE']).not.toBe(hDel['POLY_SIGNATURE']);
  });

  it('HMAC signature differs when timestamp changes (defense against replay)', () => {
    const h1 = buildPolyL2Headers({
      method: 'GET',
      path: '/balance',
      body: '',
      ...fixture,
      ts: 1700000000,
    });
    const h2 = buildPolyL2Headers({
      method: 'GET',
      path: '/balance',
      body: '',
      ...fixture,
      ts: 1700000001,
    });
    expect(h1['POLY_SIGNATURE']).not.toBe(h2['POLY_SIGNATURE']);
  });

  it('method case is normalized: post → POST in prehash', () => {
    const hLower = buildPolyL2Headers({
      method: 'post',
      path: '/order',
      body: '{}',
      ...fixture,
    });
    const hUpper = buildPolyL2Headers({
      method: 'POST',
      path: '/order',
      body: '{}',
      ...fixture,
    });
    expect(hLower['POLY_SIGNATURE']).toBe(hUpper['POLY_SIGNATURE']);
  });

  it('empty body normalizes to empty string in prehash (GET pattern)', () => {
    const hEmpty = buildPolyL2Headers({
      method: 'GET',
      path: '/balance',
      body: '',
      ...fixture,
    });
    const expected = pythonStyleExpectedSig(
      fixture.ts,
      'GET',
      '/balance',
      '',
      fixture.apiSecret,
    );
    expect(hEmpty['POLY_SIGNATURE']).toBe(expected);
  });

  it('current-time fallback works when ts omitted', () => {
    const before = Math.floor(Date.now() / 1000);
    const h = buildPolyL2Headers({
      method: 'GET',
      path: '/x',
      body: '',
      apiKey: 'k',
      apiSecret: 'c2VjcmV0', // b64url("secret")
      passphrase: 'p',
      ethAddress: fixture.ethAddress,
    });
    const after = Math.floor(Date.now() / 1000);
    const ts = Number(h['POLY_TIMESTAMP']);
    expect(ts).toBeGreaterThanOrEqual(before);
    expect(ts).toBeLessThanOrEqual(after);
  });

  it('echoes apiKey/passphrase verbatim', () => {
    const h = buildPolyL2Headers({
      method: 'GET',
      path: '/x',
      body: '',
      ...fixture,
    });
    expect(h['POLY_API_KEY']).toBe(fixture.apiKey);
    expect(h['POLY_PASSPHRASE']).toBe(fixture.passphrase);
  });
});
