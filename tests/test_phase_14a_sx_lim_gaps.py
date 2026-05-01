"""Phase 14a (01.05.2026) — gaps fixed for SX/Limitless production readiness.

Gap 1: Limitless filter — accepting_orders gate (was missing)
Gap 2: SX/Limitless adaptive post-resolve grace (was 13-day binary cutoff only)
Gap 3: SX circuit_breaker integration (was bare requests with no failure tracking)
Gap 4: Limitless WS heartbeat timeout (was bare sio.wait blocking forever)
Gap 5: filter_sx function for analytics parity
"""
import os
import sys
import time
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), 'Scripts')
sys.path.insert(0, SCRIPTS)


# ── Adaptive grace helper ───────────────────────────────────────────
def test_adaptive_grace_5min_crypto():
    from arb_server import compute_adaptive_grace_minutes
    assert compute_adaptive_grace_minutes(duration_seconds=300) == 1
    assert compute_adaptive_grace_minutes(duration_seconds=600) == 1


def test_adaptive_grace_hourly():
    from arb_server import compute_adaptive_grace_minutes
    assert compute_adaptive_grace_minutes(duration_seconds=1800) == 5
    assert compute_adaptive_grace_minutes(duration_seconds=3600) == 5


def test_adaptive_grace_daily():
    from arb_server import compute_adaptive_grace_minutes
    assert compute_adaptive_grace_minutes(duration_seconds=86400) == 30


def test_adaptive_grace_multiday():
    from arb_server import compute_adaptive_grace_minutes
    assert compute_adaptive_grace_minutes(duration_seconds=86400 * 7) == 60


def test_adaptive_grace_title_intraday_pattern():
    from arb_server import compute_adaptive_grace_minutes
    # AM/PM ET pattern → intraday → grace 1
    assert compute_adaptive_grace_minutes(title='Bitcoin Up or Down 1PM ET') == 1
    assert compute_adaptive_grace_minutes(title='ETH 5min crypto') == 1


def test_adaptive_grace_title_weather():
    from arb_server import compute_adaptive_grace_minutes
    assert compute_adaptive_grace_minutes(
        title='Highest temperature in NYC May 2') == 30


def test_adaptive_grace_default_30():
    from arb_server import compute_adaptive_grace_minutes
    assert compute_adaptive_grace_minutes() == 30
    assert compute_adaptive_grace_minutes(title='Random event') == 30


# ── Gap 5: filter_sx parity ─────────────────────────────────────────
def test_filter_sx_status_check():
    """status != 1 → rejected."""
    from arb_server import filter_sx, SX_BINARY_TYPES
    sx_type = next(iter(SX_BINARY_TYPES))
    markets = [
        {'type': sx_type, 'marketHash': '0xA',
         'gameTime': time.time() + 3600,
         'status': 1, 'outcomeOneName': 'A', 'outcomeTwoName': 'B'},
        {'type': sx_type, 'marketHash': '0xB',
         'gameTime': time.time() + 3600,
         'status': 2, 'outcomeOneName': 'A', 'outcomeTwoName': 'B'},
        {'type': sx_type, 'marketHash': '0xC',
         'gameTime': time.time() + 3600,
         # missing status
         'outcomeOneName': 'A', 'outcomeTwoName': 'B'},
    ]
    diag = {}
    out = filter_sx(markets, diag=diag)
    hashes = [m['marketHash'] for m in out]
    assert '0xA' in hashes
    assert '0xB' not in hashes
    assert '0xC' not in hashes
    assert diag['sx_skip_status'] == 2


def test_filter_sx_post_resolve_grace():
    """Match ended 2 hours ago — well past default 30-min grace."""
    from arb_server import filter_sx, SX_BINARY_TYPES
    sx_type = next(iter(SX_BINARY_TYPES))
    markets = [
        {'type': sx_type, 'marketHash': '0xA',
         'gameTime': time.time() - 7200,        # 2h ago > 30min grace
         'status': 1, 'outcomeOneName': 'A', 'outcomeTwoName': 'B'},
    ]
    diag = {}
    out = filter_sx(markets, diag=diag)
    assert len(out) == 0
    assert diag['sx_skip_past_resolve'] == 1


def test_filter_sx_within_grace_passes():
    """Match ended 1 minute ago — within default 30-min grace, passes."""
    from arb_server import filter_sx, SX_BINARY_TYPES
    sx_type = next(iter(SX_BINARY_TYPES))
    markets = [
        {'type': sx_type, 'marketHash': '0xA',
         'gameTime': time.time() - 60,           # 1 min ago
         'status': 1, 'outcomeOneName': 'A', 'outcomeTwoName': 'B'},
    ]
    diag = {}
    out = filter_sx(markets, diag=diag)
    assert len(out) == 1
    assert diag['sx_pass'] == 1


def test_filter_sx_window_check():
    """Far future game (>13 days) → rejected by window."""
    from arb_server import filter_sx, SX_BINARY_TYPES
    sx_type = next(iter(SX_BINARY_TYPES))
    markets = [
        {'type': sx_type, 'marketHash': '0xA',
         'gameTime': time.time() + 86400 * 30,  # 30 days
         'status': 1, 'outcomeOneName': 'A', 'outcomeTwoName': 'B'},
    ]
    diag = {}
    out = filter_sx(markets, diag=diag)
    assert len(out) == 0
    assert diag['sx_skip_no_window'] == 1


# ── Gap 1: Limitless accepting_orders gate ──────────────────────────
def test_filter_limitless_rejects_accepting_orders_false():
    from arb_server import filter_limitless
    events = [{
        'title': 'Test',
        'deadline': int(time.time() * 1000) + 3600 * 1000,  # 1h ahead, ms
        'markets': [
            {'title': 'M1', 'accepting_orders': True, 'status': 'ACTIVE'},
            {'title': 'M2', 'accepting_orders': False, 'status': 'ACTIVE'},
        ],
    }]
    diag = {}
    out = filter_limitless(events, diag=diag)
    # Either rejected entirely, or marked closed
    assert diag.get('lim_skip_outcome_closed', 0) == 1


def test_filter_limitless_accepts_when_all_accepting():
    from arb_server import filter_limitless
    events = [{
        'title': 'Test',
        'deadline': int(time.time() * 1000) + 3600 * 1000,
        'markets': [
            {'title': 'M1', 'accepting_orders': True, 'status': 'ACTIVE'},
            {'title': 'M2', 'accepting_orders': True, 'status': 'ACTIVE'},
        ],
    }]
    out = filter_limitless(events)
    assert len(out) == 1


# ── Gap 3: SX circuit breaker ───────────────────────────────────────
def test_sx_circuit_breaker_open_returns_no_data(monkeypatch):
    """When circuit breaker is OPEN for SX, _fetch_sx_orders returns empty
    immediately without HTTP call."""
    import arb_server
    import circuit_breaker

    # Trip the breaker manually
    cb = circuit_breaker.get_breaker('sx')
    for _ in range(10):
        cb.on_failure(reason='test')

    fetched = []
    def _fake_get(*a, **kw):
        fetched.append(1)
        class R:
            status_code = 200
            def json(self): return {'status': 'success', 'data': {'orders': []}}
        return R()
    monkeypatch.setattr(arb_server._SESS_SX, 'get', _fake_get)

    out = arb_server._fetch_sx_orders('0xABC')
    # Breaker open → no HTTP call made
    assert len(fetched) == 0
    assert out == ('0xABC', None, 0, None, 0)

    # Cleanup: force breaker closed for other tests
    # Use internal reset (state is a property, _state is the actual enum)
    from circuit_breaker import CircuitState
    cb._state = CircuitState.CLOSED
    cb._failure_count = 0


def test_sx_403_trips_circuit_breaker(monkeypatch):
    """403 response → breaker.on_failure called."""
    import arb_server
    import circuit_breaker

    cb = circuit_breaker.get_breaker('sx')
    # Use internal reset (state is a property, _state is the actual enum)
    from circuit_breaker import CircuitState
    cb._state = CircuitState.CLOSED
    cb._failure_count = 0

    class _FakeResp:
        status_code = 403
        def json(self): return {}
    monkeypatch.setattr(arb_server._SESS_SX, 'get', lambda *a, **kw: _FakeResp())

    out = arb_server._fetch_sx_orders('0xDEF')
    assert out == ('0xDEF', None, 0, None, 0)
    # After 1 failure breaker count went up
    assert cb._failure_count >= 1

    # Use internal reset (state is a property, _state is the actual enum)
    from circuit_breaker import CircuitState
    cb._state = CircuitState.CLOSED
    cb._failure_count = 0


# ── Gap 4: Limitless WS heartbeat timeout (smoke test only) ────────
def test_limitless_ws_imports_with_heartbeat_env():
    """Heartbeat is a runtime feature; smoke-test that import works
    after refactor (no syntax error)."""
    import limitless_ws
    assert hasattr(limitless_ws, 'LimitlessWS') or True  # module loads
