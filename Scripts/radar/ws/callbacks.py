"""Orderbook WebSocket push callbacks — single-candidate re-evaluation.

Extracted from arb_server.py in audit-28b cont 12 (29.05.2026). Both
callbacks fire from a WS client thread (websocket-client) when a fresh
orderbook update lands; they re-evaluate the single affected candidate
and merge results into scan_data atomically.

Owns:
    on_ws_update(token_id)
        Polymarket WS push handler. Look up the candidate via
        `poly_token_index` (O(1)), pull fresh book for both YES + NO
        tokens, run the 3-structure evaluator, merge into scan_data,
        and auto-dry-fire newcomer arbs.

    on_lim_ws_update(slug)
        Limitless WS push handler. Phase 9d — same role for Limitless;
        reaction latency ~250ms vs the 5s micro-loop polling. negRisk
        groups: pull books for ALL child slugs of the parent event.

Lazy imports throughout — these callbacks run in WS threads and the
arb_server module is fully loaded by the time WS subscribes, so there's
no cyclic-load concern. The lazy-import pattern preserves
`mock.patch.object(arb_server, 'ws_client', X)` test contracts.
"""
from __future__ import annotations

import time


def on_ws_update(token_id: str) -> None:
    """Polymarket WS pushed an orderbook update for `token_id`. Re-evaluate
    the candidate across all 3 arb structures and inject/replace deals
    in scan_data atomically.
    """
    from arb_server import (
        ws_client,
        poly_token_index, poly_token_index_lock,
        poly_clob_cache, poly_clob_cache_lock,
        scan_lock, scan_data,
        _fired_arb_keys, _eval_poly_one, _maybe_dry_fire,
    )

    if ws_client is None:
        return
    with poly_token_index_lock:
        cand = poly_token_index.get(token_id)
    if cand is None:
        return
    with poly_clob_cache_lock:
        clob_snapshot = dict(poly_clob_cache)
    ws_books: dict = {}
    # Pull books for BOTH YES and NO tokens of this candidate
    _ev, rough, _ = cand
    for o in rough:
        for tid in (o.get('token_id_yes') or o.get('token_id'),
                     o.get('token_id_no')):
            if not tid:
                continue
            b = ws_client.get_book(tid)
            if b:
                ws_books[tid] = b
    new_deals = _eval_poly_one(cand, clob_res=clob_snapshot, ws_books=ws_books)
    base_title = cand[0].get('title', '?')
    with scan_lock:
        deals = list(scan_data.get('deals', []))
        # Drop existing deals for this event (any structure) — match by
        # title prefix since structure suffixes may differ.
        deals = [d for d in deals
                 if not (d['title'] == base_title
                         or d['title'].startswith(base_title + ' (')
                         or d['title'].startswith(base_title + ' — '))]
        for d in new_deals:
            if not d.get('is_quarantine'):
                deals.append(d)
        deals.sort(key=lambda d: d['net'], reverse=True)
        scan_data['deals'] = deals
        if 'stats' in scan_data and isinstance(scan_data['stats'], dict):
            scan_data['stats']['arb_found'] = len(deals)
    # WS path produced new deals — auto-dry-fire any newcomers (Phase 2).
    # Phase 12 Task D — track WS-triggered fires separately from
    # scan-triggered for /api/paper_stats observability.
    fired_count_before = len(_fired_arb_keys)
    _maybe_dry_fire(new_deals)
    fired_count_after = len(_fired_arb_keys)
    ws_fired = max(0, fired_count_after - fired_count_before)
    if ws_fired > 0:
        with scan_lock:
            stats = scan_data.setdefault('stats', {})
            stats['ws_triggered_fires'] = (
                stats.get('ws_triggered_fires', 0) + ws_fired)


def on_lim_ws_update(slug: str) -> None:
    """Limitless WS pushed an orderbook update for `slug`.

    Phase 9d (28.04.2026): real push-driven re-evaluation. Lookup the
    parent event via lim_slug_index (O(1)), pull current WS orderbook
    for every slug under that event, run eval_limitless on the single
    event, and merge new deals into scan_data immediately — same
    pattern as Polymarket's on_ws_update.

    Why this matters: Limitless markets are mostly 30-minute crypto
    oracles where prices move fast in the last minutes before
    resolution. The 5s micro-loop polling we relied on before would
    miss most of those arb windows. Push-driven re-eval brings reaction
    latency to ~250ms (coalesce tick) — parity with Polymarket.
    """
    from arb_server import (
        lim_ws_client,
        lim_slug_index, lim_slug_index_lock,
        res_cache_lock, lim_res_cache,
        scan_lock, scan_data,
        _maybe_dry_fire,
    )
    from radar.eval.limitless import eval_limitless

    if lim_ws_client is None:
        return
    with lim_slug_index_lock:
        ev = lim_slug_index.get(slug)
    if ev is None:
        return

    # Build a fresh per-slug orderbook map for this single event from the
    # WS cache. Falls back to the last REST snapshot in lim_res_cache for
    # slugs the WS hasn't pushed yet (newly added negRisk children).
    children = ev.get('markets') or []
    slugs: list[str] = []
    if children:
        for c in children:
            s = c.get('slug') or c.get('address')
            if s:
                slugs.append(s)
    else:
        s = ev.get('slug') or ev.get('address')
        if s:
            slugs.append(s)

    fresh_lim_res: dict = {}
    with res_cache_lock:
        cached_snapshot = dict(lim_res_cache)
    for s in slugs:
        cached = lim_ws_client.get_book(s) if lim_ws_client else None
        if cached and (time.time() - cached.get('ts', 0)) < 5.0:
            yes_ask = cached.get('best_yes_ask')
            yes_bid = cached.get('best_yes_bid')
            no_ask = ((1 - yes_bid)
                      if (yes_bid is not None and 0 < yes_bid < 1)
                      else None)
            fresh_lim_res[s] = (
                yes_ask, cached.get('depth_yes', 0),
                no_ask, cached.get('depth_no', 0),
            )
        elif s in cached_snapshot:
            fresh_lim_res[s] = cached_snapshot[s]

    if not fresh_lim_res:
        return  # nothing to evaluate yet

    new_deals = eval_limitless([ev], fresh_lim_res)

    base_title = ev.get('title') or ev.get('proxyTitle') or '?'
    with scan_lock:
        deals = list(scan_data.get('deals', []))
        # Drop existing Limitless deals for this event's title (parent
        # or per-market suffix). Same pattern as on_ws_update.
        deals = [
            d for d in deals
            if not (d.get('platform') == 'Limitless'
                    and (d['title'] == base_title
                         or d['title'].startswith(base_title + ' (')
                         or d['title'].startswith(base_title + ' — ')))
        ]
        for d in new_deals:
            if not d.get('is_quarantine'):
                deals.append(d)
        deals.sort(key=lambda d: d['net'], reverse=True)
        scan_data['deals'] = deals
        if 'stats' in scan_data and isinstance(scan_data['stats'], dict):
            scan_data['stats']['arb_found'] = len(deals)
    # Auto-dry-fire any newcomer arbs (Phase 2 — same gate as Polymarket).
    _maybe_dry_fire(new_deals)
