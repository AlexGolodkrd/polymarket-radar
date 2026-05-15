# Limitless Exchange Trading (V2)

Updated 2026-05-15 against live-verified Limitless V2 behavior (api.limitless.exchange, end-to-end probe placed real order + cancelled). The earlier version of this skill described V1 conventions that no longer match the server.

## What this is

Limitless Exchange is a CLOB prediction market on Base (chainId 8453). Architecture mirrors Polymarket: YES/NO outcome shares, USDC collateral, EIP-712 signed orders, **negRisk groups** for multi-outcome events.

## API endpoints (V2)

```
Base: https://api.limitless.exchange/

POST   /orders                          → place new order (HMAC + EIP-712 sig)
DELETE /orders/{orderId}                → single cancel (HMAC)
POST   /orders/cancel-batch             → batch cancel
GET    /markets/{addressOrSlug}         → market metadata incl. venue.exchange
GET    /markets/{slug}/user-orders      → authenticated user's open orders
GET    /profiles/{address}              → profile incl. id (= ownerId) + rank.feeRateBps
GET    /portfolio/positions             → authenticated user's filled positions
```

## Auth (HMAC, NOT X-API-Key)

Legacy `X-API-Key: <token>` returns 401 on V2. The current scheme:

```
ts  = ISO-8601 timestamp (ms precision)
msg = ts + "\n" + METHOD + "\n" + path?query + "\n" + body_string
sig = base64(HMAC-SHA256(base64_decode(secret), msg))

headers:
  lmts-api-key:   <token id from limitless.exchange API keys UI>
  lmts-timestamp: <ts>
  lmts-signature: <sig>
```

`body_string` for POST = the EXACT bytes you send. JSON re-serialization between sign-time and send-time breaks the sig.

See `limitless-hmac-auth` SKILL.md for the helper + common pitfalls.

## EIP-712 typed data

```js
domain = {
  name: "Limitless CTF Exchange",
  version: "1",
  chainId: 8453,
  verifyingContract: "<PER-MARKET — fetch from /markets/{slug}.venue.exchange>"
};

types.Order = [
  { name: "salt",          type: "uint256" },
  { name: "maker",         type: "address" },
  { name: "signer",        type: "address" },
  { name: "taker",         type: "address" },     // 0x0 for public orders
  { name: "tokenId",       type: "uint256" },
  { name: "makerAmount",   type: "uint256" },
  { name: "takerAmount",   type: "uint256" },
  { name: "expiration",    type: "uint256" },     // sign as 0n
  { name: "nonce",         type: "uint256" },
  { name: "feeRateBps",    type: "uint256" },
  { name: "side",          type: "uint8" },        // 0 = BUY, 1 = SELL
  { name: "signatureType", type: "uint8" }        // EOA = 0
];

primaryType: "Order"
```

## Verified V2 quirks (each was a live bug)

### 1. `salt` must fit in Postgres BIGINT (int64)

`uint256` in the EIP-712 type, but server stores as `BIGINT` in Postgres. Max ~9.2e18. **Use 7 random bytes** (max ~7.2e16, well inside).

Wrong salt = `"value '...' is out of range for type bigint"`.

### 2. Mixed numeric/string JSON serialization

Server validators reject string variants of bounded numeric fields:

| Field | JSON type |
|---|---|
| `makerAmount`, `takerAmount`, `nonce`, `feeRateBps` | **Number** (fits in 2^53) |
| `tokenId`, `salt` | **String** (uint256 doesn't fit Number) |
| `expiration` | **String** "0" |

Wrong = `"makerAmount must be a number conforming to the specified constraints"`.

### 3. `expiration: 0n` (signed value), `"0"` in body

Server: `"Order expiration is not currently supported. Please sign orders without expiration."` Keep the field in the EIP-712 type (contract requirement), sign with `0n`.

### 4. `feeRateBps` matches the wallet's rank

Bronze = 300 bps; higher ranks lower. Wrong fee = `"feeRateBps[0] is out of user's band"`.

Fetch from `GET /profiles/{address}.rank.feeRateBps`. Env override `LIMITLESS_FEE_RATE_BPS` for ops.

### 5. Tick-snap `takerAmount` (contracts) to multiple of 1000

`price × contracts_wei` must be integer in 1e6 USDC units. For 0.001-tick markets (Limitless default), snap contracts DOWN to a multiple of 1000.

Wrong = `"Order amounts tick violation: price(0.56) * contracts(1785714) = 999999.84 is not a whole (integer) number. Use contracts ending with price tick size zeros (3), e.g. price=0.56, contracts=1785000, collateral=999600"`.

### 6. `verifyingContract` is per-market

Fetch via `GET /markets/{slug}.venue.exchange`. NegRisk family (EPL/Serie A football markets) shares `0xe3E00BA3a9888d1DE4834269f62ac008b4BB5C47`. Other categories use different exchanges. Wrong = `"Invalid signature. Exchange address for this market: 0x..."` (server kindly returns the right address in the error body).

### 7. `price` lives INSIDE the `order` object

NOT at body top-level. Missing = `"GTC order must have a price"`.

### 8. `ownerId` is required + CF rate-limits the lookup

Resolve from `GET /profiles/{address}.id`. Cache aggressively in-process. CF can ban the IP for repeated requests (Error 1015). Use `LIMITLESS_OWNER_ID` env override + negative cache for 5min on CF 429.

### 9. Response shape — `order.id` is nested

```json
{
  "order": { "id": "abb148e8-...", "status": "LIVE", ... },
  "execution": { "settlementStatus": "UNMATCHED", "txHash": "0x..." }
}
```

Read `body.order?.id`, NOT legacy `body.id`. The legacy path returns `undefined` in V2 → executor marks leg rejected even though the order placed → ghost orders sit LIVE on the book unnoticed.

## POST body shape (final, working)

```json
{
  "order": {
    "salt": "<7-byte random decimal>",
    "maker": "0xWALLET...",
    "signer": "0xWALLET...",
    "taker": "0x0000000000000000000000000000000000000000",
    "tokenId": "<uint256 decimal>",
    "makerAmount": 999600,
    "takerAmount": 1785000,
    "expiration": "0",
    "nonce": 0,
    "feeRateBps": 300,
    "side": 0,
    "signatureType": 0,
    "signature": "0x...",
    "price": 0.56
  },
  "orderType": "GTC",
  "marketSlug": "brentford-1777798810485",
  "ownerId": 1338965
}
```

## On-chain prerequisites

USDC must be approved to the market's `venue.exchange` contract. NegRisk family shares one exchange — single approve covers the family. Easiest: place a $1 manual buy via limitless.exchange UI; MetaMask prompts approve. Allowance error: `"Insufficient collateral allowance for this order."`

## Reconciliation

After every POST, atomic.ts logs `[lim-place-resp] arbId=X leg=Y status=Z order_id=W settlement=S`. If order_id is present but executor saw the leg as rejected → ghost order. Cancel via `DELETE /orders/{id}` immediately.

## CP arb context

Limitless YES leg pairs with SX NO leg for the binary cross-platform arb. See `project_cp_arb_strategy.md` in memory for the mental model and worked example.

## Reference

- `Scripts/executor/builders.py::build_limitless_order` — Python builder
- `executor-ts/src/builders/limitless.ts` — TS builder (canonical)
- `executor-ts/src/lib/limitless_profile.ts` — ownerId + venue.exchange resolver with cache
- `executor-ts/src/lib/limitless_hmac.ts` — HMAC sign helper
