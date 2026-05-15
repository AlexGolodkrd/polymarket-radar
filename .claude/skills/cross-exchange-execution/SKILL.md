# Cross-Exchange Execution — Built-Dict Contract & Per-Exchange Quirks

**Создан 02.05.2026.** Пара к `cross-platform-arbs` (которая про **детекцию**) — этот скил про **исполнительный слой**: как `fire_arb` оборачивает любую сделку (включая X1/X2 cross-platform) в единый interface через **built dict**, который понимает `atomic._fire_one_leg`.

## When to use

При работе с любыми из:
- `Scripts/executor/builders.py::build_*_order` — добавляешь новую биржу или переделываешь старую
- `Scripts/executor/atomic.py::_fire_one_leg`, `_cancel_leg_order`, `revert_filled_legs` — меняешь общий pipeline
- `Scripts/executor/bot_connector.py::place_order` — внешним ботам plain-API
- Реализуешь maker-mode для биржи, которая ещё не в `Scripts/executor/builders.py::build_poly_maker_order`

## Built-dict contract (общий для всех бирж)

Любой `build_*_order(...)` ВОЗВРАЩАЕТ dict с фиксированным набором ключей. `atomic._fire_one_leg` НЕ знает про конкретную биржу — он читает только built dict.

| Ключ | Тип | Семантика |
|---|---|---|
| `platform` | str | `'polymarket'` \| `'sx_bet'` \| `'limitless'` \| `'kalshi'` (lowercase) |
| `body` | dict\|None | JSON-тело для `requests.post(url, json=body)`. None для disabled (Kalshi). |
| `sign_payload` | bytes\|None | Детерминированный JSON подписанного объекта (для аудит-лога / тестов). None если не подписано. |
| `would_post_url` | str\|None | Куда уйдёт POST (или DELETE для cancel). None если no-op. |
| `expected_price` | float | Цена, которую радар увидел в орбукe и из неё посчитал арб. Сравнить с fill_price → slippage. |
| `expected_size_usdc` | float | USD номинал. |
| `signed` | bool | Подписано ли реально EIP-712? (если False — мы в dry-run / нет private_key). |
| `partial_fill` | bool (опц.) | Только SX Bet: маркер что сматчили < expected. |

**Дополнительные опциональные ключи** (читаются только теми, кому нужно):
- `neg_risk` (Polymarket): bool — для cancel надо знать домен.
- `eip712` (Polymarket): `{domain, primaryType, types}` — для off-chain аудита.
- `order` (Polymarket, неподписанный для дебага).
- `verifying_contract` (Limitless): per-market адрес Exchange.
- `slug` (Limitless): URL-идентификатор маркета (для cancel-batch).

## Поток исполнения (пер-leg)

```
deal['entries'][i]
       │
       ▼
atomic._build_leg(deal, i, wallet)        ← перевод entry → call to builders
       │
       ▼
builders.build_<platform>_order(...)      ← платформо-специфичный build
       │     returns built dict
       ▼
atomic._fire_one_leg(built, ...)
       │
       ├─ if dry_run:  log_decision; return LegResult('dry-fired')
       │
       ├─ POST built['body'] → built['would_post_url']
       │     │ if 200: parse fill price
       │     │ else:   LegResult('rejected', error=resp)
       │     ▼
       │     monitor fill via WS / polling
       │
       └─ if fill_ok:  _write_position_row(deal, i, leg, wallet)
              else:    revert flow if partial fill landed elsewhere
```

`atomic.fire_arb` запускает все legs **параллельно** через `ThreadPoolExecutor`. Если одна leg landed, а другая упала → `revert_filled_legs` продаёт landed leg.

## Per-exchange quirks

### Polymarket V2 (Polygon, chainId 137)

- **Две EIP-712 домена** (standard 0xE111... vs negRisk 0xe2222...) — выбор по `market.negRisk`.
- **Order struct V2** — 11 полей, **без** `feeRateBps`/`nonce`/`expiration`/`taker`. Подробности → `polymarket-v2-connector/SKILL.md`.
- **Collateral pUSD**, не USDC.e. Пользователь депонирует USDC.e → `Onramp.wrap()` → pUSD.
- **`POST /order`** — единственный URL для standard и negRisk; сервер роутит по domain.
- **L2 HMAC** для cancel/positions/user-WS. Получить через `poly_derive_api_creds.py` (один раз per bot).
- **Maker mode реализован** в `build_poly_maker_order` (1 tick inside spread, fallback to taker если spread < tick).

### SX Bet (SX Network, chainId 4162) — v2 protocol (verified live 2026-05-15)

- **Maker-fill only**. Мы taker; server подбирает makers, мы НЕ передаём `orderHashes`.
- **`POST https://api.sx.bet/orders/fill/v2`** — endpoint (**no `/v1` prefix**, старые пути 404).
- **EIP-712 v2 — domain.name = "SX Bet"** (НЕ "SX Bet Order Fill"), `verifyingContract = 0x845a2Da2D70fEDe8474b1C8518200798c60aC364` (`EIP712FillHasher` из `/metadata`).
- **Nested Details/FillObject types** — `Details { action, market, betting, stake, worstOdds, worstReturning, fills: FillObject }`. User-visible поля = `"N/A"`, реальные данные в `fills`.
- **`desiredOdds = taker_price × 1e20`** (НЕ `(1 - taker_price)`). Это implied probability стороны taker'а. Обратная сторона = `NO_MATCHING_ORDERS`.
- **`body.market` = REAL marketHash**, не `"N/A"` (docs example ввёл в заблуждение).
- **`fillSalt` — uint256 decimal string**, не hex.
- **Outcome 1 vs 2** через `isTakerBettingOutcomeOne: bool`. SX UI labels outcome 2 by opposite-team name (e.g. "Crystal Palace" в Brentford-CP market = binary NO = (CP win OR Draw)). Это БИНАРНЫЙ market, не 3-way. См. `project_cp_arb_strategy.md` в memory.
- **Partial fills**: server возвращает `data.totalFilled < stakeWei` → executor читает `totalFilled` (или `fillAmount`), отмечает leg filled ТОЛЬКО при > 0 (иначе ghost fill semantics).
- **USDC allowance** — на `TokenTransferProxy=0x38aef22152BC8965bf0af7Cf53586e4b0C4E9936` через сеть SX Rollup 4162. Manual $1 ставка через sx.bet UI триггерит approve popup.

### Limitless V2 (Base L2, chainId 8453) — verified live 2026-05-15

- **EIP-712 Order type не менялся** (salt, maker, signer, taker, tokenId, makerAmount, takerAmount, expiration, nonce, feeRateBps, side, signatureType). Но server-side валидаторы V2 строже.
- **`POST https://api.limitless.exchange/orders`** — body shape см. ниже.
- **HMAC-SHA256 auth** (НЕ plain X-API-Key — legacy путь 401-ит). См. `limitless-hmac-auth` skill.
- **`order.price` — внутри `order`**, не на body top-level. Server: `"GTC order must have a price"`.
- **`order.expiration: 0n`** — server: `"Order expiration is not currently supported. Please sign orders without expiration."` Контракт всё ещё ожидает поле, но 0 = sentinel.
- **`order.feeRateBps` matches rank** (Bronze=300). `LIMITLESS_FEE_RATE_BPS` env override. Server: `"feeRateBps[0] is out of user's band"`.
- **`order.salt` — 7-byte random** (fits Postgres int64). 128-bit overflow = `"value '...' is out of range for type bigint"`.
- **Numeric JSON serialization** для `makerAmount`/`takerAmount`/`nonce`/`feeRateBps` (Number, not string). `tokenId`/`salt` остаются строками — uint256 не лезет в JS Number.
- **Tick-snap `contractsWei`** к multiple of 1000 (для 0.001-tick markets, дефолт Limitless). `price × contracts_wei` должен быть integer в 1e6 USDC. Server: `"Order amounts tick violation: ..."`.
- **`verifyingContract` per-market** — из `GET /markets/{slug}.venue.exchange`. NegRisk family (EPL/Serie A) — `0xe3E00BA3a9888d1DE4834269f62ac008b4BB5C47`. Каждая семья своя.
- **`ownerId` — обязательно** в body top-level. Numeric profile id из `GET /profiles/{address}.id`. CF rate-limits — кэшировать + env override `LIMITLESS_OWNER_ID`.
- **Response shape — nested**: `{ order: { id, status }, execution: { settlementStatus, txHash } }`. Reading legacy top-level `body.id` = undefined → ghost orders. Read `body.order?.id` first.
- **USDC allowance** — на venue.exchange контракт для market'а. Approve через UI на любом маркете семьи покроет всю семью (NegRisk shared exchange).

### Kalshi — DISABLED, не активируется

US-only, требует KYC + 1099. `ENABLE_KALSHI=0` всегда. `kalshi-markets` skill удалён.

## Deal dict shape (что `fire_arb` принимает)

```python
{
  'title':           str,                # человекочитаемое название
  'platform':        str,                # 'Polymarket'|'SX Bet'|'Limitless'|'Polymarket+Limitless'
  'arb_structure':   str,                # 'all_yes'|'all_no'|'yes_no_pair'|'binary'|'cross_platform'
  'sum_cents':       float,              # суммарная цена (для select_fire_mode)
  'payout_target':   float,              # ожидаемый payout, обычно 1.0
  'entries': [                           # one per leg
    {
      'price':       float,              # цена которую видели на скане
      'stake':       float,              # USD номинал
      'side':        'BUY'|'SELL',
      # platform-specific:
      'token_id':    str,                # Polymarket
      'condition_id':str,                # Polymarket (V2 metadata)
      'neg_risk':    bool,               # Polymarket
      'tick_size':   float,              # Polymarket
      'market_hash': str,                # SX Bet (или на deal level)
      'outcome_index': 1|2,              # SX Bet
      'slug':        str,                # Limitless
      'verifying_contract': str,         # Limitless (per-market)
      'accepting_orders': bool,          # gate from filter_poly
      'enable_order_book': bool,         # gate from filter_poly
    }, ...
  ],
  # Cross-platform extras (Phase 13):
  'cp_legs': [{'platform': str, ...}, ...],  # raw PlatformOutcome refs
  'cp_kind': 'X1'|'X2',                  # YES_a+NO_b vs NO_a+YES_b
}
```

## Кросс-платформенный leg dispatch (Phase 13+)

Для cross-platform deal `entries` содержит legs **разных** платформ. `_build_leg(deal, i, wallet)` выбирает правильный builder по `entry['platform']` (а не `deal['platform']`, потому что deal-level platform = `'Polymarket+Limitless'`).

В текущей реализации (Phase 14b) cross-platform handling добавлен в `_build_leg` через ветку:
```python
if entry.get('platform'):           # explicit per-leg override
    platform = entry['platform']
```

При добавлении новой биржи → дополнить и эту ветку, и `BotConnector._build_entry`.

## BotConnector (single-leg API)

Для внешних ботов (gabagool, copy-trade, single-directional) есть `executor/bot_connector.py`:
```python
from executor.bot_connector import BotConnector
conn = BotConnector(wallets=pool, dry_run=True)
res = conn.place_order(
    platform='Polymarket', market_id='123...', side='BUY',
    price=0.45, size=10.0, wallet_id='bot1', neg_risk=False,
)
# res = {'status':'dry-fired', 'fill_price':None, 'arb_id':...}
```

Внутри строит синтетический `deal` с `arb_structure='binary'`, одной leg, `payout_target` пересчитан под $1 outcome — и зовёт `fire_arb`. Все защиты (preflight, killswitch, position log, dry-run gate) работают одинаково.

## Adding a new exchange — checklist

1. **Builder** в `executor/builders.py::build_<exchange>_order(...)` возвращающий built dict с обязательными ключами.
2. **EIP-712 / signing** через `_sign_<exchange>_*` — возвращает `Optional[str]` (None при ошибке, не raise).
3. **Domain constants** — `<EXCHANGE>_DOMAIN`, `<EXCHANGE>_TYPES`, chainId, верифицированный verifyingContract.
4. **Cancel builder** — `build_<exchange>_cancel(order_id, wallet)` если биржа поддерживает (Polymarket/Limitless да, SX через "противоположный fill" — нет cancel'а).
5. **Position fetcher** в `risk/reconcile.py::fetch_<exchange>_positions(wallets)` для reconcile loop.
6. **Approve script** в `Scripts/<exchange>_approve.py` — wrap/approve/setApprovalForAll.
7. **`_build_leg`** в `atomic.py` — добавить новую ветку `if platform == '<Exchange>'`.
8. **`_write_position_row`** — обновить `market_id` extraction (где у этой биржи живёт уникальный ID).
9. **Tests** — unit для builder + integration для `_build_leg`.
10. **Skill** — отдельный SKILL.md типа `polymarket-v2-connector` с адресами/доменами/шаблонами.

## Связанные файлы и скилы

- `Scripts/executor/builders.py` — все builder'ы.
- `Scripts/executor/atomic.py` — общий pipeline.
- `Scripts/executor/bot_connector.py` — plain-API для внешних ботов.
- `polymarket-v2-connector/SKILL.md` — Polymarket V2 reference.
- `polymarket-trading/SKILL.md` — narrative по Polymarket.
- `sx-bet-trading/SKILL.md` — SX OrderFill детали.
- `limitless-trading/SKILL.md` — Limitless V1 + Base.
- `cross-platform-arbs/SKILL.md` — детекция X1/X2 пар.
- `maker-taker-orders/SKILL.md` — maker/taker policy.
- `web3-onchain-prep/SKILL.md` — wrap/approve паттерны.
