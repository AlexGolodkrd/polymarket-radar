/**
 * Tests for src/wallets/signers.ts — module-scoped secret registry.
 *
 * Key invariants:
 *   - keys never appear on the public API surface (we expose count and
 *     a list of botIds, never the bytes themselves)
 *   - registerSigner rejects malformed keys (0x + 64 hex chars required)
 *   - getSignerKey returns undefined for unregistered bots (so atomic.ts
 *     can fail-soft: build unsigned vs throw)
 *   - _resetRegistry clears all state (test isolation)
 */
import { describe, expect, it, beforeEach } from 'vitest';
import {
  registerSigner,
  getSignerKey,
  hasSigner,
  registeredCount,
  registeredBotIds,
  _resetRegistry,
} from '../../src/wallets/signers.js';

const goodKey =
  '0x1111111111111111111111111111111111111111111111111111111111111111' as const;
const anotherKey =
  '0x2222222222222222222222222222222222222222222222222222222222222222' as const;

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

  it('rejects key without 0x prefix', () => {
    expect(() =>
      registerSigner(
        'bot1',
        '1111111111111111111111111111111111111111111111111111111111111111' as `0x${string}`,
      ),
    ).toThrow(/invalid private key/);
    expect(registeredCount()).toBe(0);
  });

  it('rejects key with wrong length', () => {
    expect(() => registerSigner('bot1', '0x1234' as `0x${string}`)).toThrow(
      /invalid private key/,
    );
    expect(() =>
      registerSigner(
        'bot1',
        '0x111111111111111111111111111111111111111111111111111111111111111' as `0x${string}`,
      ),
    ).toThrow(/invalid private key/);
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
    // Each entry should be a short botId, not the full key.
    for (const id of ids) {
      expect(id.length).toBeLessThan(20);
      expect(id).not.toContain(goodKey);
      expect(id).not.toContain(anotherKey);
    }
  });
});
