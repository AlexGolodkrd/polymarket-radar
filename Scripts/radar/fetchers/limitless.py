"""Limitless Exchange REST fetchers — orderbook + market metadata.

Extracted from arb_server.py in audit-28b cont 10 (29.05.2026). Owns:

    _lim_depth_usd(price, raw_size)
        Phase 9aa — heuristic normalise Limitless raw USDC size (6
        decimals) to USD notional. Phase 12b Bug 4 fixed the `>` →
        `>=` boundary that under-counted exact-$1M depths.

    _fetch_limitless_orderbook(slug)
        GET /markets/{slug}/orderbook. Returns
        (slug, best_yes_ask, depth_yes, best_no_ask, depth_no).
        WS-first when LIM WS connected + fresh book ≤2s; falls
        through to REST otherwise. Phase TS-5a WS-required mode
        skips REST entirely when WS connected + LIMITLESS_WS_REQUIRED=1.

    _fetch_limitless_market_meta(slug)
        GET /markets/{slug} → tokens.{yes,no}, venue.exchange (per-market
        verifyingContract for EIP-712 signing), isOther, volume. Cached
        forever per slug (tokens + venue immutable for deployed CTF
        conditions); volume re-fetched every LIM_META_REFRESH_S so HOT
        pool ordering reacts to liquidity changes.

Cache + session state remain on arb_server module level:
    lim_meta_cache + lim_meta_lock
    LIM_META_REFRESH_S + LIM_META_CACHE_MAX

This is the same model as Polymarket cont 9 — fork-free cache so scan_loop /
WS callbacks / classify_pools all read the same dict.
"""
from __future__ import annotations

import time


def _lim_depth_usd(price: float, raw_size: float) -> float:
    """Phase 9aa (29.04.2026) — convert Limitless raw orderbook `size` to USD.

    Limitless returns size as raw USDC amount (6 decimals). For a 100 USDC
    order at price 0.50, `size` comes back as 100_000_000. Naive
    price × raw_size = 50_000_000 → phantom "$50M depth" on the dashboard
    (G2/Astralis, US-GDP $1.84B operator-found bugs).

    Heuristic:
      raw_notional = price × raw_size
      raw >= 1_000_000  → raw USDC, divide by 1e6  (Phase 12b Bug 4: was
                          strict `>`, missed exact 1_000_000 edge case)
      else               → already USD
    Cap to $1M absolute so a future API change can't propagate absurd
    values into build_deal sizing.
    """
    if price <= 0 or raw_size <= 0:
        return 0.0
    raw = price * raw_size
    if raw >= 1_000_000:
        raw = raw / 1_000_000
    return min(raw, 1_000_000.0)


def _fetch_limitless_orderbook(slug: str) -> tuple:
    """GET /markets/{slug}/orderbook → returns (slug, best_yes_ask, depth_yes,
    best_no_ask, depth_no).

    Limitless's orderbook is per-outcome (no explicit YES/NO token ids in
    list response — request per slug). NO-side ask synthesised from
    YES-bid via no-arbitrage (yes_ask + no_ask >= 1).

    Performance: WS-first when LIM WS connected + book ≤2s old. Phase TS-5a
    WS-required mode (LIMITLESS_WS_REQUIRED=1) skips REST entirely when WS
    connected — caller's eval handles None gracefully.

    Phase 12b Bug 1: depth uses DEPTH_SLIPPAGE_TOLERANCE window (parity with
    Polymarket / Kalshi). Phase 19v21: NO-side notional uses
    `(1 - best_yes_bid) × size`, not `best_yes_bid × size` — buying NO is
    selling YES at YES bid, cost per NO share = 1 - yes_bid. Old code
    under-counted NO depth up to 9× on tight-margin bids.
    """
    from arb_server import (
        lim_ws_client, LIMITLESS_WS_REQUIRED,
        _SESS_LIM, _FETCH_TIMEOUT, LIMITLESS_API_BASE,
        DEPTH_SLIPPAGE_TOLERANCE,
    )

    # Prefer WS cache for hot slugs — falls back to REST if stale/missing.
    if lim_ws_client is not None:
        cached = lim_ws_client.get_book(slug)
        if cached and (time.time() - cached.get('ts', 0)) < 2.0:
            yes_ask = cached.get('best_yes_ask')
            yes_bid = cached.get('best_yes_bid')
            no_ask = ((1 - yes_bid)
                      if (yes_bid is not None and 0 < yes_bid < 1)
                      else None)
            return (slug, yes_ask, cached.get('depth_yes', 0),
                    no_ask, cached.get('depth_no', 0))
        # Phase TS-5a — WS-required mode: skip REST when WS connected.
        if LIMITLESS_WS_REQUIRED:
            try:
                ws_connected = bool(lim_ws_client.get_metrics().get('connected'))
            except Exception:
                ws_connected = False
            if ws_connected:
                return slug, None, 0, None, 0
    try:
        r = _SESS_LIM.get(
            f"{LIMITLESS_API_BASE}/markets/{slug}/orderbook",
            timeout=_FETCH_TIMEOUT,
        )
        if r.status_code != 200:
            return slug, None, 0, None, 0
        ob = r.json()
        asks = ob.get('asks') or []
        bids = ob.get('bids') or []
        # YES-side ask within DEPTH_SLIPPAGE_TOLERANCE of best.
        best_yes_ask: float | None = None
        depth_yes: float = 0
        if asks:
            try:
                asks_sorted = sorted(
                    asks, key=lambda a: float(a.get('price', 999)))
                best_yes_ask = float(asks_sorted[0].get('price', 0))
                cutoff = best_yes_ask + DEPTH_SLIPPAGE_TOLERANCE + 1e-9
                ladder_size = 0.0
                for level in asks_sorted:
                    p = float(level.get('price', 999))
                    if p > cutoff:
                        break
                    ladder_size += float(level.get('size', 0))
                depth_yes = _lim_depth_usd(best_yes_ask, ladder_size)
            except Exception:
                pass
        # NO-side ask synthesised from YES-bid (Phase 19v21 — use 1-bid notional)
        best_no_ask: float | None = None
        depth_no: float = 0
        if bids:
            try:
                bids_sorted = sorted(
                    bids, key=lambda b: float(b.get('price', 0)),
                    reverse=True)
                best_yes_bid = float(bids_sorted[0].get('price', 0))
                if 0 < best_yes_bid < 1:
                    best_no_ask = 1 - best_yes_bid
                    cutoff_bid = best_yes_bid - DEPTH_SLIPPAGE_TOLERANCE - 1e-9
                    ladder_size = 0.0
                    for level in bids_sorted:
                        p = float(level.get('price', 0))
                        if p < cutoff_bid:
                            break
                        ladder_size += float(level.get('size', 0))
                    depth_no = _lim_depth_usd(best_no_ask, ladder_size)
            except Exception:
                pass
        return slug, best_yes_ask, depth_yes, best_no_ask, depth_no
    except Exception:
        return slug, None, 0, None, 0


def _fetch_limitless_market_meta(slug: str) -> dict | None:
    """GET /markets/{slug} → tokens.{yes,no}, venue.exchange, isOther, volume.

    tokens (uint256 in EIP-712 Order) and venue.exchange (per-market
    verifyingContract in EIP-712 domain) are needed by atomic._build_leg
    to construct real signed Limitless orders — without them every dry-run
    leg posts `tokenId='0'` which the server rejects.

    TTL cache (LIM_META_REFRESH_S) — tokens + venue immutable for deployed
    CTF conditions, but volume changes so we refresh the whole record.

    Phase 9ss (29.04.2026) — pooled Session + tuple timeout. Without these,
    each call paid a fresh TLS handshake + hung connections sat past OS
    timeout in OpenSSL C-land. Limitless processing ballooned from 5s
    theoretical to 761s observed on 100 events.

    Phase 9uu — bounded cache size (LIM_META_CACHE_MAX); FIFO-evicts
    oldest 10% on overflow.
    """
    from arb_server import (
        _SESS_LIM, _FETCH_TIMEOUT, LIMITLESS_API_BASE,
        lim_meta_cache, lim_meta_lock,
        LIM_META_REFRESH_S, LIM_META_CACHE_MAX,
    )

    now = time.time()
    with lim_meta_lock:
        cached = lim_meta_cache.get(slug)
    if cached and (now - cached.get('fetched_at', 0)) < LIM_META_REFRESH_S:
        return cached
    try:
        r = _SESS_LIM.get(
            f"{LIMITLESS_API_BASE}/markets/{slug}",
            timeout=_FETCH_TIMEOUT,
        )
        if r.status_code != 200:
            return cached
        m = r.json()
        toks = m.get('tokens') or {}
        venue = m.get('venue') or {}
        rec = {
            'yes_token': str(toks.get('yes')) if toks.get('yes') is not None else None,
            'no_token': str(toks.get('no')) if toks.get('no') is not None else None,
            'verifying_contract': venue.get('exchange'),
            'volume': float(m.get('volume') or 0),
            'is_other': bool(m.get('isOther')),
            'fetched_at': now,
        }
        with lim_meta_lock:
            if len(lim_meta_cache) >= LIM_META_CACHE_MAX:
                evict_n = LIM_META_CACHE_MAX // 10
                oldest = sorted(lim_meta_cache.items(),
                                 key=lambda kv: kv[1].get('fetched_at', 0))[:evict_n]
                for k, _ in oldest:
                    lim_meta_cache.pop(k, None)
            lim_meta_cache[slug] = rec
        return rec
    except Exception:
        return cached
