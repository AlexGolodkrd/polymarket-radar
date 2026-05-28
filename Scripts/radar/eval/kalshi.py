"""Kalshi 3-structure evaluator (A/B/C).

Extracted from arb_server.py::eval_kalshi in audit-28b cont 3 (28.05.2026).
Kalshi is disabled by default (ENABLE_KALSHI=0 since PR #177 — geo-blocked
from non-US VPS), but the evaluator stays implemented for tests + the
eventual US-VPS pivot.

Three arb structures (per Phase 1):
    A. ALL_YES — Σ yes_ask < THRESH_KALSHI                  (multi-outcome only)
    B. ALL_NO  — Σ no_ask  < (N-1) × THRESH_KALSHI          (multi-outcome only)
    C. YES_NO_PAIR per market — yes_ask + no_ask < THRESH_KALSHI

Coverage rule (Phase 9g, 28.04.2026): ALL_YES and ALL_NO must price
EVERY outcome of the event. If filter dropped any outcome, structures
A/B are silently rejected (would otherwise over-count). Standalone
YES_NO_PAIR safe per-market.
"""
from __future__ import annotations

from typing import Any

from radar.build_deal import build_deal


def eval_kalshi(
    cands: list[tuple[dict[str, Any], list[str]]],
    kalshi_res: dict[str, tuple[Any, Any, Any, Any]],
    thresh_kalshi: float,
    theta_kalshi: float,
) -> list[dict[str, Any]]:
    """See module docstring."""
    deals: list[dict[str, Any]] = []

    for cand in cands:
        ev, tickers = cand
        # Kalshi event-level close_time, fallback to per-market field below
        end_date = ev.get('close_time') or ev.get('expected_expiration_time')
        total_outcomes_on_event = len(ev.get('markets') or [])

        per_market: list[dict[str, Any]] = []
        for m in ev.get('markets', []):
            t = m.get('ticker', '')
            if t not in kalshi_res:
                continue
            yes_ask, yes_depth, no_ask, no_depth = kalshi_res[t]
            if yes_ask is None or yes_ask < 0.05 or yes_ask >= 1:
                continue
            per_market.append({
                'name': m.get('title', t), 'ticker': t,
                'yes_price': yes_ask, 'yes_liq': yes_depth,
                'no_price': no_ask if (no_ask and 0 < no_ask < 1) else None,
                'no_liq': no_depth or 0,
                'end_date': m.get('close_time') or end_date,
            })
        if len(per_market) < 2:
            continue
        full_coverage = (len(per_market) == total_outcomes_on_event)

        # ── A. ALL_YES ──────────────────────────────────────────────
        yes_outcomes = [{'name': p['name'], 'price': p['yes_price'],
                         'liquidity': p['yes_liq'], 'source': 'kalshi_ob'}
                        for p in per_market]
        total_yes = sum(o['price'] for o in yes_outcomes)
        if (full_coverage and 0.50 <= total_yes < thresh_kalshi
                and any(o['price'] > 0.20 for o in yes_outcomes)):
            d = build_deal(ev.get('title', '?'), 'Kalshi', yes_outcomes,
                           total_yes, theta_kalshi, thresh_kalshi)
            if d:
                d['arb_structure'] = 'all_yes'
                d['end_date'] = end_date
                deals.append(d)

        # ── B. ALL_NO (N >= 3) — coverage required ──────────────────
        no_raw = [p for p in per_market if p['no_price'] is not None]
        N = len(no_raw)
        if N >= 3 and N == total_outcomes_on_event:
            no_outcomes = [{'name': f"NO {p['name']}", 'price': p['no_price'],
                            'liquidity': p['no_liq'], 'source': 'kalshi_ob'}
                           for p in no_raw]
            total_no = sum(o['price'] for o in no_outcomes)
            no_threshold = (N - 1) * thresh_kalshi
            if total_no < no_threshold:
                d = build_deal(ev.get('title', '?') + ' (ALL_NO)', 'Kalshi',
                               no_outcomes, total_no, theta_kalshi, no_threshold,
                               payout_target=float(N - 1))
                if d:
                    d['arb_structure'] = 'all_no'
                    d['payout_target'] = N - 1
                    d['end_date'] = end_date
                    deals.append(d)

        # ── C. YES_NO_PAIR ──────────────────────────────────────────
        for p in per_market:
            if p['no_price'] is None:
                continue
            pair_total = p['yes_price'] + p['no_price']
            if pair_total >= thresh_kalshi:
                continue
            pair_out = [
                {'name': f"YES {p['name']}", 'price': p['yes_price'],
                 'liquidity': p['yes_liq'], 'source': 'kalshi_ob'},
                {'name': f"NO {p['name']}", 'price': p['no_price'],
                 'liquidity': p['no_liq'], 'source': 'kalshi_ob'},
            ]
            d = build_deal(p['name'], 'Kalshi', pair_out, pair_total,
                           theta_kalshi, thresh_kalshi)
            if d:
                d['arb_structure'] = 'yes_no_pair'
                d['end_date'] = p.get('end_date')
                deals.append(d)
    return deals
