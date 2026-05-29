"""HOT / NEAR pool classification + NEAR summary builder.

Extracted from arb_server.py in audit-28b cont 8 (28.05.2026). This is
the bridge between the per-platform evaluators (eval_poly, eval_kalshi,
eval_sx, eval_limitless) and the dashboard UI.

Owns:
    classify_pools(...)          — split candidates into HOT (already an
                                    arb) and NEAR (within NEAR_BUFFER of
                                    threshold) per platform.
    near_summary(...)            — render NEAR pool to UI-friendly rows
                                    (one entry per event, with the closest-
                                    to-arb structure highlighted).
    _best_near_structure(...)    — pick the structure (A/B/C) closest to
                                    its threshold for a given per-market
                                    snapshot.
    _sum_*_cand / _sum_sx_market — normalized arb-sum per platform (uses
                                    min over A/B/C structures).

Module state (mutated by near_summary, read by radar.api.deals.api_deals):
    _last_visible_near_count     — count of rows after near_summary's
                                    full filter pipeline. Lets /api/deals
                                    badge match /api/near table.
    _last_near_rejection_stats   — breakdown of rejection reasons (raw vs
                                    visible per platform). Surfaced via
                                    /api/deals → scan_data.stats.near_diag
                                    so operator can see WHY a 376-cand raw
                                    pool surfaces 0 visible rows.

All heavy deps (fetchers, constants, locks) are lazy-imported from
arb_server inside functions to avoid cyclic load.
"""
from __future__ import annotations

from typing import Any


# Module state — set by near_summary(), read by deals.api_deals via
# lazy `import radar.eval.pools as _p`. Stays None / empty dict until
# the first near_summary() call.
_last_visible_near_count: int | None = None
_last_near_rejection_stats: dict[str, int] = {}


# Phase 9w (29.04.2026): C-structure NEAR cap. Long-tail YES_NO_PAIR
# candidates dominated NEAR (14 of 41 visible, most at +2-3¢ from
# threshold). Sequence: 9w 2c (too strict, NEAR empty), 9ff 5c (too
# loose, C dominated), 9mm 3c — current.
C_NEAR_MAX_DISTANCE: float = 0.03


# ── Normalized per-platform sums (HOT/NEAR scoring) ───────────────────
def _sum_limitless_cand(ev: dict, lim_res: dict) -> float | None:
    """Min normalized arb-sum across A/B/C structures for a Limitless event.
    Used by classify_pools. Same incomplete-coverage gate as eval_limitless
    (ALL_YES / ALL_NO require every outcome priced) + threshold-series guard
    + per-leg alive-ness gate (volume>0).
    """
    from arb_server import _fetch_limitless_market_meta
    from radar.eval.polymarket import is_threshold_series

    children = ev.get('markets') or []
    pm: list[dict] = []
    total_outcomes = 0
    yes_missing = 0
    no_missing = 0
    if children:
        total_outcomes = len(children)
        for child in children:
            slug = child.get('slug') or child.get('address')
            if not slug or slug not in lim_res:
                yes_missing += 1
                no_missing += 1
                continue
            yes_ask, _yd, no_ask, _nd = lim_res[slug]
            if yes_ask is None or not (0 < yes_ask < 1):
                yes_missing += 1
                if no_ask is None or not (0 < no_ask < 1):
                    no_missing += 1
                continue
            if no_ask is None or not (0 < no_ask < 1):
                no_missing += 1
            meta = _fetch_limitless_market_meta(slug)
            vol = (meta or {}).get('volume')
            alive = (vol is None) or (vol > 0)
            pm.append({
                'yes': yes_ask,
                'no': no_ask if (no_ask and 0 < no_ask < 1) else None,
                'alive': alive,
            })
    else:
        total_outcomes = 1
        slug = ev.get('slug') or ev.get('address')
        if slug and slug in lim_res:
            yes_ask, _yd, no_ask, _nd = lim_res[slug]
            if (yes_ask is not None and no_ask is not None
                    and 0 < yes_ask < 1 and 0 < no_ask < 1):
                meta = _fetch_limitless_market_meta(slug)
                vol = (meta or {}).get('volume')
                alive = (vol is None) or (vol > 0)
                pm.append({'yes': yes_ask, 'no': no_ask, 'alive': alive})

    if not pm:
        return None
    all_alive = all(p.get('alive') for p in pm)

    title_for_threshold = ev.get('title') or ev.get('proxyTitle') or ''
    child_titles = [(c.get('title') or c.get('proxyTitle') or '')
                    for c in (children or [])]
    threshold_series = is_threshold_series(title_for_threshold, child_titles)

    candidates: list[float] = []
    # A. ALL_YES — strict all_alive + math fallback (sum_yes ≤ 1.5)
    if (children and yes_missing == 0 and not threshold_series and all_alive):
        s_yes = sum(p['yes'] for p in pm)
        if s_yes <= 1.5:
            candidates.append(s_yes)
    # B. ALL_NO (N≥3) — full NO coverage, NOT threshold-series, all alive
    no_raw = [p for p in pm if p['no'] is not None]
    N = len(no_raw)
    if (children and N == total_outcomes and N >= 3
            and not threshold_series and all_alive):
        s_no = sum(p['no'] for p in no_raw)
        if s_no <= (N - 0.5):
            candidates.append(s_no / (N - 1))
    # C. YES_NO_PAIR per market — only over alive legs
    pair_min: float | None = None
    for p in pm:
        if p['no'] is None:
            continue
        if not p.get('alive'):
            continue
        s = p['yes'] + p['no']
        pair_min = s if pair_min is None or s < pair_min else pair_min
    if pair_min is not None:
        candidates.append(pair_min)
    return min(candidates) if candidates else None


def _sum_poly_cand(cand: tuple, clob_res: dict, ws_books: dict) -> float | None:
    """Best (smallest) normalized arb-sum across A/B/C structures for a
    Polymarket candidate. Same coverage + threshold-series guards as
    eval_poly. Used by classify_pools NEAR-pool scoring.
    """
    from radar.eval.polymarket import _poly_per_market, is_threshold_series

    ev, rough, _is_q = cand
    pm = _poly_per_market(rough, clob_res, ws_books)
    is_single_binary = bool(ev.get('_single_binary'))
    if is_single_binary:
        if len(pm) < 1:
            return None
    elif len(pm) < 2:
        return None
    total_outcomes_on_event = len(ev.get('markets') or []) or len(pm)
    full_coverage = (len(pm) == total_outcomes_on_event)

    title = (ev.get('title') or '?')
    child_titles = [(o['m'].get('question') or o['m'].get('groupItemTitle') or '')
                    for o in rough]
    threshold_series = is_threshold_series(title, child_titles)

    candidates: list[float] = []
    # A. ALL_YES
    if not is_single_binary and full_coverage and not threshold_series:
        s_yes = sum(p['yes_price'] for p in pm if 0 < p['yes_price'] < 1)
        if s_yes > 0:
            candidates.append(s_yes)
    # B. ALL_NO (N≥3)
    no_raw = [p for p in pm if p['no_price'] is not None and 0 < p['no_price'] < 1]
    N = len(no_raw)
    if (not is_single_binary and N >= 3 and N == total_outcomes_on_event
            and not threshold_series):
        s_no = sum(p['no_price'] for p in no_raw)
        candidates.append(s_no / (N - 1))
    # C. YES_NO_PAIR
    pair_min: float | None = None
    for p in pm:
        if p['no_price'] is None or not (0 < p['no_price'] < 1):
            continue
        if not (0 < p['yes_price'] < 1):
            continue
        s = p['yes_price'] + p['no_price']
        pair_min = s if pair_min is None or s < pair_min else pair_min
    if pair_min is not None:
        candidates.append(pair_min)
    return min(candidates) if candidates else None


def _sum_kalshi_cand(ev_tickers_pair: tuple, kalshi_res: dict) -> float | None:
    """Best (smallest) normalized arb-sum for a Kalshi event. Same
    incomplete-coverage rule as Polymarket / Limitless. Note: Kalshi is
    DISABLED by default since 12.05.2026 (US-only KYC) but the scoring
    fn is preserved for parity."""
    ev, _tickers = ev_tickers_pair
    total_outcomes = len(ev.get('markets') or [])
    pm: list[dict] = []
    for m in ev.get('markets', []):
        t = m.get('ticker', '')
        if t not in kalshi_res:
            continue
        yes_ask, _yd, no_ask, _nd = kalshi_res[t]
        if yes_ask is None or yes_ask < 0.05 or yes_ask >= 1:
            continue
        pm.append({'yes': yes_ask,
                    'no': no_ask if (no_ask and 0 < no_ask < 1) else None})
    if len(pm) < 2:
        return None
    full_coverage = (len(pm) == total_outcomes)
    candidates: list[float] = []
    if full_coverage:
        s_yes = sum(p['yes'] for p in pm)
        if 0.50 <= s_yes:
            candidates.append(s_yes)
    no_raw = [p for p in pm if p['no'] is not None]
    N = len(no_raw)
    if N >= 3 and N == total_outcomes:
        candidates.append(sum(p['no'] for p in no_raw) / (N - 1))
    pair_min: float | None = None
    for p in pm:
        if p['no'] is None:
            continue
        s = p['yes'] + p['no']
        pair_min = s if pair_min is None or s < pair_min else pair_min
    if pair_min is not None:
        candidates.append(pair_min)
    return min(candidates) if candidates else None


def _sum_sx_market(m: dict, sx_orders: dict) -> float | None:
    """Trivial 2-outcome sum for an SX Bet binary market."""
    mh = m.get('marketHash', '')
    if mh not in sx_orders:
        return None
    best1, _d1, best2, _d2 = sx_orders[mh]
    if not best1 or not best2 or best1 <= 0 or best2 <= 0:
        return None
    return best1 + best2


# ── Pool classification (HOT vs NEAR per platform) ───────────────────
def classify_pools(pc: list, kc: list, sx_markets: list,
                    clob_res: dict, kalshi_res: dict, sx_res: dict,
                    lim_events: list | None = None,
                    lim_res: dict | None = None,
                    ws_books: dict | None = None) -> dict:
    """Split candidates into HOT (sum<threshold) and NEAR
    ([threshold, threshold+NEAR_BUFFER)) per platform.

    NEAR is sorted by `sum` ascending — closest-to-arb candidates win
    when WS subscription set is capped at MAX_WS_SUBS.

    Phase 9bbb (29.04.2026) — request-local Polymarket info cache to drop
    O(N²) lock contention. Each candidate has 3-7 markets × 5x calls/scan
    = ~2400 lock ops/scan; batch-fetch once at the top.

    Phase 19v4 (02.05.2026) — KILL the cold-cache hang. ThreadPoolExecutor
    fan-out with 25s deadline (was serial 280-310s tarpit).
    """
    from arb_server import (
        _batch_fetch_poly_market_info,
        NEAR_BUFFER, THRESH_KALSHI, THRESH_SX, THRESH_LIMITLESS,
        SX_BINARY_TYPES,
    )
    from radar.eval.polymarket import (
        _poly_per_market, compute_poly_threshold,
    )

    # Phase 19v4 — batch Polymarket info to a request-local cache.
    _all_cids: set[str] = set()
    for cand in pc:
        _ev, _rough, _is_q = cand
        for o in _rough:
            cid = o['m'].get('conditionId') or o['m'].get('condition_id')
            if cid:
                _all_cids.add(cid)
    _info_cache = _batch_fetch_poly_market_info(list(_all_cids))

    # Phase 19v12 (04.05.2026) — REAL_OB_SOURCES filter at pool-build.
    # `_best_near_structure` cuts implied-only legs; mirror that filter
    # here so pool count = visible count consistently.
    _REAL_OB_SOURCES = {'clob_ask', 'kalshi_ob', 'sx_ob', 'lim_clob',
                        'clob_synthetic'}

    poly_hot: list = []
    poly_near: list = []
    for cand in pc:
        s = _sum_poly_cand(cand, clob_res, ws_books or {})
        if s is None:
            continue
        _ev, _rough, _is_q = cand
        pm = _poly_per_market(_rough, clob_res, ws_books or {})
        if not pm:
            continue
        # Phase 19v20 (05.05.2026) — match `_best_near_structure` filter:
        # both yes_src AND no_src must be REAL (or no_src=None for synth).
        has_real = any(
            p.get('yes_src') in _REAL_OB_SOURCES
            and (p.get('no_src') is None or p.get('no_src') in _REAL_OB_SOURCES)
            for p in pm
        )
        if not has_real:
            continue
        # Phase 9k: per-cand dynamic threshold using its actual max fee.
        cand_max_fee_bps = 0
        for o in _rough:
            cid = o['m'].get('conditionId') or o['m'].get('condition_id')
            if cid:
                info = _info_cache.get(cid)
                if info and info['taker_fee_bps'] > cand_max_fee_bps:
                    cand_max_fee_bps = info['taker_fee_bps']
        cand_threshold = compute_poly_threshold(cand_max_fee_bps)
        if s < cand_threshold:
            poly_hot.append((s, cand))
        elif s < cand_threshold + NEAR_BUFFER:
            poly_near.append((s, cand))
    poly_hot.sort(key=lambda x: x[0])
    poly_near.sort(key=lambda x: x[0])
    poly_hot = [c for _, c in poly_hot]
    poly_near = [c for _, c in poly_near]

    kalshi_hot: list = []
    kalshi_near: list = []
    for cand in kc:
        s = _sum_kalshi_cand(cand, kalshi_res)
        if s is None:
            continue
        if s < THRESH_KALSHI:
            kalshi_hot.append((s, cand))
        elif s < THRESH_KALSHI + NEAR_BUFFER:
            kalshi_near.append((s, cand))
    kalshi_hot.sort(key=lambda x: x[0])
    kalshi_near.sort(key=lambda x: x[0])
    kalshi_hot = [c for _, c in kalshi_hot]
    kalshi_near = [c for _, c in kalshi_near]

    # SX — per binary-type market
    sx_hot_sorted: list = []
    sx_near_sorted: list = []
    seen_hashes: set[str] = set()
    for m in sx_markets:
        if m.get('type') not in SX_BINARY_TYPES:
            continue
        mh = m.get('marketHash', '')
        if not mh or mh in seen_hashes:
            continue
        seen_hashes.add(mh)
        s = _sum_sx_market(m, sx_res)
        if s is None:
            continue
        if s < THRESH_SX:
            sx_hot_sorted.append((s, m))
        elif s < THRESH_SX + NEAR_BUFFER:
            sx_near_sorted.append((s, m))
    sx_hot_sorted.sort(key=lambda x: x[0])
    sx_near_sorted.sort(key=lambda x: x[0])
    sx_hot = [m for _, m in sx_hot_sorted]
    sx_near = [m for _, m in sx_near_sorted]

    # Limitless — per event (negRisk group OR standalone binary). Sort
    # by (sum_asks, -event_volume) — at equal tightness, prefer higher
    # volume since those are more likely to actually fill.
    def _ev_volume(ev: dict) -> float:
        v = ev.get('volume') or ev.get('volumeFormatted') or 0
        try:
            v = float(v)
        except Exception:
            v = 0
        for c in (ev.get('markets') or []):
            try:
                v += float(c.get('volume') or 0)
            except Exception:
                pass
        return v

    lim_hot_sorted: list = []
    lim_near_sorted: list = []
    for ev in (lim_events or []):
        s = _sum_limitless_cand(ev, lim_res or {})
        if s is None:
            continue
        sort_key = (s, -_ev_volume(ev))
        if s < THRESH_LIMITLESS:
            lim_hot_sorted.append((sort_key, ev))
        elif s < THRESH_LIMITLESS + NEAR_BUFFER:
            lim_near_sorted.append((sort_key, ev))
    lim_hot_sorted.sort(key=lambda x: x[0])
    lim_near_sorted.sort(key=lambda x: x[0])
    lim_hot = [ev for _, ev in lim_hot_sorted]
    lim_near = [ev for _, ev in lim_near_sorted]

    return {
        'poly':   {'hot': poly_hot,   'near': poly_near},
        'kalshi': {'hot': kalshi_hot, 'near': kalshi_near},
        'sx':     {'hot': sx_hot,     'near': sx_near},
        'lim':    {'hot': lim_hot,    'near': lim_near},
    }


# ── NEAR — pick best structure per event ─────────────────────────────
def _best_near_structure(pm: list[dict], threshold: float,
                           threshold_series: bool = False,
                           _reason_out: dict | None = None) -> dict | None:
    """Pick the arb structure closest to crossing its threshold.

    Returns a dict {structure, sum, threshold, distance, outcomes_count,
    prices, liqs} (plus `market_name` for YES_NO_PAIR). Returns None when
    no structure qualifies; `_reason_out` (optional) is filled with a
    rejection-reason key so caller can emit diagnostic counters.

    Filters applied:
      - REAL_OB_SOURCES on each leg (Phase 9kkk #8) — drop implied-only
        legs. `clob_synthetic` is whitelisted (Phase 10 Task A — NO_ask
        = 1 - YES_best_bid is REAL).
      - all_alive (Phase 9hh) — A and B require every leg with volume>0;
        C skips dead legs individually.
      - NEAR_BUFFER cap (Phase 9kkk #46) — A/B options must be within
        NEAR_BUFFER above threshold, not just below math-fallback caps.
      - C_NEAR_MAX_DISTANCE (Phase 9w/ff/mm) — show C in NEAR only when
        within 2-3¢ of arb.
    """
    from arb_server import NEAR_BUFFER

    if not pm:
        if _reason_out is not None:
            _reason_out['key'] = 'empty_pm'
        return None

    REAL_OB_SOURCES = {'clob_ask', 'kalshi_ob', 'sx_ob', 'lim_clob',
                        'clob_synthetic'}
    _orig_n = len(pm)
    pm = [
        p for p in pm
        if (p.get('yes_src') in REAL_OB_SOURCES)
        and (p.get('no_src') is None
             or p.get('no_src') in REAL_OB_SOURCES)
    ]
    if not pm:
        if _reason_out is not None:
            _reason_out['key'] = 'all_legs_implied'
            _reason_out['detail'] = (f'{_orig_n} legs all rejected — '
                                       'yes_src not in REAL_OB_SOURCES')
        return None

    all_alive = all(p.get('alive', True) for p in pm)
    options: list[dict] = []

    # A. ALL_YES
    yes_prices = [p['yes_price'] for p in pm if 0 < p['yes_price'] < 1]
    yes_liqs = [p['yes_liq'] for p in pm if 0 < p['yes_price'] < 1]
    if len(yes_prices) >= 2 and not threshold_series and all_alive:
        s = sum(yes_prices)
        if s <= 1.5 and (s - threshold) <= NEAR_BUFFER:
            options.append({'structure': 'all_yes', 'sum': s,
                             'threshold': threshold,
                             'outcomes_count': len(yes_prices),
                             'prices': yes_prices, 'liqs': yes_liqs})
    # B. ALL_NO (N≥3)
    no_pm = [p for p in pm if p['no_price'] is not None and 0 < p['no_price'] < 1]
    N = len(no_pm)
    if N >= 3 and not threshold_series and all_alive:
        no_prices = [p['no_price'] for p in no_pm]
        s = sum(no_prices)
        b_threshold = (N - 1) * threshold
        if s <= (N - 0.5) and (s - b_threshold) <= NEAR_BUFFER:
            options.append({'structure': 'all_no', 'sum': s,
                             'threshold': b_threshold,
                             'outcomes_count': N,
                             'prices': no_prices,
                             'liqs': [p['no_liq'] for p in no_pm]})
    # C. YES_NO_PAIR (best market within C_NEAR_MAX_DISTANCE)
    pair_best: dict | None = None
    for p in pm:
        if p['no_price'] is None or not (0 < p['no_price'] < 1):
            continue
        if not (0 < p['yes_price'] < 1):
            continue
        if not p.get('alive', True):
            continue
        s = p['yes_price'] + p['no_price']
        if pair_best is None or s < pair_best['sum']:
            pair_best = {'structure': 'yes_no_pair', 'sum': s,
                          'threshold': threshold,
                          'outcomes_count': 2,
                          'prices': [p['yes_price'], p['no_price']],
                          'liqs': [p['yes_liq'], p['no_liq']],
                          'market_name': p.get('name') or ''}
    if pair_best is not None:
        if pair_best['sum'] - pair_best['threshold'] <= C_NEAR_MAX_DISTANCE:
            options.append(pair_best)
    if not options:
        if _reason_out is not None:
            _reason_out['key'] = 'no_structure_near_threshold'
        return None
    options.sort(key=lambda o: o['sum'] - o['threshold'])
    return options[0]


# ── UI-friendly NEAR snapshot ────────────────────────────────────────
def near_summary(clob_res: dict | None = None, kalshi_res: dict | None = None,
                  sx_res: dict | None = None, lim_res: dict | None = None,
                  ws_books: dict | None = None) -> list[dict]:
    """Build the UI's NEAR table. One row per event, with the arb structure
    closest to its threshold highlighted (A/B/C/binary badges).

    Phase 19v6 (03.05.2026) — diagnostic counters tell operator WHY
    raw 376-cand pool → 0 visible rows. Surfaced via /api/deals →
    scan_data.stats.near_diag.
    """
    global _last_visible_near_count, _last_near_rejection_stats

    from datetime import datetime as _dt
    from datetime import timezone as _tz

    from arb_server import (
        pools_lock, pools, poly_clob_cache,
        _fetch_clob, _fetch_poly_market_info, _fetch_limitless_market_meta,
        THRESH_KALSHI, THRESH_SX, THRESH_LIMITLESS, THRESH_POLY,
    )
    from radar.eval.polymarket import (
        _poly_per_market, compute_poly_threshold, is_threshold_series,
    )
    from radar.eval.limitless import _resolve_lim_end_date
    from radar.filters._helpers import compute_adaptive_grace_minutes
    from radar.filters.sx import _sx_market_title

    out: list[dict] = []
    diag = {
        'poly_raw': 0, 'poly_visible': 0, 'poly_rejected_quarantine': 0,
        'poly_rejected_zombie': 0, 'poly_rejected_strict': 0,
        'poly_strict_all_implied': 0,
        'poly_strict_no_near': 0,
        'poly_strict_empty_pm': 0,
        'lim_raw': 0, 'lim_visible': 0, 'lim_rejected_strict': 0,
        'sx_raw': 0, 'sx_visible': 0, 'sx_rejected_strict': 0,
    }
    with pools_lock:
        poly_near = list(pools['poly']['near'])
        kalshi_near = list(pools['kalshi']['near'])
        sx_near = list(pools['sx']['near'])
        lim_near = list(pools['lim']['near'])
    diag['poly_raw'] = len(poly_near)
    diag['lim_raw'] = len(lim_near)
    diag['sx_raw'] = len(sx_near)

    for cand in poly_near:
        ev, rough, is_quarantine = cand
        # Phase 9kkk #48 — quarantined → never surface in NEAR.
        if is_quarantine:
            diag['poly_rejected_quarantine'] += 1
            continue
        # Phase 9kkk #5 — second-line past-resolve guard (filter_poly
        # is the first line; this catches pool entries that aged past
        # their grace window between scans).
        ev_end_date = ev.get('endDateIso') or ev.get('endDate')
        _is_zombie = False
        if ev_end_date:
            try:
                _ed = (ev_end_date[:-1] + '+00:00'
                       if isinstance(ev_end_date, str) and ev_end_date.endswith('Z')
                       else ev_end_date)
                if isinstance(_ed, str) and len(_ed) == 10:
                    _ed += 'T00:00:00+00:00'
                _end_dt = _dt.fromisoformat(_ed) if isinstance(_ed, str) else None
                if _end_dt is not None:
                    if not _end_dt.tzinfo:
                        _end_dt = _end_dt.replace(tzinfo=_tz.utc)
                    if (_dt.now(_tz.utc) - _end_dt).total_seconds() > 3600:
                        _is_zombie = True
            except (TypeError, ValueError):
                pass
        if _is_zombie:
            diag['poly_rejected_zombie'] += 1
            continue
        # Phase 19v26 — sync /book fetch for missing tokens to fix
        # pool→visible mismatch from cache decay between scans.
        clob_for_pm = dict(clob_res or poly_clob_cache)
        _missing_tids: list[str] = []
        for o in rough:
            for tid in (o.get('token_id_yes') or o.get('token_id'),
                         o.get('token_id_no')):
                if not tid:
                    continue
                v = clob_for_pm.get(tid)
                if v is None or (isinstance(v, tuple) and not v[0]):
                    _missing_tids.append(tid)
        if _missing_tids and len(_missing_tids) <= 8:
            for tid in _missing_tids[:8]:
                try:
                    _, ask, depth, bid, bid_depth = _fetch_clob(tid)
                    if ask is not None:
                        clob_for_pm[tid] = (ask, depth, bid, bid_depth)
                except Exception:
                    pass
        pm = _poly_per_market(rough, clob_for_pm, ws_books or {})
        title_p = ev.get('title') or '?'
        child_titles_p = [(o['m'].get('question') or o['m'].get('groupItemTitle') or '')
                          for o in rough]
        ts_p = is_threshold_series(title_p, child_titles_p)
        cand_max_fee_bps = 0
        for o in rough:
            m = o.get('m') or {}
            cid = m.get('conditionId') or m.get('condition_id')
            if cid:
                info = _fetch_poly_market_info(cid)
                if info and info.get('taker_fee_bps') is not None:
                    cand_max_fee_bps = max(cand_max_fee_bps, info['taker_fee_bps'])
        dyn_thresh_p = (compute_poly_threshold(cand_max_fee_bps)
                         if cand_max_fee_bps else THRESH_POLY)
        _reason: dict = {}
        best = _best_near_structure(pm, dyn_thresh_p, threshold_series=ts_p,
                                      _reason_out=_reason)
        if best is None:
            diag['poly_rejected_strict'] += 1
            rk = _reason.get('key') or 'unknown'
            if rk == 'all_legs_implied':
                diag['poly_strict_all_implied'] += 1
            elif rk == 'no_structure_near_threshold':
                diag['poly_strict_no_near'] += 1
            elif rk == 'empty_pm':
                diag['poly_strict_empty_pm'] += 1
            continue
        diag['poly_visible'] += 1
        ev_title_p = ev.get('title', '?')
        market_name_p = best.get('market_name') or ''
        display_title = ev_title_p
        if best['structure'] == 'yes_no_pair' and market_name_p:
            if (market_name_p not in ev_title_p
                    and ev_title_p not in market_name_p):
                display_title = f"{ev_title_p} — {market_name_p}"
        out.append({
            'platform': 'Polymarket',
            'arb_structure': best['structure'],
            'title': display_title,
            'sum_cents': round(best['sum'] * 100, 1),
            'distance_cents': round((best['sum'] - best['threshold']) * 100, 1),
            'threshold_cents': round(best['threshold'] * 100, 1),
            'outcomes_count': best['outcomes_count'],
            'min_price_cents': round(min(best['prices']) * 100, 1),
            'max_price_cents': round(max(best['prices']) * 100, 1),
            'min_liquidity': round(min(best['liqs']) if best['liqs'] else 0, 0),
            'end_date': ev.get('endDateIso') or ev.get('endDate'),
            'search_query': market_name_p or ev_title_p,
        })

    for cand in kalshi_near:
        ev, _tickers = cand
        pm = []
        for m in ev.get('markets', []):
            t = m.get('ticker', '')
            if not kalshi_res or t not in kalshi_res:
                continue
            yes_ask, yes_depth, no_ask, no_depth = kalshi_res[t]
            if yes_ask is None or yes_ask < 0.05 or yes_ask >= 1:
                continue
            pm.append({'name': m.get('title', t),
                        'yes_price': yes_ask, 'yes_liq': yes_depth or 0,
                        'no_price': no_ask if (no_ask and 0 < no_ask < 1) else None,
                        'no_liq': no_depth or 0})
        best = _best_near_structure(pm, THRESH_KALSHI)
        if best is None:
            continue
        display_title = ev.get('title', '?')
        if best['structure'] == 'yes_no_pair' and best.get('market_name'):
            display_title = f"{display_title} — {best['market_name']}"
        out.append({
            'platform': 'Kalshi',
            'arb_structure': best['structure'],
            'title': display_title,
            'sum_cents': round(best['sum'] * 100, 1),
            'distance_cents': round((best['sum'] - best['threshold']) * 100, 1),
            'threshold_cents': round(best['threshold'] * 100, 1),
            'outcomes_count': best['outcomes_count'],
            'min_price_cents': round(min(best['prices']) * 100, 1),
            'max_price_cents': round(max(best['prices']) * 100, 1),
            'min_liquidity': round(min(best['liqs']) if best['liqs'] else 0, 0),
            'end_date': ev.get('close_time') or ev.get('expected_expiration_time'),
        })

    for m in sx_near:
        if not sx_res:
            continue
        mh = m.get('marketHash', '')
        if mh not in sx_res:
            continue
        best1, depth1, best2, depth2 = sx_res[mh]
        if not best1 or not best2:
            continue
        s = best1 + best2
        gs = m.get('gameStartDate') or m.get('gameTime')
        end_iso: str | None = None
        if gs:
            try:
                ts = float(gs) / 1000 if float(gs) > 1e12 else float(gs)
                end_iso = _dt.fromtimestamp(ts, tz=_tz.utc).isoformat()
            except Exception:
                pass
        out.append({
            'platform': 'SX Bet',
            'arb_structure': 'binary',
            'title': _sx_market_title(m),
            'sum_cents': round(s * 100, 1),
            'distance_cents': round((s - THRESH_SX) * 100, 1),
            'threshold_cents': round(THRESH_SX * 100, 0),
            'outcomes_count': 2,
            'min_price_cents': round(min(best1, best2) * 100, 1),
            'max_price_cents': round(max(best1, best2) * 100, 1),
            'min_liquidity': round(min(depth1 or 0, depth2 or 0), 0),
            'end_date': end_iso,
        })

    # Limitless — per event (negRisk group or standalone binary)
    for ev in lim_near:
        if not lim_res:
            continue
        # Phase 19v17 — second-line past-resolve guard at NEAR level.
        try:
            _deadline = ev.get('deadline') or ev.get('expirationTimestamp')
            _end_dt = None
            if isinstance(_deadline, (int, float)):
                _ts = _deadline / 1000 if _deadline > 1e12 else _deadline
                _end_dt = _dt.fromtimestamp(_ts, tz=_tz.utc)
            elif isinstance(_deadline, str):
                _ds = ((_deadline[:-1] + '+00:00')
                       if _deadline.endswith('Z') else _deadline)
                if len(_ds) == 10:
                    _ds += 'T00:00:00+00:00'
                _end_dt = _dt.fromisoformat(_ds)
                if _end_dt.tzinfo is None:
                    _end_dt = _end_dt.replace(tzinfo=_tz.utc)
            if _end_dt is not None:
                _age_min = (_dt.now(_tz.utc) - _end_dt).total_seconds() / 60.0
                if _age_min > 0:
                    _grace = compute_adaptive_grace_minutes(
                        title=ev.get('title') or ev.get('proxyTitle') or '?')
                    if _age_min > _grace:
                        diag['lim_skip_past_resolve'] = (
                            diag.get('lim_skip_past_resolve', 0) + 1)
                        continue
        except Exception:
            pass
        children = ev.get('markets') or []
        pm = []
        if children:
            for child in children:
                slug = child.get('slug') or child.get('address')
                if not slug or slug not in lim_res:
                    continue
                yes_ask, yes_depth, no_ask, no_depth = lim_res[slug]
                if yes_ask is None or not (0 < yes_ask < 1):
                    continue
                meta = _fetch_limitless_market_meta(slug) or {}
                no_p = no_ask if (no_ask and 0 < no_ask < 1) else None
                pm.append({'name': child.get('title', '?'),
                            'yes_price': yes_ask, 'yes_liq': yes_depth or 0,
                            'yes_src': 'lim_clob',
                            'no_price': no_p,
                            'no_liq': no_depth or 0,
                            'no_src': 'lim_clob' if no_p is not None else None,
                            'volume': meta.get('volume', 0)})
        else:
            slug = ev.get('slug') or ev.get('address')
            if slug and slug in lim_res:
                yes_ask, yes_depth, no_ask, no_depth = lim_res[slug]
                if yes_ask is not None and 0 < yes_ask < 1:
                    meta = _fetch_limitless_market_meta(slug) or {}
                    no_p = no_ask if (no_ask and 0 < no_ask < 1) else None
                    pm.append({'name': ev.get('title', '?'),
                                'yes_price': yes_ask, 'yes_liq': yes_depth or 0,
                                'yes_src': 'lim_clob',
                                'no_price': no_p,
                                'no_liq': no_depth or 0,
                                'no_src': 'lim_clob' if no_p is not None else None,
                                'volume': meta.get('volume', 0)})
        for _p in pm:
            v = _p.get('volume')
            _p['alive'] = (v is None) or (v > 0)
        title_l = ev.get('title') or ev.get('proxyTitle') or '?'
        child_titles_l = [(c.get('title') or c.get('proxyTitle') or '')
                          for c in (ev.get('markets') or [])]
        ts_l = is_threshold_series(title_l, child_titles_l)
        best = _best_near_structure(pm, THRESH_LIMITLESS, threshold_series=ts_l)
        if best is None:
            continue
        ev_title = ev.get('title') or ev.get('proxyTitle') or '?'
        market_name = best.get('market_name') or ''
        display_title = ev_title
        if best['structure'] == 'yes_no_pair' and market_name:
            if (market_name and market_name not in ev_title
                    and ev_title not in market_name):
                display_title = f"{ev_title} — {market_name}"
        # Phase 9hhh — thorough end_date probe across all known fields
        end_iso = _resolve_lim_end_date(ev)
        if not end_iso:
            for ch in (ev.get('markets') or []):
                end_iso = _resolve_lim_end_date(ch)
                if end_iso:
                    break
        out.append({
            'platform': 'Limitless',
            'arb_structure': best['structure'],
            'title': display_title,
            'sum_cents': round(best['sum'] * 100, 1),
            'distance_cents': round((best['sum'] - best['threshold']) * 100, 1),
            'threshold_cents': round(best['threshold'] * 100, 1),
            'outcomes_count': best['outcomes_count'],
            'min_price_cents': round(min(best['prices']) * 100, 1),
            'max_price_cents': round(max(best['prices']) * 100, 1),
            'min_liquidity': round(min(best['liqs']) if best['liqs'] else 0, 0),
            'end_date': end_iso,
            'search_query': market_name or ev_title,
        })

    # Phase 9xx — drop misleading negative-distance rows (live snapshot
    # vs scan-time pool classification).
    out = [x for x in out if x['distance_cents'] >= -0.5]
    out.sort(key=lambda x: x['distance_cents'])
    _last_visible_near_count = len(out)
    diag['lim_visible'] = sum(1 for x in out if x.get('platform') == 'Limitless')
    diag['sx_visible'] = sum(1 for x in out if x.get('platform') == 'SX Bet')
    diag['lim_rejected_strict'] = max(0, diag['lim_raw'] - diag['lim_visible'])
    diag['sx_rejected_strict'] = max(0, diag['sx_raw'] - diag['sx_visible'])
    diag['total_visible'] = len(out)
    _last_near_rejection_stats = diag
    return out
