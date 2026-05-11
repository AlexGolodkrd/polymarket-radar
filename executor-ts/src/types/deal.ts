/**
 * Cross-process contract: what the Python detector POSTs to the TS
 * executor on `/fire`. Mirrors the deal/leg shape produced by
 * `arb_server.build_deal()` / `cross_platform.build_cross_platform_deal()`.
 *
 * Kept narrow on purpose: detection-side fields (e.g. `min_liq`,
 * `slip_pct`, fixture metadata) are not needed by the executor and
 * stay on the Python side.
 */

import type { Hex } from 'viem';

export type Platform = 'polymarket' | 'sx_bet' | 'limitless' | 'kalshi';

export type Side = 'BUY' | 'SELL';

export type ArbStructure =
  | 'all_yes'
  | 'all_no'
  | 'yes_no_pair'
  | 'X1'
  | 'X2'
  | 'cp_complement_cover';

export interface LegSpec {
  platform: Platform;

  /** Polymarket / Limitless: outcome token id (uint256 string). */
  tokenId?: string;

  /** Polymarket: parent conditionId (used to fetch tick / min / fee). */
  conditionId?: Hex;

  /** Polymarket: true → use negRisk EIP-712 domain. */
  negRisk?: boolean;

  /** SX Bet: market hash for the binary market we're filling. */
  marketHash?: Hex;

  /** SX Bet: 1 (outcome one) | 2 (outcome two). */
  outcome?: 1 | 2;

  /** Limitless: market slug (used for marketSlug body field). */
  slug?: string;

  /** Limitless: per-market exchange contract for EIP-712 verifyingContract. */
  verifyingContract?: Hex;

  side: Side;

  /** Expected price in [0, 1] — radar's view at fire-time. */
  expectedPrice: number;

  /** Stake in USDC dollars (not wei). Builder converts to wei. */
  expectedSizeUsdc: number;

  /** Polymarket V2 tick size (0.01 default, some markets 0.001 / 0.005). */
  tickSize?: number;

  /** Min order size in USDC (Polymarket per-market V2 metadata). */
  minOrderSizeUsdc?: number;

  /** GTC | GTD | FOK; default GTC. */
  orderType?: 'GTC' | 'GTD' | 'FOK';
}

export interface FireRequest {
  arbId: string;
  dealTitle: string;
  structure: ArbStructure;
  entries: LegSpec[];
  /** Optional override; default reads `DRY_RUN` env. */
  dryRun?: boolean;
  /**
   * Phase audit-2 (11.05.2026) — expected payout in USDC when one
   * outcome wins. Without this the TS executor used a hardcoded $1
   * placeholder, which gave simPnl = 1 - totalStake = strongly
   * negative for CP arbs (face $50-100 / leg), making every paper-
   * trade row count as a LOSS and pinning paper_stats.win_rate at 0%.
   * Radar passes:
   *   - per-platform ALL_YES / YN_PAIR: 1.0 (one $1 contract)
   *   - per-platform ALL_NO with N outcomes: N-1
   *   - cross-platform binary: actual_face (face value bought across legs)
   * Optional for backward compat; missing → 1.0 fallback (old behavior).
   */
  expectedPayout?: number;
}

/** Builder output — what `fireArb` consumes per leg. */
export interface BuiltOrder<TBody = unknown> {
  platform: Platform;
  /** Body to POST — already includes signature when `signed=true`. */
  body: TBody;
  /** Endpoint URL the body is destined for. */
  wouldPostUrl: string;
  /** True iff a real EIP-712 signature is embedded. */
  signed: boolean;
  /** Echo of the radar's expected price for slippage check. */
  expectedPrice: number;
  /** Echo of the radar's expected size for paper-trade math. */
  expectedSizeUsdc: number;
  /** Deterministic JSON of the unsigned order — useful for golden tests. */
  signPayload: Uint8Array;
  /** Convenience: the raw unsigned order struct. */
  order?: unknown;
  /** Polymarket: pulled-through neg_risk flag for downstream auditing. */
  negRisk?: boolean;
}
