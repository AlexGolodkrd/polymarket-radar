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
_BAL_CACHE: dict = {}
_BAL_TTL_S = 30.0


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
    if time.time() - ts > _BAL_TTL_S:
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
        contract = web3_client.eth.contract(address=PUSD_ADDRESS, abi=ERC20_ABI)
        if kind == 'balance':
            raw = contract.functions.balanceOf(address).call()
        elif kind == 'allowance':
            raw = contract.functions.allowance(address, spender).call()
        else:
            return None
        val = raw / 1e6                              # pUSD has 6 decimals
        _cache_put(cache_key, val)
        return val
    except Exception as e:
        log.warning("web3 read failed (%s/%s): %s — preflight downgrades to warn",
                    address[:8], kind, e)
        return None


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
        leg_row = {'leg_idx': i, 'wallet': addr[:10], 'stake': stake,
                    'liquidity': liq, 'checks': {}}

        # Depth check (cheapest, no I/O)
        if not skip_depth:
            ok, reason = check_depth(stake, liq)
            leg_row['checks']['depth'] = {'ok': ok, 'reason': reason}
            if not ok:
                res.ok = False
                res.failures.append(f'leg {i} depth: {reason}')

        # Balance check (web3 RPC, cached 30s)
        if not skip_balance:
            ok, val, reason = check_balance(addr, stake, web3_client=web3_client)
            leg_row['checks']['balance'] = {'ok': ok, 'value': val, 'reason': reason}
            if val is None:
                res.warnings.append(f'leg {i} balance: {reason}')
            elif not ok:
                res.ok = False
                res.failures.append(f'leg {i} balance: {reason}')

        # Allowance check (also cached)
        if not skip_allowance:
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
