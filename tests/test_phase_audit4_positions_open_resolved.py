"""Phase audit-4 (15.05.2026 PM) — split portfolio_positions into open/resolved.

Operator pain: May 17 events stayed in "Текущие позиции" all of May 18.
Backend now reads end_date from each fire_filled event, parses ISO/human
formats, and splits positions into {open, resolved}. Dashboard JS
renders them in two separate tables and computes Real P&L for resolved
rows via client-side platform API lookups.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


# ── record_fire_filled enrichment ─────────────────────────────────

def test_record_fire_filled_emits_end_date(monkeypatch, tmp_path):
    """end_date from deal must land in the emitted event so
    /api/portfolio_positions can later split open vs resolved."""
    monkeypatch.setenv('EXECUTIONS_DIR', str(tmp_path))
    import importlib, analytics
    importlib.reload(analytics)
    analytics.record_fire_filled('arb-1', {
        'platform': 'Limitless',
        'title': 'EPL, Brentford vs Crystal Palace, May 17, 2026',
        'arb_structure': 'cross_platform',
        'end_date': '2026-05-17T14:00:00+00:00',
    }, [
        {'status': 'filled', 'fill_size_usdc': 3.85,
         'platform': 'limitless', 'slug': 'brentford-x', 'fill_price': 0.561},
    ])
    events_path = os.path.join(str(tmp_path), 'analytics_events.jsonl')
    with open(events_path, encoding='utf-8') as f:
        lines = [json.loads(l) for l in f if l.strip()]
    assert len(lines) == 1
    ev = lines[0]
    assert ev['type'] == 'fire_filled'
    assert ev['end_date'] == '2026-05-17T14:00:00+00:00'


def test_record_fire_filled_enriches_legs_from_deal_entries(monkeypatch, tmp_path):
    """When deal['entries'][i] carries identifiers (slug / market_hash /
    condition_id) and leg_details[i] doesn't, the emitted leg should
    pick them up. Index-aligned merge."""
    monkeypatch.setenv('EXECUTIONS_DIR', str(tmp_path))
    import importlib, analytics
    importlib.reload(analytics)
    analytics.record_fire_filled('arb-1', {
        'platform': 'Limitless+SX Bet',
        'title': 'Test fixture',
        'end_date': '2026-06-01T00:00:00+00:00',
        'entries': [
            {'slug': 'foo-lim', 'side': 'YES', 'token_id': 'tok_y'},
            {'market_hash': '0xabc', 'outcome_index': 2, 'side': 'OUTCOME_2'},
        ],
    }, [
        {'status': 'filled', 'fill_size_usdc': 1.0,
         'platform': 'limitless', 'fill_price': 0.5},
        {'status': 'filled', 'fill_size_usdc': 1.0,
         'platform': 'sx_bet', 'fill_price': 0.5},
    ])
    events_path = os.path.join(str(tmp_path), 'analytics_events.jsonl')
    with open(events_path, encoding='utf-8') as f:
        ev = json.loads(f.readline())
    legs = ev['legs']
    assert legs[0]['slug'] == 'foo-lim'
    assert legs[0]['side'] == 'YES'
    assert legs[0]['token_id'] == 'tok_y'
    assert legs[1]['market_hash'] == '0xabc'
    assert legs[1]['outcome_index'] == 2
    assert legs[1]['side'] == 'OUTCOME_2'


# ── /api/portfolio_positions split ────────────────────────────────

def _setup_arb_server_with_events(monkeypatch, tmp_path, events):
    """Helper: write events to a temp analytics_events.jsonl, redirect
    EXECUTIONS_DIR, return the flask test client."""
    monkeypatch.setenv('EXECUTIONS_DIR', str(tmp_path))
    events_path = os.path.join(str(tmp_path), 'analytics_events.jsonl')
    with open(events_path, 'w', encoding='utf-8') as f:
        for ev in events:
            f.write(json.dumps(ev) + '\n')
    import importlib, analytics, arb_server
    importlib.reload(analytics)
    importlib.reload(arb_server)
    return arb_server.app.test_client()


def test_portfolio_positions_splits_open_and_resolved(monkeypatch, tmp_path):
    from datetime import datetime, timezone, timedelta
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    events = [
        {'type': 'fire_filled', 'ts': 1.0, 'arb_id': 'past1',
         'title': 'Past event', 'end_date': past,
         'legs': [{'platform': 'limitless', 'fill_size_usdc': 1.0,
                   'fill_price': 0.5, 'slug': 'past-slug', 'side': 'YES'}]},
        {'type': 'fire_filled', 'ts': 2.0, 'arb_id': 'fut1',
         'title': 'Future event', 'end_date': future,
         'legs': [{'platform': 'limitless', 'fill_size_usdc': 2.0,
                   'fill_price': 0.6, 'slug': 'fut-slug', 'side': 'YES'}]},
    ]
    client = _setup_arb_server_with_events(monkeypatch, tmp_path, events)
    r = client.get('/api/portfolio_positions')
    assert r.status_code == 200
    data = r.get_json()
    assert 'open' in data
    assert 'resolved' in data
    assert data['open']['count'] == 1
    assert data['resolved']['count'] == 1
    assert data['open']['positions'][0]['title'] == 'Future event'
    assert data['resolved']['positions'][0]['title'] == 'Past event'


def test_portfolio_positions_no_end_date_treated_as_open(monkeypatch, tmp_path):
    """Defensive: an event without end_date metadata must NOT silently
    disappear into resolved. Operator's invariant — never accidentally
    hide a position because of bad metadata."""
    events = [
        {'type': 'fire_filled', 'ts': 1.0, 'arb_id': 'no-date',
         'title': 'Old backfilled', 'end_date': None,
         'legs': [{'platform': 'limitless', 'fill_size_usdc': 1.0,
                   'fill_price': 0.5, 'slug': 'x', 'side': 'YES'}]},
    ]
    client = _setup_arb_server_with_events(monkeypatch, tmp_path, events)
    r = client.get('/api/portfolio_positions')
    data = r.get_json()
    assert data['open']['count'] == 1
    assert data['resolved']['count'] == 0


def test_portfolio_positions_aggregates_per_key(monkeypatch, tmp_path):
    """Two fires on the same (title, platform, side) aggregate into one row."""
    from datetime import datetime, timezone, timedelta
    future = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    events = [
        {'type': 'fire_filled', 'ts': 1.0, 'arb_id': 'a',
         'title': 'X', 'end_date': future,
         'legs': [{'platform': 'sx_bet', 'fill_size_usdc': 1.0,
                   'fill_price': 0.284, 'side': 'OUTCOME_1',
                   'market_hash': '0xabc', 'outcome_index': 1}]},
        {'type': 'fire_filled', 'ts': 2.0, 'arb_id': 'b',
         'title': 'X', 'end_date': future,
         'legs': [{'platform': 'sx_bet', 'fill_size_usdc': 1.0,
                   'fill_price': 0.284, 'side': 'OUTCOME_1',
                   'market_hash': '0xabc', 'outcome_index': 1}]},
    ]
    client = _setup_arb_server_with_events(monkeypatch, tmp_path, events)
    r = client.get('/api/portfolio_positions')
    data = r.get_json()
    positions = data['open']['positions']
    assert len(positions) == 1
    p = positions[0]
    assert p['total_size_usdc'] == 2.0
    assert p['fire_count'] == 2
    # Per-leg identifiers preserved for client-side resolution lookup
    assert p['ids'].get('market_hash') == '0xabc'
    assert p['ids'].get('outcome_index') == 1


def test_portfolio_positions_backward_compat_flat_fields(monkeypatch, tmp_path):
    """Old dashboard versions read `count`/`total_cost_usdc`/`positions`
    directly. They must continue to work, showing the UNION of open
    and resolved so a mid-deploy doesn't blank out the UI."""
    from datetime import datetime, timezone, timedelta
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    events = [
        {'type': 'fire_filled', 'ts': 1.0, 'arb_id': 'a', 'title': 'A',
         'end_date': past,
         'legs': [{'platform': 'limitless', 'fill_size_usdc': 1.0,
                   'fill_price': 0.5, 'side': 'YES'}]},
        {'type': 'fire_filled', 'ts': 2.0, 'arb_id': 'b', 'title': 'B',
         'end_date': future,
         'legs': [{'platform': 'limitless', 'fill_size_usdc': 2.0,
                   'fill_price': 0.5, 'side': 'YES'}]},
    ]
    client = _setup_arb_server_with_events(monkeypatch, tmp_path, events)
    r = client.get('/api/portfolio_positions')
    data = r.get_json()
    assert data['count'] == 2
    assert data['total_cost_usdc'] == 3.0
    assert len(data['positions']) == 2


def test_portfolio_positions_human_date_parsed(monkeypatch, tmp_path):
    """Tolerant date parsing — 'May 17, 2026' style human strings used
    to land in some backfilled events. Must classify correctly."""
    events = [
        {'type': 'fire_filled', 'ts': 1.0, 'arb_id': 'old',
         'title': 'Old event', 'end_date': 'May 17, 1970',  # ancient
         'legs': [{'platform': 'limitless', 'fill_size_usdc': 1.0,
                   'fill_price': 0.5, 'slug': 'x', 'side': 'YES'}]},
    ]
    client = _setup_arb_server_with_events(monkeypatch, tmp_path, events)
    r = client.get('/api/portfolio_positions')
    data = r.get_json()
    # 1970 < now → resolved
    assert data['resolved']['count'] == 1
    assert data['open']['count'] == 0
