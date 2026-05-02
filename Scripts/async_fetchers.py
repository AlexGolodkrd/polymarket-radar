"""Async HTTP fetchers — Phase 9fff (29.04.2026).

Drop-in replacement for the sync requests.Session-based fetchers in
arb_server.py. Activated by env ASYNC_FETCH=1; the sync path remains
the default until we've measured a full scan cycle on the VPS.

Why async?
----------
ThreadPoolExecutor with 30 workers fires 30 sync HTTP calls in 30 OS
threads, and each thread acquires the GIL during request body parsing
+ JSON deserialization. With 90+ orderbook fetches per scan, GIL
contention dominated wall time even though network I/O is the actual
bottleneck.

httpx.AsyncClient + asyncio uses ONE thread + an event loop. When a
fetch awaits I/O, the loop schedules another fetch. No threads = no
GIL contention. Measured 2-3x speedup on similar codebases.

Drop-in compatibility
---------------------
Each function returns the SAME tuple shape as its sync counterpart in
arb_server.py:
    fetch_clob_async(token_id) -> (token_id, best_ask, depth)
    fetch_limitless_orderbook_async(slug) -> (slug, ya, dy, na, dn)
    ...

`async_batch_fetch(fn, ids, max_concurrent=30)` wraps asyncio.gather
with a Semaphore — same effect as ThreadPoolExecutor(max_workers=30)
but in one thread.

The sync entry point `run_async_batch(fn_async, ids)` packages
asyncio.run() so callers in sync code (run_scan) can swap one line
without restructuring everything around it.
"""
from __future__ import annotations

import asyncio
import time
import os
from typing import Callable, Dict, Iterable, List, Optional, Tuple

# httpx is optional — if not installed, this module's functions
# raise ImportError on first call. arb_server checks for the
# presence and gates ASYNC_FETCH off if so.
try:
    import httpx
    _HAS_HTTPX = True
except ImportError:
    _HAS_HTTPX = False
    httpx = None  # type: ignore


# Match the sync (connect, read) tuple semantics from arb_server._FETCH_TIMEOUT
_FETCH_TIMEOUT_CONNECT = 3.0
_FETCH_TIMEOUT_READ = 8.0
_FETCH_TIMEOUT = httpx.Timeout(connect=_FETCH_TIMEOUT_CONNECT,
                                read=_FETCH_TIMEOUT_READ,
                                write=_FETCH_TIMEOUT_READ,
                                pool=_FETCH_TIMEOUT_CONNECT) if _HAS_HTTPX else None

# Per-host async clients. Created lazily on first use; kept warm with
# their connection pool (HTTP/2 multiplexing if supported by the server).
# Keep one per backend so a hung Limitless connection doesn't poison the
# Polymarket pool and vice versa.
_ASYNC_CLIENTS: Dict[str, "httpx.AsyncClient"] = {}
_ASYNC_CLIENTS_LOCK = asyncio.Lock()


async def _get_client(host_key: str, max_keepalive: int = 30) -> "httpx.AsyncClient":
    """Get or create the async client for a backend.

    Phase 9iii (30.04.2026) — HTTP/2 multiplexing for `limitless`.
    Phase 18 (02.05.2026) — HTTP/2 also for `gamma` (Polymarket events
    pagination) and `sx` (markets/active pagination). Cloudflare sees
    1 client, no rate limit triggers even with 15+ parallel streams.

    Limitless API rate-limits per CONNECTION at >40 concurrent. With
    HTTP/2 we open ONE TCP+TLS connection and multiplex many parallel
    requests inside it as separate streams. Server sees 1 client, so
    rate limit doesn't trigger.

    Polymarket /book stays on HTTP/1.1 — per-token fetches are bursty
    via batch_fetch and benefit from connection-pool keepalive instead.
    """
    if not _HAS_HTTPX:
        raise ImportError("httpx not installed — pip install httpx>=0.27")
    async with _ASYNC_CLIENTS_LOCK:
        client = _ASYNC_CLIENTS.get(host_key)
        if client is None or client.is_closed:
            if host_key in ('limitless', 'gamma', 'sx'):
                # HTTP/2: ONE connection, N parallel streams.
                limits = httpx.Limits(max_keepalive_connections=2,
                                      max_connections=2)
                client = httpx.AsyncClient(
                    timeout=_FETCH_TIMEOUT, limits=limits, http2=True,
                    headers={
                        'User-Agent': 'plan-kapkan-radar/1.0 (arbitrage scanner)',
                        'Accept': 'application/json',
                        'Accept-Language': 'en-US,en;q=0.9',
                    },
                )
            else:
                limits = httpx.Limits(max_keepalive_connections=max_keepalive,
                                      max_connections=max_keepalive * 2)
                client = httpx.AsyncClient(timeout=_FETCH_TIMEOUT, limits=limits,
                                           http2=False)
            _ASYNC_CLIENTS[host_key] = client
        return client


async def close_all_clients() -> None:
    """Close every cached client. Call before process exit (or in tests)."""
    async with _ASYNC_CLIENTS_LOCK:
        for c in _ASYNC_CLIENTS.values():
            try:
                await c.aclose()
            except Exception:
                pass
        _ASYNC_CLIENTS.clear()


# ── Per-fetcher async implementations ──────────────────────────────

async def fetch_clob_async(token_id: str,
                            slippage_tolerance: float = 0.005) -> tuple:
    """Polymarket CLOB orderbook for one token (V2 — bids included).

    Phase 19 (02.05.2026): matches sync `_fetch_clob` 5-tuple output:
        (token_id, best_ask, ask_depth_usd, best_bid, bid_depth_usd)

    Why bids: YES bid mathematically equals NO ask (Polymarket complement
    rule). Synthesis unlocks structure C on binary sport markets when NO
    book is empty. Caller (arb_server) consumes via the same
    `_top_of_book_depth_usd` semantics as the sync version.
    """
    try:
        client = await _get_client('poly')
        r = await client.get(
            f"https://clob.polymarket.com/book?token_id={token_id}",
        )
        body = r.json() or {}
        asks = body.get('asks', []) or []
        bids = body.get('bids', []) or []

        # Top-of-book depth (asks): ascending sort. Sum sizes within tolerance.
        best_ask, ask_depth = None, 0.0
        parsed_asks = []
        for a in asks:
            try:
                if isinstance(a, dict):
                    p = float(a.get('price', 0)); s = float(a.get('size', 0))
                else:
                    p = float(a[0]); s = float(a[1])
                if p > 0 and s > 0:
                    parsed_asks.append((p, s))
            except Exception:
                continue
        if parsed_asks:
            parsed_asks.sort(key=lambda x: x[0])
            best_ask = parsed_asks[0][0]
            cutoff = best_ask + slippage_tolerance + 1e-9
            for p, s in parsed_asks:
                if p > cutoff:
                    break
                ask_depth += p * s

        # Top-of-book depth (bids): descending sort.
        best_bid, bid_depth = None, 0.0
        parsed_bids = []
        for b in bids:
            try:
                if isinstance(b, dict):
                    p = float(b.get('price', 0)); s = float(b.get('size', 0))
                else:
                    p = float(b[0]); s = float(b[1])
                if p > 0 and s > 0:
                    parsed_bids.append((p, s))
            except Exception:
                continue
        if parsed_bids:
            parsed_bids.sort(key=lambda x: -x[0])
            best_bid = parsed_bids[0][0]
            cutoff = best_bid - slippage_tolerance - 1e-9
            for p, s in parsed_bids:
                if p < cutoff:
                    break
                bid_depth += p * s

        return token_id, best_ask, ask_depth, best_bid, bid_depth
    except Exception:
        return token_id, None, 0.0, None, 0.0


# ── Phase 19 (02.05.2026) — async batch /book fetcher ──────────────
# Replaces ThreadPoolExecutor in arb_server.batch_fetch for Polymarket
# tokens. Wins:
#   * One thread, no GIL contention during JSON parsing
#   * httpx connection pool reuses TCP+TLS for Polymarket CLOB
#   * max_concurrent semaphore prevents flooding /book (1500/10s limit)
# Empirical baseline (sync): 30 threads, ~3000 tokens, 60-100s wall.
# Expected (async): ~5-15s wall, dominated by network round-trip.
async def fetch_clob_batch_async(token_ids: List[str],
                                   max_concurrent: int = 30,
                                   slippage_tolerance: float = 0.005) -> Dict[str, tuple]:
    """Fetch /book for many tokens in parallel. Returns dict
    {token_id: (best_ask, ask_depth, best_bid, bid_depth)}.

    Note the value tuple is 4-element (no token_id prefix) — matches what
    arb_server.batch_fetch returns to its callers, where keys are the
    token_ids and values are stripped of the leading id.
    """
    if not token_ids:
        return {}
    sem = asyncio.Semaphore(max_concurrent)

    async def _one(tid: str) -> tuple:
        async with sem:
            return await fetch_clob_async(tid, slippage_tolerance=slippage_tolerance)

    tasks = [_one(tid) for tid in token_ids]
    results = await asyncio.gather(*tasks, return_exceptions=False)
    out: Dict[str, tuple] = {}
    for tup in results:
        # tup = (token_id, best_ask, ask_depth, best_bid, bid_depth)
        out[tup[0]] = tup[1:]
    return out


def run_fetch_clob_batch(token_ids: List[str],
                          max_concurrent: int = 30,
                          slippage_tolerance: float = 0.005) -> Dict[str, tuple]:
    """Sync wrapper for run_scan() / batch_fetch shim. Spawns fresh loop."""
    if not _HAS_HTTPX:
        raise ImportError("httpx required")
    return asyncio.run(fetch_clob_batch_async(
        token_ids, max_concurrent=max_concurrent,
        slippage_tolerance=slippage_tolerance,
    ))


async def fetch_limitless_orderbook_async(slug: str) -> tuple:
    """Limitless orderbook. Returns (slug, yes_ask, depth_yes, no_ask, depth_no).
    Same top-of-book + USDC-raw normalization rules as the sync version.

    Phase 9iii: respects Retry-After on 429/503 with exponential backoff,
    max 3 attempts. 403 is NOT retried (server is firmly refusing).
    Phase 9kkk: integrated with CircuitBreaker + http_codes universal handler.
    """
    import sys
    # Phase 9kkk — circuit breaker short-circuit
    try:
        from circuit_breaker import get_breaker
        from http_codes import classify, Action, compute_backoff, format_log
        cb = get_breaker('limitless', failure_threshold=3,
                         cool_down_seconds=300, success_threshold=2)
        if not cb.allow():
            # Breaker open — return empty without I/O
            return slug, None, 0, None, 0
    except ImportError:
        cb = None
        classify = None
    try:
        client = await _get_client('limitless')
        url = f"https://api.limitless.exchange/markets/{slug}/orderbook"
        r = None
        for attempt in range(3):
            r = await client.get(url)
            if classify is not None:
                action = classify(r.status_code)
                if action == Action.RETRY_BACKOFF:
                    retry_after = r.headers.get('Retry-After')
                    wait = compute_backoff(action, attempt,
                                            float(retry_after) if retry_after else None)
                    print(format_log('limitless', r.status_code, url, attempt + 1),
                          flush=True, file=sys.stderr)
                    await asyncio.sleep(wait)
                    continue
                if action == Action.RETRY_TRANSIENT:
                    print(format_log('limitless', r.status_code, url, attempt + 1),
                          flush=True, file=sys.stderr)
                    await asyncio.sleep(compute_backoff(action, attempt))
                    continue
                if action == Action.OPEN_BREAKER:
                    print(format_log('limitless', r.status_code, url, attempt + 1),
                          flush=True, file=sys.stderr)
                    if cb: cb.on_failure(reason=f'HTTP {r.status_code}')
                    return slug, None, 0, None, 0
                if action == Action.SKIP_CLIENT_ERR:
                    # 4xx — log once, do NOT retry. Treat as no data.
                    print(format_log('limitless', r.status_code, url, attempt + 1),
                          flush=True, file=sys.stderr)
                    return slug, None, 0, None, 0
                # SUCCESS / NOT_FOUND / UNKNOWN — fall through to body parse
                break
            else:
                # Fallback when http_codes not importable
                if r.status_code in (429, 503):
                    import random as _rnd
                    wait = float(r.headers.get('Retry-After', 2 ** attempt))
                    wait = min(wait, 10) + _rnd.random() * 0.3
                    await asyncio.sleep(wait)
                    continue
                break
        if r is None or r.status_code != 200:
            if cb and r is not None and r.status_code != 200:
                cb.on_failure(reason=f'HTTP {r.status_code}')
            return slug, None, 0, None, 0
        ob = r.json()
        asks = ob.get('asks') or []
        bids = ob.get('bids') or []
        # Top-of-book only (Phase 9y rule)
        best_yes_ask, depth_yes = None, 0
        if asks:
            try:
                top = sorted(asks, key=lambda a: float(a.get('price', 999)))[0]
                best_yes_ask = float(top.get('price', 0))
                size = float(top.get('size', 0))
                # USDC raw normalization (Phase 9aa)
                if size > 1e6:
                    size = size / 1e6
                depth_yes = best_yes_ask * size
            except Exception:
                pass
        # NO-side from best YES bid (Phase 9y same rule)
        best_no_ask, depth_no = None, 0
        if bids:
            try:
                top = sorted(bids, key=lambda b: float(b.get('price', 0)), reverse=True)[0]
                best_yes_bid = float(top.get('price', 0))
                if 0 < best_yes_bid < 1:
                    best_no_ask = 1 - best_yes_bid
                    size = float(top.get('size', 0))
                    if size > 1e6:
                        size = size / 1e6
                    depth_no = best_yes_bid * size
            except Exception:
                pass
        # Phase 9kkk — mark success in circuit breaker
        if cb:
            cb.on_success()
        return slug, best_yes_ask, depth_yes, best_no_ask, depth_no
    except Exception as e:
        if cb:
            cb.on_failure(reason=f'exception: {type(e).__name__}')
        return slug, None, 0, None, 0


# ── Phase 9kkk: parallel main-page fetcher for Limitless ──────────

async def fetch_limitless_pages_async(
    page_size: int = 25,
    max_pages: int = 40,
    max_concurrent: int = 20,
) -> List[dict]:
    """Fetch all /markets/active pages in parallel via HTTP/2 multiplexing.

    Replaces the sequential loop in arb_server.py:2964-2986. Limitless API
    hard-caps page_size at 25, so we MUST hit ~40 pages to cover 1000 markets.
    Sequential = 40 × 1s = 40s. Parallel HTTP/2 multiplexed = ~2-3s
    (verified: 60 concurrent pages return in 2.45s from VPS).

    Stops accumulating when:
      - A page returns 0 items (we've exceeded the active set)
      - A page returns < page_size (last page)
      - Too many failures (circuit breaker tripped → return what we have)

    `max_concurrent` caps simultaneous in-flight requests. HTTP/2 over one
    TCP connection means even max_concurrent=40 is fine, but 20 is a
    polite default that keeps memory + h2 frame buffers reasonable.
    """
    if not _HAS_HTTPX:
        raise ImportError("httpx required")
    import sys
    from urllib.parse import urlencode
    try:
        from circuit_breaker import get_breaker
        from http_codes import classify, Action, format_log
        cb = get_breaker('limitless', failure_threshold=3,
                         cool_down_seconds=300, success_threshold=2)
    except ImportError:
        cb = None
        classify = None

    if cb and not cb.allow():
        print(f"[fetch_limitless_pages] CB open — returning empty",
              flush=True, file=sys.stderr)
        return []

    client = await _get_client('limitless')
    sem = asyncio.Semaphore(max_concurrent)

    async def fetch_one(page_num: int) -> tuple:
        """Returns (page_num, items_list, status_code). items_list is None on hard error."""
        async with sem:
            qs = urlencode({'page': page_num, 'limit': page_size})
            url = f"https://api.limitless.exchange/markets/active?{qs}"
            try:
                r = await client.get(url)
                if classify is not None:
                    action = classify(r.status_code)
                    if action == Action.OPEN_BREAKER:
                        print(format_log('limitless', r.status_code, url, 1),
                              flush=True, file=sys.stderr)
                        if cb: cb.on_failure(reason=f'HTTP {r.status_code}')
                        return page_num, None, r.status_code
                if r.status_code != 200:
                    if cb: cb.on_failure(reason=f'HTTP {r.status_code}')
                    return page_num, None, r.status_code
                data = r.json()
                items = (data if isinstance(data, list)
                         else data.get('data') or data.get('markets') or [])
                if cb: cb.on_success()
                return page_num, items, 200
            except Exception as e:
                if cb: cb.on_failure(reason=f'exception: {type(e).__name__}')
                return page_num, None, 0

    # Fan out all pages in parallel — HTTP/2 multiplexes over single TCP
    t0 = time.time()
    tasks = [fetch_one(p) for p in range(1, max_pages + 1)]
    results = await asyncio.gather(*tasks, return_exceptions=False)
    elapsed = time.time() - t0

    # Sort by page number, stop at first empty/short page
    results.sort(key=lambda x: x[0])
    all_events: List[dict] = []
    last_page_seen = 0
    for page_num, items, status in results:
        if items is None:
            # Hard error on this page — caller can decide
            continue
        if not items:
            break  # empty page = beyond active set
        all_events.extend(items)
        last_page_seen = page_num
        if len(items) < page_size:
            break  # last page on API
    print(f"[fetch_limitless_pages] {len(all_events)} events from "
          f"{last_page_seen}/{max_pages} pages in {elapsed:.2f}s "
          f"(parallel HTTP/2)", flush=True)
    return all_events


def run_fetch_limitless_pages(page_size: int = 25,
                                max_pages: int = 40,
                                max_concurrent: int = 20) -> List[dict]:
    """Sync wrapper for run_scan() to call. Spawns fresh event loop."""
    if not _HAS_HTTPX:
        raise ImportError("httpx required")
    return asyncio.run(fetch_limitless_pages_async(
        page_size=page_size,
        max_pages=max_pages,
        max_concurrent=max_concurrent,
    ))


# ── Phase 18 (02.05.2026) — parallel fetcher for Polymarket /events ──
# Cloudflare rate limit on gamma-api: 500 req/10s for /events (= 50 RPS).
# Default max_concurrent=10 → 5× headroom even if scan fires twice in a
# row. HTTP/2 multiplexing on one TCP keeps Cloudflare seeing 1 client.
# Empirical (live test 02.05.2026 from VPS): 15 parallel pages in 0.5s,
# all 200 OK, no 429 observed.
async def fetch_poly_events_pages_async(page_size: int = 500,
                                          max_pages: int = 15,
                                          max_concurrent: int = 10) -> List[dict]:
    """Fetch all gamma-api /events pages in parallel.

    Returns concatenated event list, sorted by page (so order is stable
    for caller's chunked processing). Empty pages truncate the run.
    """
    import sys
    try:
        from circuit_breaker import get_breaker
        from http_codes import classify, Action, format_log
        cb = get_breaker('gamma', failure_threshold=3,
                         cool_down_seconds=300, success_threshold=2)
    except ImportError:
        cb = None
        classify = None

    if cb and not cb.allow():
        print(f"[fetch_poly_events_pages] CB open — returning empty",
              flush=True, file=sys.stderr)
        return []

    client = await _get_client('gamma')
    sem = asyncio.Semaphore(max_concurrent)

    async def fetch_one(page_idx: int) -> tuple:
        offset = page_idx * page_size
        url = (f"https://gamma-api.polymarket.com/events?"
               f"closed=false&active=true&limit={page_size}&offset={offset}")
        async with sem:
            try:
                r = await client.get(url)
                if classify is not None:
                    action = classify(r.status_code)
                    if action == Action.OPEN_BREAKER:
                        print(format_log('gamma', r.status_code, url, 1),
                              flush=True, file=sys.stderr)
                        if cb: cb.on_failure(reason=f'HTTP {r.status_code}')
                        return page_idx, None, r.status_code
                if r.status_code != 200:
                    if cb: cb.on_failure(reason=f'HTTP {r.status_code}')
                    return page_idx, None, r.status_code
                items = r.json()
                if cb: cb.on_success()
                return page_idx, (items if isinstance(items, list) else []), 200
            except Exception as e:
                if cb: cb.on_failure(reason=f'exception: {type(e).__name__}')
                return page_idx, None, 0

    t0 = time.time()
    tasks = [fetch_one(p) for p in range(max_pages)]
    results = await asyncio.gather(*tasks, return_exceptions=False)
    elapsed = time.time() - t0

    # Sort by page index, stop concatenating after first short/empty page
    # (gamma-api returns shorter pages near the tail — same convention as
    # Limitless). Hard-error pages (status != 200) are skipped.
    results.sort(key=lambda x: x[0])
    all_events: List[dict] = []
    last_page_seen = -1
    for page_idx, items, status in results:
        if items is None:
            continue
        if not items:
            break
        all_events.extend(items)
        last_page_seen = page_idx
        if len(items) < page_size:
            break
    print(f"[fetch_poly_events_pages] {len(all_events)} events from "
          f"{last_page_seen+1}/{max_pages} pages in {elapsed:.2f}s "
          f"(parallel HTTP/2)", flush=True)
    return all_events


def run_fetch_poly_events_pages(page_size: int = 500,
                                  max_pages: int = 15,
                                  max_concurrent: int = 10) -> List[dict]:
    """Sync wrapper for run_scan() to call. Spawns fresh event loop."""
    if not _HAS_HTTPX:
        raise ImportError("httpx required")
    return asyncio.run(fetch_poly_events_pages_async(
        page_size=page_size,
        max_pages=max_pages,
        max_concurrent=max_concurrent,
    ))


# ── Phase 18 — parallel fetcher for SX Bet /markets/active ────────
# SX paginates via `paginationKey` (cursor), not offset — so we can't
# parallelize naively without knowing total page count. Strategy: fetch
# page 1 to get nextKey, then fan out batches sequentially up to
# max_pages. In practice SX has ~3-5 pages of active markets, so the
# overhead vs full parallel is negligible.
# Plus: SX has very tight Cloudflare protection — being conservative.
async def fetch_sx_markets_async(page_size: int = 500,
                                   max_pages: int = 10) -> tuple:
    """Fetch SX markets via cursor pagination.
    Returns (markets_list, http_status_first, fetch_error_str_or_none).
    """
    import sys
    try:
        from circuit_breaker import get_breaker
        from http_codes import classify, Action, format_log
        cb = get_breaker('sx', failure_threshold=3,
                         cool_down_seconds=300, success_threshold=2)
    except ImportError:
        cb = None
        classify = None

    if cb and not cb.allow():
        return [], None, 'circuit_breaker_open'

    client = await _get_client('sx')
    base = "https://api.sx.bet/markets/active"
    markets: List[dict] = []
    next_key: Optional[str] = None
    first_status = None
    err = None
    t0 = time.time()
    for page_idx in range(max_pages):
        qs = f"onlyMainLine=true&pageSize={page_size}"
        if next_key:
            qs += f"&paginationKey={next_key}"
        try:
            r = await client.get(f"{base}?{qs}")
            if first_status is None:
                first_status = r.status_code
            if r.status_code != 200:
                if cb: cb.on_failure(reason=f'HTTP {r.status_code}')
                err = f'http_{r.status_code}'
                break
            data = r.json()
            if data.get('status') != 'success':
                err = f"status={data.get('status')} msg={str(data)[:100]}"
                if cb: cb.on_failure(reason=err)
                break
            data_obj = data.get('data') or {}
            page_markets = data_obj.get('markets') or []
            markets.extend(page_markets)
            next_key = data_obj.get('nextKey')
            if cb: cb.on_success()
            if not next_key:
                break
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            if cb: cb.on_failure(reason=f'exception: {type(e).__name__}')
            break
    elapsed = time.time() - t0
    print(f"[fetch_sx_markets] {len(markets)} markets in {elapsed:.2f}s "
          f"(http={first_status}, err={err})", flush=True)
    return markets, first_status, err


def run_fetch_sx_markets(page_size: int = 500,
                          max_pages: int = 10) -> tuple:
    """Sync wrapper. Returns (markets, http_status, error_str|None)."""
    if not _HAS_HTTPX:
        raise ImportError("httpx required")
    return asyncio.run(fetch_sx_markets_async(
        page_size=page_size,
        max_pages=max_pages,
    ))


# ── Phase 9kkk: parallel meta fetcher for Limitless ──────────────

async def fetch_limitless_meta_async(slug: str) -> tuple:
    """Fetch /markets/{slug} for tick/min/fee/venue. Returns (slug, meta_dict).

    Used by `_fetch_limitless_market_meta` to populate per-slug cache.
    Like orderbook fetcher, integrates with CB + http_codes.
    """
    if not _HAS_HTTPX:
        raise ImportError("httpx required")
    import sys
    try:
        from circuit_breaker import get_breaker
        from http_codes import classify, Action, format_log, compute_backoff
        cb = get_breaker('limitless')
    except ImportError:
        cb = None
        classify = None
    if cb and not cb.allow():
        return slug, None
    client = await _get_client('limitless')
    url = f"https://api.limitless.exchange/markets/{slug}"
    try:
        for attempt in range(3):
            r = await client.get(url)
            if classify is not None:
                action = classify(r.status_code)
                if action == Action.RETRY_BACKOFF or action == Action.RETRY_TRANSIENT:
                    retry_after = r.headers.get('Retry-After')
                    wait = compute_backoff(action, attempt,
                                            float(retry_after) if retry_after else None)
                    print(format_log('limitless', r.status_code, url, attempt + 1),
                          flush=True, file=sys.stderr)
                    await asyncio.sleep(wait)
                    continue
                if action == Action.OPEN_BREAKER:
                    print(format_log('limitless', r.status_code, url, attempt + 1),
                          flush=True, file=sys.stderr)
                    if cb: cb.on_failure(reason=f'HTTP {r.status_code}')
                    return slug, None
                if action in (Action.SKIP_CLIENT_ERR, Action.NOT_FOUND):
                    return slug, None
                break
            else:
                break
        if r.status_code != 200:
            if cb: cb.on_failure(reason=f'HTTP {r.status_code}')
            return slug, None
        meta = r.json()
        if cb: cb.on_success()
        return slug, meta
    except Exception as e:
        if cb: cb.on_failure(reason=f'exception: {type(e).__name__}')
        return slug, None


# ── Async batch_fetch — analogue of arb_server.batch_fetch ─────────

async def async_batch_fetch(fn_async: Callable, ids: Iterable[str],
                             max_concurrent: int = 30,
                             budget_s: float = None) -> Dict:
    """Fan-out async fetches with concurrency limit + budget.

    Same contract as arb_server.batch_fetch:
      Returns dict mapping id → tuple (without the id prefix).
      Drops failed/timed-out items silently.

    `budget_s` defaults to max(30, 2.5 × len/max_concurrent) — same as sync.
    """
    ids_list = list(ids)
    if not ids_list:
        return {}
    if budget_s is None:
        budget_s = max(30.0, 2.5 * len(ids_list) / max(1, max_concurrent))

    sem = asyncio.Semaphore(max_concurrent)

    async def _bounded(_id):
        async with sem:
            return await fn_async(_id)

    results: Dict = {}
    t0 = time.time()
    try:
        gathered = await asyncio.wait_for(
            asyncio.gather(*[_bounded(i) for i in ids_list],
                           return_exceptions=True),
            timeout=budget_s,
        )
        for res in gathered:
            if isinstance(res, BaseException):
                continue
            if res and res[0] is not None:
                results[res[0]] = res[1:]
    except asyncio.TimeoutError:
        # Bail with whatever we have. Caller proceeds with partial data.
        elapsed = int(time.time() - t0)
        fn_name = getattr(fn_async, '__name__', 'fn')
        print(f"[async_batch_fetch:{fn_name}] timeout after {elapsed}s — "
              f"{len(results)}/{len(ids_list)} done", flush=True)
    return results


# ── Sync entry point — for callers in sync code (run_scan) ────────

def run_async_batch(fn_async: Callable, ids: Iterable[str],
                    max_concurrent: int = 30) -> Dict:
    """Sync wrapper around async_batch_fetch — runs a fresh event loop
    just for this batch, returns when all coroutines complete (or budget
    exhausted). Use from sync code that doesn't already have an event loop.

    Each call creates and tears down its own loop; for a batch of 100 ids
    this overhead is negligible vs the network time. For tighter loops
    (e.g., per-WS-update re-fetch), prefer keeping one shared loop alive."""
    if not _HAS_HTTPX:
        raise ImportError("httpx required for async fetchers")
    return asyncio.run(async_batch_fetch(fn_async, ids,
                                          max_concurrent=max_concurrent))
