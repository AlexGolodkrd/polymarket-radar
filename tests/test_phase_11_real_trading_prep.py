"""Phase 11 (01.05.2026) — Task F (depth-within-tolerance) + position log
writing + web3 wiring.

F: depth counted now includes levels within DEPTH_SLIPPAGE_TOLERANCE (0.005)
   of best ask. Aligned with raised SLIPPAGE_TOLERANCE (0.001 → 0.005) so
   the executor accepts fills inside the same window the depth math claimed
   was fillable.

Position log: after each successful fill, _write_position_row appends to
   Executions/positions.jsonl. reconcile loop reads this for local truth.
"""
import json
import os
import sys
import tempfile
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), 'Scripts')
sys.path.insert(0, SCRIPTS)


# ── Task F: depth-within-tolerance ──────────────────────────────────
def test_top_of_book_with_default_tolerance_includes_ladder(monkeypatch):
    """With DEPTH_SLIPPAGE_TOLERANCE=0.005 (default), levels within 0.5c
    of best are summed. Ladder book where MMs stack on close ticks now
    reports realistic fillable USD."""
    import arb_server

    class _FakeResp:
        def json(self):
            return {
                'asks': [
                    {'price': '0.300', 'size': '50'},     # best
                    {'price': '0.302', 'size': '200'},    # +0.2c — within 0.5c
                    {'price': '0.304', 'size': '300'},    # +0.4c — within 0.5c
                    {'price': '0.310', 'size': '999'},    # +1.0c — outside
                ],
                'bids': [
                    {'price': '0.295', 'size': '100'},    # best bid
                    {'price': '0.293', 'size': '500'},    # within 0.5c
                ],
            }

    monkeypatch.setattr(arb_server._SESS_POLY, 'get',
                          lambda *a, **kw: _FakeResp())
    token, ask, ask_depth, bid, bid_depth = arb_server._fetch_clob('TID')
    assert ask == pytest.approx(0.300)
    # ask_depth should sum first 3 levels: 0.300*50 + 0.302*200 + 0.304*300 = $166.6
    expected_ask_depth = 0.300 * 50 + 0.302 * 200 + 0.304 * 300
    assert ask_depth == pytest.approx(expected_ask_depth)
    # NOT including 0.310*999 = $309.69 (outside tolerance)
    assert ask_depth < 200
    # bid side same logic — best=0.295, +0.293 within tolerance
    assert bid == pytest.approx(0.295)
    expected_bid_depth = 0.295 * 100 + 0.293 * 500
    assert bid_depth == pytest.approx(expected_bid_depth)


def test_top_of_book_tolerance_skips_outside_levels(monkeypatch):
    """Level 0.6c above best is NOT counted (outside default 0.5c)."""
    import arb_server

    class _FakeResp:
        def json(self):
            return {
                'asks': [{'price': '0.30', 'size': '50'},
                          {'price': '0.306', 'size': '500'}],   # +0.6c
                'bids': [],
            }

    monkeypatch.setattr(arb_server._SESS_POLY, 'get',
                          lambda *a, **kw: _FakeResp())
    _, ask, ask_depth, _, _ = arb_server._fetch_clob('T')
    assert ask == pytest.approx(0.30)
    assert ask_depth == pytest.approx(0.30 * 50)               # NOT 165


def test_depth_tolerance_envvar_override(monkeypatch):
    """DEPTH_SLIPPAGE_TOLERANCE env override changes default behavior."""
    # Test the helper directly with explicit tolerance
    from arb_server import _top_of_book_depth_usd
    asks = [{'price': 0.30, 'size': 50}, {'price': 0.305, 'size': 200}]
    # Strict (0) — only top
    _, depth_strict = _top_of_book_depth_usd(asks, slippage_tolerance=0.0)
    assert depth_strict == pytest.approx(15.0)
    # Loose (0.01 = 1c) — both
    _, depth_loose = _top_of_book_depth_usd(asks, slippage_tolerance=0.01)
    assert depth_loose == pytest.approx(0.30*50 + 0.305*200)


def test_slippage_tolerance_default_is_0_005():
    """atomic.SLIPPAGE_TOLERANCE raised to match depth tolerance."""
    from executor import atomic
    # Default reads from env; without env it should be 0.005
    if 'SLIPPAGE_TOLERANCE' not in os.environ:
        assert atomic.SLIPPAGE_TOLERANCE == pytest.approx(0.005)


# ── Position log writing ────────────────────────────────────────────
def test_write_position_row_appends_jsonl(tmp_path, monkeypatch):
    """_write_position_row writes one JSONL row per fill, with all required
    fields for reconcile to compare."""
    from executor import atomic
    from executor.atomic import LegResult
    from executor import builders

    # Redirect to tmp file
    pos_path = tmp_path / 'positions.jsonl'
    monkeypatch.setattr(atomic, '_positions_log_path',
                          lambda: str(pos_path))

    deal = {
        'platform': 'Polymarket',
        'entries': [{'condition_id': '0xCOND',
                      'name': 'YES',
                      'token_id_yes': 'TY'}],
    }
    wallet = builders.WalletStub(bot_id='bot1', eth_address='0x' + '1'*40)
    leg = LegResult(
        leg_idx=0, platform='Polymarket', status='filled',
        expected_price=0.30, expected_size_usdc=10.0,
        fill_price=0.302, fill_size_usdc=10.0, bot_id='bot1',
    )
    ok = atomic._write_position_row(deal, 0, leg, wallet)
    assert ok
    rows = pos_path.read_text(encoding='utf-8').strip().split('\n')
    assert len(rows) == 1
    row = json.loads(rows[0])
    assert row['platform'] == 'Polymarket'
    assert row['market_id'] == '0xCOND'
    assert row['outcome'] == 'YES'
    assert row['size_usdc'] == 10.0
    assert row['fill_price'] == 0.302
    assert row['bot_id'] == 'bot1'
    assert 'ts_unix' in row


def test_write_position_row_negative_size_for_sell(tmp_path, monkeypatch):
    """Revert (SELL) legs write NEGATIVE size_usdc so reconcile sees
    net position correctly."""
    from executor import atomic
    from executor.atomic import LegResult
    from executor import builders

    pos_path = tmp_path / 'p.jsonl'
    monkeypatch.setattr(atomic, '_positions_log_path', lambda: str(pos_path))

    deal = {
        'platform': 'Polymarket',
        'entries': [{'condition_id': 'CC', 'name': 'NO',
                      'token_id_yes': 'T', 'side': 'SELL'}],
    }
    wallet = builders.WalletStub(bot_id='bot2', eth_address='0x' + '2'*40)
    leg = LegResult(
        leg_idx=0, platform='Polymarket', status='filled',
        expected_price=0.50, expected_size_usdc=20.0,
        fill_price=0.50, fill_size_usdc=20.0, bot_id='bot2',
    )
    atomic._write_position_row(deal, 0, leg, wallet)
    row = json.loads(pos_path.read_text(encoding='utf-8').strip())
    assert row['size_usdc'] == -20.0


def test_position_log_round_trips_through_reconcile(tmp_path, monkeypatch):
    """End-to-end: write a position via atomic, then reconcile reads it
    in the same key shape."""
    from executor import atomic
    from executor.atomic import LegResult
    from executor import builders
    from risk import reconcile

    pos_path = tmp_path / 'positions.jsonl'
    monkeypatch.setattr(atomic, '_positions_log_path', lambda: str(pos_path))
    monkeypatch.setattr(reconcile, 'POSITIONS_LOG', str(pos_path))

    deal = {
        'platform': 'Polymarket',
        'entries': [{'condition_id': 'COND_A', 'name': 'YES',
                      'token_id_yes': 'T'}],
    }
    wallet = builders.WalletStub(bot_id='bot1', eth_address='0x' + 'a'*40)
    leg = LegResult(leg_idx=0, platform='Polymarket', status='filled',
                     expected_price=0.30, expected_size_usdc=15.0,
                     fill_price=0.30, fill_size_usdc=15.0, bot_id='bot1')
    atomic._write_position_row(deal, 0, leg, wallet)

    local = reconcile._read_local_positions()
    key = ('Polymarket', 'COND_A', 'YES')
    assert key in local
    assert local[key] == pytest.approx(15.0)


# ── Sanity: existing top-of-book tests still pass with default tolerance ──
def test_existing_helper_signature_back_compat():
    """_top_of_book_depth_usd default tolerance=0 still produces strict
    top-of-book, preserving Phase 10 #51 semantic for callers that pass
    tolerance=0 explicitly."""
    from arb_server import _top_of_book_depth_usd
    asks = [{'price': 0.30, 'size': 50},
             {'price': 0.31, 'size': 999}]
    best, depth = _top_of_book_depth_usd(asks, slippage_tolerance=0)
    assert best == pytest.approx(0.30)
    assert depth == pytest.approx(15.0)         # NOT including 0.31*999
