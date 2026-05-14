"""Phase TS-5a (12.05.2026) — LIMITLESS_WS_REQUIRED mode.

When set, `_fetch_limitless_orderbook(slug)` returns the WS cached
book on hit, but returns `(slug, None, 0, None, 0)` on cache miss
AS LONG AS the WS is connected. Result: zero REST traffic to
Limitless's `/markets/{slug}/orderbook` for hot pool slugs, which
was the primary rate-limit pressure source (PR #182 saga).

Graceful degradation: when the WS is NOT connected (handshake fail,
long-pause, or simply not yet established), we still fall through
to REST so the radar doesn't black out.

Tests directly patch `arb_server.lim_ws_client` and the
`arb_server.LIMITLESS_WS_REQUIRED` flag in the imported module to
control behavior without touching env at import time.
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


class _FakeLimWS:
    """Minimal LimitlessWS double — implements just the public surface
    that `_fetch_limitless_orderbook` reads."""

    def __init__(self, book=None, connected=True):
        self._book = book
        self._connected = connected

    def get_book(self, slug):
        return self._book

    def get_metrics(self):
        return {'connected': self._connected, 'subs_active': 0,
                'subs_desired': 0, 'msg_per_sec': 0.0}


def _fresh_book(yes_ask=0.45, yes_bid=0.50, depth_yes=10.0, depth_no=8.0):
    return {
        'best_yes_ask': yes_ask,
        'best_yes_bid': yes_bid,
        'depth_yes': depth_yes,
        'depth_no': depth_no,
        'ts': time.time(),
    }


def test_ws_required_off_falls_through_to_rest_on_miss(monkeypatch):
    """Default behavior preserved: when LIMITLESS_WS_REQUIRED=0,
    cache miss falls through to REST, even if WS is connected."""
    import arb_server
    monkeypatch.setattr(arb_server, 'lim_ws_client',
                        _FakeLimWS(book=None, connected=True))
    monkeypatch.setattr(arb_server, 'LIMITLESS_WS_REQUIRED', False)

    rest_called = {'count': 0}

    class FakeResp:
        status_code = 200
        def json(self):
            rest_called['count'] += 1
            return {'asks': [], 'bids': []}

    class FakeSess:
        def get(self, *a, **kw): return FakeResp()

    monkeypatch.setattr(arb_server, '_SESS_LIM', FakeSess())
    result = arb_server._fetch_limitless_orderbook('test-slug')
    assert rest_called['count'] == 1, \
        "REST must be called when LIMITLESS_WS_REQUIRED=0 and cache misses"
    assert result[0] == 'test-slug'


def test_ws_required_on_connected_miss_skips_rest(monkeypatch):
    """LIMITLESS_WS_REQUIRED=1 + WS connected + cache miss →
    return (slug, None, 0, None, 0). NO REST call."""
    import arb_server
    monkeypatch.setattr(arb_server, 'lim_ws_client',
                        _FakeLimWS(book=None, connected=True))
    monkeypatch.setattr(arb_server, 'LIMITLESS_WS_REQUIRED', True)

    rest_called = {'count': 0}

    class FakeSess:
        def get(self, *a, **kw):
            rest_called['count'] += 1
            raise AssertionError("REST must NOT be called when WS required + connected")

    monkeypatch.setattr(arb_server, '_SESS_LIM', FakeSess())
    result = arb_server._fetch_limitless_orderbook('test-slug-miss')
    assert rest_called['count'] == 0
    assert result == ('test-slug-miss', None, 0, None, 0)


def test_ws_required_on_disconnected_falls_through_to_rest(monkeypatch):
    """LIMITLESS_WS_REQUIRED=1 + WS NOT connected + cache miss →
    fall through to REST (graceful degradation, not blackout)."""
    import arb_server
    monkeypatch.setattr(arb_server, 'lim_ws_client',
                        _FakeLimWS(book=None, connected=False))
    monkeypatch.setattr(arb_server, 'LIMITLESS_WS_REQUIRED', True)

    rest_called = {'count': 0}

    class FakeResp:
        status_code = 200
        def json(self):
            rest_called['count'] += 1
            return {'asks': [], 'bids': []}

    class FakeSess:
        def get(self, *a, **kw): return FakeResp()

    monkeypatch.setattr(arb_server, '_SESS_LIM', FakeSess())
    result = arb_server._fetch_limitless_orderbook('test-slug-dc')
    assert rest_called['count'] == 1, \
        "REST must be called when WS is disconnected, regardless of WS_REQUIRED"
    assert result[0] == 'test-slug-dc'


def test_ws_required_on_cache_hit_returns_ws_data(monkeypatch):
    """Cache hit short-circuits before any REST decision — WS data
    returned regardless of LIMITLESS_WS_REQUIRED flag."""
    import arb_server
    monkeypatch.setattr(arb_server, 'lim_ws_client',
                        _FakeLimWS(book=_fresh_book(yes_ask=0.42,
                                                    yes_bid=0.50),
                                   connected=True))
    monkeypatch.setattr(arb_server, 'LIMITLESS_WS_REQUIRED', True)

    class FakeSess:
        def get(self, *a, **kw):
            raise AssertionError("REST must NOT be called on cache hit")

    monkeypatch.setattr(arb_server, '_SESS_LIM', FakeSess())
    slug, yes_ask, depth_yes, no_ask, depth_no = \
        arb_server._fetch_limitless_orderbook('test-slug-hit')
    assert slug == 'test-slug-hit'
    assert yes_ask == 0.42
    # no_ask synthesised from yes_bid: 1 - 0.50 = 0.50
    assert no_ask == 0.5
    assert depth_yes == 10.0
    assert depth_no == 8.0


def test_ws_required_metrics_exception_treated_as_disconnected(monkeypatch):
    """Defensive: if get_metrics() raises (corrupted state, etc.),
    treat as disconnected and fall through to REST. Better to take a
    rate-limit hit than to silently skip every slug."""
    import arb_server

    class _BrokenWS:
        def get_book(self, slug): return None
        def get_metrics(self): raise RuntimeError("metrics broken")

    monkeypatch.setattr(arb_server, 'lim_ws_client', _BrokenWS())
    monkeypatch.setattr(arb_server, 'LIMITLESS_WS_REQUIRED', True)

    rest_called = {'count': 0}

    class FakeResp:
        status_code = 200
        def json(self):
            rest_called['count'] += 1
            return {'asks': [], 'bids': []}

    class FakeSess:
        def get(self, *a, **kw): return FakeResp()

    monkeypatch.setattr(arb_server, '_SESS_LIM', FakeSess())
    arb_server._fetch_limitless_orderbook('test-slug-broken')
    assert rest_called['count'] == 1


def test_api_lim_ws_health_disabled_when_no_client(monkeypatch):
    """When lim_ws_client is None (ENABLE_LIMITLESS_WS=0), endpoint
    returns enabled:false instead of 500."""
    import arb_server
    monkeypatch.setattr(arb_server, 'lim_ws_client', None)
    monkeypatch.setattr(arb_server, 'LIMITLESS_WS_REQUIRED', False)
    monkeypatch.setattr(arb_server, 'ENABLE_LIMITLESS', True)

    with arb_server.app.test_client() as c:
        r = c.get('/api/lim_ws_health')
        assert r.status_code == 200
        body = r.get_json()
        assert body['enabled'] is False
        assert 'ENABLE_LIMITLESS_WS=0' in body['reason']
        assert body['required_mode'] is False


def test_api_lim_ws_health_returns_client_metrics(monkeypatch):
    """When the client is present, endpoint flattens its metrics into
    the response plus the required_mode flag."""
    import arb_server
    monkeypatch.setattr(arb_server, 'lim_ws_client',
                        _FakeLimWS(connected=True))
    monkeypatch.setattr(arb_server, 'LIMITLESS_WS_REQUIRED', True)

    with arb_server.app.test_client() as c:
        r = c.get('/api/lim_ws_health')
        assert r.status_code == 200
        body = r.get_json()
        assert body['enabled'] is True
        assert body['required_mode'] is True
        assert body['connected'] is True
        # Flattened metrics fields visible at top level (not nested)
        assert 'subs_active' in body
        assert 'msg_per_sec' in body
