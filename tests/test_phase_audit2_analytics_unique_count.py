"""Phase audit-2 (12.05.2026) — analytics.aggregate() unique-event count.

Operator's pain: dashboard showed "229 сделок увидено" overnight but
the underlying events were just 2 fixtures (Brest×Strasbourg and
Manchester United) cycling open/close every scan tick. The total
count is technically correct but misleading without context.

Adds `unique_count` + `unique_ratio` to:
  - top-level `sim` block
  - per-platform stats
  - per-structure stats
"""
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


def _write_events(path, rows):
    """Helper: write a fresh analytics_events.jsonl."""
    with open(path, 'w', encoding='utf-8') as f:
        for r in rows:
            f.write(json.dumps(r) + '\n')


def test_unique_count_with_repeated_titles(tmp_path, monkeypatch):
    """Same fixture opened 5 times → unique_count=1, sim.count=5."""
    import analytics
    events_path = str(tmp_path / 'events.jsonl')
    monkeypatch.setattr(analytics, 'EVENTS_PATH', events_path)
    # Force re-init so the events file gets read fresh
    monkeypatch.setattr(analytics, '_loaded', False)
    now = time.time()
    _write_events(events_path, [
        {'type': 'opened', 'ts': now, 'key': 'k1',
         'platform': 'Polymarket', 'arb_structure': 'cross_platform',
         'title': 'Manchester United vs Forest', 'net': 6.37},
        {'type': 'opened', 'ts': now, 'key': 'k2',
         'platform': 'Polymarket', 'arb_structure': 'cross_platform',
         'title': 'Manchester United vs Forest', 'net': 6.37},
        {'type': 'opened', 'ts': now, 'key': 'k3',
         'platform': 'Polymarket', 'arb_structure': 'cross_platform',
         'title': 'Manchester United vs Forest', 'net': 6.37},
    ])
    out = analytics.aggregate(period='all')
    assert out['sim']['count'] == 3
    assert out['sim']['unique_count'] == 1
    assert out['sim']['unique_ratio'] == round(1/3, 3)


def test_unique_count_with_distinct_titles(tmp_path, monkeypatch):
    """3 different fixtures → unique_count=3, ratio=1.0."""
    import analytics
    events_path = str(tmp_path / 'events.jsonl')
    monkeypatch.setattr(analytics, 'EVENTS_PATH', events_path)
    monkeypatch.setattr(analytics, '_loaded', False)
    now = time.time()
    _write_events(events_path, [
        {'type': 'opened', 'ts': now, 'key': 'k1',
         'platform': 'Polymarket', 'arb_structure': 'cross_platform',
         'title': 'Brest vs Strasbourg', 'net': 1.85},
        {'type': 'opened', 'ts': now, 'key': 'k2',
         'platform': 'Polymarket', 'arb_structure': 'cross_platform',
         'title': 'Charlotte FC vs NYCFC', 'net': 2.95},
        {'type': 'opened', 'ts': now, 'key': 'k3',
         'platform': 'Polymarket', 'arb_structure': 'cross_platform',
         'title': 'Manchester City vs Crystal Palace', 'net': 3.18},
    ])
    out = analytics.aggregate(period='all')
    assert out['sim']['count'] == 3
    assert out['sim']['unique_count'] == 3
    assert out['sim']['unique_ratio'] == 1.0


def test_per_platform_unique_count(tmp_path, monkeypatch):
    """Per-platform stats include unique_count alongside sim_count."""
    import analytics
    events_path = str(tmp_path / 'events.jsonl')
    monkeypatch.setattr(analytics, 'EVENTS_PATH', events_path)
    monkeypatch.setattr(analytics, '_loaded', False)
    now = time.time()
    _write_events(events_path, [
        {'type': 'opened', 'ts': now, 'key': 'k1',
         'platform': 'Limitless+SX Bet', 'arb_structure': 'cross_platform',
         'title': 'A vs B', 'net': 1.0},
        {'type': 'opened', 'ts': now, 'key': 'k2',
         'platform': 'Limitless+SX Bet', 'arb_structure': 'cross_platform',
         'title': 'A vs B', 'net': 1.0},
        {'type': 'opened', 'ts': now, 'key': 'k3',
         'platform': 'Polymarket+SX Bet', 'arb_structure': 'cross_platform',
         'title': 'C vs D', 'net': 2.0},
    ])
    out = analytics.aggregate(period='all')
    lim = out['by_platform']['Limitless+SX Bet']
    poly = out['by_platform']['Polymarket+SX Bet']
    assert lim['sim_count'] == 2
    assert lim['unique_count'] == 1
    assert poly['sim_count'] == 1
    assert poly['unique_count'] == 1


def test_per_structure_unique_count(tmp_path, monkeypatch):
    """Per-structure stats include unique_count too."""
    import analytics
    events_path = str(tmp_path / 'events.jsonl')
    monkeypatch.setattr(analytics, 'EVENTS_PATH', events_path)
    monkeypatch.setattr(analytics, '_loaded', False)
    now = time.time()
    _write_events(events_path, [
        {'type': 'opened', 'ts': now, 'key': 'k1',
         'platform': 'Polymarket', 'arb_structure': 'all_yes',
         'title': 'X', 'net': 0.5},
        {'type': 'opened', 'ts': now, 'key': 'k2',
         'platform': 'Polymarket', 'arb_structure': 'all_yes',
         'title': 'X', 'net': 0.5},
        {'type': 'opened', 'ts': now, 'key': 'k3',
         'platform': 'Polymarket', 'arb_structure': 'cross_platform',
         'title': 'Y', 'net': 1.0},
    ])
    out = analytics.aggregate(period='all')
    a = out['by_structure']['all_yes']
    cp = out['by_structure']['cross_platform']
    assert a['sim_count'] == 2 and a['unique_count'] == 1
    assert cp['sim_count'] == 1 and cp['unique_count'] == 1


def test_empty_returns_zero_unique(tmp_path, monkeypatch):
    """No events → unique_count=0, ratio=None (no division-by-zero)."""
    import analytics
    events_path = str(tmp_path / 'nonexistent.jsonl')
    monkeypatch.setattr(analytics, 'EVENTS_PATH', events_path)
    monkeypatch.setattr(analytics, '_loaded', False)
    out = analytics.aggregate(period='all')
    assert out['sim']['count'] == 0
    assert out['sim']['unique_count'] == 0
    assert out['sim']['unique_ratio'] is None


def test_internal_titles_not_in_output(tmp_path, monkeypatch):
    """The `_titles` set is internal — must NOT leak into JSON
    response (sets aren't JSON-encodable + that's a strategy leak)."""
    import analytics
    events_path = str(tmp_path / 'events.jsonl')
    monkeypatch.setattr(analytics, 'EVENTS_PATH', events_path)
    monkeypatch.setattr(analytics, '_loaded', False)
    now = time.time()
    _write_events(events_path, [
        {'type': 'opened', 'ts': now, 'key': 'k',
         'platform': 'Polymarket', 'arb_structure': 'cross_platform',
         'title': 'leaky title', 'net': 1.0},
    ])
    out = analytics.aggregate(period='all')
    # Must serialize cleanly
    json_out = json.dumps(out, default=str)
    # _titles internal field must be gone
    assert '_titles' not in json_out
    # The actual title is in top5 (intentional — that's a different
    # data path), but NOT in by_platform/by_structure stats
    for stats in out['by_platform'].values():
        assert '_titles' not in stats
    for stats in out['by_structure'].values():
        assert '_titles' not in stats
