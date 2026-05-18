"""Phase audit-3 (15.05.2026) — fee_tracker EMA + threshold integration.

Operator pain: Limitless `feeRateBps=300` (Bronze 3%) was the signed
value, but live `execution.effectiveFeeBps=0` (promo for new accounts).
Hardcoded threshold THRESH_LIMITLESS=0.99 assumed 0% — worked by luck.
If Limitless silently turns the promo off, threshold becomes too loose
and the radar fires fake arbs.

The fee_tracker module records `effective_fee_bps` from each
`fire_filled` event into a per-platform EMA. `compute_*_threshold()`
calculators query this EMA before falling back to the API-declared fee.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


# ── Direct fee_tracker API ────────────────────────────────────────

def test_initial_state_returns_default():
    """No observations → caller's default is returned untouched."""
    from fee_tracker import get_effective_fee_bps, reset_for_tests
    reset_for_tests()
    assert get_effective_fee_bps('limitless', 300.0) == 300.0
    assert get_effective_fee_bps('polymarket', 100.0) == 100.0
    assert get_effective_fee_bps('sx_bet', 200.0) == 200.0


def test_below_min_samples_returns_default():
    """1-2 observations are NOT enough to override the default — guards
    against a single anomalous fill biasing threshold."""
    from fee_tracker import record_fee_observation, get_effective_fee_bps, reset_for_tests, MIN_SAMPLES
    reset_for_tests()
    record_fee_observation('limitless', 0.0)
    record_fee_observation('limitless', 0.0)
    # MIN_SAMPLES is 3 by default → 2 isn't enough
    if MIN_SAMPLES > 2:
        assert get_effective_fee_bps('limitless', 300.0) == 300.0


def test_ema_converges_to_observation():
    """N observations of the same value → EMA converges to that value."""
    from fee_tracker import record_fee_observation, get_effective_fee_bps, reset_for_tests
    reset_for_tests()
    for _ in range(10):
        record_fee_observation('limitless', 0.0)
    # After 10 zeros, EMA is essentially 0 (within float epsilon)
    ema = get_effective_fee_bps('limitless', 300.0)
    assert abs(ema) < 0.01, f"EMA should approach 0; got {ema}"


def test_ema_reacts_to_change():
    """When observations switch from one value to another, EMA tracks."""
    from fee_tracker import record_fee_observation, get_effective_fee_bps, reset_for_tests
    reset_for_tests()
    # 5 observations at 0 bps (promo on)
    for _ in range(5):
        record_fee_observation('limitless', 0.0)
    ema_before = get_effective_fee_bps('limitless', 300.0)
    assert ema_before < 50.0  # near zero
    # 10 observations at 300 bps (promo off)
    for _ in range(10):
        record_fee_observation('limitless', 300.0)
    ema_after = get_effective_fee_bps('limitless', 300.0)
    # EMA should now be much closer to 300 than 0
    assert ema_after > 200.0, f"EMA should track upward; got {ema_after}"


def test_per_platform_isolation():
    """Observations on one platform don't bleed into another."""
    from fee_tracker import record_fee_observation, get_effective_fee_bps, reset_for_tests
    reset_for_tests()
    for _ in range(5):
        record_fee_observation('limitless', 0.0)
        record_fee_observation('sx_bet', 200.0)
    lim = get_effective_fee_bps('limitless', 300.0)
    sx = get_effective_fee_bps('sx_bet', 999.0)
    assert lim < 50.0
    assert sx > 150.0


def test_none_observation_ignored():
    """Passing None never crashes and never updates the EMA."""
    from fee_tracker import record_fee_observation, get_effective_fee_bps, reset_for_tests
    reset_for_tests()
    for _ in range(3):
        record_fee_observation('limitless', None)
    # Still default, no observations counted
    assert get_effective_fee_bps('limitless', 300.0) == 300.0


def test_invalid_observation_ignored():
    """Non-numeric strings are skipped, not crashed on."""
    from fee_tracker import record_fee_observation, get_effective_fee_bps, reset_for_tests
    reset_for_tests()
    for _ in range(3):
        record_fee_observation('limitless', 'not a number')
    assert get_effective_fee_bps('limitless', 300.0) == 300.0


def test_platform_aliases_normalised():
    """'SX Bet' vs 'sx_bet' vs 'sx' all refer to the same bucket."""
    from fee_tracker import record_fee_observation, get_effective_fee_bps, reset_for_tests
    reset_for_tests()
    record_fee_observation('SX Bet', 50.0)
    record_fee_observation('sx_bet', 50.0)
    record_fee_observation('sx', 50.0)
    record_fee_observation('SxBet', 50.0)
    # 4 observations across the same canonical key
    assert get_effective_fee_bps('sx_bet', 200.0) < 100.0


def test_snapshot_returns_state():
    from fee_tracker import record_fee_observation, snapshot, reset_for_tests
    reset_for_tests()
    record_fee_observation('limitless', 100.0)
    snap = snapshot()
    assert 'limitless' in snap
    assert snap['limitless']['samples'] == 1
    assert snap['limitless']['ema_bps'] == 100.0


# ── compute_*_threshold integration ────────────────────────────────

def test_compute_limitless_threshold_default():
    """Without observations, threshold uses default_fee_bps=300 → 0.967."""
    from fee_tracker import reset_for_tests
    reset_for_tests()
    from arb_server import compute_limitless_threshold
    t = compute_limitless_threshold(default_threshold=0.99, default_fee_bps=300.0)
    # 1 - (0.03 + 0.003) = 0.967, below ceiling 0.99
    assert 0.96 < t < 0.97


def test_compute_limitless_threshold_with_zero_fee_observations():
    """Promo case: many 0 bps observations → threshold close to 0.997
    (1 - 0.003 slip), but clamped by default_threshold ceiling."""
    from fee_tracker import record_fee_observation, reset_for_tests
    reset_for_tests()
    for _ in range(10):
        record_fee_observation('limitless', 0.0)
    from arb_server import compute_limitless_threshold
    t = compute_limitless_threshold(default_threshold=0.99, default_fee_bps=300.0)
    # 1 - 0.003 = 0.997, BUT ceiling 0.99 → 0.99
    assert t == 0.99


def test_compute_limitless_threshold_no_ceiling():
    """When default_threshold=None, no ceiling applied."""
    from fee_tracker import record_fee_observation, reset_for_tests
    reset_for_tests()
    for _ in range(10):
        record_fee_observation('limitless', 0.0)
    from arb_server import compute_limitless_threshold
    t = compute_limitless_threshold(default_threshold=None, default_fee_bps=300.0)
    # 1 - 0.003 = 0.997
    assert 0.995 < t < 0.999


def test_compute_sx_threshold_default():
    from fee_tracker import reset_for_tests
    reset_for_tests()
    from arb_server import compute_sx_threshold
    t = compute_sx_threshold(default_threshold=0.98, default_fee_bps=200.0)
    # 1 - (0.02 + 0.003) = 0.977
    assert 0.97 < t < 0.98


def test_compute_poly_threshold_uses_ema_when_available():
    """Polymarket threshold also adapts to live fee — same plumbing."""
    from fee_tracker import record_fee_observation, reset_for_tests
    reset_for_tests()
    # 10 observations at 0 bps → EMA near 0
    for _ in range(10):
        record_fee_observation('polymarket', 0.0)
    from arb_server import compute_poly_threshold
    # Caller passes API-declared 400 bps; EMA should override to ~0,
    # giving threshold near 1 - 0.008 = 0.992
    t = compute_poly_threshold(taker_fee_bps=400.0)
    # The threshold may be clamped by POLY_DYNAMIC_THRESH_CAP — verify
    # it's at least as generous as the no-fee case (>= 0.985 sanity)
    assert t > 0.985, f"With EMA=0 bps, threshold should be ≥ 0.985; got {t}"


# ── analytics integration ──────────────────────────────────────────

def test_record_fire_filled_feeds_fee_tracker(monkeypatch, tmp_path):
    """A fire_filled event with effective_fee_bps in legs must update
    the EMA on the corresponding platform."""
    # Redirect analytics persistence to a temp dir so the test doesn't
    # touch live state.
    monkeypatch.setenv('EXECUTIONS_DIR', str(tmp_path))
    from fee_tracker import reset_for_tests, snapshot
    reset_for_tests()
    # Re-import analytics so it picks up the temp dir
    import importlib, analytics
    importlib.reload(analytics)
    leg_details = [
        {'status': 'filled', 'fill_size_usdc': 1.0,
         'platform': 'limitless', 'effective_fee_bps': 0.0,
         'fill_price': 0.5, 'slug': 'foo'},
        {'status': 'filled', 'fill_size_usdc': 1.0,
         'platform': 'sx_bet', 'effective_fee_bps': 180.0,
         'fill_price': 0.5, 'market_hash': '0xabc'},
    ]
    analytics.record_fire_filled('test-arb', {
        'platform': 'Limitless+SX Bet',
        'title': 'Test fixture',
        'arb_structure': 'cross_platform',
        'sum_cents': 95.0,
        'net': 0.05,
    }, leg_details)
    snap = snapshot()
    assert snap.get('limitless', {}).get('samples') == 1
    assert snap.get('limitless', {}).get('ema_bps') == 0.0
    assert snap.get('sx_bet', {}).get('samples') == 1
    assert snap.get('sx_bet', {}).get('ema_bps') == 180.0
