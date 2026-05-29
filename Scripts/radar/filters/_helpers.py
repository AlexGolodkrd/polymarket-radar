"""Shared date / window / deadline / outcome-name helpers used by every
per-platform filter.

Extracted from arb_server.py in audit-28b (27.05.2026). These helpers
are pure functions of (datetime, env config, string) — no shared
mutable state, safe to re-import from any filter module.

Public:
    DEADLINE_RE                                — pattern matching "by 2026", "before Jan", etc.
    OTHER_RE                                   — pattern matching "Other / Any other / ..." outcomes
    WINDOW_DAYS / WINDOW_PAST_DAYS             — env-tunable window for is_within_window
    is_deadline(names)                         — does this multi-outcome title look like a poll deadline?
    is_within_window(date_str/timestamp, ...)  — calendar guard
    is_within_10_days(date_str/timestamp)      — wrapper using module defaults
    compute_adaptive_grace_minutes(...)        — post-resolve zombie-event grace
    has_other_outcome(names)                   — quarantine flag for hidden 'Other' outcome

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

    Phase audit-29.05 (29.05.2026) — tightened brackets after operator-
    found post-resolve leak (San Francisco Giants MLB +29min after
    end_date, was inside old 30-min daily bracket → looked like phantom
    arb in /api/recent_deals). New policy:

        ≤ 10 min  → 1 min   (5-min crypto — unchanged)
        ≤ 1 h     → 3 min   (was 5; hourly markets settle fast)
        ≤ 24 h    → 15 min  (was 30; MLB/NBA/Premier League games)
        > 24 h    → 30 min  (was 60; multi-day events, UMA dispute lag)

    Rationale: platform APIs typically expose the resolved status within
    5-10 min of the actual resolution event. Anything past 15-30 min is
    almost certainly stale data, not lagging resolution. Operators of
    real-money mode want zero post-resolve fires; the previous brackets
    were too generous and let 0.2% slip through.

    Env-overridable via title-heuristic fallback.
    """
    if duration_seconds is not None and duration_seconds > 0:
        if duration_seconds <= 600:
            return 1
        if duration_seconds <= 3600:
            return 3
        if duration_seconds <= 86400:
            return 15
        return 30
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
        return 15
    return 15  # safer default (was 30 — same tighten-from-30 rationale)


# ── "Other"-outcome detector ──────────────────────────────────────────
# Multi-outcome events with a hidden "Other" / "None of the above" option
# are vulnerable arbs: if we buy YES on A,B,C only and "Other" actually wins,
# every leg loses. Filters quarantine such deals (still show in UI for
# analysis, but block the executor from firing them). Pattern covers EN +
# RU phrasing seen across Polymarket / Limitless titles.

OTHER_RE: re.Pattern[str] = re.compile(
    r'\b(other|any other|none of the above|other team|other candidate|other player|'
    r'another\s+(?:candidate|player|person|team|option|nominee|contender|entrant)|'
    r'someone\s+else|will\s+a\s+different|'
    r'прочее|другое|неопределен|любой другой|'
    r'(?:другой|иной)\s+(?:кандидат|игрок|вариант))\b',
    re.IGNORECASE,
)


def has_other_outcome(names: list[str]) -> bool:
    """True if any name matches the 'Other' pattern.

    Phase 9kkk (30.04.2026): also matches `groupItemTitle == 'Other'`
    exact label as a safety net — Polymarket sometimes leaves the
    question in a misleading form while explicitly tagging the GT.
    """
    for n in names:
        if not n:
            continue
        s = str(n).strip()
        if s.lower() in ('other', 'другое', 'иное', 'остальные'):
            return True
        if OTHER_RE.search(s):
            return True
    return False
