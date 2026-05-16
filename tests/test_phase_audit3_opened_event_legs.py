"""Phase audit-3 (15.05.2026) — leg-level identifiers in analytics_events.jsonl.

Operator pain (15.05.2026 evening): Saint Etienne vs Rodez ALL_NO deal
flashed in /api/deals for 44 seconds, then closed. To verify resolution
rules per leg, we had to dig the slug out of /api/scan_state. Once the
deal was gone from /api/deals, the leg slugs were unrecoverable from
analytics_events.jsonl — the `_snapshot()` snapshot dropped them.

Fix: `_snapshot()` now emits a `legs` array carrying every identifier
the upstream evaluators attached:
  * Limitless: slug, token_id, verifying_contract, side
  * Polymarket: condition_id, token_id_yes/no, neg_risk, tick_size
  * SX Bet:    market_hash, outcome_index, sport_type
  * Cross-platform: composite of all of the above + platform field.

A field is omitted from a leg dict if its value is None — so SX legs
don't pollute the JSON with `slug=null` and vice versa.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


def _make_deal(entries):
    """Minimal valid deal dict — only the fields _snapshot reads."""
    return {
        'title': 'Test fixture',
        'platform': 'Limitless',
        'total_cents': 91.0,
        'net': 0.10,
        'grade': 'D',
        'min_liq': 100.0,
        'balance_used': 1.0,
        'roi': 5.0,
        'adj': 0.05,
        'arb_structure': 'all_no',
        'end_date': '2026-06-01',
        'theta': 0.02,
        'cross_structure': None,
        'fee': 0.005,
        'gross': 0.10,
        'fee_pct': 0.5,
        'gross_pct': 5.0,
        'adj_roi': 5.0,
        'slip_pct': 1.0,
        'slip_cost': 0.01,
        'confidence': None,
        'entries': entries,
    }


# ── Snapshot shape ────────────────────────────────────────────────

def test_snapshot_has_legs_field():
    from analytics import _snapshot
    snap = _snapshot(_make_deal([
        {'name': 'NO Saint Etienne', 'price': 0.465, 'slug': 'saint-etienne-x',
         'token_id': 'tok_se', 'side': 'NO', 'stake': 0.59, 'contracts': 1.3,
         'fee': 0.0, 'liquidity': 465.0, 'share_pct': 23.6,
         'source': 'lim_clob', 'verifying_contract': '0xe3E00...'},
    ]))
    assert 'legs' in snap
    assert isinstance(snap['legs'], list)
    assert len(snap['legs']) == 1


def test_legs_preserves_limitless_ids():
    from analytics import _snapshot
    snap = _snapshot(_make_deal([
        {'name': 'NO Draw', 'price': 0.721, 'slug': 'draw-x',
         'token_id': 'tok_draw', 'side': 'NO', 'stake': 0.91,
         'contracts': 1.3, 'fee': 0.0, 'liquidity': 474000.0,
         'source': 'lim_clob', 'verifying_contract': '0xe3E0...'},
    ]))
    leg = snap['legs'][0]
    assert leg['slug'] == 'draw-x'
    assert leg['token_id'] == 'tok_draw'
    assert leg['side'] == 'NO'
    assert leg['verifying_contract'] == '0xe3E0...'
    # Economics passes through too
    assert leg['price'] == 0.721
    assert leg['stake'] == 0.91
    assert leg['source'] == 'lim_clob'


def test_legs_preserves_polymarket_ids():
    from analytics import _snapshot
    snap = _snapshot(_make_deal([
        {'name': 'YES Lakers', 'price': 0.48,
         'condition_id': '0xabc...', 'token_id_yes': 'tok_y', 'token_id_no': 'tok_n',
         'neg_risk': True, 'tick_size': 0.01, 'min_order_size': 5,
         'taker_fee_bps': 0, 'side': 'YES', 'stake': 0.48,
         'contracts': 1.0, 'liquidity': 1000.0, 'source': 'clob_ask',
         'accepting_orders': True, 'enable_order_book': True},
    ]))
    leg = snap['legs'][0]
    assert leg['condition_id'] == '0xabc...'
    assert leg['token_id_yes'] == 'tok_y'
    assert leg['token_id_no'] == 'tok_n'
    assert leg['neg_risk'] is True
    assert leg['tick_size'] == 0.01
    assert leg['taker_fee_bps'] == 0
    # Limitless-only fields must NOT appear
    assert 'slug' not in leg
    assert 'verifying_contract' not in leg
    # SX-only fields must NOT appear
    assert 'market_hash' not in leg


def test_legs_preserves_sx_ids():
    from analytics import _snapshot
    snap = _snapshot(_make_deal([
        {'name': 'Brentford', 'price': 0.585,
         'market_hash': '0xf745cf4a...', 'outcome_index': 1,
         'side': 'OUTCOME_1', 'sport_type': 1,
         'stake': 0.585, 'contracts': 1.0,
         'liquidity': 84.56, 'source': 'sx_ob'},
        {'name': 'Crystal Palace', 'price': 0.405,
         'market_hash': '0xf745cf4a...', 'outcome_index': 2,
         'side': 'OUTCOME_2', 'sport_type': 1,
         'stake': 0.405, 'contracts': 1.0,
         'liquidity': 50.0, 'source': 'sx_ob'},
    ]))
    legs = snap['legs']
    assert len(legs) == 2
    assert all(l['market_hash'] == '0xf745cf4a...' for l in legs)
    assert legs[0]['outcome_index'] == 1
    assert legs[1]['outcome_index'] == 2
    assert legs[0]['sport_type'] == 1
    # No Limitless fields
    assert 'slug' not in legs[0]
    assert 'token_id' not in legs[0]


def test_legs_omits_none_fields():
    """A leg dict carries only fields with non-None values — keeps the JSON
    tight and tells the analytics consumer "this field genuinely had no value"
    rather than "this field is unknown for this platform"."""
    from analytics import _snapshot
    snap = _snapshot(_make_deal([
        {'name': 'X', 'price': 0.5, 'slug': 'some-slug',
         'token_id': None, 'condition_id': None,
         'market_hash': None, 'side': 'YES'},
    ]))
    leg = snap['legs'][0]
    assert leg['slug'] == 'some-slug'
    assert leg['side'] == 'YES'
    # None-valued fields must be filtered out:
    assert 'token_id' not in leg
    assert 'condition_id' not in leg
    assert 'market_hash' not in leg


def test_legs_empty_when_no_entries():
    from analytics import _snapshot
    snap = _snapshot(_make_deal([]))
    assert snap['legs'] == []


def test_legs_handles_missing_entries_key():
    """Some legacy deal shapes might not have 'entries' at all."""
    from analytics import _snapshot
    deal = _make_deal([])
    del deal['entries']  # simulate missing
    snap = _snapshot(deal)
    assert snap['legs'] == []
