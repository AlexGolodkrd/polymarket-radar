"""Wallet config + the public Wallet / WalletPool dataclasses.

Wallet is what the executor sees — a thin adapter over whatever store
holds the actual private key. It deliberately doesn't expose `private_key`
as a regular attribute so an accidental log/jsonify of the wallet doesn't
leak it. Use wallet.sign(payload) instead — the store may decline if the
key isn't loaded.
"""
from dataclasses import dataclass, field
from typing import Optional, Callable

# ── Defaults (memory feedback: 6 bots, anti-detection, auto-rebalance) ─
BOT_COUNT = 6
MIN_USDC_PER_BOT = 60.0           # below this — skip in coordinator
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
    api_key: Optional[str] = field(default=None, repr=False)   # Limitless

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
