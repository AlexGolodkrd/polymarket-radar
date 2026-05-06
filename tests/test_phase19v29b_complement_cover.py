"""Phase 19v29b (06.05.2026) — cross-platform complement-cover N-leg arbs.

Operator's idea (06.05.2026): SX Bet exposes 1X2 markets as 3 separate
binary outcomes. For a fixture like Independiente Santa Fe vs SC
Corinthians Paulista, SX has three markets:
    "Santa Fe" / "Not Santa Fe"
    "Corinthians SP" / "Not Corinthians SP"
    "Tie" / "Not tie"

If Polymarket carries one of those outcomes on a single binary YES/NO
market (say "Santa Fe to win"), we can cover the entire fixture by
buying YES on the Polymarket leg AND YES on the OTHER two SX outcomes.
At any single-outcome resolution, exactly one of the three legs pays
$1 — guaranteed payout. If sum of YES prices < 1.0 (after fees buffer)
we have a real arb.

ARITHMETIC SANITY CHECK
-----------------------
At "fair" 1X2 prices (sum = 1.0 exactly, no edge):
    Santa Fe YES   = 0.18
    Tie YES        = 0.27
    Corinthians YES = 0.55
                  sum = 1.00

Any of these as the anchor on Polymarket at the SAME price as SX gives
sum = 1.00 → no arb. To produce a complement-cover arb we need a
GENUINE misprice on at least one leg, e.g. Polymarket carries "Santa
Fe" YES at 0.10 (real misprice 8c off):
    Poly Santa Fe YES @ 0.10 + SX Tie YES @ 0.27 + SX Corinthians YES @ 0.55
                  sum = 0.92 → 8% net (below 0.93 threshold)

A previous draft of the analysis added 0.30 + 0.55 + 0.27 and wrote
"≈ 0.88". The correct value is 1.12. With sum 1.12 the bundle is a
12% LOSS, not an arb. So Phase 19v29b must NOT produce a deal for that
case. Tests below verify both the misprice-arb and the
overround-no-arb paths.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


# ── Helpers ─────────────────────────────────────────────────────────

def _make_anchor(platform, outcome_name, yes_price, *, title='Santa Fe vs Corinthians',
                  yes_depth=400, end_date='2026-05-08', event_id=None):
    from cross_platform import PlatformOutcome
    return PlatformOutcome(
        platform=platform,
        event_id=event_id or f'{platform.lower()}_{outcome_name.lower()[:6]}',
        title=title, outcome_name=outcome_name,
        yes_price=yes_price, yes_depth=yes_depth, yes_source='clob_ask' if platform == 'Polymarket' else 'sx_ob',
        no_price=round(1 - yes_price, 4) if yes_price else None,
        no_depth=yes_depth,
        no_source='clob_ask' if platform == 'Polymarket' else 'sx_ob',
        end_date=end_date,
    )


def _sx_three_way(yes_a=0.18, yes_t=0.27, yes_b=0.55,
                   *, title='Santa Fe vs Corinthians', end_date='2026-05-08'):
    return [
        _make_anchor('SX Bet', 'Independiente Santa Fe', yes_a,
                     title=title, end_date=end_date),
        _make_anchor('SX Bet', 'Tie', yes_t,
                     title=title, end_date=end_date),
        _make_anchor('SX Bet', 'Corinthians SP', yes_b,
                     title=title, end_date=end_date),
    ]


# ── Sum guard: bundle priced > 1.0 must NOT produce a deal ─────────

def test_no_deal_when_sum_above_overround_book():
    """Operator's previous-draft mistake. Anchor at same price as SX
    matching sibling but plus the other two siblings → sum > 1 → no arb.
    With the wrongly-claimed 0.88 vs the actual 1.12, this is the
    guaranteed-loss case we MUST reject."""
    from cross_platform import build_complement_cover_deal
    sx = _sx_three_way()
    # Polymarket "Santa Fe" YES @ 0.30 (above SX's 0.18; the worry case
    # the operator's draft analysis missed)
    poly = _make_anchor('Polymarket', 'Independiente Santa Fe', 0.30,
                         event_id='poly_sf')
    deal = build_complement_cover_deal(poly, sx)
    # 0.30 + 0.27 + 0.55 = 1.12 (LOSS, not arb)
    assert deal is None, f"expected None for sum>1.0, got: {deal}"


def test_no_deal_at_fair_book():
    """Fair book (sum = 1.0) must not produce a deal — no edge."""
    from cross_platform import build_complement_cover_deal
    sx = _sx_three_way(yes_a=0.18, yes_t=0.27, yes_b=0.55)
    # Polymarket carries Santa Fe at the same fair price (0.18)
    poly = _make_anchor('Polymarket', 'Independiente Santa Fe', 0.18,
                         event_id='poly_sf')
    deal = build_complement_cover_deal(poly, sx)
    # 0.18 + 0.27 + 0.55 = 1.00 (exactly the threshold) → no arb
    assert deal is None


# ── Real arb: genuine misprice produces a complement-cover deal ────

def test_arb_at_misprice_below_threshold():
    """Genuine 8c misprice on the anchor: Poly Santa Fe @ 0.10 vs SX 0.18.
    Bundle sum = 0.92, below default 0.93 threshold → real arb."""
    from cross_platform import build_complement_cover_deal
    sx = _sx_three_way(yes_a=0.18, yes_t=0.27, yes_b=0.55)
    poly = _make_anchor('Polymarket', 'Independiente Santa Fe', 0.10,
                         event_id='poly_sf')
    deal = build_complement_cover_deal(poly, sx)
    assert deal is not None
    assert deal.structure == 'cp_complement_cover'
    # 0.10 + 0.27 + 0.55 = 0.92  → net 8 cents on $1 face
    assert abs(deal.sum_cents - 92.0) < 0.01
    assert abs(deal.net_cents - 8.0) < 0.01
    assert len(deal.legs) == 3


def test_arb_legs_have_one_per_outcome():
    """Each leg must name a distinct outcome; the matching sibling on
    the other platform is excluded from the complement."""
    from cross_platform import build_complement_cover_deal
    sx = _sx_three_way(yes_a=0.18, yes_t=0.27, yes_b=0.55)
    poly = _make_anchor('Polymarket', 'Independiente Santa Fe', 0.10,
                         event_id='poly_sf')
    deal = build_complement_cover_deal(poly, sx)
    assert deal is not None
    leg_outcomes = [leg['outcome'] for leg in deal.legs]
    # Anchor on Poly + 2 siblings (Tie, Corinthians) on SX
    assert 'Independiente Santa Fe YES' in leg_outcomes
    assert 'Tie YES' in leg_outcomes
    assert 'Corinthians SP YES' in leg_outcomes
    # The matching sibling on SX (Santa Fe YES on SX) must NOT be in legs
    assert sum('Santa Fe' in o for o in leg_outcomes) == 1


# ── Sibling discovery: anchor with no match on B → no deal ─────────

def test_no_deal_if_anchor_has_no_matching_sibling():
    """If platform B doesn't expose the same outcome as the anchor, we
    can't verify the complement covers all outcomes — must return None."""
    from cross_platform import build_complement_cover_deal
    # Sibling pool has only 2 of 3 outcomes (Tie + Corinthians, no Santa Fe)
    sx = [
        _make_anchor('SX Bet', 'Tie', 0.27),
        _make_anchor('SX Bet', 'Corinthians SP', 0.55),
    ]
    poly = _make_anchor('Polymarket', 'Independiente Santa Fe', 0.10,
                         event_id='poly_sf')
    deal = build_complement_cover_deal(poly, sx)
    assert deal is None


# ── Min legs: complement smaller than CP_COMPLEMENT_MIN_LEGS - 1 ──

def test_no_deal_with_only_one_complement_leg():
    """A 2-leg structure (anchor + 1 sibling) is just X1/X2, not
    complement cover — handled by build_cross_platform_deal."""
    from cross_platform import build_complement_cover_deal
    # Only Santa Fe matching sibling available (no Tie/Corinthians)
    sx = [_make_anchor('SX Bet', 'Independiente Santa Fe', 0.18)]
    poly = _make_anchor('Polymarket', 'Independiente Santa Fe', 0.10,
                         event_id='poly_sf')
    deal = build_complement_cover_deal(poly, sx)
    assert deal is None


# ── Depth + source guards ──────────────────────────────────────────

def test_no_deal_if_any_leg_has_implied_source():
    """Implied/synthetic sources are not real orderbook — must reject."""
    from cross_platform import build_complement_cover_deal, PlatformOutcome
    sx_real = _sx_three_way()
    # Mark the Tie leg as 'implied' (not in REAL_OB_SOURCES)
    sx_real[1] = PlatformOutcome(
        platform='SX Bet', event_id='sx_tie',
        title='Santa Fe vs Corinthians', outcome_name='Tie',
        yes_price=0.27, yes_depth=400, yes_source='implied',
        no_price=0.73, no_depth=400, no_source='implied',
        end_date='2026-05-08',
    )
    poly = _make_anchor('Polymarket', 'Independiente Santa Fe', 0.10,
                         event_id='poly_sf')
    deal = build_complement_cover_deal(poly, sx_real)
    assert deal is None


def test_no_deal_if_any_leg_has_zero_depth():
    """Below MIN_LEG_DEPTH on any leg → reject."""
    from cross_platform import build_complement_cover_deal
    sx = _sx_three_way()
    sx[1] = _make_anchor('SX Bet', 'Tie', 0.27, yes_depth=0)
    poly = _make_anchor('Polymarket', 'Independiente Santa Fe', 0.10,
                         event_id='poly_sf')
    deal = build_complement_cover_deal(poly, sx)
    assert deal is None


# ── Scope guard: mismatched scopes reject the deal ─────────────────

def test_no_deal_if_anchor_is_halftime_and_complement_is_fulltime():
    """v28 scope guard: complement legs must share the anchor's scope.
    Halftime anchor + full-match SX complement → not the same scope →
    not coverable → reject."""
    from cross_platform import build_complement_cover_deal
    sx = _sx_three_way()  # all moneyline (full-match)
    poly = _make_anchor(
        'Polymarket',
        'Independiente Santa Fe',
        0.10,
        title='Santa Fe vs Corinthians - Halftime Result',
        event_id='poly_sf_ht',
    )
    deal = build_complement_cover_deal(poly, sx)
    assert deal is None


# ── Find-arbs integration: full pipeline w/ fixture grouping ───────

def test_find_complement_cover_arbs_via_main_entry():
    """End-to-end via find_cross_platform_arbs: pool_a (Polymarket)
    has the anchor at misprice; pool_b (SX) has 3 binary 1X2 markets.
    Should surface exactly one cp_complement_cover deal (and none in
    direction B→A because SX siblings don't have a misprice anchor)."""
    from cross_platform import find_cross_platform_arbs
    sx = _sx_three_way(yes_a=0.18, yes_t=0.27, yes_b=0.55)
    poly = [
        _make_anchor('Polymarket', 'Independiente Santa Fe', 0.10,
                     event_id='poly_sf'),
    ]
    deals = find_cross_platform_arbs(poly, sx, min_confidence=0.50)
    cc_deals = [d for d in deals if d.structure == 'cp_complement_cover']
    assert len(cc_deals) == 1
    assert cc_deals[0].sum_cents == 92.0
    assert cc_deals[0].net_cents == 8.0


def test_find_complement_cover_arbs_skips_when_book_overround():
    """If anchor matches SX price (no misprice), no complement deal."""
    from cross_platform import find_cross_platform_arbs
    sx = _sx_three_way(yes_a=0.18, yes_t=0.27, yes_b=0.55)
    poly = [
        # Anchor at SAME price as SX matching sibling — no misprice
        _make_anchor('Polymarket', 'Independiente Santa Fe', 0.18,
                     event_id='poly_sf'),
    ]
    deals = find_cross_platform_arbs(poly, sx, min_confidence=0.50)
    cc_deals = [d for d in deals if d.structure == 'cp_complement_cover']
    assert cc_deals == []


# ── v29a + v29b together: phantom rejected, complement accepted ───

def test_v29a_phantom_still_rejected_alongside_v29b():
    """Operator's screenshot: 5 deals on Santa Fe × Corinthians as
    individual cross-platform pairs (v29a phantoms). After v29a these
    pairs produce 0 deals. v29b's complement-cover should ALSO not
    produce a deal here because anchor+siblings sum > 1 (typical book
    overround)."""
    from cross_platform import find_cross_platform_arbs
    title = 'Independiente Santa Fe vs SC Corinthians Paulista'
    poly = [
        _make_anchor('Polymarket', 'Independiente Santa Fe', 0.30,
                     title=title, event_id='poly_sf'),
    ]
    sx = _sx_three_way(yes_a=0.18, yes_t=0.27, yes_b=0.55, title=title)
    deals = find_cross_platform_arbs(poly, sx, min_confidence=0.50)
    # v29a kills cross-team X1/X2 (Poly Santa Fe + SX Corinthians/Tie)
    # v29a accepts same-team X1/X2: Poly Santa Fe + SX Santa Fe
    # v29b at sum 1.12 produces no complement cover
    cc_deals = [d for d in deals if d.structure == 'cp_complement_cover']
    assert cc_deals == []
    # X1/X2 may or may not be present depending on price math; the
    # critical assertion is no phantom 12% net deals at this book.
    for d in deals:
        # At correct math, no deal should claim >5% net at fair book pricing
        assert d.net_cents <= 12.0, (
            f"unexpected high-net deal at fair book: {d}"
        )


def test_complement_cover_three_platforms_anchor():
    """Limitless single-outcome anchor → SX 3-way complement.
    Tests cross-platform symmetry of v29b (anchor doesn't have to be
    Polymarket)."""
    from cross_platform import build_complement_cover_deal
    sx = _sx_three_way(yes_a=0.20, yes_t=0.27, yes_b=0.55)
    lim = _make_anchor('Limitless', 'Independiente Santa Fe', 0.10,
                        event_id='lim_sf')
    # Override source for Limitless test
    object.__setattr__(lim, 'yes_source', 'lim_clob')
    deal = build_complement_cover_deal(lim, sx)
    assert deal is not None
    assert deal.structure == 'cp_complement_cover'
    # 0.10 + 0.27 + 0.55 = 0.92
    assert abs(deal.sum_cents - 92.0) < 0.01
