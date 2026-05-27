# Platform drift audit — 27.05.2026

> Автоматизированный аудит `executor-ts/` + `Scripts/` против всех breaking changes Polymarket / SX Bet / Limitless за период 20.04 → 27.05.2026.
>
> Запускался: 27.05.2026. Provider: background agent с WebSearch + file reads.

## TL;DR

| Платформа | Статус | Главные риски |
|---|---|---|
| **Polymarket V2** | 95% покрыто | DELETE /order URL mismatch между TS и Python — один путь даст 404 |
| **SX Bet** | 90% покрыто | `chainId: 4162` hardcoded — если Rollup migration уже произошла, signatures invalid |
| **Limitless** | 95% покрыто | `feeRateBps=300` для всех ботов — bot с rank > Bronze получит 401 |

---

## 1. Polymarket V2 (cutover 28.04.2026)

### Покрыто (verified в коде)

| Изменение | Файл / строки | Подтверждение |
|---|---|---|
| EIP-712 domain `version: "2"` | `executor-ts/src/types/eip712.ts:18,25` | exact для Standard + NegRisk |
| `verifyingContract Standard 0xE111180000d2663C0091e4f400237545B87B996B` | `types/eip712.ts:20` | ✓ |
| `verifyingContract NegRisk 0xe2222d279d744050d28e00520010520000310F59` | `types/eip712.ts:27` | ✓ |
| Order struct V2: `salt, maker, signer, tokenId, makerAmount, takerAmount, side, signatureType, timestamp, metadata, builder` | `types/eip712.ts:40-54` | **`nonce`, `feeRateBps`, `taker`, `expiration` отсутствуют** ✓ |
| `timestamp` в миллисекундах (`BigInt(Date.now())`) | `builders/poly.ts:171` | ✓ |
| `metadata` / `builder` = `bytes32(0)` | `builders/poly.ts:172-173` | ✓ |
| `signatureType` (EOA / Magic Proxy / Gnosis Safe) | `types/wallet.ts:23`, `builders/poly.ts:156-160` | ✓ |
| `maker = funder` для type 1/2 | `builders/poly.ts:160` через `effectiveFunder(wallet)` | ✓ |
| pUSD collateral на стороне approve | `Scripts/polymarket_approve.py:70-72` | адрес env-override через `POLY_PUSD_ADDRESS` |
| CollateralOnramp wrap USDC.e → pUSD | `Scripts/polymarket_approve.py:77-80` | адрес `0x93070a847efEf7F70739046A929D47a521F5B8ee` ✓ |
| feeSchedule (изм 31.03.2026) с fallback на legacy | `Scripts/arb_server.py:1842-1947` | ✓ |
| L2 HMAC headers | `lib/poly_hmac.ts:64-111` | URL-safe base64, EIP-55 ✓ |
| User-channel WS auth | `ws/poly_user_ws.ts:35` | плоский WS, не Ably |

### Не покрыто / требует проверки

1. **🔴 DELETE /order path mismatch (TS vs Python).**
   - Python `Scripts/executor/builders.py:362`: `DELETE /order/{order_id}`
   - TS `executor-ts/src/fire/poly_post.ts:116`: `DELETE /order` + body `{orderID: orderId}`
   - Один из двух путей даст 404. Критично для timeout-cleanup в `atomic.fireLeg`.
   - **Action**: проверить через `curl -XDELETE` к live API какой работает; синхронизировать.

2. **🟡 `py-clob-client` legacy упоминания (некритично).**
   - Прямых импортов в коде НЕТ (grep пустой).
   - Упоминания только в комментариях + mypy override.
   - `requirements.txt` НЕ содержит. Гэп закрыт.

3. **🟡 `/markets/keyset` (лимит ≤100 с 14.05.2026).**
   - Не используется. Радар Polymarket ходит на `gamma-api.polymarket.com/events?...&limit=500&offset=...`
   - Это разные endpoints (gamma-api ≠ CLOB).
   - Когда Polymarket потушит legacy offset на gamma-api — сломается discovery.
   - **Action**: мониторить, добавить keyset pagination когда придёт время.

---

## 2. SX Bet

### Покрыто

| Изменение | Файл / строки |
|---|---|
| OrderFill **v2** protocol (server-side matching) | `builders/sx.ts:1-15`, `fire/sx_post.ts:9` |
| URL `/orders/fill/v2` (без `/v1/`) | `types/eip712.ts:145` |
| Domain `name: "SX Bet"` (было "SX Bet Order Fill"), `version: "6.0"` | `types/eip712.ts:114-119` |
| `verifyingContract = EIP712FillHasher 0x845a2Da2D70fEDe8474b1C8518200798c60aC364` | `types/eip712.ts:118` |
| Nested `Details + FillObject` EIP-712 types | `types/eip712.ts:121-143` |
| `desiredOdds = taker_price × 1e20` | `builders/sx.ts:221-232` |
| `body.market = real marketHash` (не "N/A") | `builders/sx.ts:240-256` |
| `chainId: 4162` (SX Network mainnet) | `types/eip712.ts:117` |
| `baseToken` USDC `0x6629Ce1Cf35Cc1329ebB4F63202F3f197b3F050B` | `types/eip712.ts:149-150` |
| **Ably зависимость:** grep пустой в executor-ts/ и Scripts/ | паттерн отсутствует ✓ |

### Не покрыто / риски

1. **🔴 SX Rollup migration на Arbitrum Orbit (snapshot 15.05.2026).**
   - `chainId: 4162` hardcoded в `types/eip712.ts:117`.
   - Если SX мигрировал — все EIP-712 подписи invalid с молчаливым 401/422.
   - **Нет env-override!**
   - **Action**: `curl https://api.sx.bet/metadata | jq '.data.networks'` — проверить актуальный chainId; добавить env-override.

2. **🟡 Ably deprecation 01.07.2026 — нерелевантна.**
   - В executor-ts SX WS отсутствует (только REST для optional pre-flight).
   - Будущий риск: если добавим SX user-channel.

3. **🟢 SX-токен дискретится 15.05.2026 — ноль риск.**
   - Мы taker, fill в USDC. SX-токен не торгуется.

---

## 3. Limitless

### Покрыто

| Изменение | Файл / строки |
|---|---|
| HMAC-SHA256 auth (lmts-api-key/timestamp/signature) для REST | `lib/limitless_hmac.ts:40-57` |
| HMAC для DELETE /orders/{id} | `fire/lim_post.ts:244-256` |
| HMAC для WS handshake (`/socket.io`) | `ws/limitless_user_ws.ts:231-244` + `Scripts/limitless_ws.py:284-308` |
| **salt** как int64 (7 random bytes) | `builders/limitless.ts:80-92` |
| **expiration = 0** (server не поддерживает) | `builders/limitless.ts:168-176` |
| Mixed encoding: makerAmount/takerAmount/nonce/feeRateBps как Number, expiration как String | `fire/lim_post.ts:121-131` |
| `price` ВНУТРИ order object (не top-level) | `builders/limitless.ts:212-223` |
| Tick-snap: `contracts_wei % 1000 == 0` | `builders/limitless.ts:146-154` |
| `feeRateBps` per-rank (env `LIMITLESS_FEE_RATE_BPS`, default 300 Bronze) | `builders/limitless.ts:111-119` |
| Парсинг response `order.id` (nested) | `fire/lim_post.ts:18-26` |
| Domain `Limitless CTF Exchange v1`, chainId 8453, verifyingContract per-market | `types/eip712.ts:66-90` |

### На что обращать внимание

1. **🟡 `feeRateBps=300` default — bot с rank > Bronze получит 401.**
   - Если оператор повысил rank — `LIMITLESS_FEE_RATE_BPS` env надо обновить per-bot.
   - Симптом: `"feeRateBps[0] is out of user's band"`.
   - **Action**: `GET /profiles/<address>` per bot, добавить `LIMITLESS_FEE_RATE_BPS_BOTn` env override.

2. **🟡 Legacy `X-API-Key` fallback** в `fire/lim_post.ts:140-141`.
   - В DRY_RUN=0 без `limitlessApiSecret` даст 401.

---

## Топ-5 рисков silent breakage при следующем деплое

1. **🔴 Polymarket DELETE cancel URL mismatch.** TS/Python расходятся в путях. Один даст 404, оставит order на книге. Проверить через `curl -XDELETE`.

2. **🔴 SX chainId hardcoded.** Snapshot был 15.05. Если уже мигрировал — все SX-leg подписи invalid. Проверить `GET https://api.sx.bet/metadata`.

3. **🟡 Limitless rank-dependent feeRateBps.** 300 для всех 6 ботов. Bot с Silver/Gold rank молча режектится.

4. **🟡 Polymarket gamma-api `limit=500` deprecation.** Legacy offset на discovery будет потушен — discovery опустеет. Срок неясен.

5. **🟡 `limitlessApiSecret` отсутствует у части ботов.** Если не залит для всех 6 — одни боты работают, другие 401-ят молча.

---

## Чек-лист для оператора ПЕРЕД `DRY_RUN=0`

### Polymarket
- [ ] `curl -XPOST https://clob.polymarket.com/order` с реальной V2 подписью одного бота → ожидать 2xx + `{order:{id, status:"LIVE"}}`
- [ ] `curl -XDELETE https://clob.polymarket.com/order/<id>` (path) И `curl -XDELETE https://clob.polymarket.com/order -d '{"orderID":"<id>"}'` (body) — узнать который работает, синхронизировать обе стороны
- [ ] PolygonScan: `pUSD.balanceOf(funder)` > 0 для каждого funder (6 wallets)
- [ ] PolygonScan: `pUSD.allowance(funder, 0xE111...996B)` ≈ `2^256-1` для Standard, для `0xe222...0F59` для NegRisk
- [ ] polymarket.com UI: войти Web3 каждым signer-ботом (type 1/2), убедиться что funder корректно резолвится
- [ ] env `POLY_PUSD_ADDRESS` совпадает с PolygonScan «Polymarket: USD»

### SX Bet
- [ ] `curl https://api.sx.bet/metadata | jq '.data.networks'` → актуальный chainId; если ≠ 4162 — **добавить env-override в `types/eip712.ts:117`**
- [ ] `curl https://api.sx.bet/metadata | jq '.data.addresses.<chainId>.EIP712FillHasher'` → сравнить с `0x845a2Da2D70fEDe8474b1C8518200798c60aC364`
- [ ] `curl https://api.sx.bet/metadata | jq '.data.addresses.<chainId>.USDC'` → сравнить с `0x6629Ce1Cf35Cc1329ebB4F63202F3f197b3F050B`
- [ ] Тестовый fill $1 на liquid market через `/fire DRY_RUN=0`, наблюдать сериализацию body

### Limitless
- [ ] Все 6 ботов: `LIMITLESS_API_KEY_BOTn` + `LIMITLESS_API_SECRET_BOTn` в `Credentials.env`
- [ ] `curl` `GET /profiles/<addr>` per bot → узнать rank.feeRateBps; обновить `LIMITLESS_FEE_RATE_BPS_BOTn` если ≠ 300
- [ ] BaseScan: `USDC.allowance(bot, 0xC5d563A36AE78145C45a50134d48A1215220f80a)` ≈ `2^256-1` для всех 6
- [ ] BaseScan: `USDC.balanceOf(bot)` ≥ запас под (max fire-size × N × 2 safety)
- [ ] Тестовый order: `POST /orders` с salt < 2^56, observed response `{order:{id,status}, execution:{...}}`

### Общее
- [ ] Residential proxy: `PROXY_URL_POLY`, `PROXY_URL_LIM` живы — `curl -x ... https://api.ipify.org`
- [ ] `grep -r 'ably\|Ably' executor-ts/ Scripts/` → пусто
- [ ] `grep -r 'py_clob_client\|py-clob-client' Scripts/ requirements.txt` → только в комментариях
- [ ] Логи 24h: нет recurring `"signature recovery failed"`, `"NO_MATCHING_ORDERS"`, `"feeRateBps is out of user's band"`
- [ ] `cd executor-ts && pnpm test` → green (golden EIP-712 vectors)

---

## Файлы проверены

- `executor-ts/src/builders/{poly,limitless,sx}.ts`
- `executor-ts/src/types/{eip712,wallet}.ts`
- `executor-ts/src/lib/{poly_hmac,limitless_hmac}.ts`
- `executor-ts/src/fire/{poly_post,lim_post,sx_post}.ts`
- `executor-ts/src/ws/{poly_user_ws,limitless_user_ws}.ts`
- `Scripts/executor/builders.py`
- `Scripts/polymarket_approve.py`
- `Scripts/preflight.py`
- `Scripts/async_fetchers.py`
- `requirements.txt`
- `pyproject.toml`
