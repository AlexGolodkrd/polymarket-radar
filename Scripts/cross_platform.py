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
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from event_matching import find_pairs, match_event, MatchCandidate

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
    """Decide which outcome of A maps to which outcome of B by name.

    Returns (a_yes_pairs_with, b_yes_pairs_with) where 'a_yes_pairs_with'
    indicates which side of B is matched against A's YES.

    Simple case: both events have explicit team names — match by name.
    Fallback: assume order matches (outcome_index 0 → 0).

    For now, return ('opposite', 'opposite') meaning we always pair
    A.YES with B.NO and A.NO with B.YES — this is the standard cross-
    platform inversion (outcome ordering may differ between platforms).

    TODO Phase 14: smart team-name matching using event_matching.canonicalize
    """
    return ('opposite', 'opposite')


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

    # X1: YES_a + NO_b
    # Require both prices + sources whitelisted + sum below threshold
    valid_x1 = (
        out_a.yes_price is not None and out_b.no_price is not None
        and out_a.yes_source != 'implied' and out_b.no_source != 'implied'
        and out_a.yes_depth > 0 and out_b.no_depth > 0
    )
    if valid_x1:
        sum_x1 = out_a.yes_price + out_b.no_price
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

    # X2: NO_a + YES_b (symmetric)
    valid_x2 = (
        out_a.no_price is not None and out_b.yes_price is not None
        and out_a.no_source != 'implied' and out_b.yes_source != 'implied'
        and out_a.no_depth > 0 and out_b.yes_depth > 0
    )
    if valid_x2:
        sum_x2 = out_a.no_price + out_b.yes_price
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

    deals = []
    for a_dict, b_dict, mc in pairs:
        out_a = a_dict['_obj']
        out_b = b_dict['_obj']
        if out_a.platform == out_b.platform:
            continue                          # not cross-platform
        d = build_cross_platform_deal(out_a, out_b, mc.confidence,
                                        threshold=threshold)
        deals.extend(d)
    deals.sort(key=lambda d: d.net_cents, reverse=True)
    return deals


def to_radar_deal_format(cp_deal: CrossPlatformDeal) -> dict:
    """Convert CrossPlatformDeal → dict shape compatible with radar's
    /api/deals output format (so dashboard can display alongside existing
    per-platform deals)."""
    legs_formatted = [
        {
            'name': leg['outcome'],
            'price': leg['price'],
            'price_cents': leg['price_cents'],
            'stake': leg['stake'],
            'liquidity': leg['depth'],
            'source': leg['source'],
            'platform': leg['platform'],
            'side': leg['side'],
        }
        for leg in cp_deal.legs
    ]
    return {
        'title': cp_deal.title,
        'platform': f"{cp_deal.platform_pair[0]}+{cp_deal.platform_pair[1]}",
        'arb_structure': 'cross_platform',
        'cross_structure': cp_deal.structure,        # X1 | X2
        'sum_cents': cp_deal.sum_cents,
        'threshold_cents': cp_deal.threshold_cents,
        'net': round(cp_deal.net_cents / 100 * 50, 2),    # rough $ on $50 stake
        'net_cents': cp_deal.net_cents,
        'entries': legs_formatted,
        'min_liq': min(leg['depth'] for leg in cp_deal.legs),
        'confidence': cp_deal.confidence,
        'end_date': cp_deal.end_date,
        'grade': 'CP-A' if cp_deal.net_cents > 5 else 'CP-B' if cp_deal.net_cents > 2 else 'CP-C',
    }
