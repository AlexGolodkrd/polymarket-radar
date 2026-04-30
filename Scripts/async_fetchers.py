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
from typing import Callable, Dict, Iterable, List, Tuple

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

    Limitless API rate-limits per CONNECTION at >40 concurrent. With
    HTTP/2 we open ONE TCP+TLS connection and multiplex many parallel
    requests inside it as separate streams. Server sees 1 client, so
    rate limit doesn't trigger.

    Polymarket and others stay on HTTP/1.1 — no rate-limit issue for
    them, and h2-upgrade adds latency overhead unjustifiably.
    """
    if not _HAS_HTTPX:
        raise ImportError("httpx not installed — pip install httpx>=0.27")
    async with _ASYNC_CLIENTS_LOCK:
        client = _ASYNC_CLIENTS.get(host_key)
        if client is None or client.is_closed:
            if host_key == 'limitless':
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

async def fetch_clob_async(token_id: str) -> tuple:
    """Polymarket CLOB orderbook for one token. Returns (token_id, best_ask, depth)."""
    try:
        client = await _get_client('poly')
        r = await client.get(
            f"https://clob.polymarket.com/book?token_id={token_id}",
        )
        asks = r.json().get('asks', [])
        if not asks:
            return token_id, None, 0
        best = min(asks, key=lambda a: float(a.get('price', 999)))
        depth = sum(float(a.get('size', 0)) * float(a.get('price', 0)) for a in asks)
        return token_id, float(best['price']), depth
    except Exception:
        return token_id, None, 0


async def fetch_limitless_orderbook_async(slug: str) -> tuple:
    """Limitless orderbook. Returns (slug, yes_ask, depth_yes, no_ask, depth_no).
    Same top-of-book + USDC-raw normalization rules as the sync version.

    Phase 9iii: respects Retry-After on 429/503 with exponential backoff,
    max 3 attempts. 403 is NOT retried (server is firmly refusing).
    """
    import random as _rnd
    try:
        client = await _get_client('limitless')
        url = f"https://api.limitless.exchange/markets/{slug}/orderbook"
        r = None
        for attempt in range(3):
            r = await client.get(url)
            if r.status_code in (429, 503):
                wait = float(r.headers.get('Retry-After', 2 ** attempt))
                wait = min(wait, 10) + _rnd.random() * 0.3
                await asyncio.sleep(wait)
                continue
            break
        if r is None or r.status_code != 200:
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
        return slug, best_yes_ask, depth_yes, best_no_ask, depth_no
    except Exception:
        return slug, None, 0, None, 0


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
