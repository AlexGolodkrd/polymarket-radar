"""Phase 19v10 (04.05.2026) — audit fixes from operator screenshot review.

User reported History showing:
- XRP cross-platform Net=$47.80, Min liq=$4061  → PHANTOM (96% edge unrealistic)
- Bitcoin Up/Down 88c, Net=$0.83, Min liq=$7   → mosquito (legitimate but tiny)
- Singapore temp 93.3c, Net=$0.06, Min liq=$0  → mosquito phantom

Plus pool_poly_near=237 vs visible NEAR=0 → granular diagnostic counters
needed to explain WHY rejections happen.

Three fixes:

1. **CP min depth + sum sanity guard** — reject cross-platform deals with
   sum < $0.50 (96% edge = fuzzy-match phantom across different events).
2. **CP `to_radar_deal_format` accurate net** — `net` was hardcoded
   `net_cents/100*50` (assumes $50 stake). Now uses actual stake from
   min depth across legs.
3. **NEAR rejection breakdown** — `_best_near_structure` now fills
   `_reason_out` so operator sees granular reasons (cold-cache vs
   no-arb-near-threshold) in `near_diag`.
"""
import os, sys, pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


# ── Cross-platform sanity caps ─────────────────────────────────────

def test_cp_rejects_phantom_low_sum():
    """sum_x1=4¢ (96% edge) is unrealistic — likely fuzzy-match phantom."""
    from cross_platform import build_cross_platform_deal, PlatformOutcome
    out_a = PlatformOutcome(
        platform='Polymarket', event_id='1', title='XRP price on May 4',
        outcome_name='XRP', yes_price=0.02, yes_depth=4000, yes_source='clob_ask',
        no_price=0.98, no_depth=10, no_source='clob_ask',
        end_date='2026-05-04')
    out_b = PlatformOutcome(
        platform='Limitless', event_id='2', title='XRP price on May 4',
        outcome_name='XRP', yes_price=0.02, yes_depth=10, yes_source='lim_clob',
        no_price=0.02, no_depth=4000, no_source='lim_clob',
        end_date='2026-05-04')
    deals = build_cross_platform_deal(out_a, out_b, match_confidence=0.95)
    # Sum_x1 = 0.02 + 0.02 = 0.04 (4¢) → phantom rejected
    assert all(d.structure != 'X1' or d.sum_cents >= 50 for d in deals), \
        "X1 with sum<50¢ should be rejected as phantom"


def test_cp_rejects_low_depth():
    """leg depth $1 below CP_MIN_LEG_DEPTH=$5 → reject."""
    from cross_platform import build_cross_platform_deal, PlatformOutcome
    out_a = PlatformOutcome(
        platform='Polymarket', event_id='1', title='Test',
        outcome_name='X', yes_price=0.45, yes_depth=1.0, yes_source='clob_ask',  # only $1
        no_price=0.55, no_depth=100, no_source='clob_ask',
        end_date='2026-05-04')
    out_b = PlatformOutcome(
        platform='Limitless', event_id='2', title='Test',
        outcome_name='X', yes_price=0.45, yes_depth=100, yes_source='lim_clob',
        no_price=0.50, no_depth=100, no_source='lim_clob',
        end_date='2026-05-04')
    deals = build_cross_platform_deal(out_a, out_b, match_confidence=0.95)
    # X1 needs out_a.yes_depth >= 5 → with 1.0 should be rejected
    x1_deals = [d for d in deals if d.structure == 'X1']
    assert len(x1_deals) == 0, "X1 with low depth should be rejected"


def test_cp_accepts_realistic_arb():
    """sum=$0.94 (6% edge, $1000 depth) is a real cross-platform arb."""
    from cross_platform import build_cross_platform_deal, PlatformOutcome
    out_a = PlatformOutcome(
        platform='Polymarket', event_id='1', title='Game May 4',
        outcome_name='Lakers', yes_price=0.45, yes_depth=1000,
        yes_source='clob_ask', no_price=0.55, no_depth=1000,
        no_source='clob_ask', end_date='2026-05-04')
    out_b = PlatformOutcome(
        platform='Limitless', event_id='2', title='Game May 4',
        outcome_name='Lakers', yes_price=0.51, yes_depth=1000,
        yes_source='lim_clob', no_price=0.49, no_depth=1000,
        no_source='lim_clob', end_date='2026-05-04')
    deals = build_cross_platform_deal(out_a, out_b, match_confidence=0.95)
    # X1 = yes_a + no_b = 0.45 + 0.49 = 0.94 (94c, 6% edge) → ACCEPT
    x1 = [d for d in deals if d.structure == 'X1']
    assert len(x1) == 1
    assert 90 <= x1[0].sum_cents <= 95


# ── to_radar_deal_format net accuracy ──────────────────────────────

def test_radar_format_net_uses_actual_stake():
    """net should be actual_stake × edge, not hardcoded $50."""
    from cross_platform import (
        build_cross_platform_deal, PlatformOutcome, to_radar_deal_format)
    out_a = PlatformOutcome(
        platform='Polymarket', event_id='1', title='Game May 4',
        outcome_name='X', yes_price=0.45, yes_depth=10,    # only $10 depth
        yes_source='clob_ask', no_price=0.55, no_depth=10,
        no_source='clob_ask', end_date='2026-05-04')
    out_b = PlatformOutcome(
        platform='Limitless', event_id='2', title='Game May 4',
        outcome_name='X', yes_price=0.51, yes_depth=10,
        yes_source='lim_clob', no_price=0.49, no_depth=10,
        no_source='lim_clob', end_date='2026-05-04')
    deals = build_cross_platform_deal(out_a, out_b, match_confidence=0.95)
    assert deals
    radar_dict = to_radar_deal_format(deals[0])
    # Net = stake $10 × edge 6¢ = $0.60, NOT $3 (which $50 stake would give)
    assert 0.4 <= radar_dict['net'] <= 0.8
    assert radar_dict.get('balance_used') == 10.0


# ── Granular NEAR rejection diag ──────────────────────────────────

def test_best_near_structure_reports_empty_pm():
    """Empty pm input fills _reason_out with 'empty_pm'."""
    from arb_server import _best_near_structure
    reason = {}
    assert _best_near_structure([], 0.965, _reason_out=reason) is None
    assert reason.get('key') == 'empty_pm'


def test_best_near_structure_reports_all_implied():
    """All legs with non-real source → fills 'all_legs_implied'."""
    from arb_server import _best_near_structure
    pm = [{'name': 'A', 'yes_price': 0.45, 'yes_liq': 100,
           'yes_src': 'implied', 'no_price': 0.55, 'no_liq': 100,
           'no_src': 'implied', 'volume': 100, 'alive': True}]
    reason = {}
    out = _best_near_structure(pm, 0.965, _reason_out=reason)
    assert out is None
    assert reason.get('key') == 'all_legs_implied'


def test_best_near_structure_reports_no_near():
    """All structures fail threshold proximity → 'no_structure_near_threshold'."""
    from arb_server import _best_near_structure
    # Sums far above threshold
    pm = [
        {'name': 'A', 'yes_price': 0.95, 'yes_liq': 100, 'yes_src': 'clob_ask',
         'no_price': 0.95, 'no_liq': 100, 'no_src': 'clob_ask',
         'volume': 100, 'alive': True},
        {'name': 'B', 'yes_price': 0.95, 'yes_liq': 100, 'yes_src': 'clob_ask',
         'no_price': 0.95, 'no_liq': 100, 'no_src': 'clob_ask',
         'volume': 100, 'alive': True},
    ]
    reason = {}
    out = _best_near_structure(pm, 0.965, _reason_out=reason)
    assert out is None
    assert reason.get('key') == 'no_structure_near_threshold'


def test_best_near_structure_finds_real_arb():
    """Realistic NEAR candidate → returns dict, _reason_out empty."""
    from arb_server import _best_near_structure
    pm = [
        {'name': 'A', 'yes_price': 0.45, 'yes_liq': 100, 'yes_src': 'clob_ask',
         'no_price': 0.55, 'no_liq': 100, 'no_src': 'clob_ask',
         'volume': 100, 'alive': True},
        {'name': 'B', 'yes_price': 0.48, 'yes_liq': 100, 'yes_src': 'clob_ask',
         'no_price': 0.52, 'no_liq': 100, 'no_src': 'clob_ask',
         'volume': 100, 'alive': True},
    ]
    reason = {}
    out = _best_near_structure(pm, 0.965, _reason_out=reason)
    assert out is not None
    assert out['structure'] in ('all_yes', 'yes_no_pair')


def test_near_diag_has_granular_keys():
    """Phase 19v10 — `near_diag` includes granular strict-rejection breakdown."""
    import arb_server
    arb_server.near_summary(clob_res={}, kalshi_res={}, sx_res={}, lim_res={})
    diag = arb_server._last_near_rejection_stats
    assert 'poly_strict_all_implied' in diag
    assert 'poly_strict_no_near' in diag
    assert 'poly_strict_empty_pm' in diag
