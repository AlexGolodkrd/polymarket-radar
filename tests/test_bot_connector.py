"""Tests for BotConnector wrapper (executor/bot_connector.py).

Covers dry-run path for all 3 enabled platforms + reject paths.
"""
import os, sys, pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


@pytest.fixture
def connector():
    from executor.bot_connector import BotConnector
    from executor.builders import WalletStub
    wallets = [
        WalletStub(bot_id='bot1', eth_address='0x' + '1' * 40),
        WalletStub(bot_id='bot2', eth_address='0x' + '2' * 40),
    ]
    return BotConnector(wallets, dry_run=True)


def test_unsupported_platform(connector):
    res = connector.place_order(
        platform='Polychain', market_id='X', side='BUY',
        price=0.45, size=10.0, wallet_id='bot1')
    assert res['status'] == 'rejected'
    assert 'unsupported platform' in res['error']


def test_unknown_wallet(connector):
    res = connector.place_order(
        platform='Polymarket', market_id='123', side='BUY',
        price=0.5, size=5.0, wallet_id='ghost-bot')
    assert res['status'] == 'rejected'
    assert 'ghost-bot' in res['error']


def test_bad_side(connector):
    res = connector.place_order(
        platform='Polymarket', market_id='123', side='HOLD',
        price=0.5, size=5.0, wallet_id='bot1')
    assert res['status'] == 'rejected'


def test_sx_requires_outcome(connector):
    res = connector.place_order(
        platform='SX Bet', market_id='0xHASH', side='BUY',
        price=0.5, size=5.0, wallet_id='bot1')
    assert res['status'] == 'rejected'
    assert 'outcome' in res['error']


def test_polymarket_dry_run(connector):
    res = connector.place_order(
        platform='Polymarket', market_id='12345678901234567890',
        side='BUY', price=0.45, size=10.0, wallet_id='bot1',
        neg_risk=False, tick_size=0.01)
    # In dry-run we expect either dry-fired or a structured rejection
    # (e.g., risk-blocked / preflight). Critically not an unhandled crash.
    assert res['platform'] == 'Polymarket'
    assert res['arb_id']        # arb_id always populated
    assert res['wallet_id'] == 'bot1'


def test_sx_dry_run_with_outcome(connector):
    res = connector.place_order(
        platform='SX Bet', market_id='0x' + 'a' * 64, side='BUY',
        price=0.50, size=5.0, wallet_id='bot1', outcome=1)
    assert res['platform'] == 'SX Bet'
    assert res['arb_id']


def test_limitless_dry_run(connector):
    res = connector.place_order(
        platform='Limitless', market_id='lakers-celtics-2026',
        side='BUY', price=0.42, size=8.0, wallet_id='bot2')
    assert res['platform'] == 'Limitless'
    assert res['arb_id']
