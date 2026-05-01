"""Coordinator: distributes arb legs across the wallet pool.

Two rules from feedback memory:
    1. Anti-detection: ONE leg per bot per arb. Never aggregate multiple
       legs of one arb in one wallet (that's the obvious arb-bot pattern
       Polymarket bans on). With 6 bots and 3-7 legs typical we can
       always satisfy this.
    2. Balance-aware: skip bots with USDC < $60 (can't even fund a typical
       leg). If the eligible pool < legs_count, the arb is rejected.

The coordinator does NOT do real wallet locking yet — Phase 4 ships the
core logic; Phase 5+ adds locks once positions are real.
"""
import logging
import random
from typing import List, Optional, Tuple

from .config import Wallet, WalletPool, MIN_USDC_PER_BOT, ASSIGN_JITTER_MAX_MS

log = logging.getLogger(__name__)


# Phase 17 (01.05.2026) — per-chain wallet pre-filter for cross-platform.
# Bots share eth_address across all EVM chains (Polygon/Base/SX Network)
# because address derivation from private_key is chain-agnostic. So
# "filtering by chain" = filtering by BALANCE on each chain's USDC token.
# Live balance checks happen in preflight; this helper uses cached
# `last_known_*` for fast pre-filter without web3 RPC calls.
def filter_wallets_by_chain(pool: WalletPool, platform: str) -> List[Wallet]:
    """Return wallets that have non-zero last-known balance on the
    platform's chain. Pass-through if pool doesn't track per-chain
    balance yet (current state).

    Args:
        pool: full wallet pool
        platform: 'Polymarket' (Polygon/pUSD), 'Limitless' (Base/USDC),
                  'SX Bet' (SX Network/USDC)

    Returns: subset of pool.wallets eligible for this chain.
    """
    # last_known_usdc is a single number — for now, treat all positive
    # balances as eligible. Future enhancement: per-chain balance dict
    # on Wallet (last_known_usdc_polygon, last_known_usdc_base, etc.).
    # Until then we rely on preflight.check_balance_for_platform live check.
    return [w for w in pool.wallets if (w.last_known_usdc or 0) > 0]


def _eligible(pool: WalletPool, min_usdc: float = None) -> List[Wallet]:
    """Bots with enough USDC to fund a typical leg. The radar's deal-builder
    already sized stakes by min stack across legs, so MIN_USDC_PER_BOT is
    a coarse pre-filter — fine-grained per-leg balance check happens at
    fire time inside the executor (Phase 5).

    Phase 10 Task E (01.05.2026): emit Telegram alert for any bot below
    LOW_BALANCE_THRESHOLD_USD ($30 default). Dedupe per bot per hour so
    a chronically-low bot doesn't spam the channel.
    """
    if min_usdc is None:
        min_usdc = MIN_USDC_PER_BOT
    out = []
    try:
        from notify import alert_low_balance, LOW_BALANCE_THRESHOLD_USD
    except ImportError:
        alert_low_balance = None
        LOW_BALANCE_THRESHOLD_USD = 30.0
    for w in pool.wallets:
        if w.last_known_usdc >= min_usdc:
            out.append(w)
        elif alert_low_balance and w.last_known_usdc < LOW_BALANCE_THRESHOLD_USD:
            try:
                alert_low_balance(w.bot_id, w.eth_address, w.last_known_usdc)
            except Exception as e:
                log.debug("alert_low_balance %s failed: %s", w.bot_id, e)
    return out


def can_fire_pool(pool: WalletPool, legs_count: int,
                  min_usdc_per_bot: float = None) -> Tuple[bool, Optional[str]]:
    """Quick pre-check before fire_arb commits to building leg orders.
    Returns (allowed, reason)."""
    if not pool.wallets:
        # Empty pool — executor's mock-stub path will handle this in dry-run
        return True, None
    eligible = _eligible(pool, min_usdc_per_bot)
    if len(eligible) < legs_count:
        return False, (f'insufficient_eligible_bots: '
                       f'{len(eligible)}/{len(pool.wallets)} have ≥${min_usdc_per_bot or MIN_USDC_PER_BOT:.0f}, '
                       f'arb has {legs_count} legs')
    return True, None


def assign_legs(pool: WalletPool, legs_count: int,
                min_usdc_per_bot: float = None) -> List[Wallet]:
    """Pick `legs_count` wallets — one per leg, distinct (anti-detection),
    only from balance-eligible pool. Rotation is deterministic-ish: we
    sort by lowest balance first so the bots most needing throughput
    aren't starved (and the auto-rebalance thread will redistribute later).

    If the pool is empty (no addresses configured), returns []. The
    caller's executor path will then use its single-mock-stub fallback.
    """
    if not pool.wallets:
        return []
    eligible = _eligible(pool, min_usdc_per_bot)
    if len(eligible) < legs_count:
        log.warning("assign_legs: only %d/%d wallets eligible for %d legs — "
                    "executor should reject via can_fire_pool",
                    len(eligible), len(pool.wallets), legs_count)
        return []
    # Distinct wallets, prefer those with lowest balance to keep the pool
    # balanced (auto-rebalance handles the inverse direction).
    eligible.sort(key=lambda w: w.last_known_usdc)
    return eligible[:legs_count]


def jitter_ms_for_leg(leg_idx: int) -> float:
    """Random delay in milliseconds before firing this leg. Spread the
    parallel fires across 0-50ms so multiple bots don't hit the API at
    the exact same millisecond — anti-detection at the network layer.
    Returns 0 when ASSIGN_JITTER_MAX_MS is 0 (tests)."""
    if ASSIGN_JITTER_MAX_MS <= 0:
        return 0.0
    return random.uniform(0, ASSIGN_JITTER_MAX_MS)
