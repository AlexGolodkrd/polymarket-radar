"""One-time on-chain approve flow for Limitless Exchange (Base L2).

Run this ONCE per bot wallet before flipping `DRY_RUN=0`. The Limitless
CLOB matches off-chain orders against on-chain CTF (conditional-token
framework) collateral, so every wallet must grant two ERC-20 allowances:

  1. **USDC** allowance to the Limitless CTF Exchange contract — lets it
     pull collateral when our BUY orders match.
  2. **CTF (1155)** approveForAll to the same Exchange — lets it transfer
     outcome tokens out when our SELL orders match (or in reversal flow).

Both need ~$0.005 of Base ETH as gas. Approval to MAX_UINT256 means we
do this once per bot lifetime, not per trade.

Why a separate script (not part of arb_server)
- approving on-chain is a destructive, billable action — it should NEVER
  run automatically on radar startup. Operator runs it explicitly per bot.
- Keeps `arb_server.py` free of `web3` dependency at import time.

Usage
-----
Pre-reqs:
- `pip install web3 eth-account`
- `Credentials.env` has BOT{N}_ETH_ADDRESS + BOT{N}_PRIVATE_KEY for each
  bot you want to approve, plus BASE_RPC_URL (default: https://mainnet.base.org)

Examples:
    # Approve every configured bot
    python Scripts/limitless_approve.py

    # Approve a specific bot only
    python Scripts/limitless_approve.py --bot bot1

    # Dry-run — print what would happen, send no tx
    python Scripts/limitless_approve.py --dry-run

After successful approve, the cli prints tx hashes to copy into
`Executions/limitless_approves.log` for audit.

Contract addresses (Base mainnet, verified 28.04.2026)
- USDC:               0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913
- CTF (Conditional Tokens): 0xC5d563A36AE78145C45a50134d48A1215220f80a (varies per venue;
                          set per env if your venue.exchange differs)
- LimitlessExchange:  passed via --exchange or read from env LIMITLESS_EXCHANGE_ADDRESS
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# Soft-import — script gracefully aborts with a useful message if web3 is
# not installed (it's optional in requirements.txt).
try:
    from web3 import Web3
    from eth_account import Account
except ImportError as e:
    print("ERROR: this script needs web3 + eth-account installed.\n"
          "       pip install web3 eth-account")
    print(f"       (missing: {e})")
    sys.exit(1)


# ── Constants ────────────────────────────────────────────────────────
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
USDC_DECIMALS = 6
MAX_UINT256 = 2**256 - 1
BASE_RPC = os.environ.get('BASE_RPC_URL', 'https://mainnet.base.org')
DEFAULT_EXCHANGE = os.environ.get(
    'LIMITLESS_EXCHANGE_ADDRESS',
    '0x05c748E2f4DcDe0ec9Fa8DDc40DE6b867f923fa5',  # observed 28.04 on Lumy markets
)

# Minimal ABIs — only the methods we actually call.
ERC20_APPROVE_ABI = [{
    "constant": False, "inputs": [
        {"name": "spender", "type": "address"},
        {"name": "amount", "type": "uint256"},
    ],
    "name": "approve", "outputs": [{"name": "", "type": "bool"}],
    "stateMutability": "nonpayable", "type": "function",
}, {
    "constant": True, "inputs": [
        {"name": "owner", "type": "address"},
        {"name": "spender", "type": "address"},
    ],
    "name": "allowance", "outputs": [{"name": "", "type": "uint256"}],
    "stateMutability": "view", "type": "function",
}]


# ── Wallet loading ───────────────────────────────────────────────────
def _load_credentials():
    """Read Credentials.env (or env vars) for BOT{N}_ETH_ADDRESS + BOT{N}_PRIVATE_KEY.
    Returns list of dicts: [{bot_id, address, private_key}, ...]"""
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


# ── Approve flow ─────────────────────────────────────────────────────
def _ensure_usdc_allowance(w3, wallet, exchange_addr, dry_run):
    usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_BASE),
                           abi=ERC20_APPROVE_ABI)
    owner = Web3.to_checksum_address(wallet['address'])
    spender = Web3.to_checksum_address(exchange_addr)
    current = usdc.functions.allowance(owner, spender).call()
    print(f"  [{wallet['bot_id']}] USDC allowance to {spender}: "
          f"{current / 10**USDC_DECIMALS:.2f} USDC")
    if current >= 10**18:
        print(f"  [{wallet['bot_id']}] USDC already approved — skip")
        return None
    if dry_run:
        print(f"  [{wallet['bot_id']}] DRY-RUN: would approve USDC max")
        return None
    nonce = w3.eth.get_transaction_count(owner)
    tx = usdc.functions.approve(spender, MAX_UINT256).build_transaction({
        'from': owner,
        'nonce': nonce,
        'gas': 60000,
        'maxFeePerGas': w3.eth.gas_price * 2,
        'maxPriorityFeePerGas': w3.to_wei('0.001', 'gwei'),
    })
    signed = Account.sign_transaction(tx, wallet['private_key'])
    txh = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"  [{wallet['bot_id']}] USDC approve sent: {txh.hex()}")
    rcpt = w3.eth.wait_for_transaction_receipt(txh, timeout=120)
    print(f"  [{wallet['bot_id']}] USDC approve confirmed in block {rcpt.blockNumber}, "
          f"status={rcpt.status}")
    return txh.hex()


# Phase 16+ (01.05.2026) — CTF setApprovalForAll for SELL/cancel paths.
# Without this, exchange cannot transfer outcome tokens out of our wallet
# during SELL/revert. Original limitless_approve.py only covered USDC
# allowance (BUY collateral) — operator-found gap during Phase 16 audit.
CTF_APPROVE_ABI = [
    {"inputs": [
        {"name": "operator", "type": "address"},
        {"name": "approved", "type": "bool"},
     ], "name": "setApprovalForAll", "outputs": [],
     "stateMutability": "nonpayable", "type": "function"},
    {"constant": True, "inputs": [
        {"name": "owner", "type": "address"},
        {"name": "operator", "type": "address"},
     ], "name": "isApprovedForAll",
     "outputs": [{"name": "", "type": "bool"}],
     "stateMutability": "view", "type": "function"},
]


def _ensure_ctf_approval(w3, wallet, exchange_addr, ctf_addr, dry_run):
    """setApprovalForAll(CTF, exchange, true) — required for SELL flow.
    `ctf_addr` is the 1155 outcome token contract; passed via CLI flag
    (--ctf-address) or LIMITLESS_CTF_ADDRESS env. If neither set, skipped
    with warning.
    """
    if not ctf_addr:
        print(f"  [{wallet['bot_id']}] no CTF address — skip "
              "(pass --ctf-address or set LIMITLESS_CTF_ADDRESS)")
        return None
    ctf = w3.eth.contract(address=Web3.to_checksum_address(ctf_addr),
                          abi=CTF_APPROVE_ABI)
    owner = Web3.to_checksum_address(wallet['address'])
    spender = Web3.to_checksum_address(exchange_addr)
    current = ctf.functions.isApprovedForAll(owner, spender).call()
    print(f"  [{wallet['bot_id']}] CTF approveForAll: {current}")
    if current:
        print(f"  [{wallet['bot_id']}] CTF already approved — skip")
        return None
    if dry_run:
        print(f"  [{wallet['bot_id']}] DRY-RUN: would setApprovalForAll(CTF)")
        return None
    nonce = w3.eth.get_transaction_count(owner)
    tx = ctf.functions.setApprovalForAll(spender, True).build_transaction({
        'from': owner,
        'nonce': nonce,
        'gas': 80000,
        'maxFeePerGas': w3.eth.gas_price * 2,
        'maxPriorityFeePerGas': w3.to_wei('0.001', 'gwei'),
    })
    signed = Account.sign_transaction(tx, wallet['private_key'])
    txh = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"  [{wallet['bot_id']}] CTF approveForAll sent: {txh.hex()}")
    rcpt = w3.eth.wait_for_transaction_receipt(txh, timeout=120)
    print(f"  [{wallet['bot_id']}] CTF approveForAll confirmed in block "
          f"{rcpt.blockNumber}, status={rcpt.status}")
    return txh.hex()


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--bot', help='Approve only this bot (bot1..bot6). Default: all.')
    parser.add_argument('--exchange', default=DEFAULT_EXCHANGE,
                        help=f'Limitless Exchange address (default: {DEFAULT_EXCHANGE})')
    parser.add_argument('--ctf-address',
                        default=os.environ.get('LIMITLESS_CTF_ADDRESS'),
                        help='CTF 1155 contract for setApprovalForAll '
                             '(or set env LIMITLESS_CTF_ADDRESS)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Print actions, send no transactions.')
    args = parser.parse_args()

    print(f"Connecting to Base via {BASE_RPC} ...")
    w3 = Web3(Web3.HTTPProvider(BASE_RPC))
    if not w3.is_connected():
        print(f"ERROR: cannot connect to {BASE_RPC}"); sys.exit(2)
    print(f"Chain ID: {w3.eth.chain_id} (expected 8453 for Base mainnet)")

    wallets = _load_credentials()
    if args.bot:
        wallets = [w for w in wallets if w['bot_id'] == args.bot]
    if not wallets:
        print("No wallets with private keys found. Fill BOT{N}_ETH_ADDRESS + "
              "BOT{N}_PRIVATE_KEY in Credentials.env.")
        sys.exit(3)

    log_path = os.path.join(os.path.dirname(HERE), 'Executions',
                            'limitless_approves.log')
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    txs = []
    for wallet in wallets:
        print(f"\n→ {wallet['bot_id']} ({wallet['address']})")
        try:
            txh = _ensure_usdc_allowance(w3, wallet, args.exchange, args.dry_run)
            if txh:
                txs.append({'bot': wallet['bot_id'], 'kind': 'usdc_approve', 'tx': txh,
                            'ts': time.time()})
        except Exception as e:
            print(f"  [{wallet['bot_id']}] USDC FAILED: {type(e).__name__}: {e}")
        # Phase 16+ (01.05.2026) — also setApprovalForAll for CTF tokens.
        try:
            txh2 = _ensure_ctf_approval(w3, wallet, args.exchange,
                                          args.ctf_address, args.dry_run)
            if txh2:
                txs.append({'bot': wallet['bot_id'], 'kind': 'ctf_approve_all',
                            'tx': txh2, 'ts': time.time()})
        except Exception as e:
            print(f"  [{wallet['bot_id']}] CTF FAILED: {type(e).__name__}: {e}")

    if txs and not args.dry_run:
        with open(log_path, 'a', encoding='utf-8') as f:
            for tx in txs:
                f.write(json.dumps(tx) + '\n')
        print(f"\nLogged {len(txs)} tx(s) to {log_path}")

    print("\nDone. Re-run any time — already-max-approved wallets are skipped.")
    print("After this, set DRY_RUN=0 in your env to enable real Limitless trading.")


if __name__ == '__main__':
    main()
