"""Phase TS-5c (12.05.2026) — POLYMARKET_WS_REQUIRED mode.

Symmetric to LIMITLESS_WS_REQUIRED (TS-5a). When set, `_fetch_clob`
returns the WS cached book on hit or `(token_id, None, 0, None, 0)` on
cache miss AS LONG AS the WS is connected. Result: zero REST traffic to
`clob.polymarket.com/book` for hot tokens, removing the occasional
Cloudflare 403/429 pressure (BUG_CATALOG 6.3).

Graceful degradation: when the WS is NOT connected, we still fall
through to REST so the radar doesn't black out on transient WS outage.

Tests patch `arb_server.ws_client` and `arb_server.POLYMARKET_WS_REQUIRED`
in the imported module to control behavior without booting a real WS.
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


class _FakePolyWS:
    """Minimal PolyMarketWS double — implements just what `_fetch_clob`
    and `/api/poly_ws_health` read."""

    def __init__(self, book=None, connected=True):
        self._book = book
        self._connected = connected

    def get_book(self, token_id):
        return self._book

    def get_metrics(self):
        return {'connected': self._connected, 'subs_active': 0,
                'subs_desired': 0, 'msg_per_sec': 0.0, 'reconnects': 0,
                'last_msg_age_sec': None, 'subs_max': 1000}


def _book(best_ask=0.42, best_bid=0.55, depth=10.0, bid_depth=8.0):
    return {
        'best_ask': best_ask,
        'best_bid': best_bid,
        'depth': depth,
        'bid_depth': bid_depth,
        'ts': time.time(),
    }


def test_ws_required_off_falls_through_to_rest_on_miss(monkeypatch):
    """Default: POLYMARKET_WS_REQUIRED=0 → cache miss → REST."""
    import arb_server
    monkeypatch.setattr(arb_server, 'ws_client',
                        _FakePolyWS(book=None, connected=True))
    monkeypatch.setattr(arb_server, 'POLYMARKET_WS_REQUIRED', False)

    rest_called = {'count': 0}

    class FakeResp:
        def json(self):
            rest_called['count'] += 1
            return {'asks': [], 'bids': []}

    class FakeSess:
        def get(self, *a, **kw): return FakeResp()

    monkeypatch.setattr(arb_server, '_SESS_POLY', FakeSess())
    result = arb_server._fetch_clob('tok-123')
    assert rest_called['count'] == 1
    assert result[0] == 'tok-123'


def test_ws_required_on_connected_miss_skips_rest(monkeypatch):
    """POLYMARKET_WS_REQUIRED=1 + WS connected + cache miss →
    return (token, None, 0, None, 0). NO REST."""
    import arb_server
    monkeypatch.setattr(arb_server, 'ws_client',
                        _FakePolyWS(book=None, connected=True))
    monkeypatch.setattr(arb_server, 'POLYMARKET_WS_REQUIRED', True)

    class FakeSess:
        def get(self, *a, **kw):
            raise AssertionError("REST must NOT be called when WS required + connected")

    monkeypatch.setattr(arb_server, '_SESS_POLY', FakeSess())
    result = arb_server._fetch_clob('tok-miss')
    assert result == ('tok-miss', None, 0.0, None, 0.0)


def test_ws_required_on_disconnected_falls_through_to_rest(monkeypatch):
    """POLYMARKET_WS_REQUIRED=1 + WS DISCONNECTED + cache miss →
    REST fallback (graceful degradation)."""
    import arb_server
    monkeypatch.setattr(arb_server, 'ws_client',
                        _FakePolyWS(book=None, connected=False))
    monkeypatch.setattr(arb_server, 'POLYMARKET_WS_REQUIRED', True)

    rest_called = {'count': 0}

    class FakeResp:
        def json(self):
            rest_called['count'] += 1
            return {'asks': [], 'bids': []}

    class FakeSess:
        def get(self, *a, **kw): return FakeResp()

    monkeypatch.setattr(arb_server, '_SESS_POLY', FakeSess())
    arb_server._fetch_clob('tok-dc')
    assert rest_called['count'] == 1


def test_ws_required_on_cache_hit_returns_ws_data(monkeypatch):
    """Cache hit short-circuits REST regardless of required flag.
    The book values flow into the returned tuple shape."""
    import arb_server
    monkeypatch.setattr(arb_server, 'ws_client',
                        _FakePolyWS(book=_book(best_ask=0.37,
                                                best_bid=0.61,
                                                depth=12.5,
                                                bid_depth=9.0),
                                    connected=True))
    monkeypatch.setattr(arb_server, 'POLYMARKET_WS_REQUIRED', True)

    class FakeSess:
        def get(self, *a, **kw):
            raise AssertionError("REST must NOT be called on cache hit")

    monkeypatch.setattr(arb_server, '_SESS_POLY', FakeSess())
    tid, ask, depth, bid, bid_depth = arb_server._fetch_clob('tok-hit')
    assert tid == 'tok-hit'
    assert ask == 0.37
    assert bid == 0.61
    assert depth == 12.5
    assert bid_depth == 9.0


def test_ws_required_metrics_exception_treated_as_disconnected(monkeypatch):
    """Defensive: get_metrics() raising = treat as disconnected =
    fall through to REST. Better a rate-limit hit than silent skipping."""
    import arb_server

    class _BrokenWS:
        def get_book(self, t): return None
        def get_metrics(self): raise RuntimeError("broken")

    monkeypatch.setattr(arb_server, 'ws_client', _BrokenWS())
    monkeypatch.setattr(arb_server, 'POLYMARKET_WS_REQUIRED', True)

    rest_called = {'count': 0}

    class FakeResp:
        def json(self):
            rest_called['count'] += 1
            return {'asks': [], 'bids': []}

    class FakeSess:
        def get(self, *a, **kw): return FakeResp()

    monkeypatch.setattr(arb_server, '_SESS_POLY', FakeSess())
    arb_server._fetch_clob('tok-broken')
    assert rest_called['count'] == 1


def test_ws_required_off_with_ws_client_none_calls_rest(monkeypatch):
    """If ws_client is None (boot incomplete), fall through to REST
    regardless of POLYMARKET_WS_REQUIRED — no NoneType crash."""
    import arb_server
    monkeypatch.setattr(arb_server, 'ws_client', None)
    monkeypatch.setattr(arb_server, 'POLYMARKET_WS_REQUIRED', True)

    rest_called = {'count': 0}

    class FakeResp:
        def json(self):
            rest_called['count'] += 1
            return {'asks': [], 'bids': []}

    class FakeSess:
        def get(self, *a, **kw): return FakeResp()

    monkeypatch.setattr(arb_server, '_SESS_POLY', FakeSess())
    arb_server._fetch_clob('tok-noclient')
    assert rest_called['count'] == 1


def test_ws_required_invalid_book_skips_rest(monkeypatch):
    """Edge: WS has a book but best_ask is None / out-of-range.
    Required + connected → still skip slug (not REST)."""
    import arb_server
    bad = {'best_ask': None, 'best_bid': 0.5, 'depth': 0, 'ts': time.time()}
    monkeypatch.setattr(arb_server, 'ws_client',
                        _FakePolyWS(book=bad, connected=True))
    monkeypatch.setattr(arb_server, 'POLYMARKET_WS_REQUIRED', True)

    class FakeSess:
        def get(self, *a, **kw):
            raise AssertionError("REST must NOT be called")

    monkeypatch.setattr(arb_server, '_SESS_POLY', FakeSess())
    result = arb_server._fetch_clob('tok-bad')
    assert result == ('tok-bad', None, 0.0, None, 0.0)


def test_api_poly_ws_health_disabled_when_no_client(monkeypatch):
    """When ws_client is None, endpoint returns enabled:false."""
    import arb_server
    monkeypatch.setattr(arb_server, 'ws_client', None)
    monkeypatch.setattr(arb_server, 'POLYMARKET_WS_REQUIRED', False)

    with arb_server.app.test_client() as c:
        r = c.get('/api/poly_ws_health')
        assert r.status_code == 200
        body = r.get_json()
        assert body['enabled'] is False
        assert 'reason' in body
        assert body['required_mode'] is False


def test_api_poly_ws_health_returns_client_metrics(monkeypatch):
    """Endpoint flattens client metrics + required_mode flag."""
    import arb_server
    monkeypatch.setattr(arb_server, 'ws_client', _FakePolyWS(connected=True))
    monkeypatch.setattr(arb_server, 'POLYMARKET_WS_REQUIRED', True)

    with arb_server.app.test_client() as c:
        r = c.get('/api/poly_ws_health')
        assert r.status_code == 200
        body = r.get_json()
        assert body['enabled'] is True
        assert body['required_mode'] is True
        assert body['connected'] is True
        assert 'subs_active' in body
        assert 'msg_per_sec' in body
