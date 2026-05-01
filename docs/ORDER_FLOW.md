# 📋 ORDER FLOW — постановка и снятие ордеров

**Phase 9lll (PR #51, 30.04.2026)** — после внесения top-of-book depth, preflight, revert, derive_api_creds, reconcile fetcher + `/api/circuit_breakers`.

Документ описывает полный путь от обнаружения арб-окна до закрытия / отката ордеров. Цель — иметь **одну точку правды** при отладке real-mode.

---

## Содержание

1. [Высокоуровневый sequence](#1-высокоуровневый-sequence)
2. [Детект арб-окна](#2-детект-арб-окна)
3. [Pre-flight gates](#3-pre-flight-gates)
4. [Постановка ордера (Polymarket)](#4-постановка-ордера-polymarket)
5. [Подтверждение fill'а (user-channel WS + dead-man)](#5-подтверждение-filla)
6. [Snapshot тестирование (slippage check)](#6-snapshot-тестирование)
7. [Снятие ордера (cancel / cancel_all)](#7-снятие-ордера)
8. [Revert: что делать когда арб поломан](#8-revert)
9. [Position reconcile (background loop)](#9-position-reconcile)
10. [Killswitch: emergency stop](#10-killswitch)
11. [Аутентификация: L1 vs L2, ключи](#11-аутентификация-l1-vs-l2-ключи)

---

## 1. Высокоуровневый sequence

```
┌──── Polymarket gamma-api / clob-api ──────┐
│                                            │
│  GET /markets/active   → eval_poly()      │  ◀── фоновый scan каждые ~3с
│  GET /book?token_id    → _fetch_clob()    │
│       (top-of-book depth → liquidity)     │
│                                            │
└──────────────┬─────────────────────────────┘
               │  обнаружено окно: sum < threshold
               ▼
        ┌──────────────────┐
        │  build_deal()    │  ◀── REAL_OB_SOURCES guard, sizing,
        │                  │      payout calc, grade, risk
        └────────┬─────────┘
                 │
                 ▼
        ┌──────────────────────────────┐
        │  fire_arb(deal, wallets)     │
        │                              │
        │  1. _assign_wallets()        │  ◀── 1 нога = 1 кошелёк
        │  2. risk gate                │  ◀── kill, daily limit, paused
        │  3. preflight_arb()          │  ◀── depth+balance+allowance
        │  4. paper trading grad gate  │  ◀── live mode only
        │  5. parallel POST per leg    │  ◀── jitter 0-50ms anti-detect
        └────────┬─────────────────────┘
                 │
                 ▼
        ┌──────────────────────────────┐
        │  fills.registry.event.wait() │  ◀── deadman 5s, push <250ms
        │                              │
        │  user-channel WS push:       │
        │  • MATCHED → set event       │
        │  • CONFIRMED → final         │
        └────────┬─────────────────────┘
                 │
       ┌─────────┴─────────┐
       ▼                   ▼
   все filled?       partial / fail?
   ↓                  ↓
   schedule_realistic_eval  revert_filled_legs() ──→ SELL FOK
   (paper trade row)        + log aborted_reason
```

---

## 2. Детект арб-окна

### Источники цен (`arb_server.py`)
- **Polymarket**: `GET /book?token_id={id}` → `_fetch_clob` (top-of-book depth, **PR #51**).
- **Kalshi**: `GET /markets/{ticker}/orderbook` → `_fetch_kalshi_ob` (also top-of-book).
- **SX Bet**: `GET /orders?marketHashes=...` (maker book) → `_fetch_sx_orders` (top-of-book taker depth).
- **Limitless**: `GET /markets/{slug}/orderbook` → `_fetch_limitless_orderbook` (already top-of-book since Phase 9aa).
- **WebSocket**: `wss://ws-subscriptions-clob.polymarket.com/ws/market` (poly_ws.py) — асинхронные обновления orderbook'а для NEAR/HOT кандидатов; **NOT used for fire** (REAL_OB_SOURCES strict guard в `build_deal`).

### Структуры арбитража
- **A · ALL YES**: Σ price(YES_i) < threshold ⇒ покупаем YES каждого outcome.
- **B · ALL NO**: Σ price(NO_i) < (N-1) · threshold ⇒ покупаем NO каждого outcome (для N≥3).
- **C · YES+NO PAIR**: price(YES_X) + price(NO_X) < threshold ⇒ только бинарный per-market.

`threshold` для Polymarket динамический: `1 - (taker_fee_bps/10000 + slippage_reserve + safety_buffer)`. Сейчас `slippage_reserve=0.003`, `safety_buffer=0`. Для матча с `fee_bps=270` threshold=0.97 ровно.

### REAL_OB_SOURCES guard (PR #43, 30.04.2026)
В `build_deal` стрelка:
```python
REAL_OB_SOURCES = {'clob_ask', 'kalshi_ob', 'sx_ob', 'lim_clob'}
for o in outcomes:
    if o.get('source') not in REAL_OB_SOURCES: return None
    if not (o.get('liquidity') or 0) > 0: return None
```
Только живой REST CLOB фетч проходит. WS-prices, `implied`/`mid` (lastTradePrice fallback) — суппрессированы.

---

## 3. Pre-flight gates

В `executor/atomic.py::fire_arb` цепочка проверок перед эмиссией ордеров:

| # | Gate | Действие при fail |
|---|---|---|
| 1 | `_assign_wallets(legs_count, wallets)` | в live mode возвращает `[]` если кошельков < ног → `aborted_reason='wallet_assignment_failed'`; в dry-run pad'ится mock'ами |
| 2 | `risk.check_can_fire(deal)` | `aborted_reason='risk_blocked: <kill / daily limit / paused / etc>'` |
| 3 | `preflight.preflight_arb(deal, assigned)` (**PR #51**) | проверки на леге: `check_depth`, `check_balance`, `check_allowance`. Failures → `aborted_reason='preflight_failed: <reasons>'` |
| 4 | `paper_trading.graduation_status()` (live only) | `aborted_reason='graduation_gate: <blockers>'` |

После всех gate — `ThreadPoolExecutor.submit(_delayed_leg_fn)` параллельно с jitter 0-50мс между ногами.

---

## 4. Постановка ордера (Polymarket)

### 4.1 Сигнатура EIP-712 V2

`Scripts/executor/builders.py::build_poly_order(token_id, side, price, size_usdc, wallet, *, neg_risk, fee_rate_bps, expiration_secs, order_type, tick_size, min_order_size_usdc)`.

**Шаги:**
1. `assert side in ('BUY','SELL') and 0 < price < 1 and size >= min_order_size`.
2. `_round_to_tick(price, tick_size)` — V2 рынки enforce'ят tick (typical 0.01, реже 0.001 / 0.005).
3. Construct unsigned `Order` struct:
   ```
   {salt, maker, signer, tokenId, makerAmount, takerAmount,
    side, signatureType, timestamp(ms), metadata, builder}
   ```
   - `salt` = uuid4() как uint256 (V2 dropped `nonce`)
   - `timestamp` = `int(time.time()*1000)` (V2 unique key)
   - `metadata`/`builder` = `ZERO_BYTES32` (no app metadata, no Builder Program — solo trader → нулевая attribution)
   - `makerAmount` = `size_usdc * 1e6` (USDC 6 decimals)
   - `takerAmount` = `(size_usdc/price) * 1e6` (CTF outcome tokens 6 decimals)
4. Pick EIP-712 domain:
   - **Standard market**: `verifyingContract = 0xE111180000d2663C0091e4f400237545B87B996B`
   - **NegRisk market**: `verifyingContract = 0xe2222d279d744050d28e00520010520000310F59`
5. `_sign_poly_eip712(order, neg_risk, private_key)` — `eth_account.encode_typed_data` → `Account.sign_message`. Возвращает hex signature.
6. Wrap в API body:
   ```python
   {
     'order': {**order, 'signature': sig},
     'owner': wallet.eth_address,
     'orderType': order_type,            # GTC | GTD | FOK
     # 'expiration' отдельно если GTD
   }
   ```

### 4.2 Order types (Polymarket V2)

| Type | Поведение | Когда используем |
|---|---|---|
| **GTC** (Good-Till-Cancelled) | висит в book до fill / cancel | default для арб-нгог при NEAR→HOT переходе |
| **GTD** (Good-Till-Date) | как GTC, но автоматически expires в `expiration_secs` | если хотим time-bounded ордер (не использовали ещё) |
| **FOK** (Fill-Or-Kill) | либо весь стейк fill'ится at-or-better, либо целиком отменяется | **revert SELL** (PR #51 — приоритет: получить флэт, а не цену) |

### 4.3 POST `/order`

**Endpoint:** `POST https://clob.polymarket.com/order`. Single route для standard + negRisk (server роутит по signature domain). Headers: только `Content-Type: application/json` для POST (creates orders нужны только L1 EIP-712 signature внутри body, **L2 HMAC headers здесь не требуются**).

**Response shape:**
```json
{
  "id": "0xORDERID...",
  "status": "ACCEPTED" | "REJECTED",
  "createdAt": "..."
}
```

`atomic._fire_one_leg_live` извлекает `order_id` (или `orderId`/`order.id`/`order_hash`), регистрирует в `fills.registry`.

### 4.4 Sizing per leg (build_deal, arb_server.py:991)

```
contracts_per_leg = balance / total_price          # equal payouts
stake_X = balance * (price_X / total_price)
```

Балансировка: `actual_balance = BALANCE * scale_factor`, где
```
scale_factor = min(1.0,
                   min_liq / max_theoretical_stake,        # depth gate
                   _RISK_PER_TRADE_CAP / (BALANCE * max_share))  # $55 cap
```

**После PR #51:** `min_liq` теперь top-of-book → реалистичная пропускная способность. Это уменьшает stakes для неликвидных рынков, но защищает от phantom partial-fill'ов.

---

## 5. Подтверждение fill'а

### 5.1 fills.registry — двухкатушечный механизм

```python
reg = fills.registry.register(
    arb_id, leg_idx, platform='polymarket',
    slug=cond_id, order_id=order_id,
)
filled = reg.event.wait(timeout=DEADMAN_TIMEOUT_S)   # 5s
```

`reg.event` — `threading.Event`. Когда WS handler получает MATCHED событие для `order_id` — вызывает `registry.consume_by_order_id(...)` → `event.set()` → `wait` возвращается с `True` за <250 мс.

### 5.2 User-channel WebSocket

`Scripts/poly_user_ws.py`:
- URL: `wss://ws-subscriptions-clob.polymarket.com/ws/user`
- Auth: первое сообщение после open — `{"auth": {"apiKey","secret","passphrase"}, "markets": [...], "type": "user"}`. Без L2 кредов (PR #51 derive script) WS не аутентифицируется → atomic ждёт 5с deadman вместо <250 мс.
- Inbound:
  - `trade` event с `status=MATCHED` → push в `fills.registry.consume_by_order_id`
  - `order` events (placement/update/cancel) — логируем, не latch'имся
- Per-bot: 1 instance per кошелёк (каждый bot имеет свои L2 creds).

### 5.3 Slippage gate

```python
if abs(fill_price - expected_price) > SLIPPAGE_TOLERANCE (0.001):
    log.warning("slippage exceeded")
```

Сейчас это warning, не cancel. **TODO Phase 9mmm**: добавить cancel + revert на превышение. Это менее критично сейчас потому что:
1. Polymarket V2 limit orders fill'ятся at-or-better than limit price (мы submit at `expected_price` exactly).
2. Если depth < stake — partial fill (не slippage а недо-fill) → handled separately.

---

## 6. Snapshot тестирование

`dryrun_log.schedule_realistic_eval(result, deal, delay_s=5)`:
- через 5с после dry-fire переoпрашивает `_fetch_clob` для каждого token_id
- сравнивает текущую цену с тем что рейдар думал
- пишет в `Executions/dryrun.jsonl` realistic_pnl + drift
- Phase 5 калибровка: накопить ≥100 paper-trades, проверить win_rate ≥70%, drift <20% — graduation gate

---

## 7. Снятие ордера

### 7.1 Single cancel (`build_poly_cancel`)

`DELETE https://clob.polymarket.com/order/{order_id}` с **L2 HMAC headers**:

```
POLY_ADDRESS:    {wallet.eth_address}
POLY_TIMESTAMP:  {int(time.time())}
POLY_API_KEY:    {wallet.poly_api_key}
POLY_PASSPHRASE: {wallet.poly_passphrase}
POLY_SIGNATURE:  base64(hmac_sha256(secret, timestamp + 'DELETE' + path + ''))
```

Без L2 кредов (которые выдаются `poly_derive_api_creds.py --bot bot{N}`) — 401.

### 7.2 Cancel all (`build_poly_cancel_all`)

`DELETE https://clob.polymarket.com/orders` — отменяет ВСЕ open ордера на кошельке. Используется в watchdog/killswitch path.

### 7.3 Когда мы cancel'им

| Ситуация | Действие |
|---|---|
| Dead-man timeout (5с без MATCHED) | cancel этой ноги, mark `status='timeout'` |
| Slippage exceeded | currently log warning (TODO: cancel) |
| Partial fill в одной ноге | revert (см. §8) — этой ноге не cancel'им (она filled), но если SELL FOK не помог — cancel остатка |
| Killswitch trip (`Executions/.killed`) | watchdog process делает `cancel_all` для всех кошельков |
| Reconcile mismatch | trip killswitch → cancel_all через watchdog |

---

## 8. Revert

### 8.1 Когда триггер'ится

В `atomic.fire_arb` после параллельного fire'а:

```python
arb_broken = bool(partial_legs) or (failed_legs and filled_legs)
if arb_broken:
    revert_filled_legs(result, deal, assigned, dry_run=dry_run)
```

Условия:
- `partial_legs` (SX Bet matched < requested)
- ИЛИ есть `failed_legs` ('rejected'/'timeout'/'cancelled'/'disabled') И есть `filled_legs`

### 8.2 Что делает revert (PR #51)

Для каждой filled ноги:
- **dry-run**: `dryrun_log.log_order_decision(op='revert_sell', body={'side':'SELL'})` — paper-trade audit видит что мы бы продали.
- **live (Polymarket)**:
  ```python
  build_poly_order(
      token_id, side='SELL',
      price=expected_price - 0.01,   # 1c worse for guaranteed sweep of bids
      size_usdc=fill_size,
      order_type='FOK',              # fill-or-kill: get flat fast
      ...
  )
  ```
  POST с timeout 2с. Status code 200/201/202 → `'sold'`, иначе `'sell_HTTP_<code>'`.
- **TODO** для SX Bet (taker-fill на opposite outcome) и Limitless (тот же путь что Polymarket — добавим в Phase 9mmm).

`aborted_reason` теперь содержит сводку: `'arb_broken: partial=X failed=Y filled=Z, shortfalls=[...], reverts=[i:status, ...]'`

---

## 9. Position reconcile

`Scripts/risk/reconcile.py` — фоновый thread, период 60с:

1. `_read_local_positions()` парсит `Executions/positions.jsonl` → `{(platform, market, outcome): size_usdc}`.
2. Каждый registered fetcher (`_exchange_fetchers` list) вызывается, результат merge'ится в `remote`.
3. `_diff_positions(local, remote, tolerance=0.01)` → mismatches list.
4. Если mismatches непуст или fetcher ошибся → `killswitch.kill(reason='reconcile_mismatch: ...')` → halt.

**PR #51:** добавили `fetch_polymarket_positions(wallets)`:
- iterates wallets с `has_poly_creds`
- `GET /data/positions` с L2 HMAC
- парсит `[{conditionId, outcome, shares, avgPrice}, ...]` → `{('Polymarket', cond, outcome): shares*price}`

`register_polymarket_fetcher(wallets)` подключает в reconcile loop. Без креденциалов (Phase 4 не выкачена) — фетчер пропускает кошелёк silently, `_exchange_fetchers` остаётся пустым → reconcile heartbeat'ит `'skipped'` (не fail).

---

## 10. Killswitch

`Scripts/risk/killswitch.py` — файл-флаг `Executions/.killed`. Если файл существует — `is_killed()` возвращает True, executor блокирует fire.

Создаётся:
- через `POST /api/kill` (UI confirm modal: 2-step click)
- автоматически при reconcile mismatch
- через watchdog при reconnect failure 3+ раз подряд
- оператором руками: `touch Executions/.killed`

Снимается только: `POST /api/risk_resume` (UI button) или `rm Executions/.killed`.

Watchdog (`Scripts/watchdog.py`) — отдельный systemd unit / process:
- Polls `.killed` каждую секунду.
- Если main process крашнулся — watchdog всё равно делает cancel_all через прямые API-вызовы для всех ботов с L2 креденциалами.

---

## 11. Аутентификация: L1 vs L2, ключи

### L1 — EIP-712 signature with private key
**Что подписывает:** Order struct (для `POST /order`), ClobAuth message (для derive_api_creds).

**Хранится:** `Credentials.env`:
```
BOT1_ETH_ADDRESS=0x...
BOT1_PRIVATE_KEY=0x...
... до bot6
```

`Scripts/wallets/stores.py::LocalEnvStore` читает только при `sign()` вызове. Никогда не логируется. В `.gitignore`.

**Достаточно для:** `POST /order` (sign-and-submit).

### L2 — HMAC headers (POLY_API_KEY/SECRET/PASSPHRASE)
**Что хешится:** `timestamp + METHOD + path + body` через `hmac_sha256(secret_base64_decoded, prehash)`, base64-encoded.

**Хранится:** также `Credentials.env` после `poly_derive_api_creds.py --bot bot{N}`:
```
BOT1_POLY_API_KEY=...
BOT1_POLY_SECRET=...
BOT1_POLY_PASSPHRASE=...
```

**Нужно для:**
- `DELETE /order/{id}` (cancel single)
- `DELETE /orders` (cancel all)
- `GET /data/positions` (reconcile fetcher)
- WebSocket auth для user-channel `wss://ws-subscriptions-clob.polymarket.com/ws/user` (push fills)

### On-chain prep (`polymarket_approve.py`)
Один раз per кошелёк, 3 транзакции на Polygon:
1. `wrap()` USDC.e → pUSD на `CollateralOnramp` (V2 collateral)
2. `approve(pUSD, exchange_v2)` MAX_UINT256
3. `setApprovalForAll(CTF_1155, exchange_v2)` = true

После — биржа физически может списать pUSD и переместить outcome токены.

### Pre-real-mode чек-лист
1. ☐ `python Scripts/polymarket_approve.py --bot botN` per кошелёк (×6)
2. ☐ `python Scripts/poly_derive_api_creds.py --bot botN` per кошелёк (×6)
3. ☐ pUSD баланс ≥ MAX_PER_TRADE × N_legs на каждом боте
4. ☐ (опц) `POLYGON_RPC_URL=https://polygon-mainnet.g.alchemy.com/...` в `Credentials.env`
5. ☐ `register_polymarket_fetcher(wallets)` вызван в startup → reconcile активен
6. ☐ ≥100 paper-trades в `paper_results.jsonl` с win_rate ≥ 70% — `paper_trading.graduation_status().ready == True`
7. ☐ smoke_test.sh: `/api/circuit_breakers` 200, `/api/risk_status` healthy, `/api/wallets` count=6
8. ☐ kill switch testing: trip + resume
9. ☐ DRY_RUN=0 + первые 10 сделок по $5/нога — финальная калибровка
10. ☐ → DRY_RUN=0, full size $55/нога

---

## Refs

- [BUG_CATALOG.md](../BUG_CATALOG.md) — Phase 9lll #51 entries (5.X через 5.U)
- [CHANGELOG.md](../CHANGELOG.md) — PR #51 detailed
- [Scripts/executor/builders.py](../Scripts/executor/builders.py) — все billder functions с inline docs
- [Scripts/executor/atomic.py](../Scripts/executor/atomic.py) — fire_arb + revert_filled_legs
- [Scripts/preflight.py](../Scripts/preflight.py) — PR #51, новый
- [Scripts/poly_derive_api_creds.py](../Scripts/poly_derive_api_creds.py) — PR #51, новый
- [Scripts/risk/reconcile.py](../Scripts/risk/reconcile.py) — fetch_polymarket_positions
- Polymarket docs: https://docs.polymarket.com/developers/CLOB/
- py-clob-client (reference, не используем): https://github.com/Polymarket/py-clob-client
