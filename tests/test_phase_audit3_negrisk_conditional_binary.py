"""Phase audit-3 (15.05.2026) — NegRisk conditional-binary scope guard.

Companion of the SX type=52 fix (PR #233). Polymarket NegRisk groups
sometimes split a fixture into children with TIME-WINDOW or
METHOD-OF-VICTORY modifiers ("Lakers win in regulation", "Pereira by KO").
Each child is binary, but the YES-set covers a SUBSET of the outcomes
that a plain ML binary on the same fixture covers. Cross-platform
pairing produces phantom arbs:

  Lakers win in OT → Polymarket "in regulation" YES loses,
                     Limitless ML YES pays
  → an arb that paired them assuming opposite outcomes fails on both sides.

Fix: `detect_market_scope` returns 'regulation' or 'method_of_victory'
for these titles; `scopes_compatible` then refuses pairs against
'moneyline'.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


# ── Regulation-only scope ────────────────────────────────────────

def test_detect_regulation_basic():
    from event_matching import detect_market_scope
    assert detect_market_scope(
        'Will Lakers win in regulation?', 'YES',
    ) == 'regulation'
    assert detect_market_scope(
        'Brentford qualifies in regulation time', 'Brentford',
    ) == 'regulation'
    assert detect_market_scope(
        'NHL Game 7 — No Extra Time Winner', 'Bruins',
    ) == 'regulation'


def test_detect_regulation_90min_phrasing():
    from event_matching import detect_market_scope
    assert detect_market_scope(
        'Will Real Madrid lead after 90 min?', 'YES',
    ) == 'regulation'
    assert detect_market_scope(
        'Result within 90 minutes', 'Home',
    ) == 'regulation'
    assert detect_market_scope(
        'Bayern wins in 90 minutes',  'Bayern',
    ) == 'regulation'


# ── Method-of-victory scope (combat sports + tennis sets) ────────

def test_detect_method_of_victory_combat():
    from event_matching import detect_market_scope
    assert detect_market_scope(
        'Pereira vs Aspinall — Method of Victory', 'Pereira by KO',
    ) == 'method_of_victory'
    assert detect_market_scope(
        'Will Holloway win by decision?', 'YES',
    ) == 'method_of_victory'
    assert detect_market_scope(
        'Adesanya wins by submission', 'YES',
    ) == 'method_of_victory'
    assert detect_market_scope(
        'Joshua vs Fury', 'Fury by TKO',
    ) == 'method_of_victory'


def test_detect_method_of_victory_tennis():
    from event_matching import detect_market_scope
    assert detect_market_scope(
        'Will Sinner win in straight sets?', 'YES',
    ) == 'method_of_victory'
    assert detect_market_scope(
        'Alcaraz vs Djokovic', 'Alcaraz in 3 sets',
    ) == 'method_of_victory'


# ── Compatibility table ──────────────────────────────────────────

def test_scopes_compatible_same_for_new_scopes():
    from event_matching import scopes_compatible
    assert scopes_compatible('regulation', 'regulation')
    assert scopes_compatible('method_of_victory', 'method_of_victory')


def test_scopes_incompatible_with_moneyline():
    from event_matching import scopes_compatible
    assert not scopes_compatible('regulation', 'moneyline')
    assert not scopes_compatible('moneyline', 'regulation')
    assert not scopes_compatible('method_of_victory', 'moneyline')
    assert not scopes_compatible('moneyline', 'method_of_victory')
    # Cross-modifier should also reject (they are different events).
    assert not scopes_compatible('regulation', 'method_of_victory')


# ── Existing markets must NOT regress ────────────────────────────

def test_plain_ml_still_moneyline():
    """Sanity: no false positives on plain "team wins" titles."""
    from event_matching import detect_market_scope
    assert detect_market_scope(
        'Brentford vs Crystal Palace', 'Brentford',
    ) == 'moneyline'
    assert detect_market_scope(
        'Lakers vs Celtics', 'Lakers',
    ) == 'moneyline'
    assert detect_market_scope(
        'Pereira vs Aspinall', 'Pereira',
    ) == 'moneyline'
    # Words that contain "by" but aren't method-of-victory phrasing
    # must NOT trip the regex (e.g. team named "Derby County").
    assert detect_market_scope(
        'Derby County vs Leeds', 'Derby County',
    ) == 'moneyline'


# ── End-to-end refusal ───────────────────────────────────────────

def test_cross_platform_refuses_regulation_vs_moneyline():
    from cross_platform import build_cross_platform_deal, PlatformOutcome
    poly = PlatformOutcome(
        platform='Polymarket', event_id='poly_lakers_reg',
        title='NBA Game 7: Will Lakers win in regulation?',
        outcome_name='Lakers',
        yes_price=0.48, yes_depth=200.0, yes_source='clob_ask',
        no_price=0.52, no_depth=200.0, no_source='clob_ask',
        end_date='2026-05-20',
    )
    lim = PlatformOutcome(
        platform='Limitless', event_id='lim_lakers',
        title='Lakers vs Celtics',
        outcome_name='Lakers',
        yes_price=0.51, yes_depth=300.0, yes_source='lim_clob',
        no_price=0.49, no_depth=300.0, no_source='lim_clob',
        end_date='2026-05-20',
    )
    deals = build_cross_platform_deal(poly, lim, match_confidence=0.90)
    assert deals == [], (
        f"regulation + moneyline must NOT produce a deal; got: {deals}"
    )


def test_cross_platform_refuses_method_of_victory_vs_moneyline():
    from cross_platform import build_cross_platform_deal, PlatformOutcome
    poly = PlatformOutcome(
        platform='Polymarket', event_id='poly_pereira_ko',
        title='Pereira vs Aspinall — Method of Victory',
        outcome_name='Pereira by KO',
        yes_price=0.30, yes_depth=500.0, yes_source='clob_ask',
        no_price=0.70, no_depth=500.0, no_source='clob_ask',
        end_date='2026-06-01',
    )
    lim = PlatformOutcome(
        platform='Limitless', event_id='lim_pereira',
        title='Pereira vs Aspinall',
        outcome_name='Pereira',
        yes_price=0.55, yes_depth=400.0, yes_source='lim_clob',
        no_price=0.45, no_depth=400.0, no_source='lim_clob',
        end_date='2026-06-01',
    )
    deals = build_cross_platform_deal(poly, lim, match_confidence=0.92)
    assert deals == [], (
        f"method_of_victory + moneyline must NOT produce a deal; got: {deals}"
    )
