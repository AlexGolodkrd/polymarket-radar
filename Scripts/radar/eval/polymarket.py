"""Polymarket arb evaluator — A (ALL_YES) / B (ALL_NO) / C (YES_NO_PAIR).

Extracted from arb_server.py in audit-28b cont 6 (28.05.2026). Owns:
    compute_poly_threshold(taker_fee_bps, n_legs)
        Per-market break-even threshold given the actual taker fee on
        this market. Dynamic since the V2 cutover — different markets
        have different fees (0% / 1% / 2.5% / 4%) and a static THRESH
        either rejected free-fee arbs or accepted high-fee losers.

    is_threshold_series(parent_title, child_titles, child_slugs)
        Detect events with overlapping threshold outcomes (e.g.
        "BTC above $X") where ALL_YES / ALL_NO math is INVALID. Three
        signal layers: parent regex → child-title prefix → child-slug
        comparator. Phase 19v30 added the slug-based layer after the
        SOL "1076% ROI" phantom slipped through the title-only check.

    _poly_per_market(rough, clob_res, ws_books)
        Per-market YES/NO price+liquidity snapshot. Source priority:
        WS book → REST clob (ask, then synthetic-from-yes-bid) →
        implied. Phase 10 added the synthetic-NO path so sport binaries
        with only YES asks still produce a NO price.

    _attach_poly_v2_meta(deal, rough, no_only)
        V2 metadata (tick_size / min_order_size / neg_risk / taker fee
        + per-market accepting_orders flags) attached to every leg.
        Required by atomic.build_poly_order for tick-snap + neg-risk
        domain selection.

    _eval_poly_structures(cand, clob_res, ws_books)
        The actual 3-structure evaluator. Returns [] when no structure
        crosses its dynamic threshold.

    eval_poly(cands, clob_res)
        Batch wrapper — runs `_eval_poly_structures` over every
        candidate and concatenates results.

Lazy imports: heavy deps (`_fetch_poly_market_info`, constants like
`ENABLE_STRUCT_A`) are imported inside functions to avoid a cyclic
load with arb_server. Operator-tuned env constants (ENABLE_STRUCT_*,
QUALITY_TIGHT_*, etc.) are read at call time so a `mock.patch.object`
on arb_server still takes effect for in-process tests.
"""
from __future__ import annotations

import os
import re
from typing import Any, Iterable


# Phase audit-29.05 — phantom-arb sanity floor for Polymarket YES_NO_PAIR.
# Same rationale as LIMITLESS_REALISTIC_SUM_FLOOR — a single market's
# YES_ask + NO_ask on a liquid CLOB must respect MM overround (sum ≥ 1.0).
# Sum < POLY_REALISTIC_SUM_FLOOR is a tell-tale of stale orderbook on the
# implied/synthetic side. Multi-outcome ALL_YES / ALL_NO (structures A/B)
# are NOT affected — those have legitimate arb math at any sum < threshold.
# Default 0.80: more permissive than Limitless (Polymarket runs lower
# overround on some V2 markets, and Phase 10 synthetic clob_synthetic arbs
# can legitimately produce sum 0.85-0.95 on sport binaries with one-sided
# books). Sum < 0.80 is mathematically near-impossible on a functioning
# CLOB and a strong stale-data signal.
POLY_REALISTIC_SUM_FLOOR: float = float(
    os.environ.get('POLY_REALISTIC_SUM_FLOOR', '0.80'))


# ── Threshold-series detection ──────────────────────────────────────
THRESHOLD_SERIES_RE = re.compile(
    r'(\b(above|below|over|under|more\s+than|less\s+than|greater\s+than|'
    r'at\s+least|at\s+most|>|>=|<|<=|≥|≤)\s+'
    r'(_+|\?+|\$?[\d,.]+|\w+\s*[\d,.]+|N|X)|'
    r'\b(выше|ниже|больше|меньше)\s+(чем|_+|\?+|\d))',
    re.IGNORECASE,
)


def is_threshold_series(parent_title: str, child_titles: Iterable[str] | None = None,
                         child_slugs: Iterable[str] | None = None) -> bool:
    """True iff this multi-outcome event is a series of overlapping threshold
    markets — for which ALL_YES / ALL_NO arb math is INVALID.

    Three signal layers:
      1. Parent title regex (e.g. "BTC above ___").
      2. Every child title starts with the same comparator prefix
         ("Above 65M", "Above 70M", ...) — also threshold series.
      3. Phase 19v30 (06.05.2026): every child slug carries the same
         `*-above-*` / `*-below-*` / `*-over-*` / `*-under-*` segment.
         Catches Limitless-style events where parent title is generic
         ("SOL price on May 6?") and child titles are bare values
         ("$887.53") — but slugs preserve "sol-above-dollar88753-...".
    """
    if not parent_title:
        return False
    if THRESHOLD_SERIES_RE.search(parent_title):
        return True
    # Secondary: every child shares an "above N" / "below N" prefix.
    if child_titles:
        child_titles = list(child_titles)
        if len(child_titles) >= 3:
            prefixes: list[str] | None = []
            for t in child_titles:
                m = re.match(r'^\s*(above|below|over|under|>|<|≥|≤)\b',
                             t or '', re.IGNORECASE)
                if not m:
                    prefixes = None
                    break
                prefixes.append(m.group(1).lower())
            if prefixes and len(set(prefixes)) == 1:
                return True
    # Tertiary: slug-based comparator detection.
    if child_slugs:
        child_slugs = list(child_slugs)
        if len(child_slugs) >= 2:
            comp: list[str] | None = []
            for sl in child_slugs:
                if not sl:
                    comp = None
                    break
                m = re.search(
                    r'-(above|below|over|under|gt|lt|ge|le|geq|leq)-',
                    sl, re.IGNORECASE,
                )
                if not m:
                    comp = None
                    break
                comp.append(m.group(1).lower())
            if comp and len(set(comp)) == 1:
                return True
    return False


# ── Dynamic break-even threshold ─────────────────────────────────────
def compute_poly_threshold(taker_fee_bps: float, n_legs: int | None = None) -> float:
    """Return the break-even threshold for a Polymarket arb at this
    market's actual taker fee.

    n_legs reserved for future tuning (more legs = more individual
    slippage paths) — currently unused; POLY_SLIPPAGE_RESERVE is already
    a conservative arb-level number.

    Examples (Phase 9l constants):
        0% fee (0 bps)    → 1 - 0.008 = 0.992
        1% fee (100 bps)  → 1 - 0.018 = 0.982
        2.5% fee (250)    → 1 - 0.033 = 0.967
        4% fee (400)      → 1 - 0.048 = 0.952
        6% fee (600)      → 1 - 0.068 → clipped to floor 0.948
    """
    from arb_server import (
        POLY_SLIPPAGE_RESERVE, POLY_SAFETY_BUFFER,
        POLY_DYNAMIC_THRESH_FLOOR, POLY_DYNAMIC_THRESH_CAP,
    )
    theta = (taker_fee_bps or 0) / 10000.0
    raw = 1.0 - (theta + POLY_SLIPPAGE_RESERVE + POLY_SAFETY_BUFFER)
    if raw < POLY_DYNAMIC_THRESH_FLOOR:
        return POLY_DYNAMIC_THRESH_FLOOR
    if raw > POLY_DYNAMIC_THRESH_CAP:
        return POLY_DYNAMIC_THRESH_CAP
    return raw


# ── Per-market YES/NO snapshot ───────────────────────────────────────
def _poly_per_market(rough: list[dict], clob_res: dict | None,
                      ws_books: dict | None = None) -> list[dict]:
    """Per-market YES/NO price+liquidity snapshot. Used by the 3-structure
    evaluator AND by NEAR-pool classification.

    Source priority for YES side: WS book → REST clob ask → implied.
    Source priority for NO side: WS book → REST clob ask → synthetic
    from YES bid (Phase 10 — sport binaries often quote only YES asks,
    NO is implied 1 - YES_bid; depth transfers 1:1 because YES+NO=$1)
    → implied.
    """
    ws_books = ws_books or {}
    clob_res = clob_res or {}
    out: list[dict] = []
    for o in rough:
        m = o['m']
        name = m.get('question', m.get('groupItemTitle', '?'))
        yes_tid = o.get('token_id_yes') or o.get('token_id')
        no_tid = o.get('token_id_no')
        # YES side
        yes_price = o['implied']
        yes_liq = float(m.get('liquidity', 0) or 0)
        yes_src = 'implied'
        yes_clob = clob_res.get(yes_tid) if yes_tid else None
        if yes_tid:
            b = ws_books.get(yes_tid)
            if b and b.get('best_ask') and 0 < b['best_ask'] < 1:
                yes_price = b['best_ask']
                yes_liq = b.get('depth') or yes_liq
                yes_src = 'ws'
            elif yes_clob is not None:
                # Phase 10 Task A: tuple is now (ask, ask_depth, bid, bid_depth).
                ask = yes_clob[0] if len(yes_clob) >= 1 else None
                depth = yes_clob[1] if len(yes_clob) >= 2 else 0
                if ask and 0 < ask < 1:
                    yes_price = ask
                    yes_liq = depth or yes_liq
                    yes_src = 'clob_ask'
        # NO side — real orderbook first, then synthetic from YES bid.
        no_price = (1 - o['implied']) if 0 < o['implied'] < 1 else None
        no_liq: float = 0
        no_src = 'implied'
        if no_tid:
            b = ws_books.get(no_tid)
            if b and b.get('best_ask') and 0 < b['best_ask'] < 1:
                no_price = b['best_ask']
                no_liq = b.get('depth') or no_liq
                no_src = 'ws'
            elif no_tid in clob_res:
                no_clob = clob_res[no_tid]
                ask = no_clob[0] if len(no_clob) >= 1 else None
                depth = no_clob[1] if len(no_clob) >= 2 else 0
                if ask and 0 < ask < 1:
                    no_price = ask
                    no_liq = depth or no_liq
                    no_src = 'clob_ask'
        # Phase 10 Task A — synthetic NO when real book is empty.
        if no_src == 'implied' and yes_clob is not None and len(yes_clob) >= 4:
            yes_bid = yes_clob[2]
            yes_bid_depth = yes_clob[3] or 0
            if yes_bid and 0 < yes_bid < 1:
                synth_no_ask = 1.0 - yes_bid
                if 0 < synth_no_ask < 1 and yes_bid_depth > 0:
                    no_price = synth_no_ask
                    no_liq = yes_bid_depth
                    no_src = 'clob_synthetic'
        out.append({
            'name': name, 'volume': float(m.get('volume', 0) or 0),
            'yes_price': yes_price, 'yes_liq': yes_liq, 'yes_src': yes_src,
            'no_price': no_price, 'no_liq': no_liq, 'no_src': no_src,
        })
    return out


# ── V2 metadata attachment ───────────────────────────────────────────
def _attach_poly_v2_meta(deal: dict, rough: list, no_only: bool = False) -> None:
    """Attach V2 per-market metadata (tick_size / min_order_size /
    neg_risk / condition_id / taker_fee_bps / accepting_orders flags)
    to each leg entry. Required by atomic.build_poly_order for tick-snap
    + min-order-size validation + neg-risk domain selection.

    Maps leg index → rough[i]; build_deal preserves outcome order, so
    a 1:1 mapping works for ALL_YES / ALL_NO. For YES_NO_PAIR the
    caller passes a single-element `rough` matching the chosen market.
    """
    from arb_server import _fetch_poly_market_info
    entries = deal.get('entries') or []
    for i, e in enumerate(entries):
        idx = 0 if len(rough) == 1 else min(i, len(rough) - 1)
        m = rough[idx]['m'] if idx < len(rough) else None
        if not m:
            continue
        cid = m.get('conditionId') or m.get('condition_id')
        info = _fetch_poly_market_info(cid) if cid else None
        if info:
            e['condition_id'] = cid
            e['tick_size'] = info['tick_size']
            e['min_order_size'] = info['min_order_size']
            e['neg_risk'] = info['neg_risk']
            e['taker_fee_bps'] = info['taker_fee_bps']
            # Phase 9m: pre-fire-gate status flags. atomic checks these
            # RIGHT before POST and aborts the leg if the market closed
            # or disabled between scan and fire.
            e['accepting_orders'] = info.get('accepting_orders', True)
            e['enable_order_book'] = info.get('enable_order_book', True)
            e['accepting_order_timestamp'] = info.get('accepting_order_timestamp', 0)
            e['seconds_delay'] = info.get('seconds_delay', 0)
            e['neg_risk_market_id'] = info.get('neg_risk_market_id')


# ── 3-structure evaluator ────────────────────────────────────────────
def _eval_poly_structures(cand: tuple, clob_res: dict | None = None,
                           ws_books: dict | None = None) -> list[dict]:
    """Returns a list of deals — one per arb structure (A/B/C) that crosses
    its dynamic threshold. Empty list if none.

    Phase 9g (28.04.2026) — coverage rule: ALL_YES and ALL_NO must price
    EVERY outcome of the event. If even one outcome was dropped during
    filter (no outcomePrices, no clob token, etc.) we silently
    over-counted before — see Limitless EPL Leeds-vs-Burnley case.
    Standalone YES_NO_PAIR is still safe per-market.
    """
    # Lazy imports — late-binding so `mock.patch.object` on arb_server
    # constants takes effect at call time.
    from arb_server import (
        _fetch_poly_market_info, THETA_POLY,
        ENABLE_STRUCT_A, ENABLE_STRUCT_B, ENABLE_STRUCT_C,
        QUALITY_TIGHT_CUTOFF_CENTS, QUALITY_TIGHT_MIN_LIQ, QUALITY_TIGHT_MAX_SLIP,
    )
    from radar.build_deal import build_deal

    ev, rough, is_q = cand
    per_market = _poly_per_market(rough, clob_res, ws_books)
    # Phase 9w: single-binary path needs ≥1 leg (only structure C runs).
    # Multi-outcome path needs ≥2.
    is_single_binary = bool(ev.get('_single_binary'))
    if is_single_binary:
        if len(per_market) < 1:
            return []
    elif len(per_market) < 2:
        return []
    total_outcomes_on_event = len(ev.get('markets') or []) or len(per_market)
    full_coverage = (len(per_market) == total_outcomes_on_event)

    # Phase 9j: pull V2 dynamic per-market fee/tick/min_size. We use the
    # WORST (highest) taker fee across this event's markets — pessimistic
    # ranking so net is never overestimated.
    market_infos: list[dict] = []
    for o in rough:
        cid = o['m'].get('conditionId') or o['m'].get('condition_id')
        if cid:
            info = _fetch_poly_market_info(cid)
            if info:
                market_infos.append(info)
    if market_infos:
        max_taker_fee_bps = max(i['taker_fee_bps'] for i in market_infos)
        effective_theta = max_taker_fee_bps / 10000.0
    else:
        # No info available — fall back to conservative default.
        effective_theta = THETA_POLY
        max_taker_fee_bps = THETA_POLY * 10000

    dyn_threshold = compute_poly_threshold(max_taker_fee_bps)

    title = ev.get('title', '?')
    end_date = ev.get('endDate')
    deals: list[dict] = []

    def _quality_ok(d: dict) -> bool:
        """Phase 9gg quality gate: reject tight-margin deals with too little
        liquidity or too much slippage. Env-tunable via QUALITY_TIGHT_*."""
        if d['total_cents'] >= QUALITY_TIGHT_CUTOFF_CENTS:
            if (d['min_liq'] < QUALITY_TIGHT_MIN_LIQ
                    or d['slip_pct'] >= QUALITY_TIGHT_MAX_SLIP):
                return False
        return True

    def _attach(d: dict | None) -> dict | None:
        """Attach per-deal metadata (end_date so analytics history can
        show when capital becomes free, is_quarantine flag, etc.)."""
        if d:
            d['end_date'] = end_date
        return d

    # Phase 9o: threshold-series guard. Overlapping outcomes break ALL_YES/NO.
    child_titles_for_threshold = [p['name'] for p in per_market]
    child_slugs_for_threshold = [
        (o.get('m') or {}).get('slug')
        or (o.get('m') or {}).get('marketSlug')
        for o in rough
    ]
    threshold_series = is_threshold_series(
        title, child_titles_for_threshold, child_slugs_for_threshold)

    # ── A. ALL_YES ──────────────────────────────────────────────────
    yes_out = [{'name': p['name'], 'price': p['yes_price'],
                'liquidity': p['yes_liq'], 'source': p['yes_src'],
                'volume': p['volume']} for p in per_market]
    total_yes = sum(o['price'] for o in yes_out)
    if (ENABLE_STRUCT_A and not is_single_binary and full_coverage
            and total_yes < dyn_threshold and not threshold_series
            and not is_q):
        d = build_deal(title, 'Polymarket', yes_out, total_yes,
                       effective_theta, dyn_threshold)
        if d:
            d['arb_structure'] = 'all_yes'
            _attach(d)
            _attach_poly_v2_meta(d, rough)
            if _quality_ok(d):
                deals.append(d)

    # ── B. ALL_NO (N>=3, multi-outcome) ─────────────────────────────
    no_raw = [p for p in per_market
              if p['no_price'] is not None and 0 < p['no_price'] < 1]
    N = len(no_raw)
    if (ENABLE_STRUCT_B and N >= 3 and N == total_outcomes_on_event
            and not threshold_series and not is_q):
        no_out = [{'name': f"NO {p['name']}", 'price': p['no_price'],
                   'liquidity': p['no_liq'], 'source': p['no_src'],
                   'volume': p['volume']} for p in no_raw]
        total_no = sum(o['price'] for o in no_out)
        no_threshold = (N - 1) * dyn_threshold
        if total_no < no_threshold:
            # Phase 9i: payout_target=N-1 — N-1 of the N legs win (one
            # of the outcomes hits → its NO loses → the other N-1 NOs pay $1 each).
            d = build_deal(title + ' (ALL_NO)', 'Polymarket', no_out,
                           total_no, effective_theta, no_threshold,
                           payout_target=float(N - 1))
            if d:
                d['arb_structure'] = 'all_no'
                d['payout_target'] = N - 1
                _attach(d)
                _attach_poly_v2_meta(d, rough, no_only=True)
                deals.append(d)

    # ── C. YES_NO_PAIR (per-market) ─────────────────────────────────
    if not ENABLE_STRUCT_C:
        return deals
    if is_q:
        return deals  # "Other"-outcome event — never produce C deals
    for idx, p in enumerate(per_market):
        if p['no_price'] is None or not (0 < p['no_price'] < 1):
            continue
        if not (0 < p['yes_price'] < 1):
            continue
        leg_theta = effective_theta
        leg_threshold = dyn_threshold
        if idx < len(market_infos):
            leg_fee_bps = market_infos[idx]['taker_fee_bps']
            leg_theta = leg_fee_bps / 10000.0
            leg_threshold = compute_poly_threshold(leg_fee_bps)
        pair_total = p['yes_price'] + p['no_price']
        if pair_total >= leg_threshold:
            continue
        # Phase audit-29.05 — realistic-sum floor on single market YES_NO_PAIR.
        # Catches stale orderbook on the implied/synthetic side (operator's
        # phantom-arb report 29.05.2026). ALL_YES / ALL_NO (above) are NOT
        # affected — those have valid arb math at any sum < threshold.
        if pair_total < POLY_REALISTIC_SUM_FLOOR:
            continue
        pair_out = [
            {'name': f"YES {p['name']}", 'price': p['yes_price'],
             'liquidity': p['yes_liq'], 'source': p['yes_src'], 'volume': p['volume']},
            {'name': f"NO {p['name']}", 'price': p['no_price'],
             'liquidity': p['no_liq'], 'source': p['no_src'], 'volume': p['volume']},
        ]
        d = build_deal(f"{title} — {p['name']}", 'Polymarket', pair_out,
                       pair_total, leg_theta, leg_threshold)
        if d:
            d['arb_structure'] = 'yes_no_pair'
            _attach(d)
            # Pick the single rough entry matching this market by name —
            # falls back to passing the full rough list if no match
            # (legacy behaviour from inline path).
            matched_rough = [
                r for r in rough
                if (r['m'].get('question') == p['name']
                    or r['m'].get('groupItemTitle') == p['name'])
            ]
            _attach_poly_v2_meta(d, matched_rough if matched_rough else rough)
            if _quality_ok(d):
                deals.append(d)
    return deals


def eval_poly(cands: Iterable[tuple], clob_res: dict | None) -> list[dict]:
    """Batch evaluator — runs `_eval_poly_structures` over every candidate
    and concatenates results. Returns deals across all 3 arb structures
    (A=ALL_YES, B=ALL_NO, C=YES_NO_PAIR)."""
    deals: list[dict] = []
    for cand in cands:
        deals.extend(_eval_poly_structures(cand, clob_res=clob_res))
    return deals
