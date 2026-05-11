"""Auto-rebalance USDC across the bot pool.

Memory feedback (27.04.2026): "если на одном боте заканчивается маржа,
то с тех которые в этот момент зарабатывали, она должна перекидываться".

Algorithm (per scan, every 60-300s in production):
    1. For each bot, fetch USDC balance via Polygon RPC (Phase 4 = stub
       returns last_known_usdc; Phase 6 connects real RPC).
    2. Find pairs (low, high) where low.usdc < REBALANCE_LOW_USDC ($60)
       and high.usdc > REBALANCE_HIGH_USDC ($200).
    3. For each pair, propose a transfer of
       (high.usdc - REBALANCE_RESERVE_USDC) / 2 USDC from high to low.
    4. Skip pairs that rebalanced within the last cooldown window
       (REBALANCE_PAIR_COOLDOWN_S = 1h) — guards against thrashing.
5. Skip if either wallet has open positions (Phase 5 wires this; Phase 4
   marks the lock check as a TODO).

Phase 4 ships the proposal/cooldown logic + history log. Actual on-chain
USDC.transfer() is a thin wrapper that the user fills in once keys land
(commented at the bottom). The rebalance loop is OPT-IN — radar must
explicitly call start_rebalance_loop() once the operator is comfortable.
"""
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from .config import (
    REBALANCE_LOW_USDC, REBALANCE_HIGH_USDC,
    REBALANCE_RESERVE_USDC, REBALANCE_PAIR_COOLDOWN_S,
    WalletPool,
)

log = logging.getLogger(__name__)

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_THIS_DIR, '..', '..'))
EXECUTIONS_DIR = os.path.join(_REPO_ROOT, 'Executions')
REBALANCE_LOG = os.path.join(EXECUTIONS_DIR, 'rebalance.jsonl')

_lock = threading.Lock()
_pair_last_rebalance: dict = {}     # ('botA','botB') -> ts


@dataclass
class RebalanceProposal:
    from_bot: str
    to_bot: str
    amount_usdc: float
    reason: str
    proposed_at: float = field(default_factory=time.time)
    executed: bool = False
    tx_hash: Optional[str] = None
    error: Optional[str] = None


def _pair_key(a: str, b: str) -> tuple:
    return tuple(sorted([a, b]))


def _on_cooldown(a: str, b: str, now: float) -> bool:
    last = _pair_last_rebalance.get(_pair_key(a, b))
    return last is not None and (now - last) < REBALANCE_PAIR_COOLDOWN_S


def propose_rebalances(pool: WalletPool,
                       low_threshold: float = None,
                       high_threshold: float = None,
                       reserve: float = None) -> list:
    """Pure function — no I/O, no transfers. Given the pool's current
    last_known_usdc per wallet, return a list of RebalanceProposal'ы that
    would balance the pool. Caller decides whether to execute.

    Greedy pairing: each high bot is matched to one low bot at a time,
    proportional transfer amount. Conservative — prefers small transfers
    over large ones to keep gas predictable.
    """
    low_threshold = low_threshold if low_threshold is not None else REBALANCE_LOW_USDC
    high_threshold = high_threshold if high_threshold is not None else REBALANCE_HIGH_USDC
    reserve = reserve if reserve is not None else REBALANCE_RESERVE_USDC

    now = time.time()
    proposals = []
    if not pool.wallets:
        return proposals

    # Phase 19v16 — local shadow deltas instead of mutating wallet objects.
    # `propose_rebalances` runs in dry-run / planning mode; only
    # `_execute_transfer` should ever change canonical balances. Shadow
    # is local to this call so the next call starts fresh from real state.
    shadow_delta: dict = {}

    def _bal(w):
        return w.last_known_usdc + shadow_delta.get(w.bot_id, 0)

    lows = [w for w in pool.wallets if _bal(w) < low_threshold]
    highs = [w for w in pool.wallets if _bal(w) > high_threshold]
    if not lows or not highs:
        return proposals

    # Sort: most-needy low first, richest high first
    lows.sort(key=lambda w: _bal(w))
    highs.sort(key=lambda w: -_bal(w))

    for low in lows:
        # Find a high not yet paired to this low recently
        for high in highs:
            if low.bot_id == high.bot_id:
                continue
            if _on_cooldown(low.bot_id, high.bot_id, now):
                continue
            high_bal = _bal(high)
            low_bal = _bal(low)
            if high_bal <= reserve:
                # Already drained below reserve — can't take more from this bot
                continue
            transferable = high_bal - reserve
            # Half the excess so the source still has runway, but at least
            # enough to bring `low` to 1.5x the threshold (so we don't
            # immediately re-trigger).
            target_to_low = max(low_threshold * 1.5 - low_bal, 0)
            amount = min(transferable / 2, target_to_low)
            if amount < 5.0:
                # Skip dust transfers — gas isn't worth it
                continue
            proposals.append(RebalanceProposal(
                from_bot=high.bot_id, to_bot=low.bot_id,
                amount_usdc=round(amount, 2),
                reason=(f'low={low.bot_id}@${low_bal:.2f} '
                        f'< ${low_threshold:.0f}, '
                        f'high={high.bot_id}@${high_bal:.2f}'),
            ))
            # Phase 19v16 — shadow-only mutation for pairing math.
            shadow_delta[high.bot_id] = shadow_delta.get(high.bot_id, 0) - amount
            shadow_delta[low.bot_id] = shadow_delta.get(low.bot_id, 0) + amount
            break
    return proposals


def _append_log(row: dict):
    os.makedirs(EXECUTIONS_DIR, exist_ok=True)
    row = {**row, 'ts': time.time()}
    with open(REBALANCE_LOG, 'a', encoding='utf-8') as f:
        f.write(json.dumps(row, default=str) + '\n')


def _execute_transfer(proposal: RebalanceProposal, pool: WalletPool) -> bool:
    """Phase 4 stub — logs intent and marks the cooldown. Phase 6 plugs in
    the real USDC.transfer() call via a Polygon RPC + signed transaction.

    Why not implement now: real on-chain transfers require:
      - eth-account / web3.py
      - the source wallet's private key (gated on can_sign)
      - a Polygon RPC URL (env POLYGON_RPC_URL)
      - gas estimation
    None of that is available in Phase 4 by design — keys land first via
    LocalEnvStore, RPC URL configured in Phase 6 alongside Docker.
    """
    src = pool.by_id(proposal.from_bot)
    if src is None or not src.can_sign:
        proposal.error = 'source_wallet_no_signing_key (Phase 4 default)'
        _append_log({'event': 'proposal', 'executed': False,
                     'reason': proposal.error, **proposal.__dict__})
        return False
    # Phase 6 implementation outline — kept here as comment so the path is visible:
    #
    # from web3 import Web3
    # from .stores import _get_store_for(src.store_name)
    # w3 = Web3(Web3.HTTPProvider(os.environ['POLYGON_RPC_URL']))
    # usdc_contract = w3.eth.contract(address=USDC_ADDR_POLYGON, abi=USDC_ABI)
    # dst = pool.by_id(proposal.to_bot).eth_address
    # amount_wei = int(proposal.amount_usdc * 1e6)
    # tx = usdc_contract.functions.transfer(dst, amount_wei).build_transaction({...})
    # signed = src.sign(tx)
    # tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction).hex()
    # proposal.tx_hash = tx_hash
    # ...
    proposal.error = 'real_transfer_disabled_phase_4'
    _append_log({'event': 'proposal', 'executed': False,
                 'note': 'Phase 4 dry-run — transfer NOT executed',
                 **proposal.__dict__})
    return False


def auto_rebalance_check(pool: WalletPool, execute: bool = False) -> list:
    """One pass: refresh balances (Phase 4 stub uses last_known_usdc as-is),
    propose, optionally execute. Returns the proposal list (executed or
    not) so callers can log/show in UI.

    `execute=False` (default) is a dry-run — proposals are computed and
    logged, but no transfers happen. Phase 6 may flip the default once
    the operator is comfortable.
    """
    proposals = propose_rebalances(pool)
    if not proposals:
        return []

    log.info("auto_rebalance_check: %d proposals", len(proposals))
    for p in proposals:
        if execute:
            ok = _execute_transfer(p, pool)
            p.executed = ok
            if ok:
                with _lock:
                    _pair_last_rebalance[_pair_key(p.from_bot, p.to_bot)] = time.time()
        else:
            _append_log({'event': 'proposal_dryrun', **p.__dict__})
    return proposals


def rebalance_history(limit: int = 50) -> list:
    """Read recent rebalance log lines for the dashboard panel."""
    if not os.path.exists(REBALANCE_LOG):
        return []
    rows = []
    with open(REBALANCE_LOG, 'r', encoding='utf-8') as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows[-limit:]
