"""Phase 9f tests — Polymarket parity.

Covers:
- build_poly_order V2 shape + real EIP-712 signature recovery
- negRisk vs standard domain switching
- HMAC L2 auth headers (POLY_ADDRESS / POLY_TIMESTAMP / POLY_SIGNATURE / etc.)
- build_poly_cancel + build_poly_cancel_all
- PolyUserWS subscribe payload + trade event handling
- on_poly_fill bridge → fills.registry.consume
"""
import os
import sys
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'Scripts'))

from executor.builders import (
    build_poly_order, build_poly_cancel, build_poly_cancel_all,
    build_poly_hmac_headers, WalletStub,
    POLY_DOMAIN_STANDARD, POLY_DOMAIN_NEGRISK, POLY_ORDER_TYPES_V2,
)


# Anvil default test key — universally known, no funds, safe for unit tests.
TEST_PRIVATE_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
TEST_PUBLIC_ADDR = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"


def _wallet(private_key=None, **creds):
    return WalletStub(
        bot_id='bot1',
        eth_address='0x' + 'a' * 40,
        private_key=private_key,
        **creds,
    )


# ── Builder shape ────────────────────────────────────────────────────
class TestPolyOrderV2Shape(unittest.TestCase):
    def test_v2_fields_present_no_legacy(self):
        o = build_poly_order('123', 'BUY', 0.5, 10.0, _wallet())
        order = o['order']
        # V2 must have these
        for f in ('salt', 'maker', 'signer', 'tokenId',
                   'makerAmount', 'takerAmount', 'side', 'signatureType',
                   'timestamp', 'metadata', 'builder'):
            self.assertIn(f, order, f"missing V2 field {f}")
        # V1 dropped fields must NOT be there (cheatsheet rule)
        for f in ('expiration', 'nonce', 'feeRateBps', 'taker'):
            self.assertNotIn(f, order, f"legacy field {f} should be dropped")

    def test_api_wrapper_shape(self):
        o = build_poly_order('123', 'BUY', 0.5, 10.0, _wallet())
        body = o['body']
        self.assertIn('order', body)
        self.assertEqual(body['orderType'], 'GTC')
        self.assertEqual(body['owner'], '0x' + 'a' * 40)

    def test_negrisk_domain_switching(self):
        std = build_poly_order('1', 'BUY', 0.5, 10.0, _wallet())
        neg = build_poly_order('1', 'BUY', 0.5, 10.0, _wallet(), neg_risk=True)
        self.assertEqual(std['eip712']['domain']['name'],
                          'Polymarket CTF Exchange')
        self.assertEqual(neg['eip712']['domain']['name'],
                          'Polymarket Neg Risk CTF Exchange')

    def test_unsigned_when_no_private_key(self):
        o = build_poly_order('1', 'BUY', 0.5, 10.0, _wallet())
        self.assertFalse(o['signed'])
        self.assertEqual(o['body']['order']['signature'], '')

    def test_real_eip712_recover(self):
        """Sign with anvil-key, recover signer, verify == TEST_PUBLIC_ADDR."""
        try:
            from eth_account import Account
            from eth_account.messages import encode_typed_data
        except ImportError:
            self.skipTest("eth-account not installed")
        wallet = WalletStub(bot_id='bot1', eth_address=TEST_PUBLIC_ADDR,
                             private_key=TEST_PRIVATE_KEY)
        o = build_poly_order('999', 'BUY', 0.5, 5.0, wallet)
        self.assertTrue(o['signed'])
        sig = o['body']['order']['signature']
        self.assertTrue(sig.startswith('0x'))
        self.assertEqual(len(sig), 2 + 130)

        # Recover via the same typed-data shape we used to sign
        order_msg = {}
        for k, v in o['order'].items():
            if k in ('metadata', 'builder'):
                s = v[2:] if v.startswith('0x') else v
                order_msg[k] = bytes.fromhex(s.rjust(64, '0'))
            elif k in ('salt', 'tokenId', 'makerAmount', 'takerAmount',
                        'side', 'signatureType', 'timestamp'):
                order_msg[k] = int(v)
            else:
                order_msg[k] = v
        full_message = {
            'types': POLY_ORDER_TYPES_V2,
            'primaryType': 'Order',
            'domain': POLY_DOMAIN_STANDARD,
            'message': order_msg,
        }
        encoded = encode_typed_data(full_message=full_message)
        recovered = Account.recover_message(encoded, signature=sig)
        self.assertEqual(recovered.lower(), TEST_PUBLIC_ADDR.lower())


# ── HMAC auth headers ────────────────────────────────────────────────
class TestPolyHmacHeaders(unittest.TestCase):
    def test_required_headers_present(self):
        h = build_poly_hmac_headers(
            method='POST', path='/order', body='{}',
            api_key='k', api_secret='c2VjcmV0',  # base64('secret')
            passphrase='p', eth_address='0x' + 'b' * 40,
            ts=1700000000,
        )
        for f in ('POLY_ADDRESS', 'POLY_TIMESTAMP', 'POLY_API_KEY',
                  'POLY_PASSPHRASE', 'POLY_SIGNATURE', 'Content-Type'):
            self.assertIn(f, h)
        self.assertEqual(h['POLY_TIMESTAMP'], '1700000000')
        self.assertEqual(h['POLY_API_KEY'], 'k')

    def test_signature_is_deterministic(self):
        kwargs = dict(method='GET', path='/data/positions', body='',
                      api_key='k', api_secret='c2VjcmV0',
                      passphrase='p', eth_address='0xabc', ts=1700000000)
        a = build_poly_hmac_headers(**kwargs)
        b = build_poly_hmac_headers(**kwargs)
        self.assertEqual(a['POLY_SIGNATURE'], b['POLY_SIGNATURE'])

    def test_signature_changes_with_body(self):
        a = build_poly_hmac_headers(
            method='POST', path='/order', body='{"a":1}',
            api_key='k', api_secret='c2VjcmV0', passphrase='p',
            eth_address='0xabc', ts=1700000000,
        )
        b = build_poly_hmac_headers(
            method='POST', path='/order', body='{"a":2}',
            api_key='k', api_secret='c2VjcmV0', passphrase='p',
            eth_address='0xabc', ts=1700000000,
        )
        self.assertNotEqual(a['POLY_SIGNATURE'], b['POLY_SIGNATURE'])


# ── Cancel builders ──────────────────────────────────────────────────
class TestPolyCancelBuilders(unittest.TestCase):
    def test_cancel_single_with_creds(self):
        # Phase audit-28b cont 2 (27.05.2026) — PR #246 synced Python
        # build_poly_cancel with TS executor: DELETE /order with body
        # {orderID: id} per V2 spec. Old path-style /order/{id} dropped.
        w = _wallet(poly_api_key='k', poly_secret='c2VjcmV0', poly_passphrase='p')
        b = build_poly_cancel('order-123', w)
        self.assertEqual(b['op'], 'cancel')
        self.assertEqual(b['method'], 'DELETE')
        # URL is now '/order' (no id in path) — id lives in body
        self.assertTrue(b['would_post_url'].endswith('/order'))
        self.assertEqual(b['body'], {'orderID': 'order-123'})
        self.assertIn('POLY_SIGNATURE', b['headers'])
        self.assertEqual(b['headers'].get('Content-Type'), 'application/json')

    def test_cancel_single_without_creds_returns_empty_headers(self):
        b = build_poly_cancel('o', _wallet())
        self.assertEqual(b['headers'], {})

    def test_cancel_all_with_creds(self):
        w = _wallet(poly_api_key='k', poly_secret='c2VjcmV0', poly_passphrase='p')
        b = build_poly_cancel_all(w)
        self.assertEqual(b['op'], 'cancel_all')
        self.assertTrue(b['would_post_url'].endswith('/orders'))
        self.assertIn('POLY_SIGNATURE', b['headers'])


# ── PolyUserWS ───────────────────────────────────────────────────────
class TestPolyUserWS(unittest.TestCase):
    def _wallet_full(self):
        return WalletStub(
            bot_id='bot1', eth_address='0x' + 'a' * 40,
            poly_api_key='K1', poly_secret='S1', poly_passphrase='P1',
        )

    def test_no_op_without_poly_creds(self):
        from poly_user_ws import PolyUserWS
        ws = PolyUserWS(wallet=_wallet())   # no creds
        ws.start()   # must NOT raise, must not spawn thread
        self.assertIsNone(ws._ws_thread)

    def test_subscribe_payload_carries_auth_and_markets(self):
        from poly_user_ws import PolyUserWS
        ws = PolyUserWS(wallet=self._wallet_full())
        ws.update_markets(['cond-1', 'cond-2'])
        # Simulate _on_open: capture sent message
        sent = []
        fake_ws = mock.Mock()
        fake_ws.send = lambda msg: sent.append(msg)
        ws._on_open(fake_ws)
        import json
        payload = json.loads(sent[0])
        self.assertEqual(payload['type'], 'user')
        self.assertSetEqual(set(payload['markets']), {'cond-1', 'cond-2'})
        self.assertEqual(payload['auth']['apiKey'], 'K1')
        self.assertEqual(payload['auth']['secret'], 'S1')
        self.assertEqual(payload['auth']['passphrase'], 'P1')

    def test_trade_event_invokes_on_fill_and_buffers(self):
        from poly_user_ws import PolyUserWS
        seen = []
        ws = PolyUserWS(wallet=self._wallet_full(),
                        on_fill=lambda ev: seen.append(ev))
        ws._handle_event({
            'event_type': 'trade',
            'taker_order_id': 'O1',
            'price': '0.45',
            'size': '10',
            'status': 'MATCHED',
        })
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]['taker_order_id'], 'O1')
        self.assertEqual(len(ws.get_recent_fills()), 1)

    def test_non_trade_events_dont_invoke_on_fill(self):
        from poly_user_ws import PolyUserWS
        seen = []
        ws = PolyUserWS(wallet=self._wallet_full(),
                        on_fill=lambda ev: seen.append(ev))
        ws._handle_event({'event_type': 'order', 'status': 'PLACED'})
        self.assertEqual(seen, [])

    def test_set_change_triggers_close_for_resubscribe(self):
        from poly_user_ws import PolyUserWS
        ws = PolyUserWS(wallet=self._wallet_full())
        ws._ws = mock.Mock()
        ws.update_markets(['c1'])
        ws._ws.close.assert_called()


# ── on_poly_fill bridge ──────────────────────────────────────────────
class TestOnPolyFillBridge(unittest.TestCase):
    def setUp(self):
        from executor import fills
        self._fills = fills
        self._fills.registry = fills.FillRegistry()

    def test_match_event_consumes_by_order_id(self):
        import arb_server
        reg = self._fills.registry.register(
            arb_id='a', leg_idx=0, platform='polymarket',
            slug='cond-X', order_id='order-A')
        with mock.patch('executor.fills.registry', self._fills.registry):
            arb_server.on_poly_fill({
                'event_type': 'trade', 'status': 'MATCHED',
                'taker_order_id': 'order-A',
                'price': '0.42', 'size': '10',
                'market': 'cond-X',
            })
        self.assertTrue(reg.event.is_set())
        self.assertAlmostEqual(reg.result['fill_price'], 0.42)

    def test_non_match_events_ignored(self):
        import arb_server
        reg = self._fills.registry.register(
            arb_id='a', leg_idx=0, platform='polymarket',
            slug='c', order_id='O')
        with mock.patch('executor.fills.registry', self._fills.registry):
            arb_server.on_poly_fill({
                'event_type': 'order', 'status': 'PLACED',
                'taker_order_id': 'O',
            })
        self.assertFalse(reg.event.is_set())

    def test_fallback_to_slug_when_no_order_id(self):
        import arb_server
        reg = self._fills.registry.register(
            arb_id='a', leg_idx=0, platform='polymarket',
            slug='cond-Y', order_id=None)
        with mock.patch('executor.fills.registry', self._fills.registry):
            arb_server.on_poly_fill({
                'event_type': 'trade', 'status': 'MATCHED',
                'price': '0.5', 'size': '5',
                'market': 'cond-Y',
            })
        self.assertTrue(reg.event.is_set())


if __name__ == '__main__':
    unittest.main(verbosity=2)
