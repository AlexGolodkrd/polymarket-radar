"""Phase TS-5a (12.05.2026) — LimitlessWS stability hardening.

Background: Phase 9ddd disabled `ENABLE_LIMITLESS_WS` after 761s scan
hangs caused by `socketio.Client(reconnection_attempts=0)` looping on
flaky Limitless TLS — every retry spawned a thread, GIL contention
starved radar's main scan.

Fix: cap reconnect attempts via env, fail-fast handshake via env,
record handshake durations, long-pause after N consecutive failures.

These tests verify the public surface (metrics shape, env reading) and
the internal counter behavior. They do NOT spin a real socket.io
connection — the supervisor loop is too I/O-bound for unit tests.
"""
import importlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


def _fresh_limitless_ws_module(monkeypatch, env=None):
    """Reload limitless_ws with monkeypatched env so module-level
    DEFAULT_* constants pick up the test values."""
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    import limitless_ws
    importlib.reload(limitless_ws)
    return limitless_ws


def test_module_defaults_pin_reconnect_attempts_finite(monkeypatch):
    """The Phase 9ddd hang was caused by reconnection_attempts=0 (infinite).
    New default MUST be finite to bound socketio's internal retry storm."""
    mod = _fresh_limitless_ws_module(monkeypatch)
    assert mod.DEFAULT_RECONNECT_ATTEMPTS > 0
    assert mod.DEFAULT_RECONNECT_ATTEMPTS <= 20  # sanity


def test_module_defaults_fail_fast_handshake(monkeypatch):
    """Connect timeout should be aggressive — under 10s the old default
    (which let slow TLS handshakes pile up)."""
    mod = _fresh_limitless_ws_module(monkeypatch)
    assert mod.DEFAULT_CONNECT_TIMEOUT > 0
    assert mod.DEFAULT_CONNECT_TIMEOUT < 10


def test_env_overrides_reconnect_attempts(monkeypatch):
    mod = _fresh_limitless_ws_module(monkeypatch, {
        'LIMITLESS_WS_RECONNECT_ATTEMPTS': '3',
    })
    assert mod.DEFAULT_RECONNECT_ATTEMPTS == 3


def test_env_overrides_connect_timeout(monkeypatch):
    mod = _fresh_limitless_ws_module(monkeypatch, {
        'LIMITLESS_WS_CONNECT_TIMEOUT': '2.5',
    })
    assert mod.DEFAULT_CONNECT_TIMEOUT == 2.5


def test_env_overrides_max_fails_before_pause(monkeypatch):
    mod = _fresh_limitless_ws_module(monkeypatch, {
        'LIMITLESS_WS_MAX_FAILS_BEFORE_PAUSE': '7',
    })
    assert mod.DEFAULT_MAX_FAILS_BEFORE_PAUSE == 7


def test_env_overrides_long_pause_s(monkeypatch):
    mod = _fresh_limitless_ws_module(monkeypatch, {
        'LIMITLESS_WS_LONG_PAUSE_S': '120',
    })
    assert mod.DEFAULT_LONG_PAUSE_S == 120


def test_get_metrics_empty_handshake_shape(monkeypatch):
    """Before any handshake, all _ms fields are None — not 0 (avoid
    false signal in dashboards). count is 0."""
    mod = _fresh_limitless_ws_module(monkeypatch)
    client = mod.LimitlessWS()
    m = client.get_metrics()
    assert m['handshake_count'] == 0
    assert m['handshake_last_ms'] is None
    assert m['handshake_p50_ms'] is None
    assert m['handshake_p99_ms'] is None
    assert m['consecutive_failures'] == 0
    assert m['long_pause_count'] == 0


def test_get_metrics_percentiles_after_seed(monkeypatch):
    """Seed the handshake buffer directly and verify p50/p99/last math."""
    mod = _fresh_limitless_ws_module(monkeypatch)
    client = mod.LimitlessWS()
    # 5 samples: 100, 200, 300, 400, 500 ms
    with client._handshake_lock:
        client._handshake_durations_ms.extend([100.0, 200.0, 300.0, 400.0, 500.0])
    m = client.get_metrics()
    assert m['handshake_count'] == 5
    assert m['handshake_last_ms'] == 500.0
    # p50 of [100,200,300,400,500] sorted → index 5//2=2 → 300
    assert m['handshake_p50_ms'] == 300.0
    # p99 → max
    assert m['handshake_p99_ms'] == 500.0


def test_get_metrics_consecutive_failures_visible(monkeypatch):
    mod = _fresh_limitless_ws_module(monkeypatch)
    client = mod.LimitlessWS()
    with client._handshake_lock:
        client._consecutive_failures = 4
    m = client.get_metrics()
    assert m['consecutive_failures'] == 4


def test_get_metrics_long_pause_count_visible(monkeypatch):
    mod = _fresh_limitless_ws_module(monkeypatch)
    client = mod.LimitlessWS()
    with client._handshake_lock:
        client._long_pause_count = 2
    m = client.get_metrics()
    assert m['long_pause_count'] == 2


def test_handshake_ring_buffer_caps_at_20(monkeypatch):
    """deque(maxlen=HANDSHAKE_RING_SIZE) so flooded retries don't blow
    memory — only last 20 samples kept."""
    mod = _fresh_limitless_ws_module(monkeypatch)
    client = mod.LimitlessWS()
    with client._handshake_lock:
        client._handshake_durations_ms.extend(float(i) for i in range(100))
    m = client.get_metrics()
    assert m['handshake_count'] == mod.HANDSHAKE_RING_SIZE == 20
    # Last 20 entries are 80..99, so handshake_last_ms == 99
    assert m['handshake_last_ms'] == 99.0


def test_module_imports_when_socketio_unavailable():
    """Phase 5 backwards-compat — module must import cleanly without
    python-socketio installed, and start() becomes a no-op."""
    import limitless_ws
    # Module-level flag is set at import time. We don't actually uninstall
    # socketio here; just verify the import-time guard exists.
    assert hasattr(limitless_ws, '_SOCKETIO_AVAILABLE')
