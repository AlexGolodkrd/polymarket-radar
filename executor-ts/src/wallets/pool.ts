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

const BOT_COUNT = 6;

/** Read env into Wallet objects. Missing private key → canSign=false. */
export function loadWalletsFromEnv(env: NodeJS.ProcessEnv = process.env): Wallet[] {
  const wallets: Wallet[] = [];
  for (let i = 1; i <= BOT_COUNT; i++) {
    const ethAddress = env[`BOT${i}_ETH_ADDRESS`] as Hex | undefined;
    if (!ethAddress) continue;
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
    });
    // privateKey is intentionally NOT stored on Wallet — keep it scoped
    // to the signer closure if needed. Phase TS-3 dry-run doesn't sign.
    void privateKey;
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
