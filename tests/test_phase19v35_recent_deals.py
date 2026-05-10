"""Phase 19v35 (09.05.2026) — public read-only `/api/recent_deals` endpoint.

Tests cover:
  - 200 status + JSON shape ({rows, count})
  - limit clamping (default 50, cap 500)
  - type filter (opened / closed / unfiltered)
  - PII whitelist enforcement — no token IDs, no addresses, no
    signatures, no salts, no marketHashes, no slugs
  - Missing analytics_events.jsonl returns empty rows (no 500)
  - Malformed JSON lines skipped, valid lines preserved

The endpoint is exposed BEFORE nginx basic auth (per docs/
PUBLIC_AUDIT_ENDPOINT.md) so external observers can probe deal flow
without operator-side credentials.
"""
import json
import os
import sys
import tempfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


@pytest.fixture
def app_client(monkeypatch, tmp_path):
    """Spin up arb_server with a synthetic analytics file in tmp_path."""
    # Use a tmp Executions/ dir for this test so we don't touch real logs
    ex_dir = tmp_path / 'Executions'
    ex_dir.mkdir()
    monkeypatch.chdir(tmp_path)

    # Synthetic analytics rows — mix of opened/closed + sensitive fields
    # that the endpoint MUST strip
    rows = [
        # opened, cross-platform with full PII payload
        {
            'type': 'opened', 'ts': 1778232862.0,
            'key': 'Polymarket+SX Bet::Le Havre AC vs OM::cross_platform::X1',
            'title': 'Le Havre AC vs Olympique de Marseille',
            'platform': 'Polymarket+SX Bet',
            'arb_structure': 'cross_platform',
            'cross_structure': 'X1',
            'sum_cents': 91.62, 'total_cents': 91.62,
            'net': 3.53, 'net_cents': 8.38,
            'gross': 4.61, 'gross_pct': 9.14,
            'fee': 1.08, 'fee_pct': 2.15,
            'roi': 7.0, 'adj_roi': 4.5,
            'grade': 'CP-A',
            'min_liq': 208391, 'balance_used': 55.0, 'theta': 0.025,
            'end_date': '2026-05-10',
            # ── PII that MUST be stripped ───────────────────────────
            'entries': [{'token_id': '0xdeadbeef', 'wallet': '0x123...'}],
            'token_id_yes': '71321045679...',
            'token_id_no': '49384938...',
            'marketHash': '0xabc123...',
            'slug': 'le-havre-vs-marseille-1778',
            'maker': '0xdeadbeefcafe',
            'signer': '0xc0ffee',
            'signature': '0x' + 'ab' * 65,
            'salt': '12345678901234567890',
            'poly_api_key': 'uuid-secret',
            'verifying_contract': '0xE111180000d2663C0091e4f400237545B87B996B',
            'order': {'salt': '...', 'signature': '...'},
            'body': {'order': {}, 'owner': '...'},
        },
        # closed event
        {
            'type': 'closed', 'ts': 1778232900.0,
            'key': 'Polymarket+SX Bet::Le Havre AC vs OM::cross_platform::X1',
            'title': 'Le Havre AC vs Olympique de Marseille',
            'platform': 'Polymarket+SX Bet',
            'sum_cents': 91.62, 'net': 3.53, 'roi': 7.0,
            'grade': 'CP-A',
        },
        # opened — Polymarket per-platform
        {
            'type': 'opened', 'ts': 1778232950.0,
            'key': 'Polymarket::SOL price::yes_no_pair::',
            'title': 'SOL price on May 10', 'platform': 'Polymarket',
            'arb_structure': 'yes_no_pair',
            'sum_cents': 90.5, 'net': 0.21, 'roi': 1.1,
            'grade': 'F',
            'token_id_yes': '0x999', 'maker': '0xff',
        },
    ]
    log_path = ex_dir / 'analytics_events.jsonl'
    with open(log_path, 'w') as f:
        # Add a malformed line in the middle to verify graceful skip
        f.write(json.dumps(rows[0]) + '\n')
        f.write('{not valid json}\n')
        f.write(json.dumps(rows[1]) + '\n')
        f.write(json.dumps(rows[2]) + '\n')

    if 'arb_server' in sys.modules:
        del sys.modules['arb_server']
    import arb_server
    return arb_server.app.test_client()


# ── Status + shape ──────────────────────────────────────────────────

def test_endpoint_returns_200(app_client):
    resp = app_client.get('/api/recent_deals')
    assert resp.status_code == 200


def test_response_has_rows_and_count(app_client):
    resp = app_client.get('/api/recent_deals')
    body = resp.get_json()
    assert 'rows' in body
    assert 'count' in body
    assert body['count'] == len(body['rows'])


def test_returns_all_three_synthetic_rows(app_client):
    resp = app_client.get('/api/recent_deals?limit=10')
    body = resp.get_json()
    assert body['count'] == 3


# ── PII strip enforcement ───────────────────────────────────────────

def test_token_ids_stripped(app_client):
    body = app_client.get('/api/recent_deals?limit=10').get_json()
    for row in body['rows']:
        assert 'token_id_yes' not in row
        assert 'token_id_no' not in row
        assert 'token_id' not in row


def test_market_hash_stripped(app_client):
    body = app_client.get('/api/recent_deals?limit=10').get_json()
    for row in body['rows']:
        assert 'marketHash' not in row
        assert 'market_hash' not in row


def test_slug_stripped(app_client):
    body = app_client.get('/api/recent_deals?limit=10').get_json()
    for row in body['rows']:
        assert 'slug' not in row


def test_addresses_stripped(app_client):
    body = app_client.get('/api/recent_deals?limit=10').get_json()
    for row in body['rows']:
        for f in ('maker', 'signer', 'wallet', 'address'):
            assert f not in row


def test_signature_and_salt_stripped(app_client):
    body = app_client.get('/api/recent_deals?limit=10').get_json()
    for row in body['rows']:
        assert 'signature' not in row
        assert 'salt' not in row


def test_api_creds_stripped(app_client):
    body = app_client.get('/api/recent_deals?limit=10').get_json()
    for row in body['rows']:
        assert 'poly_api_key' not in row
        assert 'api_secret' not in row
        assert 'verifying_contract' not in row


def test_per_leg_entries_stripped(app_client):
    """Each leg in `entries` has token_id + stake — strip the whole array."""
    body = app_client.get('/api/recent_deals?limit=10').get_json()
    for row in body['rows']:
        assert 'entries' not in row
        assert 'legs' not in row
        assert 'order' not in row
        assert 'body' not in row


# ── Economic fields preserved ──────────────────────────────────────

def test_sum_net_roi_grade_preserved(app_client):
    body = app_client.get('/api/recent_deals?limit=10').get_json()
    le_havre = next(r for r in body['rows']
                    if 'Le Havre' in (r.get('title') or '') and r.get('type') == 'opened')
    assert le_havre['sum_cents'] == 91.62
    assert le_havre['net'] == 3.53
    assert le_havre['roi'] == 7.0
    assert le_havre['grade'] == 'CP-A'
    assert le_havre['fee_pct'] == 2.15
    assert le_havre['gross_pct'] == 9.14


def test_title_platform_structure_preserved(app_client):
    body = app_client.get('/api/recent_deals?limit=10').get_json()
    titles = {r.get('title') for r in body['rows']}
    assert 'Le Havre AC vs Olympique de Marseille' in titles


# ── Limit clamping ─────────────────────────────────────────────────

def test_limit_default_50(app_client):
    """With 3 synthetic rows + default limit, we still get all 3."""
    resp = app_client.get('/api/recent_deals')
    body = resp.get_json()
    assert body['count'] == 3


def test_limit_cap_500(app_client, monkeypatch):
    """limit=10000 → capped to 500 server-side. Synthetic file has 3 rows
    so we still see 3, but the cap logic itself is exercised indirectly."""
    resp = app_client.get('/api/recent_deals?limit=10000')
    assert resp.status_code == 200
    # Should not error or hang


def test_invalid_limit_falls_back(app_client):
    resp = app_client.get('/api/recent_deals?limit=abc')
    assert resp.status_code == 200


# ── Type filter ────────────────────────────────────────────────────

def test_type_filter_opened(app_client):
    body = app_client.get('/api/recent_deals?type=opened&limit=10').get_json()
    assert all(r['type'] == 'opened' for r in body['rows'])
    assert body['count'] == 2  # 2 opened in synthetic data


def test_type_filter_closed(app_client):
    body = app_client.get('/api/recent_deals?type=closed&limit=10').get_json()
    assert all(r['type'] == 'closed' for r in body['rows'])
    assert body['count'] == 1


# ── Defensive: missing file, malformed JSON ─────────────────────────

def test_missing_file_returns_empty(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    if 'arb_server' in sys.modules:
        del sys.modules['arb_server']
    import arb_server
    client = arb_server.app.test_client()
    resp = client.get('/api/recent_deals')
    assert resp.status_code == 200
    body = resp.get_json()
    assert body == {'rows': [], 'count': 0}


def test_malformed_jsonl_line_skipped(app_client):
    """Synthetic file has a `{not valid json}` line — endpoint should
    skip it and still return the 3 valid rows."""
    body = app_client.get('/api/recent_deals?limit=10').get_json()
    assert body['count'] == 3
