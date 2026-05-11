"""Phase audit-2 (11.05.2026) — phantom cross-platform match fix.

Operator screenshot 11.05.2026 found 2+ phantom deals on production
where fuzzy event matcher paired DIFFERENT market types because they
shared team names:

  Phantom A:
    Leg 1 (Limitless): "Both Real Madrid and Oviedo score on May 14? YES"
    Leg 2 (SX Bet):    "Real Madrid NO"
    → BTTS paired with moneyline; Real Madrid winning 1-0 loses BTTS,
      Real Madrid winning anything loses Leg 2 NO → real-money loss.

  Phantom B:
    Leg 1 (Polymarket): "FC Bayern München NO"
    Leg 2 (Limitless):  "Both Bayern Munich and 1. FC Köln score on May 16? YES"
    → moneyline paired with BTTS; same class of issue.

Root cause: `detect_market_scope` classified all 4 markets as 'moneyline'
because the BTTS/corners/cards/goalscorer regex patterns weren't present.
Default fallback to 'moneyline' meant scope_compatible('moneyline',
'moneyline') always returned True → phantom built.

Fix: added _BTTS_PATTERNS, _CORNERS_PATTERNS, _CARDS_PATTERNS,
_GOALSCORER_PATTERNS to detect_market_scope. BTTS uses a loose
"both...score within 60 chars" matcher to handle "1. FC Köln" / German
umlauts / arbitrary club name complexity.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


# ── BTTS scope detection ──────────────────────────────────────────


def test_btts_real_madrid_oviedo():
    """Operator's screenshot phantom #1 — Limitless BTTS title."""
    from event_matching import detect_market_scope
    assert detect_market_scope(
        'Both Real Madrid and Oviedo score on May 14?',
        'YES',
    ) == 'btts'


def test_btts_bayern_koln():
    """Operator's screenshot phantom #2 — Limitless BTTS title with
    '1. FC Köln' which contains period + umlauts + 3 tokens. The
    loose-matcher needs to handle this without choking."""
    from event_matching import detect_market_scope
    assert detect_market_scope(
        'Both Bayern Munich and 1. FC Köln score on May 16?',
        'YES',
    ) == 'btts'


def test_btts_generic_phrasing():
    """'Both teams to score' / 'Both teams score' canonical BTTS."""
    from event_matching import detect_market_scope
    assert detect_market_scope('Manchester United both teams to score', 'YES') == 'btts'
    assert detect_market_scope('Liverpool vs Chelsea — Both teams score', 'Yes') == 'btts'


def test_btts_acronym():
    """Explicit BTTS acronym."""
    from event_matching import detect_market_scope
    assert detect_market_scope('BTTS Bayern vs Köln', 'Yes') == 'btts'


# ── Corners / cards / goalscorer ──────────────────────────────────


def test_corners_limitless_pattern():
    """Limitless titles like 'Napoli vs Bologna: 11+ total corners?'."""
    from event_matching import detect_market_scope
    assert detect_market_scope(
        'Napoli vs Bologna: 11+ total corners?', 'YES',
    ) == 'corners'


def test_corners_alternate_phrasings():
    from event_matching import detect_market_scope
    assert detect_market_scope('Total corners over/under 8.5', '9+') == 'corners'
    assert detect_market_scope('Over 10 corners', 'Yes') == 'corners'


def test_cards_pattern():
    from event_matching import detect_market_scope
    assert detect_market_scope('Total yellow cards over/under', '5+ cards') == 'cards'
    assert detect_market_scope('Match cards 4+', 'Yes') == 'cards'


def test_goalscorer_pattern():
    from event_matching import detect_market_scope
    assert detect_market_scope('First goalscorer: Haaland', 'Haaland') == 'goalscorer'
    assert detect_market_scope('Anytime goalscorer', 'Mbappé') == 'goalscorer'


# ── Moneyline still works for plain winner markets ────────────────


def test_moneyline_charlotte_nyc():
    from event_matching import detect_market_scope
    assert detect_market_scope(
        'Charlotte FC vs. New York City FC',
        'Charlotte FC YES',
    ) == 'moneyline'


def test_moneyline_manchester_united():
    from event_matching import detect_market_scope
    assert detect_market_scope(
        'EPL, Manchester United vs Nottingham Forest, May 17, 2026',
        'Manchester United YES',
    ) == 'moneyline'


def test_moneyline_sx_bet_simple():
    from event_matching import detect_market_scope
    assert detect_market_scope('FC Bayern München', 'FC Bayern München NO') == 'moneyline'


# ── Phantom-match scope rejection ─────────────────────────────────


def test_phantom_moneyline_vs_btts_rejected():
    """The core phantom fix — moneyline and btts must not be compatible."""
    from event_matching import detect_market_scope, scopes_compatible
    s_ml = detect_market_scope('FC Bayern München', 'FC Bayern München NO')
    s_btts = detect_market_scope(
        'Both Bayern Munich and 1. FC Köln score on May 16?', 'YES',
    )
    assert s_ml == 'moneyline'
    assert s_btts == 'btts'
    assert not scopes_compatible(s_ml, s_btts), (
        f'CRITICAL: phantom pair {s_ml} × {s_btts} would be built '
        'and could lose real money on a non-arb'
    )


def test_phantom_moneyline_vs_corners_rejected():
    from event_matching import detect_market_scope, scopes_compatible
    s_ml = detect_market_scope('Charlotte FC vs. New York City FC', 'Charlotte FC YES')
    s_corners = detect_market_scope('Napoli vs Bologna: 11+ total corners?', 'YES')
    assert not scopes_compatible(s_ml, s_corners)


def test_phantom_moneyline_vs_goalscorer_rejected():
    from event_matching import detect_market_scope, scopes_compatible
    s_ml = detect_market_scope('Real Madrid', 'Real Madrid NO')
    s_gs = detect_market_scope('First goalscorer: Vinicius', 'Vinicius')
    assert not scopes_compatible(s_ml, s_gs)


def test_compatible_moneyline_pair_still_works():
    """A genuine cross-platform moneyline arb must STILL be allowed."""
    from event_matching import detect_market_scope, scopes_compatible
    s_a = detect_market_scope(
        'Manchester United vs Nottingham Forest',
        'Manchester United YES',
    )
    s_b = detect_market_scope(
        'Manchester United - Nottingham Forest',
        'Manchester United NO',
    )
    assert scopes_compatible(s_a, s_b)


def test_compatible_btts_pair_still_works():
    """Two BTTS markets across platforms should pair."""
    from event_matching import detect_market_scope, scopes_compatible
    s_a = detect_market_scope(
        'Both Bayern Munich and 1. FC Köln score on May 16?',
        'YES',
    )
    s_b = detect_market_scope(
        'Both teams to score - Bayern vs Köln',
        'Yes',
    )
    assert s_a == 'btts'
    assert s_b == 'btts'
    assert scopes_compatible(s_a, s_b)


# ── Regression guards: previous scopes (v28 / v32) must still work ─


def test_halftime_v28_regression():
    from event_matching import detect_market_scope
    assert detect_market_scope('BVB vs Frankfurt Halftime Result', 'BVB YES') == 'halftime'


def test_exact_score_v32_regression():
    from event_matching import detect_market_scope
    assert detect_market_scope(
        'Fulham FC vs. AFC Bournemouth - Exact Score',
        'Fulham FC 2-1',
    ) == 'exact_score'
    # Outcome-name fallback (no scope keyword in title)
    assert detect_market_scope(
        'Fulham vs Bournemouth',
        '2-1',
    ) == 'exact_score'


def test_handicap_regression():
    from event_matching import detect_market_scope
    assert detect_market_scope('Tottenham handicap', 'Tot -0.5') == 'handicap'


def test_totals_regression():
    from event_matching import detect_market_scope
    assert detect_market_scope('Over 2.5 total goals', 'Over') == 'totals'
    assert detect_market_scope('Tottenham vs Leeds: 3+ total goals?', 'YES') == 'totals'


# ── Phase audit-2 (continued) — 1X2 third-outcome guard ────────────


def test_draw_vs_team_no_rejected_tottenham_leeds():
    """Operator screenshot 11.05.2026 phantom #3:
    Leg 1 (Polymarket): 'Draw (Tottenham Hotspur FC vs. Leeds United FC) YES'
    Leg 2 (SX Bet): 'Tottenham Hotspur NO'

    These are different sides of a 3-way (1X2) market — pairing them
    leaves the Tottenham-wins outcome UNCOVERED, where BOTH legs lose
    simultaneously. Subset matching (tottenham ⊆ draw tottenham leeds)
    falsely accepted them. The 1X2 draw guard must reject.
    """
    from event_matching import outcomes_compatible
    assert not outcomes_compatible(
        'Draw (Tottenham Hotspur FC vs. Leeds United FC) YES',
        'Tottenham Hotspur NO',
    )


def test_draw_vs_team_no_rejected_bayern_koln():
    """Same class of phantom — Bayern Munich fixture."""
    from event_matching import outcomes_compatible
    assert not outcomes_compatible(
        'Draw Bayern Munich',
        'Bayern Munich',
    )


def test_tie_token_treated_as_draw():
    """SX Bet uses 'Tie' where Polymarket uses 'Draw' — these should
    match when both refer to the 1X2 third outcome of the same fixture."""
    from event_matching import outcomes_compatible
    assert outcomes_compatible('Draw', 'Tie YES')


def test_draw_vs_draw_match():
    """Both legs are draw → same side → compatible."""
    from event_matching import outcomes_compatible
    assert outcomes_compatible(
        'Draw (Bayern Munich vs 1. FC Köln)',
        'Draw (Bayern Munich vs Köln) YES',
    )


def test_tie_outcome_vs_team_rejected():
    """Pure tie outcome name × real team name → reject."""
    from event_matching import outcomes_compatible
    assert not outcomes_compatible('Tie', 'Real Madrid NO')


def test_yes_no_team_pair_still_works():
    """Regression — YES/NO of the SAME team (e.g. Manchester City YES on
    one platform paired with Manchester City NO on another) must still
    pass outcomes_compatible (no draw involved)."""
    from event_matching import outcomes_compatible
    assert outcomes_compatible(
        'Manchester City FC',
        'Manchester City NO',
    )


def test_canonicalize_keeps_draw_token():
    """Canonicalization must NOT strip 'draw' as noise — it's a key
    signal for the 1X2 guard."""
    from event_matching import canonicalize_outcome_name
    canon, _ = canonicalize_outcome_name('Draw (Bayern vs Koln)')
    assert 'draw' in canon.split()
