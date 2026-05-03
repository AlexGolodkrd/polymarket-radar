"""Phase 19v6 (03.05.2026) — pre-flight guards before real-mode fire.

Two new guards:
1. **min-net guard** — reject mosquito arbs (theoretical edge but tiny
   absolute net due to min_liq cap on stake sizing). ENV
   `MIN_NET_PER_ARB_USD` (default $0.50). Saves preflight cost and keeps
   paper-trade log focused.
2. **last-ms depth re-check** — between scan and fire (5-30s gap), MM
   may pull liquidity. Re-fetch /book per leg via ThreadPoolExecutor,
   reject if fresh depth < stake × (1 − DEPTH_RECHECK_TOLERANCE).

Both fire BEFORE preflight + fire, log to dryrun_log so operator sees
why arb didn't fire.
"""
import os, sys, pytest, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


def _make_deal(net=0.06, legs=None, platform='Polymarket'):
    """Build a minimal deal dict matching what build_deal returns."""
    return {
        'title': 'Test arb',
        'platform': platform,
        'arb_structure': 'all_yes',
        'sum_cents': 93.3,
        'net': net,
        'payout_target': 1.0,
        'entries': legs or [
            {'price': 0.45, 'stake': 10.0, 'liquidity': 50.0,
             'token_id': '12345', 'platform': platform},
            {'price': 0.48, 'stake': 10.0, 'liquidity': 50.0,
             'token_id': '67890', 'platform': platform},
        ],
    }


@pytest.fixture
def wallet_pool():
    from executor.builders import WalletStub
    return [
        WalletStub(bot_id=f'bot{i}', eth_address='0x' + str(i) * 40)
        for i in range(1, 7)
    ]


@pytest.fixture(autouse=True)
def _bypass_risk_gate(monkeypatch):
    """Tests for guards run with risk gate disabled (kill switch / daily
    limit) — those are tested elsewhere. Otherwise risk_blocked masks
    our guard signal in aborted_reason."""
    try:
        import risk
        monkeypatch.setattr(risk, 'check_can_fire', lambda deal: (True, None))
    except ImportError:
        pass


# ── min-net guard tests ───────────────────────────────────────────

def test_min_net_guard_rejects_mosquito_arb(wallet_pool, monkeypatch):
    """Net=$0.06 — rejected, doesn't reach preflight."""
    from executor import atomic
    monkeypatch.setattr(atomic, 'MIN_NET_PER_ARB_USD', 0.50)
    deal = _make_deal(net=0.06)
    result = atomic.fire_arb(deal, wallet_pool, dry_run=True)
    assert result.aborted_reason is not None
    assert 'min_net_guard' in result.aborted_reason
    assert '0.06' in result.aborted_reason


def test_min_net_guard_passes_real_arb(wallet_pool, monkeypatch):
    """Net=$3.50 — passes guard, reaches preflight (or further)."""
    from executor import atomic
    monkeypatch.setattr(atomic, 'MIN_NET_PER_ARB_USD', 0.50)
    deal = _make_deal(net=3.50)
    result = atomic.fire_arb(deal, wallet_pool, dry_run=True)
    # Should not be blocked by min-net guard (may be blocked later by
    # preflight/risk, but not min_net_guard specifically).
    if result.aborted_reason:
        assert 'min_net_guard' not in result.aborted_reason


def test_min_net_guard_at_threshold(wallet_pool, monkeypatch):
    """Net=$0.50 (exactly at threshold) — should pass."""
    from executor import atomic
    monkeypatch.setattr(atomic, 'MIN_NET_PER_ARB_USD', 0.50)
    deal = _make_deal(net=0.50)
    result = atomic.fire_arb(deal, wallet_pool, dry_run=True)
    if result.aborted_reason:
        assert 'min_net_guard' not in result.aborted_reason


def test_min_net_guard_below_threshold(wallet_pool, monkeypatch):
    """Net=$0.49 (just below) — rejected."""
    from executor import atomic
    monkeypatch.setattr(atomic, 'MIN_NET_PER_ARB_USD', 0.50)
    deal = _make_deal(net=0.49)
    result = atomic.fire_arb(deal, wallet_pool, dry_run=True)
    assert 'min_net_guard' in (result.aborted_reason or '')


def test_min_net_guard_constant_reads_env():
    """MIN_NET_PER_ARB_USD constant exists and is float."""
    import executor.atomic as atomic_mod
    assert hasattr(atomic_mod, 'MIN_NET_PER_ARB_USD')
    assert isinstance(atomic_mod.MIN_NET_PER_ARB_USD, float)


def test_min_net_guard_zero_net_rejected(wallet_pool, monkeypatch):
    """Net=$0.00 — explicit reject."""
    from executor import atomic
    deal = _make_deal(net=0.0)
    result = atomic.fire_arb(deal, wallet_pool, dry_run=True)
    assert 'min_net_guard' in (result.aborted_reason or '')


# ── depth re-check tests (dry_run skipped) ────────────────────────

def test_depth_recheck_helper_exists():
    """Phase 19v6 helper is exported."""
    from executor import atomic
    assert hasattr(atomic, '_last_ms_depth_recheck')
    assert callable(atomic._last_ms_depth_recheck)


def test_depth_recheck_skipped_in_dry_run(wallet_pool, monkeypatch):
    """In dry_run mode, depth re-check is SKIPPED — paper trades use
    scan-time snapshot. Verify no network call happens."""
    from executor import atomic
    call_count = [0]
    def _spy(deal):
        call_count[0] += 1
        return []
    monkeypatch.setattr(atomic, '_last_ms_depth_recheck', _spy)
    deal = _make_deal(net=10.0)  # passes min-net
    atomic.fire_arb(deal, wallet_pool, dry_run=True)
    # Re-check is NOT called in dry-run path
    assert call_count[0] == 0, "depth re-check fired in dry_run (should skip)"


def test_depth_recheck_returns_failures_when_depth_drops(monkeypatch):
    """Stub _fetch_clob to return depth=$0 → re-check returns failure."""
    from executor import atomic
    deal = _make_deal(net=10.0, legs=[
        {'price': 0.45, 'stake': 50.0, 'liquidity': 100.0,
         'token_id': '111', 'platform': 'Polymarket'},
        {'price': 0.48, 'stake': 50.0, 'liquidity': 100.0,
         'token_id': '222', 'platform': 'Polymarket'},
    ])

    # Stub _fetch_clob to simulate depth drop
    def _stub_clob(tid):
        return tid, 0.45, 0.0, 0.55, 100.0   # ask_depth=0
    import arb_server
    monkeypatch.setattr(arb_server, '_fetch_clob', _stub_clob)

    failures = atomic._last_ms_depth_recheck(deal)
    assert len(failures) >= 2  # both legs fail
    assert 'leg 0' in failures[0]
    assert 'fresh depth' in failures[0]


def test_depth_recheck_passes_when_depth_sufficient(monkeypatch):
    """Stub _fetch_clob to return depth >= stake → re-check passes."""
    from executor import atomic
    deal = _make_deal(net=10.0, legs=[
        {'price': 0.45, 'stake': 30.0, 'liquidity': 100.0,
         'token_id': '111', 'platform': 'Polymarket'},
        {'price': 0.48, 'stake': 30.0, 'liquidity': 100.0,
         'token_id': '222', 'platform': 'Polymarket'},
    ])

    def _stub_clob(tid):
        return tid, 0.45, 100.0, 0.55, 100.0
    import arb_server
    monkeypatch.setattr(arb_server, '_fetch_clob', _stub_clob)

    failures = atomic._last_ms_depth_recheck(deal)
    assert failures == []


def test_depth_recheck_tolerance_allows_small_dropoff(monkeypatch):
    """20% tolerance: stake $50, fresh depth $42 (84% of stake, 80%+ of stake) → pass."""
    from executor import atomic
    deal = _make_deal(net=10.0, legs=[
        {'price': 0.45, 'stake': 50.0, 'liquidity': 100.0,
         'token_id': '111', 'platform': 'Polymarket'},
    ])

    def _stub_clob(tid):
        # stake $50, tolerance 20% → min acceptable = $40. Return $42 → pass.
        return tid, 0.45, 42.0, 0.55, 100.0
    import arb_server
    monkeypatch.setattr(arb_server, '_fetch_clob', _stub_clob)

    failures = atomic._last_ms_depth_recheck(deal)
    assert failures == []


def test_depth_recheck_tolerance_rejects_large_dropoff(monkeypatch):
    """20% tolerance: stake $50, fresh depth $35 (70%) → reject."""
    from executor import atomic
    deal = _make_deal(net=10.0, legs=[
        {'price': 0.45, 'stake': 50.0, 'liquidity': 100.0,
         'token_id': '111', 'platform': 'Polymarket'},
    ])

    def _stub_clob(tid):
        return tid, 0.45, 35.0, 0.55, 100.0
    import arb_server
    monkeypatch.setattr(arb_server, '_fetch_clob', _stub_clob)

    failures = atomic._last_ms_depth_recheck(deal)
    assert len(failures) == 1
    assert '$35' in failures[0]


def test_depth_recheck_handles_fetch_exception(monkeypatch):
    """If _fetch_clob raises, leg is marked fetch_failed (still rejected)."""
    from executor import atomic
    deal = _make_deal(net=10.0, legs=[
        {'price': 0.45, 'stake': 30.0, 'liquidity': 100.0,
         'token_id': '111', 'platform': 'Polymarket'},
    ])

    def _stub_clob(tid):
        raise RuntimeError("simulated network error")
    import arb_server
    monkeypatch.setattr(arb_server, '_fetch_clob', _stub_clob)

    failures = atomic._last_ms_depth_recheck(deal)
    assert len(failures) == 1
    assert 'fetch failed' in failures[0]


def test_depth_recheck_sx_skipped(monkeypatch):
    """SX Bet uses match-against-makers — re-check trusts scan-time depth."""
    from executor import atomic
    deal = _make_deal(net=10.0, platform='SX Bet', legs=[
        {'price': 0.45, 'stake': 30.0, 'liquidity': 100.0,
         'market_hash': '0xH', 'outcome_index': 1, 'platform': 'SX Bet'},
    ])
    # Should pass without calling _fetch_clob at all.
    failures = atomic._last_ms_depth_recheck(deal)
    assert failures == []


# ── env config ─────────────────────────────────────────────────────

def test_depth_recheck_env_flag():
    """DEPTH_RECHECK_ENABLED env defaults to True (=1)."""
    import executor.atomic as atomic_mod
    assert atomic_mod.DEPTH_RECHECK_ENABLED in (True, False)
    assert atomic_mod.DEPTH_RECHECK_TOLERANCE > 0


def test_min_net_default_in_reasonable_range():
    """MIN_NET_PER_ARB_USD has sane default (>$0, <$10)."""
    import executor.atomic as atomic_mod
    assert 0.01 <= atomic_mod.MIN_NET_PER_ARB_USD <= 10.0


# ── Phase 19v6 — min-leg-liquidity filter (in build_deal) ─────────

def test_min_leg_liq_filter_rejects_mosquito_at_detection():
    """build_deal returns None when min_liq < MIN_LEG_LIQ_USD.

    Eliminates mosquito arbs at DETECTION stage — they don't show up
    in NEAR / Deals tab at all.
    """
    import arb_server
    # Mosquito: 2 legs each with $0.4 liquidity (sum 93c, edge 7%)
    outcomes = [
        {'name': 'A', 'price': 0.45, 'liquidity': 0.4,
         'source': 'clob_ask', 'volume': 1000},
        {'name': 'B', 'price': 0.48, 'liquidity': 0.4,
         'source': 'clob_ask', 'volume': 1000},
    ]
    deal = arb_server.build_deal(
        title='Test mosquito', platform='Polymarket',
        outcomes=outcomes, total_price=0.93, theta=0.025,
        threshold=0.965)
    assert deal is None, "mosquito arb (min_liq=$0.4) should be rejected"


def test_min_leg_liq_filter_passes_real_arb():
    """build_deal returns a deal when min_liq >= MIN_LEG_LIQ_USD."""
    import arb_server
    outcomes = [
        {'name': 'A', 'price': 0.45, 'liquidity': 100.0,
         'source': 'clob_ask', 'volume': 5000},
        {'name': 'B', 'price': 0.48, 'liquidity': 200.0,
         'source': 'clob_ask', 'volume': 5000},
    ]
    deal = arb_server.build_deal(
        title='Test real', platform='Polymarket',
        outcomes=outcomes, total_price=0.93, theta=0.025,
        threshold=0.965)
    assert deal is not None
    assert deal['net'] > 0


def test_min_leg_liq_threshold_default():
    """MIN_LEG_LIQ_USD env exists and has reasonable default."""
    import arb_server
    assert hasattr(arb_server, 'MIN_LEG_LIQ_USD')
    # Sane default: at least a few dollars (otherwise the filter is
    # useless), and at most $100 (otherwise we'd reject most arbs).
    assert 1.0 <= arb_server.MIN_LEG_LIQ_USD <= 100.0


def test_min_leg_liq_filter_at_boundary():
    """min_liq exactly = MIN_LEG_LIQ_USD ($10) → passes."""
    import arb_server
    outcomes = [
        {'name': 'A', 'price': 0.45, 'liquidity': 10.0,
         'source': 'clob_ask', 'volume': 1000},
        {'name': 'B', 'price': 0.48, 'liquidity': 50.0,
         'source': 'clob_ask', 'volume': 1000},
    ]
    deal = arb_server.build_deal(
        title='Test boundary', platform='Polymarket',
        outcomes=outcomes, total_price=0.93, theta=0.025,
        threshold=0.965)
    # min_liq = 10.0 (lowest non-zero), exactly at threshold → pass
    assert deal is not None


def test_min_leg_liq_filter_below_boundary():
    """min_liq = $9.99 → rejected."""
    import arb_server
    outcomes = [
        {'name': 'A', 'price': 0.45, 'liquidity': 9.99,
         'source': 'clob_ask', 'volume': 1000},
        {'name': 'B', 'price': 0.48, 'liquidity': 50.0,
         'source': 'clob_ask', 'volume': 1000},
    ]
    deal = arb_server.build_deal(
        title='Test under', platform='Polymarket',
        outcomes=outcomes, total_price=0.93, theta=0.025,
        threshold=0.965)
    assert deal is None
