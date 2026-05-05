"""One-time on-chain prep for Polymarket V2 trading (Polygon, post-cutover).

V2 migration changed the collateral token: trading now uses **pUSD**, not
USDC.e. So before flipping `DRY_RUN=0`, every bot wallet needs:

  1. **USDC.e on Polygon** in the wallet (deposit via Bybit/OKX/Coinbase
     → Polygon network)
  2. **wrap()** USDC.e → pUSD on the Collateral Onramp contract
  3. **approve(pUSD)** on the V2 CTF Exchange contract (one tx per
     wallet, MAX_UINT256 allowance)
  4. **approve(CTF 1155)** for setApprovalForAll on the same Exchange
     (so SELL orders / cancellations can move outcome tokens out)

This script does steps 2-4. It is destructive (sends txs that cost MATIC
gas), so run it explicitly per bot — never automatically on radar startup.

Usage:
    pip install web3 eth-account
    python Scripts/polymarket_approve.py --bot bot1
    python Scripts/polymarket_approve.py --dry-run     # show, don't send

Contract addresses (Polygon mainnet, V2 cutover ~early 2026):
- USDC.e (legacy):           0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174
- pUSD (Polymarket USD):     read from /v2-migration docs at runtime — for
                              now we use the address from on-chain
                              registry (governance-controlled, may change)
- CTF Exchange (standard):   0xE111180000d2663C0091e4f400237545B87B996B
- CTF Exchange (negRisk):    0xe2222d279d744050d28e00520010520000310F59
- CTF (1155 tokens):         0x4d97dcd97ec945f40cf65f87097ace5ea0476045

If addresses change, override via env:
    POLY_PUSD_ADDRESS=0x...
    POLY_EXCHANGE_STANDARD=0x...
    POLY_EXCHANGE_NEGRISK=0x...

Smoke verification — after running, check on Polygonscan:
- Wallet's pUSD balance > 0  (wrap successful)
- pUSD.allowance(wallet, exchange_standard) = 2^256-1  (approve successful)
- CTF.isApprovedForAll(wallet, exchange_standard) = true  (setApprovalForAll OK)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from web3 import Web3
    from eth_account import Account
    _WEB3_AVAILABLE = True
except ImportError:
    _WEB3_AVAILABLE = False
    Web3 = None
    Account = None


POLYGON_RPC = os.environ.get('POLYGON_RPC_URL', 'https://polygon-rpc.com')

# ── V2 contracts on Polygon mainnet (verified 28.04.2026) ──────────
# Source: https://docs.polymarket.com/resources/contracts
#         https://docs.polymarket.com/concepts/pusd
#         + on-chain confirmation via PolygonScan label
USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
# pUSD ERC-20 (proxy contract) — this is what gets approved + transferred.
PUSD_ADDRESS = os.environ.get(
    'POLY_PUSD_ADDRESS',
    '0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB',
)
# CollateralOnramp — separate contract holding wrap()/unwrap(). pUSD
# itself does NOT have wrap; we approve USDC.e to this onramp, then
# Onramp.wrap(amount) mints pUSD on msg.sender. unwrap() reverses.
COLLATERAL_ONRAMP = os.environ.get(
    'POLY_COLLATERAL_ONRAMP',
    '0x93070a847efEf7F70739046A929D47a521F5B8ee',
)
# CTF Exchange V2 — standard markets
EXCHANGE_STANDARD = os.environ.get(
    'POLY_EXCHANGE_STANDARD',
    '0xE111180000d2663C0091e4f400237545B87B996B',
)
# Neg Risk CTF Exchange — multi-outcome markets
EXCHANGE_NEGRISK = os.environ.get(
    'POLY_EXCHANGE_NEGRISK',
    '0xe2222d279d744050d28e00520010520000310F59',
)
# Conditional Tokens Framework (1155) — outcome tokens themselves
CTF_ADDRESS = os.environ.get(
    'POLY_CTF_ADDRESS',
    '0x4D97DCd97eC945f40cF65F87097ACe5EA0476045',
)
# NegRisk Adapter — additional spender for negRisk-related ops
NEGRISK_ADAPTER = os.environ.get(
    'POLY_NEGRISK_ADAPTER',
    '0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296',
)

MAX_UINT256 = 2**256 - 1
USDC_E_DECIMALS = 6
PUSD_DECIMALS = 6

ERC20_ABI = [
    {"constant": False, "inputs": [
        {"name": "spender", "type": "address"},
        {"name": "amount", "type": "uint256"},
    ], "name": "approve", "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable", "type": "function"},
    {"constant": True, "inputs": [
        {"name": "owner", "type": "address"},
    ], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view", "type": "function"},
    {"constant": True, "inputs": [
        {"name": "owner", "type": "address"},
        {"name": "spender", "type": "address"},
    ], "name": "allowance", "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view", "type": "function"},
]

# pUSD onramp / wrap. Real ABI may differ; placeholder until V2 docs
# expose the canonical interface. We expose `wrap(uint256 amount)` and
# `unwrap(uint256 amount)` which is the spec'd behaviour per migration docs.
WRAP_ABI = [
    {"constant": False, "inputs": [{"name": "amount", "type": "uint256"}],
     "name": "wrap", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"constant": False, "inputs": [{"name": "amount", "type": "uint256"}],
     "name": "unwrap", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
]

CTF_1155_ABI = [
    {"inputs": [
        {"name": "operator", "type": "address"},
        {"name": "approved", "type": "bool"},
    ], "name": "setApprovalForAll", "outputs": [],
        "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [
        {"name": "account", "type": "address"},
        {"name": "operator", "type": "address"},
    ], "name": "isApprovedForAll", "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view", "type": "function"},
]


def _load_credentials():
    env_file = os.path.join(os.path.dirname(HERE), 'Credentials.env')
    env_vars = dict(os.environ)
    if os.path.exists(env_file):
        with open(env_file, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line: continue
                k, v = line.split('=', 1)
                env_vars.setdefault(k.strip(), v.strip())
    out = []
    for n in range(1, 7):
        addr = env_vars.get(f'BOT{n}_ETH_ADDRESS', '').strip()
        pk = env_vars.get(f'BOT{n}_PRIVATE_KEY', '').strip()
        if addr and pk:
            out.append({'bot_id': f'bot{n}', 'address': addr, 'private_key': pk})
    return out


def _send(w3, wallet, fn, gas=120000):
    """Build, sign, send a transaction. Returns tx hash."""
    nonce = w3.eth.get_transaction_count(Web3.to_checksum_address(wallet['address']))
    tx = fn.build_transaction({
        'from': Web3.to_checksum_address(wallet['address']),
        'nonce': nonce,
        'gas': gas,
        'maxFeePerGas': w3.eth.gas_price * 2,
        'maxPriorityFeePerGas': w3.to_wei('30', 'gwei'),
    })
    signed = Account.sign_transaction(tx, wallet['private_key'])
    txh = w3.eth.send_raw_transaction(signed.raw_transaction)
    rcpt = w3.eth.wait_for_transaction_receipt(txh, timeout=180)
    return txh.hex(), rcpt.status


def _wrap_step(w3, wallet, dry_run):
    """Step 1: approve USDC.e on CollateralOnramp, then call Onramp.wrap().

    Verified architecture (Polymarket V2 docs + on-chain):
      - pUSD ERC-20 itself does NOT have wrap()/unwrap()
      - CollateralOnramp at 0x9307... is the contract holding wrap logic
      - Flow: USDC.e.approve(onramp, amount) → onramp.wrap(amount) →
              pUSD minted on msg.sender's wallet
    """
    usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_E_ADDRESS),
                            abi=ERC20_ABI)
    onramp = w3.eth.contract(address=Web3.to_checksum_address(COLLATERAL_ONRAMP),
                              abi=WRAP_ABI)
    owner = Web3.to_checksum_address(wallet['address'])
    onramp_addr = Web3.to_checksum_address(COLLATERAL_ONRAMP)
    usdc_balance = usdc.functions.balanceOf(owner).call()
    if usdc_balance == 0:
        print(f"  [{wallet['bot_id']}] USDC.e balance = 0 — deposit first")
        return None
    print(f"  [{wallet['bot_id']}] USDC.e balance: ${usdc_balance / 10**USDC_E_DECIMALS:.2f}")

    # 1a. approve USDC.e on the Onramp (NOT on pUSD itself)
    allowance = usdc.functions.allowance(owner, onramp_addr).call()
    if allowance < usdc_balance:
        if dry_run:
            print(f"  [{wallet['bot_id']}] DRY-RUN: would approve USDC.e on Onramp")
        else:
            txh, st = _send(w3, wallet,
                            usdc.functions.approve(onramp_addr, MAX_UINT256))
            print(f"  [{wallet['bot_id']}] USDC.e approve→Onramp tx: {txh} status={st}")

    # 1b. Onramp.wrap(amount) — mints pUSD into wallet
    if dry_run:
        print(f"  [{wallet['bot_id']}] DRY-RUN: would Onramp.wrap(${usdc_balance/1e6:.2f}) → pUSD")
        return None
    txh, st = _send(w3, wallet,
                     onramp.functions.wrap(usdc_balance), gas=200000)
    print(f"  [{wallet['bot_id']}] Onramp.wrap tx: {txh} status={st}")
    return txh


def _approve_exchanges(w3, wallet, dry_run):
    """Step 2 + 3: approve pUSD + setApprovalForAll on CTF for both
    standard and negRisk exchanges."""
    pusd = w3.eth.contract(address=Web3.to_checksum_address(PUSD_ADDRESS),
                            abi=ERC20_ABI)
    ctf = w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS),
                           abi=CTF_1155_ABI)
    owner = Web3.to_checksum_address(wallet['address'])
    txs = []
    # Phase 19v22 (05.05.2026) — also approve NEGRISK_ADAPTER. The adapter
    # is a SEPARATE spender used for `mergePositions`/`splitPosition`
    # on negRisk markets (also routed through during certain negRisk
    # `fillOrder` flows). Old approve loop only granted pUSD allowance
    # + CTF setApprovalForAll to the two exchange addresses, not the
    # adapter — first real negRisk fire that hit the adapter path
    # reverted on-chain with "ERC20 transfer amount exceeds allowance"
    # OR "ERC1155 caller is not approved". Pre-approve all three.
    for label, exchange in [('standard', EXCHANGE_STANDARD),
                             ('negRisk', EXCHANGE_NEGRISK),
                             ('negRisk_adapter', NEGRISK_ADAPTER)]:
        ex_addr = Web3.to_checksum_address(exchange)
        # pUSD allowance
        allow = pusd.functions.allowance(owner, ex_addr).call()
        if allow < 10**18:
            if dry_run:
                print(f"  [{wallet['bot_id']}] DRY-RUN: approve pUSD on {label} exchange")
            else:
                txh, st = _send(w3, wallet,
                                pusd.functions.approve(ex_addr, MAX_UINT256))
                print(f"  [{wallet['bot_id']}] pUSD approve {label}: {txh} st={st}")
                txs.append({'kind': f'pusd_approve_{label}', 'tx': txh})
        else:
            print(f"  [{wallet['bot_id']}] pUSD already approved on {label}")
        # CTF setApprovalForAll
        approved = ctf.functions.isApprovedForAll(owner, ex_addr).call()
        if not approved:
            if dry_run:
                print(f"  [{wallet['bot_id']}] DRY-RUN: setApprovalForAll CTF on {label}")
            else:
                txh, st = _send(w3, wallet,
                                ctf.functions.setApprovalForAll(ex_addr, True))
                print(f"  [{wallet['bot_id']}] CTF setApprovalForAll {label}: {txh} st={st}")
                txs.append({'kind': f'ctf_approveAll_{label}', 'tx': txh})
        else:
            print(f"  [{wallet['bot_id']}] CTF already approved on {label}")
    return txs


def main():
    if not _WEB3_AVAILABLE:
        print("ERROR: this script needs web3 + eth-account installed.\n"
              "       pip install web3 eth-account")
        sys.exit(1)
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--bot', help='Only this bot (bot1..bot6)')
    parser.add_argument('--skip-wrap', action='store_true',
                        help='Skip USDC.e→pUSD wrap (already wrapped manually)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Print actions, send no transactions')
    args = parser.parse_args()

    print(f"Connecting to Polygon via {POLYGON_RPC} ...")
    w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
    if not w3.is_connected():
        print(f"ERROR: cannot connect to {POLYGON_RPC}"); sys.exit(2)
    print(f"Chain ID: {w3.eth.chain_id} (expected 137 for Polygon)")
    print(f"pUSD address: {PUSD_ADDRESS}")
    print(f"Exchange (standard): {EXCHANGE_STANDARD}")
    print(f"Exchange (negRisk):  {EXCHANGE_NEGRISK}")

    wallets = _load_credentials()
    if args.bot:
        wallets = [w for w in wallets if w['bot_id'] == args.bot]
    if not wallets:
        print("No wallets with private keys found in Credentials.env")
        sys.exit(3)

    log_path = os.path.join(os.path.dirname(HERE), 'Executions',
                            'polymarket_approves.log')
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    all_txs = []
    for wallet in wallets:
        print(f"\n→ {wallet['bot_id']} ({wallet['address']})")
        try:
            if not args.skip_wrap:
                _wrap_step(w3, wallet, args.dry_run)
            txs = _approve_exchanges(w3, wallet, args.dry_run)
            for tx in txs:
                tx.update({'bot': wallet['bot_id'], 'ts': time.time()})
            all_txs.extend(txs)
        except Exception as e:
            print(f"  [{wallet['bot_id']}] FAILED: {type(e).__name__}: {e}")

    if all_txs and not args.dry_run:
        with open(log_path, 'a', encoding='utf-8') as f:
            for tx in all_txs:
                f.write(json.dumps(tx) + '\n')
        print(f"\nLogged {len(all_txs)} tx(s) to {log_path}")

    print("\nDone. Idempotent — re-run any time, already-approved wallets are skipped.")
    print("After this + Limitless approve + L2 creds, set DRY_RUN=0.")


if __name__ == '__main__':
    main()
