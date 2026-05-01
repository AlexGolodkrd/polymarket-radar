"""Phase 10 #51 (30.04.2026) — preflight + revert + derive tests.

Covers:
  - preflight.preflight_arb depth-only path (web3 unavailable → warn-not-fail)
  - preflight rejects insufficient depth
  - preflight rejects insufficient balance (mocked web3)
  - revert_filled_legs in dry-run mode logs decisions
  - revert_filled_legs handles no-filled-legs edge
  - poly_derive_api_creds dry-run produces sensible payload
  - poly_derive_api_creds writes Credentials.env idempotently
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


# ── preflight.check_depth ───────────────────────────────────────────
def test_check_depth_passes_when_stake_fits():
    import preflight
    ok, reason = preflight.check_depth(stake_usd=20.0,
                                          top_of_book_liquidity_usd=50.0)
    assert ok
    assert 'fits' in reason.lower()


def test_check_depth_fails_when_stake_exceeds_top_of_book():
    import preflight
    ok, reason = preflight.check_depth(stake_usd=100.0,
                                          top_of_book_liquidity_usd=30.0)
    assert not ok
    assert 'partial-fill' in reason or 'exceeds' in reason


def test_check_depth_fails_zero_liquidity():
    import preflight
    ok, reason = preflight.check_depth(stake_usd=10.0,
                                          top_of_book_liquidity_usd=0.0)
    assert not ok


# ── preflight.preflight_arb ─────────────────────────────────────────
def test_preflight_arb_passes_when_skip_web3():
    """Depth-only check passes when stake <= liquidity for all legs."""
    import preflight
    deal = {
        'platform': 'Polymarket',
        'entries': [
            {'stake': 10.0, 'liquidity': 50.0, 'price': 0.30},
            {'stake': 10.0, 'liquidity': 100.0, 'price': 0.65},
        ],
    }

    class _Wallet:
        eth_address = '0x' + '1' * 40
        bot_id = 'bot1'

    wallets = [_Wallet(), _Wallet()]
    res = preflight.preflight_arb(deal, wallets,
                                    skip_balance=True,
                                    skip_allowance=True)
    assert res.ok, f'expected pass, got failures: {res.failures}'
    assert len(res.leg_results) == 2


def test_preflight_arb_fails_on_depth_breach():
    """One leg's stake exceeds top-of-book → arb rejected."""
    import preflight
    deal = {
        'platform': 'Polymarket',
        'entries': [
            {'stake': 50.0, 'liquidity': 200.0, 'price': 0.30},
            {'stake': 50.0, 'liquidity': 10.0, 'price': 0.65},   # too small!
        ],
    }

    class _Wallet:
        eth_address = '0x' + '1' * 40

    res = preflight.preflight_arb(deal, [_Wallet(), _Wallet()],
                                    skip_balance=True,
                                    skip_allowance=True)
    assert not res.ok
    assert any('depth' in f for f in res.failures)


def test_preflight_arb_balance_insufficient_with_mock_web3():
    """Mocked web3 returns balance < stake → reject."""
    import preflight

    class _MockContract:
        def __init__(self, bal_raw, allow_raw):
            self._bal = bal_raw
            self._allow = allow_raw

        class _Func:
            def __init__(self, val):
                self._val = val

            def call(self):
                return self._val

        @property
        def functions(self):
            outer = self
            class F:
                def balanceOf(_, addr):
                    return _MockContract._Func(outer._bal)
                def allowance(_, owner, spender):
                    return _MockContract._Func(outer._allow)
            return F()

    class _MockWeb3:
        class _Eth:
            def __init__(self, c):
                self._c = c
            def contract(self, address, abi):
                return self._c
        def __init__(self, contract):
            self.eth = self._Eth(contract)

    # 5 USDC balance (raw 5_000_000), MAX allowance
    mock_w3 = _MockWeb3(_MockContract(5_000_000, 2**256 - 1))

    deal = {
        'platform': 'Polymarket',
        'entries': [
            {'stake': 50.0, 'liquidity': 999.0, 'price': 0.30},
        ],
    }

    class _Wallet:
        eth_address = '0x' + '2' * 40

    preflight.clear_cache()
    res = preflight.preflight_arb(deal, [_Wallet()],
                                    skip_balance=False,
                                    skip_allowance=False,
                                    web3_client=mock_w3)
    assert not res.ok
    assert any('balance' in f for f in res.failures)


def test_preflight_arb_warns_when_web3_unavailable():
    """No web3 path → balance check returns 'skipped' as warning,
    not failure. This matters because operator runs paper-trades
    before installing web3."""
    import preflight
    deal = {
        'platform': 'Polymarket',
        'entries': [{'stake': 5.0, 'liquidity': 100.0, 'price': 0.20}],
    }

    class _Wallet:
        eth_address = '0x' + '3' * 40

    # Force web3 unavailable by passing None and removing cache
    preflight.clear_cache()
    res = preflight.preflight_arb(deal, [_Wallet()],
                                    skip_balance=False,
                                    skip_allowance=False)
    # Either passes outright (web3 not installed) or includes warnings.
    # Should NOT fail just because web3 is missing.
    if not res.ok:
        # If it fails, it should be for depth, not balance/allowance
        assert any('depth' in f for f in res.failures), (
            f"Failed for non-depth reason without web3: {res.failures}")


# ── revert_filled_legs ──────────────────────────────────────────────
def test_revert_filled_legs_no_filled_returns_noop():
    """Result with only rejected legs → revert is a no-op."""
    from executor import atomic
    from executor.atomic import ArbFireResult, LegResult
    result = ArbFireResult(
        arb_id='test-1',
        deal_title='test',
        deal_structure='all_yes',
        expected_total_cost_usdc=20.0,
        expected_payout_usdc=22.0,
        legs=[
            LegResult(leg_idx=0, platform='Polymarket', status='rejected',
                       expected_price=0.30, expected_size_usdc=10.0),
            LegResult(leg_idx=1, platform='Polymarket', status='rejected',
                       expected_price=0.30, expected_size_usdc=10.0),
        ],
    )
    out = atomic.revert_filled_legs(result, {'entries': [{},{}]}, [], dry_run=True)
    assert 'no_filled_legs' in out


def test_revert_filled_legs_dryrun_logs_decisions(monkeypatch):
    """In dry-run, filled legs go through dryrun_log.log_order_decision
    with op='revert_sell'."""
    from executor import atomic
    from executor.atomic import ArbFireResult, LegResult
    from executor import dryrun_log

    captured = []
    def _capture(arb_id, leg_idx, built, bot_id, **kw):
        captured.append({'arb_id': arb_id, 'leg_idx': leg_idx,
                          'op': built.get('op'), 'side': (built.get('body') or {}).get('side')})
    monkeypatch.setattr(dryrun_log, 'log_order_decision', _capture)

    result = ArbFireResult(
        arb_id='test-2',
        deal_title='test',
        deal_structure='all_yes',
        expected_total_cost_usdc=20.0,
        expected_payout_usdc=22.0,
        legs=[
            LegResult(leg_idx=0, platform='Polymarket', status='filled',
                       expected_price=0.30, expected_size_usdc=10.0,
                       fill_size_usdc=10.0),
            LegResult(leg_idx=1, platform='Polymarket', status='timeout',
                       expected_price=0.65, expected_size_usdc=10.0),
        ],
    )
    deal = {'platform': 'Polymarket',
             'entries': [{'token_id': 'T0'}, {'token_id': 'T1'}]}
    atomic.revert_filled_legs(result, deal, [None, None], dry_run=True)
    assert len(captured) == 1
    assert captured[0]['op'] == 'revert_sell'
    assert captured[0]['side'] == 'SELL'
    assert captured[0]['leg_idx'] == 0


# ── poly_derive_api_creds ───────────────────────────────────────────
def test_poly_derive_dry_run_returns_payload_preview(monkeypatch):
    """Dry-run mode: signs the message, returns headers preview, doesn't
    hit the network. Useful for verifying signing pipeline before going
    live."""
    import poly_derive_api_creds as pdac
    eth_address = '0x' + 'a' * 40
    # Generate a throwaway keypair for the signature step
    try:
        from eth_account import Account
        acct = Account.create()
        pk = acct.key.hex()
    except ImportError:
        pytest.skip("eth-account not installed")

    out = pdac.derive_creds(acct.address, pk, dry_run=True)
    assert out['api_key'] == '<dry-run>'
    assert 'headers_preview' in out
    headers = out['headers_preview']
    assert 'POLY_ADDRESS' in headers
    assert 'POLY_TIMESTAMP' in headers
    assert headers['POLY_ADDRESS'].lower() == acct.address.lower()


def test_env_writer_replaces_existing_keys_idempotent(tmp_path):
    """`_append_or_replace_lines` should overwrite existing keys, append
    new ones, preserve comments + blank lines."""
    import poly_derive_api_creds as pdac
    p = tmp_path / 'test.env'
    p.write_text(
        '# comment\n'
        'EXISTING=old\n'
        '\n'
        'OTHER=keep\n',
        encoding='utf-8',
    )
    pdac._append_or_replace_lines(str(p), {
        'EXISTING': 'new',
        'BRAND_NEW': 'fresh',
    })
    text = p.read_text(encoding='utf-8')
    assert 'EXISTING=new' in text
    assert 'EXISTING=old' not in text
    assert 'BRAND_NEW=fresh' in text
    assert 'OTHER=keep' in text
    assert '# comment' in text


# ── fetch_polymarket_positions stub-mode ────────────────────────────
def test_fetch_polymarket_positions_no_creds_returns_empty():
    """Wallet without L2 creds → fetcher returns empty dict, doesn't raise."""
    from risk import reconcile

    class _W:
        eth_address = '0x' + 'b' * 40
        poly_api_key = None
        poly_secret = None
        poly_passphrase = None

    out = reconcile.fetch_polymarket_positions([_W()])
    assert out == {}


def test_fetch_polymarket_positions_with_mock_response():
    """Wallet with creds + mocked HTTP → positions parsed into expected key
    shape."""
    from risk import reconcile

    class _W:
        eth_address = '0x' + 'c' * 40
        poly_api_key = 'k'
        poly_secret = 'aGVsbG8='     # base64
        poly_passphrase = 'p'

    class _Resp:
        status_code = 200
        def json(self):
            return [
                {'conditionId': '0xCOND1', 'outcome': 'YES',
                 'shares': 100, 'avgPrice': 0.30},
                {'conditionId': '0xCOND2', 'outcome': 'NO',
                 'shares': 50, 'avgPrice': 0.60},
            ]

    def _fake_get(url, headers=None, timeout=None):
        return _Resp()

    out = reconcile.fetch_polymarket_positions([_W()], http_get=_fake_get)
    assert ('Polymarket', '0xCOND1', 'YES') in out
    assert out[('Polymarket', '0xCOND1', 'YES')] == pytest.approx(30.0)
    assert ('Polymarket', '0xCOND2', 'NO') in out
    assert out[('Polymarket', '0xCOND2', 'NO')] == pytest.approx(30.0)
