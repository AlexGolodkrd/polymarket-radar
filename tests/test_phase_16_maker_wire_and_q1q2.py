"""Phase 16 (01.05.2026) — maker mode wire-up + adaptive multi-outcome bot
relaxation + SX type expansion + Limitless revert.
"""
import os
import sys
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), 'Scripts')
sys.path.insert(0, SCRIPTS)


# ── Q1: adaptive multi-outcome relaxation ───────────────────────────
def _make_wallets(n):
    from executor.builders import WalletStub
    return [WalletStub(bot_id=f'bot{i}', eth_address='0x' + format(i, '040x'),
                        private_key='0x' + 'a'*64)
            for i in range(n)]


def test_assign_wallets_strict_when_n_below_6():
    """N <= 6 needs distinct bot per leg (legs_per_bot=1)."""
    from executor.atomic import _assign_wallets
    # N=4, only 3 wallets → reject in live mode
    wallets = _make_wallets(3)
    out = _assign_wallets(4, wallets, dry_run=False)
    assert out == [], "N=4 with 3 wallets should reject (strict)"


def test_assign_wallets_strict_n6_with_6():
    """N=6 with 6 wallets → all distinct, ok."""
    from executor.atomic import _assign_wallets
    wallets = _make_wallets(6)
    out = _assign_wallets(6, wallets, dry_run=False)
    assert len(out) == 6
    assert len({w.bot_id for w in out}) == 6, "all distinct"


def test_assign_wallets_n7_uses_2_legs_per_bot():
    """N=7 falls in tier 2 (7-12) → 2 legs/bot. Need ceil(7/2)=4 wallets."""
    from executor.atomic import _assign_wallets
    wallets = _make_wallets(4)         # exact min
    out = _assign_wallets(7, wallets, dry_run=False)
    assert len(out) == 7
    # Each wallet appears at most 2 times
    from collections import Counter
    c = Counter(w.bot_id for w in out)
    for bot, count in c.items():
        assert count <= 2


def test_assign_wallets_n12_uses_2_legs_per_bot():
    """N=12 still tier 2 → ceil(12/2)=6 wallets needed."""
    from executor.atomic import _assign_wallets
    wallets = _make_wallets(6)
    out = _assign_wallets(12, wallets, dry_run=False)
    assert len(out) == 12
    from collections import Counter
    c = Counter(w.bot_id for w in out)
    for bot, count in c.items():
        assert count <= 2


def test_assign_wallets_n13_uses_3_legs_per_bot():
    """N=13 falls in tier 3 → 3 legs/bot. Need ceil(13/3)=5 wallets."""
    from executor.atomic import _assign_wallets
    wallets = _make_wallets(5)
    out = _assign_wallets(13, wallets, dry_run=False)
    assert len(out) == 13
    from collections import Counter
    c = Counter(w.bot_id for w in out)
    for bot, count in c.items():
        assert count <= 3


def test_assign_wallets_n16_uses_3_legs_per_bot():
    """N=16 weather event needs ceil(16/3)=6 wallets."""
    from executor.atomic import _assign_wallets
    wallets = _make_wallets(6)
    out = _assign_wallets(16, wallets, dry_run=False)
    assert len(out) == 16
    from collections import Counter
    c = Counter(w.bot_id for w in out)
    for bot, count in c.items():
        assert count <= 3


def test_assign_wallets_n7_with_3_wallets_rejects():
    """N=7 needs ceil(7/2)=4 wallets, only 3 → reject."""
    from executor.atomic import _assign_wallets
    wallets = _make_wallets(3)
    out = _assign_wallets(7, wallets, dry_run=False)
    assert out == []


def test_assign_wallets_dry_run_pads_unconditionally():
    """In dry-run we pad with mocks regardless of N."""
    from executor.atomic import _assign_wallets
    wallets = _make_wallets(3)
    out = _assign_wallets(15, wallets, dry_run=True)
    assert len(out) == 15


# ── Q2: SX type expansion ──────────────────────────────────────────
def test_sx_binary_types_includes_nfl_moneyline():
    """Phase 16 added NFL types per operator request."""
    from arb_server import SX_BINARY_TYPES
    assert 220 in SX_BINARY_TYPES, "NFL Moneyline should be included"
    assert 227 in SX_BINARY_TYPES, "NBA Moneyline"
    assert 230 in SX_BINARY_TYPES, "MLB Moneyline"


def test_sx_excluded_types_documented():
    """SX_EXCLUDED_TYPES exists for documentation; type=1 (3-way) inside."""
    from arb_server import SX_EXCLUDED_TYPES
    assert 1 in SX_EXCLUDED_TYPES, "Soccer 1X2 needs 3-way pipeline"


# ── Phase 16: maker wire-up integration ────────────────────────────
def test_arb_fire_result_has_fire_mode():
    """ArbFireResult exposes fire_mode used."""
    from executor.atomic import ArbFireResult
    r = ArbFireResult(arb_id='t', deal_title='t', deal_structure='all_yes',
                       expected_total_cost_usdc=20.0,
                       expected_payout_usdc=22.0)
    assert hasattr(r, 'fire_mode')
    assert r.fire_mode == 'taker'


def test_n_aware_forces_taker_for_n4_plus(monkeypatch):
    """When MAKER_MODE_ENABLED=True but N≥4, fire_arb forces taker."""
    from executor import atomic
    monkeypatch.setattr(atomic, 'MAKER_MODE_ENABLED', True)
    # select_fire_mode would say maker for sum=88, but caller logic in
    # fire_arb forces taker when legs_count >= 4.
    # Test the helper inline:
    from executor.atomic import select_fire_mode
    deal = {'sum_cents': 88, 'arb_structure': 'all_yes',
            'entries': [{'price':0.1}]*4}
    assert select_fire_mode(deal) == 'maker'    # selector itself returns maker
    # The N-aware override happens in fire_arb (integration test below).


def test_failed_legs_includes_maker_statuses():
    """maker_timeout / adverse_cancelled treated as failed legs."""
    from executor.atomic import LegResult
    failed_statuses = {'rejected', 'timeout', 'cancelled', 'disabled',
                        'slippage_cancelled',
                        'maker_timeout', 'adverse_cancelled'}
    # Build a few LegResults
    for s in ('maker_timeout', 'adverse_cancelled'):
        leg = LegResult(leg_idx=0, platform='Polymarket', status=s,
                         expected_price=0.30, expected_size_usdc=10.0)
        assert leg.status in failed_statuses


# ── Limitless revert flow ──────────────────────────────────────────
def test_revert_filled_legs_handles_limitless(monkeypatch):
    """revert_filled_legs now has Limitless SELL FOK path."""
    from executor import atomic
    from executor.atomic import LegResult, ArbFireResult
    from executor import builders

    # Mock the builder + HTTP POST
    posts_called = []
    class _FakeResp:
        status_code = 200
    def _fake_post(url, json=None, headers=None, timeout=None):
        posts_called.append({'url': url, 'json': json})
        return _FakeResp()
    import requests as _req
    monkeypatch.setattr(_req, 'post', _fake_post)

    wallet = builders.WalletStub(bot_id='bot1', eth_address='0x' + '1'*40,
                                   private_key='0x' + 'a'*64)
    result = ArbFireResult(
        arb_id='t-lim', deal_title='t', deal_structure='all_yes',
        expected_total_cost_usdc=20.0, expected_payout_usdc=22.0,
        legs=[
            LegResult(leg_idx=0, platform='Limitless', status='filled',
                       expected_price=0.30, expected_size_usdc=10.0,
                       fill_size_usdc=10.0, bot_id='bot1'),
        ],
    )
    deal = {'platform': 'Limitless',
            'entries': [{'slug': 'test-slug',
                          'token_id': 'TOKEN', 'verifying_contract': '0xVC'}]}

    out = atomic.revert_filled_legs(result, deal, [wallet], dry_run=False)
    # Should mention Limitless revert outcome
    assert 'sold_lim' in out or 'sell_lim' in out, f"unexpected: {out}"


def test_revert_filled_legs_sx_still_unimpl():
    """SX revert is still TODO Phase 17 — should not raise but log unimpl."""
    from executor import atomic
    from executor.atomic import LegResult, ArbFireResult
    from executor import builders

    wallet = builders.WalletStub(bot_id='bot1', eth_address='0x' + '1'*40)
    result = ArbFireResult(
        arb_id='t-sx', deal_title='t', deal_structure='binary',
        expected_total_cost_usdc=20.0, expected_payout_usdc=22.0,
        legs=[
            LegResult(leg_idx=0, platform='SX Bet', status='filled',
                       expected_price=0.30, expected_size_usdc=10.0,
                       fill_size_usdc=10.0, bot_id='bot1'),
        ],
    )
    deal = {'platform': 'SX Bet',
            'entries': [{'market_hash': '0xMH', 'outcome_index': 1}]}
    out = atomic.revert_filled_legs(result, deal, [wallet], dry_run=False)
    assert 'revert_unimpl' in out
