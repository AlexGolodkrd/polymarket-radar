"""Phase audit-2 (11.05.2026) — exchange latency shadow probe tests."""
import os
import sys
from unittest.mock import MagicMock, patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


def _reset():
    """Drain ring buffers between tests to avoid cross-pollution."""
    import exchange_latency_probe as probe
    with probe._rtt_lock:
        for k in probe._rtt_buffers:
            probe._rtt_buffers[k].clear()


def test_stats_empty_returns_none_per_platform():
    _reset()
    import exchange_latency_probe as probe
    out = probe.stats()
    assert 'note' in out
    assert 'probe_interval_s' in out
    for plat in ('polymarket', 'limitless', 'sx_bet'):
        assert plat in out
        assert out[plat]['count'] == 0
        assert out[plat]['p50'] is None
        assert out[plat]['ok_rate_pct'] is None


def test_record_appends_to_ring_buffer():
    _reset()
    import exchange_latency_probe as probe
    probe._record('polymarket', 123.4, True, 200)
    probe._record('polymarket', 250.1, True, 200)
    out = probe.stats()
    assert out['polymarket']['count'] == 2
    assert out['polymarket']['last'] == 250.1
    assert out['polymarket']['min'] == 123.4
    assert out['polymarket']['max'] == 250.1


def test_percentiles_sane():
    _reset()
    import exchange_latency_probe as probe
    # 10 samples 100..1000ms
    for v in (100, 200, 300, 400, 500, 600, 700, 800, 900, 1000):
        probe._record('limitless', v, True, 200)
    out = probe.stats()['limitless']
    assert out['count'] == 10
    # idx=round(0.5*9)=4 (banker's even round of 4.5) → 500
    assert out['p50'] == 500
    # idx=round(0.9*9)=8 → 900
    assert out['p90'] == 900
    # idx=round(0.99*9)=9 → 1000
    assert out['p99'] == 1000
    assert out['mean'] == 550.0


def test_ring_buffer_caps_at_50():
    _reset()
    import exchange_latency_probe as probe
    for i in range(100):
        probe._record('sx_bet', float(i), True, 200)
    out = probe.stats()['sx_bet']
    assert out['count'] == 50
    assert out['min'] == 50  # last 50: 50..99
    assert out['max'] == 99


def test_ok_rate_counts_errors():
    _reset()
    import exchange_latency_probe as probe
    # 8 ok + 2 errors out of last 10
    for _ in range(8):
        probe._record('polymarket', 100, True, 200)
    for _ in range(2):
        probe._record('polymarket', 5000, False, None)
    out = probe.stats()['polymarket']
    assert out['ok_rate_pct'] == 80.0
    assert out['errors_last_10'] == 2


def test_probe_once_records_on_http_success(monkeypatch):
    _reset()
    import exchange_latency_probe as probe
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    with patch.object(probe._session, 'get', return_value=fake_resp):
        probe._probe_once('polymarket', 'http://test')
    out = probe.stats()['polymarket']
    assert out['count'] == 1
    assert out['errors_last_10'] == 0


def test_probe_once_records_on_exception(monkeypatch):
    _reset()
    import exchange_latency_probe as probe
    import requests as _req
    with patch.object(probe._session, 'get',
                       side_effect=_req.ConnectionError('refused')):
        probe._probe_once('limitless', 'http://test')
    out = probe.stats()['limitless']
    assert out['count'] == 1
    assert out['errors_last_10'] == 1
    assert out['ok_rate_pct'] == 0.0


def test_probe_once_treats_5xx_as_not_ok(monkeypatch):
    """5xx = exchange degraded; we still record the latency but
    flag as not-ok so ok_rate_pct reflects reality."""
    _reset()
    import exchange_latency_probe as probe
    fake = MagicMock()
    fake.status_code = 502
    with patch.object(probe._session, 'get', return_value=fake):
        probe._probe_once('sx_bet', 'http://test')
    out = probe.stats()['sx_bet']
    assert out['count'] == 1
    assert out['errors_last_10'] == 1


def test_probe_once_treats_4xx_as_ok(monkeypatch):
    """4xx = "connection completed, server rejected" — still
    represents real network + server-parsing time, which is the
    metric we want as a POST-latency floor."""
    _reset()
    import exchange_latency_probe as probe
    fake = MagicMock()
    fake.status_code = 404
    with patch.object(probe._session, 'get', return_value=fake):
        probe._probe_once('polymarket', 'http://test')
    out = probe.stats()['polymarket']
    assert out['count'] == 1
    assert out['errors_last_10'] == 0
