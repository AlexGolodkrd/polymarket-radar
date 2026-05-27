"""Shared date / window / deadline helpers used by every per-platform filter.

Extracted from arb_server.py in audit-28b (27.05.2026). These helpers
are pure functions of (datetime, env config) — no shared mutable state,
safe to re-import from any filter module.

Public:
    DEADLINE_RE                                — pattern matching "by 2026", "before Jan", etc.
    WINDOW_DAYS / WINDOW_PAST_DAYS             — env-tunable window for is_within_window
    is_deadline(names)                         — does this multi-outcome title look like a poll deadline?
    is_within_window(date_str/timestamp, ...)  — calendar guard
    is_within_10_days(date_str/timestamp)      — wrapper using module defaults
    compute_adaptive_grace_minutes(...)        — post-resolve zombie-event grace

arb_server.py re-imports each name from here for backward compat.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Optional


# ── Tunables ──────────────────────────────────────────────────────
# Operator may override via env without restarting the radar's main
# class. Defaults reflect 29.04.2026 sweet spot: 13 days ahead (covers
# most Polymarket events) + 2 days past (UMA dispute window grace).
WINDOW_DAYS: int = int(os.environ.get('WINDOW_DAYS', '13'))
WINDOW_PAST_DAYS: int = int(os.environ.get('WINDOW_PAST_DAYS', '2'))


DEADLINE_RE: re.Pattern[str] = re.compile(
    r'\b(by|before)\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|'
    r'january|february|march|april|june|july|august|september|october|november|december|'
    r'20\d{2}|end of|q[1-4])',
    re.IGNORECASE,
)


def is_deadline(names: list[str]) -> bool:
    """True if ≥50% of outcome names match the DEADLINE pattern.
    Used to skip poll-style multi-outcome events ("Approved by Dec / Jan / Feb / ...")
    that don't fit the per-platform arb structures."""
    if len(names) < 2:
        return False
    return sum(1 for n in names if DEADLINE_RE.search(n)) >= len(names) * 0.5


def is_within_window(date_str: Optional[str] = None,
                     timestamp: Optional[float] = None,
                     max_days: Optional[int] = None,
                     past_days: Optional[int] = None) -> bool:
    """True iff the event ends within `max_days` ahead OR ended within
    `past_days` behind (still resolving). Both default to module-level
    WINDOW_DAYS / WINDOW_PAST_DAYS."""
    if max_days is None:
        max_days = WINDOW_DAYS
    if past_days is None:
        past_days = WINDOW_PAST_DAYS
    now = datetime.now(timezone.utc)
    try:
        if timestamp:
            dt = datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
        elif date_str:
            if date_str.endswith('Z'):
                date_str = date_str[:-1] + '+00:00'
            elif len(date_str) == 10:
                date_str += 'T00:00:00+00:00'
            dt = datetime.fromisoformat(date_str)
            if not dt.tzinfo:
                dt = dt.replace(tzinfo=timezone.utc)
        else:
            return False

        diff = (dt - now).total_seconds()
        return -86400 * past_days <= diff <= 86400 * max_days
    except Exception:
        return False


def is_within_10_days(date_str: Optional[str] = None,
                      timestamp: Optional[float] = None) -> bool:
    """Back-compat shim — older code paths may still call this name.
    Identical to is_within_window with module defaults."""
    return is_within_window(date_str=date_str, timestamp=timestamp)


def compute_adaptive_grace_minutes(duration_seconds: Optional[float] = None,
                                   title: Optional[str] = None) -> int:
    """Pick grace window based on event duration.

    Used to filter post-resolve zombie events (cached `closed=false` flags
    from the platform's API that lag behind the real resolution event).
    Mirrors the Polymarket Phase 9kkk policy:

        ≤ 10 min  → 1 min   (5-min crypto)
        ≤ 1 h     → 5 min   (hourly events)
        ≤ 24 h    → 30 min  (daily — weather, daily polls)
        > 24 h    → 60 min  (multi-day — UMA dispute window)

    Falls back to title-pattern heuristic when duration unknown.
    """
    if duration_seconds is not None and duration_seconds > 0:
        if duration_seconds <= 600:
            return 1
        if duration_seconds <= 3600:
            return 5
        if duration_seconds <= 86400:
            return 30
        return 60
    # Title heuristic fallback
    title_lower = (title or '').lower()
    intraday_signals = (' 5min', '-5min', '5-min',
                        ' 1min', '-1min', '1-min',
                        'minutely', 'every 5 min', '5min crypto')
    is_intraday_ampm = bool(re.search(
        r'\b\d{1,2}(am|pm)(-\d{1,2}(am|pm))?\s*et\b', title_lower))
    if any(s in title_lower for s in intraday_signals) or is_intraday_ampm:
        return 1
    if 'highest temperature' in title_lower or 'lowest temperature' in title_lower:
        return 30
    return 30  # safer default
