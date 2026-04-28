"""Unit tests for Limitless Exchange integration (Phase 9 add-on, 28.04.2026).

Covers:
- build_limitless_order body shape + correct EIP-712 wrapper
- Real EIP-712 signing path with eth-account when private_key supplied
- _fetch_limitless_orderbook synthesises NO-ask from YES bids correctly
- eval_limitless three structures (A/B/C + standalone binary)
- 10-day window filter on deadline
- LimitlessWS subscribe payload + orderbookUpdate handling
"""
import json
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'Scripts'))

import arb_server
from executor.builders import (
    build_limitless_order, WalletStub,
    LIMITLESS_DOMAIN_NAME, LIMITLESS_DOMAIN_VERSION, LIMITLESS_CHAIN_ID,
    LIMITLESS_DEFAULT_EXCHANGE, LIMITLESS_ORDER_TYPES,
)


def _wallet():
    return WalletStub(bot_id='bot1', eth_address='0x' + 'a' * 40)


# A throwaway test key (NEVER use on mainnet). Anvil/Foundry default key #0,
# universally known — safe for unit tests because it has no funds.
TEST_PRIVATE_KEY = (
    "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
)
TEST_PUBLIC_ADDR = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"


# ── Builder ──────────────────────────────────────────────────────────
class TestLimitlessBuilder(unittest.TestCase):
    def test_basic_buy_order_shape(self):
        o = build_limitless_order('test-slug', 'BUY', 0.45, 10.0, _wallet())
        self.assertEqual(o['platform'], 'limitless')
        self.assertEqual(o['expected_price'], 0.45)
        self.assertEqual(o['expected_size_usdc'], 10.0)
        # API-wrapper body: {order:{...,signature}, marketSlug, orderType}
        body = o['body']
        self.assertEqual(body['marketSlug'], 'test-slug')
        self.assertEqual(body['orderType'], 'GTC')
        order = body['order']
        self.assertEqual(order['side'], '0')               # BUY
        self.assertEqual(order['signatureType'], '0')
        # chainId is NOT in the order body — it lives in the EIP-712 domain
        self.assertNotIn('chainId', order)
        # makerAmount = 10 USDC × 1e6
        self.assertEqual(order['makerAmount'], '10000000')
        # takerAmount = (10 / 0.45) × 1e6 ≈ 22222222
        self.assertEqual(order['takerAmount'], '22222222')
        # EIP-712 domain surfaced for atomic.py / paper-trade audit
        self.assertEqual(o['eip712']['domain']['name'],
                         LIMITLESS_DOMAIN_NAME)
        self.assertEqual(o['eip712']['domain']['chainId'], LIMITLESS_CHAIN_ID)

    def test_rejects_invalid_price(self):
        with self.assertRaises(AssertionError):
            build_limitless_order('s', 'BUY', 0, 10.0, _wallet())
        with self.assertRaises(AssertionError):
            build_limitless_order('s', 'BUY', 1.0, 10.0, _wallet())

    def test_rejects_below_min_size(self):
        with self.assertRaises(AssertionError):
            build_limitless_order('s', 'BUY', 0.5, 0.5, _wallet())

    def test_sell_side_flag(self):
        o = build_limitless_order('s', 'SELL', 0.5, 5.0, _wallet())
        self.assertEqual(o['body']['order']['side'], '1')

    def test_unsigned_when_no_private_key(self):
        """Dry-run path: no private_key on wallet → no signature, signed=False."""
        o = build_limitless_order('s', 'BUY', 0.5, 5.0, _wallet(),
                                   token_id='1234', verifying_contract='0x' + 'b' * 40)
        self.assertFalse(o['signed'])
        self.assertEqual(o['body']['order']['signature'], '')

    def test_real_eip712_signature_when_keys_present(self):
        """When wallet has private_key + token_id + verifying_contract, the
        builder produces a 65-byte hex signature recoverable to the wallet's
        own address — proving the EIP-712 typed data was hashed correctly."""
        try:
            from eth_account import Account
            from eth_account.messages import encode_typed_data
        except ImportError:
            self.skipTest("eth-account not installed")
        wallet = WalletStub(bot_id='bot1', eth_address=TEST_PUBLIC_ADDR,
                            private_key=TEST_PRIVATE_KEY)
        verifying_contract = '0x' + 'c' * 40
        o = build_limitless_order('test-slug', 'BUY', 0.5, 5.0, wallet,
                                   token_id='9999',
                                   verifying_contract=verifying_contract)
        self.assertTrue(o['signed'])
        sig = o['body']['order']['signature']
        self.assertTrue(sig.startswith('0x'))
        self.assertEqual(len(sig), 2 + 130)   # 65-byte sig as hex

        # Recover the signer to prove the typed-data was hashed correctly.
        order_msg = {k: (int(v) if k in (
            'salt', 'tokenId', 'makerAmount', 'takerAmount',
            'expiration', 'nonce', 'feeRateBps', 'side', 'signatureType',
        ) else v) for k, v in o['order'].items()}
        full_message = {
            'types': LIMITLESS_ORDER_TYPES,
            'primaryType': 'Order',
            'domain': {
                'name': LIMITLESS_DOMAIN_NAME,
                'version': LIMITLESS_DOMAIN_VERSION,
                'chainId': LIMITLESS_CHAIN_ID,
                'verifyingContract': verifying_contract,
            },
            'message': order_msg,
        }
        encoded = encode_typed_data(full_message=full_message)
        recovered = Account.recover_message(encoded, signature=sig)
        self.assertEqual(recovered.lower(), TEST_PUBLIC_ADDR.lower())

    def test_token_id_lands_in_order_body(self):
        o = build_limitless_order('s', 'BUY', 0.5, 5.0, _wallet(),
                                   token_id='1234567890')
        self.assertEqual(o['order']['tokenId'], '1234567890')


# ── Orderbook fetcher ────────────────────────────────────────────────
class TestLimitlessOrderbookFetch(unittest.TestCase):
    def test_synthesises_no_ask_from_best_yes_bid(self):
        """Limitless returns single-side orderbook per slug. We compute
        the NO-side ask as 1 - best YES bid (no-arbitrage condition)."""
        fake_response = mock.Mock()
        fake_response.status_code = 200
        fake_response.json.return_value = {
            'asks': [
                {'price': '0.45', 'size': '100'},
                {'price': '0.46', 'size': '50'},
            ],
            'bids': [
                {'price': '0.42', 'size': '80'},
                {'price': '0.41', 'size': '60'},
            ],
            'tokenId': '0xabc',
        }
        with mock.patch('arb_server.requests.get', return_value=fake_response):
            slug, yes_ask, depth_yes, no_ask, depth_no = \
                arb_server._fetch_limitless_orderbook('test')
        self.assertEqual(slug, 'test')
        self.assertAlmostEqual(yes_ask, 0.45)
        # NO-ask = 1 - best YES bid (0.42) = 0.58
        self.assertAlmostEqual(no_ask, 0.58)
        # Depth = price * size summed across asks/bids
        self.assertGreater(depth_yes, 0)
        self.assertGreater(depth_no, 0)

    def test_handles_404_gracefully(self):
        fake = mock.Mock(); fake.status_code = 404
        with mock.patch('arb_server.requests.get', return_value=fake):
            slug, ya, dy, na, dn = arb_server._fetch_limitless_orderbook('x')
        self.assertIsNone(ya)
        self.assertIsNone(na)

    def test_handles_empty_book(self):
        fake = mock.Mock(); fake.status_code = 200
        fake.json.return_value = {'asks': [], 'bids': []}
        with mock.patch('arb_server.requests.get', return_value=fake):
            slug, ya, dy, na, dn = arb_server._fetch_limitless_orderbook('x')
        self.assertIsNone(ya)
        self.assertIsNone(na)

    def test_handles_request_exception(self):
        with mock.patch('arb_server.requests.get', side_effect=Exception('net down')):
            slug, ya, dy, na, dn = arb_server._fetch_limitless_orderbook('x')
        self.assertIsNone(ya)
        self.assertIsNone(na)


# ── eval_limitless ───────────────────────────────────────────────────
import time, datetime as _dt

def _future_ts(days=2):
    """Unix-ms timestamp `days` ahead — within 10-day window."""
    return int((time.time() + days * 86400) * 1000)


class TestEvalLimitless(unittest.TestCase):
    def test_negrisk_group_all_yes_arb(self):
        """Multi-outcome event whose Σ YES asks < 0.99 → ALL_YES deal."""
        events = [{
            'title': 'Test event',
            'slug': 'test',
            'deadline': _future_ts(2),
            'markets': [
                {'slug': 'a', 'title': 'Outcome A'},
                {'slug': 'b', 'title': 'Outcome B'},
                {'slug': 'c', 'title': 'Outcome C'},
            ],
        }]
        # Σ asks = 0.30 + 0.30 + 0.30 = 0.90 < 0.99 → arb (~10c profit per $1)
        lim_res = {
            'a': (0.30, 100, 0.65, 80),  # yes_ask, depth_yes, no_ask, depth_no
            'b': (0.30, 100, 0.65, 80),
            'c': (0.30, 100, 0.65, 80),
        }
        deals = arb_server.eval_limitless(events, lim_res)
        all_yes = [d for d in deals if d.get('arb_structure') == 'all_yes']
        self.assertEqual(len(all_yes), 1)
        self.assertEqual(all_yes[0]['platform'], 'Limitless')
        # Each entry should carry slug + side for builder dispatch
        for e in all_yes[0]['entries']:
            self.assertIn(e['slug'], {'a', 'b', 'c'})
            self.assertEqual(e['side'], 'YES')

    def test_yes_no_pair_per_market_arb(self):
        """Per-market YES + NO < 0.99 → YES_NO_PAIR deal per outcome."""
        events = [{
            'title': 'Test', 'slug': 't', 'deadline': _future_ts(3),
            'markets': [{'slug': 'a', 'title': 'A'}, {'slug': 'b', 'title': 'B'}],
        }]
        # Per-market: yes 0.40 + no 0.55 = 0.95 < 0.99 → arb on each
        lim_res = {
            'a': (0.40, 100, 0.55, 80),
            'b': (0.40, 100, 0.55, 80),
        }
        deals = arb_server.eval_limitless(events, lim_res)
        pair_deals = [d for d in deals if d.get('arb_structure') == 'yes_no_pair']
        # Two markets × 1 pair each = 2
        self.assertGreaterEqual(len(pair_deals), 2)

    def test_window_filter_drops_far_future_events(self):
        """Events outside 10-day window are dropped at filter level."""
        events = [{
            'title': 'Far event', 'slug': 'far',
            'deadline': _future_ts(60),   # 60 days ahead
            'markets': [{'slug': 'far', 'title': 'F'}],
        }]
        lim_res = {'far': (0.10, 100, 0.10, 100)}  # would be a tasty arb
        deals = arb_server.eval_limitless(events, lim_res)
        self.assertEqual(deals, [])

    def test_standalone_binary_market(self):
        """Event without `markets[]` list = standalone binary; only C structure."""
        events = [{
            'title': 'Will BTC > 100k',
            'slug': 'btc-100k',
            'deadline': _future_ts(2),
        }]
        lim_res = {'btc-100k': (0.45, 100, 0.50, 80)}  # 0.95 total → arb
        deals = arb_server.eval_limitless(events, lim_res)
        self.assertEqual(len(deals), 1)
        self.assertEqual(deals[0]['arb_structure'], 'binary')
        for e in deals[0]['entries']:
            self.assertEqual(e['slug'], 'btc-100k')

    def test_no_arb_when_total_above_threshold(self):
        events = [{
            'title': 'T', 'slug': 't', 'deadline': _future_ts(2),
            'markets': [{'slug': 'a', 'title': 'A'}, {'slug': 'b', 'title': 'B'}],
        }]
        # Σ asks = 0.50 + 0.50 = 1.00 → above 0.99 threshold → no arb
        lim_res = {
            'a': (0.50, 100, 0.55, 80),
            'b': (0.50, 100, 0.55, 80),
        }
        all_yes = [d for d in arb_server.eval_limitless(events, lim_res)
                   if d.get('arb_structure') == 'all_yes']
        self.assertEqual(all_yes, [])


# ── filter_limitless parity ──────────────────────────────────────────
# These check that Limitless events pass through the SAME pre-eval gates
# we apply to Polymarket: blacklist, is_deadline text reject, and the
# "Other" outcome quarantine. Phase 9b — added 28.04.2026 to close the
# parity gap with filter_poly.
class TestFilterLimitlessParity(unittest.TestCase):
    def setUp(self):
        # Snapshot blacklist; restore at tearDown so tests don't bleed.
        self._blacklist_backup = set(arb_server.blacklist)

    def tearDown(self):
        arb_server.blacklist.clear()
        arb_server.blacklist.update(self._blacklist_backup)

    def test_blacklist_filters_event(self):
        arb_server.blacklist.add('Banned event')
        events = [
            {'title': 'Banned event', 'slug': 'b', 'deadline': _future_ts(2),
             'markets': [{'slug': 'a', 'title': 'A'}, {'slug': 'b', 'title': 'B'}]},
            {'title': 'Allowed event', 'slug': 'al', 'deadline': _future_ts(2),
             'markets': [{'slug': 'a', 'title': 'A'}, {'slug': 'b', 'title': 'B'}]},
        ]
        result = arb_server.filter_limitless(events)
        titles = [ev.get('title') for ev, _q in result]
        self.assertNotIn('Banned event', titles)
        self.assertIn('Allowed event', titles)

    def test_deadline_text_pattern_filters_event(self):
        """Title like 'By March 31' / 'Before Q4 2026' → dropped (resolves
        ambiguously). Same logic as filter_poly."""
        events = [
            {'title': 'Will rates fall', 'slug': 'rate',
             'deadline': _future_ts(2),
             'markets': [
                 {'slug': 'a', 'title': 'By March 31'},
                 {'slug': 'b', 'title': 'By June 30'},
             ]},
        ]
        result = arb_server.filter_limitless(events)
        self.assertEqual(result, [])

    def test_other_outcome_marks_quarantine(self):
        """Multi-outcome event with hidden 'Other' option → is_quarantine=True,
        but the event is still passed through (quarantine ≠ skip; UI shows
        it for analysis, executor refuses to fire)."""
        events = [
            {'title': 'Election winner', 'slug': 'elec',
             'deadline': _future_ts(2),
             'markets': [
                 {'slug': 'a', 'title': 'Candidate A'},
                 {'slug': 'b', 'title': 'Candidate B'},
                 {'slug': 'c', 'title': 'Other'},
             ]},
        ]
        result = arb_server.filter_limitless(events)
        self.assertEqual(len(result), 1)
        ev, is_q = result[0]
        self.assertTrue(is_q, "expected 'Other' outcome to set quarantine")

    def test_clean_event_not_quarantined(self):
        events = [
            {'title': 'Clean', 'slug': 'c', 'deadline': _future_ts(2),
             'markets': [
                 {'slug': 'a', 'title': 'Team A'},
                 {'slug': 'b', 'title': 'Team B'},
             ]},
        ]
        result = arb_server.filter_limitless(events)
        self.assertEqual(len(result), 1)
        _ev, is_q = result[0]
        self.assertFalse(is_q)

    def test_eval_limitless_propagates_quarantine_to_deal(self):
        """When filter_limitless flags Other, the resulting deal carries
        is_quarantine=True so atomic._build_leg refuses to fire it."""
        events = [{
            'title': 'Election', 'slug': 'el', 'deadline': _future_ts(2),
            'markets': [
                {'slug': 'a', 'title': 'Trump'},
                {'slug': 'b', 'title': 'Biden'},
                {'slug': 'c', 'title': 'Other candidate'},
            ],
        }]
        lim_res = {
            'a': (0.30, 100, 0.65, 80),
            'b': (0.30, 100, 0.65, 80),
            'c': (0.30, 100, 0.65, 80),
        }
        deals = arb_server.eval_limitless(events, lim_res)
        self.assertGreater(len(deals), 0)
        for d in deals:
            self.assertTrue(d.get('is_quarantine'),
                            f"expected quarantine on {d.get('arb_structure')} deal")

    def test_has_other_outcome_recognises_ru_and_en(self):
        from arb_server import has_other_outcome
        self.assertTrue(has_other_outcome(['Team A', 'Team B', 'Other']))
        self.assertTrue(has_other_outcome(['A', 'B', 'None of the above']))
        self.assertTrue(has_other_outcome(['A', 'Прочее']))
        self.assertTrue(has_other_outcome(['A', 'Любой другой']))
        self.assertFalse(has_other_outcome(['Team A', 'Team B']))


# ── LimitlessWS ──────────────────────────────────────────────────────
class TestLimitlessWS(unittest.TestCase):
    """Verify the Socket.IO-based WS wrapper:
       - desired-set tracking + cap
       - emit `subscribe_market_prices` on /markets namespace with marketSlugs
       - parse orderbookUpdate into best_yes_ask / best_yes_bid
       - graceful handling of unknown events
       - degrades to no-op when python-socketio is unavailable."""

    def test_emits_subscribe_to_markets_namespace(self):
        """`_sync_subscriptions` should emit 'subscribe_market_prices' on
        the /markets namespace with the current desired slug set."""
        from limitless_ws import LimitlessWS, WS_NAMESPACE
        ws = LimitlessWS(verbose=False)
        ws.update_subscriptions(['a', 'b', 'c'])
        self.assertEqual(ws._desired, {'a', 'b', 'c'})
        # Inject a fake sio + connected state, then sync
        fake_sio = mock.Mock()
        ws._sio = fake_sio
        ws._connected = True
        ws._sync_subscriptions()
        fake_sio.emit.assert_called_once()
        args, kwargs = fake_sio.emit.call_args
        self.assertEqual(args[0], 'subscribe_market_prices')
        self.assertSetEqual(set(args[1]['marketSlugs']), {'a', 'b', 'c'})
        self.assertEqual(kwargs.get('namespace'), WS_NAMESPACE)

    def test_no_emit_when_disconnected(self):
        """If the socket is not yet connected, _sync_subscriptions must NOT
        crash — the on-connect handler will resync."""
        from limitless_ws import LimitlessWS
        ws = LimitlessWS(verbose=False)
        ws.update_subscriptions(['a'])
        # _sio still None → no exception, no emit
        ws._sync_subscriptions()   # must not raise

    def test_orderbook_update_parses_yes_ask_and_synth_no_ask(self):
        """Server pushes orderbookUpdate; we store best_yes_ask and
        best_yes_bid so consumers can synthesise NO_ask = 1 - best_yes_bid."""
        from limitless_ws import LimitlessWS
        ws = LimitlessWS(verbose=False)
        ev = {
            'event': 'orderbookUpdate',
            'data': {
                'marketSlug': 'btc-100k',
                'asks': [{'price': '0.45', 'size': '100'},
                         {'price': '0.46', 'size': '50'}],
                'bids': [{'price': '0.42', 'size': '80'},
                         {'price': '0.41', 'size': '60'}],
            },
        }
        ws._handle_event(ev)
        book = ws.get_book('btc-100k')
        self.assertIsNotNone(book)
        self.assertAlmostEqual(book['best_yes_ask'], 0.45)
        self.assertAlmostEqual(book['best_yes_bid'], 0.42)
        self.assertGreater(book['depth_yes'], 0)
        self.assertGreater(book['depth_no'], 0)

    def test_unknown_events_ignored(self):
        from limitless_ws import LimitlessWS
        ws = LimitlessWS(verbose=False)
        # No exception, no books recorded
        ws._handle_event({'event': 'something_new', 'data': {'x': 1}})
        self.assertEqual(ws.books, {})

    def test_metrics_reflect_subscription_state(self):
        from limitless_ws import LimitlessWS
        ws = LimitlessWS(verbose=False, max_subs=100)
        ws.update_subscriptions(['a', 'b', 'c', 'd'])
        m = ws.get_metrics()
        self.assertEqual(m['subs_desired'], 4)
        self.assertEqual(m['subs_max'], 100)
        self.assertFalse(m['connected'])

    def test_subs_capped_at_max(self):
        from limitless_ws import LimitlessWS
        ws = LimitlessWS(verbose=False, max_subs=3)
        ws.update_subscriptions(['a', 'b', 'c', 'd', 'e'])
        # Cap applied at update_subscriptions time (not just at emit time)
        self.assertLessEqual(len(ws._desired), 3)


if __name__ == '__main__':
    unittest.main(verbosity=2)
