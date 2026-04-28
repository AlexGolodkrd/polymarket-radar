"""Unit tests for Scripts/notify.py — Telegram alert sender (Phase 8 add-on)."""
import os
import sys
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'Scripts'))

import notify


class _NotifyTest(unittest.TestCase):
    def setUp(self):
        notify.reset_for_test()

    def tearDown(self):
        notify.reset_for_test()


# ── Configuration / graceful degradation ─────────────────────────────
class TestConfigured(_NotifyTest):
    def test_unconfigured_send_returns_false(self):
        """When env vars missing, send is a no-op (returns False) — local
        dev workflow keeps working without Telegram setup."""
        with mock.patch.object(notify, 'TELEGRAM_BOT_TOKEN', ''), \
             mock.patch.object(notify, 'TELEGRAM_CHAT_ID', ''):
            self.assertFalse(notify.is_configured())
            self.assertFalse(notify.send('hello'))

    def test_configured_when_both_set(self):
        with mock.patch.object(notify, 'TELEGRAM_BOT_TOKEN', '123:abc'), \
             mock.patch.object(notify, 'TELEGRAM_CHAT_ID', '456'):
            self.assertTrue(notify.is_configured())

    def test_partial_config_treated_as_unconfigured(self):
        with mock.patch.object(notify, 'TELEGRAM_BOT_TOKEN', '123:abc'), \
             mock.patch.object(notify, 'TELEGRAM_CHAT_ID', ''):
            self.assertFalse(notify.is_configured())


# ── Send path (mocked) ────────────────────────────────────────────────
class TestSend(_NotifyTest):
    def test_send_invokes_post(self):
        """Non-rate-limited send should spawn a background thread that
        eventually calls _post_blocking."""
        with mock.patch.object(notify, 'TELEGRAM_BOT_TOKEN', '1:tok'), \
             mock.patch.object(notify, 'TELEGRAM_CHAT_ID', '42'), \
             mock.patch.object(notify, '_post_blocking') as mocked:
            self.assertTrue(notify.send('test'))
            # Daemon thread fires async — wait briefly
            time.sleep(0.05)
            mocked.assert_called_once()
            # Message should include level prefix
            sent_text = mocked.call_args[0][0]
            self.assertIn('test', sent_text)

    def test_level_prefix_emoji(self):
        """info/warn/crit/success each map to a distinct emoji prefix."""
        with mock.patch.object(notify, 'TELEGRAM_BOT_TOKEN', '1:tok'), \
             mock.patch.object(notify, 'TELEGRAM_CHAT_ID', '42'), \
             mock.patch.object(notify, '_post_blocking') as mocked:
            for level in ['info', 'warn', 'crit', 'success']:
                notify.reset_for_test()
                notify.send(f'msg-{level}', level=level)
                time.sleep(0.05)
            sent_msgs = [c[0][0] for c in mocked.call_args_list]
            # Each emoji shows up exactly once
            self.assertTrue(any('ℹ️' in m for m in sent_msgs))
            self.assertTrue(any('⚠️' in m for m in sent_msgs))
            self.assertTrue(any('🚨' in m for m in sent_msgs))
            self.assertTrue(any('✅' in m for m in sent_msgs))


# ── Dedupe / rate limiting ───────────────────────────────────────────
class TestDedupe(_NotifyTest):
    def test_dedupe_suppresses_repeat_within_window(self):
        with mock.patch.object(notify, 'TELEGRAM_BOT_TOKEN', '1:tok'), \
             mock.patch.object(notify, 'TELEGRAM_CHAT_ID', '42'), \
             mock.patch.object(notify, '_post_blocking') as mocked:
            self.assertTrue(notify.send('first', dedupe_key='alert'))
            time.sleep(0.05)
            # Second send within window → suppressed
            self.assertFalse(notify.send('second', dedupe_key='alert'))
            time.sleep(0.05)
            # Only the first call should have made it through
            self.assertEqual(mocked.call_count, 1)

    def test_different_dedupe_keys_each_send(self):
        with mock.patch.object(notify, 'TELEGRAM_BOT_TOKEN', '1:tok'), \
             mock.patch.object(notify, 'TELEGRAM_CHAT_ID', '42'), \
             mock.patch.object(notify, '_post_blocking') as mocked:
            self.assertTrue(notify.send('a', dedupe_key='key_a'))
            self.assertTrue(notify.send('b', dedupe_key='key_b'))
            time.sleep(0.05)
            self.assertEqual(mocked.call_count, 2)

    def test_no_dedupe_key_means_always_sends(self):
        with mock.patch.object(notify, 'TELEGRAM_BOT_TOKEN', '1:tok'), \
             mock.patch.object(notify, 'TELEGRAM_CHAT_ID', '42'), \
             mock.patch.object(notify, '_post_blocking') as mocked:
            self.assertTrue(notify.send('a'))
            self.assertTrue(notify.send('b'))
            time.sleep(0.05)
            self.assertEqual(mocked.call_count, 2)


# ── Network failure handling ─────────────────────────────────────────
class TestNetworkFailure(_NotifyTest):
    def test_post_failure_returns_none_doesnt_crash(self):
        """If Telegram API call raises, _post_blocking should swallow and
        return None — caller shouldn't ever see an exception."""
        with mock.patch.object(notify, 'TELEGRAM_BOT_TOKEN', '1:tok'), \
             mock.patch.object(notify, 'TELEGRAM_CHAT_ID', '42'), \
             mock.patch('urllib.request.urlopen', side_effect=Exception('network down')):
            result = notify._post_blocking('test')
            self.assertIsNone(result)


if __name__ == '__main__':
    unittest.main(verbosity=2)
