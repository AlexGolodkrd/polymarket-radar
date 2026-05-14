"""Pluggable wallet storage backends.

Each backend implements the `Store` protocol:
    addresses() -> dict[bot_id, eth_address]   — always works (just config)
    has_key(bot_id) -> bool                     — True iff key is loaded
    sign(bot_id, payload) -> Optional[bytes]   — None if no key

Phase 4 ships LocalEnvStore (reads .env) as the default. The other two
are skeletons that follow the same interface — Phase 6 (VPS) will fill
them in once the user provisions keys via AWS Secrets Manager or the
Windows credential vault.

CRITICAL: stores are the ONLY place that touches private keys. They must
NEVER log a key, NEVER include keys in repr/dict/jsonify, and the sign()
implementation should not pass the raw bytes back to the caller — it
returns the signature only.
"""
import logging
import os
import threading
from typing import Optional, Protocol

from .config import Wallet, WalletPool, BOT_COUNT

log = logging.getLogger(__name__)


class Store(Protocol):
    name: str
    def addresses(self) -> dict: ...
    def has_key(self, bot_id: str) -> bool: ...
    def sign(self, bot_id: str, payload: bytes) -> Optional[bytes]: ...


# ── LocalEnvStore (default for dev) ─────────────────────────────────
class LocalEnvStore:
    """Reads BOT{N}_ETH_ADDRESS / BOT{N}_PRIVATE_KEY from environment.

    Looks at: process env, then Credentials.env in repo root, then
    .env (alias). Doesn't add keys to env — only reads.

    Phase 4 default: the user populates Credentials.env with the 6
    addresses. Private keys stay blank until the user is ready (Phase 5
    graduation gate must pass before keys go live)."""
    name = 'local'

    def __init__(self, env_path: Optional[str] = None):
        self._lock = threading.Lock()
        self._cache_addresses: Optional[dict] = None
        self._cache_keys: dict = {}
        self._env_path = env_path or self._find_env()

    @staticmethod
    def _find_env() -> Optional[str]:
        """Walk up the directory tree from this file looking for
        Credentials.env or .env. Handles worktree layouts where the file
        sits at the OUTER project root, several levels above Scripts/."""
        here = os.path.dirname(os.path.abspath(__file__))
        current = here
        for _ in range(8):                           # cap walk at 8 levels
            for candidate in ['Credentials.env', '.env']:
                p = os.path.join(current, candidate)
                if os.path.exists(p):
                    return p
            parent = os.path.dirname(current)
            if parent == current:                    # filesystem root
                break
            current = parent
        return None

    def _load_env_file(self) -> dict:
        if not self._env_path or not os.path.exists(self._env_path):
            return {}
        out = {}
        with open(self._env_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                # Phase 19v14 (05.05.2026) — strip inline `#`-comments. A
                # line `BOT1_PRIVATE_KEY=0xabc... # rotated 2026-04-30`
                # otherwise stored the comment as part of the key, which
                # corrupts EIP-712 signing on first use. Only strip on `#`
                # NOT inside a quoted value.
                v = v.strip()
                if v and v[0] in ('"', "'"):
                    # Quoted value: only strip outer quotes, leave any `#`
                    # inside the quotes alone (some passphrases contain `#`).
                    quote = v[0]
                    end = v.find(quote, 1)
                    if end > 0:
                        v = v[1:end]
                else:
                    # Unquoted: split on first `#` (must be preceded by space
                    # to avoid clipping legitimate `#` in opaque tokens).
                    if ' #' in v:
                        v = v.split(' #', 1)[0]
                    elif '\t#' in v:
                        v = v.split('\t#', 1)[0]
                    v = v.strip()
                out[k.strip()] = v
        return out

    def _read(self, key: str) -> Optional[str]:
        # process env wins, then file
        v = os.environ.get(key)
        if v:
            return v
        if self._cache_addresses is None or self._cache_keys is None:
            pass
        env = self._load_env_file()
        return env.get(key)

    def addresses(self) -> dict:
        with self._lock:
            if self._cache_addresses is not None:
                return dict(self._cache_addresses)
            out = {}
            for i in range(1, BOT_COUNT + 1):
                bot_id = f'bot{i}'
                addr = self._read(f'BOT{i}_ETH_ADDRESS')
                if addr:
                    out[bot_id] = addr
            self._cache_addresses = out
            return dict(out)

    def has_key(self, bot_id: str) -> bool:
        with self._lock:
            if bot_id in self._cache_keys:
                return self._cache_keys[bot_id] is not None
            i = int(bot_id.replace('bot', ''))
            k = self._read(f'BOT{i}_PRIVATE_KEY')
            self._cache_keys[bot_id] = k or None
            return bool(k)

    def sign(self, bot_id: str, payload: bytes) -> Optional[bytes]:
        """Real signing requires `eth-account`. We import it lazily and
        return None if the dependency isn't available — Phase 4 keeps the
        executor working in dry-run even before the user installs it."""
        if not self.has_key(bot_id):
            return None
        try:
            from eth_account.messages import encode_defunct
            from eth_account import Account
        except ImportError:
            log.warning("eth-account not installed — sign() returns None. "
                        "Add `eth-account` to requirements.txt before going live.")
            return None
        with self._lock:
            key = self._cache_keys.get(bot_id)
        if not key:
            return None
        msg = encode_defunct(payload)
        sig = Account.sign_message(msg, private_key=key)
        return sig.signature


# ── WindowsCredStore (skeleton) ────────────────────────────────────
class WindowsCredStore:
    """Reads keys from Windows Credential Manager via pywin32.
    Skeleton — Phase 6 fills in. Keeps the same interface so swapping
    backends in load_pool() is a one-line config change.
    """
    name = 'windows_cred'

    def __init__(self):
        self._available = False
        try:
            import win32cred  # noqa: F401
            self._available = True
        except ImportError:
            log.info("pywin32 not installed — WindowsCredStore disabled")

    def addresses(self) -> dict:
        # Phase 6: enumerate generic creds named "plan-kapkan/bot{N}/address"
        return {}

    def has_key(self, bot_id: str) -> bool:
        return False

    def sign(self, bot_id: str, payload: bytes) -> Optional[bytes]:
        return None


# ── AwsSecretsStore (skeleton) ─────────────────────────────────────
class AwsSecretsStore:
    """Pulls keys from AWS Secrets Manager via boto3. Used on the VPS
    deployment (Phase 6). IAM role on the EC2/Fargate task grants
    GetSecretValue on `plan-kapkan/bot{N}` secrets only."""
    name = 'aws'

    def __init__(self, region_name: Optional[str] = None,
                 secret_prefix: str = 'plan-kapkan/'):
        self._region = region_name or os.environ.get('AWS_REGION') or 'us-east-2'
        self._prefix = secret_prefix
        self._client = None

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            import boto3
            self._client = boto3.client('secretsmanager', region_name=self._region)
        except ImportError:
            log.info("boto3 not installed — AwsSecretsStore disabled")
            return None
        return self._client

    def addresses(self) -> dict:
        # Phase 6: list secrets with prefix, parse JSON values
        return {}

    def has_key(self, bot_id: str) -> bool:
        return False

    def sign(self, bot_id: str, payload: bytes) -> Optional[bytes]:
        return None


# ── load_pool — public entry point ──────────────────────────────────
def load_pool(backend: str = None, cold_address: Optional[str] = None) -> WalletPool:
    """Build a WalletPool from the named backend (default = WALLET_BACKEND env
    var or 'local'). If the store has no addresses (e.g. nobody filled
    Credentials.env yet) we return an empty pool — the executor falls back
    to its mock single-stub path and the radar still runs in dry-run.

    `cold_address` is the destination for auto-sweep (Phase 5+). Read
    from COLD_WALLET_ADDRESS env if not passed.
    """
    backend = backend or os.environ.get('WALLET_BACKEND', 'local')
    if backend == 'local':
        store = LocalEnvStore()
    elif backend == 'windows_cred':
        store = WindowsCredStore()
    elif backend == 'aws':
        store = AwsSecretsStore()
    else:
        log.warning("unknown WALLET_BACKEND=%r — falling back to local", backend)
        store = LocalEnvStore()

    addrs = store.addresses()
    # Phase 19v19 (05.05.2026) — guard against silent fallback. If
    # operator set WALLET_BACKEND=aws / windows_cred and it returns
    # NO addresses, refuse to silently downgrade to mock-stub when
    # `DRY_RUN=0` (real-mode). Old behavior: empty addresses → empty
    # pool → executor's "single-mock-stub" path → every "real" fire
    # was actually fake. Operator could lose hours thinking they were
    # trading live. Hard-fail when intent + reality diverge.
    if (not addrs) and backend != 'local':
        dry_run_default = os.environ.get('DRY_RUN', '1')
        if dry_run_default == '0':
            raise RuntimeError(
                f"WALLET_BACKEND={backend} returned 0 addresses but "
                f"DRY_RUN=0 (real mode requested). Refusing to silently "
                f"fall back to mock stub. Check that the backend is "
                f"actually configured (e.g. boto3 installed + IAM role "
                f"set up for AWS), or set DRY_RUN=1 explicitly."
            )
    wallets = []
    for bot_id, addr in sorted(addrs.items()):
        i = int(bot_id.replace('bot', ''))
        # Phase 9f: read Polymarket L2 creds + Limitless api_key from env
        # via the store's _read (LocalEnvStore reads Credentials.env;
        # other stores can override). Missing creds → None, leaves the
        # auth-only paths gated. Reads are cheap (cached after first hit).
        read = getattr(store, '_read', lambda _k: None)
        w = Wallet(
            bot_id=bot_id, eth_address=addr,
            store_name=store.name,
            can_sign=store.has_key(bot_id),
            poly_api_key=read(f'BOT{i}_POLY_API_KEY') or None,
            poly_secret=read(f'BOT{i}_POLY_SECRET') or None,
            poly_passphrase=read(f'BOT{i}_POLY_PASSPHRASE') or None,
            api_key=read(f'BOT{i}_LIMITLESS_API_KEY')
                    or os.environ.get('LIMITLESS_API_KEY') or None,
            # Phase TS-5f.3 — Limitless HMAC secret. Per-bot
            # BOT{i}_LIMITLESS_API_SECRET takes priority, falls back to
            # global LIMITLESS_API_SECRET (single-bot pilot mode).
            api_secret=read(f'BOT{i}_LIMITLESS_API_SECRET')
                       or os.environ.get('LIMITLESS_API_SECRET') or None,
        )
        # Bind sign function — closure over store keeps the raw key inside
        w._sign_fn = store.sign
        wallets.append(w)

    if not wallets:
        log.info("no wallet addresses configured (backend=%s) — "
                 "executor will run with mock single-stub", backend)

    # cold_address: explicit arg → env → Credentials.env. The store's _read
    # already handles env+file fallback for the local backend.
    cold = cold_address or os.environ.get('COLD_WALLET_ADDRESS')
    if not cold and isinstance(store, LocalEnvStore):
        cold = store._read('COLD_WALLET_ADDRESS')
    return WalletPool(wallets=wallets, cold_address=cold, backend=backend)
