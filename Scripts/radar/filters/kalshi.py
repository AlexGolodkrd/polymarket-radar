"""Kalshi event filter.

Extracted from arb_server.py::filter_kalshi in audit-28b (27.05.2026).
Disabled by default in production (ENABLE_KALSHI=0 since PR #177 —
Kalshi is geo-blocked from non-US VPS) but the filter still runs during
tests + when the operator explicitly enables it.

Filter responsibilities:
    1. Drop events with < 2 markets (need ≥2 outcomes for an arb).
    2. Drop events outside the calendar window (WINDOW_DAYS).
    3. Drop post-resolve zombies with adaptive grace (Phase 19v22).
    4. Drop events whose names match the deadline pattern.
    5. Drop events without any tradeable tickers.

Inputs:
    events: list of raw Kalshi event dicts (gamma-api shape)
    diag:   optional counter dict — fills 'kalshi_in', 'kalshi_skip_*',
            'kalshi_pass'.

Returns:
    (candidates, tickers) — list of (event, [ticker]) tuples and the
    flat list of tickers ready for orderbook fetch.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from radar.filters._helpers import (
    compute_adaptive_grace_minutes,
    is_deadline,
    is_within_10_days,
)


def filter_kalshi(
    events: list[dict[str, Any]],
    diag: Optional[dict[str, int]] = None,
) -> tuple[list[tuple[dict[str, Any], list[str]]], list[str]]:
    """See module docstring."""
    if diag is None:
        diag = {}
    diag['kalshi_in'] = len(events)
    for k in ('kalshi_skip_lt2_markets', 'kalshi_skip_no_window',
              'kalshi_skip_deadline_text', 'kalshi_skip_no_tickers',
              'kalshi_pass', 'kalshi_skip_past_resolve'):
        diag.setdefault(k, 0)

    candidates: list[tuple[dict[str, Any], list[str]]] = []
    tickers: list[str] = []

    for ev in events:
        markets = ev.get('markets', [])
        if len(markets) < 2:
            diag['kalshi_skip_lt2_markets'] += 1
            continue

        # 10-day window guard
        close_time = (markets[0].get('close_time')
                      or markets[0].get('expected_expiration_time'))
        if not is_within_10_days(date_str=close_time):
            diag['kalshi_skip_no_window'] += 1
            continue

        # Phase 19v22 — adaptive grace gate (parity with Polymarket
        # Phase 9kkk #41, Limitless Phase 19v17, SX Phase 14a). Kalshi
        # exposes hourly weather + intraday polls; when their
        # `status=open` filter lags behind `close_time` (edge cache,
        # momentary refresh delay), the radar treated those as live and
        # could produce phantom arbs with collapsed prices on losing
        # outcomes. Apply per-event adaptive grace by duration.
        try:
            if isinstance(close_time, str) and close_time:
                _ds = (close_time[:-1] + '+00:00') if close_time.endswith('Z') else close_time
                if len(_ds) == 10:
                    _ds += 'T00:00:00+00:00'
                end_dt = datetime.fromisoformat(_ds)
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)
                age_min = (datetime.now(timezone.utc) - end_dt).total_seconds() / 60.0
                if age_min > 0:
                    duration_s = None
                    open_time = (markets[0].get('open_time')
                                  or markets[0].get('expected_open_time'))
                    if isinstance(open_time, str) and open_time:
                        try:
                            _ots = (open_time[:-1] + '+00:00') if open_time.endswith('Z') else open_time
                            if len(_ots) == 10:
                                _ots += 'T00:00:00+00:00'
                            open_dt = datetime.fromisoformat(_ots)
                            if open_dt.tzinfo is None:
                                open_dt = open_dt.replace(tzinfo=timezone.utc)
                            duration_s = (end_dt - open_dt).total_seconds()
                        except Exception:
                            duration_s = None
                    title_for_grace = ev.get('title') or markets[0].get('title') or ''
                    grace_min = compute_adaptive_grace_minutes(
                        duration_seconds=duration_s, title=title_for_grace)
                    if age_min > grace_min:
                        diag['kalshi_skip_past_resolve'] += 1
                        continue
        except Exception:
            # Fail-open: don't block events on parse error
            pass

        names = [m.get('title', m.get('ticker', '?')) for m in markets]
        if is_deadline(names):
            diag['kalshi_skip_deadline_text'] += 1
            continue
        ev_tickers: list[str] = []
        for m in markets:
            t = m.get('ticker')
            if t:
                ev_tickers.append(t)
                tickers.append(t)
        if len(ev_tickers) >= 2:
            candidates.append((ev, ev_tickers))
            diag['kalshi_pass'] += 1
        else:
            diag['kalshi_skip_no_tickers'] += 1

    return candidates, tickers
