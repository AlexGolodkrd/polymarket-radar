# Polymarket Trading (V2 CLOB)

**Created Phase 11 (01.05.2026)** — when work touched real-mode signed orders, cancels, fills.

## What this is for

Use when modifying:
- `Scripts/executor/builders.py::build_poly_order` — EIP-712 V2 Order signing
- `Scripts/executor/builders.py::build_poly_cancel*` — DELETE /order/{id} with L2 HMAC
- `Scripts/poly_user_ws.py` — user-channel WebSocket with auth
- `Scripts/poly_derive_api_creds.py` — one-time L2 creds derivation
- `Scripts/polymarket_approve.py` — on-chain wrap/approve

## V2 vs V1 — what changed (verified 28.04.2026)

| Old (V1) | New (V2) | Why it matters |
|---|---|---|
| `nonce` field in Order struct | `salt` (uuid4 as uint256) + `timestamp` (ms) | uniqueness shifted; salt is per-order, not per-account |
| `expiration` in Order | `expiration` only in body for GTD; not signed | server enforces from body for GTD |
| `feeRateBps` in Order | dynamic per-market via `getClobMarketInfo(conditionID)` | NOT a signed field anymore |
| `taker` in Order | dropped (always zero) | server fills with anyone |
| Single CTF Exchange contract | TWO domains: standard + negRisk | EIP-712 verifyingContract differs; server routes by signature |
| USDC.e collateral | **pUSD** (Polymarket USD) | one-time on-chain wrap via CollateralOnramp |
| Builder Program via HMAC headers | `builder` bytes32 in signed Order | for solo trader → ZERO_BYTES32 (no extra fees) |

## EIP-712 domains

```
Standard: name='Polymarket CTF Exchange', v=2, chainId=137,
          verifyingContract=0xE111180000d2663C0091e4f400237545B87B996B
NegRisk:  name='Polymarket Neg Risk CTF Exchange', v=2, chainId=137,
          verifyingContract=0xe2222d279d744050d28e00520010520000310F59
```

`market.negRisk` (from gamma-api response) decides which domain. WRONG domain = signature rejected silently.

## V2 Order struct (11 fields)

```
{
    salt:           uint256 (uuid4().int)
    maker:          address  (= signer for EOA)
    signer:         address
    tokenId:        uint256  (CTF outcome token)
    makerAmount:    uint256  (USDC raw, 6 decimals)
    takerAmount:    uint256  (CTF tokens raw, 6 decimals)
    side:           uint8    (0=BUY, 1=SELL)
    signatureType:  uint8    (0=EOA)
    timestamp:      uint256  (ms — server uses for uniqueness, NOT seconds)
    metadata:       bytes32  (zero for solo)
    builder:        bytes32  (zero for solo — see Builder Program below)
}
```

`signature` is added to body wrapper, NOT signed inside Order itself.

## Order types (POST /order body wrapper)

```python
{
    'order': {**order, 'signature': sig},
    'owner': wallet.eth_address,
    'orderType': 'GTC' | 'GTD' | 'FOK',
    # 'expiration': str(int(time.time()) + N) only if orderType='GTD'
}
```

- **GTC** — default for arb entry. Order rests in book until fill/cancel.
- **GTD** — auto-expires at given Unix timestamp. Use for time-bounded fills.
- **FOK** — fill-or-kill (entire stake at-or-better OR entire reject). Used for **revert SELL** path (atomic.py::revert_filled_legs) — priority is getting flat, not exact price.

## Tick size enforcement

Polymarket V2 enforces per-market tick (typical 0.01, sometimes 0.001 or 0.005). Submitting a misaligned price → 400 error. `_round_to_tick(price, tick_size)` snaps before signing. tick_size comes from `_attach_poly_v2_meta(deal, rough)` which calls `getClobMarketInfo(conditionID)`.

## Two-level auth

| Level | What | Need for |
|---|---|---|
| **L1** = EIP-712 signature with private_key | sign Order struct, sign ClobAuth message | `POST /order` |
| **L2** = HMAC headers (POLY_API_KEY/SECRET/PASSPHRASE) | hmac_sha256(secret, ts+METHOD+path+body), base64-url | `DELETE /order/{id}`, `DELETE /orders`, `GET /data/positions`, user-channel WS auth |

L2 derived ONCE per wallet via `Scripts/poly_derive_api_creds.py --bot bot{N}`:
1. Sign ClobAuth message (EIP-712 with domain `name='ClobAuthDomain'`, chainId=137)
2. GET `/auth/derive-api-key` (or POST `/auth/api-key` if 404)
3. Server returns `{api_key, secret, passphrase}` → store in Credentials.env

## ClobAuth message format

```
domain = {name: 'ClobAuthDomain', version: '1', chainId: 137}
types.ClobAuth = [
    {name: 'address', type: 'address'},
    {name: 'timestamp', type: 'string'},
    {name: 'nonce', type: 'uint256'},
    {name: 'message', type: 'string'},
]
message = {
    address: eth_address,
    timestamp: str(int(time.time())),
    nonce: 0,
    message: 'This message attests that I control the given wallet',
}
```

Server response: `{api_key, secret, passphrase}`. `secret` is base64-url-encoded — decode before HMAC.

## L2 HMAC headers (per request)

```
prehash = f"{ts}{METHOD.upper()}{path}{body or ''}"
sig = base64.urlsafe_b64encode(
    hmac.new(base64.urlsafe_b64decode(secret), prehash.encode(), hashlib.sha256).digest()
).decode('ascii')
headers = {
    'POLY_ADDRESS': eth_address,
    'POLY_TIMESTAMP': str(ts),
    'POLY_API_KEY': api_key,
    'POLY_PASSPHRASE': passphrase,
    'POLY_SIGNATURE': sig,
    'Content-Type': 'application/json',
}
```

## User-channel WebSocket (push fills <250ms)

```
URL: wss://ws-subscriptions-clob.polymarket.com/ws/user
First message after open:
  {auth: {apiKey, secret, passphrase}, markets: [conditionId, ...], type: "user"}
```

Inbound `trade` events with `status=MATCHED` → push to fills.registry → atomic.fire_arb wakes from `event.wait(deadman_s)` in <250ms instead of 5s deadman.

## Builder Program — DON'T register for solo

Builder Program is for apps/aggregators routing external user flow — they CHARGE additional taker fee (up to 100bps) on top of platform fee. For a solo trader:
- Default `builder=ZERO_BYTES32` → no attribution → no extra fees
- Registering would only ADD cost, no rebate

Source: `docs.polymarket.com/builders/{overview,tiers,fees}`.

## Common gotchas

1. **Wrong domain (standard vs negRisk)** — silent rejection. Always check `market.negRisk` from gamma-api before signing.
2. **`timestamp` in seconds instead of ms** — server treats as duplicate (V2 uses ms uniqueness). Use `int(time.time() * 1000)`.
3. **Tick mismatch** — 400 with cryptic error. `_round_to_tick(price, tick_size)` MANDATORY.
4. **L2 HMAC with `secret` not base64-decoded** — invalid signature. py-clob-client convention is `base64.urlsafe_b64decode(secret)` then HMAC.
5. **POST /order with L2 headers** — works, but redundant. POST is L1-authenticated via signature in body. L2 headers ONLY needed for DELETE/GET data.
6. **Builder Program HMAC headers (V1 leftover)** — deprecated. V2 has `builder` field IN the signed Order struct (bytes32). Don't add `POLY_BUILDER_*` headers; they'll be ignored or rejected.

## Reference implementations

- `Scripts/executor/builders.py` — pure-function builders, no I/O, unit-testable
- `Scripts/executor/atomic.py::_fire_one_leg_live` — POST + fills.registry wait
- `Scripts/poly_user_ws.py` — user-channel WS with HMAC auth
- `Scripts/poly_derive_api_creds.py` — L2 deriving CLI

## See also

- `polymarket-query` skill — public read-only APIs (gamma, clob, data)
- `web3-onchain-prep` skill — pUSD wrap, approve, allowance
- `secrets-management` skill — Credentials.env handling
- `BUG_CATALOG.md` 5.X-5.U — Phase 10 #51 entries
- `docs/ORDER_FLOW.md` — full place/cancel/identify flow

---

## May 2026 docs verification (02.05.2026)

Прошлась по docs.polymarket.com и community sources, чтобы зафиксировать актуальное состояние перед DRY_RUN=0:

### Rate limits per endpoint (Cloudflare-throttled, queue not reject)
- `gamma-api/events`: **500 req/10s** (50 RPS)
- `gamma-api/markets`: 300 req/10s
- `gamma-api/search`: 350 req/10s
- `gamma-api` global: 4,000 req/10s
- `clob-api/book`: **1,500 req/10s** (150 RPS) — наш main bottleneck в Polymarket processing
- `clob-api/order` (POST/DELETE): не задокументировано, эмпирически spike-tolerant (`5,500 req/min` для submit observed)

Все 3 биржи: при превышении Cloudflare **queues** запросы (latency растёт), не reject'ит. **HTTP 429 = hard limit** только когда queue exhausted. **HTTP 503 = Cloudflare edge transient**.

### V1 deprecation
- **V2 launched 28.04.2026 ~11:00 UTC.** Все V1 ордера wiped, V1 контракты отключены.
- Любая bot using `py-clob-client` < V2 версии или `@polymarket/clob-client` < V2 будет получать "signature mismatch" / "version error" — backward compat **ZERO**.
- Наш код в `executor/builders.py:53-115` использует V2 типы корректно.

### Ghost fills mitigation
- V1 баг: атакующий тратил $0.10 чтобы invalidate losing orders через `incrementNonce()`, очищая MM books на десятки тысяч.
- V2: nonce удалён → этот вектор закрыт. Builder attribution теперь в подписи, не HMAC.
- **Off-chain/on-chain settlement gap всё ещё существует** (Polymarket admits it). При signing с metadata (timestamp/builder) — следить за settlement timing на крупных fills.

### Polymarket — НЕ "pUSDt"
Пользователь спрашивал про "pUSDt". На самом деле торговый токен — **pUSD** (Polymarket USD), 6 decimals, backed 1:1 by USDC. Не USDT-связанный, не stablecoin-вариация — просто rename. Адрес: `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB` (env override `POLY_PUSD_ADDRESS` если governance меняет).

### Onramp.wrap signature — pending verification
Один из community источников (research agent 02.05.2026) сообщил что `CollateralOnramp.wrap` принимает `(assetAddress, recipient, amount)` — 3 аргумента. Наш ABI в `polymarket_approve.py:127` использует `wrap(amount)` — 1 аргумент. **Не подтверждено** — нужна верификация на Polygonscan перед DRY_RUN=0. Не критично сейчас (мы в dry-run), но добавлено в pre-real-mode чек-лист.
