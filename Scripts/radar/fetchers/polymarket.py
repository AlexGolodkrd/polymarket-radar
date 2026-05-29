"""Polymarket REST fetchers — orderbook + market metadata.

Extracted from arb_server.py in audit-28b cont 9 (29.05.2026). Owns:

    _fetch_clob(token_id)
        GET /book?token_id=...  Returns (token_id, best_ask, ask_depth,
        best_bid, bid_depth). Phase 10 + Task A — also returns bid side
        so we can synthesise NO ask from YES bid. Phase TS-5c WS-only
        mode: when POLYMARKET_WS_REQUIRED=1 AND ws_client.connected, the
        REST call is skipped — returns None pricing instead.

    _read_poly_fee_bps(market, side)
        Defensive multi-shape reader after the 31.03.2026 feeSchedule
        migration. Priority: feeSchedule.rate (×10000) → camelCase
        makerBaseFee/takerBaseFee → snake_case maker_base_fee/
        taker_base_fee. Returns 0 only when none parse.

    _fetch_poly_market_info(condition_id)
        GET /markets/{cid} → tick / min_order_size / fees / neg_risk /
        accepting_orders. 10-min TTL cache. Phase 19v3 short timeouts
        (1.0, 1.5) so chunk loop never blocks more than ~2s/cid even
        on cold Cloudflare tarpit.

    _batch_fetch_poly_market_info(condition_ids, ...)
        Phase 19v4 ThreadPoolExecutor fan-out with HARD 25s deadline.
        Replaces serial 280-310s/chunk on cold cache. Returns
        dict[cid] → info_dict or None (None triggers fallback to
        THETA_POLY default).

Cache state remains on arb_server.py module level:
    poly_market_info_cache + poly_market_info_lock
    POLY_MARKET_INFO_REFRESH_S + POLY_MARKET_INFO_CACHE_MAX

This avoids forking the cache between modules — fetchers read+write
the same dict that scan_loop / classify_pools / WS callbacks already
read elsewhere.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as _CFTimeoutError

log = logging.getLogger('arb_server')


def _read_poly_fee_bps(market: dict, side: str) -> float:
    """Read Polymarket fee (basis points) for 'maker' or 'taker'.

    Priority order:
      1. `feeSchedule.rate` × 10000 (post-31.03.2026 source of truth).
         Respects `feeSchedule.takerOnly` (makers pay 0).
      2. camelCase `{side}BaseFee` (gamma /events response).
      3. snake_case `{side}_base_fee` (CLOB /markets/{cid}).

    Returns 0.0 only when nothing parseable found — a typo or future
    rename can't silently disable fee subtraction.
    """
    if not isinstance(market, dict):
        return 0.0
    fs = market.get('feeSchedule') or market.get('fee_schedule')
    if isinstance(fs, dict):
        rate = fs.get('rate')
        if rate is not None:
            try:
                rate_f = float(rate)
            except (TypeError, ValueError):
                rate_f = None
            if rate_f is not None:
                taker_only = bool(fs.get('takerOnly') or fs.get('taker_only'))
                if side == 'maker' and taker_only:
                    return 0.0
                return rate_f * 10000.0
    cc_key = f'{side}BaseFee'
    v = market.get(cc_key)
    if v is not None:
        try:
            return float(v)
        except (TypeError, ValueError):
            pass
    sc_key = f'{side}_base_fee'
    v = market.get(sc_key)
    if v is not None:
        try:
            return float(v)
        except (TypeError, ValueError):
            pass
    return 0.0


def _fetch_clob(token_id: str) -> tuple:
    """GET /book?token_id=... — returns (token_id, best_ask, ask_depth,
    best_bid, bid_depth).

    Phase 10 + Task A: bid side returned too so eval can synth NO from
    YES bid when real NO orderbook is empty. Phase TS-5c: WS-required
    mode skips REST entirely when WS connected (cache miss returns
    Nones; main scan does a WS-first sweep before calling here).
    """
    from arb_server import (
        POLYMARKET_WS_REQUIRED, ws_client,
        _SESS_POLY, _FETCH_TIMEOUT,
        _top_of_book_depth_usd, DEPTH_SLIPPAGE_TOLERANCE,
    )

    if POLYMARKET_WS_REQUIRED and ws_client is not None:
        try:
            ws_connected = bool(ws_client.get_metrics().get('connected'))
        except Exception:
            ws_connected = False
        if ws_connected:
            try:
                cached = ws_client.get_book(token_id)
            except Exception:
                cached = None
            if cached and cached.get('best_ask') and 0 < cached['best_ask'] < 1:
                ask = cached['best_ask']
                depth = cached.get('depth') or 0.0
                bid = cached.get('best_bid')
                bid_depth = cached.get('bid_depth') or 0.0
                return token_id, ask, depth, bid, bid_depth
            return token_id, None, 0.0, None, 0.0
    try:
        r = _SESS_POLY.get(
            f"https://clob.polymarket.com/book?token_id={token_id}",
            timeout=_FETCH_TIMEOUT,
        )
        body = r.json() or {}
        asks = body.get('asks', [])
        bids = body.get('bids', [])
        # Top-of-book depth with DEPTH_SLIPPAGE_TOLERANCE window (Phase 11 F).
        best_ask, ask_depth = _top_of_book_depth_usd(
            asks, slippage_tolerance=DEPTH_SLIPPAGE_TOLERANCE)
        # Bid side: descending sort, count bids within tolerance of best.
        best_bid: float | None = None
        bid_depth: float = 0.0
        parsed_bids: list[tuple[float, float]] = []
        for a in bids or []:
            try:
                if isinstance(a, dict):
                    p = float(a.get('price', 0))
                    s = float(a.get('size', 0))
                else:
                    p = float(a[0])
                    s = float(a[1])
                if p > 0 and s > 0:
                    parsed_bids.append((p, s))
            except Exception:
                continue
        if parsed_bids:
            parsed_bids.sort(key=lambda x: -x[0])
            best_bid = parsed_bids[0][0]
            cutoff = best_bid - DEPTH_SLIPPAGE_TOLERANCE - 1e-9
            for p, s in parsed_bids:
                if p < cutoff:
                    break
                bid_depth += p * s
        return token_id, best_ask, ask_depth, best_bid, bid_depth
    except Exception:
        return token_id, None, 0.0, None, 0.0


def _fetch_poly_market_info(condition_id: str) -> dict | None:
    """GET /markets/{cid} — tick / min_order_size / fees / neg_risk +
    pre-fire-gate flags (accepting_orders, enable_order_book,
    accepting_order_timestamp, seconds_delay) + neg_risk identifiers.

    10-min TTL cache (POLY_MARKET_INFO_REFRESH_S). Phase 19v3 short
    timeouts (1s/1.5s) so chunk loop never blocks more than ~2s/cid on
    cold Cloudflare tarpit. Returns cached stale data when API fails
    rather than None — stale better than nothing for threshold math.
    """
    from arb_server import (
        _SESS_POLY, _safe_int_ts,
        poly_market_info_cache, poly_market_info_lock,
        POLY_MARKET_INFO_REFRESH_S, POLY_MARKET_INFO_CACHE_MAX,
    )

    if not condition_id:
        return None
    now = time.time()
    with poly_market_info_lock:
        cached = poly_market_info_cache.get(condition_id)
    if cached and (now - cached.get('fetched_at', 0)) < POLY_MARKET_INFO_REFRESH_S:
        return cached
    try:
        r = _SESS_POLY.get(
            f"https://clob.polymarket.com/markets/{condition_id}",
            timeout=(1.0, 1.5),
        )
        if r.status_code != 200:
            return cached
        m = r.json() or {}
        rec = {
            'condition_id': condition_id,
            'tick_size': float(m.get('minimum_tick_size') or 0.01),
            'min_order_size': float(m.get('minimum_order_size') or 1),
            # Phase audit-3 — feeSchedule is the authoritative source after
            # Polymarket's 31.03.2026 fee model change. See
            # .claude/skills/polymarket-fee-schedule for live-probe results.
            'maker_fee_bps': _read_poly_fee_bps(m, 'maker'),
            'taker_fee_bps': _read_poly_fee_bps(m, 'taker'),
            'neg_risk': bool(m.get('neg_risk')),
            'accepting_orders': bool(m.get('accepting_orders')),
            'enable_order_book': bool(m.get('enable_order_book')),
            'closed': bool(m.get('closed')),
            'archived': bool(m.get('archived')),
            'active': (bool(m.get('active'))
                       if m.get('active') is not None else True),
            'accepting_order_timestamp': _safe_int_ts(
                m.get('accepting_order_timestamp')),
            'seconds_delay': int(m.get('seconds_delay') or 0),
            'neg_risk_market_id': m.get('neg_risk_market_id'),
            'neg_risk_request_id': m.get('neg_risk_request_id'),
            'rewards': m.get('rewards') or {},
            'fetched_at': now,
        }
        with poly_market_info_lock:
            # Phase 9uu — bound cache size; evict oldest 10% on overflow.
            if len(poly_market_info_cache) >= POLY_MARKET_INFO_CACHE_MAX:
                evict_n = POLY_MARKET_INFO_CACHE_MAX // 10
                oldest = sorted(poly_market_info_cache.items(),
                                key=lambda kv: kv[1].get('fetched_at', 0))[:evict_n]
                for k, _ in oldest:
                    poly_market_info_cache.pop(k, None)
            poly_market_info_cache[condition_id] = rec
        return rec
    except Exception:
        return cached


def _batch_fetch_poly_market_info(condition_ids, max_concurrent: int = 20,
                                    deadline_s: float = 25.0) -> dict:
    """Phase 19v4 (02.05.2026) — kill the cold-cache hang in classify_pools.

    Before: serial `_fetch_poly_market_info(cid)` × 20 cids × 14s on cold
    Cloudflare tarpit = 280-310s/chunk × N chunks = scan-loop frozen for
    30+ minutes.

    After: ThreadPoolExecutor fan-out (20 threads) with HARD wall-clock
    deadline. Worst-case all-misses returns at deadline; not-yet-completed
    cids resolve to None and the caller falls back to THETA_POLY default
    (conservative — over-rejects rather than over-fires).

    `cancel_futures=True` on shutdown so hung workers don't leak past the
    deadline (sockets eventually close via OS TIMEOUT).
    """
    cids = [c for c in condition_ids if c]
    if not cids:
        return {}
    out: dict = {cid: None for cid in cids}
    deadline = time.time() + deadline_s
    pool = ThreadPoolExecutor(max_workers=min(max_concurrent, len(cids)),
                                thread_name_prefix='poly-info')
    try:
        future_to_cid = {pool.submit(_fetch_poly_market_info, cid): cid
                         for cid in cids}
        try:
            for fut in as_completed(future_to_cid, timeout=deadline_s):
                cid = future_to_cid[fut]
                try:
                    out[cid] = fut.result(timeout=0.5)
                except Exception:
                    out[cid] = None
                if time.time() > deadline:
                    break
        except _CFTimeoutError:
            pass
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return out
