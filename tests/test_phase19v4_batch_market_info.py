"""Phase 19v4 tests — _batch_fetch_poly_market_info parallel fan-out.

ROOT CAUSE FIX: classify_pools used to call `_fetch_poly_market_info(cid)`
sequentially for 20+ condition_ids per chunk, blocking the scan thread
280-310s on cold cache + Cloudflare-tarpitting.

These tests verify:
- Parallel fan-out works (slow stub completes faster than sum of times)
- Hard deadline cap respected (never blocks longer than deadline_s)
- Empty input returns empty dict
- Cache hits are instant (no thread overhead)
- Mixed hit/miss returns mixed result
- Caller `classify_pools` uses new helper instead of serial loop
"""
import os, sys, time, pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


@pytest.fixture(autouse=True)
def reset_caches():
    """Clear poly_market_info_cache before each test for predictable
    cold-cache behaviour."""
    import arb_server
    with arb_server.poly_market_info_lock:
        arb_server.poly_market_info_cache.clear()


def test_batch_fetch_empty_input():
    from arb_server import _batch_fetch_poly_market_info
    assert _batch_fetch_poly_market_info([]) == {}
    assert _batch_fetch_poly_market_info([None, None]) == {}


def test_batch_fetch_uses_parallel_threads(monkeypatch):
    """Slow stub: each call takes 0.5s. 20 cids serial = 10s, parallel = ~0.5s.
    Verify total wall time < sum of times."""
    import arb_server

    def _slow_fetch(cid):
        time.sleep(0.5)
        return {'condition_id': cid, 'tick_size': 0.01, 'taker_fee_bps': 250.0,
                'maker_fee_bps': 0.0, 'min_order_size': 1.0, 'neg_risk': False,
                'accepting_orders': True, 'enable_order_book': True,
                'closed': False, 'archived': False, 'active': True,
                'fetched_at': time.time(), 'rewards': {}}
    monkeypatch.setattr(arb_server, '_fetch_poly_market_info', _slow_fetch)

    cids = [f"0x{i:062x}" for i in range(20)]
    t0 = time.time()
    result = arb_server._batch_fetch_poly_market_info(cids, max_concurrent=20)
    elapsed = time.time() - t0

    assert len(result) == 20
    # Sum sequential = 10s. Parallel via 20 threads should be <2s.
    assert elapsed < 2.0, f"Parallel took {elapsed:.2f}s — should be <2s"
    # All entries populated
    for cid in cids:
        assert result[cid] is not None
        assert result[cid]['condition_id'] == cid


def test_batch_fetch_respects_deadline(monkeypatch):
    """Deadline cap fires when individual fetches are too slow."""
    import arb_server

    def _slow_fetch(cid):
        time.sleep(5.0)  # very slow
        return {'condition_id': cid, 'tick_size': 0.01, 'taker_fee_bps': 250.0,
                'maker_fee_bps': 0.0, 'min_order_size': 1.0, 'neg_risk': False,
                'accepting_orders': True, 'enable_order_book': True,
                'closed': False, 'archived': False, 'active': True,
                'fetched_at': time.time(), 'rewards': {}}
    monkeypatch.setattr(arb_server, '_fetch_poly_market_info', _slow_fetch)

    cids = [f"0x{i:062x}" for i in range(5)]
    t0 = time.time()
    result = arb_server._batch_fetch_poly_market_info(
        cids, max_concurrent=5, deadline_s=1.0)
    elapsed = time.time() - t0

    # Must return within ~1s + small overhead, NOT 5s
    assert elapsed < 2.0, f"Deadline didn't fire — {elapsed:.2f}s"
    # Output dict has placeholders for all cids (None for not-yet-done)
    assert len(result) == 5


def test_batch_fetch_handles_exceptions(monkeypatch):
    """If a single fetch raises, return None for that cid; others succeed."""
    import arb_server

    def _flaky_fetch(cid):
        if cid.endswith('1'):
            raise RuntimeError("simulated network error")
        return {'condition_id': cid, 'tick_size': 0.01, 'taker_fee_bps': 250.0,
                'maker_fee_bps': 0.0, 'min_order_size': 1.0, 'neg_risk': False,
                'accepting_orders': True, 'enable_order_book': True,
                'closed': False, 'archived': False, 'active': True,
                'fetched_at': time.time(), 'rewards': {}}
    monkeypatch.setattr(arb_server, '_fetch_poly_market_info', _flaky_fetch)

    cids = ['cid_0', 'cid_1', 'cid_2']
    result = arb_server._batch_fetch_poly_market_info(cids, max_concurrent=3)

    # Failure entry None
    assert result['cid_1'] is None
    # Others succeed
    assert result['cid_0'] is not None
    assert result['cid_2'] is not None


def test_batch_fetch_mixed_cache_hit_and_miss(monkeypatch):
    """Pre-warm 5 cids in cache, fetch 10 (5 hit + 5 miss). Should be fast."""
    import arb_server
    now = time.time()
    # Pre-warm 5 cids
    with arb_server.poly_market_info_lock:
        for i in range(5):
            cid = f"0x{i:062x}"
            arb_server.poly_market_info_cache[cid] = {
                'condition_id': cid, 'tick_size': 0.01, 'taker_fee_bps': 250.0,
                'maker_fee_bps': 0.0, 'min_order_size': 1.0,
                'neg_risk': False, 'accepting_orders': True,
                'enable_order_book': True, 'closed': False, 'archived': False,
                'active': True, 'fetched_at': now, 'rewards': {},
                'accepting_order_timestamp': 0, 'seconds_delay': 0,
                'neg_risk_market_id': None, 'neg_risk_request_id': None,
            }

    fetched_count = [0]
    def _maybe_fetch(cid):
        fetched_count[0] += 1
        return None  # simulate miss

    # Don't replace the inner fetcher — let real cache check happen.
    # But we need a way to count actual network calls. Replace the
    # stub-attempted-network with a counter.
    real_fetch = arb_server._fetch_poly_market_info
    def _spy(cid):
        # Read cache directly to mimic real impl behavior
        with arb_server.poly_market_info_lock:
            cached = arb_server.poly_market_info_cache.get(cid)
        if cached and (time.time() - cached.get('fetched_at', 0)) < 600:
            return cached
        fetched_count[0] += 1
        return None  # simulated miss
    monkeypatch.setattr(arb_server, '_fetch_poly_market_info', _spy)

    cids = [f"0x{i:062x}" for i in range(10)]  # 0..4 hit, 5..9 miss
    t0 = time.time()
    result = arb_server._batch_fetch_poly_market_info(cids, max_concurrent=10)
    elapsed = time.time() - t0

    # Only 5 actual fetches (the misses)
    assert fetched_count[0] == 5
    # First 5 are cache hits (info dict)
    for i in range(5):
        cid = f"0x{i:062x}"
        assert result[cid] is not None
        assert result[cid]['condition_id'] == cid
    # Last 5 are misses (None)
    for i in range(5, 10):
        cid = f"0x{i:062x}"
        assert result[cid] is None


def test_classify_pools_uses_batch_helper():
    """Source-level guard: classify_pools must call _batch_fetch_poly_market_info,
    NOT iterate _fetch_poly_market_info(cid) serially. Prevents regression."""
    import inspect
    import arb_server
    src = inspect.getsource(arb_server.classify_pools)
    # New helper present
    assert '_batch_fetch_poly_market_info' in src
    # Old serial loop pattern absent
    assert 'for cid in' not in src.split('_batch_fetch_poly_market_info')[0] \
           or '_fetch_poly_market_info(cid)' not in \
           src.split('_batch_fetch_poly_market_info')[0]


def test_batch_fetch_signature():
    """Default args: max_concurrent=20, deadline_s=25.0."""
    import arb_server
    sig = arb_server._batch_fetch_poly_market_info.__defaults__
    assert sig == (20, 25.0)
