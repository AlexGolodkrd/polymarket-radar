"""
Analytics: lifecycle tracking and P&L aggregation for the arbitrage radar.

Tracks **sim P&L** only — the bot is now fully automated (Phase 2 dry-run
executor + Phase 5 paper-trading + future live execution). Manual "took /
skipped" decisions removed 28.04.2026 because the bot, not the human,
makes the trade decisions; risk gating + paper-trade graduation handle
the dispipline layer.

Storage layout (under project_root/Executions):

  analytics_events.jsonl      append-only log; persists across restarts
      {"type":"opened", "ts":..., "key":..., "platform":..., "title":...,
       "sum_cents":..., "net":..., "grade":..., "min_liq":..., "balance_used":...,
       "arb_structure":...}                              ← new in this revision
      {"type":"closed", "ts":..., "key":..., "duration_sec":...}

  analytics_state.json        currently-open deals snapshot
      Survives server restart so we don't double-count "opened" events
      for arbs that were already visible when the radar was killed.

The aggregate() period filter slides a window over the entire log; data
NEVER resets between runs. To intentionally start fresh, delete the two
files in Executions/ and restart.
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
_open_deals: dict = {}    # key -> {opened_ts, last_seen_ts, snapshot}
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


def aggregate(period: str = 'month') -> dict:
    """Aggregate stats over `period`: 'day', 'week', 'month', 'all'."""
    init()
    cutoff = _period_cutoff(period)
    sim_net_total = 0.0
    sim_count = 0
    closed_count = 0
    by_platform = defaultdict(lambda: {'sim_net': 0.0, 'sim_count': 0})
    by_structure = defaultdict(lambda: {'sim_net': 0.0, 'sim_count': 0})
    top_sim = []  # list of (net, key, snap)

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
                structure = ev.get('arb_structure') or 'all_yes'
                sim_net_total += net
                sim_count += 1
                by_platform[platform]['sim_net'] += net
                by_platform[platform]['sim_count'] += 1
                by_structure[structure]['sim_net'] += net
                by_structure[structure]['sim_count'] += 1
                top_sim.append((net, ev.get('key'), {
                    'platform': platform,
                    'title': ev.get('title', ''),
                    'sum_cents': ev.get('sum_cents'),
                    'grade': ev.get('grade'),
                    'min_liq': ev.get('min_liq'),
                    'arb_structure': structure,
                }))
            elif t == 'closed':
                closed_count += 1

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
        'closed_count': closed_count,
        'by_platform': {p: {**stats, 'sim_net': round(stats['sim_net'], 2)}
                        for p, stats in by_platform.items()},
        'by_structure': {s: {**stats, 'sim_net': round(stats['sim_net'], 2)}
                         for s, stats in by_structure.items()},
        'top5_by_sim_net': top5,
        'currently_open': _currently_open_summary(),
    }


def history(period: str = 'all', limit: int = 200, offset: int = 0,
            platform: Optional[str] = None,
            structure: Optional[str] = None,
            min_net: float = 0.0) -> dict:
    """Per-trade history — every 'opened' event in the period, joined with
    its 'closed' counterpart (if any) for duration. Newest first.

    Filters:
        platform: 'Polymarket' / 'Kalshi' / 'SX Bet' (None = all)
        structure: 'all_yes' / 'all_no' / 'yes_no_pair' / 'binary' (None = all)
        min_net: skip entries with net < this (e.g. 1.0 to hide tiny ones)

    Pagination: limit + offset (0 = newest). UI typically calls with
    limit=100 and an "older" button to step.

    Returns:
        {
          'total': N,            # total matching after filters
          'shown': len(rows),    # actually returned
          'period': period,
          'rows': [
            {ts, ts_iso, platform, title, sum_cents, net, grade, min_liq,
             balance_used, arb_structure, duration_sec, status},
            ...
          ]
        }
    """
    init()
    cutoff = _period_cutoff(period)
    if not os.path.exists(EVENTS_PATH):
        return {'total': 0, 'shown': 0, 'period': period, 'rows': []}

    opens: list = []          # (ts, key, snap)
    close_durations: dict = {}  # key -> duration_sec for the LATEST close

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
                opens.append((ts, ev.get('key'), {
                    'platform': ev.get('platform'),
                    'title': ev.get('title'),
                    'sum_cents': ev.get('sum_cents'),
                    'net': ev.get('net'),
                    'grade': ev.get('grade'),
                    'min_liq': ev.get('min_liq'),
                    'balance_used': ev.get('balance_used'),
                    'roi': ev.get('roi'),
                    'adj': ev.get('adj'),
                    'arb_structure': ev.get('arb_structure') or 'all_yes',
                    'end_date': ev.get('end_date'),  # may be None for legacy events
                }))
            elif t == 'closed':
                close_durations[ev.get('key')] = ev.get('duration_sec', 0)

    # Build rows
    with _lock:
        open_set = set(_open_deals.keys())
    rows = []
    for ts, key, snap in opens:
        net = float(snap.get('net') or 0)
        if min_net and net < min_net:
            continue
        if platform and snap.get('platform') != platform:
            continue
        if structure and snap.get('arb_structure') != structure:
            continue
        is_open = key in open_set
        rows.append({
            'ts': ts,
            'ts_iso': datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
            'key': key,
            'platform': snap.get('platform'),
            'title': snap.get('title'),
            'sum_cents': snap.get('sum_cents'),
            'net': round(net, 2),
            'grade': snap.get('grade'),
            'min_liq': snap.get('min_liq'),
            'balance_used': snap.get('balance_used'),
            'roi': snap.get('roi'),
            'adj': snap.get('adj'),
            'arb_structure': snap.get('arb_structure'),
            'duration_sec': close_durations.get(key),
            'status': 'open' if is_open else 'closed',
            # 28.04.2026: when this event resolves & pays out (ISO-8601 UTC).
            # None for legacy events written before this PR.
            'end_date': snap.get('end_date'),
        })
    # Newest first, pagination
    rows.sort(key=lambda r: r['ts'], reverse=True)
    total = len(rows)
    page = rows[offset:offset + limit]
    return {
        'total': total,
        'shown': len(page),
        'period': period,
        'offset': offset,
        'limit': limit,
        'rows': page,
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
        # Phase 1 structure tracking — needed for history filtering + aggregate
        'arb_structure': deal.get('arb_structure') or 'all_yes',
        # 28.04.2026: when does this event resolve & pay out? ISO-8601 UTC.
        # Lets the operator see capital lock-up duration in history.
        # Legacy events (pre-PR) won't have this — UI shows '—' fallback.
        'end_date': deal.get('end_date'),
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
        return {'count': 0, 'sim_net_open': 0}
    return {
        'count': len(entries),
        'sim_net_open': round(sum((e.get('snapshot') or {}).get('net') or 0 for e in entries), 2),
    }


def _empty_aggregate(period: str) -> dict:
    return {
        'period': period, 'cutoff_ts': _period_cutoff(period),
        'sim': {'count': 0, 'net_total': 0, 'avg_net': 0},
        'closed_count': 0, 'by_platform': {}, 'by_structure': {},
        'top5_by_sim_net': [],
        'currently_open': {'count': 0, 'sim_net_open': 0},
    }
