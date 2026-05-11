---
name: polymarket-v2-troubleshoot
description: Failure-to-cause matrix for Polymarket V2 trading. Maps every observable symptom (HTTP code, error string, on-chain revert, "gas paid no fill") to specific cause and fix. Includes live-bug list from py-clob-client-v2 GitHub and field-reported errors as of 05.05.2026.
---

# Polymarket V2 Troubleshoot — Failure → Cause Matrix

**Created 05.05.2026** — companion to `polymarket-v2-auth` and `polymarket-v2-connector`. Use when a trade fails. Find your symptom, then walk the diagnosis.

## When to use

- Bot logs show `INVALID_SIGNATURE`, `order_version_mismatch`, `401`, `403`, or any 4xx from `clob.polymarket.com`
- `"maker address not allowed, please use the deposit wallet flow"` error
- Order is accepted (200) but never fills
- Order is matched but on-chain settlement reverts (gas spent, no position)
- Cancellation fails with `403`

For the conceptual auth model see `polymarket-v2-auth`. For the canonical signed-order template see `polymarket-v2-connector`.

---

## Step 0 — collect evidence before guessing

Without these five pieces of data you're guessing:

1. **Exact error string** from the HTTP response body (not just the status code)
2. **`signature_type`** value passed to ClobClient
3. **`funder`** address passed to ClobClient
4. **Package version** — `pip show py-clob-client-v2 | grep Version` (must be ≥ 1.0.0; if you see `py-clob-client` instead — that's V1, swap immediately)
5. **For "gas paid, no fill"**: the on-chain tx hash — get from `GET /transaction?transactionID=<id>` after `POST /submit`, then look up on Polygonscan for the revert reason

---

## Symptom matrix

### A. HTTP errors at `POST /order`

| Symptom | Likely cause | Fix |
|---|---|---|
| `400 {"error":"order_version_mismatch"}` | Using `py-clob-client` (V1, hardcodes `version: "1"`) against V2 server | `pip uninstall py-clob-client && pip install py-clob-client-v2`. Or, if writing raw signer: set EIP-712 domain `version: "2"` and use V2 verifyingContract addresses |
| `400 {"error":"maker address not allowed, please use the deposit wallet flow"}` | **Field-reported (05.05.2026)**: operator put a deposit-address EOA in `funder` instead of the trading proxy/Safe. Server's known proxy for the signer is the actual Safe contract; the deposit address is just an ephemeral USDC receiver, not the trading wallet. | (1) Verify `funder` is a CONTRACT not EOA: `len(w3.eth.get_code(funder)) > 0`. (2) Get the real Safe address from polymarket.com/settings → click → must show "Contract" on Polygonscan. (3) Re-derive API creds with `signature_type` and `funder` BOTH set on the temp client. (4) Run `python Scripts/poly_verify_funder.py --bot bot1` |
| `400 {"error":"invalid signature"}` (proxy works, EOA fails or vice versa) | `signature_type` doesn't match wallet topology. Most common: type 2 used with `funder = signer_address` instead of Gnosis Safe address | Run the [decision tree in `polymarket-v2-auth`](../polymarket-v2-auth/SKILL.md#decision-tree). Set `funder` = address from polymarket.com/settings |
| `400 invalid signature` on negRisk markets only | Wrong EIP-712 domain — using standard `verifyingContract` for a negRisk market | Fetch `market.negRisk` from gamma-api → use `0xe2222d279d744050d28e00520010520000310F59` for negRisk |
| `400 price not on tick` | `price` not aligned to per-market tickSize | `price = round(price / tick_size) * tick_size` before signing. Get `tick_size` from `/markets/{condition_id}` |
| `400 insufficient allowance` (returned at submit, not on-chain) | pUSD or CTF approval not set on funder | Re-run approve from FUNDER (Gnosis Safe owner if type 2, Magic proxy relay if type 1, EOA if type 0) |
| `401 Unauthorized` on POST /order | API creds bound to a DIFFERENT (signer, funder, sig_type) tuple than the one currently signing the order | Re-derive creds with the temp client initialized with the SAME `signature_type` and `funder` you'll trade with |
| `401` after ~30 min (was working) | Live bug py-clob-client-v2 #40 — creds expire mid-session | Periodically `client.set_api_creds(client.create_or_derive_api_creds())` every 25 min, or re-init client on 401 |
| `403 Cloudflare` on `/auth/api-key` | Live bug py-clob-client-v2 #38, #41 — Cloudflare WAF false-positive | Add residential User-Agent header, slow down, retry with backoff. Confirmed Polymarket-side issue |
| `400 {"error":"NONCE_ALREADY_USED"}` on creds derive | Reusing a nonce that already minted creds | Use `GET /auth/derive-api-key` (re-fetch existing) instead of `POST /auth/api-key` (mint new), OR pick a fresh nonce |

### B. HTTP errors at `DELETE /order/{id}` or `GET /data/positions`

| Symptom | Cause | Fix |
|---|---|---|
| `403` immediately | L2 HMAC headers wrong | Check: secret is base64-decoded BEFORE HMAC; prehash = `ts + METHOD.upper() + path + body`; signature is base64-url-encoded; `POLY_TIMESTAMP` is seconds (not ms); clock drift < 60s |
| `403` after a while (worked earlier) | Same py-clob-client-v2 #40 expiry bug | Re-derive creds |
| `404 order not found` on cancel | Order already filled / cancelled / expired | Pre-check `GET /data/orders/{id}`; treat 404 as success on cancel |

### C. Order accepted (200) but never fills

| Symptom | Cause | Fix |
|---|---|---|
| `status=LIVE` but no fills, your price visible in book | Normal — wait for taker | If you wanted immediate fill, use `orderType=FOK` with price ≥ best ask |
| `status=LIVE`, your price NOT visible in `/book/{token}` | Self-trade prevention or below-min-size | Check `/markets/{cid}` `min_order_size`; verify maker ≠ existing maker on opposite side |
| Order rests forever even when book moves through your price | Order signed against stale `tick_size` cached client-side | Refresh `getClobMarketInfo` every ~5 min; py-clob-client-v2 has `tickSizeTtlMs` removed in V2 |

### D. Order matched (status=MATCHED) but settlement fails — **gas paid, no fill**

This is the most expensive failure mode. **Always** start by getting the tx hash and reading the revert reason on Polygonscan.

| Polygonscan revert | Cause | Fix |
|---|---|---|
| `INSUFFICIENT_ALLOWANCE` / `ERC20: insufficient allowance` | pUSD allowance to V2 Exchange = 0 from FUNDER (likely approved from EOA but funder is a proxy) | For type 2: execute `pUSD.approve(exchangeV2, MAX)` as Gnosis Safe transaction (not EOA). For type 1: relay through proxy. Easiest: place a small order via polymarket.com UI — UI sets approves from the proxy automatically |
| `INSUFFICIENT_BALANCE` / `ERC20: transfer amount exceeds balance` | Funder has 0 pUSD (still in USDC.e) OR has pUSD but not enough for `amount + fee` | Wrap USDC.e → pUSD via Onramp; keep ≥ 2% buffer above order amount for taker fee |
| `NOT_ENOUGH_TOKENS` / `ERC1155: insufficient balance for transfer` (on SELL) | CTF outcome tokens not approved to V2 Exchange | `CTF.setApprovalForAll(exchangeV2, true)` from FUNDER |
| `INVALID_SIGNATURE` reaching contract level | EIP-712 domain mismatch (e.g. `version: "1"` or wrong verifyingContract) | Verify signed order uses V2 domain with `version: "2"` and correct contract per market.negRisk |
| `MarketResolved` / payoutDenominator non-zero | Market resolved between submit and inclusion | Pre-flight check market is `closed=false` AND not in resolution window; gate via `time-freshness-validation` skill |
| `Order has been filled` / `Order is cancelled` | Race: counterparty already took or cancelled before your tx mined | For FOK retry on race; for GTC accept partial; mind issue py-clob-client-v2 #34 (ghost MATCHED) |
| Out-of-gas (no revert string, all gas burned) | Settlement of multi-fill match exceeds gasLimit | `gasLimit = estimate * 1.3`; for negRisk wraps add ~150k headroom |
| Empty revert / no reason | Often: contract paused or upgrade in progress | Check `https://status.polymarket.com`; abort with backoff |

### E. Live bugs in py-clob-client-v2 v1.0.0 as of 05.05.2026

Track these in your bot — they're upstream, you can't fix them without a workaround:

| # | Title | Workaround |
|---|---|---|
| #34 | `status=MATCHED` for orders that never settle on-chain ("ghost fills") | Don't trust MATCHED until tx hash confirms on Polygonscan; treat MATCHED as "pending" until on-chain confirmation |
| #36 | Library `prints()` errors to console; breaks curses/structured-log layouts | Monkey-patch `builtins.print` for the library, or pipe stderr appropriately |
| #38, #41 | Cloudflare 403 on `/auth/api-key` creation | Set residential User-Agent; backoff retry; or derive once via TS SDK and reuse creds |
| #40 | L2 creds become invalid after ~30 minutes | Refresh creds every 25 min; treat any 401 as "re-derive needed" |
| #43 | `signature_type=2 (POLY_GNOSIS_SAFE)` returns 401 on POST /order | Reported active bug — verify your wallet is REALLY type 2 first; if confirmed, watch issue for fix |
| #45 | Derived creds return 401 on all L2 endpoints | Same root as #40/#43; refresh creds on every 401. ALSO ensure derivation context matches trading context (sig_type + funder) |
| #46 | "Unable to send order with the generated api credentials" | Same; also verify signer = creds-deriving key |
| #47 | Order filled at wrong price | Server-side issue; for now use tighter slippage / FOK with strict price |
| #335-340 (in `py-clob-client` V1 repo) | `order_version_mismatch` — V1 lib against V2 server | **Switch to `py-clob-client-v2` package — V1 will never be fixed** |

---

## Diagnostic flow chart

```
Trade fails
│
├── HTTP 4xx at POST /order?
│   └── Read response body for "error" string → row in matrix A
│       ├── "order_version_mismatch"           → use py-clob-client-v2
│       ├── "maker address not allowed"        → funder is a deposit EOA, not Safe
│       ├── "invalid signature"                → sig_type / domain / funder mismatch
│       └── "401 / 403"                        → re-derive creds in matching mode
│
├── HTTP 200 but never matches?
│   └── Check /book; check tick alignment → row in matrix C
│
├── status=MATCHED but no position appears?
│   ├── Wait 30s for on-chain confirm
│   ├── Get tx hash via GET /transaction
│   ├── Polygonscan → "Status: Failed" → revert reason → matrix D
│   └── If "Status: Success" but position not in /data/positions → ghost fill (#34)
│
└── Cancellation fails?
    └── Matrix B
```

---

## Five smoke checks every bot should run on startup

```python
def smoke_check_polymarket_v2(client, signer_addr, funder_addr, sig_type):
    import requests, base64
    from web3 import Web3

    # 1. Package version
    import py_clob_client_v2
    assert py_clob_client_v2.__version__ >= "1.0.0", "Use V2 package"

    # 2. Signer address matches private key
    from eth_account import Account
    actual_signer = Account.from_key(client.key).address
    assert actual_signer.lower() == signer_addr.lower(), \
        f"signer mismatch: key controls {actual_signer}, expected {signer_addr}"

    # 3. Funder is a CONTRACT (not deposit-address EOA)
    if sig_type in (1, 2):
        w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))
        code = w3.eth.get_code(Web3.to_checksum_address(funder_addr))
        assert len(code) > 0, (
            f"funder {funder_addr} has no contract code — looks like a "
            f"deposit address, not the trading proxy/Safe")

    # 4. Funder has pUSD
    pusd = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
    bal = call_erc20_balance(pusd, funder_addr)
    assert bal > 0, f"Funder {funder_addr} has 0 pUSD"

    # 5. pUSD allowance to V2 Exchange
    exchange_std = "0xE111180000d2663C0091e4f400237545B87B996B"
    allow = call_erc20_allowance(pusd, funder_addr, exchange_std)
    assert allow > 10**24, "Approve pUSD on V2 Exchange first (UI or Safe SDK)"

    # 6. L2 read works (HMAC sanity)
    positions = client.get_positions()
    assert isinstance(positions, list), "Positions endpoint failed (401/403?)"

    return True
```

If any of these fail, fix BEFORE first order. The smoke check should be in `Scripts/preflight_polymarket.py` and run as part of the deploy pipeline (see `deploy-pipeline` skill). Plan-kapkan ships `Scripts/poly_verify_funder.py` which automates checks 3-5.

---

## When to escalate vs work around

- **Upstream bug confirmed in py-clob-client-v2 issues** → workaround locally, file/+1 the issue, don't waste time debugging your own code
- **Auth setup error** (signer/funder/sig_type/derivation context) → ALWAYS your code; the matrix above identifies it deterministically
- **On-chain revert with clear reason** → ALWAYS your account state (allowance, balance, approval, market state)
- **Empty revert / unknown** → check status.polymarket.com first; if green, file an issue with tx hash

---

## Reference

- `polymarket-v2-auth` — auth model decision tree (read first if you don't know what `signature_type` to use)
- `polymarket-v2-connector` — addresses, domains, raw signing
- `web3-onchain-prep` — wrap/approve from FUNDER
- `systematic-debugging` — general flow when this matrix doesn't cover your case
- `Scripts/poly_verify_funder.py` — automated funder check (catches the deposit-address mistake)
- https://github.com/Polymarket/py-clob-client-v2/issues — live bug tracker (check before debugging)
- https://status.polymarket.com — platform status
