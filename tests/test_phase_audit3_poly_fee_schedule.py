"""Phase audit-3 (12.05.2026) — Polymarket fee model after 31.03.2026.

Polymarket added `feeSchedule: {rate, exponent, takerOnly, rebateRate}`
as the authoritative fee source on gamma /events. snake_case fields
(`maker_base_fee`, `taker_base_fee`) were dropped from gamma. CLOB
/markets/{cid} (which we use for per-market info) still returns
snake_case at this writing, but the per-event feeSchedule on gamma is
the new source of truth — and a market with BOTH shapes can have
divergent values.

Live probe (12.05.2026):
  gamma /events first event:
    feeSchedule = {exponent: 1, rate: 0.04, takerOnly: true, rebateRate: 0.25}
    makerBaseFee = 1000
    takerBaseFee = 1000
    maker_base_fee = None
    taker_base_fee = None

The new `_read_poly_fee_bps(market, side)` helper:
  - Reads `feeSchedule.rate × 10000` as primary
  - Falls back to camelCase `{side}BaseFee`
  - Falls back to snake_case `{side}_base_fee`
  - Respects `feeSchedule.takerOnly` (maker fee = 0 when true)
  - Returns 0.0 only if NONE parseable (no silent corruption)
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


def test_feeschedule_taker_only_maker_zero():
    """Live shape from gamma /events: rate=0.04, takerOnly=true → maker=0, taker=400."""
    import arb_server
    m = {'feeSchedule': {'exponent': 1, 'rate': 0.04,
                          'takerOnly': True, 'rebateRate': 0.25}}
    assert arb_server._read_poly_fee_bps(m, 'maker') == 0.0
    assert arb_server._read_poly_fee_bps(m, 'taker') == 400.0


def test_feeschedule_both_sides_pay():
    """If takerOnly is false, BOTH sides pay the rate."""
    import arb_server
    m = {'feeSchedule': {'rate': 0.02, 'takerOnly': False}}
    assert arb_server._read_poly_fee_bps(m, 'maker') == 200.0
    assert arb_server._read_poly_fee_bps(m, 'taker') == 200.0


def test_feeschedule_takerOnly_default_false():
    """Missing takerOnly key → treat as both-sided (most conservative —
    we'd rather over-account fees than miss them)."""
    import arb_server
    m = {'feeSchedule': {'rate': 0.03}}
    assert arb_server._read_poly_fee_bps(m, 'maker') == 300.0
    assert arb_server._read_poly_fee_bps(m, 'taker') == 300.0


def test_feeschedule_string_rate_coerces():
    """Rate may arrive as string on JSON-decode quirks — coerce."""
    import arb_server
    m = {'feeSchedule': {'rate': '0.04', 'takerOnly': True}}
    assert arb_server._read_poly_fee_bps(m, 'taker') == 400.0


def test_feeschedule_malformed_falls_back():
    """If feeSchedule is present but unusable (string, list, missing rate),
    fall through to camelCase / snake_case."""
    import arb_server
    # Not a dict → ignored, falls back to camelCase
    m = {'feeSchedule': 'not a dict', 'takerBaseFee': 250}
    assert arb_server._read_poly_fee_bps(m, 'taker') == 250.0
    # Dict but no rate → ignored
    m = {'feeSchedule': {'exponent': 1}, 'taker_base_fee': 150}
    assert arb_server._read_poly_fee_bps(m, 'taker') == 150.0


def test_camelcase_fallback():
    """When feeSchedule absent, camelCase wins over snake_case (newer field)."""
    import arb_server
    m = {'makerBaseFee': 100, 'maker_base_fee': 999}
    assert arb_server._read_poly_fee_bps(m, 'maker') == 100.0


def test_snake_case_fallback():
    """CLOB /markets/{cid} shape — only snake_case present."""
    import arb_server
    m = {'maker_base_fee': 50, 'taker_base_fee': 250}
    assert arb_server._read_poly_fee_bps(m, 'maker') == 50.0
    assert arb_server._read_poly_fee_bps(m, 'taker') == 250.0


def test_missing_everything_returns_zero():
    """No fee fields anywhere → 0.0 (current legacy behavior preserved)."""
    import arb_server
    assert arb_server._read_poly_fee_bps({}, 'maker') == 0.0
    assert arb_server._read_poly_fee_bps({}, 'taker') == 0.0


def test_non_dict_input_returns_zero():
    """Defensive: None / list / string inputs don't crash."""
    import arb_server
    assert arb_server._read_poly_fee_bps(None, 'taker') == 0.0
    assert arb_server._read_poly_fee_bps([], 'taker') == 0.0
    assert arb_server._read_poly_fee_bps('', 'taker') == 0.0


def test_zero_rate_in_feeschedule_returns_zero():
    """rate=0 is a legitimate value (free market) — must be returned, not
    skipped to a fallback that might have a stale non-zero value."""
    import arb_server
    m = {'feeSchedule': {'rate': 0.0, 'takerOnly': True},
         'takerBaseFee': 250, 'taker_base_fee': 250}
    assert arb_server._read_poly_fee_bps(m, 'taker') == 0.0


def test_compute_poly_threshold_with_real_fee():
    """End-to-end: with the live shape (4% taker fee, takerOnly), our
    detection threshold becomes much stricter than the legacy 0-fee
    code path. Documents the impact of the fix on detection."""
    import arb_server
    m = {'feeSchedule': {'rate': 0.04, 'takerOnly': True}}
    taker_bps = arb_server._read_poly_fee_bps(m, 'taker')
    assert taker_bps == 400.0
    # Threshold for a 2-leg arb with real fees should be substantially
    # below 1.0 (specifically: 1 - theta * n_legs - safety_buffer).
    # Old behavior: taker_bps=0 → threshold ≈ 0.97 (only safety buffer)
    # New behavior: taker_bps=400 → threshold ≈ 0.93 (real arb gate)
    new_thresh = arb_server.compute_poly_threshold(taker_bps, n_legs=2)
    old_thresh = arb_server.compute_poly_threshold(0, n_legs=2)
    assert new_thresh < old_thresh, (
        f"4% fee should LOWER threshold (more selective). "
        f"new={new_thresh}, old={old_thresh}")
    # And it should be significantly lower — at least 2 cents.
    assert (old_thresh - new_thresh) >= 0.02
