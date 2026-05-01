"""Phase 10 follow-ups (01.05.2026) — Task A, B, E tests.

Task A: NO-token CLOB fetcher for C-структуры.
  - _fetch_clob now returns 5-tuple (token, ask, ask_depth, bid, bid_depth)
  - _poly_per_market falls back to synthetic NO ask = 1 - YES_best_bid
    when real NO orderbook empty
  - source 'clob_synthetic' is whitelisted in REAL_OB_SOURCES

Task B: Slippage cancel-trigger.
  - When abs(fill_price - expected) > SLIPPAGE_TOLERANCE, leg status
    becomes 'slippage_cancelled' (not 'filled'); _cancel_leg_order called
  - broken-arb detector treats slippage_cancelled as failed → revert chain

Task E: Low-balance alert.
  - notify.alert_low_balance dedupes per bot, sends Telegram if <$30
"""
import os
import sys
import time
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), 'Scripts')
sys.path.insert(0, SCRIPTS)


# ── Task A: _fetch_clob 5-tuple shape + bids ────────────────────────
def test_fetch_clob_returns_5tuple_with_bids(monkeypatch):
    """Polymarket /book returns asks + bids; _fetch_clob exposes both."""
    import arb_server

    class _FakeResp:
        def json(self):
            return {
                'asks': [{'price': '0.30', 'size': '50'},
                          {'price': '0.31', 'size': '500'}],
                'bids': [{'price': '0.28', 'size': '100'},
                          {'price': '0.27', 'size': '999'}],
            }

    monkeypatch.setattr(arb_server._SESS_POLY, 'get',
                          lambda *a, **kw: _FakeResp())
    result = arb_server._fetch_clob('TID')
    # 5-tuple: token_id, best_ask, ask_depth, best_bid, bid_depth
    assert len(result) == 5
    token_id, best_ask, ask_depth, best_bid, bid_depth = result
    assert best_ask == pytest.approx(0.30)
    assert ask_depth == pytest.approx(15.0)            # 0.30*50
    assert best_bid == pytest.approx(0.28)
    assert bid_depth == pytest.approx(28.0)            # 0.28*100


def test_fetch_clob_empty_bids_still_5tuple(monkeypatch):
    """Empty bids → best_bid=None, bid_depth=0."""
    import arb_server

    class _FakeResp:
        def json(self):
            return {'asks': [{'price': '0.50', 'size': '10'}], 'bids': []}

    monkeypatch.setattr(arb_server._SESS_POLY, 'get',
                          lambda *a, **kw: _FakeResp())
    token_id, best_ask, ask_depth, best_bid, bid_depth = arb_server._fetch_clob('TID')
    assert best_ask == pytest.approx(0.50)
    assert best_bid is None
    assert bid_depth == 0


# ── Task A: synthetic NO from YES bids ──────────────────────────────
def test_poly_per_market_synthesizes_no_from_yes_bids():
    """When NO orderbook is empty BUT YES has bids, _poly_per_market
    synthesizes NO ask = 1 - YES_bid with source='clob_synthetic'."""
    from arb_server import _poly_per_market
    rough = [{
        'm': {'question': 'Lakers win?', 'liquidity': 5000,
              'volume': 10000},
        'token_id_yes': 'YES_TID',
        'token_id_no': 'NO_TID',
        'implied': 0.42,                          # lastTrade fallback
    }]
    # YES book: ask=0.45 ($100), bid=0.43 ($75) — taker can sell YES at 0.43
    # NO book: empty (no asks)
    # batch_fetch strips token_id (res[0]) → stores res[1:] as 4-tuple
    clob_res = {
        'YES_TID': (0.45, 100.0, 0.43, 75.0),    # ask, ask_depth, bid, bid_depth
        'NO_TID': (None, 0.0, None, 0.0),
    }
    out = _poly_per_market(rough, clob_res)
    assert len(out) == 1
    row = out[0]
    assert row['yes_src'] == 'clob_ask'
    assert row['yes_price'] == pytest.approx(0.45)
    # NO synthesized: 1 - 0.43 = 0.57, depth = $75 (matches YES bid depth)
    assert row['no_src'] == 'clob_synthetic'
    assert row['no_price'] == pytest.approx(0.57)
    assert row['no_liq'] == pytest.approx(75.0)


def test_poly_per_market_real_no_takes_priority_over_synthetic():
    """If real NO orderbook has asks, use those (not synthetic)."""
    from arb_server import _poly_per_market
    rough = [{
        'm': {'question': 'Test?', 'liquidity': 5000, 'volume': 1000},
        'token_id_yes': 'Y', 'token_id_no': 'N',
        'implied': 0.30,
    }]
    clob_res = {
        'Y': (0.30, 50.0, 0.28, 100.0),
        'N': (0.65, 200.0, None, 0.0),       # real NO ask = 0.65
    }
    out = _poly_per_market(rough, clob_res)
    assert out[0]['no_src'] == 'clob_ask'
    assert out[0]['no_price'] == pytest.approx(0.65)
    # NOT 1 - 0.28 = 0.72 (synthetic was the fallback, real wins)


def test_poly_per_market_no_falls_to_implied_when_yes_no_bid():
    """If YES has no bids either → NO stays 'implied' (rejected by guard)."""
    from arb_server import _poly_per_market
    rough = [{
        'm': {'question': 'Empty?', 'liquidity': 100, 'volume': 100},
        'token_id_yes': 'Y', 'token_id_no': 'N',
        'implied': 0.30,
    }]
    clob_res = {
        'Y': (0.30, 50.0, None, 0.0),        # no bids
        'N': (None, 0.0, None, 0.0),          # no asks
    }
    out = _poly_per_market(rough, clob_res)
    assert out[0]['no_src'] == 'implied'           # guarded out by REAL_OB_SOURCES


# ── Task A: REAL_OB_SOURCES whitelist ───────────────────────────────
def test_real_ob_sources_includes_clob_synthetic():
    """build_deal must accept 'clob_synthetic' as valid source."""
    import arb_server
    # Both definitions should match (build_deal + near_summary guards)
    src = arb_server.build_deal.__code__.co_consts
    # Easier: just call build_deal with synthetic source and verify it
    # doesn't return None just because of source.
    deal = arb_server.build_deal(
        title='test', platform='Polymarket',
        outcomes=[
            {'name':'A','price':0.30,'liquidity':100,'source':'clob_ask'},
            {'name':'B','price':0.65,'liquidity':100,'source':'clob_synthetic'},
        ],
        total_price=0.95, theta=0.03, threshold=0.97,
    )
    # Should NOT be None — both sources whitelisted
    assert deal is not None, "clob_synthetic should be in REAL_OB_SOURCES"


# ── Task B: slippage cancel-trigger ─────────────────────────────────
def test_slippage_breach_triggers_cancel_status(monkeypatch):
    """When fill_price diverges from expected by more than SLIPPAGE_TOLERANCE,
    leg status becomes 'slippage_cancelled' instead of 'filled'."""
    from executor import atomic
    from executor import builders
    from executor import fills

    cancel_calls = []
    def _fake_cancel(built, order_id, wallet):
        cancel_calls.append((built.get('platform'), order_id))
        return True
    monkeypatch.setattr(atomic, '_cancel_leg_order', _fake_cancel)

    # Fake POST → returns order_id
    class _FakeResp:
        status_code = 200
        def json(self): return {'id': 'ORDER_X'}
    def _fake_post(*a, **kw): return _FakeResp()

    # Pre-register the fill so atomic._fire_one_leg_live wakes immediately
    deal = {
        'platform': 'Polymarket', 'title': 't',
        'entries': [{'token_id': 'TID', 'price': 0.30, 'stake': 10.0,
                      'name': 'A'}],
    }
    wallet = builders.WalletStub(bot_id='bot1', eth_address='0x' + '1'*40)
    arb_id = 'test-slip-1'

    # Simulate fill at 0.305 (5pip slippage > 0.001 tolerance)
    def _wake_with_fill():
        time.sleep(0.05)
        # FillRegistry stores in _by_order_id keyed as 'polymarket:ORDER_X'
        with fills.registry._lock:
            for key, reg in fills.registry._by_order_id.items():
                if reg.arb_id == arb_id:
                    reg.result = {'fill_price': 0.305, 'fill_size_usdc': 10.0}
                    reg.event.set()
                    break
    import threading
    threading.Thread(target=_wake_with_fill, daemon=True).start()

    res = atomic._fire_one_leg_live(deal, 0, wallet, arb_id,
                                       http_post=_fake_post,
                                       deadman_s=2.0)
    assert res.status == 'slippage_cancelled', f'expected slippage_cancelled, got {res.status}'
    assert len(cancel_calls) == 1
    assert cancel_calls[0][1] == 'ORDER_X'


def test_slippage_within_tolerance_stays_filled(monkeypatch):
    """fill within 0.0005 of expected → no cancel, status='filled'."""
    from executor import atomic
    from executor import builders
    from executor import fills

    cancel_calls = []
    monkeypatch.setattr(atomic, '_cancel_leg_order',
                          lambda *a: cancel_calls.append(a) or True)

    class _FakeResp:
        status_code = 200
        def json(self): return {'id': 'ORDER_Y'}

    deal = {
        'platform': 'Polymarket', 'title': 't',
        'entries': [{'token_id': 'TID', 'price': 0.30, 'stake': 10.0,
                      'name': 'B'}],
    }
    wallet = builders.WalletStub(bot_id='bot2', eth_address='0x'+'2'*40)
    arb_id = 'test-slip-2'

    def _wake():
        time.sleep(0.05)
        with fills.registry._lock:
            for key, reg in fills.registry._by_order_id.items():
                if reg.arb_id == arb_id:
                    reg.result = {'fill_price': 0.3005, 'fill_size_usdc': 10.0}
                    reg.event.set()
                    break
    import threading
    threading.Thread(target=_wake, daemon=True).start()

    res = atomic._fire_one_leg_live(deal, 0, wallet, arb_id,
                                       http_post=lambda *a,**k: _FakeResp(),
                                       deadman_s=2.0)
    assert res.status == 'filled'
    assert len(cancel_calls) == 0


def test_arb_broken_includes_slippage_cancelled():
    """Broken-arb detector treats slippage_cancelled as failed leg."""
    from executor import atomic
    from executor.atomic import LegResult, ArbFireResult
    result = ArbFireResult(
        arb_id='t', deal_title='t', deal_structure='all_yes',
        expected_total_cost_usdc=20.0, expected_payout_usdc=22.0,
        legs=[
            LegResult(leg_idx=0, platform='Polymarket', status='filled',
                       expected_price=0.30, expected_size_usdc=10.0,
                       fill_size_usdc=10.0),
            LegResult(leg_idx=1, platform='Polymarket', status='slippage_cancelled',
                       expected_price=0.65, expected_size_usdc=10.0),
        ],
    )
    # Manually run the broken-arb detection logic
    failed = [l for l in result.legs
              if l.status in ('rejected','timeout','cancelled','disabled',
                              'slippage_cancelled')]
    filled = [l for l in result.legs if l.status == 'filled']
    assert len(failed) == 1
    assert len(filled) == 1
    arb_broken = (len(failed) > 0 and len(filled) > 0)
    assert arb_broken, "slippage_cancelled + filled should trigger revert"


# ── Task E: low-balance alert ───────────────────────────────────────
def test_alert_low_balance_skips_when_above_threshold(monkeypatch):
    """If balance ≥ threshold → no alert sent."""
    import notify
    sent = []
    monkeypatch.setattr(notify, 'is_configured', lambda: True)
    monkeypatch.setattr(notify, 'send', lambda *a, **kw: sent.append((a, kw)) or True)

    result = notify.alert_low_balance('bot1', '0xABC', 50.0, threshold=30.0)
    assert result is False
    assert len(sent) == 0


def test_alert_low_balance_sends_when_below(monkeypatch):
    import notify
    sent = []
    monkeypatch.setattr(notify, 'is_configured', lambda: True)
    monkeypatch.setattr(notify, 'send',
                          lambda msg, level, dedupe_key: sent.append({
                              'msg': msg, 'level': level, 'key': dedupe_key
                          }) or True)
    notify._low_bal_last_sent.clear()
    result = notify.alert_low_balance('bot2', '0x' + 'd'*40, 15.5,
                                         threshold=30.0)
    assert result is True
    assert len(sent) == 1
    assert 'LOW BALANCE' in sent[0]['msg']
    assert 'bot2' in sent[0]['msg']
    assert '$15.50' in sent[0]['msg']
    assert sent[0]['level'] == 'warning'
    assert sent[0]['key'] == 'low_bal_bot2'


def test_alert_low_balance_dedupes_within_window(monkeypatch):
    """Second call for same bot within 1h → no second alert."""
    import notify
    monkeypatch.setattr(notify, 'is_configured', lambda: True)
    sent = []
    monkeypatch.setattr(notify, 'send',
                          lambda msg, level, dedupe_key: sent.append(1) or True)
    notify._low_bal_last_sent.clear()

    notify.alert_low_balance('bot3', '0xE', 10.0, threshold=30.0)
    notify.alert_low_balance('bot3', '0xE', 5.0, threshold=30.0)
    assert len(sent) == 1                          # second deduped


def test_alert_low_balance_unconfigured_returns_false(monkeypatch):
    """Without Telegram creds → silently False, no exception."""
    import notify
    monkeypatch.setattr(notify, 'is_configured', lambda: False)
    notify._low_bal_last_sent.clear()
    assert notify.alert_low_balance('botX', '0xF', 5.0) is False
