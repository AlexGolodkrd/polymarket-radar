"""Unit tests for risk.network_check (Layer 3 VPN safety, 28.04.2026)."""
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'Scripts'))

from risk import network_check


class _NetTest(unittest.TestCase):
    def setUp(self):
        # Reset cache between tests so each starts clean
        network_check.reset_for_test()
        # Default: tests run with no allowed-countries set unless override
        self._allowed_patch = mock.patch.object(network_check, 'ALLOWED_COUNTRIES', set())
        self._allowed_patch.start()

    def tearDown(self):
        self._allowed_patch.stop()
        network_check.reset_for_test()


# ── check_country_allowed (the gate) ────────────────────────────────
class TestGate(_NetTest):
    def test_disabled_when_no_allowed_countries(self):
        """Empty ALLOWED_COUNTRIES = check disabled, always passes."""
        with mock.patch.object(network_check, 'ALLOWED_COUNTRIES', set()):
            ok, reason = network_check.check_country_allowed()
        self.assertTrue(ok)
        self.assertIsNone(reason)

    def test_allowed_country_passes(self):
        with mock.patch.object(network_check, 'ALLOWED_COUNTRIES', {'GE'}):
            with mock.patch.object(network_check, '_fetch_now',
                                   return_value=('1.2.3.4', 'GE', None)):
                ok, reason = network_check.check_country_allowed()
        self.assertTrue(ok)
        self.assertIsNone(reason)

    def test_disallowed_country_blocked(self):
        with mock.patch.object(network_check, 'ALLOWED_COUNTRIES', {'GE'}):
            with mock.patch.object(network_check, '_fetch_now',
                                   return_value=('5.6.7.8', 'RU', None)):
                ok, reason = network_check.check_country_allowed()
        self.assertFalse(ok)
        self.assertIn('ip_geo_disallowed', reason)
        self.assertIn("'RU'", reason)
        self.assertIn('GE', reason)

    def test_us_country_blocked_even_for_multi_allowed(self):
        with mock.patch.object(network_check, 'ALLOWED_COUNTRIES', {'GE', 'AM', 'TR'}):
            with mock.patch.object(network_check, '_fetch_now',
                                   return_value=('192.3.146.164', 'US', None)):
                ok, reason = network_check.check_country_allowed()
        self.assertFalse(ok)
        self.assertIn('US', reason)

    def test_failed_fetch_is_blocked_failsafe(self):
        """Fail-safe: if all geo providers fail, BLOCK new fires.
        Trades availability for safety — correct for an arb bot whose
        leak risk (account ban) >> missed-scan risk."""
        with mock.patch.object(network_check, 'ALLOWED_COUNTRIES', {'GE'}):
            with mock.patch.object(network_check, '_fetch_now',
                                   return_value=(None, None, 'all providers failed')):
                ok, reason = network_check.check_country_allowed()
        self.assertFalse(ok)
        self.assertIn('network_check_failed', reason)


# ── caching ──────────────────────────────────────────────────────────
class TestCache(_NetTest):
    def test_cache_avoids_repeat_fetches(self):
        with mock.patch.object(network_check, 'ALLOWED_COUNTRIES', {'GE'}):
            with mock.patch.object(network_check, '_fetch_now',
                                   return_value=('1.2.3.4', 'GE', None)) as mocked:
                # First call hits network
                network_check.get_current_ip_country()
                self.assertEqual(mocked.call_count, 1)
                # Second call within cache window should NOT re-fetch
                network_check.get_current_ip_country()
                self.assertEqual(mocked.call_count, 1)

    def test_force_refresh_bypasses_cache(self):
        with mock.patch.object(network_check, '_fetch_now',
                               return_value=('1.2.3.4', 'GE', None)) as mocked:
            network_check.get_current_ip_country()
            network_check.get_current_ip_country(force_refresh=True)
        self.assertEqual(mocked.call_count, 2)


# ── status() diagnostics ─────────────────────────────────────────────
class TestStatus(_NetTest):
    def test_status_shape(self):
        with mock.patch.object(network_check, 'ALLOWED_COUNTRIES', {'GE'}):
            with mock.patch.object(network_check, '_fetch_now',
                                   return_value=('1.2.3.4', 'GE', None)):
                network_check.get_current_ip_country()
                s = network_check.status()
        self.assertTrue(s['enabled'])
        self.assertEqual(s['allowed_countries'], ['GE'])
        self.assertEqual(s['current_country'], 'GE')
        self.assertEqual(s['current_ip'], '1.2.3.4')
        self.assertIsNone(s['last_error'])

    def test_status_disabled_when_empty(self):
        with mock.patch.object(network_check, 'ALLOWED_COUNTRIES', set()):
            s = network_check.status()
        self.assertFalse(s['enabled'])
        self.assertEqual(s['allowed_countries'], [])


# ── Provider parsers (offline tests) ────────────────────────────────
class TestProviders(_NetTest):
    def test_ifconfig_co_parser(self):
        with mock.patch('requests.get') as mocked:
            mocked.return_value.json.return_value = {
                'ip': '95.10.20.30', 'country_iso': 'ge'  # lowercase OK, we upper()
            }
            ip, country = network_check._from_ifconfig_co()
        self.assertEqual(ip, '95.10.20.30')
        self.assertEqual(country, 'GE')

    def test_country_is_parser(self):
        with mock.patch('requests.get') as mocked:
            mocked.return_value.json.return_value = {
                'ip': '95.10.20.30', 'country': 'GE'
            }
            ip, country = network_check._from_country_is()
        self.assertEqual(ip, '95.10.20.30')
        self.assertEqual(country, 'GE')

    def test_fetch_falls_back_to_second_provider(self):
        """If first provider raises, second is tried."""
        def first_fails():
            raise RuntimeError('boom')
        def second_works():
            return ('1.2.3.4', 'GE')
        with mock.patch.object(network_check, '_PROVIDERS', [first_fails, second_works]):
            ip, country, err = network_check._fetch_now()
        self.assertEqual(ip, '1.2.3.4')
        self.assertEqual(country, 'GE')
        self.assertIsNone(err)

    def test_fetch_returns_error_when_all_fail(self):
        def fail1(): raise ValueError('nope')
        def fail2(): raise ValueError('also nope')
        with mock.patch.object(network_check, '_PROVIDERS', [fail1, fail2]):
            ip, country, err = network_check._fetch_now()
        self.assertIsNone(ip)
        self.assertIsNone(country)
        self.assertIsNotNone(err)


if __name__ == '__main__':
    unittest.main(verbosity=2)
