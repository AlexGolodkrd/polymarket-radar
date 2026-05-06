"""Phase 19v29 (06.05.2026) — cross-platform outcome-name guard.

Operator screenshot 06.05.2026 — paper-trading dashboard showed 5 deals on
Independiente Santa Fe vs SC Corinthians Paulista (Copa Libertadores 4th
matchday, 07.05.2026), all Polymarket+SX Bet, all "Net $1.88-1.90 / 12%".

Manual verification through the SX Bet active-markets API revealed that SX
has THREE separate binary markets for this fixture (1X2 split):
    "Independiente Santa Fe" / "Not Independiente Santa Fe"
    "Corinthians SP"          / "Not Corinthians SP"
    "Tie"                     / "Not tie"

The radar's `find_pairs` matched events by title (same fixture string
across platforms) and produced multiple cross-platform pairs. The pairing
helper `_outcome_match_cross_platform` returned ('opposite', 'opposite')
unconditionally — TODO Phase 14, never implemented — so
`build_cross_platform_deal` blindly built X1 (YES_a + NO_b) for every
pair regardless of which TEAMS the outcomes named. Examples of the
phantoms produced:

    Polymarket "Santa Fe" YES 0.30  +  SX "Corinthians SP" NO 0.58  → 0.88
    Polymarket "Santa Fe" YES 0.30  +  SX "Tie"            NO 0.65  → 0.95
    Polymarket "Corinthians" YES 0.55 + SX "Santa Fe"      NO 0.78  → 1.33

The first two pass v28's scope guard (both 'moneyline') and v10's sanity
guards (sum ≥ 0.50, depth ≥ $5, sum < 0.96), so they show up as deals at
12% net. But they do NOT cover all 1X2 outcomes — at any tie result, both
legs of phantom #1 lose ($0 from Poly + $0 from SX-NO-on-Corinthians).
Not an arb. Not even close.

ARITHMETIC NOTE — a previous draft of this analysis claimed the same
"12% net" arose if you reinterpreted the pair as a 3-leg complement
(Poly Santa Fe YES + SX Corinthians YES + SX Tie YES). That claim came
from a wrong addition: 0.30 + 0.55 + 0.27 = 1.12, NOT 0.88. With the
correct sum of 1.12, the 3-leg complement at these prices would be a
12% LOSS, not an arb. A real complement-cover arb requires sum < 1.0,
i.e. a genuine misprice on at least one leg. So the 5 deals on the
screenshot have no hidden 3-leg arb behind them — they are simply
phantoms produced by the missing outcome-name guard. The fix below
removes them as a class.

Fix:
  1. event_matching.canonicalize_outcome_name strips YES/NO/win/etc.
     noise + handicap numerals + applies team aliases.
  2. event_matching.outcomes_compatible checks canonical equality or
     fuzzy similarity ≥ 0.70 between two outcome names.
  3. cross_platform._outcome_match_cross_platform returns
     ('opposite', 'opposite') iff outcomes_compatible, else None.
  4. cross_platform.build_cross_platform_deal refuses to build X1/X2
     when _outcome_match_cross_platform returns None.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


# ── canonicalize_outcome_name ───────────────────────────────────────

def test_canonicalize_strips_yes_no():
    from event_matching import canonicalize_outcome_name
    a, _ = canonicalize_outcome_name('Tottenham YES')
    b, _ = canonicalize_outcome_name('Tottenham NO')
    c, _ = canonicalize_outcome_name('Tottenham')
    assert a == c == 'tottenham'
    assert b == 'tottenham'


def test_canonicalize_applies_team_alias():
    """Tottenham alias hit — sport detected. Exact canonical may keep
    'hotspur' as a leftover word ('tottenham hotspur'); equality is
    handled by outcomes_compatible's token-subset rule, not here."""
    from event_matching import canonicalize_outcome_name
    a, sport = canonicalize_outcome_name('Tottenham Hotspur FC')
    assert 'tottenham' in a.split(), f"expected 'tottenham' token in canon, got {a!r}"
    assert sport == 'soccer'


def test_canonicalize_strips_handicap_numerals():
    from event_matching import canonicalize_outcome_name
    a, _ = canonicalize_outcome_name('Tottenham -0.5')
    b, _ = canonicalize_outcome_name('Tottenham +1')
    c, _ = canonicalize_outcome_name('Tottenham')
    assert a == b == c == 'tottenham'


def test_canonicalize_unknown_team_passes_through():
    """Teams not in alias dict (Copa Libertadores) keep their canon
    string. The fuzzy threshold in outcomes_compatible handles
    'Santa Fe' vs 'Independiente Santa Fe'."""
    from event_matching import canonicalize_outcome_name
    a, _ = canonicalize_outcome_name('Independiente Santa Fe')
    assert a == 'independiente santa fe'
    b, _ = canonicalize_outcome_name('Corinthians SP')
    # 'sp' is in the club-suffix noise list — stripped
    assert b == 'corinthians'


def test_canonicalize_empty_input():
    from event_matching import canonicalize_outcome_name
    a, _ = canonicalize_outcome_name('')
    assert a == ''
    b, _ = canonicalize_outcome_name('YES')
    assert b == ''                # all noise → empty


# ── outcomes_compatible ─────────────────────────────────────────────

def test_outcomes_compatible_same_canonical():
    from event_matching import outcomes_compatible
    assert outcomes_compatible('Tottenham Hotspur FC', 'Tottenham')
    assert outcomes_compatible('LA Lakers', 'Los Angeles Lakers')
    assert outcomes_compatible('Borussia Dortmund', 'BV Borussia 09 Dortmund')


def test_outcomes_compatible_strips_yes_no_before_compare():
    from event_matching import outcomes_compatible
    assert outcomes_compatible('Tottenham YES', 'Tottenham NO')
    # YES on one platform, raw team name on the other — same outcome
    assert outcomes_compatible('Lakers YES', 'Los Angeles Lakers')


def test_outcomes_compatible_different_teams_rejected():
    from event_matching import outcomes_compatible
    # Operator's phantom: same fixture, DIFFERENT teams
    assert not outcomes_compatible('Independiente Santa Fe',
                                    'Corinthians SP')
    assert not outcomes_compatible('Santa Fe', 'Corinthians')
    assert not outcomes_compatible('Lakers', 'Celtics')


def test_outcomes_compatible_fuzzy_fallback():
    """Teams not in alias dict but similar enough should still match
    after canonicalization (fuzzy threshold ≥ 0.70)."""
    from event_matching import outcomes_compatible
    # 'santa fe' (subset) vs 'independiente santa fe' should be close
    assert outcomes_compatible('Santa Fe', 'Independiente Santa Fe')


def test_outcomes_compatible_empty_inputs():
    from event_matching import outcomes_compatible
    assert not outcomes_compatible('', 'Tottenham')
    assert not outcomes_compatible('Tottenham', '')
    assert not outcomes_compatible('YES', 'NO')   # both → '' after noise strip


# ── cross_platform refusal of mismatched outcomes ──────────────────

def _make_outcomes(team_a, team_b, *, title=None):
    """Helper for cross-platform tests."""
    from cross_platform import PlatformOutcome
    title = title or f"{team_a} vs {team_b}"
    poly = PlatformOutcome(
        platform='Polymarket', event_id='poly_evt',
        title=title, outcome_name=team_a,
        yes_price=0.30, yes_depth=400.0, yes_source='clob_ask',
        no_price=0.70, no_depth=400.0, no_source='clob_ask',
        end_date='2026-05-08',
    )
    sx = PlatformOutcome(
        platform='SX Bet', event_id='sx_evt',
        title=title, outcome_name=team_b,
        yes_price=0.55, yes_depth=400.0, yes_source='sx_ob',
        no_price=0.45, no_depth=400.0, no_source='sx_ob',
        end_date='2026-05-08',
    )
    return poly, sx


def test_cross_platform_refuses_different_outcomes():
    """Operator's exact reproduction: Poly Santa Fe + SX Corinthians SP
    on the same fixture must produce ZERO deals."""
    from cross_platform import build_cross_platform_deal
    poly, sx = _make_outcomes(
        'Independiente Santa Fe', 'Corinthians SP',
        title='Independiente Santa Fe vs Corinthians SP',
    )
    deals = build_cross_platform_deal(poly, sx, match_confidence=0.85)
    assert deals == [], (
        f"different-team pair must NOT produce a deal; got: {deals}"
    )


def test_cross_platform_refuses_team_vs_tie():
    """Polymarket 'Santa Fe' YES + SX 'Tie' NO is also a phantom — no
    matter the prices, NOT an arb because they're not the same outcome."""
    from cross_platform import build_cross_platform_deal
    poly, sx = _make_outcomes(
        'Independiente Santa Fe', 'Tie',
        title='Independiente Santa Fe vs Corinthians SP',
    )
    deals = build_cross_platform_deal(poly, sx, match_confidence=0.85)
    assert deals == []


def test_cross_platform_allows_same_outcome_with_alias():
    """Tottenham Hotspur FC vs Tottenham — alias hit, valid pair."""
    from cross_platform import build_cross_platform_deal
    poly, sx = _make_outcomes(
        'Tottenham Hotspur FC', 'Tottenham',
        title='Tottenham vs Leeds',
    )
    # X2: out_a.NO 0.70 + out_b.YES 0.55 = 1.25 — too high, no deal
    # X1: out_a.YES 0.30 + out_b.NO 0.45 = 0.75 — should be a deal
    deals = build_cross_platform_deal(poly, sx, match_confidence=0.90)
    assert any(d.structure == 'X1' for d in deals), (
        f"Tottenham alias pair must produce X1 deal; got: {deals}"
    )


def test_cross_platform_allows_same_outcome_fuzzy():
    """Santa Fe vs Independiente Santa Fe — no alias, fuzzy match."""
    from cross_platform import build_cross_platform_deal
    poly, sx = _make_outcomes(
        'Santa Fe', 'Independiente Santa Fe',
        title='Santa Fe vs Corinthians',
    )
    deals = build_cross_platform_deal(poly, sx, match_confidence=0.85)
    # X1 sum = 0.30 + 0.45 = 0.75 < 0.96 → should produce a deal
    assert any(d.structure == 'X1' for d in deals)


# ── End-to-end Santa Fe × Corinthians regression ───────────────────

def test_santa_fe_corinthians_5_phantoms_eliminated():
    """Operator's 06.05.2026 screenshot reproduction.

    All 5 deals on the screenshot were Polymarket+SX Bet on the same
    fixture but with mismatched outcomes (different teams or team-vs-tie).
    After v29a, every cross-team pair must produce zero deals; only
    same-team pairs are eligible to be evaluated against the threshold.
    """
    from cross_platform import build_cross_platform_deal, PlatformOutcome
    title = 'Independiente Santa Fe vs SC Corinthians Paulista'
    end_date = '2026-05-08'
    # Polymarket: Santa Fe to win
    poly_sf = PlatformOutcome(
        platform='Polymarket', event_id='poly_sf',
        title=title, outcome_name='Independiente Santa Fe',
        yes_price=0.30, yes_depth=15476, yes_source='clob_ask',
        no_price=0.70, no_depth=15476, no_source='clob_ask',
        end_date=end_date,
    )
    # SX Bet: three separate binary markets
    sx_sf = PlatformOutcome(
        platform='SX Bet', event_id='sx_sf',
        title=title, outcome_name='Independiente Santa Fe',
        yes_price=0.18, yes_depth=15498, yes_source='sx_ob',
        no_price=0.82, no_depth=15498, no_source='sx_ob',
        end_date=end_date,
    )
    sx_co = PlatformOutcome(
        platform='SX Bet', event_id='sx_co',
        title=title, outcome_name='Corinthians SP',
        yes_price=0.55, yes_depth=15498, yes_source='sx_ob',
        no_price=0.45, no_depth=15498, no_source='sx_ob',
        end_date=end_date,
    )
    sx_tie = PlatformOutcome(
        platform='SX Bet', event_id='sx_tie',
        title=title, outcome_name='Tie',
        yes_price=0.27, yes_depth=15498, yes_source='sx_ob',
        no_price=0.73, no_depth=15498, no_source='sx_ob',
        end_date=end_date,
    )

    # Phantom 1: Poly Santa Fe + SX Corinthians SP — must be 0 deals
    deals_phantom_co = build_cross_platform_deal(
        poly_sf, sx_co, match_confidence=0.85)
    assert deals_phantom_co == []

    # Phantom 2: Poly Santa Fe + SX Tie — must be 0 deals
    deals_phantom_tie = build_cross_platform_deal(
        poly_sf, sx_tie, match_confidence=0.85)
    assert deals_phantom_tie == []

    # Real same-side pair: Poly Santa Fe + SX Santa Fe — should evaluate
    # X1 sum = 0.30 + 0.82 = 1.12 → no deal (sum > 0.96)
    # X2 sum = 0.70 + 0.18 = 0.88 → DEAL (sum < 0.96)
    deals_real = build_cross_platform_deal(
        poly_sf, sx_sf, match_confidence=0.95)
    assert any(d.structure == 'X2' for d in deals_real), (
        f"same-team pair Santa Fe ↔ Santa Fe should produce a real X2 "
        f"deal at sum 0.88; got: {deals_real}"
    )


# ── Integration: pre-existing v28 scope guard still works ──────────

def test_v28_halftime_guard_still_active():
    """v29 must not regress the v28 scope guard. Halftime + moneyline
    same-team pair must STILL be rejected (different scopes)."""
    from cross_platform import build_cross_platform_deal, PlatformOutcome
    poly_ht = PlatformOutcome(
        platform='Polymarket', event_id='p1',
        title='Tottenham vs Leeds - Halftime Result',
        outcome_name='Tottenham Hotspur FC',
        yes_price=0.40, yes_depth=400, yes_source='clob_ask',
        no_price=0.60, no_depth=400, no_source='clob_ask',
        end_date='2026-05-11',
    )
    sx_ml = PlatformOutcome(
        platform='SX Bet', event_id='s1',
        title='Tottenham vs Leeds',
        outcome_name='Tottenham',
        yes_price=0.50, yes_depth=400, yes_source='sx_ob',
        no_price=0.50, no_depth=400, no_source='sx_ob',
        end_date='2026-05-11',
    )
    deals = build_cross_platform_deal(poly_ht, sx_ml, match_confidence=0.85)
    assert deals == [], (
        f"halftime + moneyline must still be rejected (v28 guard); "
        f"got: {deals}"
    )
