"""Phase 13 (01.05.2026) — cross_platform.py tests.

Cross-platform arb detection: matches events across Polymarket / Limitless
/ SX Bet, builds X1/X2 deal structures, returns radar-format deals.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), 'Scripts')
sys.path.insert(0, SCRIPTS)


# ── PlatformOutcome construction helpers ────────────────────────────
def _make_outcome(platform, title, yes_price, no_price, depth=50.0,
                   yes_src='clob_ask', no_src='clob_ask',
                   end_date='2026-03-25T22:00:00Z',
                   outcome_name='OUT', event_id='E1'):
    from cross_platform import PlatformOutcome
    return PlatformOutcome(
        platform=platform, event_id=event_id, outcome_name=outcome_name,
        yes_price=yes_price, yes_depth=depth, yes_source=yes_src,
        no_price=no_price, no_depth=depth, no_source=no_src,
        end_date=end_date, title=title,
    )


# ── build_cross_platform_deal ───────────────────────────────────────
def test_x1_arb_when_yes_a_plus_no_b_below_threshold():
    """Polymarket YES Lakers @ 0.40 + Limitless NO Lakers @ 0.55 = 0.95
    < 0.96 threshold → X1 deal."""
    from cross_platform import build_cross_platform_deal
    a = _make_outcome('Polymarket', 'Lakers vs Celtics',
                       yes_price=0.40, no_price=0.62)
    b = _make_outcome('Limitless', 'Lakers vs Celtics',
                       yes_price=0.45, no_price=0.55)
    deals = build_cross_platform_deal(a, b, match_confidence=0.92)
    structures = [d.structure for d in deals]
    assert 'X1' in structures
    x1 = next(d for d in deals if d.structure == 'X1')
    assert x1.sum_cents == pytest.approx(95.0)
    assert x1.net_cents == pytest.approx(5.0)


def test_x2_arb_when_no_a_plus_yes_b_below_threshold():
    """Symmetric: Polymarket NO Lakers @ 0.55 + Limitless YES Lakers @ 0.40
    = 0.95 → X2 deal."""
    from cross_platform import build_cross_platform_deal
    a = _make_outcome('Polymarket', 'Lakers vs Celtics',
                       yes_price=0.40, no_price=0.55)
    b = _make_outcome('Limitless', 'Lakers vs Celtics',
                       yes_price=0.40, no_price=0.62)
    deals = build_cross_platform_deal(a, b, match_confidence=0.92)
    structures = [d.structure for d in deals]
    assert 'X2' in structures


def test_no_arb_when_sum_above_threshold():
    """sum >= 0.96 → no deal."""
    from cross_platform import build_cross_platform_deal
    a = _make_outcome('Polymarket', 'X', yes_price=0.50, no_price=0.50)
    b = _make_outcome('Limitless', 'X', yes_price=0.50, no_price=0.50)
    deals = build_cross_platform_deal(a, b, 0.95)
    # Both X1=1.00 and X2=1.00 → no deals
    assert len(deals) == 0


def test_arb_rejected_if_implied_source():
    """Sources 'implied' (lastTradePrice fallback) → rejected."""
    from cross_platform import build_cross_platform_deal
    a = _make_outcome('Polymarket', 'X',
                       yes_price=0.40, no_price=0.55,
                       no_src='implied')
    b = _make_outcome('Limitless', 'X', yes_price=0.40, no_price=0.55)
    deals = build_cross_platform_deal(a, b, 0.95)
    # X1 = a.YES + b.NO — both NOT implied → X1 valid
    # X2 = a.NO + b.YES — a.NO is implied → REJECTED
    structures = [d.structure for d in deals]
    assert 'X1' in structures
    assert 'X2' not in structures


def test_arb_rejected_if_zero_depth():
    from cross_platform import build_cross_platform_deal
    a = _make_outcome('Polymarket', 'X',
                       yes_price=0.40, no_price=0.55, depth=0.0)
    b = _make_outcome('Limitless', 'X', yes_price=0.40, no_price=0.55)
    deals = build_cross_platform_deal(a, b, 0.95)
    assert len(deals) == 0


# ── find_cross_platform_arbs (with event matching) ──────────────────
def test_find_arbs_pairs_matching_events():
    from cross_platform import find_cross_platform_arbs
    pool_a = [
        _make_outcome('Polymarket',
                       'Will the Lakers beat Celtics on Mar 25?',
                       yes_price=0.40, no_price=0.62,
                       end_date='2026-03-25T22:00:00Z'),
        _make_outcome('Polymarket', 'Some unrelated NFL game',
                       yes_price=0.50, no_price=0.50,
                       end_date='2026-04-01T00:00:00Z'),
    ]
    pool_b = [
        _make_outcome('Limitless', 'Lakers vs Celtics — Mar 25',
                       yes_price=0.45, no_price=0.55,
                       end_date='2026-03-25T22:00:00Z'),
    ]
    deals = find_cross_platform_arbs(pool_a, pool_b, min_confidence=0.70)
    # Should find at least one cross-platform arb
    # X1: 0.40 + 0.55 = 0.95 < 0.96 → valid
    assert len(deals) >= 1
    assert any(d.structure == 'X1' for d in deals)


def test_find_arbs_skips_same_platform():
    """If both pool_a and pool_b are 'Polymarket', skip — not cross-platform."""
    from cross_platform import find_cross_platform_arbs
    pool_a = [_make_outcome('Polymarket', 'Lakers vs Celtics Mar 25',
                              yes_price=0.40, no_price=0.55)]
    pool_b = [_make_outcome('Polymarket', 'Lakers vs Celtics Mar 25',
                              yes_price=0.40, no_price=0.55)]
    deals = find_cross_platform_arbs(pool_a, pool_b)
    assert len(deals) == 0


# ── to_radar_deal_format ────────────────────────────────────────────
def test_to_radar_deal_format_has_required_fields():
    from cross_platform import build_cross_platform_deal, to_radar_deal_format
    a = _make_outcome('Polymarket', 'Lakers vs Celtics',
                       yes_price=0.40, no_price=0.55)
    b = _make_outcome('Limitless', 'Lakers vs Celtics',
                       yes_price=0.40, no_price=0.55)
    deals = build_cross_platform_deal(a, b, 0.92)
    formatted = to_radar_deal_format(deals[0])
    # Required keys for dashboard compatibility
    for k in ('title', 'platform', 'arb_structure', 'sum_cents',
              'threshold_cents', 'net', 'entries', 'min_liq',
              'confidence', 'end_date', 'grade'):
        assert k in formatted, f"missing key {k}"
    assert formatted['arb_structure'] == 'cross_platform'
    assert formatted['platform'] == 'Polymarket+Limitless'
    assert len(formatted['entries']) == 2
    # Each entry has source whitelisted
    for e in formatted['entries']:
        assert e['source'] != 'implied'


def test_grade_assignment():
    from cross_platform import build_cross_platform_deal, to_radar_deal_format
    # net_cents > 5 → CP-A
    a = _make_outcome('Polymarket', 'X', yes_price=0.30, no_price=0.50)
    b = _make_outcome('Limitless', 'X', yes_price=0.40, no_price=0.55)
    # X1: 0.30 + 0.55 = 0.85 → 15c profit → CP-A
    deals = build_cross_platform_deal(a, b, 0.95)
    formatted = to_radar_deal_format(deals[0])
    assert formatted['grade'] == 'CP-A'


# ── env defaults ────────────────────────────────────────────────────
def test_default_threshold_096():
    from cross_platform import CROSS_PLATFORM_THRESHOLD
    if 'CROSS_PLATFORM_THRESHOLD' not in os.environ:
        assert CROSS_PLATFORM_THRESHOLD == pytest.approx(0.96)


def test_disabled_by_default():
    """CROSS_PLATFORM_ENABLED defaults to 0 — feature is opt-in."""
    from cross_platform import CROSS_PLATFORM_ENABLED
    if 'CROSS_PLATFORM_ENABLED' not in os.environ:
        assert CROSS_PLATFORM_ENABLED is False
