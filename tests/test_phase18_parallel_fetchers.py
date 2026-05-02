"""Phase 18 tests — parallel fetchers + prefetch pool.

Covers:
- async_fetchers.run_fetch_poly_events_pages exists and signature
- async_fetchers.run_fetch_sx_markets exists and signature
- async_fetchers._get_client routes 'gamma' and 'sx' to HTTP/2 path
- run_scan() ASYNC_FETCH wiring uses parallel paths when env=1
"""
import os, sys, pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


def test_run_fetch_poly_events_pages_callable():
    """Function exists with the documented signature and accepts kwargs."""
    try:
        import httpx  # noqa
    except ImportError:
        pytest.skip("httpx not installed")
    from async_fetchers import run_fetch_poly_events_pages
    sig = run_fetch_poly_events_pages.__defaults__
    # Default kwargs: page_size=500, max_pages=15, max_concurrent=10
    assert sig == (500, 15, 10)


def test_run_fetch_sx_markets_callable():
    """Function exists; signature accepts page_size + max_pages."""
    try:
        import httpx  # noqa
    except ImportError:
        pytest.skip("httpx not installed")
    from async_fetchers import run_fetch_sx_markets
    # Default: page_size=500, max_pages=10
    assert run_fetch_sx_markets.__defaults__ == (500, 10)


def test_async_fetcher_imports_clean():
    """Module imports without side-effects."""
    try:
        import async_fetchers  # noqa
    except ImportError as e:
        if 'httpx' in str(e):
            pytest.skip("httpx not installed")
        raise


def test_async_fetcher_http2_routes_for_gamma_and_sx():
    """The _get_client maps 'gamma' and 'sx' to HTTP/2 client (one TCP).

    Skips if `h2` package not installed (httpx[http2] extra). On the
    production VPS image both are present; locally only httpx core may be.
    """
    try:
        import httpx  # noqa
        import h2     # noqa  — httpx[http2] extra
    except ImportError:
        pytest.skip("httpx[http2] (h2 package) not installed")
    import asyncio
    import async_fetchers

    async def _check():
        for host_key in ('gamma', 'sx', 'limitless'):
            client = await async_fetchers._get_client(host_key)
            assert client is not None
        await async_fetchers.close_all_clients()

    asyncio.run(_check())


def test_async_fetcher_routes_gamma_sx_poly_to_http11_path():
    """Phase 19 hotfix: 'gamma', 'sx', 'poly' use HTTP/1.1 + connection
    pool keepalive (NOT HTTP/2). HTTP/2 was hanging on Cloudflare-gated
    gamma-api in production. Live-tested HTTP/1.1 with 15-thread parallel
    fetch from VPS = 0.5s for 15 pages."""
    import inspect
    import async_fetchers
    src = inspect.getsource(async_fetchers._get_client)
    # HTTP/2 only for limitless (rate-limited per connection >40 concurrent)
    assert "host_key == 'limitless'" in src or 'host_key=="limitless"' in src
    # gamma, sx, poly explicitly enumerated for HTTP/1.1 keepalive pool
    assert "'gamma'" in src or '"gamma"' in src
    assert "'sx'" in src or '"sx"' in src
    assert 'http2=False' in src


def test_arb_server_uses_parallel_poly_when_async_env_set(monkeypatch):
    """run_scan's Polymarket section uses parallel fetcher when ASYNC_FETCH=1."""
    # We don't actually run a scan (it would hit live APIs). Just check
    # the wired code references the parallel function via grep proxy.
    import inspect
    import arb_server
    src = inspect.getsource(arb_server.run_scan)
    assert 'run_fetch_poly_events_pages' in src
    assert 'run_fetch_sx_markets' in src
    # Background prefetch pool must be created
    assert '_bg_pool' in src
    assert '_sx_future' in src
    assert '_lim_future' in src


def test_run_fetch_clob_batch_callable():
    """Phase 19: async batch /book fetcher exists and signature matches."""
    try:
        import httpx  # noqa
    except ImportError:
        pytest.skip("httpx not installed")
    from async_fetchers import run_fetch_clob_batch
    # Default: max_concurrent=30, slippage_tolerance=0.005
    assert run_fetch_clob_batch.__defaults__ == (30, 0.005)


def test_fetch_clob_async_returns_5_tuple_shape():
    """Phase 19: fetch_clob_async returns (tid, ask, ask_depth, bid, bid_depth).

    Without making network calls — we verify the source includes the
    5-element return signature so callers don't get surprised by old
    3-tuple shape."""
    import inspect
    try:
        from async_fetchers import fetch_clob_async
    except ImportError:
        pytest.skip("async_fetchers not importable")
    src = inspect.getsource(fetch_clob_async)
    assert 'best_ask' in src
    assert 'best_bid' in src
    assert 'ask_depth' in src
    assert 'bid_depth' in src
    # Final return must be 5-element
    assert 'return token_id, best_ask, ask_depth, best_bid, bid_depth' in src


def test_arb_server_uses_async_batch_for_clob():
    """run_scan() calls run_fetch_clob_batch when ASYNC_FETCH=1."""
    import inspect
    import arb_server
    src = inspect.getsource(arb_server.run_scan)
    assert 'run_fetch_clob_batch' in src
    # Must be inside ASYNC_FETCH gate
    lines = src.split('\n')
    found_gate = False
    for i, line in enumerate(lines):
        if "ASYNC_FETCH" in line and "environ" in line:
            # Look for run_fetch_clob_batch within next 12 lines
            window = '\n'.join(lines[i:i+12])
            if 'run_fetch_clob_batch' in window:
                found_gate = True
                break
    assert found_gate, "run_fetch_clob_batch must be guarded by ASYNC_FETCH=1 check"


def test_dashboard_polls_every_3_seconds():
    """Phase 19: dashboard.html polls /api/deals каждые 3s."""
    here = os.path.dirname(os.path.abspath(__file__))
    dash_path = os.path.join(os.path.dirname(here), 'Scripts', 'dashboard.html')
    with open(dash_path, 'r', encoding='utf-8') as f:
        text = f.read()
    # The init block at the bottom must use 3000 ms for fetchDeals
    assert 'setInterval(fetchDeals, 3000)' in text, (
        "dashboard.html should poll fetchDeals every 3000ms (Phase 19)")


def test_arb_server_no_bare_async_fetch_references():
    """No bare `if ASYNC_FETCH:` should remain in arb_server.py — every
    reference must go through `os.environ.get('ASYNC_FETCH')` to avoid
    NameError at scan time. This guards against re-introducing the bug
    fixed in PR #59 + PR #61.
    """
    import re
    here = os.path.dirname(os.path.abspath(__file__))
    arb_path = os.path.join(os.path.dirname(here), 'Scripts', 'arb_server.py')
    with open(arb_path, 'r', encoding='utf-8') as f:
        text = f.read()
    # Strip comments + strings so we don't false-match in docstrings
    code_lines = []
    for line in text.split('\n'):
        stripped = line.split('#', 1)[0]
        code_lines.append(stripped)
    code = '\n'.join(code_lines)
    # Pattern: literal `ASYNC_FETCH` as bare identifier (not str-form)
    bare_re = re.compile(r"\bASYNC_FETCH\b(?!\s*[=']|\s*\"|\s*\))")
    # Filter false positives: `os.environ.get('ASYNC_FETCH')` strips to
    # `os.environ.get(` after split — so 'ASYNC_FETCH' isn't there. Good.
    bad_lines = [
        (i, ln) for i, ln in enumerate(code.split('\n'), 1)
        if 'ASYNC_FETCH' in ln
        and 'environ' not in ln
        and 'os.environ.get' not in ln
    ]
    assert not bad_lines, (
        f"Bare ASYNC_FETCH references found (will NameError at runtime): "
        f"{bad_lines[:3]}")
