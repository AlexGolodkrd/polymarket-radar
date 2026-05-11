"""Cross-platform arbitrage detection (Phase 13, 01.05.2026).

Combines pools from multiple platforms (Polymarket / Limitless / SX Bet)
and finds events that exist on >= 2 platforms simultaneously. For each
matched pair, evaluates two arb structures:

  X1 = YES_a + NO_b  < 1  → BUY YES on platform A, BUY NO on platform B
  X2 = NO_a  + YES_b < 1  → BUY NO  on platform A, BUY YES on platform B

These are the SAME-EVENT-DIFFERENT-PLATFORM arbs. Spread within 1 platform
closes in <100ms (HFT MMs); spread between 2 platforms stays open 5-30s
(MMs not synchronized). This is OUR realistic edge.

ISOLATED from per-platform A/B/C arbs — those continue working unchanged.
Cross-platform is ADDITIVE: each matched pair adds (potentially) 2 deals
to the existing per-platform deal list.

NOT yet wired into main scan loop — operator must explicitly enable via
env CROSS_PLATFORM_ENABLED=1 before this layer activates. See PR #56 for
integration pattern.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import List, Optional, Tuple

from event_matching import (
    find_pairs, detect_market_scope, scopes_compatible,
    outcomes_compatible,
)

log = logging.getLogger(__name__)

# Default threshold for cross-platform sum < threshold (similar to single
# platform but tighter because we have 2 fee structures).
# Polymarket fee = 270bps, Limitless fee = 0bps, SX fee = 200bps (taker)
# Conservative: 0.96 = 1 - max(fee_a + fee_b) - safety
CROSS_PLATFORM_THRESHOLD = float(
    os.environ.get('CROSS_PLATFORM_THRESHOLD', '0.96'))
CROSS_PLATFORM_MIN_NET_USD = float(
    os.environ.get('CROSS_PLATFORM_MIN_NET_USD', '1.0'))
CROSS_PLATFORM_ENABLED = (
    os.environ.get('CROSS_PLATFORM_ENABLED', '0') == '1')

# Phase 19v29b (06.05.2026) — complement-cover threshold.
# Tighter than the 2-leg threshold because each extra leg adds another
# fee bucket and another execution failure mode (depth shock, partial
# fill, settlement-timing skew). Default 0.93 = 1 - (270bps Poly + 2x
# 200bps SX/Lim) - safety. Tunable via env when adding more legs.
CROSS_PLATFORM_COMPLEMENT_THRESHOLD = float(
    os.environ.get('CROSS_PLATFORM_COMPLEMENT_THRESHOLD', '0.93'))
# Minimum number of complement legs (single leg + complement). 3 = single
# A side + 2 B sides (true N≥3 cover); below that the structure collapses
# to the X1/X2 case which has its own builder.
CROSS_PLATFORM_COMPLEMENT_MIN_LEGS = int(
    os.environ.get('CROSS_PLATFORM_COMPLEMENT_MIN_LEGS', '3'))


@dataclass
class PlatformOutcome:
    """One outcome (e.g. 'Lakers Win') on one platform."""
    platform: str                # 'Polymarket' | 'Limitless' | 'SX Bet'
    event_id: str                # platform-specific identifier
    outcome_name: str            # human-readable: 'Lakers', 'YES', etc
    yes_price: Optional[float]
    yes_depth: float
    yes_source: str              # MUST be in REAL_OB_SOURCES
    no_price: Optional[float]
    no_depth: float
    no_source: str
    end_date: Optional[str]
    title: str                   # full event title for display


@dataclass
class CrossPlatformDeal:
    """A cross-platform arb candidate."""
    structure: str               # 'X1' | 'X2'
    title: str                   # combined title
    sum_cents: float
    threshold_cents: float
    net_cents: float
    legs: List[dict]             # each leg has platform/outcome/price/depth/source
    confidence: float            # event-match confidence (0..1)
    platform_pair: Tuple[str, str]
    end_date: Optional[str]
    arb_structure: str = 'cross_platform'


def _outcome_match_cross_platform(
    a: PlatformOutcome, b: PlatformOutcome,
) -> Optional[Tuple[str, str]]:
    """Decide whether outcome A on platform A and outcome B on platform B
    refer to the same real-world side of a market.

    Phase 19v29 (06.05.2026) — was a stub returning ('opposite', 'opposite')
    for every pair, which left a phantom-arb gap: when find_pairs matched
    two outcomes of the same event but for DIFFERENT teams (e.g.
    Polymarket "Santa Fe to win" with SX Bet "Corinthians SP NO" — same
    fixture, different team), build_cross_platform_deal blindly built
    X1/X2. With both sides being 'moneyline' scope, v28's scope guard
    accepted them, and we got 5 deals at "12% net" that were not arbs:
    a tie result lets both legs lose (or both win), breaking the
    full-coverage assumption that X1/X2 require.

    The fix here is the outcome-name guard: before saying "yes, pair A's
    YES with B's NO", we check that A and B name the SAME side. The
    canonicalization in event_matching.canonicalize_outcome_name strips
    YES/NO suffixes, club tags, and handicap numerals so
    'BV Borussia 09 Dortmund' matches 'Borussia Dortmund' and
    'Tottenham Hotspur FC' matches 'Tottenham', while
    'Santa Fe' does not match 'Corinthians SP'.

    Returns ('opposite', 'opposite') iff outcomes refer to the same
    side (the only valid X1/X2 mapping is then A.YES ↔ B.NO and vice
    versa, since same-side YES on both platforms IS the same bet).
    Returns None when outcomes name different sides — caller must
    refuse to build any deal for the pair.
    """
    if outcomes_compatible(a.outcome_name, b.outcome_name):
        return ('opposite', 'opposite')
    return None


def build_cross_platform_deal(
    out_a: PlatformOutcome,
    out_b: PlatformOutcome,
    match_confidence: float,
    threshold: float = None,
    balance_per_leg: float = 50.0,
) -> List[CrossPlatformDeal]:
    """Given two matched outcomes (one per platform), build X1 and/or X2 deals.

    Returns 0, 1, or 2 deals (X1 valid, X2 valid, both, or neither).
    """
    if threshold is None:
        threshold = CROSS_PLATFORM_THRESHOLD
    deals = []

    # Phase 19v28 (06.05.2026) — market-scope guard. Refuse to pair two
    # outcomes whose market scopes differ. Operator screenshot showed 6
    # phantom deals where Polymarket's "Halftime Result" was paired with
    # SX Bet's full-match moneyline / handicap / 1X2 — superficially
    # opposite-side YES/NO but semantically different markets. Each leg
    # could win OR lose under conditions the other leg doesn't cover →
    # not a real arb. Detect scope from title+outcome_name and enforce
    # equality (halftime↔halftime, moneyline↔moneyline, etc.).
    scope_a = detect_market_scope(out_a.title, out_a.outcome_name)
    scope_b = detect_market_scope(out_b.title, out_b.outcome_name)
    if not scopes_compatible(scope_a, scope_b):
        # Incompatible market types — no arb possible regardless of price.
        return deals

    # Phase 19v29 (06.05.2026) — outcome-name guard. Refuses to build X1
    # or X2 when out_a and out_b name DIFFERENT sides of the event (e.g.
    # 'Santa Fe' on Polymarket paired with 'Corinthians SP' on SX Bet
    # for the same Copa Libertadores fixture). Both sides are 'moneyline'
    # scope so v28 accepts them, but the pair is not a real X1/X2 arb:
    # the third 1X2 outcome ('Tie') lets both legs lose simultaneously.
    # Operator's 06.05.2026 screenshot: 5 such phantoms at "12% net" on
    # Santa Fe × Corinthians, all surfaced after v28 unblocked SX from
    # the API breaking changes.
    #
    # Note: returning empty deals here is the conservative choice. The
    # complementary feature — building an N-leg "complement cover" deal
    # when outcomes don't match but together cover the event — lives in
    # build_complement_cover_deal (Phase 19v29b, separate flow).
    if _outcome_match_cross_platform(out_a, out_b) is None:
        return deals

    # X1: YES_a + NO_b
    # Require both prices + sources whitelisted + sum below threshold
    # Phase 19v10 (04.05.2026) — phantom-arb sanity cap:
    # 1. min depth check (mosquito reject) — match per-platform MIN_LEG_LIQ
    # 2. sum > CP_MIN_REALISTIC_SUM — sums below ~50¢ are almost certainly
    #    fuzzy-match phantoms (different events, different resolution
    #    criteria). Real cross-platform arbs are typically 80-99¢ sum
    #    (1-20% edge), not 4¢ (96% edge — operator's XRP $47 phantom).
    _CP_MIN_LEG_DEPTH = 5.0
    _CP_MIN_REALISTIC_SUM = 0.50
    valid_x1 = (
        out_a.yes_price is not None and out_b.no_price is not None
        and out_a.yes_source != 'implied' and out_b.no_source != 'implied'
        and out_a.yes_depth >= _CP_MIN_LEG_DEPTH
        and out_b.no_depth >= _CP_MIN_LEG_DEPTH
    )
    # Phase 19v13 (05.05.2026) — compute sum_x1 once and reuse.
    # Earlier version computed it twice in two adjacent `if valid_x1`
    # blocks; harmless but a code smell hiding the real flow.
    sum_x1 = None
    if valid_x1:
        sum_x1 = out_a.yes_price + out_b.no_price
        if sum_x1 < _CP_MIN_REALISTIC_SUM:
            valid_x1 = False    # phantom — fuzzy-match likely paired wrong events
    if valid_x1 and sum_x1 is not None:
        if sum_x1 < threshold:
            net_cents = (1.0 - sum_x1) * 100  # per $1 contract
            deals.append(CrossPlatformDeal(
                structure='X1',
                title=f"{out_a.title}",
                sum_cents=round(sum_x1 * 100, 2),
                threshold_cents=round(threshold * 100, 2),
                net_cents=round(net_cents, 2),
                legs=[
                    {'platform': out_a.platform,
                     'event_id': out_a.event_id,
                     'outcome': out_a.outcome_name + ' YES',
                     'price': out_a.yes_price,
                     'price_cents': round(out_a.yes_price * 100, 2),
                     'depth': out_a.yes_depth,
                     'source': out_a.yes_source,
                     'side': 'YES',
                     'stake': min(balance_per_leg, out_a.yes_depth)},
                    {'platform': out_b.platform,
                     'event_id': out_b.event_id,
                     'outcome': out_b.outcome_name + ' NO',
                     'price': out_b.no_price,
                     'price_cents': round(out_b.no_price * 100, 2),
                     'depth': out_b.no_depth,
                     'source': out_b.no_source,
                     'side': 'NO',
                     'stake': min(balance_per_leg, out_b.no_depth)},
                ],
                confidence=match_confidence,
                platform_pair=(out_a.platform, out_b.platform),
                end_date=out_a.end_date or out_b.end_date,
            ))

    # X2: NO_a + YES_b (symmetric — same Phase 19v10 sanity guards)
    valid_x2 = (
        out_a.no_price is not None and out_b.yes_price is not None
        and out_a.no_source != 'implied' and out_b.yes_source != 'implied'
        and out_a.no_depth >= _CP_MIN_LEG_DEPTH
        and out_b.yes_depth >= _CP_MIN_LEG_DEPTH
    )
    # Phase 19v13 (05.05.2026) — same dedup as X1 above.
    sum_x2 = None
    if valid_x2:
        sum_x2 = out_a.no_price + out_b.yes_price
        if sum_x2 < _CP_MIN_REALISTIC_SUM:
            valid_x2 = False
    if valid_x2 and sum_x2 is not None:
        if sum_x2 < threshold:
            net_cents = (1.0 - sum_x2) * 100
            deals.append(CrossPlatformDeal(
                structure='X2',
                title=f"{out_a.title}",
                sum_cents=round(sum_x2 * 100, 2),
                threshold_cents=round(threshold * 100, 2),
                net_cents=round(net_cents, 2),
                legs=[
                    {'platform': out_a.platform,
                     'event_id': out_a.event_id,
                     'outcome': out_a.outcome_name + ' NO',
                     'price': out_a.no_price,
                     'price_cents': round(out_a.no_price * 100, 2),
                     'depth': out_a.no_depth,
                     'source': out_a.no_source,
                     'side': 'NO',
                     'stake': min(balance_per_leg, out_a.no_depth)},
                    {'platform': out_b.platform,
                     'event_id': out_b.event_id,
                     'outcome': out_b.outcome_name + ' YES',
                     'price': out_b.yes_price,
                     'price_cents': round(out_b.yes_price * 100, 2),
                     'depth': out_b.yes_depth,
                     'source': out_b.yes_source,
                     'side': 'YES',
                     'stake': min(balance_per_leg, out_b.yes_depth)},
                ],
                confidence=match_confidence,
                platform_pair=(out_a.platform, out_b.platform),
                end_date=out_a.end_date or out_b.end_date,
            ))

    return deals


# ──────────────────────────────────────────────────────────────────────
# Phase 19v29b (06.05.2026) — complement-cover N-leg deals.
#
# Background: SX Bet exposes 1X2 markets as 3 separate binary outcomes
# (e.g. 'Santa Fe' / 'Corinthians SP' / 'Tie' for one Copa Libertadores
# fixture). When Polymarket has a single-outcome bet on the same fixture
# (say 'Santa Fe to win' as a binary YES/NO), we can cover the entire
# event by buying YES on the Polymarket leg AND YES on every OTHER SX
# outcome. If the sum of all YES prices is below 1.0 (after fees), the
# bet is a guaranteed-payout arb — exactly $1 face value at any matchA
# resolution.
#
# Stake sizing: each leg stakes its own price share, so a $F face-value
# bundle costs $F * sum(prices). Profit at any single-outcome resolution
# is $F * (1 - sum). Min net check uses the same threshold as X1/X2 but
# tightened (default 0.93) to account for 3 fee buckets and execution risk.
#
# CRITICAL contrast with v29a: v29a refuses cross-team pairs because they
# don't cover all outcomes. v29b accepts cross-team SETS because together
# the set DOES cover all outcomes. The two are complementary safety
# checks, not contradictions: a 2-leg pair with mismatched outcomes is a
# phantom; a 3+ leg set with mismatched outcomes BUT covering every
# outcome is a real arb.
# ──────────────────────────────────────────────────────────────────────


def build_complement_cover_deal(
    single_outcome: PlatformOutcome,
    other_outcomes: List[PlatformOutcome],
    *,
    threshold: Optional[float] = None,
    balance_per_leg: float = 50.0,
    match_confidence: float = 1.0,
) -> Optional[CrossPlatformDeal]:
    """Build an N-leg complement-cover arb if the prices justify it.

    Inputs:
      single_outcome — one PlatformOutcome from platform A (the
                       'anchor' leg) on which we'll buy YES.
      other_outcomes — every PlatformOutcome on the OTHER platform B
                       that belongs to the SAME real-world fixture.
                       Must include all 1X2 sides (or all multi-outcome
                       binary sides). Caller is responsible for grouping.

    Process:
      1. Identify the sibling on B that names the same side as A
         (via outcomes_compatible). If none → return None: the other
         platform doesn't carry this side, complement isn't possible.
      2. Complement set = every B outcome except the matched sibling.
         If complement has < (CP_COMPLEMENT_MIN_LEGS - 1) legs → None.
      3. Scope guard — every leg's market scope must equal A's.
      4. Source/depth gate — every leg must have a real (non-implied)
         YES price and depth ≥ MIN_LEG_DEPTH.
      5. Sum YES prices: A.yes + Σ(other complement YES prices). If
         sum >= threshold → not an arb, return None.
      6. Build a deal with structure='cp_complement_cover'. Net_cents =
         (1.0 - sum) * 100 per $1 face value.

    Why we require the matching sibling on B: without it we couldn't
    verify that the complement actually covers all outcomes — there
    might be a hidden 4th outcome we didn't account for. With the
    matching sibling visible, we know B exposes exactly the same set
    of outcomes as A, just split across separate binary markets.
    """
    if threshold is None:
        threshold = CROSS_PLATFORM_COMPLEMENT_THRESHOLD
    if not other_outcomes:
        return None
    if single_outcome.yes_price is None or single_outcome.yes_source == 'implied':
        return None

    # Find the matching sibling on the other platform
    matching = [
        o for o in other_outcomes
        if outcomes_compatible(single_outcome.outcome_name, o.outcome_name)
    ]
    if not matching:
        return None
    matching_ids = {id(o) for o in matching}
    complement = [o for o in other_outcomes if id(o) not in matching_ids]
    # Need ≥ (MIN_LEGS - 1) sibling outcomes to count as a real cover.
    # Below that the structure collapses to X1/X2 which is handled by
    # build_cross_platform_deal.
    needed_complement = max(2, CROSS_PLATFORM_COMPLEMENT_MIN_LEGS - 1)
    if len(complement) < needed_complement:
        return None

    # Scope guard — must all share the anchor's scope
    scope_anchor = detect_market_scope(
        single_outcome.title, single_outcome.outcome_name)
    for c in complement:
        scope_c = detect_market_scope(c.title, c.outcome_name)
        if not scopes_compatible(scope_anchor, scope_c):
            return None

    _MIN_LEG_DEPTH = 5.0
    if single_outcome.yes_depth < _MIN_LEG_DEPTH:
        return None

    total = single_outcome.yes_price
    legs = [
        {
            'platform': single_outcome.platform,
            'event_id': single_outcome.event_id,
            'outcome': single_outcome.outcome_name + ' YES',
            'price': single_outcome.yes_price,
            'price_cents': round(single_outcome.yes_price * 100, 2),
            'depth': single_outcome.yes_depth,
            'source': single_outcome.yes_source,
            'side': 'YES',
            'stake': min(balance_per_leg, single_outcome.yes_depth),
        }
    ]

    other_platform = None
    for c in complement:
        if c.yes_price is None or c.yes_source == 'implied':
            return None
        if c.yes_depth < _MIN_LEG_DEPTH:
            return None
        if other_platform is None:
            other_platform = c.platform
        total += c.yes_price
        legs.append(
            {
                'platform': c.platform,
                'event_id': c.event_id,
                'outcome': c.outcome_name + ' YES',
                'price': c.yes_price,
                'price_cents': round(c.yes_price * 100, 2),
                'depth': c.yes_depth,
                'source': c.yes_source,
                'side': 'YES',
                'stake': min(balance_per_leg, c.yes_depth),
            }
        )

    if total >= threshold:
        return None

    net_cents = (1.0 - total) * 100   # per $1 face value
    return CrossPlatformDeal(
        structure='cp_complement_cover',
        title=single_outcome.title,
        sum_cents=round(total * 100, 2),
        threshold_cents=round(threshold * 100, 2),
        net_cents=round(net_cents, 2),
        legs=legs,
        confidence=match_confidence,
        platform_pair=(single_outcome.platform, other_platform or 'unknown'),
        end_date=single_outcome.end_date
                  or (complement[0].end_date if complement else None),
    )


def _group_by_fixture(
    pool: List[PlatformOutcome],
) -> dict:
    """Group outcomes by canonicalized fixture title.

    Used by complement-cover discovery — for each anchor outcome on
    platform A, look up all outcomes on platform B that share the same
    fixture title (as canonicalized via normalize_title +
    canonicalize_teams). This handles SX Bet's 3-binary 1X2 split
    (3 different event_ids but identical fixture title).
    """
    from collections import defaultdict
    from event_matching import normalize_title, canonicalize_teams
    groups: dict = defaultdict(list)
    for o in pool:
        norm = normalize_title(o.title or '')
        canon, _ = canonicalize_teams(norm)
        if canon:
            groups[canon].append(o)
    return groups


def _find_complement_cover_arbs(
    pool_a: List[PlatformOutcome],
    pool_b: List[PlatformOutcome],
    *,
    threshold: Optional[float] = None,
) -> List[CrossPlatformDeal]:
    """Iterate pool_a × fixture-groups in pool_b looking for complement
    covers. Each anchor outcome in A maps to at most one B fixture group
    (the one whose canonicalized title matches A's). Returns deals only
    when the cover is mathematically valid (sum < threshold).

    Symmetry note: this function checks A → B direction. For full
    coverage callers should run it twice (A→B and B→A). find_cross_platform_arbs
    does that below.
    """
    deals: List[CrossPlatformDeal] = []
    if not pool_a or not pool_b:
        return deals
    groups_b = _group_by_fixture(pool_b)
    if not groups_b:
        return deals
    from event_matching import normalize_title, canonicalize_teams
    for anchor in pool_a:
        norm = normalize_title(anchor.title or '')
        canon, _ = canonicalize_teams(norm)
        if not canon:
            continue
        siblings = groups_b.get(canon)
        if not siblings:
            continue
        # Same-platform safety: anchor must be on a different platform
        # than every sibling we'll mix into the complement.
        if all(s.platform == anchor.platform for s in siblings):
            continue
        # Scope+settlement timing already gated inside builder; just call it.
        deal = build_complement_cover_deal(
            anchor, siblings, threshold=threshold,
        )
        if deal is not None:
            deals.append(deal)
    return deals


# Phase 16+ (01.05.2026) — settlement timing check.
# Cross-platform arbs are exposed to settlement-timing risk: Polymarket may
# resolve event 1 hour BEFORE Limitless (different oracles, different UMA
# windows). During that window we hold a directional position. If the gap
# is tiny (< few hours) we accept it; if large (> 24h) reject the pair.
SETTLEMENT_TIMING_TOLERANCE_HOURS = float(
    os.environ.get('CROSS_PLATFORM_SETTLEMENT_TIMING_HOURS', '24'))


def _check_settlement_timing(out_a, out_b) -> tuple:
    """Check if both events resolve close enough in time. Returns (ok, reason).
    Both end_dates parsed from ISO strings or unix ms; if either is missing
    → ok (best-effort, can't enforce without data)."""
    from datetime import datetime, timezone
    def _parse(v):
        if v is None: return None
        if isinstance(v, (int, float)):
            ts = v / 1000 if v > 1e12 else v
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        if isinstance(v, str):
            try:
                return datetime.fromisoformat(v.replace('Z', '+00:00'))
            except Exception:
                return None
        return None
    da = _parse(out_a.end_date)
    db = _parse(out_b.end_date)
    if da is None or db is None:
        return True, 'missing_end_date'                 # best-effort accept
    delta = abs((da - db).total_seconds()) / 3600.0
    if delta > SETTLEMENT_TIMING_TOLERANCE_HOURS:
        return False, f'settlement_delta_{delta:.1f}h_>_{SETTLEMENT_TIMING_TOLERANCE_HOURS}h'
    return True, f'settlement_delta_{delta:.1f}h_OK'


# Phase audit (11.05.2026) — SZ-4. Per-call diagnostics so the operator
# can answer "why are cross-platform arbs the ONLY arbs we see?" — i.e.
# how many candidate pairs were found, how many were rejected at each
# stage (same-platform, settlement-timing, deal-build-zero). Reset each
# call to find_cross_platform_arbs.
_pairing_diag: dict = {
    'last_call_ts': None,
    'pool_a_size': 0,
    'pool_b_size': 0,
    'pairs_found': 0,
    'rejected_same_platform': 0,
    'rejected_settlement_timing': 0,
    'deals_built': 0,
    'complement_cover_built': 0,
    'errors': 0,
}


def get_pairing_diag() -> dict:
    """Read-only snapshot of last find_cross_platform_arbs invocation."""
    return dict(_pairing_diag)


def find_cross_platform_arbs(
    pool_a: List[PlatformOutcome],
    pool_b: List[PlatformOutcome],
    *,
    min_confidence: float = 0.85,
    threshold: float = None,
) -> List[CrossPlatformDeal]:
    """Top-level entry. Iterates pool_a × pool_b, fuzzy-matches events,
    and for each high-confidence pair builds cross-platform deals.

    `pool_a` and `pool_b` are lists of PlatformOutcome (one per outcome
    per platform). Caller responsible for building these from per-platform
    scan results.

    Returns sorted list of deals (highest net first).
    """
    import time as _time
    _pairing_diag['last_call_ts'] = _time.time()
    _pairing_diag['pool_a_size'] = len(pool_a)
    _pairing_diag['pool_b_size'] = len(pool_b)
    _pairing_diag['rejected_same_platform'] = 0
    _pairing_diag['rejected_settlement_timing'] = 0
    _pairing_diag['deals_built'] = 0
    _pairing_diag['complement_cover_built'] = 0
    _pairing_diag['errors'] = 0
    # Convert PlatformOutcome to dict for find_pairs (which expects dict)
    list_a_dicts = [
        {'_obj': o, 'title': o.title, 'end_date': o.end_date}
        for o in pool_a
    ]
    list_b_dicts = [
        {'_obj': o, 'title': o.title, 'end_date': o.end_date}
        for o in pool_b
    ]
    pairs = find_pairs(list_a_dicts, list_b_dicts,
                        min_confidence=min_confidence)
    _pairing_diag['pairs_found'] = len(pairs)

    deals = []
    for a_dict, b_dict, mc in pairs:
        out_a = a_dict['_obj']
        out_b = b_dict['_obj']
        if out_a.platform == out_b.platform:
            _pairing_diag['rejected_same_platform'] += 1
            continue                          # not cross-platform
        # Phase 16+ (01.05.2026): settlement timing gate
        ok, reason = _check_settlement_timing(out_a, out_b)
        if not ok:
            _pairing_diag['rejected_settlement_timing'] += 1
            log.info("cp pair rejected: %s (%s vs %s)",
                     reason, out_a.platform, out_b.platform)
            continue
        d = build_cross_platform_deal(out_a, out_b, mc.confidence,
                                        threshold=threshold)
        _pairing_diag['deals_built'] += len(d)
        deals.extend(d)

    # Phase 19v29b (06.05.2026) — complement-cover discovery. Run after
    # X1/X2 so that simple 2-leg arbs are surfaced first; then look for
    # N-leg covers across platform-A → platform-B fixture groups (and
    # symmetrically B → A). De-dup is left to the caller — radar-side
    # /api/deals already keys by (title, structure, legs-set).
    try:
        before = len(deals)
        deals.extend(_find_complement_cover_arbs(pool_a, pool_b))
        deals.extend(_find_complement_cover_arbs(pool_b, pool_a))
        _pairing_diag['complement_cover_built'] += len(deals) - before
    except Exception:
        # Complement cover is additive; failure here must NOT regress
        # X1/X2 detection above. Log + continue.
        _pairing_diag['errors'] += 1
        log.exception("complement-cover scan failed; ignoring")

    deals.sort(key=lambda d: d.net_cents, reverse=True)
    return deals


# Phase 19v34 (09.05.2026) — per-platform taker-fee defaults, used by
# `to_radar_deal_format` to compute fee/gross/roi for cross-platform
# deals. Mirrors `THETA_*` constants in arb_server.py:
#   - Polymarket: 250 bps (V2 default; per-market override possible)
#   - SX Bet:     200 bps (taker)
#   - Limitless:  0 bps   (no fee)
#   - Kalshi:     700 bps (variance fee — disabled but kept for symmetry)
PLATFORM_THETA = {
    'Polymarket': 0.025,
    'SX Bet': 0.02,
    'Limitless': 0.0,
    'Kalshi': 0.07,
}


def _platform_theta(platform: str) -> float:
    """Look up taker-fee multiplier for a platform; default 250 bps if
    we ever encounter a new platform name (defensive)."""
    return PLATFORM_THETA.get(platform, 0.025)


def to_radar_deal_format(cp_deal: CrossPlatformDeal) -> dict:
    """Convert CrossPlatformDeal → dict shape compatible with radar's
    /api/deals output format (so dashboard can display alongside existing
    per-platform deals)."""
    # Phase 19v10 (04.05.2026) — net based on actual stake, not assumed $50.
    # Stake = min(leg depth across both legs, $55 per-trade cap). Net $ =
    # actual_stake × (net_cents / 100) — accurate paper-trade economics.
    #
    # Phase 19v13 (05.05.2026) — guard against empty `legs`. Earlier code
    # called `min(min(leg['depth'] for leg in cp_deal.legs), 55.0)` which
    # raises `ValueError: min() arg is an empty sequence` on a malformed
    # CrossPlatformDeal (no builder path produces one today, but defensive
    # programming — also makes unit-testing the formatter safer).
    if cp_deal.legs:
        min_leg_depth = min(leg['depth'] for leg in cp_deal.legs)
    else:
        min_leg_depth = 0.0
    # Phase audit-2 (11.05.2026) — BUG-E5: depth safety factor.
    # Operator observation: if min_leg_depth=$23 and we try to fill the
    # FULL $23, race losses (someone else takes $5 of the book before us)
    # cause partial fills → leg #1 fills $18 but leg #2 fires the full
    # $23 → imbalanced position → not a true arb anymore.
    # Fix: keep 20% buffer (factor 0.8). Cap remains $55 per leg per trade.
    _CP_DEPTH_SAFETY_FACTOR = float(
        os.environ.get('CP_DEPTH_SAFETY_FACTOR', '0.8'))
    safe_min_depth = min_leg_depth * _CP_DEPTH_SAFETY_FACTOR
    actual_face = min(safe_min_depth, 55.0)
    # `actual_face` is the FACE VALUE of the arb (= max payout per leg
    # if that leg wins, in units of $1-contracts). Both legs buy the
    # SAME number of contracts so that whichever leg wins, the payout
    # equals `actual_face` — the canonical equal-payout arb sizing.
    actual_stake = actual_face  # alias for backward compat (gross_dollars math below)
    # Phase audit-2 (11.05.2026) — BUG-E6: equal-payout sizing.
    # Previously per-leg `stake` was rendered as `actual_stake` (the face
    # value), making all legs show the same $-amount. Operator correctly
    # pointed out this isn't how arb sizing works — for a true arb:
    #   - face value (= contracts owned) is EQUAL across legs (= guaranteed
    #     payout if that leg wins)
    #   - capital deployed per leg is DIFFERENT (= face × leg_price)
    #
    # Example Charlotte FC (face=$23):
    #   Leg YES @ 43¢: 23 contracts × $0.43 = $9.89 capital, pays $23 on YES win
    #   Leg NO  @ 47¢: 23 contracts × $0.47 = $10.81 capital, pays $23 on NO win
    #   Total capital: $20.70, guaranteed payout: $23, profit: $2.30 either way
    #
    # Dashboard semantics:
    #   - 'stake'     = capital deployed on THIS leg = face × leg_price
    #   - 'contracts' = face value (same for all legs in binary CP arb)
    legs_formatted = [
        {
            'name': leg['outcome'],
            'price': leg['price'],
            'price_cents': leg['price_cents'],
            # Capital deployed on this leg (DIFFERENT per leg — proper arb sizing)
            'stake': round(actual_face * (leg.get('price') or 0), 2),
            # Face value bought on this leg (SAME across legs — payout if leg wins)
            'contracts': round(actual_face, 2),
            'liquidity': leg['depth'],
            'source': leg['source'],
            'platform': leg['platform'],
            'side': leg['side'],
        }
        for leg in cp_deal.legs
    ]
    # `net_cents` is per-$1-face profit (= 100 - sum_cents). actual_stake
    # is interpreted as face-value cap, so dollars-on-the-table for one
    # face unit is `actual_stake * net_cents / 100`. This is GROSS profit
    # — we subtract fees below to get true net.
    gross_dollars = round(actual_stake * cp_deal.net_cents / 100, 2)

    # Phase 19v34 (09.05.2026) — fee/gross/roi parity with per-platform.
    # Operator's screenshot showed cross-platform card with all four UI
    # columns at 0% (GROSS / FEE / ROI / ROI ADJ). Root cause: this
    # formatter never wrote `gross_pct`/`fee_pct`/`roi`/`adj_roi` so the
    # dashboard read undefined → displayed 0%. Now we compute them in
    # the same spirit as build_deal() in arb_server.py:1755:
    #
    #   total_cash    = face × sum_prices  (capital actually deployed
    #                                       at fire — sum of per-leg
    #                                       price × face)
    #   gross_dollars = face × (1 − sum_prices)  (worst-case payout
    #                                              minus capital, =
    #                                              guaranteed profit
    #                                              before fees on a
    #                                              correctly-balanced
    #                                              cross-platform arb)
    #   fee           = Σ leg_cash × theta_leg
    #                 = Σ (face × leg.price × theta_leg)
    #   net_dollars   = gross_dollars − fee
    #   roi_pct       = net_dollars / total_cash × 100
    #
    # roi_adj subtracts a slippage estimate based on the smallest-depth
    # leg (mirrors the per-platform `slip_pct` heuristic).
    sum_fraction = (cp_deal.sum_cents or 0) / 100.0  # e.g. 91.62¢ → 0.9162
    if sum_fraction > 0:
        total_cash = actual_stake * sum_fraction       # cash deployed
    else:
        total_cash = 0.0

    total_fee = 0.0
    for leg in cp_deal.legs:
        leg_cash = actual_stake * (leg.get('price') or 0)
        leg_theta = _platform_theta(leg.get('platform', ''))
        total_fee += leg_cash * leg_theta

    net_dollars = gross_dollars - total_fee
    if total_cash > 0:
        gross_pct = gross_dollars / total_cash * 100
        fee_pct = total_fee / total_cash * 100
        roi_pct = net_dollars / total_cash * 100
    else:
        gross_pct = fee_pct = roi_pct = 0.0

    # Slippage estimate (same spirit as build_deal):
    #   slip_pct = max(stake_per_leg) / min_leg_depth × 100, capped at 5%
    if cp_deal.legs and min_leg_depth > 0:
        max_leg_stake = max(
            (actual_stake * (leg.get('price') or 0)) for leg in cp_deal.legs
        )
        slip_pct = min(5.0, max_leg_stake / min_leg_depth * 100)
    else:
        slip_pct = 5.0
    slip_cost = total_cash * slip_pct / 100 if total_cash > 0 else 0.0
    adj_dollars = net_dollars - slip_cost
    roi_adj_pct = (adj_dollars / total_cash * 100) if total_cash > 0 else 0.0

    # `theta` field: report worst (highest) per-leg theta so the UI's
    # Theta column matches conservative pricing assumptions.
    if cp_deal.legs:
        theta_max = max(
            _platform_theta(leg.get('platform', '')) for leg in cp_deal.legs
        )
    else:
        theta_max = 0.0

    return {
        'title': cp_deal.title,
        'platform': f"{cp_deal.platform_pair[0]}+{cp_deal.platform_pair[1]}",
        'arb_structure': 'cross_platform',
        'cross_structure': cp_deal.structure,        # X1 | X2 | cp_complement_cover
        'sum_cents': cp_deal.sum_cents,
        # Phase 19v32 (08.05.2026) — also surface as `total_cents` for UI
        # parity. The active-deals widget in dashboard.html (`d.total_cents`)
        # and Polymarket's `_quality_ok` function both read `total_cents`;
        # writing it on CP deals too means CP rows get the same Sum column
        # display + are subject to the same tight-arb quality gate.
        'total_cents': cp_deal.sum_cents,
        'threshold_cents': cp_deal.threshold_cents,
        # Phase 19v34 — `net` now subtracts fee. Operator-visible "Net"
        # in dashboard now reflects realistic post-fee profit, not gross.
        'net': round(net_dollars, 2),
        'net_cents': cp_deal.net_cents,
        # Phase 19v34 — UI parity with per-platform deals
        'gross': round(gross_dollars, 2),
        'gross_pct': round(gross_pct, 2),
        'fee': round(total_fee, 4),
        'fee_pct': round(fee_pct, 2),
        'roi': round(roi_pct, 1),
        'adj': round(adj_dollars, 2),
        'adj_roi': round(roi_adj_pct, 1),
        'slip_pct': round(slip_pct, 2),
        'slip_cost': round(slip_cost, 2),
        'theta': round(theta_max, 4),
        'balance_used': round(actual_stake, 2),
        'entries': legs_formatted,
        'min_liq': min_leg_depth,
        'confidence': cp_deal.confidence,
        'end_date': cp_deal.end_date,
        # Grade reflects ROI on deployed capital (post-fee, post-slippage).
        # Cross-platform tends to be tight (1-5%), so the grade ladder is
        # narrower than per-platform: A>2%, B>1%, C>0%, F<=0%.
        'grade': (
            'CP-A' if roi_adj_pct > 2 else
            'CP-B' if roi_adj_pct > 1 else
            'CP-C' if roi_adj_pct > 0 else
            'CP-F'
        ),
    }
