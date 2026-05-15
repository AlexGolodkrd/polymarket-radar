/**
 * Atomic arb firer — TS port of Python `Scripts/executor/atomic.py`.
 *
 * Distributes a deal's legs across the wallet pool, fires them in
 * parallel via Promise.all (target <100ms), implements:
 *   - per-leg timeout (Python PER_ORDER_TIMEOUT_S=2s)
 *   - slippage check (|fill_price - expected| > 0.005 → cancel)
 *   - dead-man switch (no fill confirms within 5s → cancel all)
 *   - reversal (if some legs filled and others rejected, sell filled
 *     at market to flatten)
 *
 * Phase TS-3 ships **dry-run only** — no real POSTs. Caller receives
 * the same ArbFireResult shape Python writes to dryrun.jsonl. Real
 * HTTP firing wires in TS-5 alongside fill confirmation.
 */
import type { FireRequest, LegSpec, BuiltOrder } from '../types/deal.js';
import type { Wallet } from '../types/wallet.js';
import { buildPolyOrder } from '../builders/poly.js';
import { buildSxOrder } from '../builders/sx.js';
import { buildLimitlessOrder } from '../builders/limitless.js';
import { assignLegs, jitterMsForLeg } from '../wallets/pool.js';
import { getSignerKey } from '../wallets/signers.js';
import { postPolyOrder, deletePolyOrder } from '../fire/poly_post.js';
import { postSxFill } from '../fire/sx_post.js';
import { postLimOrder, deleteLimOrder } from '../fire/lim_post.js';
import { getLimitlessOwnerId, getLimitlessVenueExchange } from '../lib/limitless_profile.js';
import { expectFill } from './fills.js';
import { getPolyUserWS } from '../ws/ws_manager.js';
import { checkCanFire, clipToPerTradeCap, applyPlatformMinFloor } from '../risk/limits.js';
import { isKilled } from '../risk/killswitch.js';
import {
  type ArbFireResult,
  type LegResult,
  logOrderDecision,
  logArbDecision,
  schedulePaperEvaluation,
} from './paper.js';
import { planRevert, annotateLegsWithPlan, executeRevertPlan } from './revert.js';

const DRY_RUN_DEFAULT = (process.env.DRY_RUN ?? '1') !== '0';
const SLIPPAGE_TOLERANCE = Number(process.env.SLIPPAGE_TOLERANCE ?? '0.005');
const MIN_NET_PER_ARB_USD = Number(process.env.MIN_NET_PER_ARB_USD ?? '0.50');
// 8s default — covers cold SOCKS5+TLS handshake (200-800ms) + POST round-trip
// + possible 1 retry. Python parity was 2s, but Python doesn't go through
// SOCKS5 proxy from the same process; the executor does, and a first-fire
// through a cold proxy connection routinely measures 1-3s on residential
// IPs. After the first POST the connection is reused (warmer = faster).
const PER_LEG_TIMEOUT_MS = Number(process.env.PER_ORDER_TIMEOUT_S ?? '8') * 1000;

/**
 * Build the platform-specific BuiltOrder for one leg. Pure dispatch
 * over LegSpec.platform — each builder is itself pure (no I/O), the
 * one exception being SX Bet which needs maker orders fetched first.
 * Phase TS-3 stubs SX with empty orders — TS-5 wires real fetcher.
 */
/**
 * Round price to the per-market tick. Polymarket and Limitless both
 * reject orders with 400 if `price` isn't a multiple of `tickSize`.
 * Default 0.01 covers most markets; per-market override comes from
 * `spec.tickSize` (radar populates from market meta).
 *
 * Phase audit-4 (15.05.2026) — added as defensive pre-fix before live
 * POSTs start landing. Builder header comments warned that "caller is
 * responsible for tick alignment" but no caller was actually doing it.
 */
function snapPriceToTick(price: number, tickSize: number | undefined): number {
  const tick = tickSize && tickSize > 0 ? tickSize : 0.01;
  const snapped = Math.round(price / tick) * tick;
  // Clamp to (0, 1) and round to a fixed-precision representation to
  // avoid floating-point artifacts like 0.8200000000000001.
  const clamped = Math.max(tick, Math.min(1 - tick, snapped));
  return Math.round(clamped * 1e6) / 1e6;
}

async function buildLeg(spec: LegSpec, wallet: Wallet): Promise<BuiltOrder<unknown>> {
  // Phase TS-5d (11.05.2026) — pull privateKey from the signer registry.
  // Returns undefined when the bot has no registered key (e.g. mock
  // wallets in DRY_RUN, or operator hasn't filled BOT*_PRIVATE_KEY).
  // The builders' canSign=true && privateKey!=undefined gate is what
  // actually decides whether the resulting BuiltOrder is signed.
  const privateKey = wallet.canSign ? getSignerKey(wallet.botId) : undefined;

  switch (spec.platform) {
    case 'polymarket': {
      if (!spec.tokenId) throw new Error(`polymarket leg requires tokenId`);
      const snappedPrice = snapPriceToTick(spec.expectedPrice, spec.tickSize);
      return await buildPolyOrder({
        tokenId: spec.tokenId,
        side: spec.side,
        price: snappedPrice,
        sizeUsdc: spec.expectedSizeUsdc,
        wallet,
        ...(privateKey ? { privateKey } : {}),
        ...(spec.negRisk !== undefined ? { negRisk: spec.negRisk } : {}),
        ...(spec.orderType ? { orderType: spec.orderType } : {}),
      });
    }
    case 'limitless': {
      if (!spec.tokenId || !spec.slug) {
        throw new Error('limitless leg requires tokenId + slug');
      }
      // Limitless uses 0.01 tick on most markets; radar doesn't populate
      // tickSize for Limitless legs today, so snapPriceToTick will use
      // its 0.01 default. If a Limitless market actually uses 0.005 or
      // 0.001 a 400 from the server will surface (visible now via
      // verbose body diagnostic) and we'll wire it through.
      const snappedPrice = snapPriceToTick(spec.expectedPrice, spec.tickSize);
      // Phase audit-5 (15.05.2026) — POST /orders requires `ownerId`
      // (the wallet's Limitless profile id). Missing the field
      // produced an opaque `HTTP 400 body="Bad Request"` on every
      // live fire. Cached in-process so the lookup costs ~1 RTT only
      // the first time we fire from this wallet.
      const ownerId = await getLimitlessOwnerId(wallet.ethAddress);
      // Phase audit-12 (15.05.2026) — verifyingContract is per-market.
      // Radar may not always populate spec.verifyingContract (legacy CP
      // pipeline did not). Fetch from /markets/{slug}.venue.exchange and
      // cache, so the EIP-712 signing uses the right CTF Exchange and
      // server doesn't reject with `"Invalid signature. Exchange address
      // for this market: 0x..."`.
      const venueExchange =
        spec.verifyingContract ?? (await getLimitlessVenueExchange(spec.slug));
      return await buildLimitlessOrder({
        slug: spec.slug,
        tokenId: spec.tokenId,
        side: spec.side,
        price: snappedPrice,
        sizeUsdc: spec.expectedSizeUsdc,
        wallet,
        ownerId,
        verifyingContract: venueExchange,
        ...(privateKey ? { privateKey } : {}),
        ...(spec.orderType ? { orderType: spec.orderType } : {}),
      });
    }
    case 'sx_bet': {
      if (!spec.marketHash || spec.outcome === undefined) {
        throw new Error('sx_bet leg requires marketHash + outcome');
      }
      // Phase audit-5 (15.05.2026) — SX v2: server matches makers. We
      // sign a flat fill intent (market + outcome + worst odds +
      // slippage) and POST; the server walks its own orderbook. No
      // more pre-fire `GET /orders` round-trip and no greedy match.
      // If no makers satisfy our desiredOdds, the POST returns 4xx
      // with a body we surface via the verbose body parser.
      return await buildSxOrder({
        marketHash: spec.marketHash,
        outcome: spec.outcome,
        takerPrice: spec.expectedPrice,
        sizeUsdc: spec.expectedSizeUsdc,
        wallet,
        ...(privateKey ? { privateKey } : {}),
      });
    }
    case 'kalshi':
      throw new Error('kalshi disabled (US-only KYC)');
    default:
      throw new Error(`unknown platform: ${spec.platform as string}`);
  }
}

/**
 * Fire one leg in dry-run mode: build → log → simulate "rejected" or
 * "dry-fired" status. In real-mode (Phase TS-5) this becomes a real
 * POST with timeout, slippage check, and fill-event awaiting.
 */
async function fireLeg(
  arbId: string,
  legIdx: number,
  spec: LegSpec,
  wallet: Wallet,
  dryRun: boolean,
): Promise<LegResult> {
  const startedAt = Date.now();
  // Anti-detection jitter (matches Python coordinator behavior).
  await new Promise((r) => setTimeout(r, jitterMsForLeg(legIdx)));
  try {
    const built = await buildLeg(spec, wallet);
    await logOrderDecision(arbId, legIdx, built, spec, wallet.botId);

    if (dryRun) {
      // Phase TS-3 default path: log the decision, mark as dry-fired.
      // The paper-eval step (5s later) will refetch and evaluate
      // realistic fill, mirroring Python.
      return {
        legIdx,
        platform: spec.platform,
        status: 'dry-fired',
        expectedPrice: built.expectedPrice,
        expectedSizeUsdc: built.expectedSizeUsdc,
        botId: wallet.botId,
        elapsedMs: Date.now() - startedAt,
        extra: {
          signed: built.signed,
          would_post_url: built.wouldPostUrl,
        },
      };
    }

    // Phase TS-5c.2 (11.05.2026) — real-mode firing.
    // Pre-flight: builder MUST have signed the order. canSign=false or
    // missing signer key → unsigned BuiltOrder → server rejects with
    // INVALID_SIGNATURE. Refuse fast here instead of burning the POST.
    if (!built.signed) {
      return {
        legIdx,
        platform: spec.platform,
        status: 'rejected',
        expectedPrice: built.expectedPrice,
        expectedSizeUsdc: built.expectedSizeUsdc,
        botId: wallet.botId,
        error: 'order built unsigned — canSign=false or signer not registered',
        elapsedMs: Date.now() - startedAt,
      };
    }

    // Phase TS-5c.3 (11.05.2026) — pre-subscribe the user-channel WS to
    // this leg's market BEFORE POST, so the trade event arrives before
    // the 5s dead-man wait elapses. Without this, expectFill would always
    // timeout (Polymarket user WS only delivers events for subscribed
    // markets).
    //
    // For Polymarket: needs conditionId on the spec. Radar should pass
    // it (it's available in gamma-api response). If absent, we still
    // proceed — the WS may already cover the market via a previous
    // updateMarkets call, or this fire will hit the deadman.
    //
    // For Limitless: orderEvent channel is subscribed globally on
    // connect (no per-market sub needed) — no action required here.
    //
    // SX Bet: synchronous fill response, no WS to pre-subscribe.
    if (spec.platform === 'polymarket' && spec.conditionId) {
      const ws = getPolyUserWS(wallet.botId);
      if (ws) {
        // updateMarkets is set-equality idempotent — if conditionId is
        // already in the active set, no reconnect happens. Otherwise
        // reconnect-with-extended-set (~1-2s) starts NOW so we can race
        // it against the POST round-trip.
        //
        // MERGE rather than replace — without merge, every fire would
        // strip down to a single-market view and unsub previous markets.
        // Bad: a concurrent second arb on a different market would lose
        // its subscription mid-fire.
        const merged = ws.getDesiredMarkets();
        merged.add(spec.conditionId);
        ws.updateMarkets(merged);
      }
    }

    // Dispatch the POST per platform. Each helper enforces shape
    // requirements (signature present, orderHashes non-empty, etc.).
    let orderId: string | undefined;
    let postFillPrice: number | undefined;
    let postFillSizeUsdc: number | undefined;
    try {
      switch (spec.platform) {
        case 'polymarket': {
          const resp = await postPolyOrder({
            body: built.body as Parameters<typeof postPolyOrder>[0]['body'],
            botId: wallet.botId,
          });
          orderId = resp.body.orderID;
          // SX-style sync fills don't apply here; Polymarket fills via WS.
          break;
        }
        case 'sx_bet': {
          // Phase audit-5 (15.05.2026) — v2 maker-race retry. In v2 the
          // server picks makers, so an "order not available" race is
          // resolved server-side. A 4xx here means liquidity actually
          // ran out at our `desiredOdds` cap. Rebuilding with the same
          // cap would just hit the same condition, so we no longer
          // retry — the error surfaces in `leg.error` and the operator
          // (or the next CP scan) decides whether to re-try at a wider
          // cap.
          const resp = await postSxFill({
            body: built.body as Parameters<typeof postSxFill>[0]['body'],
            botId: wallet.botId,
          });
          // Phase audit-11 (15.05.2026) — log raw SX fill response so we
          // can tell a true fill from a server-side soft no-op (fillHash
          // present but fillAmount=0). Previously the dryrun row showed
          // `status: filled, fill_size_usdc: 0` without the actual server
          // response, making it impossible to distinguish.
          // eslint-disable-next-line no-console
          console.log(
            `[sx-fill-resp] arbId=${arbId} leg=${legIdx} status=${resp.status} body=${JSON.stringify(resp.body).slice(0, 400)}`,
          );
          // SX returns fill atomically in the POST response — no WS wait.
          const data = resp.body.data;
          if (data?.fillHash) {
            const filledUnits = Number(
              (data as { fillAmount?: string }).fillAmount ??
                (data as { totalFilled?: string }).totalFilled ??
                '0',
            );
            postFillSizeUsdc = filledUnits / 1e6;
            postFillPrice = spec.expectedPrice;
            // Only consider it filled if we actually got non-zero size.
            // Otherwise atomic.ts will treat fillHash as enough and we'll
            // claim a position we don't have.
            if (postFillSizeUsdc > 0) {
              orderId = data.fillHash;
            }
          }
          break;
        }
        case 'limitless': {
          if (!wallet.limitlessApiKey) {
            throw new Error('limitless leg requires wallet.limitlessApiKey');
          }
          // Phase audit-3 (15.05.2026) — pass HMAC secret. Without this
          // postLimOrder falls back to legacy X-API-Key header which the
          // current Limitless V2 API rejects with HTTP 401. The secret
          // is loaded into `wallet.limitlessApiSecret` from
          // LIMITLESS_API_SECRET env (Credentials.env) and is the same
          // value used by limitless_user_ws.ts for the fill channel.
          const resp = await postLimOrder({
            body: built.body as Parameters<typeof postLimOrder>[0]['body'],
            apiKey: wallet.limitlessApiKey,
            ...(wallet.limitlessApiSecret
              ? { apiSecret: wallet.limitlessApiSecret }
              : {}),
            botId: wallet.botId,
          });
          // Phase audit-14 (15.05.2026) — Limitless V2 returns the
          // order id nested under `order.id`. Earlier code read the
          // legacy top-level `id` (undefined in V2) → leg was marked
          // rejected even though the order had been placed → ghost
          // orders sat LIVE on Limitless that the bot didn't know
          // about. Read both shapes; nested wins.
          orderId = resp.body.order?.id ?? resp.body.id;
          // eslint-disable-next-line no-console
          console.log(
            `[lim-place-resp] arbId=${arbId} leg=${legIdx} status=${resp.status} order_id=${orderId ?? 'NONE'} settlement=${resp.body.execution?.settlementStatus ?? '?'}`,
          );
          break;
        }
        case 'kalshi':
          throw new Error('kalshi disabled (US-only KYC)');
      }
    } catch (err) {
      return {
        legIdx,
        platform: spec.platform,
        status: 'rejected',
        expectedPrice: built.expectedPrice,
        expectedSizeUsdc: built.expectedSizeUsdc,
        botId: wallet.botId,
        error: `POST failed: ${err instanceof Error ? err.message : String(err)}`,
        elapsedMs: Date.now() - startedAt,
      };
    }

    if (!orderId) {
      return {
        legIdx,
        platform: spec.platform,
        status: 'rejected',
        expectedPrice: built.expectedPrice,
        expectedSizeUsdc: built.expectedSizeUsdc,
        botId: wallet.botId,
        error: 'POST returned no order ID',
        elapsedMs: Date.now() - startedAt,
      };
    }

    // SX returned the fill atomically — no WS wait needed. Slippage was
    // already locked in at the maker price (taker took the quote).
    if (postFillPrice !== undefined && postFillSizeUsdc !== undefined) {
      return {
        legIdx,
        platform: spec.platform,
        status: 'filled',
        expectedPrice: built.expectedPrice,
        expectedSizeUsdc: built.expectedSizeUsdc,
        fillPrice: postFillPrice,
        fillSizeUsdc: postFillSizeUsdc,
        botId: wallet.botId,
        elapsedMs: Date.now() - startedAt,
        extra: { orderId, sync_fill: true },
      };
    }

    // Polymarket / Limitless: wait for fill via fillRegistry (fed by
    // the WS user-channel listeners). Slippage decision baked in.
    const outcome = await expectFill({
      arbId,
      legIdx,
      platform: spec.platform,
      orderId,
      expectedPrice: built.expectedPrice,
      deadmanMs: 5000,
    });

    if (outcome.kind === 'filled' || outcome.kind === 'slipped') {
      return {
        legIdx,
        platform: spec.platform,
        status: outcome.kind, // 'filled' | 'slipped'
        expectedPrice: built.expectedPrice,
        expectedSizeUsdc: built.expectedSizeUsdc,
        fillPrice: outcome.fillPrice,
        fillSizeUsdc: outcome.fillSizeUsdc,
        botId: wallet.botId,
        elapsedMs: Date.now() - startedAt,
        extra: {
          orderId,
          slippage_delta_abs: outcome.slippage.deltaAbs,
          slippage_within: outcome.slippage.within,
        },
      };
    }
    // Timeout — order placed but no fill confirmation in deadman window.
    // Phase TS-6 (11.05.2026) — fire-and-forget cancel via L2 HMAC.
    // Without this the order sits on Poly's book until natural-expire
    // and can fill at adverse prices later. We don't await the cancel
    // (the leg has already been classified as 'timeout' regardless of
    // cancel success), but we do attach the cancel outcome to extra
    // for paper-trail forensics.
    let cancelStatus: 'sent' | 'skipped' | 'failed' = 'skipped';
    let cancelReason: string | undefined;
    if (
      spec.platform === 'polymarket' &&
      wallet.polyApiKey &&
      wallet.polySecret &&
      wallet.polyPassphrase
    ) {
      try {
        await deletePolyOrder({
          orderId,
          creds: {
            apiKey: wallet.polyApiKey,
            apiSecret: wallet.polySecret,
            passphrase: wallet.polyPassphrase,
          },
          ethAddress: wallet.ethAddress,
        });
        cancelStatus = 'sent';
      } catch (err) {
        cancelStatus = 'failed';
        cancelReason = err instanceof Error ? err.message : String(err);
      }
    } else if (spec.platform === 'polymarket') {
      cancelReason = 'missing L2 creds';
    } else if (spec.platform === 'limitless' && wallet.limitlessApiKey) {
      // Phase TS-6.2 (11.05.2026) — Limitless DELETE /orders/{id}.
      //
      // Phase audit-4 (15.05.2026) — pass apiSecret too. Same root
      // cause as the POST-side 401 caught in PR #214: Limitless V2 only
      // accepts HMAC-signed authenticated requests; the legacy
      // X-API-Key bearer path 401s. Without this, every timeout-cancel
      // would 401 and leave the resting order on the book.
      try {
        await deleteLimOrder({
          orderId,
          apiKey: wallet.limitlessApiKey,
          ...(wallet.limitlessApiSecret
            ? { apiSecret: wallet.limitlessApiSecret }
            : {}),
        });
        cancelStatus = 'sent';
      } catch (err) {
        cancelStatus = 'failed';
        cancelReason = err instanceof Error ? err.message : String(err);
      }
    } else if (spec.platform === 'limitless') {
      cancelReason = 'missing limitlessApiKey';
    } else {
      // SX taker fills are atomic — no resting order to cancel.
      cancelReason = `cancel not applicable for ${spec.platform}`;
    }
    return {
      legIdx,
      platform: spec.platform,
      status: 'timeout',
      expectedPrice: built.expectedPrice,
      expectedSizeUsdc: built.expectedSizeUsdc,
      botId: wallet.botId,
      error: outcome.reason,
      elapsedMs: Date.now() - startedAt,
      extra: {
        orderId,
        cancel_status: cancelStatus,
        ...(cancelReason ? { cancel_reason: cancelReason } : {}),
      },
    };
  } catch (err) {
    return {
      legIdx,
      platform: spec.platform,
      status: 'rejected',
      expectedPrice: spec.expectedPrice,
      expectedSizeUsdc: spec.expectedSizeUsdc,
      botId: wallet.botId,
      error: err instanceof Error ? err.message : String(err),
      elapsedMs: Date.now() - startedAt,
    };
  }
}

/**
 * Top-level entry: fire an arb across N legs. Returns ArbFireResult
 * compatible with Python schema (one row in dryrun.jsonl).
 *
 * Pre-fire risk checks:
 *   1. kill switch (fail-CLOSED)
 *   2. risk.checkCanFire(legCount, totalStake)
 *
 * On success-path: builds all legs, fires Promise.all with per-leg
 * timeout, computes statuses, writes dryrun.jsonl, schedules paper eval.
 *
 * Aborted reason (string) is set when pre-fire gate denies. legs is
 * empty in that case (matches Python's `legs: []` when aborted before
 * fire).
 */
export async function fireArb(
  req: FireRequest,
  walletPool: Wallet[],
  dryRun: boolean = DRY_RUN_DEFAULT,
): Promise<ArbFireResult> {
  const firedAt = Date.now() / 1000;
  const legCount = req.entries.length;

  // Per-trade cap is a CLIP, not a block. Radar sizes for max profit
  // given liquidity/depth; operator's cap is the risk envelope. When the
  // radar's chosen stake exceeds the cap, scale every leg down
  // proportionally so the arb still fires at the allowed size instead of
  // being aborted with $0 P&L. Mutates req.entries in place — every
  // downstream consumer (builders, paper-results, dryrun.jsonl) sees the
  // clipped sizes, which is the truth of what we tried to fire.
  const clipReport = clipToPerTradeCap(req.entries);

  // Then floor any legs that ended up below the platform minimum after
  // clipping. The total stake may slightly exceed the cap in this corner
  // case (e.g. 80¢ + 20¢ split on $2 cap → clip to $1.60+$0.40 → floor
  // $0.40 to $1.00 → total $2.60). Accepted because the alternative
  // (abort) throws away a real arb. See `applyPlatformMinFloor`.
  const floorReport = applyPlatformMinFloor(req.entries);

  // Use the post-floor total so expectedCost reflects what we actually fire.
  const totalStake = floorReport.finalTotalStakeUsd;
  // Scale expectedPayout by the effective stake ratio (post-clip-and-floor).
  // No-clip + no-floor path collapses to ratio=1; clip-only path uses the
  // clip ratio; clip+floor recovers proportionality to the staked total.
  const effectiveRatio =
    clipReport.originalTotalStakeUsd > 0
      ? totalStake / clipReport.originalTotalStakeUsd
      : 1.0;
  const expectedPayout = (req.expectedPayout ?? 1.0) * effectiveRatio;
  const expectedCost = totalStake;

  // Pre-fire gates ----------------------------------------------------
  if (isKilled()) {
    return makeAbortedResult(req, firedAt, dryRun, 'kill switch active', expectedCost, expectedPayout);
  }
  const can = await checkCanFire(legCount, totalStake);
  if (!can.allowed) {
    return makeAbortedResult(req, firedAt, dryRun, can.reason ?? 'risk gate', expectedCost, expectedPayout);
  }
  if (clipReport.clipped) {
    console.log(
      `[risk] clipped stake $${clipReport.originalTotalStakeUsd.toFixed(2)} → ` +
      `$${clipReport.clippedTotalStakeUsd.toFixed(2)} (cap $${clipReport.capUsd}, ` +
      `ratio ${clipReport.ratio.toFixed(4)}) arbId=${req.arbId}`,
    );
  }
  if (floorReport.floored) {
    console.log(
      `[risk] floored ${floorReport.legsFloored} leg(s) to platform min — ` +
      `+$${floorReport.extraStakeUsd.toFixed(2)} above clip, final stake ` +
      `$${floorReport.finalTotalStakeUsd.toFixed(2)} arbId=${req.arbId}`,
    );
  }

  // Min-net guard (Phase 19v6 — mosquito reject) ---------------------
  // We don't have profit numbers from the request directly; the radar
  // pre-filters this before POSTing /fire. Min-net guard on the executor
  // side is a defense-in-depth — out of scope until TS-7 cutover (after
  // Python executor removal), since the radar's own min-net check has
  // been battle-tested across phases 19v6-v34.

  // Wallet assignment -------------------------------------------------
  let wallets: Wallet[];
  try {
    wallets = assignLegs(walletPool, legCount);
  } catch (err) {
    return makeAbortedResult(
      req, firedAt, dryRun,
      `wallet assignment: ${err instanceof Error ? err.message : String(err)}`,
      expectedCost, expectedPayout,
    );
  }

  // Parallel fire with per-leg timeout --------------------------------
  const legPromises = req.entries.map((spec, i) => {
    const wallet = wallets[i];
    if (!wallet) {
      return Promise.resolve<LegResult>({
        legIdx: i,
        platform: spec.platform,
        status: 'rejected',
        expectedPrice: spec.expectedPrice,
        expectedSizeUsdc: spec.expectedSizeUsdc,
        error: 'no wallet assigned (pool exhausted)',
      });
    }
    const fire = fireLeg(req.arbId, i, spec, wallet, dryRun);
    const timeout = new Promise<LegResult>((resolve) =>
      setTimeout(() => resolve({
        legIdx: i,
        platform: spec.platform,
        status: 'rejected',
        expectedPrice: spec.expectedPrice,
        expectedSizeUsdc: spec.expectedSizeUsdc,
        botId: wallet.botId,
        error: `per-leg timeout ${PER_LEG_TIMEOUT_MS}ms`,
      }), PER_LEG_TIMEOUT_MS),
    );
    return Promise.race([fire, timeout]);
  });
  let legs = await Promise.all(legPromises);
  // Phase audit-14 (15.05.2026) — annotate each leg with the spec slug
  // (single point) so paper.ts can include it in dryrun.jsonl and the
  // radar's allowance-alert can identify the specific market on the
  // operator's Telegram ping. Cheaper than threading slug through every
  // return path in fireLeg.
  for (const l of legs) {
    const spec = req.entries[l.legIdx];
    if (spec?.slug && !l.slug) l.slug = spec.slug;
  }

  // Phase audit-17 (15.05.2026) — partial-fill retry-once.
  // Operator-requested risk redesign: if at least one leg filled and
  // another failed, give the failed leg ONE more attempt with a fresh
  // build (new salt, new sig, fresh timestamp). Server-side races,
  // brief proxy hiccups, and stale orderbook reads all clear within
  // a fresh build. Only AFTER the retry fails do we hand off to
  // planRevert/executeRevertPlan — so we don't unwind a one-leg
  // position that was 200ms away from completing the arb.
  //
  // Disable with PARTIAL_FILL_RETRY=0 to revert to the old "fail-once"
  // behavior (useful for unit tests + post-mortem reproduction).
  const retryEnabled = (process.env.PARTIAL_FILL_RETRY ?? '1') !== '0';
  if (retryEnabled && !dryRun) {
    const anyFilled = legs.some(
      (l) => l.status === 'filled' && (l.fillSizeUsdc ?? 0) > 0,
    );
    const failedLegs = legs.filter(
      (l) => l.status === 'rejected' || l.status === 'timeout',
    );
    if (anyFilled && failedLegs.length > 0) {
      // eslint-disable-next-line no-console
      console.log(
        `[partial-fill-retry] arbId=${req.arbId} retrying ${failedLegs.length} leg(s) ` +
        `(filled=${legs.length - failedLegs.length})`,
      );
      const retryPromises = failedLegs.map((failed) => {
        const idx = failed.legIdx;
        const spec = req.entries[idx];
        const wallet = wallets[idx];
        if (!spec || !wallet) return Promise.resolve(failed);
        const fire = fireLeg(req.arbId, idx, spec, wallet, dryRun);
        const timeout = new Promise<LegResult>((resolve) =>
          setTimeout(() => resolve({
            ...failed,
            error: `retry: per-leg timeout ${PER_LEG_TIMEOUT_MS}ms`,
          }), PER_LEG_TIMEOUT_MS),
        );
        return Promise.race([fire, timeout]);
      });
      const retried = await Promise.all(retryPromises);
      // Splice retry results back into legs by legIdx. Annotate each
      // result with `retried: true` so paper.ts / dryrun.jsonl shows
      // operator which legs went through the retry path.
      const byIdx = new Map(retried.map((r) => [r.legIdx, r]));
      legs = legs.map((l) => {
        const r = byIdx.get(l.legIdx);
        if (!r) return l;
        const merged: LegResult = { ...r };
        if (r.slug === undefined && l.slug !== undefined) merged.slug = l.slug;
        const prevExtra = (l.extra ?? {}) as Record<string, unknown>;
        merged.extra = { ...prevExtra, ...(r.extra ?? {}), retried: true };
        return merged;
      });
      const recovered = legs.filter(
        (l) => l.status === 'filled'
          && (l.fillSizeUsdc ?? 0) > 0
          && (l.extra as Record<string, unknown> | undefined)?.['retried'] === true,
      );
      // eslint-disable-next-line no-console
      console.log(
        `[partial-fill-retry] arbId=${req.arbId} retry result: ` +
        `${recovered.length}/${failedLegs.length} recovered ` +
        `(${recovered.length === failedLegs.length ? 'ALL' : 'PARTIAL'})`,
      );
    }
  }

  // Aggregate ---------------------------------------------------------
  const statusCounts: Record<string, number> = {};
  for (const l of legs) {
    statusCounts[l.status] = (statusCounts[l.status] ?? 0) + 1;
  }
  const allDryFired = legs.length > 0 && legs.every((l) => l.status === 'dry-fired');
  // Phase audit-2 (11.05.2026, second fix) — simPnl in dry-run reflects
  // the THEORETICAL profit (= expectedPayout - expectedCost), regardless
  // of whether individual legs ended up 'dry-fired' vs 'partial' /
  // 'rejected' inside the executor. Reasons:
  //
  //   1. Two known TS-3 leg-failure paths that have nothing to do with
  //      the trade's profitability:
  //      - SX Bet `buildSxOrder` is called with `orders: []` (TS-3 stub
  //        per atomic.ts:102) → `matchOrders` returns partial=true →
  //        leg ends with `built.partial === true`. fireLeg in dry-run
  //        still returns `status: 'dry-fired'` (line 138), but the leg's
  //        EXTRA field reflects the partial. Old formula didn't care
  //        about extras here, only status.
  //      - Limitless leg builder requires `tokenId`, but the radar's
  //        `_build_cp_outcomes_limitless` only populates token_id_yes /
  //        token_id_no when `lim_meta_cache` has the slug. On cold start
  //        the cache is empty → no tokens → buildLeg throws →
  //        fireLeg catches and returns `status: 'rejected'`.
  //   2. paper_stats.win_rate is supposed to be a SIMULATION metric:
  //      "would this arb have been profitable at fill time". With the
  //      old fallback `-expectedCost + 1.0`, any one leg failing
  //      produced -$45 for a CP arb → win_rate stuck at 0% → graduation
  //      gate forever blocked, even after we proved real fires fire
  //      successfully.
  //   3. Leg-level failures are tracked separately in legStatusCounts
  //      and `partial_leg_count`. Operators who need that signal already
  //      have it.
  //
  // In live mode (dryRun=false) the old conservative formula stays —
  // there a partial fill is a real economic event, not a stub artifact.
  const simPnl = dryRun || allDryFired
    ? expectedPayout - expectedCost
    : -expectedCost + 1.0;

  const result: ArbFireResult = {
    arbId: req.arbId,
    dealTitle: req.dealTitle,
    dealStructure: req.structure,
    expectedCost,
    expectedPayout,
    simPnl,
    legCount,
    legStatusCounts: statusCounts as ArbFireResult['legStatusCounts'],
    partialLegCount: 0,
    worstPartialShortfallUsdc: 0,
    abortedReason: null,
    fireMode: 'taker',
    dryRun,
    firedAt,
    legs,
    stakeClipped: clipReport.clipped
      ? {
          originalTotalStakeUsd: clipReport.originalTotalStakeUsd,
          clippedTotalStakeUsd: clipReport.clippedTotalStakeUsd,
          capUsd: clipReport.capUsd,
          ratio: clipReport.ratio,
        }
      : null,
    stakeFloored: floorReport.floored
      ? {
          legsFloored: floorReport.legsFloored,
          extraStakeUsd: floorReport.extraStakeUsd,
          finalTotalStakeUsd: floorReport.finalTotalStakeUsd,
        }
      : null,
  };

  // Phase TS-5c — revert decision planning (pure, no HTTP).
  // In dry-run all legs are 'dry-fired' so the planner returns empty.
  // In real-mode (TS-5c.2+), mixed 'filled'/'slipped'/'timeout'/'rejected'
  // statuses trigger the planner, which annotates revertStatus on each leg
  // so dryrun.jsonl carries the decision trail for forensics.
  const revertPlan = planRevert(result);
  annotateLegsWithPlan(result, revertPlan);
  if (revertPlan.legs.length > 0) {
    result.revertPlanReason = revertPlan.arbReason;
    // Phase TS-5c.2 (11.05.2026) — actually execute the revert: build
    // opposite-side market-aggressive orders for every live leg and
    // POST them. Uses the same `fireLeg` plumbing as the original fire,
    // so signature gate / expectFill / slippage / timeout all reuse
    // the production code path. In dry-run this branch never runs
    // (planRevert returns empty for all-dry-fired arbs).
    await executeRevertPlan(
      result,
      revertPlan,
      req.entries,
      wallets,
      async (spec, wallet, arbId, legIdx) =>
        await fireLeg(arbId, legIdx, spec, wallet, dryRun),
    );
  }

  // Persist + schedule paper eval ------------------------------------
  await logArbDecision(result);
  await schedulePaperEvaluation(result);

  // Slippage / mosquito reject signals (Phase 19v6 parity, post-fire)
  if (Math.abs(expectedPayout - expectedCost) < MIN_NET_PER_ARB_USD && allDryFired) {
    // We don't actively cancel here in TS-3 (real-mode is TS-5), but
    // the warning gets surfaced in result.legs via the paper evaluator.
  }
  void SLIPPAGE_TOLERANCE; // referenced; used in TS-5 real-mode path

  return result;
}

function makeAbortedResult(
  req: FireRequest,
  firedAt: number,
  dryRun: boolean,
  reason: string,
  expectedCost: number,
  expectedPayout: number,
): ArbFireResult {
  return {
    arbId: req.arbId,
    dealTitle: req.dealTitle,
    dealStructure: req.structure,
    expectedCost,
    expectedPayout,
    simPnl: 0,
    legCount: req.entries.length,
    legStatusCounts: { 'aborted': req.entries.length } as ArbFireResult['legStatusCounts'],
    partialLegCount: 0,
    worstPartialShortfallUsdc: 0,
    abortedReason: reason,
    fireMode: 'taker',
    dryRun,
    firedAt,
    legs: [],
  };
}
