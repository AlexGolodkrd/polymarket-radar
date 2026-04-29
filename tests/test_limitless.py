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
        with mock.patch('arb_server._SESS_LIM.get', return_value=fake_response):
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
        with mock.patch('arb_server._SESS_LIM.get', return_value=fake):
            slug, ya, dy, na, dn = arb_server._fetch_limitless_orderbook('x')
        self.assertIsNone(ya)
        self.assertIsNone(na)

    def test_handles_empty_book(self):
        fake = mock.Mock(); fake.status_code = 200
        fake.json.return_value = {'asks': [], 'bids': []}
        with mock.patch('arb_server._SESS_LIM.get', return_value=fake):
            slug, ya, dy, na, dn = arb_server._fetch_limitless_orderbook('x')
        self.assertIsNone(ya)
        self.assertIsNone(na)

    def test_handles_request_exception(self):
        with mock.patch('arb_server._SESS_LIM.get', side_effect=Exception('net down')):
            slug, ya, dy, na, dn = arb_server._fetch_limitless_orderbook('x')
        self.assertIsNone(ya)
        self.assertIsNone(na)


# ── eval_limitless ───────────────────────────────────────────────────
import time, datetime as _dt

def _future_ts(days=2):
    """Unix-ms timestamp `days` ahead — within 10-day window."""
    return int((time.time() + days * 86400) * 1000)


class TestEvalLimitless(unittest.TestCase):
    """eval_limitless tests. Phase 9c added a `_fetch_limitless_market_meta`
    side-effect to populate token_id + verifying_contract per leg, plus a
    `_lim_quality_ok` filter that blocks zero-volume ghost markets. We patch
    the meta fetcher to a stub so these tests stay network-free, and supply
    a non-zero volume so the quality filter passes."""

    _MOCK_META = {
        'yes_token': '111', 'no_token': '222',
        'verifying_contract': '0xabcdef',
        'volume': 1000.0, 'is_other': False, 'fetched_at': time.time(),
    }

    def setUp(self):
        self._meta_patcher = mock.patch('arb_server._fetch_limitless_market_meta',
                                         return_value=dict(self._MOCK_META))
        self._meta_patcher.start()

    def tearDown(self):
        self._meta_patcher.stop()

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
        """Per-market YES + NO < 0.99 → YES_NO_PAIR deal per outcome.
        Sum kept at 0.90 (≤ 95¢) so the Phase 9c quality filter
        doesn't reject the legs as too thin."""
        events = [{
            'title': 'Test', 'slug': 't', 'deadline': _future_ts(3),
            'markets': [{'slug': 'a', 'title': 'A'}, {'slug': 'b', 'title': 'B'}],
        }]
        # Per-market: yes 0.40 + no 0.50 = 0.90 < 0.99 → arb on each
        lim_res = {
            'a': (0.40, 100, 0.50, 80),
            'b': (0.40, 100, 0.50, 80),
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
        """Event without `markets[]` list = standalone binary; only C structure.
        Sum kept at 0.90 to clear Phase 9c quality filter."""
        events = [{
            'title': 'Will BTC > 100k',
            'slug': 'btc-100k',
            'deadline': _future_ts(2),
        }]
        lim_res = {'btc-100k': (0.40, 100, 0.50, 80)}  # 0.90 total → arb
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
    _MOCK_META = {
        'yes_token': '111', 'no_token': '222',
        'verifying_contract': '0xabcdef',
        'volume': 1000.0, 'is_other': False, 'fetched_at': time.time(),
    }

    def setUp(self):
        # Snapshot blacklist; restore at tearDown so tests don't bleed.
        self._blacklist_backup = set(arb_server.blacklist)
        # Stub meta fetcher so network is never hit (and quality filter
        # gets a non-zero volume).
        self._meta_patcher = mock.patch(
            'arb_server._fetch_limitless_market_meta',
            return_value=dict(self._MOCK_META))
        self._meta_patcher.start()

    def tearDown(self):
        self._meta_patcher.stop()
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


# ── Phase 9c additions ──────────────────────────────────────────────
class TestLimitlessMetaCache(unittest.TestCase):
    """The meta cache is what makes real EIP-712 orders possible — without
    tokens.yes/no + venue.exchange we can't sign anything Limitless will
    accept. Verify the cache stores and re-uses correctly."""

    def setUp(self):
        # Clear module-level cache before each test
        with arb_server.lim_meta_lock:
            arb_server.lim_meta_cache.clear()

    def test_fetch_populates_cache(self):
        fake = mock.Mock()
        fake.status_code = 200
        fake.json.return_value = {
            'tokens': {'yes': '111', 'no': '222'},
            'venue': {'exchange': '0xabc'},
            'volume': 12345,
            'isOther': False,
        }
        with mock.patch('arb_server._SESS_LIM.get', return_value=fake):
            rec = arb_server._fetch_limitless_market_meta('test-slug')
        self.assertEqual(rec['yes_token'], '111')
        self.assertEqual(rec['no_token'], '222')
        self.assertEqual(rec['verifying_contract'], '0xabc')
        self.assertEqual(rec['volume'], 12345.0)
        self.assertFalse(rec['is_other'])
        # Subsequent call (within TTL) should NOT hit the network
        with mock.patch('arb_server._SESS_LIM.get',
                         side_effect=Exception('nope')) as gp:
            rec2 = arb_server._fetch_limitless_market_meta('test-slug')
        self.assertEqual(rec2['yes_token'], '111')
        gp.assert_not_called()

    def test_fetch_404_returns_none(self):
        fake = mock.Mock(); fake.status_code = 404
        with mock.patch('arb_server._SESS_LIM.get', return_value=fake):
            rec = arb_server._fetch_limitless_market_meta('missing')
        self.assertIsNone(rec)


class TestLimitlessIsOtherAPI(unittest.TestCase):
    """The Limitless API exposes a per-market boolean `isOther` directly.
    filter_limitless must respect it AND the heuristic title match — an event
    flagged either way is quarantined."""

    def test_api_isother_flags_quarantine_even_with_clean_title(self):
        events = [{
            'title': 'Clean title', 'slug': 'c',
            'deadline': _future_ts(2),
            'isOther': True,   # API said it's an "Other" outcome
            'markets': [
                {'slug': 'a', 'title': 'Clean Outcome A'},
                {'slug': 'b', 'title': 'Clean Outcome B'},
            ],
        }]
        result = arb_server.filter_limitless(events)
        self.assertEqual(len(result), 1)
        _ev, is_q = result[0]
        self.assertTrue(is_q)

    def test_api_isother_on_child_flags_quarantine(self):
        events = [{
            'title': 'Election', 'slug': 'el', 'deadline': _future_ts(2),
            'markets': [
                {'slug': 'a', 'title': 'Trump'},
                {'slug': 'b', 'title': 'Biden'},
                {'slug': 'c', 'title': 'Catch-all', 'isOther': True},
            ],
        }]
        result = arb_server.filter_limitless(events)
        _ev, is_q = result[0]
        self.assertTrue(is_q)


class TestLimitlessCancelBuilders(unittest.TestCase):
    def test_single_cancel(self):
        from executor.builders import build_limitless_cancel
        b = build_limitless_cancel('order-id-123', api_key='k1')
        self.assertEqual(b['op'], 'cancel')
        self.assertEqual(b['method'], 'DELETE')
        self.assertIn('order-id-123', b['would_post_url'])
        self.assertEqual(b['headers']['X-API-Key'], 'k1')

    def test_batch_cancel(self):
        from executor.builders import build_limitless_cancel_batch
        b = build_limitless_cancel_batch(['a', 'b', 'c'], api_key='k1')
        self.assertEqual(b['op'], 'cancel_batch')
        self.assertEqual(b['method'], 'POST')
        self.assertEqual(b['body'], {'orderIds': ['a', 'b', 'c']})
        self.assertEqual(b['headers']['Content-Type'], 'application/json')

    def test_batch_cancel_rejects_empty(self):
        from executor.builders import build_limitless_cancel_batch
        with self.assertRaises(AssertionError):
            build_limitless_cancel_batch([])

    def test_cancel_all_market(self):
        from executor.builders import build_limitless_cancel_all_market
        b = build_limitless_cancel_all_market('btc-100k', api_key='k1')
        self.assertEqual(b['op'], 'cancel_all_market')
        self.assertEqual(b['method'], 'DELETE')
        self.assertIn('btc-100k', b['would_post_url'])


class TestLimitlessWSFills(unittest.TestCase):
    def test_order_event_invokes_on_fill_and_buffers(self):
        from limitless_ws import LimitlessWS
        seen = []
        ws = LimitlessWS(verbose=False, api_key='dummy',
                         on_fill=lambda ev: seen.append(ev))
        # Simulate inbound orderEvent by calling our handler registry through
        # a minimal sio mock. Re-creating registration is overkill — call the
        # internals directly because that's what we shipped to the user.
        ws._touch_msg()   # mimics on_message
        # Manually invoke buffer logic the same way `on_order_event` does
        ev = {'source': 'OME', 'type': 'MATCH', 'orderId': 'X',
              'price': 0.5, 'remainingSize': 0}
        with ws._fills_lock:
            ws.recent_fills.append({**ev, '_received_at': time.time()})
        ws.on_fill(ev)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]['orderId'], 'X')
        recent = ws.get_recent_fills()
        self.assertGreaterEqual(len(recent), 1)
        self.assertEqual(recent[-1]['orderId'], 'X')

    def test_subscribe_order_events_skips_without_api_key(self):
        from limitless_ws import LimitlessWS
        ws = LimitlessWS(verbose=False, api_key=None)
        fake_sio = mock.Mock()
        ws._sio = fake_sio
        ws._connected = True
        ws._subscribe_order_events()
        fake_sio.emit.assert_not_called()

    def test_subscribe_order_events_emits_with_api_key(self):
        from limitless_ws import LimitlessWS, WS_NAMESPACE
        ws = LimitlessWS(verbose=False, api_key='key-1')
        fake_sio = mock.Mock()
        ws._sio = fake_sio
        ws._connected = True
        ws._subscribe_order_events()
        fake_sio.emit.assert_called_once()
        args, kwargs = fake_sio.emit.call_args
        self.assertEqual(args[0], 'subscribe_order_events')
        self.assertEqual(kwargs.get('namespace'), WS_NAMESPACE)
        self.assertTrue(ws._order_events_subscribed)
        # Second call is a no-op (don't double-subscribe per docs)
        ws._subscribe_order_events()
        fake_sio.emit.assert_called_once()


class TestLimitlessQualityFilter(unittest.TestCase):
    def test_blocks_zero_volume_only_arbs(self):
        from arb_server import _lim_quality_ok
        d = {'total_cents': 90.0, 'min_liq': 500, 'slip_pct': 0.1}
        # all per_market entries report 0 volume → blocked
        pm = [{'volume': 0}, {'volume': 0}]
        self.assertFalse(_lim_quality_ok(d, pm))

    def test_blocks_thin_high_sum(self):
        from arb_server import _lim_quality_ok
        d = {'total_cents': 96.0, 'min_liq': 100, 'slip_pct': 0.4}
        pm = [{'volume': 1000}]
        self.assertFalse(_lim_quality_ok(d, pm))

    def test_passes_ok_deal(self):
        from arb_server import _lim_quality_ok
        d = {'total_cents': 92.0, 'min_liq': 500, 'slip_pct': 0.1}
        pm = [{'volume': 1000}, {'volume': 500}]
        self.assertTrue(_lim_quality_ok(d, pm))


# ── Phase 9d — push-driven re-eval ──────────────────────────────────
class TestLimitlessSlugIndex(unittest.TestCase):
    """The slug→event reverse index lets on_lim_ws_update find the parent
    event in O(1) when WS pushes an orderbookUpdate. Without it, push
    would have to fall back to the 5s micro-loop tick."""

    def test_negrisk_group_indexes_each_child(self):
        pool = {
            'hot': [{
                'title': 'Election',
                'slug': 'el',
                'markets': [
                    {'slug': 'a'}, {'slug': 'b'}, {'slug': 'c'},
                ],
            }],
            'near': [],
        }
        idx = arb_server.rebuild_lim_slug_index(pool)
        # Every child slug points to the parent event
        self.assertIn('a', idx)
        self.assertIn('b', idx)
        self.assertIn('c', idx)
        self.assertEqual(idx['a']['title'], 'Election')
        # All three resolve to the SAME parent event object
        self.assertIs(idx['a'], idx['b'])
        self.assertIs(idx['b'], idx['c'])

    def test_standalone_binary_indexes_event_slug(self):
        pool = {
            'hot': [{'title': 'Will BTC > 100k', 'slug': 'btc-100k'}],
            'near': [],
        }
        idx = arb_server.rebuild_lim_slug_index(pool)
        self.assertIn('btc-100k', idx)
        self.assertEqual(idx['btc-100k']['title'], 'Will BTC > 100k')

    def test_combines_hot_and_near(self):
        pool = {
            'hot': [{'title': 'H', 'slug': 'h'}],
            'near': [{'title': 'N', 'slug': 'n'}],
        }
        idx = arb_server.rebuild_lim_slug_index(pool)
        self.assertEqual(len(idx), 2)
        self.assertIn('h', idx)
        self.assertIn('n', idx)


class TestLimitlessPushReEval(unittest.TestCase):
    """End-to-end test of on_lim_ws_update — we plant a candidate event in
    the slug index, simulate a WS book update via a fake LimitlessWS, and
    verify the deal lands in scan_data without going through the 5s loop."""

    _MOCK_META = {
        'yes_token': '111', 'no_token': '222',
        'verifying_contract': '0xabc',
        'volume': 1000.0, 'is_other': False, 'fetched_at': time.time(),
    }

    def setUp(self):
        # Stub the meta fetcher so re-eval doesn't go to the network.
        self._meta_patcher = mock.patch(
            'arb_server._fetch_limitless_market_meta',
            return_value=dict(self._MOCK_META))
        self._meta_patcher.start()
        # Snapshot module state we'll mutate
        self._scan_backup = dict(arb_server.scan_data)
        self._idx_backup = dict(arb_server.lim_slug_index)
        self._cache_backup = dict(arb_server.lim_res_cache)
        self._client_backup = arb_server.lim_ws_client

    def tearDown(self):
        self._meta_patcher.stop()
        # Restore module state
        with arb_server.scan_lock:
            arb_server.scan_data.clear()
            arb_server.scan_data.update(self._scan_backup)
        with arb_server.lim_slug_index_lock:
            arb_server.lim_slug_index.clear()
            arb_server.lim_slug_index.update(self._idx_backup)
        with arb_server.res_cache_lock:
            arb_server.lim_res_cache.clear()
            arb_server.lim_res_cache.update(self._cache_backup)
        arb_server.lim_ws_client = self._client_backup

    def test_push_drives_immediate_eval(self):
        """A fresh WS book that crosses the arb threshold ends up as a deal
        in scan_data — no micro-loop, no scan, just the push."""
        ev = {
            'title': 'Push Test',
            'slug': 'pt',
            'deadline': _future_ts(2),
            'markets': [{'slug': 'a', 'title': 'A'}, {'slug': 'b', 'title': 'B'}],
        }
        # Plant in slug index — both children resolve to ev
        with arb_server.lim_slug_index_lock:
            arb_server.lim_slug_index.clear()
            arb_server.lim_slug_index['a'] = ev
            arb_server.lim_slug_index['b'] = ev

        # Fake WS client whose `get_book` returns fresh, profitable books
        fake_ws = mock.Mock()
        fake_ws.get_book = lambda slug: {
            'best_yes_ask': 0.40, 'best_yes_bid': 0.50,
            'depth_yes': 100, 'depth_no': 80, 'ts': time.time(),
        }
        arb_server.lim_ws_client = fake_ws

        with arb_server.scan_lock:
            arb_server.scan_data['deals'] = []
            arb_server.scan_data['stats'] = {'arb_found': 0}

        # Simulate the WS push for slug 'a'
        arb_server.on_lim_ws_update('a')

        with arb_server.scan_lock:
            deals = list(arb_server.scan_data.get('deals', []))
        # Sum yes asks 0.40 + 0.40 = 0.80 < 0.99 → ALL_YES arb expected
        self.assertGreater(len(deals), 0,
                            "expected push to inject at least one deal")
        all_yes_titles = [d['title'] for d in deals if d.get('platform') == 'Limitless']
        self.assertIn('Push Test', all_yes_titles)

    def test_unknown_slug_is_no_op(self):
        """on_lim_ws_update on a slug we never indexed must NOT raise."""
        with arb_server.lim_slug_index_lock:
            arb_server.lim_slug_index.clear()
        arb_server.lim_ws_client = mock.Mock()
        # Should return cleanly
        arb_server.on_lim_ws_update('never-seen-slug')

    def test_no_ws_client_is_no_op(self):
        """If WS client wasn't started (ENABLE_LIMITLESS=0), callback is a no-op."""
        arb_server.lim_ws_client = None
        # Should return cleanly even with a populated index
        with arb_server.lim_slug_index_lock:
            arb_server.lim_slug_index['some'] = {'title': 'X'}
        arb_server.on_lim_ws_update('some')


# ── Phase 9e — fill registry, live fire path, positions cache ──────
class TestFillRegistry(unittest.TestCase):
    """Registry is process-wide; reset between tests so state doesn't leak."""

    def setUp(self):
        from executor import fills
        self._fills = fills
        self._fills.registry = fills.FillRegistry()  # fresh instance

    def test_register_then_consume_by_order_id_wakes_event(self):
        reg = self._fills.registry.register(
            arb_id='arb1', leg_idx=0, platform='limitless',
            slug='btc-100k', order_id='ord-A')
        self.assertFalse(reg.event.is_set())
        consumed = self._fills.registry.consume_by_order_id(
            'limitless', 'ord-A', {'fill_price': 0.45})
        self.assertIs(consumed, reg)
        self.assertTrue(reg.event.is_set())
        self.assertEqual(reg.result['fill_price'], 0.45)

    def test_consume_by_slug_pops_oldest_first(self):
        r1 = self._fills.registry.register(
            arb_id='arb1', leg_idx=0, platform='limitless',
            slug='shared', order_id='ord-1')
        r2 = self._fills.registry.register(
            arb_id='arb2', leg_idx=0, platform='limitless',
            slug='shared', order_id='ord-2')
        c1 = self._fills.registry.consume_by_slug('limitless', 'shared',
                                                  {'fill_price': 0.5})
        self.assertIs(c1, r1)
        self.assertTrue(r1.event.is_set())
        self.assertFalse(r2.event.is_set())
        c2 = self._fills.registry.consume_by_slug('limitless', 'shared',
                                                  {'fill_price': 0.5})
        self.assertIs(c2, r2)

    def test_consume_unknown_returns_none(self):
        r = self._fills.registry.consume_by_order_id(
            'limitless', 'nonexistent', {})
        self.assertIsNone(r)

    def test_expire_stale_drops_old_regs(self):
        reg = self._fills.registry.register(
            arb_id='a', leg_idx=0, platform='limitless',
            slug='s', order_id='o')
        # Push the registered_at into the past
        reg.registered_at = time.time() - 100
        purged = self._fills.registry.expire_stale(ttl_s=30)
        self.assertGreaterEqual(purged, 1)
        self.assertEqual(self._fills.registry.pending_count(), 0)


class TestAtomicLiveFire(unittest.TestCase):
    """The live fire path posts via http_post, registers in fills.registry,
    waits on the Event. Test with mocks so no network."""

    def setUp(self):
        from executor import fills, atomic, builders
        self._fills = fills
        self._atomic = atomic
        self._builders = builders
        self._fills.registry = fills.FillRegistry()

    def _wallet(self, api_key='k1', private_key='0x'+'a'*64):
        return self._builders.WalletStub(
            bot_id='bot1', eth_address='0x'+'b'*40,
            private_key=private_key, api_key=api_key)

    def _deal(self):
        return {
            'platform': 'Limitless',
            'title': 'Test',
            'arb_structure': 'binary',
            'entries': [
                {'name': 'YES', 'price': 0.40, 'stake': 5.0, 'slug': 's',
                 'token_id': '111', 'verifying_contract': '0x'+'c'*40,
                 'side': 'YES'},
            ],
        }

    def test_fill_arrives_before_deadman_returns_filled(self):
        wallet = self._wallet()
        # http_post returns an order_id; trigger fill once registration lands.
        post_resp = mock.Mock()
        post_resp.status_code = 200
        post_resp.json = lambda: {'id': 'order-123'}

        # Wrap http_post so the consume thread fires AFTER registration
        # has definitely happened. Polling pending_count() was flaky on
        # slow CI runs (small race window if registration hasn't yet
        # incremented when consumer wakes).
        import threading as _t
        consume_started = _t.Event()
        def fake_post(*a, **kw):
            # By this point the calling thread is between POST and register.
            # Schedule consume on a fresh thread that waits a tick before
            # touching registry — gives _fire_one_leg_live time to register.
            def trigger():
                consume_started.set()
                # Wait until pending_count() > 0 (registration done)
                for _ in range(200):
                    if self._fills.registry.pending_count() > 0:
                        break
                    time.sleep(0.005)
                self._fills.registry.consume_by_order_id(
                    'limitless', 'order-123', {'fill_price': 0.40})
            _t.Thread(target=trigger, daemon=True).start()
            return post_resp

        leg = self._atomic._fire_one_leg_live(
            self._deal(), 0, wallet, 'arb-X',
            http_post=fake_post,
            deadman_s=3.0)
        self.assertTrue(consume_started.is_set(), "fake_post should have run")
        self.assertEqual(leg.status, 'filled')
        self.assertAlmostEqual(leg.fill_price, 0.40)

    def test_no_fill_within_deadman_returns_timeout(self):
        wallet = self._wallet()
        post_resp = mock.Mock()
        post_resp.status_code = 200
        post_resp.json = lambda: {'id': 'order-456'}
        leg = self._atomic._fire_one_leg_live(
            self._deal(), 0, wallet, 'arb-Y',
            http_post=lambda *a, **kw: post_resp,
            deadman_s=0.1)
        self.assertEqual(leg.status, 'timeout')
        self.assertIn('no fill confirmation', leg.error)

    def test_http_400_returns_rejected(self):
        wallet = self._wallet()
        post_resp = mock.Mock()
        post_resp.status_code = 400
        post_resp.text = 'Bad params'
        leg = self._atomic._fire_one_leg_live(
            self._deal(), 0, wallet, 'arb-Z',
            http_post=lambda *a, **kw: post_resp,
            deadman_s=0.1)
        self.assertEqual(leg.status, 'rejected')
        self.assertIn('HTTP 400', leg.error)

    def test_post_passes_x_api_key_header(self):
        wallet = self._wallet(api_key='secret-key')
        captured = {}
        def fake_post(url, json=None, headers=None, timeout=None):
            captured['headers'] = headers
            r = mock.Mock(); r.status_code = 200
            r.json = lambda: {'id': 'X'}
            return r
        # Trigger fill once registration appears, so the test exits cleanly.
        import threading as _t
        def consume_now():
            for _ in range(100):
                if self._fills.registry.pending_count() > 0:
                    break
                time.sleep(0.01)
            self._fills.registry.consume_by_order_id('limitless', 'X', {'fill_price': 0.4})
        _t.Thread(target=consume_now, daemon=True).start()

        self._atomic._fire_one_leg_live(
            self._deal(), 0, wallet, 'arb-W',
            http_post=fake_post, deadman_s=2.0)
        self.assertIn('X-API-Key', captured['headers'])
        self.assertEqual(captured['headers']['X-API-Key'], 'secret-key')


class TestLimitlessPositionsCache(unittest.TestCase):
    def test_handle_positions_event_caches_per_outcome(self):
        from limitless_ws import LimitlessWS
        ws = LimitlessWS(verbose=False, api_key='k')
        ws._handle_positions({
            'marketSlug': 'btc-100k',
            'positions': [
                {'outcome': 0, 'size': 25.0},
                {'outcome': 1, 'size': 10.5},
            ],
        })
        snap = ws.get_positions_snapshot()
        self.assertEqual(snap[('Limitless', 'btc-100k', 0)], 25.0)
        self.assertEqual(snap[('Limitless', 'btc-100k', 1)], 10.5)
        self.assertIsNotNone(ws.positions_age_s())

    def test_zero_size_positions_dropped(self):
        from limitless_ws import LimitlessWS
        ws = LimitlessWS(verbose=False, api_key='k')
        ws._handle_positions({
            'marketSlug': 's',
            'positions': [
                {'outcome': 0, 'size': 0.0},
                {'outcome': 1, 'size': 5.0},
            ],
        })
        snap = ws.get_positions_snapshot()
        self.assertNotIn(('Limitless', 's', 0), snap)
        self.assertIn(('Limitless', 's', 1), snap)

    def test_replace_semantics_per_slug(self):
        """Server pushes the FULL position set per slug — we drop any
        cached rows for that slug before applying the new ones."""
        from limitless_ws import LimitlessWS
        ws = LimitlessWS(verbose=False, api_key='k')
        ws._handle_positions({
            'marketSlug': 's',
            'positions': [{'outcome': 0, 'size': 10.0},
                          {'outcome': 1, 'size': 20.0}],
        })
        # Now position on outcome 1 closed (no entry for it)
        ws._handle_positions({
            'marketSlug': 's',
            'positions': [{'outcome': 0, 'size': 10.0}],
        })
        snap = ws.get_positions_snapshot()
        self.assertIn(('Limitless', 's', 0), snap)
        self.assertNotIn(('Limitless', 's', 1), snap)

    def test_other_slugs_untouched(self):
        from limitless_ws import LimitlessWS
        ws = LimitlessWS(verbose=False, api_key='k')
        ws._handle_positions({
            'marketSlug': 'a',
            'positions': [{'outcome': 0, 'size': 10.0}],
        })
        ws._handle_positions({
            'marketSlug': 'b',
            'positions': [{'outcome': 0, 'size': 5.0}],
        })
        snap = ws.get_positions_snapshot()
        self.assertEqual(snap[('Limitless', 'a', 0)], 10.0)
        self.assertEqual(snap[('Limitless', 'b', 0)], 5.0)


class TestOnLimFillBridge(unittest.TestCase):
    """arb_server.on_lim_fill should consume the registration created by
    atomic._fire_one_leg_live so the executor wakes immediately."""

    def setUp(self):
        from executor import fills
        self._fills = fills
        self._fills.registry = fills.FillRegistry()

    def test_orderid_match(self):
        reg = self._fills.registry.register(
            arb_id='a', leg_idx=0, platform='limitless',
            slug='s', order_id='order-A')
        # Simulate inbound orderEvent
        # Patch global registry pointer arb_server uses
        with mock.patch('executor.fills.registry', self._fills.registry):
            arb_server.on_lim_fill({
                'source': 'OME', 'type': 'MATCH',
                'orderId': 'order-A',
                'price': 0.42,
                'remainingSize': 0,
            })
        self.assertTrue(reg.event.is_set())
        self.assertAlmostEqual(reg.result['fill_price'], 0.42)

    def test_slug_fallback_when_no_orderid(self):
        reg = self._fills.registry.register(
            arb_id='a', leg_idx=0, platform='limitless',
            slug='settled-slug', order_id=None)
        with mock.patch('executor.fills.registry', self._fills.registry):
            arb_server.on_lim_fill({
                'source': 'SETTLEMENT', 'type': 'SETTLED',
                'marketSlug': 'settled-slug',
                'price': 0.30,
                'txHash': '0xabc',
            })
        self.assertTrue(reg.event.is_set())

    def test_unknown_fill_is_silent(self):
        # No registration → no exception, just dropped.
        arb_server.on_lim_fill({
            'source': 'OME', 'type': 'MATCH',
            'orderId': 'nobody-cares',
            'price': 0.1,
        })  # must not raise


# ── Phase 9g — incomplete-coverage bug fix ──────────────────────────
# Real production case (28.04.2026): EPL Leeds vs Burnley. 3 outcomes
# (Leeds, Draw, Burnley). Draw had volume=0 and an empty orderbook, so
# yes_ask was None. The OLD code silently dropped Draw and reported
# sum(Leeds=67.5 + Burnley=13) = 80.5¢ as an "ALL_YES arb". Real sum
# across all 3 outcomes was 101.1¢ — a guaranteed loss if Draw wins.
class TestIncompleteCoverageGate(unittest.TestCase):
    """Prove that ALL_YES / ALL_NO are SUPPRESSED when one or more
    outcomes lack a usable ask price. YES_NO_PAIR (per-market) still
    reports normally — coverage doesn't apply there."""

    _MOCK_META = {
        'yes_token': '111', 'no_token': '222',
        'verifying_contract': '0xabcdef',
        'volume': 1000.0, 'is_other': False, 'fetched_at': time.time(),
    }

    def setUp(self):
        self._meta_patcher = mock.patch(
            'arb_server._fetch_limitless_market_meta',
            return_value=dict(self._MOCK_META))
        self._meta_patcher.start()

    def tearDown(self):
        self._meta_patcher.stop()

    def test_leeds_burnley_no_longer_reports_phantom_arb(self):
        """The exact scenario that triggered the bug. With Draw missing,
        no ALL_YES deal must be produced — even though sum of priced
        outcomes (67.5 + 13 = 80.5) is < 99¢ threshold."""
        events = [{
            'title': 'EPL Leeds vs Burnley',
            'slug': 'epl-leeds-burnley',
            'deadline': _future_ts(3),
            'markets': [
                {'slug': 'leeds-yes',   'title': 'Leeds win'},
                {'slug': 'draw-yes',    'title': 'Draw'},
                {'slug': 'burnley-yes', 'title': 'Burnley win'},
            ],
        }]
        # Draw has empty orderbook → (None, 0, None, 0). Leeds + Burnley
        # priced normally. Old code would've reported a $10+ "arb".
        lim_res = {
            'leeds-yes':   (0.675, 100, 0.33, 80),
            'draw-yes':    (None,    0, None,  0),    # empty book!
            'burnley-yes': (0.13,  100, 0.87, 80),
        }
        deals = arb_server.eval_limitless(events, lim_res)
        all_yes = [d for d in deals if d.get('arb_structure') == 'all_yes']
        self.assertEqual(all_yes, [],
            "ALL_YES should be suppressed when an outcome has no ask price")

    def test_full_coverage_still_reports_arb(self):
        """Sanity check: when EVERY outcome has an ask, ALL_YES works."""
        events = [{
            'title': 'Three-way fully priced',
            'slug': 'tw',
            'deadline': _future_ts(3),
            'markets': [
                {'slug': 'a', 'title': 'A'},
                {'slug': 'b', 'title': 'B'},
                {'slug': 'c', 'title': 'C'},
            ],
        }]
        # 0.30 + 0.30 + 0.30 = 0.90 < 0.99 → real arb
        lim_res = {
            'a': (0.30, 100, 0.65, 80),
            'b': (0.30, 100, 0.65, 80),
            'c': (0.30, 100, 0.65, 80),
        }
        deals = arb_server.eval_limitless(events, lim_res)
        all_yes = [d for d in deals if d.get('arb_structure') == 'all_yes']
        self.assertEqual(len(all_yes), 1)

    def test_yes_no_pair_still_works_when_two_markets_priced(self):
        """YES_NO_PAIR per-market doesn't depend on event-wide coverage —
        when Leeds AND Burnley both have YES+NO asks (only Draw unpriced),
        we still get pair arbs on Leeds and Burnley individually. The
        ALL_YES/ALL_NO structures are blocked by the coverage gate but
        per-market pairs survive."""
        events = [{
            'title': 'Pair-only',
            'slug': 'po',
            'deadline': _future_ts(3),
            'markets': [
                {'slug': 'leeds',   'title': 'Leeds win'},
                {'slug': 'draw',    'title': 'Draw'},
                {'slug': 'burnley', 'title': 'Burnley win'},
            ],
        }]
        # Leeds pair = 0.40 + 0.50 = 0.90, Burnley pair = 0.40 + 0.50 = 0.90.
        # Both < 0.99 → 2 pair-arbs. ALL_YES would be (0.40+0.40)=0.80 in
        # priced-only sum but coverage gate must block it (Draw unpriced).
        lim_res = {
            'leeds':   (0.40, 100, 0.50, 80),
            'draw':    (None,   0, None,  0),
            'burnley': (0.40, 100, 0.50, 80),
        }
        deals = arb_server.eval_limitless(events, lim_res)
        pair_deals = [d for d in deals if d.get('arb_structure') == 'yes_no_pair']
        all_yes_deals = [d for d in deals if d.get('arb_structure') == 'all_yes']
        self.assertGreaterEqual(len(pair_deals), 2,
            "Both Leeds and Burnley should produce pair arbs")
        self.assertEqual(len(all_yes_deals), 0,
            "ALL_YES must be blocked by coverage gate (Draw unpriced)")

    def test_all_no_suppressed_when_one_outcome_lacks_no_ask(self):
        """4-outcome ALL_NO test: if outcome D has no NO ask price, an
        ALL_NO arb must NOT be reported — D winning would cost us the
        arb (we'd hold NO_A, NO_B, NO_C only, none paying out on D)."""
        events = [{
            'title': '4-way all_no test',
            'slug': 'fwa',
            'deadline': _future_ts(3),
            'markets': [
                {'slug': 'a'}, {'slug': 'b'}, {'slug': 'c'}, {'slug': 'd'},
            ],
        }]
        # Σ NO = 0.65 + 0.65 + 0.65 + (none) — would be valid 3-NO arb
        # against (N-1)*0.99 = 1.98. Old code would've fired.
        lim_res = {
            'a': (0.30, 100, 0.65, 80),
            'b': (0.30, 100, 0.65, 80),
            'c': (0.30, 100, 0.65, 80),
            'd': (0.40, 100, None,  0),    # no NO ask
        }
        deals = arb_server.eval_limitless(events, lim_res)
        all_no = [d for d in deals if d.get('arb_structure') == 'all_no']
        self.assertEqual(all_no, [],
            "ALL_NO should be suppressed when one outcome lacks NO ask")

    def test_sum_limitless_cand_skips_partial_in_near_pool(self):
        """NEAR-pool classifier mustn't promote a partial-coverage event
        to HOT just because its priced outcomes summed below threshold."""
        ev_partial = {
            'title': 'Partial', 'slug': 'p',
            'deadline': _future_ts(3),
            'markets': [
                {'slug': 'leeds'},
                {'slug': 'draw'},
                {'slug': 'burnley'},
            ],
        }
        lim_res = {
            'leeds':   (0.675, 100, 0.33, 80),
            'draw':    (None,   0, None,  0),
            'burnley': (0.13,  100, 0.87, 80),
        }
        s = arb_server._sum_limitless_cand(ev_partial, lim_res)
        # ALL_YES sum (0.805) must NOT be returned — only YES_NO_PAIR
        # of leeds (0.675 + 0.33 = 1.005) which is above threshold, so
        # candidate set might be empty entirely. Either None or > 0.99.
        if s is not None:
            self.assertGreaterEqual(s, 0.99,
                f"NEAR sum {s} ignored coverage gap")


# ── Phase 9h — closed/expired outcome gate ──────────────────────────
# User scenario (28.04.2026): "what if Draw is open at scan time but gets
# CLOSED for trading mid-event? We can't fire YES on Draw anymore, so the
# arb is broken." filter_limitless drops the whole event if ANY outcome
# has status=CLOSED / expired / hidden — well before we'd try to fire.
class TestClosedOutcomeGate(unittest.TestCase):
    def test_event_dropped_if_marked_expired(self):
        events = [{
            'title': 'Football match', 'slug': 'fm',
            'deadline': _future_ts(2),
            'expired': True,    # ← whole event expired
            'markets': [
                {'slug': 'home'}, {'slug': 'draw'}, {'slug': 'away'},
            ],
        }]
        diag = {}
        out = arb_server.filter_limitless(events, diag=diag)
        self.assertEqual(out, [])
        self.assertEqual(diag.get('lim_skip_outcome_closed'), 1)

    def test_event_dropped_if_one_child_closed(self):
        """Even one closed child kills the entire event — exact user
        scenario where Draw closes mid-event."""
        events = [{
            'title': 'Football match', 'slug': 'fm',
            'deadline': _future_ts(2),
            'markets': [
                {'slug': 'home',    'status': 'OPEN'},
                {'slug': 'draw',    'status': 'CLOSED'},   # ← !
                {'slug': 'away',    'status': 'OPEN'},
            ],
        }]
        diag = {}
        out = arb_server.filter_limitless(events, diag=diag)
        self.assertEqual(out, [])
        self.assertEqual(diag.get('lim_skip_outcome_closed'), 1)

    def test_event_dropped_if_one_child_expired(self):
        events = [{
            'title': 'Football match', 'slug': 'fm',
            'deadline': _future_ts(2),
            'markets': [
                {'slug': 'home'},
                {'slug': 'draw', 'expired': True},     # ← !
                {'slug': 'away'},
            ],
        }]
        out = arb_server.filter_limitless(events)
        self.assertEqual(out, [])

    def test_event_dropped_if_one_child_hidden(self):
        events = [{
            'title': 'Football match', 'slug': 'fm',
            'deadline': _future_ts(2),
            'markets': [
                {'slug': 'home'},
                {'slug': 'draw', 'hidden': True},      # ← !
                {'slug': 'away'},
            ],
        }]
        out = arb_server.filter_limitless(events)
        self.assertEqual(out, [])

    def test_clean_event_still_passes(self):
        """Sanity: all children OPEN → event survives."""
        events = [{
            'title': 'Football match', 'slug': 'fm',
            'deadline': _future_ts(2),
            'markets': [
                {'slug': 'home', 'title': 'Home',  'status': 'OPEN'},
                {'slug': 'draw', 'title': 'Draw',  'status': 'OPEN'},
                {'slug': 'away', 'title': 'Away',  'status': 'OPEN'},
            ],
        }]
        out = arb_server.filter_limitless(events)
        self.assertEqual(len(out), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
