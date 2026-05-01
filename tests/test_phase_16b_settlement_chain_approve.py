"""Phase 16+ (01.05.2026) — settlement timing + per-chain balance check
+ limitless_approve.py CTF approveForAll.
"""
import os
import sys
from datetime import datetime, timezone, timedelta

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), 'Scripts')
sys.path.insert(0, SCRIPTS)


# ── Settlement timing ───────────────────────────────────────────────
def test_settlement_timing_same_day_ok():
    from cross_platform import _check_settlement_timing, PlatformOutcome
    a = PlatformOutcome(
        platform='Polymarket', event_id='e1', outcome_name='YES',
        yes_price=0.40, yes_depth=100, yes_source='clob_ask',
        no_price=0.55, no_depth=100, no_source='clob_ask',
        end_date='2026-03-25T22:00:00Z', title='Lakers vs Celtics')
    b = PlatformOutcome(
        platform='Limitless', event_id='e1', outcome_name='YES',
        yes_price=0.42, yes_depth=100, yes_source='lim_clob',
        no_price=0.55, no_depth=100, no_source='lim_clob',
        end_date='2026-03-25T22:30:00Z', title='Lakers vs Celtics')
    ok, reason = _check_settlement_timing(a, b)
    assert ok
    assert 'OK' in reason


def test_settlement_timing_24h_diff_rejects():
    """36h apart > default 24h tolerance → reject."""
    from cross_platform import _check_settlement_timing, PlatformOutcome
    a = PlatformOutcome(
        platform='Polymarket', event_id='e1', outcome_name='YES',
        yes_price=0.40, yes_depth=100, yes_source='clob_ask',
        no_price=0.55, no_depth=100, no_source='clob_ask',
        end_date='2026-03-25T22:00:00Z', title='X')
    b = PlatformOutcome(
        platform='Limitless', event_id='e1', outcome_name='YES',
        yes_price=0.40, yes_depth=100, yes_source='lim_clob',
        no_price=0.55, no_depth=100, no_source='lim_clob',
        end_date='2026-03-27T10:00:00Z', title='X')   # 36h later
    ok, reason = _check_settlement_timing(a, b)
    assert not ok
    assert 'settlement_delta' in reason


def test_settlement_timing_missing_dates_passes_best_effort():
    """If end_date missing on either side → best-effort accept."""
    from cross_platform import _check_settlement_timing, PlatformOutcome
    a = PlatformOutcome(
        platform='Polymarket', event_id='e1', outcome_name='YES',
        yes_price=0.40, yes_depth=100, yes_source='clob_ask',
        no_price=0.55, no_depth=100, no_source='clob_ask',
        end_date=None, title='X')
    b = PlatformOutcome(
        platform='Limitless', event_id='e1', outcome_name='YES',
        yes_price=0.40, yes_depth=100, yes_source='lim_clob',
        no_price=0.55, no_depth=100, no_source='lim_clob',
        end_date=None, title='X')
    ok, reason = _check_settlement_timing(a, b)
    assert ok                    # best-effort
    assert reason == 'missing_end_date'


# ── find_cross_platform_arbs respects settlement timing ─────────────
def test_find_arbs_rejects_settlement_mismatch():
    from cross_platform import find_cross_platform_arbs, PlatformOutcome
    pool_a = [PlatformOutcome(
        platform='Polymarket', event_id='e1', outcome_name='Lakers',
        yes_price=0.40, yes_depth=100, yes_source='clob_ask',
        no_price=0.55, no_depth=100, no_source='clob_ask',
        end_date='2026-03-25T22:00:00Z',
        title='Lakers vs Celtics Mar 25')]
    pool_b = [PlatformOutcome(
        platform='Limitless', event_id='e1b', outcome_name='Lakers',
        yes_price=0.40, yes_depth=100, yes_source='lim_clob',
        no_price=0.55, no_depth=100, no_source='lim_clob',
        end_date='2026-03-30T22:00:00Z',          # 5 days later — reject
        title='Lakers vs Celtics Mar 25')]
    deals = find_cross_platform_arbs(pool_a, pool_b, min_confidence=0.50)
    assert deals == []                     # settlement mismatch → no arbs


# ── Per-chain balance helper ────────────────────────────────────────
def test_check_balance_for_platform_unknown_skips():
    from preflight import check_balance_for_platform
    ok, bal, reason = check_balance_for_platform(
        '0x' + '0' * 40, 50.0, platform='Kalshi')
    assert ok                    # unknown platform → skip with reason
    assert 'no per-chain config' in reason


def test_per_chain_config_has_3_platforms():
    """PER_CHAIN_CONFIG covers Polymarket / Limitless / SX Bet."""
    from preflight import PER_CHAIN_CONFIG
    assert 'Polymarket' in PER_CHAIN_CONFIG
    assert 'Limitless' in PER_CHAIN_CONFIG
    assert 'SX Bet' in PER_CHAIN_CONFIG
    # Each tuple: (rpc_url, usdc_addr, exchange_addr_or_None)
    for plat, cfg in PER_CHAIN_CONFIG.items():
        assert len(cfg) == 3


# ── limitless_approve.py CTF approval ───────────────────────────────
def test_limitless_approve_has_ctf_function():
    """Verify presence of _ensure_ctf_approval via source inspection
    (web3 may not be installed in test env)."""
    import os
    path = os.path.join(SCRIPTS, 'limitless_approve.py')
    with open(path, 'r', encoding='utf-8') as f:
        src = f.read()
    assert '_ensure_ctf_approval' in src
    assert 'setApprovalForAll' in src
    assert 'CTF_APPROVE_ABI' in src
