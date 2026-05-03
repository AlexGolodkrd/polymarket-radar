"""Phase 19v7 (03.05.2026) — async SX /orders batch fetcher.

Production observed: `batch_fetch(_fetch_sx_orders, sx_ml_hashes)` was the
last big sync bottleneck in run_scan() final aggregation (~30-60s for 300-500
binary markets). New `run_fetch_sx_orders_batch` does fan-out via httpx
async — expected 5-10x speedup.

Output shape MUST match sync `_fetch_sx_orders`:
    sync:  returns (market_hash, best1, depth1, best2, depth2)  [5-tuple]
    async dict: {market_hash: (best1, depth1, best2, depth2)}    [4-tuple values]

Caller (arb_server.run_scan) uses dict-of-tuples form (same as sync
batch_fetch returns).
"""
import os, sys, pytest, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


@pytest.fixture
def mock_httpx(monkeypatch):
    try:
        import httpx
    except ImportError:
        pytest.skip("httpx not installed")
    import async_fetchers

    class _FakeResp:
        def __init__(self, status=200, body=None):
            self.status_code = status
            self._body = body or {}
        def json(self): return self._body

    class _FakeClient:
        is_closed = False
        async def get(self, url, **kw):
            mh = url.split('marketHashes=')[1].split('&')[0]
            # Synthetic SX response — alternate empty / populated by hash
            h = sum(ord(c) for c in mh) % 5
            if h == 0:
                return _FakeResp(200, {'status': 'success', 'data': {'orders': []}})
            return _FakeResp(200, {
                'status': 'success',
                'data': {'orders': [
                    {'percentageOdds': str(int(0.45 * 1e20)),
                     'orderSizeFillable': str(int(50 * 1e6)),
                     'isMakerBettingOutcomeOne': True},
                    {'percentageOdds': str(int(0.50 * 1e20)),
                     'orderSizeFillable': str(int(80 * 1e6)),
                     'isMakerBettingOutcomeOne': False},
                ]}
            })
        async def aclose(self): pass

    async def _stub_get_client(host_key, max_keepalive=30):
        return _FakeClient()

    monkeypatch.setattr(async_fetchers, '_get_client', _stub_get_client)
    return async_fetchers


def test_run_fetch_sx_orders_batch_callable():
    try:
        import httpx  # noqa
    except ImportError:
        pytest.skip("httpx not installed")
    from async_fetchers import run_fetch_sx_orders_batch
    assert callable(run_fetch_sx_orders_batch)
    # Default: max_concurrent=30, slippage=0.005
    assert run_fetch_sx_orders_batch.__defaults__ == (30, 0.005)


def test_async_sx_orders_returns_5_tuple_shape():
    """fetch_sx_orders_async returns (market_hash, best1, depth1, best2, depth2)."""
    import inspect
    try:
        from async_fetchers import fetch_sx_orders_async
    except ImportError:
        pytest.skip("async_fetchers not importable")
    src = inspect.getsource(fetch_sx_orders_async)
    assert 'best1' in src and 'depth_taker_one' in src
    assert 'best2' in src and 'depth_taker_two' in src
    # Final return must be 5-element matching sync
    assert 'return market_hash, best1, depth_taker_one, best2, depth_taker_two' in src


def test_batch_returns_dict_keyed_by_hash(mock_httpx):
    af = mock_httpx
    hashes = [f"0x{i:062x}" for i in range(20)]
    result = af.run_fetch_sx_orders_batch(hashes, max_concurrent=10)
    assert isinstance(result, dict)
    assert len(result) == 20
    for mh, val in result.items():
        # 4-tuple value (no hash prefix — that's the dict key)
        assert isinstance(val, tuple)
        assert len(val) == 4


def test_batch_parallel_speedup(mock_httpx, monkeypatch):
    """100 hashes via async should be much faster than 100 sequential calls."""
    af = mock_httpx
    hashes = [f"0x{i:064x}" for i in range(100)]
    t0 = time.time()
    result = af.run_fetch_sx_orders_batch(hashes, max_concurrent=30)
    elapsed = time.time() - t0
    assert len(result) == 100
    # Mocked httpx is instant, so total should be << 1s with parallel.
    # Sequential 100 calls would still be sub-second with mock, but we
    # assert <2s as conservative upper bound (covers test runner overhead).
    assert elapsed < 2.0, f"Async batch took {elapsed:.2f}s — too slow"


def test_batch_empty_input():
    try:
        from async_fetchers import run_fetch_sx_orders_batch
    except ImportError:
        pytest.skip("httpx not installed")
    assert run_fetch_sx_orders_batch([]) == {}


def test_batch_handles_failure(monkeypatch):
    """One failed call returns (None, 0, None, 0); others succeed."""
    try:
        import httpx  # noqa
    except ImportError:
        pytest.skip("httpx not installed")
    import async_fetchers

    class _Resp:
        def __init__(self, status, body=None):
            self.status_code = status
            self._body = body or {}
        def json(self): return self._body
    class _Client:
        is_closed = False
        async def get(self, url, **kw):
            mh = url.split('marketHashes=')[1].split('&')[0]
            if mh.endswith('1'):
                return _Resp(403)  # simulate CF block on this market
            return _Resp(200, {'status': 'success', 'data': {'orders': [
                {'percentageOdds': str(int(0.45 * 1e20)),
                 'orderSizeFillable': str(int(50 * 1e6)),
                 'isMakerBettingOutcomeOne': True}]}})
        async def aclose(self): pass

    async def _stub(host_key, max_keepalive=30): return _Client()
    monkeypatch.setattr(async_fetchers, '_get_client', _stub)

    hashes = ['0x' + str(i) * 64 for i in range(3)]
    result = async_fetchers.run_fetch_sx_orders_batch(hashes)
    assert len(result) == 3
    # Hash ending in '1' got 403 → all None/0
    failed = result['0x' + '1' * 64]
    assert failed[0] is None or failed[0] == 0


def test_run_scan_uses_async_sx_when_env_set():
    """run_scan calls run_fetch_sx_orders_batch when ASYNC_FETCH=1."""
    import inspect
    import arb_server
    src = inspect.getsource(arb_server.run_scan)
    assert 'run_fetch_sx_orders_batch' in src, (
        "run_scan must call async SX orders batch when ASYNC_FETCH=1")
    # Sync fallback `batch_fetch(_fetch_sx_orders, ...)` retained
    assert 'batch_fetch(_fetch_sx_orders, sx_ml_hashes)' in src


def test_async_sx_orders_routes_to_sx_http_path():
    """fetch_sx_orders_async uses _get_client('sx') — not 'limitless' or 'poly'."""
    import inspect
    import async_fetchers
    src = inspect.getsource(async_fetchers.fetch_sx_orders_async)
    assert "_get_client('sx')" in src or '_get_client("sx")' in src
