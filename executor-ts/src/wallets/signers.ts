/**
 * Signer key registry — module-scoped secret storage for bot private
 * keys. Keys never appear on the Wallet object (which is serialized to
 * /metrics, log lines, paper JSONL), so even with `console.log(wallet)`
 * we can't accidentally leak them. Keys are accessed only via the
 * deliberate `getSignerKey(botId)` lookup that EIP-712-signing code
 * calls.
 *
 * Mirrors the Python "stores" pattern (`Scripts/wallets/stores.py`):
 *   LocalEnvStore reads BOT*_PRIVATE_KEY from env at startup, hands the
 *   value to anyone who calls `get_signer_key(bot_id)`, and never
 *   echoes it elsewhere.
 *
 * Test seam: `_resetRegistry()` clears state so test files don't leak
 * mock keys between suites.
 *
 * Phase TS-5d (11.05.2026) — wires the secret-handling layer that
 * lets `atomic.fireLeg` call into builders with a real privateKey
 * when wallet.canSign=true. Without this module, builders sit unsigned
 * even in real mode and POST helpers refuse the request.
 */
import type { Hex } from 'viem';

const _signerKeys = new Map<string, Hex>();

/**
 * Register a private key for a botId. Idempotent — re-registering the
 * same botId overwrites (used in tests).
 *
 * Input normalization (Phase fix-signer-registration, 11.05.2026):
 *   - strips ASCII whitespace (space, tab, CR, LF) — env values often
 *     have trailing newlines or BOM-like artifacts on Windows
 *   - lowercases (Ethereum private keys are case-insensitive; uppercase
 *     hex passes viem but our strict validator was rejecting it)
 *   - adds `0x` prefix if the operator pasted bare 64-hex without prefix
 *
 * After normalization, validates: must be `0x` + exactly 64 hex chars.
 * Throws a SPECIFIC error message telling the operator WHICH validation
 * step failed (length, prefix, non-hex chars) so misconfiguration is
 * obvious from the startup log without leaking the key itself.
 */
export function registerSigner(botId: string, privateKey: Hex): void {
  const normalized = normalizePrivateKey(privateKey);
  // Strict format check after normalization.
  if (normalized.length !== 66) {
    throw new Error(
      `registerSigner(${botId}): private key has ${normalized.length - 2} hex chars ` +
        `after normalization (expected 64). Check Credentials.env BOT${botId.slice(3)}_PRIVATE_KEY value.`,
    );
  }
  if (!/^0x[0-9a-f]{64}$/.test(normalized)) {
    throw new Error(
      `registerSigner(${botId}): private key contains non-hex characters ` +
        `after normalization. Expected 0x + 64 hex chars [0-9a-f].`,
    );
  }
  _signerKeys.set(botId, normalized as Hex);
}

/**
 * Normalize an env-sourced private key into the canonical form viem
 * accepts. Exported separately so tests can pin specific cases.
 *
 * Examples (all map to the same canonical form):
 *   '0xABCD...'                  → '0xabcd...'
 *   'abcd...' (no prefix)        → '0xabcd...'
 *   '  0xABCD...\n'              → '0xabcd...'
 *   '0X  abcd  ...' (mixed)      → '0xabcd...'
 */
export function normalizePrivateKey(raw: string): string {
  // Strip all ASCII whitespace anywhere (env-paste artifacts).
  let s = raw.replace(/\s+/g, '');
  // Lowercase the whole thing — both prefix and hex chars.
  s = s.toLowerCase();
  // Add 0x prefix if missing.
  if (!s.startsWith('0x')) s = '0x' + s;
  return s;
}

/**
 * Lookup the private key for a botId. Returns `undefined` if the bot
 * isn't registered. Returning undefined lets callers decide whether to
 * fail-loud (atomic.fireLeg in real mode) or fail-soft (build unsigned
 * for paper trade).
 */
export function getSignerKey(botId: string): Hex | undefined {
  return _signerKeys.get(botId);
}

/** True iff a key is registered for this bot. Avoids exposing the key. */
export function hasSigner(botId: string): boolean {
  return _signerKeys.has(botId);
}

/** Number of registered bots — for /metrics surface (count only, no keys). */
export function registeredCount(): number {
  return _signerKeys.size;
}

/** List of registered botIds. Safe to expose (no key material). */
export function registeredBotIds(): string[] {
  return Array.from(_signerKeys.keys());
}

/** Clear state — for test isolation only. Never call from production. */
export function _resetRegistry(): void {
  _signerKeys.clear();
}
