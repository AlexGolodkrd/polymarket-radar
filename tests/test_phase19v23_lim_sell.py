"""Phase 19v23 (05.05.2026) — Limitless SELL maker/taker semantics.

Final-audit agent caught the Limitless analog of the Phase 19v19
Polymarket SELL fix. Limitless CTF Exchange follows the same convention
as Polymarket V2:
  BUY  (side=0): maker gives USDC, takes CTF
  SELL (side=1): maker gives CTF,  takes USDC

Old `build_limitless_order` unconditionally built BUY-shape (`makerAmount=USDC,
takerAmount=CTF`) regardless of side. SELL FOK orders rejected by server
AND on-chain CTF Exchange (insufficient USDC delta to satisfy CTF
withdrawal). Triggered on `revert_filled_legs` for cross-platform arbs
with a filled Limitless leg → directional Limitless exposure left open
after revert "completes" with `sell_lim_HTTP_4xx`.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


def test_limitless_sell_maker_is_contracts():
    """Source-level: SELL branch sets maker=contracts (CTF), taker=USDC."""
    import inspect
    from executor import builders
    src = inspect.getsource(builders.build_limitless_order)
    # Branch on side must be present
    assert "if side == 'BUY':" in src
    # SELL branch swaps maker/taker
    assert 'maker_amount_wei = contracts_wei' in src
    assert 'taker_amount_wei = usdc_wei' in src


def test_limitless_buy_uses_usdc_maker():
    """BUY branch: maker=USDC (legacy semantics preserved)."""
    import inspect
    from executor import builders
    src = inspect.getsource(builders.build_limitless_order)
    # The BUY assignment must come AFTER `if side == 'BUY':`
    pre = src.split("if side == 'BUY':")[1].split('else:')[0]
    assert 'maker_amount_wei = usdc_wei' in pre
    assert 'taker_amount_wei = contracts_wei' in pre


def test_limitless_order_body_uses_correct_amounts():
    """End-to-end: build a BUY and a SELL order, verify amounts swap."""
    from executor import builders

    class _W:
        bot_id = 'bot1'
        eth_address = '0x' + '0' * 40
        private_key = None
        api_key = 'test'

        @property
        def can_sign(self):
            return False

    # BUY $50 @ 0.50 → 100 contracts
    buy = builders.build_limitless_order(
        slug='test', side='BUY', price=0.50, size_usdc=50.0,
        wallet=_W(), token_id='1', verifying_contract='0x' + '1' * 40,
    )
    buy_order = buy['body']['order']
    assert buy_order['side'] == '0'
    # BUY: maker=USDC=50e6, taker=contracts=100e6
    assert int(buy_order['makerAmount']) == 50_000_000
    assert int(buy_order['takerAmount']) == 100_000_000

    # SELL $50 @ 0.50 → 100 contracts
    sell = builders.build_limitless_order(
        slug='test', side='SELL', price=0.50, size_usdc=50.0,
        wallet=_W(), token_id='1', verifying_contract='0x' + '1' * 40,
    )
    sell_order = sell['body']['order']
    assert sell_order['side'] == '1'
    # SELL: maker=contracts=100e6, taker=USDC=50e6
    assert int(sell_order['makerAmount']) == 100_000_000
    assert int(sell_order['takerAmount']) == 50_000_000
