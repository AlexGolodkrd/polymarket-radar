"""Phase audit-2 (11.05.2026) — Smart Matcher #2: league/competition guard.

Operator question: "as нам сделать умный поиск событий, чтобы во-первых
не было фантомов, во вторых лучше находились сделки".

The league guard closes the "same teams, different competition" phantom
class: Manchester United Premier League × Manchester United Champions
League on the same date — team-fuzzy match passes but they're DIFFERENT
fixtures with different outcomes.

extract_league() detects league marker from title; leagues_compatible()
returns False only when BOTH events have detected leagues that differ
(conservative — unknown league on one side ≠ reject).
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


# ── extract_league ─────────────────────────────────────────────────


def test_extract_epl():
    from event_matching import extract_league
    assert extract_league('EPL, Manchester United vs Nottingham Forest, May 17') == 'epl'
    assert extract_league('Premier League match Tottenham vs Leeds') == 'epl'


def test_extract_ucl():
    from event_matching import extract_league
    assert extract_league('UCL: Bayern Munich vs Real Madrid') == 'ucl'
    assert extract_league('Champions League QF leg 1') == 'ucl'


def test_extract_laliga():
    from event_matching import extract_league
    assert extract_league('LaLiga, Real Madrid vs Atletico') == 'laliga'
    assert extract_league('La Liga matchday') == 'laliga'


def test_extract_bundesliga():
    from event_matching import extract_league
    assert extract_league('Bundesliga: Bayern Munich vs Köln') == 'bundesliga'


def test_extract_nba():
    from event_matching import extract_league
    assert extract_league('NBA, Lakers vs Celtics') == 'nba'


def test_extract_returns_none_for_plain_title():
    """Polymarket sometimes shows just 'Team A vs Team B' without
    league — we must return None (conservative) so leagues_compatible
    doesn't lock these out."""
    from event_matching import extract_league
    assert extract_league('Charlotte FC vs. New York City FC') is None
    assert extract_league('FC Bayern München vs. 1. FC Köln') is None


def test_extract_efl_championship():
    from event_matching import extract_league
    assert extract_league('EFL Championship, Millwall vs Hull') == 'eflchamp'


def test_extract_copa_libertadores():
    from event_matching import extract_league
    assert extract_league('Copa Libertadores QF Santa Fe vs Corinthians') == 'copa_libertadores'


# ── leagues_compatible ─────────────────────────────────────────────


def test_leagues_compatible_same():
    from event_matching import leagues_compatible
    assert leagues_compatible('epl', 'epl')
    assert leagues_compatible('ucl', 'ucl')


def test_leagues_compatible_different_rejected():
    """Same teams in DIFFERENT leagues (EPL × Champions League on same
    date) — must reject."""
    from event_matching import leagues_compatible
    assert not leagues_compatible('epl', 'ucl')
    assert not leagues_compatible('nba', 'nfl')


def test_leagues_compatible_one_unknown_allowed():
    """One side has no league marker → fall back to existing match
    logic (don't penalize platforms that omit league from title)."""
    from event_matching import leagues_compatible
    assert leagues_compatible('epl', None)
    assert leagues_compatible(None, 'epl')
    assert leagues_compatible(None, None)


# ── Integration: build_cross_platform_deal rejects league mismatch ─


def test_build_rejects_epl_vs_ucl_same_teams():
    """The phantom: Manchester United Premier League × Manchester
    United Champions League — same date, same teams, DIFFERENT
    competition. League guard must reject."""
    from cross_platform import PlatformOutcome, build_cross_platform_deal
    out_a = PlatformOutcome(
        platform='Polymarket', event_id='p1',
        outcome_name='Manchester United',
        yes_price=0.45, yes_depth=1000.0, yes_source='clob_ask',
        no_price=0.55, no_depth=1000.0, no_source='clob_ask',
        end_date='2026-05-17',
        title='EPL, Manchester United vs Nottingham Forest, May 17, 2026',
    )
    out_b = PlatformOutcome(
        platform='SX Bet', event_id='s1',
        outcome_name='Manchester United',
        yes_price=0.40, yes_depth=1000.0, yes_source='sx_ob',
        no_price=0.60, no_depth=1000.0, no_source='sx_ob',
        end_date='2026-05-17',
        title='Champions League: Manchester United vs Bayern Munich',
    )
    deals = build_cross_platform_deal(out_a, out_b, match_confidence=0.85)
    assert deals == [], 'CRITICAL: EPL × UCL phantom built when it should reject'


def test_build_allows_epl_vs_epl_same_teams():
    """Same teams in same league with sum < threshold — legitimate arb."""
    from cross_platform import PlatformOutcome, build_cross_platform_deal
    # Prices chosen so X1 (yes_a + no_b) = 0.45 + 0.50 = 0.95 ← right at threshold
    # Use sub-threshold prices: 0.43 + 0.48 = 0.91 → real arb
    out_a = PlatformOutcome(
        platform='Polymarket', event_id='p1',
        outcome_name='Manchester United',
        yes_price=0.43, yes_depth=1000.0, yes_source='clob_ask',
        no_price=0.50, no_depth=1000.0, no_source='clob_ask',
        end_date='2026-05-17',
        title='EPL, Manchester United vs Nottingham Forest, May 17, 2026',
    )
    out_b = PlatformOutcome(
        platform='SX Bet', event_id='s1',
        outcome_name='Manchester United',
        yes_price=0.42, yes_depth=1000.0, yes_source='sx_ob',
        no_price=0.48, no_depth=1000.0, no_source='sx_ob',
        end_date='2026-05-17',
        title='EPL Manchester United vs Nottingham Forest',
    )
    deals = build_cross_platform_deal(out_a, out_b, match_confidence=0.85,
                                       threshold=0.96)
    # X1 sum = 0.43 + 0.48 = 0.91 → arb built
    # X2 sum = 0.50 + 0.42 = 0.92 → arb built
    assert len(deals) > 0, 'legitimate EPL × EPL pair was rejected'


def test_build_allows_when_one_league_unknown():
    """One title has league, other doesn't → fall back to current
    logic. Conservative — don't lock out unlabeled events."""
    from cross_platform import PlatformOutcome, build_cross_platform_deal
    # Sub-threshold prices: X1 = 0.43 + 0.48 = 0.91, X2 = 0.50 + 0.45 = 0.95
    out_a = PlatformOutcome(
        platform='Polymarket', event_id='p1',
        outcome_name='Charlotte FC',
        yes_price=0.43, yes_depth=1000.0, yes_source='clob_ask',
        no_price=0.50, no_depth=1000.0, no_source='clob_ask',
        end_date='2026-05-13',
        title='Charlotte FC vs. New York City FC',  # no league marker
    )
    out_b = PlatformOutcome(
        platform='SX Bet', event_id='s1',
        outcome_name='Charlotte FC',
        yes_price=0.45, yes_depth=1000.0, yes_source='sx_ob',
        no_price=0.48, no_depth=1000.0, no_source='sx_ob',
        end_date='2026-05-13',
        title='MLS, Charlotte FC vs NYCFC',  # has MLS marker
    )
    deals = build_cross_platform_deal(out_a, out_b, match_confidence=0.85,
                                       threshold=0.96)
    assert len(deals) > 0, 'one-side-unknown-league must NOT lock out arb'


def test_diag_counter_increments_on_league_mismatch():
    """rejected_league_mismatch counter must increment per rejection."""
    from cross_platform import (
        PlatformOutcome, build_cross_platform_deal, _pairing_diag,
    )
    # Reset for test
    _pairing_diag['rejected_league_mismatch'] = 0
    out_a = PlatformOutcome(
        platform='Polymarket', event_id='p1', outcome_name='X',
        yes_price=0.45, yes_depth=1000, yes_source='clob_ask',
        no_price=0.55, no_depth=1000, no_source='clob_ask',
        end_date='2026-05-17', title='EPL match X vs Y',
    )
    out_b = PlatformOutcome(
        platform='SX Bet', event_id='s1', outcome_name='X',
        yes_price=0.40, yes_depth=1000, yes_source='sx_ob',
        no_price=0.60, no_depth=1000, no_source='sx_ob',
        end_date='2026-05-17', title='UCL match X vs Z',
    )
    build_cross_platform_deal(out_a, out_b, match_confidence=0.85)
    assert _pairing_diag['rejected_league_mismatch'] == 1
