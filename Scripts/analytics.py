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
from typing import Any, Iterable, Optional


# ── Paths ────────────────────────────────────────────────
# Phase audit-4 (15.05.2026 PM) — honor EXECUTIONS_DIR env so tests
# can redirect persistence to a tmp_path.
# Phase audit-27.05 (27.05.2026) — paths still resolved at import time
# (matches existing semantics) but defer to `config.config.executions_dir`
# when present so the central config can override.
_DEFAULT_BASE_DIR: str = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', 'Executions'))


def _resolve_base_dir() -> str:
    """Honor EXECUTIONS_DIR env (legacy) or config.executions_dir (new)."""
    try:
        from config import config as _cfg
        if _cfg.executions_dir:
            return _cfg.executions_dir
    except Exception:
        pass
    return os.environ.get('EXECUTIONS_DIR') or _DEFAULT_BASE_DIR


_BASE_DIR: str = _resolve_base_dir()
EVENTS_PATH: str = os.path.join(_BASE_DIR, 'analytics_events.jsonl')
STATE_PATH: str = os.path.join(_BASE_DIR, 'analytics_state.json')

# ── State ────────────────────────────────────────────────
_lock: threading.RLock = threading.RLock()
_open_deals: dict[str, dict[str, Any]] = {}    # key -> open-entry record
_loaded: bool = False
# Phase audit (11.05.2026) — BUG-B2. Track which NEAR keys we've already
# logged in the current scan epoch; only re-log after _NEAR_RELOG_SEC so
# the events file doesn't explode (NEAR rescans every few seconds).
_near_logged: dict[str, float] = {}   # key -> last_log_ts
_NEAR_RELOG_SEC: int = 300            # re-log a still-present NEAR candidate every 5 min


# ── Public API ───────────────────────────────────────────
def deal_key(deal: dict[str, Any]) -> str:
    """Stable identifier for dedup across scans.

    Phase 19v19 (05.05.2026) — include arb_structure (and cross_structure
    for cross-platform deals) so X1 vs X2 don't collide on the same
    `platform::title`. Old key collapsed both directions of a CP pair
    into one entry → only one was logged as `opened`, the other was
    silently dropped from analytics. Same for ALL_YES vs YES_NO_PAIR on
    the same Polymarket event title.
    """
    plat = deal.get('platform', '?')
    title = deal.get('title', '?')
    struct = deal.get('arb_structure', '')
    sub = deal.get('cross_structure', '')
    return f"{plat}::{title}::{struct}::{sub}"


def record_fire_filled(arb_id: str, deal: dict[str, Any], leg_details: list[dict[str, Any]]) -> None:
    """Phase audit-15 (15.05.2026) — emit a `fire_filled` event for an arb
    where the bot actually entered at least one leg. Operator-requested
    so the Analytics view counts REAL positions, not predictions.

    A leg is considered "really filled" iff status=='filled' AND
    fill_size_usdc > 0 (we guard against SX soft no-op where fillHash is
    returned with totalFilled=0 — see paper.ts + sx-fill-resp log).
    """
    real_fills = [
        l for l in (leg_details or [])
        if (l.get('status') == 'filled'
            and float(l.get('fill_size_usdc') or 0) > 0)
    ]
    if not real_fills:
        return
    init()
    # Phase audit-4 (15.05.2026 PM) — fire_filled event must carry enough
    # info for /api/portfolio_positions to:
    #   (a) split open vs resolved by end_date
    #   (b) let the dashboard fetch resolution per leg via platform APIs
    # We pull `end_date` from the deal itself and per-leg identifiers
    # from BOTH leg_details (TS-supplied) AND deal['entries'] (Python-side
    # post-build_deal attach pass). Index-aligned merge — leg_details[i]
    # corresponds to deal.entries[i] for both per-platform deals
    # (build_deal) and cross-platform (to_radar_deal_format).
    deal_entries = (deal.get('entries') or [])
    enriched_legs = []
    for idx, l in enumerate(real_fills):
        # The index in `real_fills` may not match the original
        # leg_details index because we filtered. Recover original index
        # by identity (`leg_details.index(l)`); falls back to enumerate.
        try:
            orig_idx = (leg_details or []).index(l)
        except (ValueError, AttributeError):
            orig_idx = idx
        entry = deal_entries[orig_idx] if orig_idx < len(deal_entries) else {}
        enriched_legs.append({
            'platform': l.get('platform'),
            'fill_price': l.get('fill_price'),
            'fill_size_usdc': l.get('fill_size_usdc'),
            'slug': l.get('slug') or entry.get('slug'),
            'note': l.get('note'),
            'side': l.get('side') or entry.get('side'),
            # Identifiers for client-side resolution lookup
            # (limitless slug, sx marketHash, polymarket condition_id).
            'market_hash': l.get('market_hash') or entry.get('market_hash'),
            'outcome_index': l.get('outcome_index') or entry.get('outcome_index'),
            'condition_id': entry.get('condition_id'),
            'token_id': entry.get('token_id'),
            'token_id_yes': entry.get('token_id_yes'),
            'token_id_no': entry.get('token_id_no'),
            'neg_risk': entry.get('neg_risk'),
            'sport_type': entry.get('sport_type'),
        })
    ev = {
        'type': 'fire_filled',
        'ts': time.time(),
        'arb_id': arb_id,
        'platform': deal.get('platform', '?'),
        'title': deal.get('title', ''),
        'arb_structure': deal.get('arb_structure') or deal.get('structure'),
        'sum_cents': deal.get('sum_cents'),
        'net_expected': deal.get('net'),
        # Phase audit-4 — end_date drives open/resolved split in
        # /api/portfolio_positions. ISO-8601 UTC string.
        'end_date': deal.get('end_date'),
        'leg_count_filled': len(real_fills),
        'leg_count_total': len(leg_details or []),
        'legs': enriched_legs,
    }
    _append_event(ev)


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


def update_from_scan(deals: Iterable[dict[str, Any]]) -> None:
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

    # Phase 19v19 (05.05.2026) — close-grace window. Old logic:
    # `closed_keys = open_keys - new_keys` → close immediately on first
    # miss. A single transient WS hiccup or threshold-edge flicker
    # caused: scan_t   → deal in pool → tracked
    #         scan_t+1 → deal missing → CLOSE event
    #         scan_t+2 → deal back    → OPEN event (fresh opened_ts!)
    # Same arb counted twice in `aggregate()`'s sim_count and sim_net.
    # Fix: count consecutive misses; only close after `_CLOSE_GRACE_SCANS`
    # consecutive scans without the key.
    #
    # Phase audit-27.05 (27.05.2026) — bumped 3 → 10 scans (100s grace).
    # Phase audit-29.05 (29.05.2026) — bumped 10 → 30 (300s grace). The
    # 100s window was still too short for thin sport markets where MMs
    # can leave the book empty for 30-90s during refresh cycles. Operator
    # screenshot 29.05 showed Saint Etienne 63 opens-closes in 5h on the
    # same underlying deal (4-min cycle: 100s close grace + ~140s
    # reappear = false-positive lifecycle event). 5 min grace absorbs
    # natural MM breathing without losing genuine arb-end detection.
    # Env-tunable via CLOSE_GRACE_SCANS.
    try:
        from config import config as _cfg
        _CLOSE_GRACE_SCANS = int(os.environ.get('CLOSE_GRACE_SCANS', str(_cfg.close_grace_scans)))
    except Exception:
        _CLOSE_GRACE_SCANS = int(os.environ.get('CLOSE_GRACE_SCANS', '30'))
    with _lock:
        # Detect newly opened
        opened_keys = new_keys - set(_open_deals.keys())
        for k in opened_keys:
            snap = snapshots[k]
            _open_deals[k] = {
                'opened_ts': now,
                'last_seen_ts': now,
                'first_seen_ts': now,        # never changes during this open period
                'consecutive_scans_seen': 1,
                'misses': 0,
                'snapshot': snap,
            }
            _append_event({'type': 'opened', 'ts': now, 'key': k, **snap})

        # Update last_seen + snapshot for ones that are still around
        for k in new_keys & set(_open_deals.keys()):
            _open_deals[k]['last_seen_ts'] = now
            _open_deals[k]['misses'] = 0  # reset miss counter on reappearance
            _open_deals[k]['snapshot'] = snapshots[k]
            _open_deals[k]['consecutive_scans_seen'] = (
                _open_deals[k].get('consecutive_scans_seen', 0) + 1)

        # Detect potentially-closed: increment miss counter; only close
        # after grace window expires.
        candidate_close = set(_open_deals.keys()) - new_keys
        actually_closed = []
        for k in candidate_close:
            entry = _open_deals[k]
            entry['misses'] = entry.get('misses', 0) + 1
            if entry['misses'] >= _CLOSE_GRACE_SCANS:
                actually_closed.append(k)
        for k in actually_closed:
            entry = _open_deals.pop(k)
            duration = now - entry['opened_ts']
            _append_event({'type': 'closed', 'ts': now, 'key': k,
                            'duration_sec': round(duration, 1)})

        _persist_state()


def update_from_near_scan(near_items: Iterable[dict[str, Any]]) -> None:
    """Phase audit (11.05.2026) — BUG-B2. Log NEAR-pool snapshots so we can
    forensically reconstruct WHY a deal was hovering at threshold (e.g.
    'this Polymarket sports market sat at 94.8c for 2 hours, never crossed').

    Dedup by deal_key with TTL — re-logs the same NEAR after _NEAR_RELOG_SEC.
    Writes {'type':'near_seen', ts, key, snapshot} to events log.
    """
    init()
    now = time.time()
    with _lock:
        for d in near_items:
            k = deal_key(d)
            last = _near_logged.get(k, 0)
            if now - last < _NEAR_RELOG_SEC:
                continue
            _near_logged[k] = now
            snap = _near_snapshot(d)
            _append_event({'type': 'near_seen', 'ts': now, 'key': k, **snap})


def _near_snapshot(item: dict[str, Any]) -> dict[str, Any]:
    """Subset of fields safe + useful for NEAR-pool forensics."""
    return {
        'platform': item.get('platform'),
        'title': item.get('title'),
        'arb_structure': item.get('arb_structure'),
        'sum_cents': item.get('sum_cents'),
        'distance_cents': item.get('distance_cents'),
        'threshold_cents': item.get('threshold_cents'),
        'outcomes_count': item.get('outcomes_count'),
        'min_liquidity': item.get('min_liquidity'),
        'theta': item.get('theta'),
        'end_date': item.get('end_date'),
    }


def aggregate(period: str = 'month') -> dict[str, Any]:
    """Aggregate stats over `period`: 'day', 'week', 'month', 'all'."""
    init()
    cutoff = _period_cutoff(period)
    sim_net_total = 0.0
    sim_count = 0
    closed_count = 0
    # Phase audit-15 (15.05.2026) — real entered-trade counter.
    # `sim_count` keeps counting `opened` events (predictions); the new
    # `filled_count` only tallies fires where the bot actually entered.
    filled_count = 0
    filled_by_platform: dict = defaultdict(int)
    filled_by_structure: dict = defaultdict(int)
    filled_titles: set = set()
    # Phase audit (11.05.2026) — request #3: aggregate NEAR-pool snapshots
    # alongside opened arbs so the operator can see threshold-flow funnel
    # (how many candidates sit at threshold but never become arbs).
    near_count = 0
    near_by_platform: dict = defaultdict(int)
    near_by_structure: dict = defaultdict(int)
    by_platform = defaultdict(lambda: {'sim_net': 0.0, 'sim_count': 0,
                                          '_titles': set()})
    by_structure = defaultdict(lambda: {'sim_net': 0.0, 'sim_count': 0,
                                           '_titles': set()})
    top_sim = []  # list of (net, key, snap)
    # Phase audit-2 (12.05.2026) — operator's screenshot showed
    # "229 сделок увидено" but only 2 unique fixtures (same Brest×Strasbourg
    # and Manchester United re-opened every scan tick due to natural
    # arb-window cycling). To answer "сколько РАЗНЫХ событий" without
    # mental subtraction, we track distinct titles per period and per
    # platform/structure.
    unique_titles: set = set()

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
                title = ev.get('title') or ''
                sim_net_total += net
                sim_count += 1
                unique_titles.add(title)
                by_platform[platform]['sim_net'] += net
                by_platform[platform]['sim_count'] += 1
                by_platform[platform]['_titles'].add(title)
                by_structure[structure]['sim_net'] += net
                by_structure[structure]['sim_count'] += 1
                by_structure[structure]['_titles'].add(title)
                top_sim.append((net, ev.get('key'), {
                    'platform': platform,
                    'title': title,
                    'sum_cents': ev.get('sum_cents'),
                    'grade': ev.get('grade'),
                    'min_liq': ev.get('min_liq'),
                    'arb_structure': structure,
                }))
            elif t == 'near_seen':
                # Phase audit (11.05.2026) — request #3 + BUG-B2.
                near_count += 1
                near_by_platform[ev.get('platform', '?')] += 1
                near_by_structure[ev.get('arb_structure') or 'all_yes'] += 1
            elif t == 'closed':
                closed_count += 1
            elif t == 'fire_filled':
                # Phase audit-15 (15.05.2026) — real entered trade.
                filled_count += 1
                filled_by_platform[ev.get('platform', '?')] += 1
                filled_by_structure[ev.get('arb_structure') or 'all_yes'] += 1
                if ev.get('title'):
                    filled_titles.add(ev['title'])

    top_sim.sort(key=lambda x: x[0], reverse=True)
    top5 = [{'net': round(n, 2), 'key': k, **snap} for n, k, snap in top_sim[:5]]

    # Pop internal `_titles` sets and replace with their counts before
    # JSON serialization (sets aren't JSON-encodable, and the radar's
    # /api/analytics consumer expects scalar dicts).
    def _finalize(stats):
        titles = stats.pop('_titles', set())
        stats['unique_count'] = len(titles)
        stats['sim_net'] = round(stats['sim_net'], 2)
        return stats

    return {
        'period': period,
        'cutoff_ts': cutoff,
        'sim': {
            'count': sim_count,
            # Phase audit-2 (12.05.2026): distinct fixtures observed.
            # 1.0 = every "opened" event was a different fixture; values
            # close to 0 mean the same fixture(s) cycled open/close many
            # times (typical for stable cross-platform arbs).
            'unique_count': len(unique_titles),
            'unique_ratio': (round(len(unique_titles) / sim_count, 3)
                              if sim_count else None),
            'net_total': round(sim_net_total, 2),
            'avg_net': round(sim_net_total / sim_count, 2) if sim_count else 0,
        },
        'closed_count': closed_count,
        # Phase audit-15 (15.05.2026) — REAL entered trades counter.
        # `sim.count` above tracks predictions (arbs the radar SAW);
        # `filled` here tracks fires where the bot ACTUALLY took a
        # position (at least one leg status=='filled' with size > 0).
        # Operator-requested separation so the dashboard's "real" metric
        # is unambiguous.
        'filled': {
            'count': filled_count,
            'unique_count': len(filled_titles),
            'by_platform': dict(filled_by_platform),
            'by_structure': dict(filled_by_structure),
        },
        'by_platform': {p: _finalize(s) for p, s in by_platform.items()},
        'by_structure': {s: _finalize(st) for s, st in by_structure.items()},
        'top5_by_sim_net': top5,
        'currently_open': _currently_open_summary(),
        # Phase audit (11.05.2026) — request #3 + BUG-B2: NEAR-pool funnel.
        # Shows how many "almost-arb" candidates were logged vs how many
        # actually crossed threshold (`sim.count`). near_to_arb_ratio is
        # the conversion rate — a low number indicates lots of NEAR
        # activity with few actual arbs, useful for tuning thresholds.
        'near': {
            'count': near_count,
            'by_platform': dict(near_by_platform),
            'by_structure': dict(near_by_structure),
            'near_to_arb_ratio': round(sim_count / near_count, 3)
                                   if near_count else None,
        },
    }


def history(period: str = 'all', limit: int = 200, offset: int = 0,
            platform: Optional[str] = None,
            structure: Optional[str] = None,
            min_net: float = 0.0) -> dict[str, Any]:
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
def _snapshot(deal: dict[str, Any]) -> dict[str, Any]:
    # Phase 19v32 (08.05.2026) — sum_cents fallback. Per-platform deals
    # (built via arb_server.build_deal) write `total_cents`; cross-platform
    # deals (built via cross_platform.to_radar_deal_format) write
    # `sum_cents`. Old code only read `total_cents` → cross-platform rows
    # had `sum_cents=null` in analytics_events.jsonl and on the dashboard
    # «История сделок» Sum column showed `—` for every CP deal. Operator
    # screenshot 08.05.2026: 100+ rows of Fulham×Bournemouth all with
    # empty Sum. Read either source so both deal shapes populate.
    #
    # Phase audit-extras (11.05.2026) — also snapshot fields needed for
    # forensics on threshold/fee/slippage questions: theta (per-market
    # taker fee in decimal — used to compute the dynamic Polymarket
    # threshold), cross_structure (X1 vs X2 for cross-platform deals),
    # fee/gross/slip_pct/confidence (economics breakdown that was added
    # to UI in Phase 19v34 but never persisted to analytics_events.jsonl).
    # Operator's "why is threshold 94.8 for Kilmarnock?" couldn't be
    # answered before because theta wasn't persisted — now it is.
    return {
        'platform': deal.get('platform'),
        'title': deal.get('title'),
        'sum_cents': (deal.get('total_cents')
                      if deal.get('total_cents') is not None
                      else deal.get('sum_cents')),
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
        # ── Phase audit-extras additions ──────────────────────────
        # theta = decimal taker fee per market (e.g. 0.025 for 2.5%).
        # Used to derive the dynamic threshold per Polymarket market.
        # If theta is high (>=0.049 = 4.9%) → threshold floors at 0.948.
        'theta': deal.get('theta'),
        # X1 vs X2 for cross-platform deals (which side gets bet on
        # which platform). Operator's history table can now show this.
        'cross_structure': deal.get('cross_structure'),
        # Per-deal fee / gross dollar breakdowns + slippage estimate.
        # Match the UI fields added in Phase 19v34 so analytics rows
        # are consistent with the dashboard «Сделки» cards.
        'fee': deal.get('fee'),
        'gross': deal.get('gross'),
        'fee_pct': deal.get('fee_pct'),
        'gross_pct': deal.get('gross_pct'),
        'adj_roi': deal.get('adj_roi'),
        'slip_pct': deal.get('slip_pct'),
        'slip_cost': deal.get('slip_cost'),
        # Cross-platform confidence score (0.0..1.0) — Phase 13+ how
        # certain the radar is that two events on two platforms are
        # the SAME real-world event (title fuzzy match + end_date).
        'confidence': deal.get('confidence'),
    }


def get_first_seen_ts(key: str) -> Optional[float]:
    """Phase audit-2 (11.05.2026) — return the moment a deal first
    entered the open-deals tracker, or None if the key isn't currently
    open. Used by pipeline_timing to compute scan-to-dispatch latency.

    Lookup is O(1) under _lock; safe to call from any thread. We do NOT
    call init() here on purpose — the caller (fire path) is always in
    a process where update_from_scan has already initialised state, and
    a fresh `init()` from the fire path would race with persisters.
    """
    with _lock:
        entry = _open_deals.get(key)
        if not entry:
            return None
        return entry.get('first_seen_ts') or entry.get('opened_ts')


def live_deals_snapshot() -> list[dict[str, Any]]:
    """Phase audit-2 (11.05.2026) — real-time visibility into currently
    open deals + how long they've been continuously visible.

    Used by /api/active_deals so the operator can see lifecycle in REAL
    TIME, not the 90-120s grace-period-bounded duration from the close
    events. `consecutive_scans_seen` × scan_interval = real lifespan.

    Returns list of:
      {key, opened_ts, first_seen_ts, last_seen_ts, age_sec,
       consecutive_scans_seen, snapshot, misses}
    """
    init()
    with _lock:
        now = time.time()
        out = []
        for k, entry in _open_deals.items():
            opened_ts = entry.get('opened_ts', 0)
            out.append({
                'key': k,
                'opened_ts': opened_ts,
                'first_seen_ts': entry.get('first_seen_ts', opened_ts),
                'last_seen_ts': entry.get('last_seen_ts', opened_ts),
                'age_sec': round(now - opened_ts, 1),
                'consecutive_scans_seen': entry.get('consecutive_scans_seen', 1),
                'misses': entry.get('misses', 0),
                'snapshot': entry.get('snapshot', {}),
            })
        # Sort by age (oldest = most stable arb first)
        out.sort(key=lambda x: x['age_sec'], reverse=True)
        return out


def _append_event(ev: dict[str, Any]) -> None:
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


def _currently_open_summary() -> dict[str, Any]:
    with _lock:
        entries = list(_open_deals.values())
    if not entries:
        return {'count': 0, 'sim_net_open': 0}
    return {
        'count': len(entries),
        'sim_net_open': round(sum((e.get('snapshot') or {}).get('net') or 0 for e in entries), 2),
    }


def _empty_aggregate(period: str) -> dict[str, Any]:
    return {
        'period': period, 'cutoff_ts': _period_cutoff(period),
        'sim': {'count': 0, 'unique_count': 0, 'unique_ratio': None,
                'net_total': 0, 'avg_net': 0},
        'closed_count': 0, 'by_platform': {}, 'by_structure': {},
        'top5_by_sim_net': [],
        'currently_open': {'count': 0, 'sim_net_open': 0},
        'near': {'count': 0, 'by_platform': {}, 'by_structure': {},
                  'near_to_arb_ratio': None},
    }
