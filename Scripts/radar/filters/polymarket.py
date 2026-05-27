"""Polymarket event filter.

Extracted from arb_server.py::filter_poly in audit-28b continuation
(27.05.2026). This is the largest filter — ~280 lines of layered guards
that accumulated through Phase 9kkk and v19/v20/v22 hotfixes.

Filter responsibilities (each guard cites its originating phase):
    1. Title blacklist (operator-controlled).
    2. Calendar window (WINDOW_DAYS ahead, WINDOW_PAST_DAYS behind).
    3. Past-resolve adaptive grace (#41/#42 — Highest-Temp + BTC-Up-or-Down
       phantoms had 5h-old endDates flagged as live).
    4. Phantom-on-resolution (#yy — closed/archived UMA dispute window).
    5. ≥1 market (single-binary = structure C path, see 9w).
    6. Per-market closed/archived/no-orderbook/not-accepting-orders gate.
    7. negRisk enforcement (multi-outcome only — single-binary skips, 9w).
    8. Quarantine flag via has_other_outcome().
    9. Rough-list build: alive markets with outcomePrices in (0, 1).
   10. Min-rough check (1 for single-binary OR closed-children path, 2 otherwise).
   11. Sum-implied < 0.99 gate (skipped for single-binary + closed-children).
   12. Deadline-text rejection.
   13. clobTokenIds extraction (YES + NO) — populated on each rough entry.

Inputs:
    events: list of raw Polymarket event dicts (gamma-api shape).
    diag:   optional counter dict — fills 'poly_in', 'poly_skip_*',
            'poly_pass'.
    blacklist: optional iterable of titles to drop. Defaults to empty —
            radar passes the runtime-mutable `arb_server.blacklist` set.

Returns:
    (candidates, token_ids)
        candidates: list of (event, rough_outcomes, is_quarantine) tuples.
        token_ids:  flat list of CLOB token ids ready for orderbook fetch.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Optional

from radar.filters._helpers import (
    has_other_outcome,
    is_deadline,
    is_within_10_days,
)


def filter_poly(
    events: list[dict[str, Any]],
    diag: Optional[dict[str, int]] = None,
    blacklist: Optional[set[str]] = None,
) -> tuple[list[tuple[dict[str, Any], list[dict[str, Any]], bool]], list[str]]:
    """See module docstring."""
    if diag is None:
        diag = {}
    if blacklist is None:
        # Lazy import to avoid circular dependency with arb_server.
        try:
            import arb_server as _radar
            blacklist = _radar.blacklist
        except Exception:
            blacklist = set()
    diag['poly_in'] = len(events)
    for k in ('poly_skip_blacklist', 'poly_skip_no_window', 'poly_skip_lt2_markets',
              'poly_skip_no_negrisk', 'poly_skip_lt2_rough', 'poly_skip_sum_high',
              'poly_skip_deadline_text', 'poly_pass'):
        diag.setdefault(k, 0)

    candidates: list[tuple[dict[str, Any], list[dict[str, Any]], bool]] = []
    token_ids: list[str] = []

    for ev in events:
        title = ev.get('title', '?')
        if title in blacklist:
            diag['poly_skip_blacklist'] += 1
            continue

        # ── Calendar window ───────────────────────────────────────────
        end_date = ev.get('endDateIso') or ev.get('endDate')
        if not is_within_10_days(date_str=end_date):
            diag['poly_skip_no_window'] += 1
            continue

        # ── Past-resolve adaptive grace (Phase 9kkk #41/#42) ──────────
        # Operator-found: "Highest temperature in Miami on April 30" /
        # "Bitcoin Up or Down - 1PM ET" appeared hours after resolve.
        # Grace window scales with event duration (5-min crypto → 1 min,
        # 1h → 5 min, 24h → 30 min, multi-day → 60 min UMA window).
        if end_date:
            try:
                ed: Any = (end_date[:-1] + '+00:00') if isinstance(end_date, str) and end_date.endswith('Z') else end_date
                if isinstance(ed, str) and len(ed) == 10:
                    ed += 'T00:00:00+00:00'
                end_dt = datetime.fromisoformat(ed) if isinstance(ed, str) else None
                if end_dt is not None:
                    if not end_dt.tzinfo:
                        end_dt = end_dt.replace(tzinfo=timezone.utc)
                    duration_seconds: Optional[float] = None
                    start_date = ev.get('startDate') or ev.get('startDateIso')
                    if start_date:
                        try:
                            sd: Any = (start_date[:-1] + '+00:00') if isinstance(start_date, str) and start_date.endswith('Z') else start_date
                            if isinstance(sd, str) and len(sd) == 10:
                                sd += 'T00:00:00+00:00'
                            start_dt = datetime.fromisoformat(sd) if isinstance(sd, str) else None
                            if start_dt is not None:
                                if not start_dt.tzinfo:
                                    start_dt = start_dt.replace(tzinfo=timezone.utc)
                                duration_seconds = (end_dt - start_dt).total_seconds()
                        except Exception:
                            pass
                    if duration_seconds is not None and duration_seconds > 0:
                        if duration_seconds <= 600:
                            grace_minutes = 1
                        elif duration_seconds <= 3600:
                            grace_minutes = 5
                        elif duration_seconds <= 86400:
                            grace_minutes = 30
                        else:
                            grace_minutes = 60
                    else:
                        title_lower = (ev.get('title') or '').lower()
                        intraday_signals = (
                            ' 5min', '-5min', '5-min',
                            ' 1min', '-1min', '1-min',
                            'minutely', 'every 5 min', '5min crypto',
                        )
                        is_intraday_ampm = bool(re.search(
                            r'\b\d{1,2}(am|pm)(-\d{1,2}(am|pm))?\s*et\b',
                            title_lower))
                        if any(s in title_lower for s in intraday_signals) or is_intraday_ampm:
                            grace_minutes = 1
                        elif 'highest temperature' in title_lower or 'lowest temperature' in title_lower:
                            grace_minutes = 30
                        else:
                            grace_minutes = 30

                    age_minutes = (datetime.now(timezone.utc) - end_dt).total_seconds() / 60
                    if age_minutes > grace_minutes:
                        diag.setdefault('poly_skip_past_resolve', 0)
                        diag['poly_skip_past_resolve'] += 1
                        continue
            except (TypeError, ValueError):
                pass

        # ── Phantom-on-resolution (Phase 9yy) ─────────────────────────
        if ev.get('closed') is True or ev.get('archived') is True:
            diag.setdefault('poly_skip_closed', 0)
            diag['poly_skip_closed'] += 1
            continue

        markets = ev.get('markets', [])
        if len(markets) < 1:
            diag['poly_skip_lt2_markets'] += 1
            continue

        # ── Single-binary structure C path (Phase 9w) ─────────────────
        is_single_binary = (len(markets) == 1)
        ev['_single_binary'] = is_single_binary

        if ev.get('closed') is True or ev.get('archived') is True:
            diag.setdefault('poly_skip_outcome_closed', 0)
            diag['poly_skip_outcome_closed'] += 1
            continue

        # Phase 9jj — per-child closed children flag, NOT a hard reject.
        # Phase 9ll — drop `restricted` from per-child checks (CFTC tag).
        ev_has_closed_children = False
        if not is_single_binary:
            ev_has_closed_children = any(
                (m.get('closed') is True or m.get('archived') is True
                 or m.get('enableOrderBook') is False
                 or m.get('acceptingOrders') is False)
                for m in markets
            )
            ev['_has_closed_children'] = ev_has_closed_children
        else:
            m = markets[0]
            if (m.get('closed') is True or m.get('archived') is True
                or m.get('enableOrderBook') is False
                or m.get('acceptingOrders') is False):
                diag.setdefault('poly_skip_outcome_closed', 0)
                diag['poly_skip_outcome_closed'] += 1
                continue

        # ── negRisk enforcement (multi-outcome only) ──────────────────
        if not is_single_binary:
            if not (ev.get('negRisk') is True or
                    (markets and all(m.get('negRisk') is True for m in markets))):
                diag['poly_skip_no_negrisk'] += 1
                continue

        # ── Quarantine via has_other_outcome ──────────────────────────
        market_names: list[str] = []
        for m in markets:
            q = m.get('question') or ''
            gt = m.get('groupItemTitle') or ''
            if q:
                market_names.append(q)
            if gt:
                market_names.append(gt)
        if title:
            market_names.append(title)
        is_quarantine = has_other_outcome(market_names)

        # ── Rough-list build ─────────────────────────────────────────
        rough: list[dict[str, Any]] = []
        for m in markets:
            if (m.get('closed') is True or m.get('archived') is True
                    or m.get('enableOrderBook') is False
                    or m.get('acceptingOrders') is False):
                continue
            ps = m.get('outcomePrices')
            if not ps:
                continue
            try:
                p = float(json.loads(ps)[0])
            except (ValueError, TypeError, IndexError, json.JSONDecodeError):
                continue
            if p <= 0 or p >= 1:
                continue
            rough.append({'m': m, 'implied': p})

        if ev_has_closed_children:
            min_rough = 1
        else:
            min_rough = 1 if is_single_binary else 2
        if len(rough) < min_rough:
            diag['poly_skip_lt2_rough'] += 1
            continue

        if not is_single_binary and not ev_has_closed_children:
            if sum(o['implied'] for o in rough) >= 0.99:
                diag['poly_skip_sum_high'] += 1
                continue

        names = [o['m'].get('question', o['m'].get('groupItemTitle', '?')) for o in rough]
        if is_deadline(names):
            diag['poly_skip_deadline_text'] += 1
            continue

        # ── clobTokenIds extraction ──────────────────────────────────
        for o in rough:
            tids_str = o['m'].get('clobTokenIds')
            if tids_str:
                try:
                    tids = json.loads(tids_str)
                    if tids:
                        o['token_id'] = tids[0]
                        o['token_id_yes'] = tids[0]
                        token_ids.append(tids[0])
                        if len(tids) > 1 and tids[1]:
                            o['token_id_no'] = tids[1]
                            token_ids.append(tids[1])
                        else:
                            o['token_id_no'] = None
                except (ValueError, TypeError, json.JSONDecodeError, IndexError):
                    pass
        candidates.append((ev, rough, is_quarantine))
        diag['poly_pass'] += 1

    return candidates, token_ids
