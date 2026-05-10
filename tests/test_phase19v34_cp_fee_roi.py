"""Phase 19v34 (09.05.2026) — fee/gross/roi for cross-platform deals.

Operator screenshot 09.05.2026: Le Havre AC × Olympique de Marseille
cross-platform arb (Polymarket Le Havre YES @ 27¢ + SX Le Havre NO @
64.62¢, sum 91.62¢). Dashboard correctly showed Sum 91.62¢ (v32 fix
working) and Net $4.61, but the **GROSS / FEE / ROI / ROI ADJ** columns
all showed 0% — because `to_radar_deal_format` didn't write those fields,
so dashboard.html read undefined → displayed 0%.

Per-platform deals (Polymarket / Limitless / SX Bet single-platform via
build_deal in arb_server.py:1600) DO write these fields. v34 brings
cross-platform output to parity.

Math:
  sum_cents = 91.62 → sum_fraction = 0.9162
  net_cents = 8.38 (= 100 - 91.62)
  actual_stake = min(min_depth, $55) — face value
  gross_dollars  = face × (1 - sum_fraction)
  total_cash     = face × sum_fraction          # capital deployed
  fee_per_leg    = face × leg.price × theta_leg # taker fee on each leg's cash
  total_fee      = Σ fee_per_leg
  net_dollars    = gross_dollars - total_fee
  gross_pct      = gross_dollars / total_cash × 100
  fee_pct        = total_fee     / total_cash × 100
  roi_pct        = net_dollars   / total_cash × 100
  slip_pct       = max(leg.stake) / min_depth × 100, capped 5%
  adj_roi_pct    = (net - slip_cost) / total_cash × 100
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


def _make_le_havre_deal():
    """Reproduce the operator's 09.05.2026 Le Havre AC × Marseille deal."""
    from cross_platform import CrossPlatformDeal
    return CrossPlatformDeal(
        structure='X1',
        title='Le Havre AC vs. Olympique de Marseille',
        sum_cents=91.62,
        threshold_cents=96.0,
        net_cents=8.38,
        legs=[
            {
                'platform': 'Polymarket',
                'event_id': 'p_lehavre',
                'outcome': 'Le Havre AC YES',
                'price': 0.27,
                'price_cents': 27.0,
                'depth': 2_033_305,
                'source': 'clob_ask',
                'side': 'YES',
                'stake': 50.0,
            },
            {
                'platform': 'SX Bet',
                'event_id': 's_lehavre',
                'outcome': 'Le Havre NO',
                'price': 0.6462,
                'price_cents': 64.62,
                'depth': 208_391,
                'source': 'sx_ob',
                'side': 'NO',
                'stake': 50.0,
            },
        ],
        confidence=0.95,
        platform_pair=('Polymarket', 'SX Bet'),
        end_date='2026-05-10T00:00:00Z',
    )


# ── UI fields exist and are non-zero ───────────────────────────────

def test_to_radar_deal_format_writes_gross_pct():
    from cross_platform import to_radar_deal_format
    d = to_radar_deal_format(_make_le_havre_deal())
    assert 'gross_pct' in d
    assert d['gross_pct'] > 0


def test_to_radar_deal_format_writes_fee_pct():
    from cross_platform import to_radar_deal_format
    d = to_radar_deal_format(_make_le_havre_deal())
    assert 'fee_pct' in d
    # Both legs charge taker fee → fee_pct > 0
    assert d['fee_pct'] > 0


def test_to_radar_deal_format_writes_roi():
    from cross_platform import to_radar_deal_format
    d = to_radar_deal_format(_make_le_havre_deal())
    assert 'roi' in d
    # Should be positive on a profitable arb
    assert d['roi'] > 0


def test_to_radar_deal_format_writes_adj_roi():
    from cross_platform import to_radar_deal_format
    d = to_radar_deal_format(_make_le_havre_deal())
    assert 'adj_roi' in d


# ── Math correctness ──────────────────────────────────────────────

def test_gross_pct_equals_inverse_minus_one():
    """gross_dollars / total_cash = (1 - sum) / sum × 100"""
    from cross_platform import to_radar_deal_format
    d = to_radar_deal_format(_make_le_havre_deal())
    # sum=0.9162 → expected gross_pct = (1-0.9162)/0.9162 ×100 ≈ 9.14%
    assert abs(d['gross_pct'] - 9.14) < 0.05


def test_fee_pct_proportional_to_platform_thetas():
    """Polymarket leg cost × 250bps + SX leg cost × 200bps."""
    from cross_platform import to_radar_deal_format
    d = to_radar_deal_format(_make_le_havre_deal())
    # Le Havre @ 27¢ Poly + 64.62¢ SX, face=$55:
    #   poly leg cash = 55 × 0.27 = $14.85, fee = 14.85 × 0.025 = $0.371
    #   sx leg cash   = 55 × 0.6462 = $35.54, fee = 35.54 × 0.02 = $0.711
    #   total fee     = $1.082
    #   total_cash    = 55 × 0.9162 = $50.39
    #   fee_pct       = 1.082 / 50.39 × 100 ≈ 2.15%
    assert abs(d['fee_pct'] - 2.15) < 0.10


def test_net_dollars_equals_gross_minus_fee():
    """The displayed `net` column should be post-fee."""
    from cross_platform import to_radar_deal_format
    d = to_radar_deal_format(_make_le_havre_deal())
    expected_net = d['gross'] - d['fee']
    assert abs(d['net'] - expected_net) < 0.05


def test_roi_pct_post_fee():
    """ROI = net_dollars / total_cash × 100 (post-fee)."""
    from cross_platform import to_radar_deal_format
    d = to_radar_deal_format(_make_le_havre_deal())
    # Expected ROI ≈ (gross - fee) / total_cash × 100
    # ≈ (4.61 - 1.08) / 50.39 ≈ 7%
    assert d['roi'] > 5.0
    assert d['roi'] < 10.0


# ── Theta column shows worst-case (highest) leg ─────────────────────

def test_theta_field_reports_max_leg_theta():
    from cross_platform import to_radar_deal_format
    d = to_radar_deal_format(_make_le_havre_deal())
    # Polymarket 0.025 > SX 0.02 → max = 0.025
    assert d['theta'] == 0.025


def test_theta_zero_for_limitless_only_pair():
    """Two Limitless legs → theta=0 (zero-fee platform)."""
    from cross_platform import CrossPlatformDeal, to_radar_deal_format
    cp = CrossPlatformDeal(
        structure='X1', title='Some Limitless arb',
        sum_cents=85.0, threshold_cents=96.0, net_cents=15.0,
        legs=[
            {'platform': 'Limitless', 'event_id': 'a', 'outcome': 'A YES',
             'price': 0.40, 'price_cents': 40, 'depth': 1000,
             'source': 'lim_clob', 'side': 'YES', 'stake': 50},
            {'platform': 'Limitless', 'event_id': 'b', 'outcome': 'B YES',
             'price': 0.45, 'price_cents': 45, 'depth': 1000,
             'source': 'lim_clob', 'side': 'YES', 'stake': 50},
        ],
        confidence=0.9, platform_pair=('Limitless', 'Limitless'),
        end_date='2026-05-10',
    )
    d = to_radar_deal_format(cp)
    assert d['theta'] == 0.0
    assert d['fee'] == 0.0
    assert d['fee_pct'] == 0.0


# ── Grade scaled to post-fee post-slippage ROI ─────────────────────

def test_grade_cp_a_when_roi_above_2pct():
    from cross_platform import to_radar_deal_format
    d = to_radar_deal_format(_make_le_havre_deal())
    # Le Havre arb has ROI ~7% → grade should be CP-A
    assert d['grade'] == 'CP-A'


def test_grade_cp_f_when_negative():
    """Phantom-tight arb where fees + slip > gross → grade F."""
    from cross_platform import CrossPlatformDeal, to_radar_deal_format
    cp = CrossPlatformDeal(
        structure='X1', title='Phantom arb',
        sum_cents=99.5, threshold_cents=96.0, net_cents=0.5,
        legs=[
            {'platform': 'Polymarket', 'event_id': 'a', 'outcome': 'X YES',
             'price': 0.50, 'price_cents': 50, 'depth': 100,
             'source': 'clob_ask', 'side': 'YES', 'stake': 50},
            {'platform': 'SX Bet', 'event_id': 'b', 'outcome': 'X NO',
             'price': 0.495, 'price_cents': 49.5, 'depth': 100,
             'source': 'sx_ob', 'side': 'NO', 'stake': 50},
        ],
        confidence=0.95, platform_pair=('Polymarket', 'SX Bet'),
        end_date='2026-05-10',
    )
    d = to_radar_deal_format(cp)
    # gross_pct ≈ 0.5%, fee_pct ≈ 2.25%, slip_pct = 5% (max) → adj negative
    assert d['adj_roi'] < 0
    assert d['grade'] == 'CP-F'


# ── Defensive: no legs → safe defaults ─────────────────────────────

def test_empty_legs_yields_zero_metrics():
    from cross_platform import CrossPlatformDeal, to_radar_deal_format
    cp = CrossPlatformDeal(
        structure='X1', title='Empty', sum_cents=0, threshold_cents=96,
        net_cents=0, legs=[], confidence=0.0,
        platform_pair=('Polymarket', 'SX Bet'), end_date=None,
    )
    d = to_radar_deal_format(cp)
    assert d['gross_pct'] == 0.0
    assert d['fee_pct'] == 0.0
    assert d['roi'] == 0.0
    assert d['fee'] == 0.0
