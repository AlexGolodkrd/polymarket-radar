"""Build-deal — пер-арб расчёт ставок, gross/net, grade, slip.

Extracted from arb_server.py in audit-28b cont 2 (27.05.2026). One of
the most-touched functions in the codebase — every accepted arb passes
through here. All historical guards preserved (REAL_OB_SOURCES strict
CLOB, MIN_LEG_LIQ_USD mosquito gate, payout_target ALL_NO fix etc.).

History:
    Phase 9i (28.04.2026)  — payout_target arg for ALL_NO gross math.
    Phase 9q (29.04.2026)  — `/ total_price` normalisation (ALL_NO ×2 inflation fix).
    Phase 9kkk #7 (30.04)  — REAL_OB_SOURCES strict CLOB-only.
    Phase 9yy              — gross_pct formula uses payout_target.
    Phase 10 Task A        — `clob_synthetic` whitelisted in sources.
    Phase 19v6             — MIN_LEG_LIQ_USD mosquito gate.
    Phase 19v13            — risk-tier monotonic ladder (LOW/MED/HIGH/CRIT).
    Phase 19v18            — calc_fee platform-specific.

Dependencies pulled via lazy import to avoid cyclic deps with arb_server:
    BALANCE (default 100.0) — overrideable via env BALANCE
    MAX_PER_TRADE_USD       — from risk module
    MIN_LEG_LIQ_USD         — env or default 5
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Optional

from radar.fees import calc_fee


# Whitelist of orderbook sources we trust for arb sizing. Anything not
# in this set (implied / ws cached / mid) gets rejected at build time —
# strict CLOB-only after Phase 9kkk #43 (30.04.2026).
REAL_OB_SOURCES: frozenset[str] = frozenset({
    'clob_ask',       # Polymarket /book?token_id  (live REST)
    'kalshi_ob',      # Kalshi /markets/{ticker}/orderbook  (live REST)
    'sx_ob',          # SX Bet /orders?marketHashes  (live REST)
    'lim_clob',       # Limitless /markets/{slug}/orderbook  (live REST)
    'clob_synthetic', # Polymarket NO ask synthesized from YES bid (Phase 10 Task A)
})


def _balance() -> float:
    """Operator-tunable balance for sizing math. Default 100.0."""
    try:
        return float(os.environ.get('BALANCE', '100') or '100')
    except (TypeError, ValueError):
        return 100.0


def _max_per_trade_cap() -> float:
    """Per-leg risk cap from `risk.MAX_PER_TRADE_USD`. Safe fallback 55.0."""
    try:
        from risk import MAX_PER_TRADE_USD
        return float(MAX_PER_TRADE_USD)
    except Exception:
        return 55.0


def _min_leg_liq() -> float:
    """Mosquito gate threshold. Default $5."""
    try:
        return float(os.environ.get('MIN_LEG_LIQ_USD', '5'))
    except (TypeError, ValueError):
        return 5.0


def build_deal(
    title: str,
    platform: str,
    outcomes: list[dict[str, Any]],
    total_price: float,
    theta: float,
    threshold: float,
    payout_target: float = 1.0,
) -> Optional[dict[str, Any]]:
    """Build a deal record (sized stakes + grade + economics).

    `payout_target`: $ guaranteed payout per $1 of contracts purchased.
      - ALL_YES (one outcome wins, gets $1): payout_target = 1.0
      - YES_NO_PAIR per market (always pays $1): 1.0
      - ALL_NO with N outcomes (N-1 of them pay $1 each): payout_target = N-1
      - SX Bet binary: 1.0

    Returns deal dict or None if any reject-gate trips (source not REAL,
    leg has zero liquidity, mosquito liquidity, non-profitable net).
    """
    BALANCE = _balance()
    _RISK_PER_TRADE_CAP = _max_per_trade_cap()
    MIN_LEG_LIQ_USD = _min_leg_liq()

    # ── Liquidity / sizing ────────────────────────────────────────
    min_liq = float('inf')
    for o in outcomes:
        liq = o.get('liquidity', 0)
        if liq > 0 and liq < min_liq:
            min_liq = liq
    if min_liq == float('inf'):
        min_liq = 0

    max_share = max(o['price'] / total_price for o in outcomes) if total_price > 0 else 0
    max_theoretical_stake = BALANCE * max_share

    scale_factor = 1.0
    if min_liq > 0 and max_theoretical_stake > min_liq:
        scale_factor = min_liq / max_theoretical_stake
    elif min_liq == 0:
        scale_factor = 0.1  # safety

    # Per-trade risk-cap scale — Phase 9i: cap is per-LEG.
    target_max_leg = _RISK_PER_TRADE_CAP
    if max_share > 0 and BALANCE * scale_factor * max_share > target_max_leg:
        scale_factor = target_max_leg / (BALANCE * max_share)

    actual_balance = BALANCE * scale_factor

    # ── Gross math (Phase 9q fix: `/ total_price` normalisation) ──
    if total_price > 0:
        gross = actual_balance * (payout_target - total_price) / total_price
    else:
        gross = 0.0

    # ── REAL_OB_SOURCES strict guard (Phase 9kkk #7) ──────────────
    for o in outcomes:
        src = o.get('source', '?')
        if src not in REAL_OB_SOURCES:
            return None
        if not (o.get('liquidity') or 0) > 0:
            return None

    # ── Mosquito gate (Phase 19v6) ───────────────────────────────
    if min_liq < MIN_LEG_LIQ_USD:
        return None

    # ── Per-leg entries + fees ───────────────────────────────────
    total_fee: float = 0.0
    entries: list[dict[str, Any]] = []
    for o in outcomes:
        stake = actual_balance * (o['price'] / total_price) if total_price > 0 else 0
        contracts = stake / o['price'] if o['price'] > 0 else 0
        fee = calc_fee(o['price'], contracts, theta, platform=platform)
        total_fee += fee
        entries.append({
            'name': o['name'],
            'price': o['price'],
            'price_cents': round(o['price'] * 100, 1),
            'coeff': round(1 / o['price'], 1) if o['price'] > 0 else 0,
            'stake': round(stake, 2),
            'contracts': round(contracts, 1),
            'fee': round(fee, 4),
            'liquidity': round(o.get('liquidity', 0), 0),
            'share_pct': round(o['price'] / total_price * 100, 1) if total_price > 0 else 0,
            'source': o.get('source', '?'),
        })

    net = gross - total_fee
    if net <= 0:
        return None  # Non-profitable

    roi = net / actual_balance * 100 if actual_balance > 0 else 0
    max_stake = max(e['stake'] for e in entries) if entries else 0
    slip_pct = min(5.0, (max_stake / min_liq) * 100) if min_liq > 0 and max_stake > 0 else 5.0
    slip_cost = actual_balance * slip_pct / 100
    adj = net - slip_cost
    liq_ok = all(e['liquidity'] >= 50 for e in entries if e['liquidity'] > 0)

    # ── Grade ladder ─────────────────────────────────────────────
    if adj > 20 and liq_ok:
        grade = 'A+'
    elif adj > 10:
        grade = 'A'
    elif adj > 5:
        grade = 'B'
    elif adj > 2:
        grade = 'C'
    elif adj > 0:
        grade = 'D'
    else:
        grade = 'F'

    # ── Risk tier (Phase 19v13 monotonic ladder) ────────────────
    if min_liq > max_stake * 10:
        risk = 'LOW'
    elif min_liq > max_stake * 3:
        risk = 'MED'
    elif min_liq > max_stake:
        risk = 'MED'
    elif min_liq > 0:
        risk = 'HIGH'
    else:
        risk = 'CRIT'

    return {
        'title': title,
        'platform': platform,
        'outcomes': len(outcomes),
        'total_cents': round(total_price * 100, 1),
        'threshold': round(threshold * 100, 0),
        'spread_cents': round((threshold - total_price) * 100, 1),
        'gross': round(gross, 2),
        # Phase 9yy — gross_pct uses payout_target, not 1.0.
        'gross_pct': round((payout_target - total_price) / total_price * 100, 1) if total_price > 0 else 0,
        'fee': round(total_fee, 3),
        'fee_pct': round(total_fee / actual_balance * 100, 2) if actual_balance else 0,
        'net': round(net, 2),
        'roi': round(roi, 1),
        'slip_pct': round(slip_pct, 2),
        'slip_cost': round(slip_cost, 2),
        'adj': round(adj, 2),
        'adj_roi': round(adj / actual_balance * 100, 1) if actual_balance else 0,
        'min_liq': round(min_liq, 0),
        'max_stake': round(max_stake, 2),
        'balance_used': round(actual_balance, 2),
        'liq_ok': liq_ok,
        'grade': grade,
        'risk': risk,
        'theta': theta,
        'entries': entries,
        'scan_time': datetime.now(timezone.utc).isoformat(),
    }
