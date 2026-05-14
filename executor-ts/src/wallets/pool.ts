/**
 * Wallet pool — minimal port of Python `Scripts/wallets/`. Phase TS-3
 * loads wallets from env (`BOT{N}_ETH_ADDRESS`, `BOT{N}_PRIVATE_KEY`,
 * etc.) and assigns one wallet per leg with anti-detection round-robin.
 *
 * Same-bot-twice-on-one-arb is forbidden (anti-detection per Phase 4
 * rationale: ties two legs of the same arb to one wallet → on-chain
 * heuristics can detect arb activity → ban risk).
 */
import type { Hex } from 'viem';
import type { Wallet, PolySignatureType } from '../types/wallet.js';
import { registerSigner, registeredCount } from './signers.js';

export interface BotEnvSpec {
  botId: string;
  ethAddress?: Hex;
  privateKey?: Hex;
  signatureType?: PolySignatureType;
  funder?: Hex;
  polyApiKey?: string;
  polySecret?: string;
  polyPassphrase?: string;
  limitlessApiKey?: string;
}

// Phase TS-5e (14.05.2026) — env-overridable. Default 6 preserves the
// baseline; operator can set BOT_COUNT=1 for single-bot mode (small-
// deposit pilot before full multi-wallet rollout) or any value 1..6.
// Clamped defensively at module load so a typo can't disable the
// coordinator entirely.
const BOT_COUNT: number = (() => {
  const raw = process.env['BOT_COUNT'];
  if (!raw || raw.trim() === '') return 6;
  const n = Number.parseInt(raw, 10);
  if (Number.isNaN(n)) return 6;
  // 6 wallet slots are hardcoded in Credentials.env; 0 breaks the
  // coordinator. Clamp 1..6.
  return Math.max(1, Math.min(6, n));
})();

/**
 * Deterministic mock wallets for DRY_RUN-only mode.
 *
 * Used when no real `BOT*_ETH_ADDRESS` is configured but the operator
 * wants to exercise the TS executor path through the radar's TS-3
 * dispatcher (otherwise /fire returns 503 and the radar silently falls
 * back to the in-process Python executor — meaning TS code is never
 * actually executed in prod despite the container running).
 *
 * The addresses are an obviously-fake pattern: `0x000…0001` through
 * `0x000…0006`. They are 100% safe — `canSign=false` is hardcoded so
 * atomic.ts always log-only paths through these. viem accepts these as
 * valid `Hex` strings (40 hex chars, all in `[0-9a-f]`).
 *
 * IMPORTANT: `synthesizeMockWallets` MUST NOT be used when DRY_RUN=0.
 * The caller (server.ts startServer) enforces this gate. We belt-and-
 * braces enforce `canSign=false` here so even an accidental real-mode
 * call can never produce a signed order. Logged with a loud warning.
 */
export function synthesizeMockWallets(count: number = BOT_COUNT): Wallet[] {
  const wallets: Wallet[] = [];
  for (let i = 1; i <= count; i++) {
    // 40-hex-char address: 0x + 39 zeros + single digit i (1..6).
    // Recognisable as fake on sight, valid EIP-55 (all-lowercase passes).
    const addr = `0x${'0'.repeat(39)}${i.toString(16)}` as Hex;
    wallets.push({
      botId: `bot${i}`,
      ethAddress: addr,
      canSign: false, // belt-and-braces — mock wallets can NEVER sign
      signatureType: 0,
    });
  }
  return wallets;
}

/**
 * Read env into Wallet objects. Missing private key → canSign=false.
 *
 * Phase fix-signer (11.05.2026) — added detailed audit log of what's
 * present in env so operators can diagnose `signers_registered=0`
 * mismatches in /api/ts_metrics. Logs counts only (no key material
 * leaks). Per-bot tally tells operator which BOT*N* slots are
 * misconfigured.
 */
export function loadWalletsFromEnv(env: NodeJS.ProcessEnv = process.env): Wallet[] {
  const wallets: Wallet[] = [];
  const auditPresent: string[] = [];
  const auditMissing: string[] = [];
  for (let i = 1; i <= BOT_COUNT; i++) {
    const ethAddress = env[`BOT${i}_ETH_ADDRESS`] as Hex | undefined;
    if (!ethAddress) {
      auditMissing.push(`bot${i}`);
      continue;
    }
    auditPresent.push(`bot${i}`);
    const privateKey = env[`BOT${i}_PRIVATE_KEY`] as Hex | undefined;
    const sigTypeRaw = env[`BOT${i}_SIGNATURE_TYPE`];
    const sigType: PolySignatureType =
      sigTypeRaw === '1' ? 1 : sigTypeRaw === '2' ? 2 : 0;
    const funder = env[`BOT${i}_FUNDER_ADDRESS`] as Hex | undefined;
    wallets.push({
      botId: `bot${i}`,
      ethAddress,
      canSign: !!privateKey,
      signatureType: sigType,
      ...(funder ? { funder } : {}),
      ...(env[`BOT${i}_POLY_API_KEY`] ? { polyApiKey: env[`BOT${i}_POLY_API_KEY`] } : {}),
      ...(env[`BOT${i}_POLY_SECRET`] ? { polySecret: env[`BOT${i}_POLY_SECRET`] } : {}),
      ...(env[`BOT${i}_POLY_PASSPHRASE`]
        ? { polyPassphrase: env[`BOT${i}_POLY_PASSPHRASE`] }
        : {}),
      ...(env[`BOT${i}_LIMITLESS_API_KEY`] ?? env.LIMITLESS_API_KEY
        ? { limitlessApiKey: env[`BOT${i}_LIMITLESS_API_KEY`] ?? env.LIMITLESS_API_KEY }
        : {}),
      // Phase TS-5f.4 — HMAC secret for Limitless V2. Per-bot
      // BOT{i}_LIMITLESS_API_SECRET takes priority, falls back to
      // global LIMITLESS_API_SECRET (single-bot pilot pattern).
      ...(env[`BOT${i}_LIMITLESS_API_SECRET`] ?? env.LIMITLESS_API_SECRET
        ? { limitlessApiSecret: env[`BOT${i}_LIMITLESS_API_SECRET`] ?? env.LIMITLESS_API_SECRET }
        : {}),
    });
    // Phase TS-5d (11.05.2026) — private key is intentionally NOT stored
    // on Wallet (which gets serialized to /metrics, log lines, paper
    // JSONL). Instead we register it in a module-scoped Map keyed by
    // botId, accessed only via getSignerKey(botId) in EIP-712 code paths.
    // Even console.log(wallet) cannot leak the key.
    if (privateKey) {
      try {
        registerSigner(`bot${i}`, privateKey);
      } catch (err) {
        // Bad key format — keep wallet in pool but with canSign=false-effective.
        // We already set canSign=true based on env presence; downgrade by
        // returning the wallet without the registered signer. atomic.ts will
        // detect the mismatch via hasSigner() at fire time.
        console.error(
          `[wallets/pool] registerSigner(bot${i}) failed: ${(err as Error).message}`,
        );
      }
    }
  }
  // Phase fix-signer (11.05.2026) — startup audit summary. Counts only,
  // no key material. Tells operator at a glance what got picked up
  // from Credentials.env vs. what's missing.
  const hasGlobalLimKey = !!env.LIMITLESS_API_KEY;
  console.log(
    `[wallets/pool] env audit: wallets_loaded=${wallets.length} ` +
      `bots_present=[${auditPresent.join(',')}] ` +
      `bots_missing_addr=[${auditMissing.join(',')}] ` +
      `signers_registered=${registeredCount()} ` +
      `limitless_api_key_global=${hasGlobalLimKey ? 'yes' : 'no'} ` +
      `limitless_api_key_per_bot=[${
        wallets.filter((w) => !!w.limitlessApiKey).map((w) => w.botId).join(',')
      }] ` +
      `poly_l2_creds_per_bot=[${
        wallets
          .filter((w) => !!(w.polyApiKey && w.polySecret && w.polyPassphrase))
          .map((w) => w.botId)
          .join(',')
      }]`,
  );
  // Explicit mismatch warning — registered != canSign means a key got
  // through env validation but failed registerSigner format check.
  const canSignCount = wallets.filter((w) => w.canSign).length;
  if (canSignCount > 0 && registeredCount() !== canSignCount) {
    console.warn(
      `[wallets/pool] MISMATCH: ${canSignCount} wallets have BOT*_PRIVATE_KEY in env but ` +
        `only ${registeredCount()} passed registerSigner format check. ` +
        `Real-mode signing will fail for the unregistered bots. ` +
        `See error messages above for which bots / why.`,
    );
  }
  return wallets;
}

/**
 * Assign one wallet per leg. Round-robin starts from a rotating offset
 * so two consecutive arbs don't both start with bot1 (small anti-
 * detection nicety). Throws if pool too small.
 */
let _rrCursor = 0;
export function assignLegs(pool: Wallet[], legCount: number): Wallet[] {
  if (pool.length < legCount) {
    throw new Error(
      `wallet pool too small: ${pool.length} wallets, need ${legCount}`,
    );
  }
  const out: Wallet[] = [];
  const used = new Set<string>();
  for (let i = 0; i < legCount; i++) {
    let pick: Wallet | undefined;
    for (let attempt = 0; attempt < pool.length; attempt++) {
      const candidate = pool[(_rrCursor + i + attempt) % pool.length];
      if (candidate && !used.has(candidate.botId)) {
        pick = candidate;
        used.add(candidate.botId);
        break;
      }
    }
    if (!pick) {
      throw new Error('failed to pick distinct wallets — internal logic error');
    }
    out.push(pick);
  }
  _rrCursor = (_rrCursor + 1) % Math.max(1, pool.length);
  return out;
}

/** Reset cursor for tests. */
export function _resetCursor(): void {
  _rrCursor = 0;
}

/**
 * Returns 0..50ms jitter per leg for anti-detection. Phase 4 invariant
 * preserved (operator config). Mirrors Python coordinator.jitter_ms_for_leg.
 */
export function jitterMsForLeg(legIdx: number): number {
  // Deterministic per-index jitter so tests are reproducible.
  // Different legs of the same arb get different delays (anti-detection),
  // but the SAME leg index across runs gets the same delay (stability).
  return ((legIdx * 7 + 13) % 50);
}
