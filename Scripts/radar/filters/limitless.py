"""Limitless Exchange event filter.

Extracted from arb_server.py::filter_limitless in audit-28b cont
(27.05.2026). Parity with filter_poly + Limitless-specific deadline
formats (Unix ms timestamps + ISO strings + nested fields).

Filter responsibilities:
    1. Title blacklist (operator-controlled).
    2. Calendar window via deadline / expirationTimestamp.
    3. Event-level closed/paused gate.
    4. Past-resolve adaptive grace (Phase 19v17 — ETH 24h-stale phantom).
    5. Title-pattern deadline reject.
    6. Per-child closed/paused gate (Phase 14a).
    7. Quarantine via Limitless `isOther` flag OR has_other_outcome heuristic.

Inputs:
    events: list of raw Limitless event dicts.
    diag:   optional counter dict.
    blacklist: optional title set; defaults to arb_server.blacklist.

Returns:
    list of (event, is_quarantine) tuples.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from radar.filters._helpers import (
    compute_adaptive_grace_minutes,
    has_other_outcome,
    is_deadline,
    is_within_10_days,
)


def filter_limitless(
    events: list[dict[str, Any]],
    diag: Optional[dict[str, int]] = None,
    blacklist: Optional[set[str]] = None,
) -> list[tuple[dict[str, Any], bool]]:
    """See module docstring."""
    if diag is None:
        diag = {}
    if blacklist is None:
        try:
            import arb_server as _radar
            blacklist = _radar.blacklist
        except Exception:
            blacklist = set()
    diag['lim_in'] = len(events)
    for k in ('lim_skip_blacklist', 'lim_skip_no_window', 'lim_skip_deadline_text',
              'lim_pass', 'lim_quarantine', 'lim_skip_outcome_closed',
              'lim_skip_past_resolve'):
        diag.setdefault(k, 0)

    out: list[tuple[dict[str, Any], bool]] = []
    for ev in events:
        title = ev.get('title') or ev.get('proxyTitle') or '?'
        if title in blacklist:
            diag['lim_skip_blacklist'] += 1
            continue

        # ── Event-level status gate (Phase 9h) ────────────────────────
        ev_status = (ev.get('status') or '').upper()
        ev_closed = (ev.get('expired') or ev.get('hidden')
                     or ev_status in ('CLOSED', 'RESOLVED', 'PAUSED', 'SUSPENDED'))
        if ev_closed:
            diag['lim_skip_outcome_closed'] += 1
            continue

        # ── Calendar window ───────────────────────────────────────────
        deadline = ev.get('deadline') or ev.get('expirationTimestamp')
        if isinstance(deadline, (int, float)):
            ts = deadline / 1000 if deadline > 1e12 else deadline
            if not is_within_10_days(timestamp=ts):
                diag['lim_skip_no_window'] += 1
                continue
        elif isinstance(deadline, str):
            if not is_within_10_days(date_str=deadline):
                diag['lim_skip_no_window'] += 1
                continue
        else:
            diag['lim_skip_no_window'] += 1
            continue

        # ── Past-resolve adaptive grace (Phase 19v17) ─────────────────
        try:
            end_dt: Optional[datetime] = None
            if isinstance(deadline, (int, float)):
                _ts = deadline / 1000 if deadline > 1e12 else deadline
                end_dt = datetime.fromtimestamp(_ts, tz=timezone.utc)
            elif isinstance(deadline, str):
                _ds = (deadline[:-1] + '+00:00') if deadline.endswith('Z') else deadline
                if len(_ds) == 10:
                    _ds += 'T00:00:00+00:00'
                end_dt = datetime.fromisoformat(_ds)
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)
            if end_dt is not None:
                now_utc = datetime.now(timezone.utc)
                age_min = (now_utc - end_dt).total_seconds() / 60.0
                if age_min > 0:
                    duration_s: Optional[float] = None
                    start = ev.get('startDate') or ev.get('startedAt')
                    if isinstance(start, (int, float)):
                        _sts = start / 1000 if start > 1e12 else start
                        duration_s = max(0.0, end_dt.timestamp() - _sts)
                    elif isinstance(start, str):
                        try:
                            _sds = (start[:-1] + '+00:00') if start.endswith('Z') else start
                            if len(_sds) == 10:
                                _sds += 'T00:00:00+00:00'
                            _start_dt = datetime.fromisoformat(_sds)
                            if _start_dt.tzinfo is None:
                                _start_dt = _start_dt.replace(tzinfo=timezone.utc)
                            duration_s = (end_dt - _start_dt).total_seconds()
                        except Exception:
                            duration_s = None
                    grace_min = compute_adaptive_grace_minutes(
                        duration_seconds=duration_s, title=title)
                    if age_min > grace_min:
                        diag['lim_skip_past_resolve'] += 1
                        continue
        except Exception:
            # Defensive: fail-open on parse error
            pass

        # ── Title-pattern deadline reject ─────────────────────────────
        children = ev.get('markets') or []
        names = [c.get('title') or c.get('proxyTitle') or '' for c in children]
        if not names:
            names = [title]
        if is_deadline(names):
            diag['lim_skip_deadline_text'] += 1
            continue

        # ── Per-child status gate (Phase 14a Gap 1) ───────────────────
        if children:
            child_closed = False
            for c in children:
                cs = (c.get('status') or '').upper()
                if (c.get('expired') or c.get('hidden')
                        or cs in ('CLOSED', 'RESOLVED', 'PAUSED', 'SUSPENDED')
                        or c.get('accepting_orders') is False):
                    child_closed = True
                    break
            if child_closed:
                diag['lim_skip_outcome_closed'] += 1
                continue

        # ── Quarantine ────────────────────────────────────────────────
        api_other = bool(ev.get('isOther')) or any(
            bool((c or {}).get('isOther')) for c in children)
        is_quarantine = api_other or has_other_outcome(names + [title])
        if is_quarantine:
            diag['lim_quarantine'] += 1
        out.append((ev, is_quarantine))
        diag['lim_pass'] += 1
    return out
