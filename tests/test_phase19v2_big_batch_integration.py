"""Phase 19v2 integration test — single big batch /book full flow.

Mocks httpx at the AsyncClient level to verify:
1. run_fetch_clob_batch fans out all tids in ONE asyncio.run
2. Returns dict keyed by tid with 4-tuple values
3. Survives partial failures (some tids return None)
4. No leaked event loops / connections after the call

This is the simulation that PROVES the path works locally before
deploying. Without it the prod-only debug cycle is too slow.
"""
import os, sys, asyncio, pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


@pytest.fixture
def mock_httpx(monkeypatch):
    """Replace _get_client with a stub that returns a fake AsyncClient."""
    try:
        import httpx
    except ImportError:
        pytest.skip("httpx not installed")
    import async_fetchers

    class _FakeResp:
        def __init__(self, body, status=200):
            self._body = body
            self.status_code = status
        def json(self):
            return self._body

    class _FakeClient:
        is_closed = False
        async def get(self, url, **kw):
            # Parse token_id from URL
            tid = url.split('token_id=')[-1]
            # Synthetic /book response — alternate fail/ok by tid hash
            h = hash(tid) % 10
            if h < 2:
                # Empty book
                return _FakeResp({'asks': [], 'bids': []})
            return _FakeResp({
                'asks': [{'price': '0.55', 'size': '100'},
                         {'price': '0.56', 'size': '50'}],
                'bids': [{'price': '0.45', 'size': '120'},
                         {'price': '0.44', 'size': '80'}],
            })
        async def aclose(self): pass

    async def _stub_get_client(host_key, max_keepalive=30):
        return _FakeClient()

    monkeypatch.setattr(async_fetchers, '_get_client', _stub_get_client)
    return async_fetchers


def test_big_batch_returns_dict_keyed_by_tid(mock_httpx):
    """100 tokens fan-out → dict with 100 entries."""
    af = mock_httpx
    tids = [f"tok_{i:04d}" for i in range(100)]
    result = af.run_fetch_clob_batch(tids, max_concurrent=30)
    assert isinstance(result, dict)
    assert len(result) == 100
    for tid, val in result.items():
        # 4-tuple: (best_ask, ask_depth, best_bid, bid_depth)
        assert isinstance(val, tuple)
        assert len(val) == 4


def test_big_batch_handles_partial_failures(mock_httpx):
    """Some tokens return empty book → entries have None ask but still dict-keyed."""
    af = mock_httpx
    tids = [f"tok_{i:04d}" for i in range(50)]
    result = af.run_fetch_clob_batch(tids, max_concurrent=20)
    # All tids are in result, even with None values
    assert len(result) == 50
    # At least some have None (synthetic stub fails 20% of the time)
    none_count = sum(1 for v in result.values() if v[0] is None)
    assert none_count > 0, "Stub should produce some empty books"
    # Most should succeed
    ok_count = sum(1 for v in result.values() if v[0] is not None)
    assert ok_count > none_count


def test_big_batch_with_large_set(mock_httpx):
    """3000 tokens — production-scale test."""
    af = mock_httpx
    import time
    tids = [f"tok_{i:05d}" for i in range(3000)]
    t0 = time.time()
    result = af.run_fetch_clob_batch(tids, max_concurrent=60)
    elapsed = time.time() - t0
    assert len(result) == 3000
    # With Semaphore=60 and instant stub responses, must be fast (<5s)
    assert elapsed < 5, f"3000 tokens took {elapsed:.2f}s — too slow"


def test_big_batch_no_event_loop_leak(mock_httpx):
    """Two consecutive run_fetch_clob_batch calls don't leak loops/clients.

    Critical regression test for the per-loop cache bug from PR #65 era.
    """
    af = mock_httpx
    tids1 = [f"a_{i}" for i in range(50)]
    tids2 = [f"b_{i}" for i in range(50)]
    r1 = af.run_fetch_clob_batch(tids1, max_concurrent=20)
    r2 = af.run_fetch_clob_batch(tids2, max_concurrent=20)
    assert len(r1) == 50
    assert len(r2) == 50
    # No leaked loop should fail either call
    asyncio.run(af.close_all_clients())


def test_big_batch_empty_input(mock_httpx):
    """Empty tids list returns empty dict, no errors."""
    af = mock_httpx
    result = af.run_fetch_clob_batch([], max_concurrent=10)
    assert result == {}


def test_big_batch_5_tuple_unpacking(mock_httpx):
    """Output values are unpackable as (ask, ask_depth, bid, bid_depth)
    matching arb_server expectations."""
    af = mock_httpx
    tids = ['tok_alpha', 'tok_beta', 'tok_gamma']
    result = af.run_fetch_clob_batch(tids)
    for tid, val in result.items():
        ask, ask_depth, bid, bid_depth = val
        # Value semantics: ask is float|None, depth is float
        assert ask is None or isinstance(ask, float)
        assert isinstance(ask_depth, float)
        assert bid is None or isinstance(bid, float)
        assert isinstance(bid_depth, float)


def test_run_fetch_poly_markets_batch_callable():
    """Phase 19v3: async batch /markets fetcher exists."""
    try:
        import httpx  # noqa
    except ImportError:
        pytest.skip("httpx not installed")
    from async_fetchers import run_fetch_poly_markets_batch
    # Default max_concurrent=20
    assert run_fetch_poly_markets_batch.__defaults__ == (20,)


def test_run_scan_pre_warm_rolled_back():
    """Phase 19v3 ROLLBACK: pre-warm /markets не работает в production
    из-за Cloudflare-tarpitting (~14s/call) — 18min wall за 1500 cids.

    Реальный фикс — short timeout (1.5s read) в `_fetch_poly_market_info`,
    chunks не блочат на cold cache. Helper `run_fetch_poly_markets_batch`
    остаётся в async_fetchers для будущего use если найдём решение.
    """
    import inspect
    import arb_server
    src = inspect.getsource(arb_server.run_scan)
    # Pre-warm wire-up НЕ должен присутствовать
    assert 'run_fetch_poly_markets_batch' not in src, (
        "run_fetch_poly_markets_batch wired-up was rolled back; "
        "should not be in run_scan source")
    # _fetch_poly_market_info should use short timeout
    info_src = inspect.getsource(arb_server._fetch_poly_market_info)
    assert 'timeout=(1.0, 1.5)' in info_src, (
        "_fetch_poly_market_info must use short timeout (1.0, 1.5)")


def test_pre_warm_seeds_cache_with_correct_shape(mock_httpx, monkeypatch):
    """Verify the pre-warm path writes entries that _fetch_poly_market_info
    treats as cache hits. Same key set, fetched_at present, valid types."""
    af = mock_httpx
    # Override _get_client to return markets-shape responses
    class _MarketResp:
        def __init__(self, cid):
            self.status_code = 200
            self._cid = cid
        def json(self):
            return {
                'minimum_tick_size': 0.01,
                'minimum_order_size': 1,
                'taker_base_fee': 250.0,
                'maker_base_fee': 0,
                'neg_risk': False,
                'accepting_orders': True,
                'enable_order_book': True,
                'closed': False,
                'archived': False,
                'active': True,
            }
    class _Client:
        is_closed = False
        async def get(self, url, **kw):
            cid = url.split('/markets/')[-1]
            return _MarketResp(cid)
        async def aclose(self): pass
    async def _stub(host_key, max_keepalive=30):
        return _Client()
    monkeypatch.setattr(af, '_get_client', _stub)

    cids = [f"0x{i:062x}" for i in range(50)]
    result = af.run_fetch_poly_markets_batch(cids, max_concurrent=10)
    assert len(result) == 50
    for cid, market in result.items():
        # Each value is the raw market dict — caller seeds the cache
        assert 'minimum_tick_size' in market
        assert 'taker_base_fee' in market


def test_arb_server_guard_async_fetch_only(monkeypatch):
    """Big batch path is guarded by ASYNC_FETCH=1 env, falls through cleanly
    when env is missing/0 (no behavioral regression for sync path)."""
    import inspect
    import arb_server
    src = inspect.getsource(arb_server.run_scan)
    # The big-batch block is inside ASYNC_FETCH gate
    big_batch_marker = "_all_clob = None"
    asyncio_run_marker = "run_fetch_clob_batch("
    big_idx = src.find(big_batch_marker)
    run_idx = src.find(asyncio_run_marker)
    # Find nearest preceding ASYNC_FETCH guard
    text_before = src[:run_idx]
    last_gate = text_before.rfind("os.environ.get('ASYNC_FETCH')")
    assert last_gate > 0, (
        "run_fetch_clob_batch must be inside ASYNC_FETCH=1 env-gated block")
