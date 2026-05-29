"""Limitless Exchange arb evaluator — A/B/C structures.

Extracted from arb_server.py in audit-28b cont 7 (28.05.2026).

Limitless is a Polymarket-like CLOB on Base L2 (added 28.04.2026). Same
EIP-712 architecture, no KYC, no platform fee. Two event shapes from
/markets/active:
    - Binary market: {slug, title, deadline, prices:[yes,no], liquidity}
    - NegRisk group: {slug, title, markets:[{slug, title, prices, ...}]}

For groups we treat each child market as a YES outcome of the umbrella
event and apply the same A/ALL_YES, B/ALL_NO, C/YES_NO_PAIR logic as
Polymarket. For standalone binary markets only structure C applies.

Owns:
    _lim_quality_ok       — drop tight Limitless deals with low liq /
                              dead legs (Limitless-specific min_liq tune)
    _resolve_lim_end_date — robust deadline extraction across API variants
                              (Phase 9hhh — 8 different field names + ms/s
                              heuristic)
    eval_limitless        — full A/B/C evaluator over a list of events

Lazy imports: heavy deps (`_fetch_limitless_market_meta`,
`filter_limitless`, constants like `ENABLE_STRUCT_A`, `THRESH_LIMITLESS`)
are imported inside functions to avoid cyclic load with arb_server and
to honour `mock.patch.object(arb_server, 'CONST', X)` at call time.
"""
from __future__ import annotations

import os
from typing import Any


# Phase audit-29.05 (29.05.2026) — phantom-arb sanity floor.
#
# On thin / near-resolution Limitless binary markets (e.g. "BTC Up or Down -
# Hourly" 5 минут до резолва) MMs withdraw orders and the API can return
# stale lastTradePrice for the missing side. Result: yes_ask + no_ask
# sometimes drops to 0.73-0.86, looking like a 14-27% arb. On a real liquid
# CLOB this is mathematically impossible (overround keeps sum ≥ 1.01-1.04
# always); seeing sum < 0.95 is a tell-tale of stale orderbook on the
# missing side rather than a real opportunity.
#
# Operator-found 29.05.2026 (BTC/XRP Up-or-Down screenshot): paper-trade
# history accumulated multiple sum=73-86c entries that were impossible-arb
# phantoms. Calibration trade-off:
#   - 0.92 floor = block all phantoms (73-86c), но обрезает legit tests
#     that use sum 0.90 to verify arb math.
#   - 0.85 floor = block worst phantoms (73-84c — operator's worst cases),
#     allow sum 0.85+ through; relies on existing depth+volume quality
#     gates to filter the 85-90c marginal cases.
# Default 0.85 is the calibrated value. Operator can tighten via env
# `LIMITLESS_REALISTIC_SUM_FLOOR=0.92` once 85c-range phantom rate is
# observed in post-deploy paper data.
LIMITLESS_REALISTIC_SUM_FLOOR: float = float(
    os.environ.get('LIMITLESS_REALISTIC_SUM_FLOOR', '0.85'))


def _lim_quality_ok(d: dict, per_market: list[dict] | None) -> bool:
    """Drop ultra-tight Limitless deals that look attractive on paper but
    fall apart in execution. Same intent as Polymarket's `_quality_ok`
    but tuned to Limitless economics:

      - When sum is ≥ QUALITY_TIGHT_CUTOFF_CENTS (margin <5¢), require
        min_liq ≥ QUALITY_LIM_TIGHT_MIN_LIQ (default $130 — Phase 9gg
        lowered from $200 per operator request; more deals surface,
        slightly higher slippage risk).
      - Slippage cap kept at QUALITY_TIGHT_MAX_SLIP (0.3%) — same as
        Polymarket; same orderbook math.
      - Block deals where ALL legs report $0 volume — ghost market or
        stale price; we'd happily fire and not get filled.
    """
    from arb_server import (
        QUALITY_TIGHT_CUTOFF_CENTS,
        QUALITY_LIM_TIGHT_MIN_LIQ,
        QUALITY_TIGHT_MAX_SLIP,
    )
    if d['total_cents'] >= QUALITY_TIGHT_CUTOFF_CENTS:
        if (d.get('min_liq', 0) < QUALITY_LIM_TIGHT_MIN_LIQ
                or d.get('slip_pct', 0) >= QUALITY_TIGHT_MAX_SLIP):
            return False
    if per_market:
        all_dead = all((p.get('volume', 0) or 0) <= 0 for p in per_market)
        if all_dead:
            return False
    return True


def _resolve_lim_end_date(ev_or_child: dict) -> str | None:
    """Phase 9hhh — robust deadline extraction across Limitless API variants.

    The API returns deadline in different shapes depending on event type:
      - negRisk parent events: `deadline` (ms unix int)
      - standalone binary: `expirationTimestamp` (ms unix int)
      - some events: `expirationDate` (ISO 8601 string)
      - children may inherit any of these from parent

    Returns ISO 8601 string with UTC tz, or None if nothing parseable found.
    """
    if not isinstance(ev_or_child, dict):
        return None
    # ISO string fields first (cheapest — no math).
    for key in ('expirationDate', 'expiresAt', 'endDate', 'endDateIso'):
        v = ev_or_child.get(key)
        if isinstance(v, str) and len(v) >= 10:
            return v
    # Then unix-timestamp fields (could be seconds OR milliseconds).
    for key in ('deadline', 'expirationTimestamp', 'expiration', 'endTimestamp'):
        v = ev_or_child.get(key)
        if v is None:
            continue
        try:
            from datetime import datetime as _dt
            from datetime import timezone as _tz
            f = float(v)
            ts = f / 1000 if f > 1e12 else f  # ms vs s heuristic
            if ts > 0:
                # Phase 19v18 (05.05.2026) — pass `_tz.utc` instance, not
                # the module obj. Previously `tz=_tz` silently raised
                # TypeError → all numeric-ms deadlines returned None and
                # the UI showed "—" in the deadline column.
                return _dt.fromtimestamp(ts, tz=_tz.utc).isoformat()
        except (TypeError, ValueError):
            continue
    return None


def eval_limitless(events: list[dict], lim_res: dict,
                    diag: dict | None = None) -> list[dict]:
    """Evaluate Limitless Exchange events for arb structures A/B/C.

    `events` is the raw list from /markets/active (each event = a market
    or a negRisk group). `lim_res` maps slug → (best_yes_ask, depth_yes,
    best_no_ask, depth_no) from _fetch_limitless_orderbook.

    Phase 9b (28.04.2026): events run through filter_limitless first so
    we apply blacklist + 10-day window + is_deadline text reject + Other
    quarantine — parity with filter_poly.

    Coverage rule (Phase 9g): ALL_YES / ALL_NO MUST price every outcome.
    Even one missing outcome breaks the math (Leeds-vs-Burnley case —
    Draw outcome empty book → sum(Leeds + Burnley) = 80.5¢ looked like
    an arb but a Draw win was an unhedged 100¢ loss).
    """
    from arb_server import (
        _fetch_limitless_market_meta,
        ENABLE_STRUCT_A, ENABLE_STRUCT_B, ENABLE_STRUCT_C,
        THRESH_LIMITLESS, THETA_LIMITLESS,
    )
    from radar.build_deal import build_deal
    from radar.eval.polymarket import is_threshold_series
    from radar.filters.limitless import filter_limitless

    deals: list[dict] = []
    filtered = filter_limitless(events, diag=diag)
    for ev, is_quarantine in filtered:
        # Phase clean-quarantine (11.05.2026) — drop "Other"-outcome events
        # entirely. UI tab removed, executor refused them anyway.
        if is_quarantine:
            continue
        title = ev.get('title') or ev.get('proxyTitle') or '?'
        # Phase 9kkk: robust deadline (8-field probe + ms/s heuristic).
        end_date_iso = _resolve_lim_end_date(ev)

        children = ev.get('markets') or []
        if children:
            # ── NegRisk group → multi-outcome A/B/C ─────────────────
            total_outcomes = len(children)
            per_market: list[dict] = []
            outcomes_missing_yes = 0
            outcomes_missing_no = 0
            for child in children:
                slug = child.get('slug') or child.get('address')
                if not slug or slug not in lim_res:
                    outcomes_missing_yes += 1
                    outcomes_missing_no += 1
                    continue
                yes_ask, yes_depth, no_ask, no_depth = lim_res[slug]
                if yes_ask is None or not (0 < yes_ask < 1):
                    outcomes_missing_yes += 1
                    if no_ask is None or not (0 < no_ask < 1):
                        outcomes_missing_no += 1
                    continue
                if no_ask is None or not (0 < no_ask < 1):
                    outcomes_missing_no += 1
                # Per-market token IDs + verifying_contract from cached meta
                # so atomic._build_leg can construct a real EIP-712 order.
                meta = _fetch_limitless_market_meta(slug) or {}
                per_market.append({
                    'name': child.get('title') or child.get('proxyTitle') or '?',
                    'slug': slug,
                    'yes_price': yes_ask, 'yes_liq': yes_depth or 0,
                    'no_price': no_ask if (no_ask and 0 < no_ask < 1) else None,
                    'no_liq': no_depth or 0,
                    'yes_token': meta.get('yes_token'),
                    'no_token': meta.get('no_token'),
                    'verifying_contract': meta.get('verifying_contract'),
                    'volume': meta.get('volume', 0),
                })
            if len(per_market) < 2:
                continue
            full_yes_coverage = (outcomes_missing_yes == 0)
            full_no_coverage = (outcomes_missing_no == 0)

            # Phase 9o + 19v30 — threshold-series guard (parent + child
            # titles + slugs). YES/NO not mutually exclusive on overlapping
            # threshold series → ALL_YES/NO invalid; YES_NO_PAIR still OK.
            child_titles = [p['name'] for p in per_market]
            child_slugs = [p.get('slug') for p in per_market]
            threshold_series = is_threshold_series(
                title, child_titles, child_slugs)

            # ── A. ALL_YES — gated on full_yes_coverage ─────────────
            yes_outcomes = [{'name': p['name'], 'price': p['yes_price'],
                              'liquidity': p['yes_liq'], 'source': 'lim_clob',
                              'volume': p.get('volume', 0)}
                             for p in per_market]
            total_yes = sum(o['price'] for o in yes_outcomes)
            if (ENABLE_STRUCT_A and full_yes_coverage
                    and total_yes < THRESH_LIMITLESS
                    and not threshold_series):
                d = build_deal(title, 'Limitless', yes_outcomes, total_yes,
                                THETA_LIMITLESS, THRESH_LIMITLESS)
                if d:
                    d['arb_structure'] = 'all_yes'
                    d['end_date'] = end_date_iso
                    # Attach slug + token + verifying_contract per leg so
                    # atomic._build_leg can build a signed EIP-712 order.
                    for i, e in enumerate(d.get('entries', [])):
                        if i < len(per_market):
                            p = per_market[i]
                            e['slug'] = p['slug']
                            e['side'] = 'YES'
                            e['token_id'] = p['yes_token']
                            e['verifying_contract'] = p['verifying_contract']
                    if _lim_quality_ok(d, per_market):
                        deals.append(d)

            # ── B. ALL_NO (N≥3) — gated on full_no_coverage + N==total
            no_raw = [p for p in per_market if p['no_price'] is not None]
            N = len(no_raw)
            if (ENABLE_STRUCT_B and full_no_coverage
                    and N == total_outcomes and N >= 3
                    and not threshold_series):
                no_outcomes = [{'name': f"NO {p['name']}", 'price': p['no_price'],
                                 'liquidity': p['no_liq'], 'source': 'lim_clob',
                                 'volume': p.get('volume', 0)}
                                for p in no_raw]
                total_no = sum(o['price'] for o in no_outcomes)
                no_threshold = (N - 1) * THRESH_LIMITLESS
                if total_no < no_threshold:
                    d = build_deal(title + ' (ALL_NO)', 'Limitless',
                                    no_outcomes, total_no, THETA_LIMITLESS,
                                    no_threshold, payout_target=float(N - 1))
                    if d:
                        d['arb_structure'] = 'all_no'
                        d['payout_target'] = N - 1
                        d['end_date'] = end_date_iso
                        for i, e in enumerate(d.get('entries', [])):
                            if i < len(no_raw):
                                p = no_raw[i]
                                e['slug'] = p['slug']
                                e['side'] = 'NO'
                                e['token_id'] = p['no_token']
                                e['verifying_contract'] = p['verifying_contract']
                        if _lim_quality_ok(d, no_raw):
                            deals.append(d)

            # ── C. YES_NO_PAIR per market ───────────────────────────
            if not ENABLE_STRUCT_C:
                continue
            for p in per_market:
                if p['no_price'] is None:
                    continue
                pair_total = p['yes_price'] + p['no_price']
                if pair_total >= THRESH_LIMITLESS:
                    continue
                # Phase audit-29.05 — realistic-sum floor on per-market C
                # (same rationale as standalone binary above). Multi-outcome
                # groups can have legitimate ALL_YES/ALL_NO arbs at any
                # sum, but a single market's YES+NO must respect overround.
                if pair_total < LIMITLESS_REALISTIC_SUM_FLOOR:
                    continue
                pair_out = [
                    {'name': f"YES {p['name']}", 'price': p['yes_price'],
                     'liquidity': p['yes_liq'], 'source': 'lim_clob',
                     'volume': p.get('volume', 0)},
                    {'name': f"NO {p['name']}", 'price': p['no_price'],
                     'liquidity': p['no_liq'], 'source': 'lim_clob',
                     'volume': p.get('volume', 0)},
                ]
                d = build_deal(f"{title} — {p['name']}", 'Limitless', pair_out,
                                pair_total, THETA_LIMITLESS, THRESH_LIMITLESS)
                if d:
                    d['arb_structure'] = 'yes_no_pair'
                    d['end_date'] = end_date_iso
                    for e in d.get('entries', []):
                        is_yes = e['name'].startswith('YES ')
                        e['slug'] = p['slug']
                        e['side'] = 'YES' if is_yes else 'NO'
                        e['token_id'] = p['yes_token'] if is_yes else p['no_token']
                        e['verifying_contract'] = p['verifying_contract']
                    if _lim_quality_ok(d, [p]):
                        deals.append(d)
        else:
            # ── Standalone binary market — only structure C ─────────
            if not ENABLE_STRUCT_C:
                continue
            slug = ev.get('slug') or ev.get('address')
            if not slug or slug not in lim_res:
                continue
            yes_ask, yes_depth, no_ask, no_depth = lim_res[slug]
            if yes_ask is None or no_ask is None:
                continue
            if not (0 < yes_ask < 1) or not (0 < no_ask < 1):
                continue
            pair_total = yes_ask + no_ask
            if pair_total >= THRESH_LIMITLESS:
                continue
            # Phase audit-29.05 — realistic-sum floor. Single binary on a
            # liquid CLOB always has sum ≥ 1.0 (MM overround). Sum <
            # LIMITLESS_REALISTIC_SUM_FLOOR (default 0.92) means one side
            # is stale (MM withdrew near resolution, API returned last-
            # trade-price). Operator-found 29.05.2026 — BTC/XRP Up or
            # Down Hourly markets producing sum=73-86c phantom arbs.
            if pair_total < LIMITLESS_REALISTIC_SUM_FLOOR:
                continue
            meta = _fetch_limitless_market_meta(slug) or {}
            volume = meta.get('volume', 0)
            pair_out = [
                {'name': f"YES {title}", 'price': yes_ask,
                 'liquidity': yes_depth or 0, 'source': 'lim_clob',
                 'volume': volume},
                {'name': f"NO {title}", 'price': no_ask,
                 'liquidity': no_depth or 0, 'source': 'lim_clob',
                 'volume': volume},
            ]
            d = build_deal(title, 'Limitless', pair_out, pair_total,
                            THETA_LIMITLESS, THRESH_LIMITLESS)
            if d:
                d['arb_structure'] = 'binary'
                d['end_date'] = end_date_iso
                d['slug'] = slug
                for e in d.get('entries', []):
                    is_yes = e['name'].startswith('YES ')
                    e['slug'] = slug
                    e['side'] = 'YES' if is_yes else 'NO'
                    e['token_id'] = meta.get('yes_token') if is_yes else meta.get('no_token')
                    e['verifying_contract'] = meta.get('verifying_contract')
                pseudo_pm = [{
                    'yes_liq': yes_depth or 0,
                    'no_liq': no_depth or 0,
                    'volume': volume,
                }]
                if _lim_quality_ok(d, pseudo_pm):
                    deals.append(d)
    return deals
