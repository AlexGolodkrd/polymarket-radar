"""Wallet management for the executor (Phase 4 — PR #15).

Provisions a pool of 6 bot wallets, each backed by a private key from a
pluggable storage backend. The coordinator distributes arb legs across
bots with anti-detection (one leg per bot per arb), balance-awareness
(skip bots with USDC < $60), and auto-rebalance (transfer USDC from
bots with > $200 to those < $60 when both are idle).

Module split:
    config.py      — defaults (BOT_COUNT=6, MIN_USDC_PER_BOT=60, etc.) and
                     the public Wallet dataclass.
    stores.py      — pluggable backends: LocalEnvStore (.env / dev),
                     WindowsCredStore (OS keystore), AwsSecretsStore
                     (boto3, prod). The store loads addresses + signs
                     orders without exposing the private key to the
                     application layer.
    coordinator.py — assign_legs(deal) round-robin + balance filter +
                     anti-detection. fire-time wallet picker.
    rebalance.py   — auto_rebalance(): scan balances, find (low, high)
                     pairs, transfer USDC on-chain. Per-pair 1h cooldown.
    balance.py     — RPC USDC balance check per bot (Polygon/USDC contract).

Phase 4 ships WITHOUT real keys. Wallet addresses go into Credentials.env
when the user is ready (`BOT1_ETH_ADDRESS=0x...`); private keys stay
empty until graduation gate passes (`BOT1_PRIVATE_KEY=` blank).
With empty keys the executor stays in dry-run regardless of DRY_RUN
env var — the wallet objects exist but `wallet.can_sign` is False.

Anti-detection rule (memory feedback): NEVER aggregate multiple legs of
one arb in one wallet. Coordinator enforces this; if pool < legs we
gracefully fall through to round-robin (some bots take 2 legs, but
across DIFFERENT arbs).

Auto-rebalance rule (memory feedback): when bot N drops below $60 and
bot M has > $200, coordinator initiates an on-chain transfer from M to N
(half the excess, leaving M ~ $130). Only fires when both wallets are
idle (no open positions) — locking is handled via reconciliation state.
"""
from .config import (
    BOT_COUNT, MIN_USDC_PER_BOT, REBALANCE_HIGH_USDC, REBALANCE_LOW_USDC,
    REBALANCE_RESERVE_USDC, REBALANCE_PAIR_COOLDOWN_S,
    Wallet, WalletPool,
)
from .stores import (
    Store, LocalEnvStore, WindowsCredStore, AwsSecretsStore, load_pool,
)
from .coordinator import assign_legs, can_fire_pool
from .rebalance import (
    auto_rebalance_check, propose_rebalances, rebalance_history,
)

__all__ = [
    'BOT_COUNT', 'MIN_USDC_PER_BOT',
    'REBALANCE_HIGH_USDC', 'REBALANCE_LOW_USDC',
    'REBALANCE_RESERVE_USDC', 'REBALANCE_PAIR_COOLDOWN_S',
    'Wallet', 'WalletPool',
    'Store', 'LocalEnvStore', 'WindowsCredStore', 'AwsSecretsStore', 'load_pool',
    'assign_legs', 'can_fire_pool',
    'auto_rebalance_check', 'propose_rebalances', 'rebalance_history',
]
