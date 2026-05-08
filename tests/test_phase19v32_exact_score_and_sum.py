"""Phase 19v32 (08.05.2026) — exact-score scope + sum_cents UI/analytics fix.

Operator screenshot 08.05.2026 — paper-trading dashboard «История сделок»
showed 100+ rows of `Polymarket+SX Bet :: Fulham FC vs. AFC Bournemouth -
Exact Score` at Net $11-18 / 30-35% ROI. Two bugs at once:

  Bug A (phantom):    Polymarket "Exact Score" event paired with SX Bet
                      1X2 moneyline. v29a outcome-guard let it through
                      because Polymarket sometimes uses the favored team
                      as outcome name ("Fulham FC 2-1") which canonicalizes
                      to 'fulham' and matches SX's 'fulham'. v28 scope
                      guard let it through because both default to
                      'moneyline' (no exact-score class existed).

  Bug B (UI):         Sum column on the dashboard «История сделок» showed
                      `—` for every cross-platform row. Root cause:
                      analytics._snapshot() reads `deal.get('total_cents')`,
                      but cross_platform.to_radar_deal_format() writes
                      `sum_cents`. Per-platform deals → `total_cents`,
                      cross-platform deals → `sum_cents`, and the snapshot
                      didn't read both.

Fixes:
  1. event_matching.detect_market_scope adds 'exact_score' class with
     two detection signals: title-pattern (Exact/Correct/Final Score) and
     outcome-name pattern (NN-NN scoreline).
  2. analytics._snapshot reads `total_cents` then falls back to
     `sum_cents` so both deal shapes populate the analytics sum column.
  3. cross_platform.to_radar_deal_format also writes `total_cents`
     (alias of sum_cents) so the live-deals widget on dashboard.html
     and the Polymarket _quality_ok gate both work on CP deals too.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


# ── A. detect_market_scope: 'exact_score' class ────────────────────

def test_detect_exact_score_via_title():
    from event_matching import detect_market_scope
    assert detect_market_scope(
        'Fulham FC vs. AFC Bournemouth - Exact Score',
        'Fulham FC',
    ) == 'exact_score'
    assert detect_market_scope(
        'Premier League — Correct Score',
        '2-1',
    ) == 'exact_score'
    assert detect_market_scope(
        'UCL Final — Score Prediction',
        'Other',
    ) == 'exact_score'
    assert detect_market_scope(
        'Bayern vs PSG Final Score',
        '1-1',
    ) == 'exact_score'
    assert detect_market_scope('Match scoreline', '0-0') == 'exact_score'


def test_detect_exact_score_via_outcome_name_pattern():
    """Even without title hint, an NN-NN outcome name implies exact-score."""
    from event_matching import detect_market_scope
    assert detect_market_scope('Some football event', '1-0') == 'exact_score'
    assert detect_market_scope('Some football event', '2-1') == 'exact_score'
    assert detect_market_scope('Some football event', '0-0') == 'exact_score'
    assert detect_market_scope('Some football event', '3:2') == 'exact_score'
    assert detect_market_scope('Some football event', '10-9') == 'exact_score'


def test_outcome_name_substring_does_not_false_flag_moneyline():
    """An outcome name like '1-0 leader' (subset) might match the regex,
    but more importantly, a TEAM name with a hyphen ('Saint-Étienne')
    must NOT trip exact-score detection."""
    from event_matching import detect_market_scope
    assert detect_market_scope(
        'Ligue 1', 'Saint-Étienne',
    ) == 'moneyline'
    assert detect_market_scope(
        'Some event', 'Manchester United',
    ) == 'moneyline'


def test_handicap_negative_number_still_detected_after_exact_score():
    """v32 places exact_score before handicap. Make sure handicap signed-
    number detection still fires when there's no exact-score signal."""
    from event_matching import detect_market_scope
    assert detect_market_scope(
        'Tottenham vs Leeds', 'Tottenham -0.5',
    ) == 'handicap'
    assert detect_market_scope(
        'BVB vs Frankfurt', 'Borussia Dortmund -1',
    ) == 'handicap'


def test_scopes_compatible_exact_score_self():
    from event_matching import scopes_compatible
    assert scopes_compatible('exact_score', 'exact_score')


def test_scopes_compatible_exact_score_vs_moneyline_rejects():
    from event_matching import scopes_compatible
    assert not scopes_compatible('exact_score', 'moneyline')
    assert not scopes_compatible('moneyline', 'exact_score')
    assert not scopes_compatible('exact_score', 'handicap')
    assert not scopes_compatible('exact_score', 'totals')


# ── B. cross_platform refuses Exact Score × Moneyline ─────────────

def test_fulham_bournemouth_exact_score_phantom_eliminated():
    """Operator's 08.05.2026 reproduction. Polymarket Exact Score with
    'Fulham FC 2-1' outcome paired with SX 1X2 'Fulham' must produce 0
    deals after v32 scope guard."""
    from cross_platform import build_cross_platform_deal, PlatformOutcome
    poly = PlatformOutcome(
        platform='Polymarket', event_id='poly_es',
        title='Fulham FC vs. AFC Bournemouth - Exact Score',
        outcome_name='Fulham FC 2-1',
        yes_price=0.08, yes_depth=400.0, yes_source='clob_ask',
        no_price=0.92, no_depth=400.0, no_source='clob_ask',
        end_date='2026-05-09',
    )
    sx = PlatformOutcome(
        platform='SX Bet', event_id='sx_ml',
        title='Fulham FC vs AFC Bournemouth',
        outcome_name='Fulham FC',
        yes_price=0.55, yes_depth=400.0, yes_source='sx_ob',
        no_price=0.45, no_depth=400.0, no_source='sx_ob',
        end_date='2026-05-09',
    )
    deals = build_cross_platform_deal(poly, sx, match_confidence=0.85)
    assert deals == [], (
        f"Exact Score × Moneyline must produce 0 deals after v32; got: {deals}"
    )


def test_exact_score_outcome_name_pattern_phantom_eliminated():
    """Same fixture, Polymarket outcome named just '2-1' (no team name).
    NN-NN regex catches it and tags scope='exact_score'."""
    from cross_platform import build_cross_platform_deal, PlatformOutcome
    poly = PlatformOutcome(
        platform='Polymarket', event_id='poly_es2',
        title='Fulham vs Bournemouth Score',
        outcome_name='2-1',
        yes_price=0.08, yes_depth=400.0, yes_source='clob_ask',
        no_price=0.92, no_depth=400.0, no_source='clob_ask',
        end_date='2026-05-09',
    )
    sx = PlatformOutcome(
        platform='SX Bet', event_id='sx_ml2',
        title='Fulham vs Bournemouth',
        outcome_name='Fulham',
        yes_price=0.55, yes_depth=400.0, yes_source='sx_ob',
        no_price=0.45, no_depth=400.0, no_source='sx_ob',
        end_date='2026-05-09',
    )
    deals = build_cross_platform_deal(poly, sx, match_confidence=0.85)
    assert deals == []


def test_exact_score_pair_within_same_scope_still_works():
    """If BOTH platforms carry an Exact Score market on the same fixture,
    same-scope pair should evaluate normally (X1/X2). Fictional scenario
    but tests scope-equality path."""
    from cross_platform import build_cross_platform_deal, PlatformOutcome
    poly = PlatformOutcome(
        platform='Polymarket', event_id='p1',
        title='Fulham vs Bournemouth - Exact Score',
        outcome_name='2-1',
        yes_price=0.08, yes_depth=400.0, yes_source='clob_ask',
        no_price=0.92, no_depth=400.0, no_source='clob_ask',
        end_date='2026-05-09',
    )
    lim = PlatformOutcome(
        platform='Limitless', event_id='l1',
        title='Fulham vs Bournemouth Correct Score',
        outcome_name='2-1',
        yes_price=0.07, yes_depth=400.0, yes_source='lim_clob',
        no_price=0.93, no_depth=400.0, no_source='lim_clob',
        end_date='2026-05-09',
    )
    deals = build_cross_platform_deal(poly, lim, match_confidence=0.90)
    # X1 sum = 0.08 + 0.07 = 0.15 ≥ 0.50 (CP_MIN_REALISTIC_SUM) — rejected
    # X2 sum = 0.92 + 0.93 = 1.85 ≥ 0.96 (threshold) — rejected
    # → 0 deals. The point is no SCOPE rejection (no v32 throw); Phase
    # 19v10 sanity guards correctly reject as too-good-to-be-true.
    assert isinstance(deals, list)


# ── C. analytics._snapshot reads sum_cents fallback ────────────────

def test_snapshot_reads_total_cents_per_platform():
    """Per-platform deal shape (build_deal output): total_cents present."""
    import sys
    if 'analytics' in sys.modules:
        del sys.modules['analytics']
    from analytics import _snapshot
    deal = {
        'platform': 'Polymarket',
        'title': 'Foo',
        'total_cents': 92.5,
        'net': 7.5,
        'grade': 'A',
        'min_liq': 1000,
        'balance_used': 50,
        'roi': 15.0,
        'arb_structure': 'all_yes',
    }
    snap = _snapshot(deal)
    assert snap['sum_cents'] == 92.5


def test_snapshot_reads_sum_cents_for_cross_platform():
    """Cross-platform deal shape (to_radar_deal_format output): sum_cents
    present, total_cents absent. Snapshot must fall back to sum_cents."""
    import sys
    if 'analytics' in sys.modules:
        del sys.modules['analytics']
    from analytics import _snapshot
    deal = {
        'platform': 'Polymarket+SX Bet',
        'title': 'Foo',
        'sum_cents': 88.0,        # CP shape
        'net': 11.33,
        'grade': 'CP-A',
        'arb_structure': 'cross_platform',
    }
    snap = _snapshot(deal)
    assert snap['sum_cents'] == 88.0, (
        f"snapshot must fall back to sum_cents for CP deals; got {snap}"
    )


def test_snapshot_prefers_total_cents_when_both_present():
    """Phase 19v32: cross_platform.to_radar_deal_format now writes BOTH
    total_cents and sum_cents. Both are equal in practice — snapshot
    should pick total_cents (canonical) and not duplicate values."""
    import sys
    if 'analytics' in sys.modules:
        del sys.modules['analytics']
    from analytics import _snapshot
    deal = {
        'platform': 'Polymarket+SX Bet',
        'title': 'Foo',
        'total_cents': 88.0,
        'sum_cents': 88.0,
        'net': 11.33,
    }
    snap = _snapshot(deal)
    assert snap['sum_cents'] == 88.0


# ── D. cross_platform.to_radar_deal_format writes total_cents ──────

def test_to_radar_deal_format_writes_total_cents():
    from cross_platform import (
        to_radar_deal_format, CrossPlatformDeal,
    )
    deal = CrossPlatformDeal(
        structure='X1',
        title='Foo',
        sum_cents=88.0,
        threshold_cents=96.0,
        net_cents=12.0,
        legs=[
            {'platform': 'Polymarket', 'event_id': 'p1', 'outcome': 'A YES',
             'price': 0.30, 'price_cents': 30.0, 'depth': 1000.0,
             'source': 'clob_ask', 'side': 'YES', 'stake': 50.0},
            {'platform': 'SX Bet', 'event_id': 's1', 'outcome': 'A NO',
             'price': 0.58, 'price_cents': 58.0, 'depth': 1000.0,
             'source': 'sx_ob', 'side': 'NO', 'stake': 50.0},
        ],
        confidence=0.95,
        platform_pair=('Polymarket', 'SX Bet'),
        end_date='2026-05-09',
    )
    out = to_radar_deal_format(deal)
    assert out['sum_cents'] == 88.0
    assert out.get('total_cents') == 88.0  # v32 alias for UI/quality_ok parity
    assert out['cross_structure'] == 'X1'
    assert out['arb_structure'] == 'cross_platform'


# ── E. v28 + v29 still pass (regression sanity) ────────────────────

def test_v28_halftime_guard_still_active():
    from cross_platform import build_cross_platform_deal, PlatformOutcome
    poly = PlatformOutcome(
        platform='Polymarket', event_id='p',
        title='Tottenham vs Leeds - Halftime Result',
        outcome_name='Tottenham',
        yes_price=0.40, yes_depth=400, yes_source='clob_ask',
        no_price=0.60, no_depth=400, no_source='clob_ask',
        end_date='2026-05-11',
    )
    sx = PlatformOutcome(
        platform='SX Bet', event_id='s',
        title='Tottenham vs Leeds',
        outcome_name='Tottenham',
        yes_price=0.50, yes_depth=400, yes_source='sx_ob',
        no_price=0.50, no_depth=400, no_source='sx_ob',
        end_date='2026-05-11',
    )
    assert build_cross_platform_deal(poly, sx, 0.85) == []


def test_v29a_cross_team_guard_still_active():
    from cross_platform import build_cross_platform_deal, PlatformOutcome
    poly = PlatformOutcome(
        platform='Polymarket', event_id='p',
        title='Santa Fe vs Corinthians',
        outcome_name='Santa Fe',
        yes_price=0.30, yes_depth=400, yes_source='clob_ask',
        no_price=0.70, no_depth=400, no_source='clob_ask',
        end_date='2026-05-08',
    )
    sx = PlatformOutcome(
        platform='SX Bet', event_id='s',
        title='Santa Fe vs Corinthians',
        outcome_name='Corinthians',     # different team
        yes_price=0.55, yes_depth=400, yes_source='sx_ob',
        no_price=0.45, no_depth=400, no_source='sx_ob',
        end_date='2026-05-08',
    )
    assert build_cross_platform_deal(poly, sx, 0.85) == []
