"""SX Bet evaluator — one binary deal per market hash.

Extracted from arb_server.py::eval_sx in audit-28b cont 3 (28.05.2026).
SX Bet markets are inherently binary (outcomeOne vs outcomeTwo). A single
match can have Moneyline + Total + Spread + Period markets — each is an
independent binary arb opportunity, evaluated separately.

History:
    Phase 9kkk (30.04.2026) — status filter (1=open / 2=closed / 3=settled / 4=cancelled).
    Phase 12b              — fail-CLOSED on missing status (was fail-OPEN).
    Phase 14a Gap 2        — adaptive post-resolve grace.
    Phase 19v9             — string 'ACTIVE' status accepted (SX API format change).

`eval_sx_3way` остаётся stub — type=1 markets (3-way soccer with Draw)
исключены через SX_BINARY_TYPES; 3-way pipeline ещё не wired.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from radar.build_deal import build_deal
from radar.filters._helpers import compute_adaptive_grace_minutes, is_within_10_days
from radar.filters.sx import _sx_market_title


_ACTIVE_STATUSES: set[Any] = {1, 'ACTIVE', 'active'}


def eval_sx(
    sx_markets: list[dict[str, Any]],
    sx_orders: dict[str, tuple[Any, Any, Any, Any]],
    sx_binary_types: set[int],
    thresh_sx: float,
    theta_sx: float,
) -> list[dict[str, Any]]:
    """Evaluate SX binary markets for arb opportunities.

    Args:
        sx_markets: raw SX market dicts from /markets/active.
        sx_orders:  marketHash → (best1, depth1, best2, depth2) from _fetch_sx_orders.
        sx_binary_types: set of SX market `type` values that are binary (no Draw).
        thresh_sx:  Σ best_taker_prices must be < this to qualify.
        theta_sx:   per-leg fee rate (decimal, e.g. 0.02 for 2%).

    Returns:
        list of deal dicts with `arb_structure='binary'` and `end_date`
        populated from gameTime.
    """
    deals: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()

    for m in sx_markets:
        if m.get('type') not in sx_binary_types:
            continue
        mh = m.get('marketHash', '')
        if not mh or mh in seen_hashes:
            continue
        seen_hashes.add(mh)

        # Status filter (Phase 9kkk + 12b + 19v9): active markets only.
        status = m.get('status')
        if status not in _ACTIVE_STATUSES:
            continue
        if m.get('outcome') is not None and m.get('outcome') != 0:
            continue

        # Calendar window via gameTime (unix seconds).
        if not is_within_10_days(timestamp=m.get('gameTime')):
            continue

        # Adaptive post-resolve grace (Phase 14a Gap 2).
        game_ts = m.get('gameTime')
        if isinstance(game_ts, (int, float)) and game_ts > 0:
            now_ts = time.time()
            age_seconds = now_ts - game_ts
            if age_seconds > 0:
                title = _sx_market_title(m)
                grace_min = compute_adaptive_grace_minutes(
                    duration_seconds=None, title=title)
                if (age_seconds / 60) > grace_min:
                    continue

        if mh not in sx_orders:
            continue
        best1, depth1, best2, depth2 = sx_orders[mh]
        if best1 is None or best2 is None:
            continue
        if best1 <= 0 or best2 <= 0:
            continue
        total = best1 + best2
        if total >= thresh_sx:
            continue

        outcomes = [
            {'name': m.get('outcomeOneName', 'Team 1'), 'price': best1,
             'liquidity': depth1, 'source': 'sx_ob'},
            {'name': m.get('outcomeTwoName', 'Team 2'), 'price': best2,
             'liquidity': depth2, 'source': 'sx_ob'},
        ]
        deal = build_deal(_sx_market_title(m), 'SX Bet', outcomes, total,
                          theta_sx, thresh_sx)
        if deal:
            deal['arb_structure'] = 'binary'
            if isinstance(game_ts, (int, float)) and game_ts > 0:
                deal['end_date'] = datetime.fromtimestamp(
                    game_ts, tz=timezone.utc).isoformat()
            deals.append(deal)
    return deals


def eval_sx_3way(
    sx_markets: list[dict[str, Any]],
    sx_orders: dict[str, Any],
    sx_three_way_types: set[int],
) -> list[dict[str, Any]]:
    """STUB — full 3-way 1X2 implementation pending SX orderbook semantics."""
    deals: list[dict[str, Any]] = []
    for m in sx_markets:
        if m.get('type') not in sx_three_way_types:
            continue
        # Future: fetch 3 outcomes' best taker prices, sum, compare to threshold.
        pass
    return deals
