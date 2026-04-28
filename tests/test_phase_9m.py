"""Phase 9m — verify the 4 V2 uncertainty items the user flagged.

1. pUSD address: now real verified value (0xC011...DFB), not placeholder
2. /markets/{cid} REST endpoint: confirmed correct, plus we now use
   additional fields (accepting_orders, accepting_order_timestamp,
   seconds_delay, neg_risk_market_id, rewards)
3. Pre-fire gate in atomic.build_poly_order: aborts leg if market state
   changed between scan and fire
4. NegRisk routes: confirmed single POST /order; builder field stays zero
"""
import os
import sys
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'Scripts'))

import arb_server
from executor import builders, atomic


# ── 1. pUSD verified address ───────────────────────────────────────
class TestPusdAddressVerified(unittest.TestCase):
    def test_real_pusd_address(self):
        import polymarket_approve as pa
        # Should be the on-chain-verified pUSD proxy, NOT a placeholder
        # Real address: 0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB
        self.assertTrue(pa.PUSD_ADDRESS.lower().startswith('0xc011a7e12'),
            f"PUSD_ADDRESS must be the verified Polymarket pUSD: got {pa.PUSD_ADDRESS}")

    def test_collateral_onramp_present(self):
        import polymarket_approve as pa
        # Phase 9m: separate Onramp contract for wrap()/unwrap()
        self.assertTrue(hasattr(pa, 'COLLATERAL_ONRAMP'))
        self.assertTrue(pa.COLLATERAL_ONRAMP.lower().startswith('0x93070a847'),
            f"COLLATERAL_ONRAMP must be 0x93070a847efEf7F70739046A929D47a521F5B8ee")

    def test_negrisk_adapter_present(self):
        import polymarket_approve as pa
        # NegRiskAdapter is a separate contract relevant for negRisk flow
        self.assertTrue(hasattr(pa, 'NEGRISK_ADAPTER'))


# ── 2. Extended market info fields ─────────────────────────────────
class TestExtendedMarketInfo(unittest.TestCase):
    def setUp(self):
        with arb_server.poly_market_info_lock:
            arb_server.poly_market_info_cache.clear()

    def test_new_fields_populated(self):
        fake = mock.Mock()
        fake.status_code = 200
        fake.json.return_value = {
            'minimum_tick_size': 0.01,
            'minimum_order_size': 5,
            'maker_base_fee': 0,
            'taker_base_fee': 0,
            'neg_risk': True,
            'accepting_orders': True,
            'enable_order_book': True,
            'closed': False,
            'archived': False,
            'active': True,
            'accepting_order_timestamp': 1700000000,
            'seconds_delay': 3,
            'neg_risk_market_id': 'nr-1',
            'neg_risk_request_id': 'nrr-1',
            'rewards': {'rates': [{'asset_address': '0xA', 'rewards_daily_rate': 100}],
                         'min_size': 50, 'max_spread': 100},
        }
        with mock.patch('arb_server.requests.get', return_value=fake):
            rec = arb_server._fetch_poly_market_info('0xCID')
        self.assertEqual(rec['accepting_order_timestamp'], 1700000000)
        self.assertEqual(rec['seconds_delay'], 3)
        self.assertEqual(rec['neg_risk_market_id'], 'nr-1')
        self.assertEqual(rec['neg_risk_request_id'], 'nrr-1')
        self.assertIn('rates', rec['rewards'])

    def test_missing_optional_fields_safe(self):
        """API may not include rewards / neg_risk_market_id on every
        market. Fetcher must not crash."""
        fake = mock.Mock()
        fake.status_code = 200
        fake.json.return_value = {
            'minimum_tick_size': 0.01,
            'minimum_order_size': 5,
            'taker_base_fee': 100,
            'accepting_orders': True,
            'enable_order_book': True,
            'closed': False,
        }
        with mock.patch('arb_server.requests.get', return_value=fake):
            rec = arb_server._fetch_poly_market_info('0xMINI')
        self.assertEqual(rec['accepting_order_timestamp'], 0)
        self.assertEqual(rec['seconds_delay'], 0)
        self.assertEqual(rec['rewards'], {})


# ── 3. Pre-fire gate ────────────────────────────────────────────────
class TestPreFireGate(unittest.TestCase):
    def _deal(self, **entry_overrides):
        entry = {
            'name': 'A', 'price': 0.5, 'stake': 5.0,
            'token_id': '111', 'token_id_yes': '111', 'token_id_no': '222',
            'tick_size': 0.01, 'min_order_size': 1.0, 'neg_risk': False,
            'accepting_orders': True, 'enable_order_book': True,
            'accepting_order_timestamp': 0,
        }
        entry.update(entry_overrides)
        return {
            'platform': 'Polymarket',
            'title': 'T',
            'arb_structure': 'binary',
            'entries': [entry],
        }

    def _wallet(self):
        return builders.WalletStub(bot_id='b', eth_address='0x' + 'a' * 40)

    def test_accepting_orders_false_aborts(self):
        deal = self._deal(accepting_orders=False)
        result = atomic._build_leg(deal, 0, self._wallet())
        self.assertIsNone(result)

    def test_enable_order_book_false_aborts(self):
        deal = self._deal(enable_order_book=False)
        result = atomic._build_leg(deal, 0, self._wallet())
        self.assertIsNone(result)

    def test_pre_market_timestamp_aborts(self):
        future_ts = int(time.time()) + 3600
        deal = self._deal(accepting_order_timestamp=future_ts)
        result = atomic._build_leg(deal, 0, self._wallet())
        self.assertIsNone(result)

    def test_past_timestamp_passes(self):
        past_ts = int(time.time()) - 3600
        deal = self._deal(accepting_order_timestamp=past_ts)
        result = atomic._build_leg(deal, 0, self._wallet())
        self.assertIsNotNone(result, "past timestamp should not block fire")

    def test_clean_market_passes(self):
        deal = self._deal()
        result = atomic._build_leg(deal, 0, self._wallet())
        self.assertIsNotNone(result)


# ── 4. NegRisk + builder confirmations ─────────────────────────────
class TestNegRiskAndBuilder(unittest.TestCase):
    def test_post_order_url_unchanged_for_negrisk(self):
        """Both standard and negRisk hit POST /order."""
        wallet = builders.WalletStub(bot_id='b', eth_address='0x' + 'a' * 40)
        std = builders.build_poly_order('1', 'BUY', 0.5, 5.0, wallet, neg_risk=False)
        neg = builders.build_poly_order('1', 'BUY', 0.5, 5.0, wallet, neg_risk=True)
        self.assertEqual(std['would_post_url'], neg['would_post_url'])
        self.assertTrue(std['would_post_url'].endswith('/order'))

    def test_negrisk_signature_uses_different_domain(self):
        """negRisk differs only in EIP-712 domain — that's how server
        routes it. The HTTP body has no neg_risk field."""
        wallet = builders.WalletStub(bot_id='b', eth_address='0x' + 'a' * 40)
        std = builders.build_poly_order('1', 'BUY', 0.5, 5.0, wallet, neg_risk=False)
        neg = builders.build_poly_order('1', 'BUY', 0.5, 5.0, wallet, neg_risk=True)
        # API body has NO 'neg_risk' field — server reads it from signature
        self.assertNotIn('neg_risk', std['body'])
        self.assertNotIn('neg_risk', neg['body'])
        self.assertNotIn('negRisk', std['body'])
        self.assertNotIn('negRisk', neg['body'])
        # But verifyingContract differs
        self.assertNotEqual(
            std['eip712']['domain']['verifyingContract'],
            neg['eip712']['domain']['verifyingContract'],
        )

    def test_builder_field_is_zero(self):
        """Solo trader does NOT register a builderCode; field stays zero."""
        wallet = builders.WalletStub(bot_id='b', eth_address='0x' + 'a' * 40)
        o = builders.build_poly_order('1', 'BUY', 0.5, 5.0, wallet)
        self.assertEqual(o['order']['builder'], builders.ZERO_BYTES32)


if __name__ == '__main__':
    unittest.main(verbosity=2)
