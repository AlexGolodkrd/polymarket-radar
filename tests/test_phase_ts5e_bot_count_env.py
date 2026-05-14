"""Phase TS-5e (14.05.2026) — BOT_COUNT + MIN_USDC_PER_BOT env-overridable.

Hard-coded 6 in wallets/config.py blocked the single-bot pilot mode
(operator wanted $5/wallet across 1 bot). Defaults preserved at 6/60.
This test pins the env contract so a future config refactor can't
silently break single-bot mode.
"""
import importlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


def _reload_with_env(env: dict):
    """Reload wallets.config with the given env, return the module."""
    for k in ('BOT_COUNT', 'MIN_USDC_PER_BOT'):
        os.environ.pop(k, None)
    for k, v in env.items():
        os.environ[k] = v
    from wallets import config  # type: ignore
    importlib.reload(config)
    return config


def test_default_preserves_baseline():
    """No env → BOT_COUNT=6, MIN_USDC_PER_BOT=60.0 (baseline)."""
    cfg = _reload_with_env({})
    assert cfg.BOT_COUNT == 6
    assert cfg.MIN_USDC_PER_BOT == 60.0


def test_env_override_single_bot_mode():
    """Operator's single-bot pilot: BOT_COUNT=1, MIN_USDC_PER_BOT=5."""
    cfg = _reload_with_env({'BOT_COUNT': '1', 'MIN_USDC_PER_BOT': '5'})
    assert cfg.BOT_COUNT == 1
    assert cfg.MIN_USDC_PER_BOT == 5.0


def test_bot_count_clamped_upper():
    """BOT_COUNT > 6 → clamped to 6 (Credentials.env has 6 wallet slots)."""
    cfg = _reload_with_env({'BOT_COUNT': '100'})
    assert cfg.BOT_COUNT == 6


def test_bot_count_clamped_lower():
    """BOT_COUNT = 0 would disable coordinator entirely → clamp to 1."""
    cfg = _reload_with_env({'BOT_COUNT': '0'})
    assert cfg.BOT_COUNT == 1


def test_bot_count_negative_clamped():
    cfg = _reload_with_env({'BOT_COUNT': '-3'})
    assert cfg.BOT_COUNT == 1


def test_bot_count_garbage_falls_back_to_default():
    """Non-numeric env value → fall back to 6 (don't crash bootstrap)."""
    cfg = _reload_with_env({'BOT_COUNT': 'six'})
    assert cfg.BOT_COUNT == 6


def test_bot_count_empty_string_falls_back():
    cfg = _reload_with_env({'BOT_COUNT': '   '})
    assert cfg.BOT_COUNT == 6


def test_min_usdc_per_bot_float_value():
    """Float values for MIN_USDC_PER_BOT work."""
    cfg = _reload_with_env({'MIN_USDC_PER_BOT': '12.5'})
    assert cfg.MIN_USDC_PER_BOT == 12.5


def test_min_usdc_per_bot_negative_allowed():
    """Negative MIN_USDC_PER_BOT is technically valid (always-accept bot)
    — coordinator will treat any balance as 'eligible'. Don't clamp."""
    cfg = _reload_with_env({'MIN_USDC_PER_BOT': '-1'})
    assert cfg.MIN_USDC_PER_BOT == -1.0


def test_min_usdc_garbage_falls_back():
    cfg = _reload_with_env({'MIN_USDC_PER_BOT': 'cheap'})
    assert cfg.MIN_USDC_PER_BOT == 60.0
