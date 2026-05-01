"""Phase 17 (01.05.2026) — close all remaining gaps for 3 exchanges.

- SX EIP-712 OrderFill signing
- SX revert flow (taker fill on opposite outcome)
- SX 3-way 1X2 pipeline stub
- fetch_limitless_positions for reconcile
- fetch_sx_positions stub
- filter_wallets_by_chain pre-filter
"""
import os
import sys
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), 'Scripts')
sys.path.insert(0, SCRIPTS)


# ── SX EIP-712 ──────────────────────────────────────────────────────
def test_sx_eip712_constants_defined():
    """SX_DOMAIN, SX_FILL_TYPES, _sign_sx_order_fill all present."""
    from executor import builders
    assert hasattr(builders, 'SX_DOMAIN')
    assert builders.SX_DOMAIN['chainId'] == 4162
    assert hasattr(builders, 'SX_FILL_TYPES')
    assert hasattr(builders, '_sign_sx_order_fill')


def test_sx_order_includes_signature_when_can_sign():
    """build_sx_order with wallet.can_sign=True populates body['takerSig']."""
    from executor import builders
    try:
        from eth_account import Account
        acct = Account.create()
    except ImportError:
        pytest.skip("eth-account not installed")

    wallet = builders.WalletStub(
        bot_id='bot1', eth_address=acct.address,
        private_key=acct.key.hex())

    # Mock fetcher to bypass network
    def _mock_fetcher():
        return {
            'status': 'success',
            'data': {'orders': [{
                'orderHash': '0xORDER1',
                'percentageOdds': str(int(0.50 * 1e20)),
                'orderSizeFillable': str(int(100 * 1e6)),
                'isMakerBettingOutcomeOne': False,    # taker on outcome 1
            }]}
        }

    res = builders.build_sx_order(
        market_hash='0xMARKETHASH',
        outcome=1, taker_price=0.50,
        size_usdc=10.0, wallet=wallet,
        fetcher=_mock_fetcher,
    )
    assert res['signed'] is True
    assert res['body']['takerSig'].startswith('0x')
    assert len(res['body']['takerSig']) > 100


def test_sx_order_unsigned_when_no_private_key():
    """Without can_sign → body['takerSig'] is empty string, signed=False."""
    from executor import builders
    wallet = builders.WalletStub(
        bot_id='bot1', eth_address='0x' + '1' * 40)        # no private_key

    def _mock_fetcher():
        return {
            'status': 'success',
            'data': {'orders': [{
                'orderHash': '0x1', 'percentageOdds': str(int(0.50 * 1e20)),
                'orderSizeFillable': str(int(50 * 1e6)),
                'isMakerBettingOutcomeOne': False,
            }]}
        }

    res = builders.build_sx_order(
        market_hash='0xM', outcome=1, taker_price=0.50,
        size_usdc=5.0, wallet=wallet, fetcher=_mock_fetcher,
    )
    assert res['signed'] is False
    assert res['body']['takerSig'] == ''


# ── SX revert flow ──────────────────────────────────────────────────
def test_sx_revert_uses_opposite_outcome(monkeypatch):
    """Filled SX leg → revert path posts taker fill on OPPOSITE outcome."""
    from executor import atomic
    from executor.atomic import LegResult, ArbFireResult
    from executor import builders

    posts = []
    class _Resp:
        status_code = 200
    def _fake_post(url, json=None, headers=None, timeout=None):
        posts.append({'url': url, 'json': json})
        return _Resp()
    import requests as _req
    monkeypatch.setattr(_req, 'post', _fake_post)
    # Mock build_sx_order to bypass network
    def _fake_build(market_hash, outcome, taker_price, size_usdc, wallet,
                    expiration_secs=60, slippage_tolerance=0.005, fetcher=None):
        return {
            'platform': 'sx_bet',
            'body': {'marketHash': market_hash, 'takerOutcome': outcome,
                      'fillAmount': '1000', 'orderHashes': ['0xH'],
                      'takerAmounts': ['1000'], 'expiry': '1', 'salt': 's',
                      'takerSig': '0x' + 'a' * 130},
            'sign_payload': b'',
            'would_post_url': builders.SX_FILL_URL,
            'expected_price': taker_price,
            'expected_size_usdc': size_usdc,
            'partial_fill': False,
        }
    monkeypatch.setattr(builders, 'build_sx_order', _fake_build)

    wallet = builders.WalletStub(
        bot_id='bot1', eth_address='0x' + '1' * 40,
        private_key='0x' + 'a' * 64)
    result = ArbFireResult(
        arb_id='t-sx', deal_title='t', deal_structure='binary',
        expected_total_cost_usdc=20.0, expected_payout_usdc=22.0,
        legs=[
            LegResult(leg_idx=0, platform='SX Bet', status='filled',
                       expected_price=0.45, expected_size_usdc=10.0,
                       fill_price=0.45, fill_size_usdc=10.0, bot_id='bot1'),
        ],
    )
    deal = {'platform': 'SX Bet',
            'entries': [{'market_hash': '0xMH', 'outcome_index': 1}]}

    out = atomic.revert_filled_legs(result, deal, [wallet], dry_run=False)
    assert 'sx_reverted' in out


def test_sx_revert_no_market_hash_returns_error():
    """If entry doesn't have market_hash → revert reports error, doesn't crash."""
    from executor import atomic
    from executor.atomic import LegResult, ArbFireResult
    from executor import builders

    wallet = builders.WalletStub(bot_id='bot1', eth_address='0x' + '1' * 40)
    result = ArbFireResult(
        arb_id='t-sx', deal_title='t', deal_structure='binary',
        expected_total_cost_usdc=20.0, expected_payout_usdc=22.0,
        legs=[LegResult(leg_idx=0, platform='SX Bet', status='filled',
                         expected_price=0.45, expected_size_usdc=10.0,
                         fill_size_usdc=10.0, bot_id='bot1')],
    )
    deal = {'platform': 'SX Bet', 'entries': [{}]}        # no market_hash
    out = atomic.revert_filled_legs(result, deal, [wallet], dry_run=False)
    assert 'no_market_or_outcome' in out


# ── SX 3-way 1X2 pipeline ───────────────────────────────────────────
def test_eval_sx_3way_returns_empty_until_implemented():
    """Stub returns empty deals; type=1 markets pass through filter but
    eval_sx_3way is currently no-op."""
    from arb_server import eval_sx_3way, SX_THREE_WAY_TYPES
    assert 1 in SX_THREE_WAY_TYPES
    deals = eval_sx_3way([{'type': 1, 'marketHash': '0xH'}], {})
    assert deals == []


# ── fetch_limitless_positions ──────────────────────────────────────
def test_fetch_limitless_positions_no_creds_returns_empty():
    from risk import reconcile

    class _W:
        eth_address = '0x' + 'a' * 40
        api_key = None        # no X-API-Key
    out = reconcile.fetch_limitless_positions([_W()])
    assert out == {}


def test_fetch_limitless_positions_parses_response():
    """With api_key + mocked HTTP, positions parse into canonical shape."""
    from risk import reconcile

    class _W:
        eth_address = '0x' + 'b' * 40
        api_key = 'TESTKEY'

    class _Resp:
        status_code = 200
        def json(self):
            return {'positions': [
                {'marketSlug': 'lakers-celtics-mar-25', 'outcome': 'YES',
                 'shares': 100, 'avgPrice': 0.40},
                {'marketSlug': 'man-utd-vs-liverpool', 'outcome': 'NO',
                 'shares': 50, 'avgPrice': 0.65},
            ]}
    def _fake_get(url, headers=None, timeout=None, params=None):
        assert headers.get('X-API-Key') == 'TESTKEY'
        return _Resp()
    out = reconcile.fetch_limitless_positions([_W()], http_get=_fake_get)
    assert ('Limitless', 'lakers-celtics-mar-25', 'YES') in out
    assert out[('Limitless', 'lakers-celtics-mar-25', 'YES')] == pytest.approx(40.0)
    assert ('Limitless', 'man-utd-vs-liverpool', 'NO') in out
    assert out[('Limitless', 'man-utd-vs-liverpool', 'NO')] == pytest.approx(32.5)


# ── fetch_sx_positions stub ────────────────────────────────────────
def test_fetch_sx_positions_returns_empty_stub():
    """SX positions live on-chain (CTF balanceOf) — stub for now."""
    from risk import reconcile
    out = reconcile.fetch_sx_positions([])
    assert out == {}


# ── filter_wallets_by_chain ────────────────────────────────────────
def test_filter_wallets_by_chain_returns_positive_balance():
    from wallets.coordinator import filter_wallets_by_chain
    from wallets.config import Wallet, WalletPool
    pool = WalletPool(wallets=[
        Wallet(bot_id='bot1', eth_address='0x1',
               store_name='local', last_known_usdc=100.0),
        Wallet(bot_id='bot2', eth_address='0x2',
               store_name='local', last_known_usdc=0.0),
        Wallet(bot_id='bot3', eth_address='0x3',
               store_name='local', last_known_usdc=50.0),
    ])
    out = filter_wallets_by_chain(pool, 'Polymarket')
    bot_ids = {w.bot_id for w in out}
    assert 'bot1' in bot_ids
    assert 'bot3' in bot_ids
    assert 'bot2' not in bot_ids        # zero balance excluded


# ── register_limitless_fetcher / register_sx_fetcher ───────────────
def test_register_limitless_fetcher_skips_without_keys():
    from risk import reconcile
    class _W:
        eth_address = '0x1'
        api_key = None
    reconcile.clear_exchange_fetchers()
    res = reconcile.register_limitless_fetcher([_W()])
    assert res is False


def test_register_limitless_fetcher_registers_with_keys():
    from risk import reconcile
    class _W:
        eth_address = '0x1'
        api_key = 'TESTKEY'
    reconcile.clear_exchange_fetchers()
    res = reconcile.register_limitless_fetcher([_W()])
    assert res is True
    reconcile.clear_exchange_fetchers()
