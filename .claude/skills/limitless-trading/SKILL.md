# Limitless Exchange Trading

**Created Phase 12 (01.05.2026)** — for Limitless Exchange (Base L2) CLOB.

## What this is

Limitless Exchange is a **CLOB-based prediction market** on **Base** (Coinbase L2, EVM). Architecture mirrors Polymarket: YES/NO outcome shares, $1 collateral, EIP-712 signed orders, negRisk-style multi-outcome groups.

Volume: ~$3M/day vs Polymarket's $110M/day. **Less competition** → wider spreads → more arb opportunity per LoC, but smaller depth → smaller stakes.

## API endpoints

```
Base: https://api.limitless.exchange/

GET  /markets                    → list of all markets
GET  /markets/{slug}             → single market detail
GET  /markets/{slug}/orderbook   → asks/bids per token
GET  /events                     → multi-outcome event groups (negRisk)

POST /orders                     → place new order (signed EIP-712)
DELETE /orders/{orderId}         → single cancel
POST /orders/cancel-batch        → batch cancel  body: {orderIds: [...]}
DELETE /orders/all/{slug}        → cancel every order on this market

WebSocket: Socket.IO namespace "/markets" — book updates + fill confirmations
```

X-API-Key header required for /orders and /orders/* endpoints. Issued per account at registration.

## EIP-712 signing

Different domain from Polymarket:
```
domain = {
  name: "Limitless CTF Exchange",
  version: "1",
  chainId: 8453,                  // Base mainnet
  verifyingContract: 0xC5d563A36AE78145C45a50134d48A1215220f80a  // default; override per market
}
types.Order = [
  salt(u256), maker(addr), signer(addr), taker(addr),
  tokenId(u256), makerAmount(u256), takerAmount(u256),
  expiration(u256), nonce(u256), feeRateBps(u256),
  side(u8), signatureType(u8)
]
```

Note: Limitless V1 still has `nonce` and `expiration` IN the signed Order struct (unlike Polymarket V2 which dropped them). `taker` field also still in Limitless. Don't copy-paste from Polymarket builder.

## Reference: `Scripts/executor/builders.py::build_limitless_order`

Already implemented. Handles dry-run path (token_id optional) and real-mode (signs with `_sign_limitless_eip712`). EIP-712 domain/types are correct per `https://docs.limitless.exchange/developers/eip712-signing`.

## Cancel endpoints

3 flavours, all need X-API-Key:
- `DELETE /orders/{id}` — single
- `POST /orders/cancel-batch` body `{orderIds: [...]}` — batch
- `DELETE /orders/all/{slug}` — cancel everything on a market

`Scripts/executor/builders.py` has `build_limitless_cancel`, `build_limitless_cancel_batch`, `build_limitless_cancel_all_market`.

## WebSocket — Socket.IO (not raw WS like Polymarket)

```python
from socketio.client import Client
sio = Client()
sio.connect('https://api.limitless.exchange/markets', namespaces=['/markets'])
sio.emit('subscribe', {'slugs': [...]}, namespace='/markets')
```

Inbound events:
- `orderbook` — full book for one slug (snapshot)
- `orderbookUpdate` — incremental update
- `tradeFill` — fill notification with our orderId (if we placed it)

Already wrapped in `Scripts/limitless_ws.py`.

**Caveat:** Socket.IO 5.11 has known reconnect-loop bug with namespaces — pinned `<5.11` in requirements.txt. Don't upgrade.

## Cloudflare 403 issue

Limitless is behind Cloudflare with **adaptive challenge** for non-residential IPs. Our VPS hits 403 in bursts when scan concurrency spikes. Mitigations in `Scripts/async_fetchers.py::fetch_limitless_pages_async`:
- HTTP/2 multiplexing (40 streams in 1 TCP) → looks like fewer "connections"
- Retry-After header honoring → backs off when CF says
- Circuit breaker per-host with 3-failure threshold + 5-min cool-down

If CF tightens further: may need residential proxy or co-located VPS in `gcp-southamerica-east1` (Limitless serves there).

## Common gotchas

1. **`size` field is raw USDC (6 decimals)** for some endpoints, **scaled USD-like** for others. `_lim_depth_usd` heuristic normalizes (Phase 9aa). Verify per endpoint when adding new integration.

2. **Slug format** — Limitless slugs are kebab-case (`will-lakers-win-march-25`). When matching cross-platform, normalize (drop date suffix).

3. **negRisk groups** — `events` endpoint returns `markets[]` array. Each market has its own slug + token IDs. `eval_limitless` correctly walks both YES and NO.

4. **Per-market `tick_size` and `min_order_size`** — fetch from `/markets/{slug}` once, cache. Submit-time mismatch = 400.

5. **WebSocket reconnect loops** — emit `subscribe` AFTER `connect` event (not in connect handler — race condition). Already handled.

6. **API key expiry** — keys expire after 30 days inactivity. Re-issue manually via Limitless web UI when stale. We don't auto-renew.

7. **`takerAmount` rounding** — must be int(round(...)). Some integrations use truncate which causes "amount mismatch" error 422.

## Audit checklist (Phase 12 PR)

- [ ] `_fetch_limitless_orderbook` returns top-of-book depth (Phase 10 #51)? — verify
- [ ] WebSocket subscriptions don't leak on reconnect — verify `Scripts/limitless_ws.py`
- [ ] Circuit breaker on 403/429 — verify `Scripts/circuit_breaker.py` config
- [ ] `_lim_depth_usd` heuristic — sample 5 real markets, check normalization
- [ ] EIP-712 signing — verify against test vector from `docs.limitless.exchange`
- [ ] Cancel paths return correct method (DELETE vs POST batch)
- [ ] Slug normalization for cross-platform matching — see `event-matching-fuzzy` skill
- [ ] approval flow — does Limitless need `setApprovalForAll(CTF, exchange)` like Polymarket? Verify via `docs.limitless.exchange/approve`

## See also

- `Scripts/executor/builders.py` — `build_limitless_*` family
- `Scripts/limitless_ws.py` — Socket.IO client
- `Scripts/limitless_approve.py` — on-chain prep (Base USDC approve)
- `Scripts/async_fetchers.py::fetch_limitless_pages_async` — HTTP/2 multiplex
- BUG_CATALOG entries for Limitless: §6.1 (CF 403), §1.1 (Leeds-Burnley phantom)

---

## May 2026 docs verification (02.05.2026)

### Status: all configuration correct
Прошлась по docs.limitless.exchange, GitHub limitless-labs-group/*, basescan.org для верификации:

| Параметр | Verified | Соответствует коду |
|---|---|---|
| Domain name | `Limitless CTF Exchange` | ✅ `builders.py:814` |
| Domain version | `1` (no V2 announced) | ✅ |
| Chain ID | `8453` (Base mainnet) | ✅ |
| Default exchange | `0xC5d563A36AE78145C45a50134d48A1215220f80a` | ✅ `builders.py:821`, `preflight.py:59` |
| USDC on Base | `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` | ✅ |
| API base | `api.limitless.exchange/api-v1` | ✅ |
| WS endpoint | `wss://ws.limitless.exchange/markets` (Socket.IO) | ✅ `limitless_ws.py` |

### Per-market venue в approval flow
Limitless использует **per-market CTF контракты** (ERC-1155), не один shared CTF как Polymarket. Каждый market имеет свой `venue.exchange`, который мы fetch'им через `_fetch_limitless_market_meta(slug)`.

**`limitless_approve.py` — комментарий line 42 устарел.** Скрипт принимает `--ctf-address` flag, но текущая логика предполагает один ctf-address per bot run. Для production нужно вызывать `limitless_approve.py --bot botN --ctf-address 0x<venue-exchange>` для **каждого** market venue который собираемся торговать. Альтернатива — fetch всех distinct venues после первого main scan и approve батчем.

### Rate limits
- **Per-connection at >40 concurrent.** Наш `_get_client('limitless')` в `async_fetchers.py:69` использует HTTP/2 с `max_connections=2` → server видит 1 client, лимит не триггерится.
- WS subscriptions: max 250 в текущем `LIMITLESS_WS_MAX_SUBS` env.

### Fees
- Maker/taker: **0% on-chain** (volume-based, не per-trade в API).
- Gas на Base: ~$0.01 per leg.
- Наш `compute_lim_threshold` использует 0.5% buffer как safety — приемлемо.

### V2 migration — НЕ объявлена
В отличие от Polymarket, Limitless **не анонсировал V2 миграцию**. Нет breaking changes в API в 2026.
