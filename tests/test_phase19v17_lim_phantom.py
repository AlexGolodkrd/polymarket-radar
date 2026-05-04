"""Phase 19v17 (05.05.2026) — Limitless phantom-arb fix.

Operator screenshot: dashboard showing 2 deals on "ETH price on May 4,
11:00 UTC?" with sum=8.7¢ and ROI=1048.9%. Resolution date 2026-05-03
(yesterday). The Limitless server kept status='ACTIVE' / closed=false
24h+ AFTER resolution, but the orderbook prices for losing children
collapsed to ~0.3¢ each → 24 children × 0.3¢ = 8.7¢ phantom ALL_YES.

Polymarket has had Phase 9kkk #41 past-resolve adaptive grace since
30.04.2026; Limitless was the gap. This phase mirrors it.

Fixes:
  1. filter_limitless — past-resolve adaptive grace gate
  2. near_summary lim_near loop — second-line guard for in-flight events
"""
import os
import sys
from datetime import datetime, timezone, timedelta

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


# ── Fix #1: filter_limitless past-resolve gate ────────────────────

def test_filter_limitless_drops_past_resolve_hourly():
    """A Limitless event with deadline 2h in the past must be dropped
    (5min grace for hourly events)."""
    from arb_server import filter_limitless
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    ev = {
        'title': 'ETH price on May 4, 11:00 UTC?',
        'deadline': past,
        'startDate': (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(),
        'status': 'ACTIVE',  # Limitless still says active
        'markets': [{'title': 'YES', 'slug': 's', 'status': 'ACTIVE'}],
    }
    diag = {}
    result = filter_limitless([ev], diag=diag)
    assert result == [], f"past-resolve hourly event must be dropped: {result}"
    assert diag.get('lim_skip_past_resolve', 0) == 1


def test_filter_limitless_keeps_within_grace():
    """Event resolved 30s ago (well within hourly 5min grace) still passes."""
    from arb_server import filter_limitless
    past = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    ev = {
        'title': 'ETH price on May 4, 11:00 UTC?',
        'deadline': past,
        'startDate': (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        'status': 'ACTIVE',
        'markets': [{'title': 'YES', 'slug': 's', 'status': 'ACTIVE'}],
    }
    diag = {}
    result = filter_limitless([ev], diag=diag)
    assert len(result) == 1
    assert diag.get('lim_skip_past_resolve', 0) == 0


def test_filter_limitless_strict_grace_for_5min_crypto():
    """5-min crypto event resolved 2min ago must be dropped (1min grace)."""
    from arb_server import filter_limitless
    past = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    start = (datetime.now(timezone.utc) - timedelta(minutes=7)).isoformat()
    ev = {
        'title': 'BTC Up or Down 5min',
        'deadline': past,
        'startDate': start,
        'status': 'ACTIVE',
        'markets': [{'title': 'UP', 'slug': 'up', 'status': 'ACTIVE'},
                     {'title': 'DOWN', 'slug': 'down', 'status': 'ACTIVE'}],
    }
    diag = {}
    result = filter_limitless([ev], diag=diag)
    assert result == []
    assert diag.get('lim_skip_past_resolve', 0) == 1


def test_filter_limitless_drops_eth_phantom_screenshot_case():
    """End-to-end reproduction of operator's screenshot: ETH May 4 11:00 UTC
    event resolved yesterday with 24 children, all priced 0.3¢ each from
    stale orderbook. Must NOT enter the radar pipeline."""
    from arb_server import filter_limitless
    # End date: 1 day ago (well past 5-min hourly grace)
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    start = (datetime.now(timezone.utc) - timedelta(days=1, hours=1)).isoformat()
    children = [{'title': f'$2{i:02d}{i:02d}-$2{i:02d}{i+1:02d}',
                  'slug': f'eth-bucket-{i}', 'status': 'ACTIVE'}
                 for i in range(24)]
    ev = {
        'title': 'ETH price on May 4, 11:00 UTC?',
        'deadline': past,
        'startDate': start,
        'status': 'ACTIVE',
        'markets': children,
    }
    diag = {}
    result = filter_limitless([ev], diag=diag)
    assert result == [], "phantom ETH event must be filtered out"
    assert diag.get('lim_skip_past_resolve', 0) == 1


# ── Fix #2: near_summary lim_near second-line guard ───────────────

def test_near_summary_skips_past_resolve_lim():
    """`near_summary` must independently filter past-resolve Limitless
    events that slipped through (e.g. cached pool from prior scan)."""
    import arb_server
    past = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    # Inject a stale lim event into the pool
    arb_server.pools['lim'] = {
        'hot': [],
        'near': [{
            'title': 'ETH price on May 4, 11:00 UTC?',
            'deadline': past,
            'slug': 'eth-may4',
            'markets': [],
        }],
    }
    arb_server.near_summary(
        clob_res={}, kalshi_res={}, sx_res={},
        lim_res={'eth-may4': (0.087, 100, None, 0)},
    )
    diag = arb_server._last_near_rejection_stats
    assert diag.get('lim_skip_past_resolve', 0) >= 1, \
        f"near_summary must report past-resolve Lim drop in diag: {diag}"


# ── Diag counter wiring ───────────────────────────────────────────

def test_filter_limitless_diag_has_past_resolve_key():
    """Stats counter must be initialized even when no events trip it."""
    from arb_server import filter_limitless
    diag = {}
    filter_limitless([], diag=diag)
    assert 'lim_skip_past_resolve' in diag
