# Web3 On-Chain Prep (Polygon — pUSD, allowance, balance)

**Created Phase 11 (01.05.2026)** — when work touched `polymarket_approve.py`, `Scripts/preflight.py`, or any web3 read for balance/allowance.

## What this is for

Polymarket V2 trades collateralized in **pUSD** (Polymarket USD), an ERC-20 on Polygon. To trade, every bot wallet needs (one-time):

1. **USDC.e on Polygon** in the wallet (deposit from CEX → Polygon network)
2. **wrap()** USDC.e → pUSD via CollateralOnramp contract
3. **approve(pUSD, exchange_v2)** = MAX_UINT256
4. **setApprovalForAll(CTF, exchange_v2)** = true (so SELL / cancel can move outcome tokens)

Then ongoing:
- **balance reads** before every fire (preflight)
- **allowance reads** at startup + occasionally (in case it gets reset)

## Contracts (verified 28.04.2026)

| Contract | Address | Purpose |
|---|---|---|
| USDC.e (legacy) | `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174` | source of wrap |
| pUSD (Polymarket USD) | `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB` | V2 collateral |
| CollateralOnramp | `0x93070a847efEf7F70739046A929D47a521F5B8ee` | wrap/unwrap |
| CTF Exchange (standard) | `0xE111180000d2663C0091e4f400237545B87B996B` | order matching |
| CTF Exchange (negRisk) | `0xe2222d279d744050d28e00520010520000310F59` | negRisk markets |
| CTF (1155 token) | `0x4d97dcd97ec945f40cf65f87097ace5ea0476045` | outcome shares |

Override via env: `POLY_PUSD_ADDRESS`, `POLY_EXCHANGE_STANDARD`, `POLY_EXCHANGE_NEGRISK`, `POLY_COLLATERAL_ONRAMP`.

## RPC endpoint

Default: `https://polygon-rpc.com` — public, **rate-limits at high volume**.

For real-mode set `POLYGON_RPC_URL` in `Credentials.env`:
- Alchemy free tier: 300k req/mo (enough for ~1 read/30s × 6 wallets × 2 fields = ~12 reads/min ≈ 17k/day)
- Infura free tier: 100k req/day
- QuickNode: starts paid

## Read patterns (preflight.py)

```python
from web3 import Web3

ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "owner", "type": "address"}],
     "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"constant": True, "inputs": [{"name": "owner", "type": "address"},
                                    {"name": "spender", "type": "address"}],
     "name": "allowance", "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
]

w3 = Web3(Web3.HTTPProvider(POLYGON_RPC, request_kwargs={'timeout': 5}))
contract = w3.eth.contract(address=PUSD_ADDRESS, abi=ERC20_ABI)
balance_raw = contract.functions.balanceOf(eth_address).call()
balance_pUSD = balance_raw / 1e6     # pUSD has 6 decimals
```

**ALWAYS cache 30s** (preflight.py uses `_BAL_CACHE`) — RPC reads cost rate-limit; same answer for 30s is fine.

**Always wrap in try/except returning None** — public RPCs flap, downgrade to warning rather than block all trades.

## Write patterns (polymarket_approve.py — one-time per wallet)

```python
# 1. Wrap USDC.e → pUSD
usdc_e = w3.eth.contract(address=USDC_E_ADDRESS, abi=ERC20_ABI)
onramp = w3.eth.contract(address=COLLATERAL_ONRAMP, abi=ONRAMP_ABI)

# Approve onramp to take USDC.e
tx = usdc_e.functions.approve(COLLATERAL_ONRAMP, MAX_UINT256).build_transaction({
    'from': wallet.address, 'nonce': w3.eth.get_transaction_count(wallet.address),
    'gas': 80_000, 'gasPrice': w3.eth.gas_price,
})
signed = wallet.sign_transaction(tx)
w3.eth.send_raw_transaction(signed.rawTransaction)

# Then wrap
tx = onramp.functions.wrap(amount_in_raw).build_transaction({...})
# ...

# 2. Approve pUSD spending by exchange
tx = pusd.functions.approve(EXCHANGE_STANDARD, MAX_UINT256).build_transaction({...})

# 3. setApprovalForAll for CTF tokens
tx = ctf.functions.setApprovalForAll(EXCHANGE_STANDARD, True).build_transaction({...})
```

Run BOTH for standard AND negRisk exchanges (4 approves + 1 wrap = 5 txs per wallet ≈ $0.05 in MATIC gas).

## Verification (after running approve script)

On Polygonscan look up the bot address:
- `pUSD.balanceOf(wallet) > 0` ✅ wrap worked
- `pUSD.allowance(wallet, exchange_standard) = 2^256-1` ✅ approve worked
- `CTF.isApprovedForAll(wallet, exchange_standard) = true` ✅ setApprovalForAll worked
- Repeat for negRisk

## Common gotchas

1. **MATIC for gas** — wallet needs ~0.5 MATIC for the 5 prep txs. Without it, all approves silently fail to broadcast (RPC returns "insufficient funds for gas").
2. **Old USDC vs USDC.e** — Polygon has BOTH `USDC` (native, USDCn) and `USDC.e` (bridged from Ethereum, our target). Bridge if needed. Polymarket V1 used USDC.e; V2 uses pUSD wrapped FROM USDC.e.
3. **Allowance reset** — some contract upgrades reset allowance. Re-run preflight allowance check at startup.
4. **Public RPC throttle** — `polygon-rpc.com` flaps to 429 / 502 around peak hours. Set Alchemy/Infura URL.
5. **Block confirmation lag** — `send_raw_transaction` returns immediately, but state isn't settled until ~3 confirmations (~10s on Polygon). Don't read balance immediately after wrap; wait or use `wait_for_transaction_receipt`.
6. **Same nonce error** — Polygon JSON-RPC sometimes returns stale `eth_getTransactionCount`. Bump manually if "nonce too low" comes back.

## Why not py-clob-client for the trading flow

We deliberately wrote our own EIP-712 signer in `Scripts/executor/builders.py` (~150 LoC):
- No heavy dependency
- V2 spec verified against `github.com/Polymarket/clob-client` main branch
- Deterministic — every byte we sign is in our control

But for **on-chain prep** (`polymarket_approve.py`) we use `web3.py` directly (it's a different layer — JSON-RPC to Polygon, not API to Polymarket). No reason to roll our own there.

## See also

- `polymarket-trading` skill — V2 EIP-712 signing, L2 HMAC
- `secrets-management` skill — Credentials.env handling for keys
- `Scripts/polymarket_approve.py` — one-time prep CLI
- `Scripts/preflight.py` — runtime balance + allowance reads
- `BUG_CATALOG.md` 5.W — preflight entry
