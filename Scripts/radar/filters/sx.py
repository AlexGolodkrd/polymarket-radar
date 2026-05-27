"""SX Bet market filter.

Extracted from arb_server.py::filter_sx in audit-28b cont (27.05.2026).
SX-specific: status field is STRING ('ACTIVE') not int (1) (Phase 19v9
critical fix); gameTime is Unix seconds; markets are inherently binary.

Filter responsibilities:
    1. Status ∈ {1, 'ACTIVE', 'active'} — drop paused/settled/cancelled.
    2. Outcome == 0 (or None) — drop markets that already resolved.
    3. Title blacklist.
    4. Calendar window via gameTime (Unix seconds).
    5. Past-resolve adaptive grace.
    6. Title-pattern deadline reject.

Inputs:
    markets: raw SX market dicts from /markets/active.
    diag:    optional counter dict.
    blacklist: optional title set; defaults to arb_server.blacklist.

Returns:
    list of SX market dicts (unchanged shape) that passed all gates.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from radar.filters._helpers import (
    compute_adaptive_grace_minutes,
    is_deadline,
    is_within_10_days,
)


# SX status values that mean "tradeable now". Phase 19v9 (03.05.2026)
# discovered SX migrated from int (1) to string ('ACTIVE') — without
# accepting both shapes, EVERY SX market was rejected in production.
_ACTIVE_STATUSES: set[Any] = {1, 'ACTIVE', 'active'}


def _sx_market_title(m: dict[str, Any]) -> str:
    """Pretty title disambiguating Moneyline vs Total vs Spread for the
    same matchup. Uses outcomeOneName/outcomeTwoName (carry Over/Under
    and ±line annotations)."""
    league = m.get('leagueLabel', '')
    o1 = m.get('outcomeOneName', m.get('teamOneName', 'Team 1'))
    o2 = m.get('outcomeTwoName', m.get('teamTwoName', 'Team 2'))
    return f"{o1} vs {o2} ({league})" if league else f"{o1} vs {o2}"


def filter_sx(
    markets: list[dict[str, Any]],
    diag: Optional[dict[str, int]] = None,
    blacklist: Optional[set[str]] = None,
) -> list[dict[str, Any]]:
    """See module docstring."""
    if diag is None:
        diag = {}
    if blacklist is None:
        try:
            import arb_server as _radar
            blacklist = _radar.blacklist
        except Exception:
            blacklist = set()
    diag['sx_in'] = len(markets)
    for k in ('sx_skip_blacklist', 'sx_skip_status', 'sx_skip_no_window',
              'sx_skip_past_resolve', 'sx_skip_deadline_text', 'sx_pass'):
        diag.setdefault(k, 0)

    out: list[dict[str, Any]] = []
    now_ts = time.time()

    for m in markets:
        # ── Status gate ───────────────────────────────────────────────
        status = m.get('status')
        if status not in _ACTIVE_STATUSES:
            diag['sx_skip_status'] += 1
            continue
        outcome = m.get('outcome')
        if outcome is not None and outcome != 0:
            diag['sx_skip_status'] += 1
            continue

        # ── Blacklist ─────────────────────────────────────────────────
        title = _sx_market_title(m)
        if title in blacklist:
            diag['sx_skip_blacklist'] += 1
            continue

        # ── Calendar window via gameTime (Unix seconds) ───────────────
        gt = m.get('gameTime')
        if not is_within_10_days(timestamp=gt):
            diag['sx_skip_no_window'] += 1
            continue

        # ── Past-resolve adaptive grace ───────────────────────────────
        if isinstance(gt, (int, float)) and gt > 0:
            age_seconds = now_ts - gt
            if age_seconds > 0:
                grace_min = compute_adaptive_grace_minutes(
                    duration_seconds=None, title=title)
                if (age_seconds / 60) > grace_min:
                    diag['sx_skip_past_resolve'] += 1
                    continue

        # ── Title-pattern deadline reject ─────────────────────────────
        if is_deadline([title]):
            diag['sx_skip_deadline_text'] += 1
            continue

        out.append(m)
        diag['sx_pass'] += 1
    return out
