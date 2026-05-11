/**
 * Tests for src/wallets/signers.ts — module-scoped secret registry +
 * private-key normalization.
 *
 * Phase fix-signer (11.05.2026): registerSigner now normalizes input
 * (strip whitespace, lowercase, auto-0x prefix) before validation.
 * This lets operators paste env values in less-strict formats without
 * silently dropping the signer.
 *
 * Key invariants:
 *   - keys never appear on the public API surface
 *   - registerSigner accepts any case + optional 0x prefix
 *   - registerSigner rejects with SPECIFIC error messages (length, hex)
 *   - getSignerKey returns undefined for unregistered bots
 *   - _resetRegistry clears all state (test isolation)
 */
import { describe, expect, it, beforeEach } from 'vitest';
import {
  registerSigner,
  getSignerKey,
  hasSigner,
  registeredCount,
  registeredBotIds,
  normalizePrivateKey,
  _resetRegistry,
} from '../../src/wallets/signers.js';

const goodKey =
  '0x1111111111111111111111111111111111111111111111111111111111111111' as const;
const anotherKey =
  '0x2222222222222222222222222222222222222222222222222222222222222222' as const;

describe('normalizePrivateKey', () => {
  it('canonical form (0x + lowercase) passes through unchanged', () => {
    expect(normalizePrivateKey(goodKey)).toBe(goodKey);
  });

  it('strips leading + trailing whitespace', () => {
    expect(normalizePrivateKey(`  ${goodKey}\n`)).toBe(goodKey);
    expect(normalizePrivateKey(`\t${goodKey}  `)).toBe(goodKey);
  });

  it('lowercases uppercase hex', () => {
    expect(
      normalizePrivateKey(
        '0xAAAA111111111111111111111111111111111111111111111111111111111111',
      ),
    ).toBe(
      '0xaaaa111111111111111111111111111111111111111111111111111111111111',
    );
  });

  it('adds 0x prefix when missing', () => {
    expect(
      normalizePrivateKey(
        '1111111111111111111111111111111111111111111111111111111111111111',
      ),
    ).toBe(goodKey);
  });

  it('normalizes "0X" uppercase prefix to "0x"', () => {
    expect(
      normalizePrivateKey(
        '0X1111111111111111111111111111111111111111111111111111111111111111',
      ),
    ).toBe(goodKey);
  });

  it('strips internal whitespace too (defensive)', () => {
    expect(
      normalizePrivateKey(
        '0x 1111 1111 1111 1111 1111 1111 1111 1111 1111 1111 1111 1111 1111 1111 1111 1111',
      ),
    ).toBe(goodKey);
  });
});

describe('signers registry', () => {
  beforeEach(() => {
    _resetRegistry();
  });

  it('initially empty', () => {
    expect(registeredCount()).toBe(0);
    expect(registeredBotIds()).toEqual([]);
    expect(getSignerKey('bot1')).toBeUndefined();
    expect(hasSigner('bot1')).toBe(false);
  });

  it('registerSigner stores key, retrievable via getSignerKey', () => {
    registerSigner('bot1', goodKey);
    expect(getSignerKey('bot1')).toBe(goodKey);
    expect(hasSigner('bot1')).toBe(true);
    expect(registeredCount()).toBe(1);
    expect(registeredBotIds()).toEqual(['bot1']);
  });

  it('multiple bots independent', () => {
    registerSigner('bot1', goodKey);
    registerSigner('bot3', anotherKey);
    expect(getSignerKey('bot1')).toBe(goodKey);
    expect(getSignerKey('bot3')).toBe(anotherKey);
    expect(getSignerKey('bot2')).toBeUndefined();
    expect(registeredCount()).toBe(2);
    expect(registeredBotIds().sort()).toEqual(['bot1', 'bot3']);
  });

  it('re-registering same botId overwrites', () => {
    registerSigner('bot1', goodKey);
    registerSigner('bot1', anotherKey);
    expect(getSignerKey('bot1')).toBe(anotherKey);
    expect(registeredCount()).toBe(1);
  });

  it('NORMALIZES key without 0x prefix (was previously rejected)', () => {
    // Phase fix-signer — operators sometimes paste bare 64-hex without
    // the 0x prefix. Old code threw; new code auto-prefixes.
    registerSigner(
      'bot1',
      '1111111111111111111111111111111111111111111111111111111111111111' as `0x${string}`,
    );
    expect(getSignerKey('bot1')).toBe(goodKey);
  });

  it('NORMALIZES uppercase hex (was previously rejected)', () => {
    registerSigner(
      'bot1',
      '0xAAAA111111111111111111111111111111111111111111111111111111111111' as `0x${string}`,
    );
    expect(getSignerKey('bot1')).toBe(
      '0xaaaa111111111111111111111111111111111111111111111111111111111111',
    );
  });

  it('NORMALIZES whitespace-padded key (env-paste artifact)', () => {
    registerSigner(
      'bot1',
      `  ${goodKey}\n` as `0x${string}`,
    );
    expect(getSignerKey('bot1')).toBe(goodKey);
  });

  it('rejects key too short with specific error message', () => {
    expect(() => registerSigner('bot1', '0x1234' as `0x${string}`)).toThrow(
      /private key has \d+ hex chars/,
    );
    expect(registeredCount()).toBe(0);
  });

  it('rejects key too long with specific error', () => {
    const tooLong = '0x' + '1'.repeat(65);
    expect(() => registerSigner('bot1', tooLong as `0x${string}`)).toThrow(
      /private key has \d+ hex chars/,
    );
  });

  it('rejects key with non-hex characters', () => {
    const withNonHex = '0x' + 'z'.repeat(64);
    expect(() => registerSigner('bot1', withNonHex as `0x${string}`)).toThrow(
      /non-hex/,
    );
  });

  it('error mentions specific bot ID so operator can find culprit', () => {
    expect(() => registerSigner('bot3', '0xshort' as `0x${string}`)).toThrow(
      /bot3/,
    );
  });

  it('_resetRegistry clears all state', () => {
    registerSigner('bot1', goodKey);
    registerSigner('bot2', anotherKey);
    expect(registeredCount()).toBe(2);
    _resetRegistry();
    expect(registeredCount()).toBe(0);
    expect(getSignerKey('bot1')).toBeUndefined();
  });

  it('registeredBotIds list does not include key material', () => {
    registerSigner('bot1', goodKey);
    registerSigner('bot2', anotherKey);
    const ids = registeredBotIds();
    for (const id of ids) {
      expect(id.length).toBeLessThan(20);
      expect(id).not.toContain(goodKey);
      expect(id).not.toContain(anotherKey);
    }
  });
});
