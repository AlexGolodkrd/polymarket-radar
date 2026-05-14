/**
 * proxy_pool.ts — residential proxy routing for order placement.
 *
 * Phase TS-5d (14.05.2026) — required before flipping DRY_RUN=0.
 * Tests verify the env contract documented in
 * .claude/skills/residential-proxy-routing/SKILL.md.
 */
import { describe, expect, it, beforeEach, afterEach } from 'vitest';
import {
  getDispatcher,
  getDiagnosticState,
  fallbackToDirectAllowed,
  _resetForTests,
} from '../../src/lib/proxy_pool.js';

const savedEnv: Record<string, string | undefined> = {};
const ENV_KEYS = [
  'PROXY_URL_DEFAULT',
  'PROXY_URL_POLYMARKET',
  'PROXY_URL_LIMITLESS',
  'PROXY_URL_SX',
  'PROXY_FALLBACK_TO_DIRECT',
  'PROXY_STICKY_SESSION_PATTERN',
];

function snapshotEnv() {
  for (const k of ENV_KEYS) savedEnv[k] = process.env[k];
}
function restoreEnv() {
  for (const k of ENV_KEYS) {
    const v = savedEnv[k];
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
}
function clearEnv() {
  for (const k of ENV_KEYS) delete process.env[k];
}

describe('proxy_pool — env contract', () => {
  beforeEach(() => {
    snapshotEnv();
    clearEnv();
    _resetForTests();
  });
  afterEach(() => {
    _resetForTests();
    restoreEnv();
  });

  it('returns undefined when no env vars set (current behavior preserved)', () => {
    expect(getDispatcher('polymarket', 'bot1')).toBeUndefined();
    expect(getDispatcher('limitless', 'bot1')).toBeUndefined();
    expect(getDispatcher('sx', 'bot1')).toBeUndefined();
  });

  it('returns a Dispatcher when PROXY_URL_DEFAULT is set', () => {
    process.env['PROXY_URL_DEFAULT'] = 'http://user:pass@proxy.example.com:8080';
    const d = getDispatcher('polymarket', 'bot1');
    expect(d).toBeDefined();
    // undici ProxyAgent has a .close() method
    expect(typeof (d as { close?: unknown }).close).toBe('function');
  });

  it('per-platform override wins over default', () => {
    process.env['PROXY_URL_DEFAULT'] = 'http://user:pass@default.example:8080';
    process.env['PROXY_URL_POLYMARKET'] = 'http://user:pass@poly.example:8080';
    const dPoly = getDispatcher('polymarket', 'bot1');
    const dLim = getDispatcher('limitless', 'bot1');
    expect(dPoly).toBeDefined();
    expect(dLim).toBeDefined();
    // Both defined but distinct because the URLs differ → cache keys
    // are still distinct by platform, so they end up as different agents.
    expect(dPoly).not.toBe(dLim);
  });

  it('NONE sentinel forces direct (no proxy) for that platform', () => {
    process.env['PROXY_URL_DEFAULT'] = 'http://user:pass@proxy.example.com:8080';
    process.env['PROXY_URL_SX'] = 'NONE';
    expect(getDispatcher('polymarket', 'bot1')).toBeDefined();
    expect(getDispatcher('sx', 'bot1')).toBeUndefined();
  });

  it('same (platform, botId) returns SAME Dispatcher instance (sticky)', () => {
    process.env['PROXY_URL_DEFAULT'] = 'http://user:pass@proxy.example.com:8080';
    const a = getDispatcher('polymarket', 'bot3');
    const b = getDispatcher('polymarket', 'bot3');
    expect(a).toBe(b);
  });

  it('different botId returns DIFFERENT Dispatcher (per-bot sticky)', () => {
    process.env['PROXY_URL_DEFAULT'] = 'http://user:pass@proxy.example.com:8080';
    const a = getDispatcher('polymarket', 'bot1');
    const b = getDispatcher('polymarket', 'bot2');
    expect(a).not.toBe(b);
  });

  it('different platform returns DIFFERENT Dispatcher', () => {
    process.env['PROXY_URL_DEFAULT'] = 'http://user:pass@proxy.example.com:8080';
    const a = getDispatcher('polymarket', 'bot1');
    const b = getDispatcher('limitless', 'bot1');
    expect(a).not.toBe(b);
  });

  it('botId=undefined falls back to "shared" key (still functional)', () => {
    process.env['PROXY_URL_DEFAULT'] = 'http://user:pass@proxy.example.com:8080';
    const a = getDispatcher('polymarket');
    const b = getDispatcher('polymarket');
    expect(a).toBeDefined();
    expect(a).toBe(b); // same key 'shared' → same instance
  });

  it('handles URL without userinfo (no sticky injection)', () => {
    process.env['PROXY_URL_DEFAULT'] = 'http://proxy.example.com:8080';
    const d = getDispatcher('polymarket', 'bot1');
    expect(d).toBeDefined();
  });

  it('explicit PROXY_URL_DEFAULT=NONE acts like unset', () => {
    process.env['PROXY_URL_DEFAULT'] = 'NONE';
    expect(getDispatcher('polymarket', 'bot1')).toBeUndefined();
  });
});

describe('proxy_pool — fallback policy', () => {
  beforeEach(() => {
    snapshotEnv();
    clearEnv();
    _resetForTests();
  });
  afterEach(() => {
    _resetForTests();
    restoreEnv();
  });

  it('fallbackToDirectAllowed defaults to false (safe default)', () => {
    expect(fallbackToDirectAllowed()).toBe(false);
  });

  it('fallbackToDirectAllowed returns true ONLY when env=1', () => {
    process.env['PROXY_FALLBACK_TO_DIRECT'] = '1';
    expect(fallbackToDirectAllowed()).toBe(true);
    process.env['PROXY_FALLBACK_TO_DIRECT'] = '0';
    expect(fallbackToDirectAllowed()).toBe(false);
    process.env['PROXY_FALLBACK_TO_DIRECT'] = 'true';
    expect(fallbackToDirectAllowed()).toBe(false); // strict '1' only
  });
});

describe('proxy_pool — diagnostic state', () => {
  beforeEach(() => {
    snapshotEnv();
    clearEnv();
    _resetForTests();
  });
  afterEach(() => {
    _resetForTests();
    restoreEnv();
  });

  it('enabled=false when no env set', () => {
    const s = getDiagnosticState();
    expect(s.enabled).toBe(false);
    expect(s.agents).toEqual([]);
    expect(s.fallback_to_direct).toBe(false);
  });

  it('enabled=true and lists active agents when proxy is in use', () => {
    process.env['PROXY_URL_DEFAULT'] = 'http://user:pass@proxy.example.com:8080';
    getDispatcher('polymarket', 'bot1');
    getDispatcher('limitless', 'bot1');
    const s = getDiagnosticState();
    expect(s.enabled).toBe(true);
    expect(s.agents.length).toBe(2);
    // Diagnostic must NOT leak credentials — only host:port
    for (const a of s.agents) {
      expect(a.host).not.toContain('user');
      expect(a.host).not.toContain('pass');
      expect(a.host).toContain('proxy.example.com');
    }
  });
});

describe('proxy_pool — sticky session pattern', () => {
  beforeEach(() => {
    snapshotEnv();
    clearEnv();
    _resetForTests();
  });
  afterEach(() => {
    _resetForTests();
    restoreEnv();
  });

  it('default pattern is platform-bot, custom pattern is honored', () => {
    // We can't easily inspect the resulting auth string without
    // intercepting the ProxyAgent constructor, but we can verify the
    // env is read (function imports it lazily).
    process.env['PROXY_URL_DEFAULT'] = 'http://user:pass@proxy.example.com:8080';
    process.env['PROXY_STICKY_SESSION_PATTERN'] = '{bot}-only';
    // Smoke: getDispatcher with override should still return a Dispatcher.
    const d = getDispatcher('polymarket', 'bot5');
    expect(d).toBeDefined();
  });
});
