"""preflight.py — pre-fire safety checks for the executor.

Runs BEFORE atomic.fire_arb actually emits any orders. Checks:

  1. **pUSD balance per leg** — does the assigned wallet have enough pUSD
     to cover its leg stake? (Polymarket V2 collateral)
  2. **Allowance per leg** — is the per-wallet pUSD allowance to the V2
     CTF Exchange contract still active? Polygon allowance can be reset
     by user or contract upgrade.
  3. **Top-of-book depth** — is the planned `stake` <= top-of-book
     `liquidity` for each leg? After Phase 10 #51 fix the `liquidity`
     field already reflects top-of-book, so this becomes a simple compare.

Flow per leg:
    1. Read entry['stake'], entry['liquidity'], wallet.eth_address
    2. (optional) Read pUSD balance + allowance via web3 — cached 30s
    3. Return list of failures with reasons; empty = all good.

If web3 is unavailable, balance/allowance checks return PASS-by-default
with a warning row — keeps the radar working in dry-run without on-chain
deps. Real-mode users will install web3 + set POLYGON_RPC_URL.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import List, Optional

log = logging.getLogger(__name__)

# pUSD contract on Polygon (matches polymarket_approve.py)
PUSD_ADDRESS = os.environ.get(
    'POLY_PUSD_ADDRESS',
    '0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB',
)
EXCHANGE_STANDARD = os.environ.get(
    'POLY_EXCHANGE_STANDARD',
    '0xE111180000d2663C0091e4f400237545B87B996B',
)
EXCHANGE_NEGRISK = os.environ.get(
    'POLY_EXCHANGE_NEGRISK',
    '0xe2222d279d744050d28e00520010520000310F59',
)
POLYGON_RPC = os.environ.get('POLYGON_RPC_URL', 'https://polygon-rpc.com')

# Phase 16+ (01.05.2026) — per-chain RPC + USDC contracts for cross-platform.
# Each platform needs balance on its own chain:
#   Polymarket → Polygon → pUSD (above)
#   Limitless  → Base    → USDC.e
#   SX Bet     → SX Network → USDC
# Operator can override each via env. Defaults are public RPC; for production
# use Alchemy / QuickNode / Infura.
BASE_RPC = os.environ.get('BASE_RPC_URL', 'https://mainnet.base.org')
BASE_USDC = os.environ.get(
    'LIMITLESS_USDC_ADDRESS', '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913')
LIMITLESS_EXCHANGE = os.environ.get(
    'LIMITLESS_EXCHANGE_ADDRESS', '0xC5d563A36AE78145C45a50134d48A1215220f80a')

SX_RPC = os.environ.get('SX_RPC_URL', 'https://rpc.sx.technology')
SX_USDC = os.environ.get(
    'SX_USDC_ADDRESS', '0xe2aa35C2039Bd0Ff196A6Ef99523CC0D3972ae3e')

# Per-chain config map: platform → (rpc_url, usdc_addr, exchange_addr)
PER_CHAIN_CONFIG = {
    'Polymarket': (POLYGON_RPC, PUSD_ADDRESS, EXCHANGE_STANDARD),
    'Limitless':  (BASE_RPC, BASE_USDC, LIMITLESS_EXCHANGE),
    'SX Bet':     (SX_RPC, SX_USDC, None),       # SX uses different mechanic
}

ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "owner", "type": "address"}],
     "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"constant": True, "inputs": [{"name": "owner", "type": "address"},
                                    {"name": "spender", "type": "address"}],
     "name": "allowance", "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
]

# In-memory cache: (address, kind) → (value, ts). 30s TTL keeps RPC load down.
# Phase 19v19 (05.05.2026) — split TTL: balance changes on every fill, so
# stale balance reads risk over-firing the wallet. Allowance only changes
# on operator action (re-approve, cancel approve) — once-an-hour or so.
# Old uniform 30s TTL: two fires within 30s on same wallet → second fire
# read pre-fire balance → thought it had $50 → on-chain insufficient-funds.
_BAL_CACHE: dict = {}
_BAL_TTL_S = 2.0           # balance — short, must reflect recent fills
_ALLOWANCE_TTL_S = 600.0   # allowance — long, rarely changes


def _ttl_for_kind(kind: str) -> float:
    return _BAL_TTL_S if kind == 'balance' else _ALLOWANCE_TTL_S


def invalidate_balance_cache(address: str = None):
    """Phase 19v19 — call this from atomic.py right after every successful
    fire so the next preflight reads a fresh balance."""
    if address:
        addr = address.lower()
        for k in list(_BAL_CACHE.keys()):
            if isinstance(k, tuple) and len(k) >= 1 and k[0] == addr and k[1] == 'balance':
                _BAL_CACHE.pop(k, None)
    else:
        # Wipe all balance entries
        for k in list(_BAL_CACHE.keys()):
            if isinstance(k, tuple) and len(k) >= 2 and k[1] == 'balance':
                _BAL_CACHE.pop(k, None)


@dataclass
class PreflightResult:
    """Result of preflight check. `ok=True` only when ALL checks pass.
    Failures list contains human-readable reasons; the executor surfaces
    them to dryrun.jsonl as `aborted_reason`."""
    ok: bool
    failures: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    leg_results: List[dict] = field(default_factory=list)


def _cache_get(key: tuple) -> Optional[float]:
    entry = _BAL_CACHE.get(key)
    if entry is None:
        return None
    val, ts = entry
    # Phase 19v19 — TTL depends on kind (balance=2s, allowance=600s)
    kind = key[1] if isinstance(key, tuple) and len(key) >= 2 else 'balance'
    if time.time() - ts > _ttl_for_kind(kind):
        return None
    return val


def _cache_put(key: tuple, val: float) -> None:
    _BAL_CACHE[key] = (val, time.time())


def _read_chain(address: str, kind: str, *,
                 spender: Optional[str] = None,
                 web3_client=None) -> Optional[float]:
    """Returns balance (kind='balance') or allowance (kind='allowance')
    in human pUSD units (6-decimal token → divided by 1e6). Returns None
    on failure (web3 not installed, RPC down, etc) — caller treats as
    'unknown, allow with warning'."""
    cache_key = (address.lower(), kind, (spender or '').lower())
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        if web3_client is None:
            from web3 import Web3
            web3_client = Web3(Web3.HTTPProvider(POLYGON_RPC,
                                                  request_kwargs={'timeout': 5}))
        # Phase 19v19 (05.05.2026) — checksum-normalize ALL addresses
        # before passing to web3.py. Old code relied on env-var values
        # being already EIP-55 checksummed; mixed-case-but-not-checksum
        # values (e.g. operator hand-edited Credentials.env) raised
        # `InvalidAddress` from web3.py → caught by outer except →
        # `_read_chain` returned None → preflight downgraded to "skip
        # with warning" → allowance never enforced for neg_risk markets
        # → on-chain TX failed silently because allowance=0.
        from web3 import Web3 as _W3
        try:
            address_cs = _W3.to_checksum_address(address)
        except Exception:
            address_cs = address
        try:
            spender_cs = _W3.to_checksum_address(spender) if spender else None
        except Exception:
            spender_cs = spender
        try:
            pusd_cs = _W3.to_checksum_address(PUSD_ADDRESS)
        except Exception:
            pusd_cs = PUSD_ADDRESS
        contract = web3_client.eth.contract(address=pusd_cs, abi=ERC20_ABI)
        if kind == 'balance':
            raw = contract.functions.balanceOf(address_cs).call()
        elif kind == 'allowance':
            raw = contract.functions.allowance(address_cs, spender_cs).call()
        else:
            return None
        val = raw / 1e6                              # pUSD has 6 decimals
        _cache_put(cache_key, val)
        return val
    except Exception as e:
        log.warning("web3 read failed (%s/%s): %s — preflight downgrades to warn",
                    address[:8], kind, e)
        return None


# Phase 16+ (01.05.2026) — per-chain balance helper.
def check_balance_for_platform(eth_address: str, required_usd: float,
                                  platform: str = 'Polymarket') -> tuple:
    """Phase 16+ — chain-aware balance read. Cross-platform arbs need each
    leg's wallet to have balance on the LEG'S chain. Polymarket leg →
    pUSD on Polygon; Limitless → USDC on Base; SX → USDC on SX Network.

    Returns (ok: bool, balance: Optional[float], reason: str).
    """
    cfg = PER_CHAIN_CONFIG.get(platform)
    if cfg is None:
        return True, None, f'no per-chain config for {platform}'
    rpc_url, token_addr, _exchange = cfg
    try:
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={'timeout': 5}))
        contract = w3.eth.contract(address=token_addr, abi=ERC20_ABI)
        raw = contract.functions.balanceOf(eth_address).call()
        bal = raw / 1e6                    # USDC variants all have 6 decimals
    except ImportError:
        return True, None, f'web3 not installed — {platform} balance skipped'
    except Exception as e:
        return True, None, f'{platform} RPC failed: {type(e).__name__}'
    if bal < required_usd:
        return False, bal, (f'{platform} balance ${bal:.2f} < required '
                             f'${required_usd:.2f} on {eth_address[:8]}…')
    return True, bal, f'{platform} balance ${bal:.2f} >= ${required_usd:.2f} OK'


def check_balance(eth_address: str, required_usd: float,
                   web3_client=None) -> tuple:
    """Return (ok: bool, balance: Optional[float], reason: str).
    `balance is None` → web3 unavailable, treat as warning (not failure)."""
    bal = _read_chain(eth_address, 'balance', web3_client=web3_client)
    if bal is None:
        return True, None, 'balance check skipped (web3 unavailable)'
    if bal < required_usd:
        return False, bal, (f'pUSD balance ${bal:.2f} < required ${required_usd:.2f} '
                             f'on wallet {eth_address[:8]}…')
    return True, bal, f'balance ${bal:.2f} >= ${required_usd:.2f} OK'


def check_allowance(eth_address: str, required_usd: float, *,
                     neg_risk: bool = False, web3_client=None) -> tuple:
    """Same shape as check_balance. Allowance to V2 exchange must cover
    at least the stake (we recommend MAX_UINT256 set via polymarket_approve.py
    so this check is essentially a sanity-test that approval wasn't reset)."""
    spender = EXCHANGE_NEGRISK if neg_risk else EXCHANGE_STANDARD
    al = _read_chain(eth_address, 'allowance', spender=spender,
                      web3_client=web3_client)
    if al is None:
        return True, None, 'allowance check skipped (web3 unavailable)'
    if al < required_usd:
        return False, al, (f'pUSD allowance ${al:.2f} < required ${required_usd:.2f} '
                             f'(spender={spender[:8]}…) — re-run polymarket_approve.py')
    return True, al, f'allowance ${al:.2f} >= ${required_usd:.2f} OK'


def check_depth(stake_usd: float, top_of_book_liquidity_usd: float) -> tuple:
    """Return (ok, reason). After Phase 10 #51 the `liquidity` field
    already reflects top-of-book — so we just compare."""
    if top_of_book_liquidity_usd <= 0:
        return False, 'top-of-book liquidity is 0 — order would not fill'
    if stake_usd > top_of_book_liquidity_usd:
        return False, (f'stake ${stake_usd:.2f} exceeds top-of-book '
                        f'depth ${top_of_book_liquidity_usd:.2f} → would partial-fill')
    return True, (f'stake ${stake_usd:.2f} fits in depth ${top_of_book_liquidity_usd:.2f}')


def preflight_arb(deal: dict, wallets: list, *,
                   web3_client=None,
                   skip_balance: bool = False,
                   skip_allowance: bool = False,
                   skip_depth: bool = False) -> PreflightResult:
    """Run all checks for every leg. Returns aggregated PreflightResult.

    `wallets` is the assigned wallet list (one per leg, same order as
    deal['entries']) produced by atomic._assign_wallets.

    Skip flags useful for tests and dry-run mode where on-chain reads
    are not desired.
    """
    res = PreflightResult(ok=True)
    legs = deal.get('entries', [])
    if len(wallets) < len(legs):
        res.ok = False
        res.failures.append(
            f'wallet pool too small: {len(wallets)} wallets for {len(legs)} legs'
        )
        return res

    for i, (leg, wallet) in enumerate(zip(legs, wallets)):
        stake = float(leg.get('stake', 0) or 0)
        liq = float(leg.get('liquidity', 0) or 0)
        addr = getattr(wallet, 'eth_address', '0x0')
        neg_risk = bool(leg.get('neg_risk'))
        # Phase 19v15 (05.05.2026) — route per-platform. Old code always
        # used `check_balance` (Polygon pUSD) and `check_allowance`
        # (Polymarket exchange) regardless of leg.platform → for a
        # cross-platform deal where leg N is Limitless or SX, the
        # balance check read the WRONG chain and returned 0 → fire
        # aborted with a bogus reason. Use chain-aware variant.
        platform = leg.get('platform') or 'Polymarket'
        leg_row = {'leg_idx': i, 'wallet': addr[:10], 'stake': stake,
                    'liquidity': liq, 'platform': platform, 'checks': {}}

        # Depth check (cheapest, no I/O)
        if not skip_depth:
            ok, reason = check_depth(stake, liq)
            leg_row['checks']['depth'] = {'ok': ok, 'reason': reason}
            if not ok:
                res.ok = False
                res.failures.append(f'leg {i} depth: {reason}')

        # Balance check — chain-aware
        if not skip_balance:
            if platform == 'Polymarket':
                ok, val, reason = check_balance(
                    addr, stake, web3_client=web3_client)
            else:
                ok, val, reason = check_balance_for_platform(
                    addr, stake, platform=platform)
            leg_row['checks']['balance'] = {'ok': ok, 'value': val, 'reason': reason}
            if val is None:
                res.warnings.append(f'leg {i} balance: {reason}')
            elif not ok:
                res.ok = False
                res.failures.append(f'leg {i} balance: {reason}')

        # Allowance check — only meaningful for Polymarket V2 (Limitless
        # uses X-API-Key + EIP-712 V1 / no allowance; SX uses CTF tokens).
        if not skip_allowance and platform == 'Polymarket':
            ok, val, reason = check_allowance(addr, stake, neg_risk=neg_risk,
                                                 web3_client=web3_client)
            leg_row['checks']['allowance'] = {'ok': ok, 'value': val, 'reason': reason}
            if val is None:
                res.warnings.append(f'leg {i} allowance: {reason}')
            elif not ok:
                res.ok = False
                res.failures.append(f'leg {i} allowance: {reason}')

        res.leg_results.append(leg_row)
    return res


def clear_cache():
    """For tests + watchdog forced refresh."""
    _BAL_CACHE.clear()
