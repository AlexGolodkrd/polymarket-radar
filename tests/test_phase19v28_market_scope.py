"""Phase 19v28 (06.05.2026) — market-scope filter for cross-platform arbs.

Operator verified the 6 cross-platform deals after v26+v27 SX-API fix
restored SX orderbook fetching. ALL 6 were phantom — Polymarket
"Halftime Result" markets paired with SX Bet full-match moneyline /
handicap / 1X2 markets. Same teams + date + opposite YES/NO but DIFFERENT
market scopes → both legs can win OR lose simultaneously → not arbs.

Examples from operator screenshot (deals at risk if DRY_RUN flipped to 0):

  Deal 1: Poly "BVB Halftime YES" 53¢ + SX "Borussia Dortmund NO" 20.75¢
          Halftime ≠ full match: BVB leads 1-0 at HT, loses 1-2 FT →
          BOTH legs pay → no arb structure.

  Deal 6: Poly "Tottenham Halftime YES" 40¢ + SX "Tottenham -0.5 NO" 49.88¢
          Halftime ≠ handicap full match.

Fix: `detect_market_scope` classifies title + outcome into
{halftime, handicap, totals, period, moneyline}, and
`build_cross_platform_deal` refuses pairs whose scopes differ.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


# ── Scope detection ──────────────────────────────────────────────

def test_detect_halftime():
    from event_matching import detect_market_scope
    assert detect_market_scope(
        'BV Borussia Dortmund vs Eintracht Frankfurt - Halftime Result',
        'BVB YES',
    ) == 'halftime'
    assert detect_market_scope(
        'West Ham vs Arsenal — Halftime Result', 'Draw NO',
    ) == 'halftime'
    assert detect_market_scope(
        'Tottenham 1H Result', 'Tottenham',
    ) == 'halftime'


def test_detect_handicap():
    from event_matching import detect_market_scope
    # Handicap encoded in outcome name (SX style)
    assert detect_market_scope(
        'BVB vs Frankfurt', 'Borussia Dortmund -1',
    ) == 'handicap'
    assert detect_market_scope(
        'West Ham vs Arsenal', 'West Ham +1',
    ) == 'handicap'
    assert detect_market_scope(
        'Tottenham vs Leeds', 'Tottenham Hotspur -0.5',
    ) == 'handicap'


def test_detect_totals():
    from event_matching import detect_market_scope
    assert detect_market_scope(
        'BVB vs Frankfurt - Total Goals', 'Over 2.5',
    ) == 'totals'
    assert detect_market_scope(
        'Game Total Points', 'Under 220',
    ) == 'totals'


def test_detect_moneyline_default():
    from event_matching import detect_market_scope
    assert detect_market_scope(
        'BV Borussia 09 Dortmund vs Eintracht Frankfurt',
        'Borussia Dortmund',
    ) == 'moneyline'
    assert detect_market_scope(
        'BTC Up or Down - 1 day', 'YES',
    ) == 'moneyline'


def test_detect_period():
    from event_matching import detect_market_scope
    assert detect_market_scope(
        'Lakers vs Celtics 1st Quarter', 'Lakers',
    ) == 'period'


# ── Compatibility check ─────────────────────────────────────────

def test_scopes_compatible_same():
    from event_matching import scopes_compatible
    for s in ('halftime', 'moneyline', 'handicap', 'totals', 'period'):
        assert scopes_compatible(s, s)


def test_scopes_compatible_different_rejects():
    from event_matching import scopes_compatible
    assert not scopes_compatible('halftime', 'moneyline')
    assert not scopes_compatible('moneyline', 'handicap')
    assert not scopes_compatible('halftime', 'handicap')
    assert not scopes_compatible('totals', 'moneyline')


# ── End-to-end: cross_platform refuses incompatible pair ─────────

def test_cross_platform_refuses_halftime_vs_moneyline():
    """Operator's deal #1 reproduction — should produce 0 deals now."""
    from cross_platform import build_cross_platform_deal, PlatformOutcome
    poly = PlatformOutcome(
        platform='Polymarket', event_id='poly_bvb_ht',
        title='BV Borussia 09 Dortmund vs. Eintracht Frankfurt - Halftime Result',
        outcome_name='BV Borussia 09 Dortmund',
        yes_price=0.53, yes_depth=171.72, yes_source='clob_ask',
        no_price=0.47, no_depth=100.0, no_source='clob_ask',
        end_date='2026-05-08',
    )
    sx = PlatformOutcome(
        platform='SX Bet', event_id='sx_bvb_full',
        title='BV Borussia 09 Dortmund vs. Eintracht Frankfurt',
        outcome_name='Borussia Dortmund',
        yes_price=0.79, yes_depth=300.0, yes_source='sx_ob',
        no_price=0.2075, no_depth=84.56, no_source='sx_ob',
        end_date='2026-05-08',
    )
    deals = build_cross_platform_deal(poly, sx, match_confidence=0.76)
    assert deals == [], (
        f"halftime + moneyline must NOT produce a deal; got: {deals}"
    )


def test_cross_platform_refuses_halftime_vs_handicap():
    """Operator's deal #6 reproduction."""
    from cross_platform import build_cross_platform_deal, PlatformOutcome
    poly = PlatformOutcome(
        platform='Polymarket', event_id='poly_tot_ht',
        title='Tottenham Hotspur FC vs. Leeds United FC - Halftime Result',
        outcome_name='Tottenham Hotspur FC',
        yes_price=0.40, yes_depth=400.0, yes_source='clob_ask',
        no_price=0.60, no_depth=400.0, no_source='clob_ask',
        end_date='2026-05-11',
    )
    sx = PlatformOutcome(
        platform='SX Bet', event_id='sx_tot_h',
        title='Tottenham Hotspur vs. Leeds United',
        outcome_name='Tottenham Hotspur -0.5',
        yes_price=0.5012, yes_depth=50.0, yes_source='sx_ob',
        no_price=0.4988, no_depth=13.52, no_source='sx_ob',
        end_date='2026-05-11',
    )
    deals = build_cross_platform_deal(poly, sx, match_confidence=0.80)
    assert deals == [], (
        f"halftime + handicap must NOT produce a deal; got: {deals}"
    )


def test_cross_platform_allows_moneyline_pair():
    """Same scope on both platforms → still works."""
    from cross_platform import build_cross_platform_deal, PlatformOutcome
    a = PlatformOutcome(
        platform='Polymarket', event_id='1', title='Lakers vs Celtics',
        outcome_name='Lakers', yes_price=0.45, yes_depth=1000,
        yes_source='clob_ask', no_price=0.55, no_depth=1000,
        no_source='clob_ask', end_date='2026-05-04',
    )
    b = PlatformOutcome(
        platform='Limitless', event_id='2', title='Lakers vs Celtics',
        outcome_name='Lakers', yes_price=0.51, yes_depth=1000,
        yes_source='lim_clob', no_price=0.49, no_depth=1000,
        no_source='lim_clob', end_date='2026-05-04',
    )
    deals = build_cross_platform_deal(a, b, match_confidence=0.95)
    assert any(d.structure == 'X1' for d in deals)
