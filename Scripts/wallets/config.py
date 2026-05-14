"""Wallet config + the public Wallet / WalletPool dataclasses.

Wallet is what the executor sees — a thin adapter over whatever store
holds the actual private key. It deliberately doesn't expose `private_key`
as a regular attribute so an accidental log/jsonify of the wallet doesn't
leak it. Use wallet.sign(payload) instead — the store may decline if the
key isn't loaded.
"""
import os
from dataclasses import dataclass, field
from typing import Optional, Callable

# ── Defaults ─────────────────────────────────────────────────────────
# Phase TS-5e (14.05.2026) — env-overridable. Default 6 bots preserves
# the baseline; operator can set BOT_COUNT=1 for single-bot mode (e.g.
# small-deposit pilot before full multi-wallet rollout) or any value
# 1..6. MIN_USDC_PER_BOT similarly env-overridable so a $5/wallet pilot
# isn't auto-skipped by the coordinator.
def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or raw.strip() == '':
        return default
    try:
        n = int(raw.strip())
    except ValueError:
        return default
    # Clamp defensively — BOT_COUNT must be 1..6 (we have 6 hardcoded
    # wallet slots in Credentials.env). 0 would break the coordinator.
    if key == 'BOT_COUNT':
        return max(1, min(6, n))
    return max(0, n)


def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw is None or raw.strip() == '':
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


BOT_COUNT = _env_int('BOT_COUNT', 6)
MIN_USDC_PER_BOT = _env_float('MIN_USDC_PER_BOT', 60.0)  # coordinator skips below this
REBALANCE_LOW_USDC = 60.0         # trigger rebalance when bot has < this
REBALANCE_HIGH_USDC = 200.0       # source bot must have > this
REBALANCE_RESERVE_USDC = 130.0    # leave the source bot with at least this
REBALANCE_PAIR_COOLDOWN_S = 3600  # don't re-rebalance same pair < 1h
ASSIGN_JITTER_MAX_MS = 50         # 0..50ms random delay between fires
                                  # (anti-detection, not enforced in dry-run)


@dataclass
class Wallet:
    """Public wallet view. The actual private key is held only inside the
    store; the wallet exposes sign() which delegates to the store.

    Phase 9f: also carries Polymarket L2 creds (api_key/secret/passphrase)
    and Limitless `api_key`. These are non-secret-by-themselves auth
    tokens — losing them lets an attacker cancel orders / read positions
    but NOT move USDC out of the wallet (private key is needed for that).
    Still, treat as secrets — never log, never serialise to UI.
    """
    bot_id: str                  # 'bot1' .. 'bot6'
    eth_address: str             # 0x...
    store_name: str              # 'local' / 'windows_cred' / 'aws'
    can_sign: bool = False       # True iff the store can produce signatures
    last_known_usdc: float = 0.0 # populated by balance.py
    last_balance_check_unix: float = 0.0

    # Phase 9f auth tokens — optional. None until provisioned.
    poly_api_key: Optional[str] = field(default=None, repr=False)
    poly_secret: Optional[str] = field(default=None, repr=False)
    poly_passphrase: Optional[str] = field(default=None, repr=False)
    api_key: Optional[str] = field(default=None, repr=False)   # Limitless token ID
    # Phase TS-5f.3 (14.05.2026) — Limitless HMAC secret. Used together
    # with `api_key` to sign REST + WS handshake requests. Trading-scope
    # tokens require this; legacy bearer-only X-API-Key 401s.
    api_secret: Optional[str] = field(default=None, repr=False)

    # The actual signing function set by the store on load. The wallet
    # itself never sees the raw key bytes — store keeps them and exposes
    # sign(payload, wallet_id) only.
    _sign_fn: Optional[Callable] = field(default=None, repr=False, compare=False)

    def sign(self, payload: bytes) -> Optional[bytes]:
        if not self.can_sign or not self._sign_fn:
            return None
        return self._sign_fn(self.bot_id, payload)

    @property
    def has_poly_creds(self) -> bool:
        return bool(self.poly_api_key and self.poly_secret
                    and self.poly_passphrase)


@dataclass
class WalletPool:
    """Container — the executor + coordinator import this from a pool
    instance, not raw lists, so we can attach metadata (last rebalance
    time, lock-state, etc.) per pool."""
    wallets: list = field(default_factory=list)
    cold_address: Optional[str] = None     # destination for auto-sweep (Phase 5+)
    backend: str = 'local'

    def __iter__(self):
        return iter(self.wallets)

    def __len__(self):
        return len(self.wallets)

    def by_id(self, bot_id: str) -> Optional[Wallet]:
        for w in self.wallets:
            if w.bot_id == bot_id:
                return w
        return None

    def with_balance_above(self, threshold: float) -> list:
        return [w for w in self.wallets if w.last_known_usdc >= threshold]
