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
 * same botId overwrites (used in tests). The caller is responsible for
 * sanitizing the input (key must be `0x` + 64 hex chars; viem validates
 * at signing time too).
 *
 * Calls log a redacted confirmation — useful so operators can verify
 * load-time which bots got keys, without leaking the keys themselves.
 */
export function registerSigner(botId: string, privateKey: Hex): void {
  if (!privateKey.startsWith('0x') || privateKey.length !== 66) {
    throw new Error(
      `registerSigner(${botId}): invalid private key length/prefix (expected 0x + 64 hex chars)`,
    );
  }
  _signerKeys.set(botId, privateKey);
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
