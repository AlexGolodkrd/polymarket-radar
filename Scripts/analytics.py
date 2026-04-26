"""
Analytics: lifecycle tracking and P&L aggregation for the arbitrage radar.

The radar shows opportunities, it does not (yet) place orders. So we track
two layers of P&L in parallel:

  - **Sim P&L**:  every opportunity the radar surfaces is treated as if we
                  entered $100. Sums net at the moment the deal first appears.
                  Useful for evaluating the strategy.
  - **Real P&L**: only deals the user explicitly marked "took" via the UI.
                  Sums net at the moment of decision. Reflects actual money.

Storage layout (under project_root/Executions):

  analytics_events.jsonl
      Append-only log. One JSON object per line. Types:
        {"type":"opened",   "ts":..., "key":..., "platform":..., "title":...,
         "sum_cents":..., "net":..., "grade":..., "min_liq":..., "balance_used":...}
        {"type":"closed",   "ts":..., "key":..., "duration_sec":...}
        {"type":"decision", "ts":..., "key":..., "decision":"took"|"skipped",
         "net_at_decision":..., "balance_at_decision":...}

  analytics_state.json
      Current set of currently-open deals (key -> snapshot at open time +
      latest snapshot + any decision). Survives server restart so we don't
      double-count "opened" events on every boot.

Concurrency: all public functions take an internal lock. Designed to be
called from the scan thread, micro-scan threads, and Flask request handlers
without surprises.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Iterable, Optional


# ── Paths ────────────────────────────────────────────────
_BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'Executions'))
EVENTS_PATH = os.path.join(_BASE_DIR, 'analytics_events.jsonl')
STATE_PATH = os.path.join(_BASE_DIR, 'analytics_state.json')

# ── State ────────────────────────────────────────────────
_lock = threading.RLock()
_open_deals: dict = {}    # key -> {opened_ts, last_seen_ts, snapshot, decision, decision_ts, net_at_decision}
_loaded = False


# ── Public API ───────────────────────────────────────────
def deal_key(deal: dict) -> str:
    """Stable identifier for dedup across scans."""
    return f"{deal.get('platform','?')}::{deal.get('title','?')}"


def init() -> None:
    """Load persisted state from disk. Safe to call multiple times."""
    global _loaded
    with _lock:
        if _loaded:
            return
        os.makedirs(_BASE_DIR, exist_ok=True)
        if os.path.exists(STATE_PATH):
            try:
                with open(STATE_PATH, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                _open_deals.update(data.get('open_deals') or {})
            except Exception as e:
                print(f"[analytics] failed to load state: {e}", flush=True)
        _loaded = True


def update_from_scan(deals: Iterable[dict]) -> None:
    """Compare current `deals` against tracked open set.
       New keys -> log 'opened'; missing keys -> log 'closed'."""
    init()
    now = time.time()
    new_keys = set()
    snapshots = {}
    for d in deals:
        k = deal_key(d)
        new_keys.add(k)
        snapshots[k] = _snapshot(d)

    with _lock:
        # Detect newly opened
        opened_keys = new_keys - set(_open_deals.keys())
        for k in opened_keys:
            snap = snapshots[k]
            _open_deals[k] = {
                'opened_ts': now,
                'last_seen_ts': now,
                'snapshot': snap,
                'decision': None,
                'decision_ts': None,
                'net_at_decision': None,
                'balance_at_decision': None,
            }
            _append_event({'type': 'opened', 'ts': now, 'key': k, **snap})

        # Update last_seen + snapshot for ones that are still around
        for k in new_keys & set(_open_deals.keys()):
            _open_deals[k]['last_seen_ts'] = now
            _open_deals[k]['snapshot'] = snapshots[k]

        # Detect closed (was tracked, missing now)
        closed_keys = set(_open_deals.keys()) - new_keys
        for k in closed_keys:
            entry = _open_deals.pop(k)
            duration = now - entry['opened_ts']
            _append_event({'type': 'closed', 'ts': now, 'key': k, 'duration_sec': round(duration, 1)})

        _persist_state()


def record_decision(key: str, decision: str) -> dict:
    """User clicked 'took' or 'skipped' for a deal currently open in the UI.
       Returns echo dict (status, current snapshot, etc) for HTTP response."""
    if decision not in ('took', 'skipped'):
        return {'status': 'error', 'reason': 'decision must be took|skipped'}
    init()
    now = time.time()
    with _lock:
        entry = _open_deals.get(key)
        if entry is None:
            # Decision on a closed/unknown deal — log it anyway with no snapshot
            _append_event({'type': 'decision', 'ts': now, 'key': key,
                          'decision': decision, 'net_at_decision': None,
                          'balance_at_decision': None, 'note': 'deal not currently open'})
            return {'status': 'ok', 'note': 'deal not open; decision logged'}

        snap = entry.get('snapshot') or {}
        entry['decision'] = decision
        entry['decision_ts'] = now
        entry['net_at_decision'] = snap.get('net')
        entry['balance_at_decision'] = snap.get('balance_used')
        _append_event({
            'type': 'decision', 'ts': now, 'key': key, 'decision': decision,
            'net_at_decision': snap.get('net'),
            'balance_at_decision': snap.get('balance_used'),
        })
        _persist_state()
        return {'status': 'ok', 'decision': decision, 'key': key}


def aggregate(period: str = 'month') -> dict:
    """Aggregate stats over `period`: 'day', 'week', 'month', 'all'."""
    init()
    cutoff = _period_cutoff(period)
    sim_net_total = 0.0
    real_net_total = 0.0
    sim_count = 0
    taken_count = 0
    skipped_count = 0
    closed_count = 0
    by_platform = defaultdict(lambda: {'sim_net': 0.0, 'real_net': 0.0, 'sim_count': 0, 'taken': 0})
    top_sim = []  # list of (net, key, snap)
    decisions_by_key = {}  # latest decision per key in window

    if not os.path.exists(EVENTS_PATH):
        return _empty_aggregate(period)

    with open(EVENTS_PATH, 'r', encoding='utf-8') as f:
        for line in f:
            try:
                ev = json.loads(line)
            except Exception:
                continue
            ts = ev.get('ts', 0)
            if ts < cutoff:
                continue
            t = ev.get('type')
            if t == 'opened':
                net = float(ev.get('net') or 0)
                platform = ev.get('platform', '?')
                sim_net_total += net
                sim_count += 1
                by_platform[platform]['sim_net'] += net
                by_platform[platform]['sim_count'] += 1
                top_sim.append((net, ev.get('key'), {
                    'platform': platform,
                    'title': ev.get('title', ''),
                    'sum_cents': ev.get('sum_cents'),
                    'grade': ev.get('grade'),
                    'min_liq': ev.get('min_liq'),
                }))
            elif t == 'closed':
                closed_count += 1
            elif t == 'decision':
                decisions_by_key[ev.get('key')] = ev

    # Apply decisions
    for k, ev in decisions_by_key.items():
        dec = ev.get('decision')
        if dec == 'took':
            taken_count += 1
            net = float(ev.get('net_at_decision') or 0)
            real_net_total += net
            # Try to attribute to platform via key prefix
            platform = (k or '::').split('::', 1)[0]
            by_platform[platform]['real_net'] += net
            by_platform[platform]['taken'] += 1
        elif dec == 'skipped':
            skipped_count += 1

    # Top 5 sim deals
    top_sim.sort(key=lambda x: x[0], reverse=True)
    top5 = [{'net': round(n, 2), 'key': k, **snap} for n, k, snap in top_sim[:5]]

    return {
        'period': period,
        'cutoff_ts': cutoff,
        'sim': {
            'count': sim_count,
            'net_total': round(sim_net_total, 2),
            'avg_net': round(sim_net_total / sim_count, 2) if sim_count else 0,
        },
        'real': {
            'taken': taken_count,
            'skipped': skipped_count,
            'net_total': round(real_net_total, 2),
            'avg_net': round(real_net_total / taken_count, 2) if taken_count else 0,
        },
        'closed_count': closed_count,
        'by_platform': {p: {**stats,
                            'sim_net': round(stats['sim_net'], 2),
                            'real_net': round(stats['real_net'], 2)}
                        for p, stats in by_platform.items()},
        'top5_by_sim_net': top5,
        'currently_open': _currently_open_summary(),
    }


# ── Internals ────────────────────────────────────────────
def _snapshot(deal: dict) -> dict:
    return {
        'platform': deal.get('platform'),
        'title': deal.get('title'),
        'sum_cents': deal.get('total_cents'),
        'net': deal.get('net'),
        'grade': deal.get('grade'),
        'min_liq': deal.get('min_liq'),
        'balance_used': deal.get('balance_used'),
        'roi': deal.get('roi'),
        'adj': deal.get('adj'),
    }


def _append_event(ev: dict) -> None:
    try:
        with open(EVENTS_PATH, 'a', encoding='utf-8') as f:
            f.write(json.dumps(ev, ensure_ascii=False) + '\n')
    except Exception as e:
        print(f"[analytics] append failed: {e}", flush=True)


def _persist_state() -> None:
    try:
        tmp = STATE_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'open_deals': _open_deals}, f, ensure_ascii=False)
        os.replace(tmp, STATE_PATH)
    except Exception as e:
        print(f"[analytics] state persist failed: {e}", flush=True)


def _period_cutoff(period: str) -> float:
    now = datetime.now(timezone.utc)
    spans = {'day': 1, 'week': 7, 'month': 30, 'all': 0}
    days = spans.get(period, 30)
    if days == 0:
        return 0.0
    return (now - timedelta(days=days)).timestamp()


def _currently_open_summary() -> dict:
    with _lock:
        entries = list(_open_deals.values())
    if not entries:
        return {'count': 0, 'sim_net_open': 0, 'taken_open': 0}
    return {
        'count': len(entries),
        'sim_net_open': round(sum((e.get('snapshot') or {}).get('net') or 0 for e in entries), 2),
        'taken_open': sum(1 for e in entries if e.get('decision') == 'took'),
    }


def _empty_aggregate(period: str) -> dict:
    return {
        'period': period, 'cutoff_ts': _period_cutoff(period),
        'sim': {'count': 0, 'net_total': 0, 'avg_net': 0},
        'real': {'taken': 0, 'skipped': 0, 'net_total': 0, 'avg_net': 0},
        'closed_count': 0, 'by_platform': {}, 'top5_by_sim_net': [],
        'currently_open': {'count': 0, 'sim_net_open': 0, 'taken_open': 0},
    }
