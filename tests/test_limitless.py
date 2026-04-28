"""Unit tests for Limitless Exchange integration (Phase 9 add-on, 28.04.2026).

Covers:
- build_limitless_order body shape + EIP-712 ready sign_payload
- _fetch_limitless_orderbook synthesises NO-ask from YES bids correctly
- eval_limitless three structures (A/B/C + standalone binary)
- 10-day window filter on deadline
"""
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'Scripts'))

import arb_server
from executor.builders import build_limitless_order, WalletStub


def _wallet():
    return WalletStub(bot_id='bot1', eth_address='0x' + 'a' * 40)


# ── Builder ──────────────────────────────────────────────────────────
class TestLimitlessBuilder(unittest.TestCase):
    def test_basic_buy_order_shape(self):
        o = build_limitless_order('test-slug', 'BUY', 0.45, 10.0, _wallet())
        self.assertEqual(o['platform'], 'limitless')
        self.assertEqual(o['expected_price'], 0.45)
        self.assertEqual(o['expected_size_usdc'], 10.0)
        body = o['body']
        self.assertEqual(body['marketSlug'], 'test-slug')
        self.assertEqual(body['side'], '0')   # BUY
        self.assertEqual(body['chainId'], 8453)   # Base mainnet
        self.assertEqual(body['signatureType'], '0')
        # makerAmount = 10 USDC × 1e6
        self.assertEqual(body['makerAmount'], '10000000')
        # takerAmount = (10 / 0.45) × 1e6 ≈ 22222222
        self.assertEqual(body['takerAmount'], '22222222')

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
        self.assertEqual(o['body']['side'], '1')


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


if __name__ == '__main__':
    unittest.main(verbosity=2)
