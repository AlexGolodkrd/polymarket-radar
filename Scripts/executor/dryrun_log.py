"""Dry-run logging + post-hoc realistic-fill evaluator.

Two log streams:
    Executions/dryrun.jsonl       — every fired (or dry-fired) order leg + the
                                     top-level arb decision row.
    Executions/paper_results.jsonl — appended ~5s after each arb decision once
                                     the realistic evaluator re-fetches the
                                     orderbook and computes "what would the
                                     real fill look like + what's the realised
                                     P&L drift". This is the foundation for
                                     Phase 5 paper-trading metrics.

Realistic eval is scheduled on a daemon thread per decision so the firer
returns immediately. We never block the radar's hot path.
"""
import json
import logging
import os
import threading
import time
from collections import Counter
from typing import Optional

import requests

log = logging.getLogger(__name__)

# Project-relative — Executions/ is in repo root, .gitignore'd
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_THIS_DIR, '..', '..'))
EXECUTIONS_DIR = os.path.join(_REPO_ROOT, 'Executions')
DRYRUN_LOG_PATH = os.path.join(EXECUTIONS_DIR, 'dryrun.jsonl')
PAPER_RESULTS_PATH = os.path.join(EXECUTIONS_DIR, 'paper_results.jsonl')

_log_lock = threading.Lock()


def _ensure_dir():
    os.makedirs(EXECUTIONS_DIR, exist_ok=True)


def _append_jsonl(path: str, row: dict):
    _ensure_dir()
    line = json.dumps(row, default=str, ensure_ascii=False)
    with _log_lock:
        with open(path, 'a', encoding='utf-8') as f:
            f.write(line + '\n')


def log_order_decision(arb_id: str, leg_idx: int, built: dict, bot_id: str):
    """Per-leg log line — written from inside the parallel fire.

    Phase 7: SX Bet adds a `sx_match` block (avg fill price, partial flag,
    matched orders, available orders) so post-hoc analysis can see WHY a
    leg partial-filled vs. fully filled. Polymarket / Kalshi don't have
    this concept (single-book CLOB) so the field stays None for them.
    """
    _append_jsonl(DRYRUN_LOG_PATH, {
        'kind': 'leg',
        'arb_id': arb_id,
        'leg_idx': leg_idx,
        'platform': built.get('platform'),
        'expected_price': built.get('expected_price'),
        'expected_size_usdc': built.get('expected_size_usdc'),
        'bot_id': bot_id,
        'would_post_url': built.get('would_post_url'),
        'body': built.get('body'),
        'sx_match': built.get('sx_match'),
        'partial_fill': built.get('partial_fill', False),
        'ts': time.time(),
    })


def log_decision(result):
    """Top-level arb decision summary — one line per fire_arb call.

    Phase 7: includes partial-leg counts + worst shortfall so the dashboard
    can surface SX Bet liquidity issues separately from regular dry-fires.
    """
    partial_legs = [l for l in result.legs if l.status == 'partial']
    worst_shortfall = max(
        ((l.extra or {}).get('shortfall_usdc') or 0 for l in partial_legs),
        default=0,
    )
    _append_jsonl(DRYRUN_LOG_PATH, {
        'kind': 'arb',
        'arb_id': result.arb_id,
        'title': result.deal_title,
        'structure': result.deal_structure,
        'expected_cost': result.expected_total_cost_usdc,
        'expected_payout': result.expected_payout_usdc,
        'sim_pnl': result.expected_payout_usdc - result.expected_total_cost_usdc,
        'dry_run': result.dry_run,
        'aborted_reason': result.aborted_reason,
        'leg_count': len(result.legs),
        'leg_status_counts': dict(Counter(l.status for l in result.legs)),
        'partial_leg_count': len(partial_legs),
        'worst_partial_shortfall_usdc': worst_shortfall,
        'fired_at': result.fired_at_unix,
    })


# ── Realistic-fill evaluator ────────────────────────────────────────
def _refetch_poly_ask(token_id: str) -> Optional[float]:
    try:
        r = requests.get(f"https://clob.polymarket.com/book?token_id={token_id}", timeout=4)
        asks = r.json().get('asks', [])
        if not asks: return None
        return float(min(asks, key=lambda a: float(a.get('price', 999)))['price'])
    except Exception:
        return None


def _refetch_sx_taker_ask(market_hash: str, outcome: int) -> Optional[float]:
    """Same maker→taker conversion used by arb_server._fetch_sx_orders."""
    try:
        r = requests.get(f"https://api.sx.bet/orders?marketHashes={market_hash}&maker=true",
                         timeout=4)
        orders = r.json().get('data', {}).get('orders', [])
        # taker buys outcome X iff maker is on the OTHER side
        max_other = None
        for o in orders:
            p = float(o.get('percentageOdds', '0')) / 1e20
            if not (0 < p < 1): continue
            maker_on_one = o.get('isMakerBettingOutcomeOne', True)
            if outcome == 1 and not maker_on_one:
                if max_other is None or p > max_other: max_other = p
            elif outcome == 2 and maker_on_one:
                if max_other is None or p > max_other: max_other = p
        return (1 - max_other) if max_other is not None else None
    except Exception:
        return None


def _evaluate_realistic_fill(result, deal: dict):
    """Re-fetch the orderbook ~5s after the dry-fire and compute realistic
    fill prices. Writes a paper_results.jsonl row with fill drift metrics.

    For Phase 5 graduation gate: we need win rate >= 70% and drift <= 20%
    over 100 such rows before flipping DRY_RUN off.
    """
    rows_per_leg = []
    for leg in result.legs:
        if leg.status != 'dry-fired':
            rows_per_leg.append({'leg_idx': leg.leg_idx, 'realistic_fill': None,
                                 'reason': leg.status})
            continue
        entry = deal['entries'][leg.leg_idx]
        platform = deal['platform']
        realistic = None
        if platform == 'Polymarket':
            tid = entry.get('token_id') or entry.get('token_id_yes')
            if tid: realistic = _refetch_poly_ask(tid)
        elif platform == 'SX Bet':
            mh = deal.get('market_hash') or entry.get('market_hash')
            outcome = entry.get('outcome_index')
            if mh and outcome in (1, 2):
                realistic = _refetch_sx_taker_ask(mh, outcome)
        # Kalshi disabled — leg.status would be 'disabled', skipped above
        rows_per_leg.append({
            'leg_idx': leg.leg_idx,
            'expected_price': leg.expected_price,
            'realistic_fill': realistic,
            'slippage': (realistic - leg.expected_price) if realistic is not None else None,
        })

    # Phase 19v14 (05.05.2026) — `contracts` is populated by build_deal in
    # arb_server.py but cross-platform deals (cross_platform.py /
    # to_radar_deal_format) only set `stake` + `price`. Without a fallback
    # this raises `KeyError: 'contracts'` for every CP deal — caught by
    # `_worker`'s outer `except Exception`, logged at warning, and the
    # paper_results.jsonl row is silently dropped. Result: graduation gate
    # judges only on per-platform deals; CP arbs never count.
    def _contracts_of(entry):
        c = entry.get('contracts')
        if c is not None:
            return float(c)
        stake = float(entry.get('stake') or 0)
        price = float(entry.get('price') or 0)
        return (stake / price) if price > 0 else 0.0

    # Phase 19v16 (05.05.2026) — old filter `if realistic_fill is not None
    # or expected_price` skipped aborted/disabled leg rows (rows lacking
    # `expected_price` because they only have `realistic_fill: None,
    # reason: leg.status`). For an arb with one disabled leg, those legs
    # contributed $0 to realistic_total, so realistic_pnl = expected_payout
    # − (cost of only the dry-fired legs) became artificially POSITIVE.
    # Graduation gate then saw fake wins → premature live trading. Fix:
    # always include leg cost using leg.expected_price as fallback (we
    # know it from the LegResult even when the row dict lacks it).
    leg_by_idx = {l.leg_idx: l for l in result.legs}

    def _row_cost(r):
        idx = r['leg_idx']
        leg = leg_by_idx.get(idx)
        # Prefer realistic fill, else row-level expected, else leg expected
        price = (r.get('realistic_fill')
                  or r.get('expected_price')
                  or (leg.expected_price if leg else 0))
        return float(price or 0) * _contracts_of(deal['entries'][idx])

    realistic_total = sum(_row_cost(r) for r in rows_per_leg)
    # Coarse "realistic P&L" — whether we'd have crossed the threshold with
    # realistic fills (assumes one outcome wins paying $1×N or payout_target).
    realistic_pnl = result.expected_payout_usdc - realistic_total

    _append_jsonl(PAPER_RESULTS_PATH, {
        'arb_id': result.arb_id,
        'title': result.deal_title,
        'structure': result.deal_structure,
        'sim_pnl': result.expected_payout_usdc - result.expected_total_cost_usdc,
        'realistic_pnl_5s': realistic_pnl,
        'drift': realistic_pnl - (result.expected_payout_usdc - result.expected_total_cost_usdc),
        'legs': rows_per_leg,
        'dry_fired_at': result.fired_at_unix,
        'evaluated_at': time.time(),
    })


def schedule_realistic_eval(result, deal: dict, delay_s: float = 5.0):
    """Spawn a daemon thread that waits `delay_s`, refetches the orderbook
    and writes a paper_results row. We don't gate on completion — radar
    keeps scanning while this runs."""
    def _worker():
        try:
            time.sleep(delay_s)
            _evaluate_realistic_fill(result, deal)
        except Exception as e:
            log.warning("realistic-fill eval failed for %s: %s", result.arb_id, e)
    t = threading.Thread(target=_worker, daemon=True,
                         name=f"realistic-eval-{result.arb_id[:24]}")
    t.start()


# ── Aggregate stats for dashboard / Phase 5 graduation gate ─────────
def paper_stats(window_n: int = 100) -> dict:
    """Read up to last `window_n` rows from paper_results.jsonl and compute
    rolling win rate + mean slippage + drift. Used by /api/paper_stats and
    the Phase 5 graduation gate (>=70% win rate, <=20% drift)."""
    if not os.path.exists(PAPER_RESULTS_PATH):
        return {'count': 0, 'win_rate_pct': None, 'mean_drift': None,
                'mean_slippage_cents': None}
    rows = []
    with open(PAPER_RESULTS_PATH, 'r', encoding='utf-8') as f:
        for line in f:
            try: rows.append(json.loads(line))
            except: continue
    rows = rows[-window_n:]
    n = len(rows)
    if n == 0:
        return {'count': 0, 'win_rate_pct': None, 'mean_drift': None,
                'mean_slippage_cents': None}
    wins = sum(1 for r in rows if (r.get('realistic_pnl_5s') or 0) > 0)
    drifts = [r.get('drift') for r in rows if r.get('drift') is not None]
    slips = [s.get('slippage') for r in rows for s in r.get('legs', [])
             if s.get('slippage') is not None]
    return {
        'count': n,
        'win_rate_pct': round(100 * wins / n, 1),
        'mean_drift': round(sum(drifts) / len(drifts), 4) if drifts else None,
        'mean_slippage_cents': round(100 * sum(slips) / len(slips), 3) if slips else None,
        'graduation_ready': (n >= 100
                              and wins / n >= 0.70
                              and (sum(drifts) / len(drifts) if drifts else 1) <= 0.20),
    }
