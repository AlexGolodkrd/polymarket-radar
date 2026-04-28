# Arbitrage Bot System

## Описание проекта

Система автоматического арбитража на prediction-market площадках **Polymarket**, **Kalshi** и P2P-бирже ставок **SX Bet**.

Программа сканирует активные события на платформах, находит арбитражные окна (когда сумма реальных ask-цен по всем взаимоисключающим исходам < порога) и отображает их в real-time дашборде.

Каждый бот отвечает за один исход события. Все боты — **активные** (тейкер-ордера по рыночной цене).

---

## Архитектура системы

### Панель управления (Arbitrage Radar)
- Real-time UI дашборд на `http://localhost:5050`
- Live-обновление данных каждые 10 секунд
- Раскрытие карточки сделки по клику (не сбрасывается при обновлении)
- Цветовая индикация качества (A+/A/B/C/D/F) и риска (LOW/MED/HIGH/CRIT)
- Статистика: количество арбитражей, суммарный профит, средний ROI

### Бэкенд (Flask)
- Файл: `Scripts/arb_server.py`
- Основное сканирование (Main Scan): 300 событий Polymarket + 200 событий Kalshi + 200 рынков SX Bet = **700 событий**. Выполняется каждые 90 секунд (цикл ~35 сек).
- Сканирование в паузах (Pause Scan): теневой процесс, который собирает оставшиеся события с помощью пагинации, чтобы не тормозить UI.
- Микро-сканирование (Micro Scan): каждые 5 секунд "пингует" orderbook топовых кандидатов для мгновенного нахождения арбитража.
- Global batch CLOB/orderbook запросы (ThreadPoolExecutor, 40 workers).

---

## Критические фильтры

### 1. Взаимоисключаемость исходов (negRisk)

> **Главное правило:** Арбитраж возможен ТОЛЬКО на взаимоисключающих исходах, где ровно один исход побеждает.

**Polymarket:**
- Используется поле `negRisk` из API
- `negRisk = true` → исходы взаимоисключающие ✅ (арбитраж возможен)
- `negRisk = false` → исходы независимые ❌ (НЕ арбитраж)

**Kalshi:**
- Нет поля `negRisk`, используются эвристики:
  - Минимальная цена исхода: **≥ 5¢** (ниже — мусорные заявки)
  - Сумма цен: **от 50% до 93%** (ниже 50% = явно не покрывающий набор)
  - Хотя бы один исход **> 20¢** (есть реальный фаворит)

**SX Bet:**
- Мы берем **27 бинарных типов рынков** (`SX_BINARY_TYPES` в `arb_server.py`): Moneyline (type=226), Total Over/Under, Spread/Handicap, Period Totals/Spreads/Moneylines (Basketball/Hockey/Tennis/E-Sports/MMA), Draw No Bet (type=52), и т.п. Все они по определению бинарные и **исчерпывающие** (outcomeOne+outcomeTwo покрывают все варианты).
- **Не включаем** type=1 (soccer Moneyline 3-way: Team A win / Team B win / Draw) — он 3-way с draw, не бинарный. Для него нужен отдельный 3-way pipeline (отложено).

### 2. Deadline-события
Отфильтровываются события, где все исходы привязаны к дедлайнам ("by January", "before 2027"), т.к. они часто имеют зависимые исходы.

### 3. Минимальное количество исходов
Событие должно иметь ≥ 2 исходов с ценами от 0 до 1.

---

## Источники цен

### CLOB (Central Limit Order Book)

> **CLOB** — стакан ордеров с лимитными заявками. Содержит asks (продавцы) и bids (покупатели). Best ask — минимальная цена, по которой можно купить контракт прямо сейчас.

**Критически важно:** Система использует **реальные ASK-цены из CLOB orderbook**, а не implied probability (mid-price).

**Polymarket CLOB API:**
```
GET https://clob.polymarket.com/book?token_id={TOKEN_ID}
→ { "asks": [{"price": "0.62", "size": "500"}, ...], "bids": [...] }
```

**Kalshi Orderbook API:**
```
GET https://api.elections.kalshi.com/trade-api/v2/markets/{TICKER}/orderbook
→ { "orderbook_fp": { "yes_dollars": [["0.62", "1000.00"], ...] } }
```

**SX Bet Orderbook API:**
```
GET https://api.sx.bet/orders?marketHashes={MARKET_HASH}&maker=true
→ Возвращает лимитные ордера маркет-мейкеров.
```

> **Maker → Taker конверсия для SX Bet (важно!):** API отдаёт `percentageOdds` мейкера для стороны на которую он ставит. Таkер берёт ПРОТИВОПОЛОЖНУЮ сторону по цене `1 − maker_price`.
>
> Если `isMakerBettingOutcomeOne=True` → тэйкер может купить **outcomeTwo** за `1 − maker_price`.
> Если `isMakerBettingOutcomeOne=False` → тэйкер может купить **outcomeOne** за `1 − maker_price`.
>
> Best ask для тэйкера = `1 − max(maker_bid_на_противоположной_стороне)`. Реализовано в `_fetch_sx_orders` (Phase 1, PR #12).

---

## Боты

Все боты — **активные** (тейкер-ордера). Каждый бот отвечает ровно за **один исход** события и выставляет ставку на своей платформе.

**Ограничение по стакану:** Все боты ориентируют свой объём по наименьшему объёму стакана из всех исходов события. Если у одного исхода макс стакан $3, а у других $15 — все ставят по $2 (с запасом).

---

## Стратегия

Система мониторит live события и ищет арбитражные окна по **трём структурам** (Phase 1, PR #12):

### A. ALL_YES (классическая)
Σ YES_ask < THRESH. Покупаем YES на каждом исходе → победитель платит $1, гарантированный профит = $1 − Σ YES.

### B. ALL_NO (multi-outcome, N≥3)
Σ NO_ask < (N−1) · THRESH. Покупаем NO на каждом исходе → выигрывают N−1 NO (проигравшие исходы), гарантированный возврат = (N−1) − Σ NO.

### C. YES_NO_PAIR (per-market, бинарная нога)
Per-market: yes_ask + no_ask < THRESH. На каждом маркете покупаем и YES и NO — гарантированно выплата $1, профит = 1 − (yes+no).

**SX Bet:** все 3 структуры схлопываются в одну (рынки бинарные с outcomeOne/outcomeTwo) — помечаются как `binary`.

**Polymarket clobTokenIds:** API возвращает `[YES_token_id, NO_token_id]`. Радар захватывает оба и подписывается через WS на оба, чтобы пересчитывать B/C структуры в real-time.

Все 3 структуры считают **реальные ASK-цены** (CLOB/orderbook), а не implied probability.

---

## Комиссии платформ

### Polymarket
- **Taker Fee**: ~2.5% (с учетом 50% рибейта).
- **Порог входа**: **< 97¢**
- Строгое правило: Для сделок 95-97¢ ликвидность должна быть > $1000 и проскальзывание < 0.3%.

### Kalshi
- **Taker Fee**: ~7%
- **Порог входа**: **< 93¢**

### SX Bet
- **Taker Fee**: ~2%
- **Порог входа**: **< 97¢**

---

## Исполнение ставок

### Шаг 1 — Сканирование (CLOB batch)
1. Fetch 300 Poly + 200 Kalshi + 200 SX Bet (быстрая фаза).
2. Фильтр: negRisk=true (или эвристики), ≥2 исхода, rough sum < 98¢
3. Batch fetch orderbook'ов (40 потоков, ThreadPoolExecutor)
4. Расчёт реальной суммы ask-цен, комиссий, slippage

### Шаг 2 — Проверка ликвидности
Перед входом:
- Проверить объём стакана (ask depth) по каждому исходу
- Если объём любого из исходов недостаточен → **не входить**

### Шаг 3 — Атомарное исполнение (Phase 2, PR #13)

Реализовано в `Scripts/executor/`:
- `builders.py` — собирает платформо-специфичные order bodies (Polymarket EIP-712, SX Bet pre-signed, Kalshi disabled)
- `atomic.py` `fire_arb(deal, wallets)` — параллельный fire через ThreadPoolExecutor (target <100мс), per-order timeout 2с, dead-man switch 5с, реверсивный продаж partial fills
- `dryrun_log.py` — пишет каждое решение в `Executions/dryrun.jsonl`, через 5с после fire'а перепрашивает orderbook и пишет реалистичный fill в `Executions/paper_results.jsonl`
- `fills.py` — заглушка для WS user-channel listener'ов (полная реализация в Phase 4 когда появятся ключи)

**Phase 2 = dry-run mode по умолчанию.** Реальный POST к биржам отключён до тех пор, пока:
1. Phase 4 (PR #15) не обеспечит 6 ботов с приватными ключами
2. Phase 5 (PR #16) graduation gate не пройдёт: ≥70% win rate + ≤20% drift на 100 paper-trades

Anti-detection: одна нога арбитража = один кошелёк (round-robin coordinator). 6 ботов вместо одного, чтобы паттерн не выглядел как очевидный арб-бот.

### Шаг 4 — Контроль проскальзывания
- `SLIPPAGE_TOLERANCE = 0.001` (0.1¢) — если любой fill отличается от ожидаемой цены больше — cancel + revert
- Dead-man switch: за 5с не пришёл хотя бы один fill_confirmed → cancel all + revert
- Reversal: если арб сломан, продать legs которые успели заполниться по market-цене

### Phase 2 endpoints (radar)

- `GET /api/paper_stats?window=100` — rolling метрики dry-run сделок (win rate, drift, slippage, graduation_ready flag)
- `POST /api/dryfire {title}` — ручной dry-fire конкретной сделки (UI кнопка `🧪 Dry-fire`)

В дашборде в шапке — панель `paper: X% win · drift Y% · N/100`, обновляется каждые 10с.

---

## Risk management (Phase 3, PR #14)

Реализовано в `Scripts/risk/`:

- **`limits.py`** `check_can_fire(deal)` — единственная hot-path функция. Executor вызывает её перед каждым `fire_arb`. Возвращает `(allowed, reason)`. Проверки в порядке:
    1. kill switch активен
    2. cost > $55 per-trade cap
    3. paused (paused_until > now)
    4. pre-trade daily check: при worst-case loss этой сделки превысим ли -$35 за день
- **`state.py`** — single source of truth, persists в `Executions/risk_state.json` (atomic write). Daily counter ресетится в 00:00 UTC.
- **`killswitch.py`** — file-flag `Executions/.killed`. Watchdog-процесс (Phase 4) читает флаг каждую секунду — если main упал, всё равно отменит pending ордера.
- **`reconcile.py`** — каждые 60с сверяет local positions log с биржевыми /positions endpoint'ами. Расхождение > $0.01 → trip kill switch + лог mismatch'а в `Executions/reconcile.jsonl`.

Параметры (memory feedback `feedback_risk_params.md`):
| Параметр | Значение |
|---|---|
| MAX_PER_TRADE_USD | $55 |
| DAILY_LOSS_LIMIT_USD | $35 (resets 00:00 UTC) |
| LOSING_TRADES_PER_HOUR | 5 → 1h pause |
| Concurrent positions | без лимита |
| Repeat arbs per event | без лимита |

**Важные правила (от пользователя):**
- На паузе/kill **не закрывать** существующие позиции — только блокировать новые fire'ы
- Kill switch требует **двойного подтверждения** в UI (modal + window.confirm + server-side `confirm:'YES'` в body)
- При hourly pause существующие позиции продолжают жить, daily limit pause тоже не закрывает позиции

### Phase 3 endpoints (radar)

- `GET /api/risk_status` — snapshot daily P&L, paused, killed, last reconcile, лимиты. Поллится каждые 5с дашбордом.
- `POST /api/kill {confirm:'YES', reason}` — trip kill switch.
- `POST /api/risk_resume {confirm:'YES'}` — снять kill + любую активную паузу.

В шапке дашборда — панель `risk: $-12.50/-$35 · L2/5` (daily P&L vs limit, losing trades в часовом окне). Кнопка `🛑 STOP` справа, при killed превращается в `↺ RESUME`.

---

## Multi-bot wallet architecture (Phase 4, PR #15)

Реализовано в `Scripts/wallets/`:

- **`config.py`** — параметры: `BOT_COUNT=6`, `MIN_USDC_PER_BOT=$60`, `REBALANCE_LOW_USDC=$60`, `REBALANCE_HIGH_USDC=$200`, `REBALANCE_RESERVE_USDC=$130`, `REBALANCE_PAIR_COOLDOWN_S=3600` (1 час между rebalance одной пары).
- **`stores.py`** — три pluggable backend'а:
  - `LocalEnvStore` (по умолчанию) — читает `BOT{N}_ETH_ADDRESS`/`BOT{N}_PRIVATE_KEY` из `Credentials.env`. Ленивый импорт `eth-account` для подписи EIP-712.
  - `WindowsCredStore` — Windows Credential Manager через `pywin32` (skeleton, Phase 6).
  - `AwsSecretsStore` — AWS Secrets Manager через `boto3` (skeleton, Phase 6).
- **`coordinator.py`** — `assign_legs(pool, n_legs)`:
  - **Anti-detection:** одна нога арбитража = один кошелёк. С 6 ботами и типичными 2-5 ногами всегда хватает.
  - **Balance-aware:** пропускает ботов с USDC < $60.
  - Сортировка по lowest balance first — самые «пустые» боты получают throughput первыми (auto-rebalance подтянет деньги).
- **`rebalance.py`** — `auto_rebalance_check(pool, execute=False)`:
  - Сканирует пары `(low<$60, high>$200)`.
  - Предлагает transfer = `(high - $130) / 2` (чтобы оставить high с резервом, а low с 1.5× threshold).
  - Skip dust transfers < $5 (gas не стоит).
  - Per-pair cooldown 1 час против thrashing.
  - В Phase 4 transfer'ы НЕ исполняются (нет `POLYGON_RPC_URL` и приватных ключей) — пишутся `proposal_dryrun` строки в `Executions/rebalance.jsonl`. Phase 6 включит реальный USDC.transfer на Polygon.

### Phase 4 endpoints (radar)

- `GET /api/wallets` — состав пула: бот, eth_address, can_sign, usdc, store_name
- `GET /api/rebalance/proposals` — текущие rebalance proposals + история (последние 20 строк)

В шапке дашборда — кликабельная панель `wallets: 4/6 (3 can sign) · $850 pool`. По клику — алерт с детализацией всех ботов и текущими rebalance proposals.

### Конфигурация (`Credentials.env`)

```
WALLET_BACKEND=local          # local / windows_cred / aws
COLD_WALLET_ADDRESS=0x...
BOT1_ETH_ADDRESS=0x...
BOT1_PRIVATE_KEY=             # blank до Phase 5 graduation gate
... через BOT6
POLYGON_RPC_URL=              # Phase 6
```

Шаблон в `.env.example`. Радар работает с пустым пулом — executor падает на mock single-stub, dry-run всё равно пишет paper trades.

---

## Paper trading + graduation gate (Phase 5, PR #16)

Реализовано в `Scripts/paper_trading.py`. Phase 2 уже пишет `paper_results.jsonl` после каждого dry-fired арба (с realistic-fill через 5с). Phase 5 добавляет:

- **`graduation_status(window=100)`** — `GraduationStatus` объект с count, win_rate, mean_drift, blockers, ready flag.
- **`paper_distribution(window=500)`** — гистограмма P&L distribution (бины от <-$2 до >$5).
- **`graduation_history(days=14)`** — daily rolling win-rate / drift для time-series chart'а.
- **`first_real_trade_size_usdc(real_count)`** — для первых 10 реальных трейдов после graduation возвращает $5/нога (final calibration), потом `None` (= использовать полный stake из deal builder, capped Phase 3 на $55).

### Условия прохождения graduation gate (immutable)

| Условие | Порог |
|---|---|
| Минимум paper trades | 100 |
| Win rate (доля сделок с positive realistic_pnl_5s) | ≥ 70% |
| Mean drift (среднее `\|realistic - sim\|`) | ≤ 20% |

Все 3 условия должны выполняться → `graduation_ready: true` → можно флипать `DRY_RUN=0`.

### После graduation

1. **Первые 10 реальных трейдов** — leg size принудительно $5 (а не $55), независимо от deal builder. Это финальная калибровка vs реальный fill.
2. **После 10 трейдов** — full size, но в пределах Phase 3 risk limits ($55/trade, $35/day, 5 losing/h).

### Phase 5 endpoints

- `GET /api/graduation` — `{count, win_rate_pct, mean_drift, ready, blockers, next_threshold_hint, ...}`
- `GET /api/paper_distribution?window=500` — `{bins, counts, total}` для гистограммы
- `GET /api/graduation_history?days=14` — daily series `[{date, count, win_rate_pct, mean_drift_pct}]`

В дашборде клик по `paper:` панели в шапке → modal с детализацией: header (ready или blockers), gate status, distribution histogram (ASCII bars), 14-day history.

---

## VPS deployment (Phase 6, PR #17)

Контейнеризованная архитектура. Два сервиса в `docker-compose.yml`:

- **radar** — `python Scripts/arb_server.py`, порт 5050, healthcheck по `/api/risk_status`
- **watchdog** — `python Scripts/watchdog.py`, читает `Executions/.killed` каждую секунду; при kill-transition исполняет cancel-pending hooks (Phase 4 wires реальные cancel API; Phase 6 ships skeleton)

Оба маунтят `./Executions` как volume — state переживает рестарт.

### Образ

`Dockerfile`: `python:3.11-slim` base, ставит `requirements.txt`, копирует `Scripts/` + `tests/`, запускает под non-root user `radar`, healthcheck каждые 30с.

`.dockerignore` — исключает `Credentials.env`, `.git`, `Executions/`, `__pycache__`, `.venv` etc.

### `requirements.txt` обновлён

```
flask>=3.0
flask-cors
requests>=2.31
websocket-client>=1.7
eth-account>=0.11   # для подписи EIP-712 на Polymarket / SX Bet (Phase 4+)
# web3>=6.13         # для Polygon RPC (Phase 6 после POLYGON_RPC_URL)
# boto3>=1.34        # для AWS Secrets Manager backend
```

`eth-account` обязателен перед флипом `DRY_RUN=0` (Phase 5 graduation gate). `web3` и `boto3` закомментированы — включаются когда понадобятся (баланс reads, AWS Secrets).

### `deploy/README.md`

Пошаговая инструкция для **AWS us-east-2** (рекомендуется — рядом с Polymarket Polygon nodes, latency 5-15мс) и **DigitalOcean NYC** ($12/мес Basic Droplet).

Содержит:
- Стоимость: t4g.small Reserved $15/мес, Fargate $12/мес, DO $12/мес
- IAM Role + inline policy для AWS Secrets Manager
- SSH-туннель для доступа к дашборду (не публиковать :5050 на public IP)
- Operational checklist (бекап, kill, resume, логи)
- Latency budget per region

### Что готово в Phase 6 vs позже

| Готово | Доделается позже |
|---|---|
| Dockerfile, docker-compose, watchdog skeleton | Real cancel API в watchdog (Phase 4 wallet keys) |
| `eth-account` в requirements | Real `USDC.transfer()` в rebalance (требует POLYGON_RPC_URL) |
| AWS IAM template в README | `AwsSecretsStore.addresses()/sign()` реальная реализация |
| Healthcheck + restart policy | WindowsCredStore production wiring |

---

## SX Bet executor finalization (Phase 7, PR #18)

Phase 2 ship'нул `build_sx_order` как skeleton (поле `orderHashes: None`). Phase 7 доделывает реальный matching через live `/orders` endpoint.

### Flow исполнения SX-ноги

1. **Fetch live `/orders?marketHashes=X&maker=true`** — `fetch_sx_matchable_orders()`
2. **Filter** — оставить только мейкеров на **противоположной** стороне (taker на outcome 1 фильтрует maker'ов с `isMakerBettingOutcomeOne=False`)
3. **Sort** по best taker price (lowest first) — чем выше maker_pct, тем дешевле тейкеру
4. **Greedy match** — `match_sx_orders()` берёт ордера сверху вниз пока не покроет `size_usdc`. Останавливается:
   - При покрытии полного размера → full fill
   - Если следующий ордер дороже `taker_price + slippage_tolerance` (default 0.5¢) → cap, partial fill
   - Если кончились matchable ордера → partial fill
5. **Build POST body** с массивами `orderHashes[]` + `takerAmounts[]` ready to sign
6. **Sign EIP-712** (когда есть приватник, Phase 4+)
7. **POST `/orders/fill`** (когда `DRY_RUN=0`, Phase 5+)

### Partial-fill handling — критическое правило

Если **любая нога** арба partial-fill'нулась — `fire_arb` помечает арб aborted с reason `partial_fill_arb_broken: ...`. Логика: одна нога без покрытия = арб больше не арб (один исход без позиции). В real-mode (Phase 5+) executor должен реверснуть filled ноги по market-цене.

В dry-run такие арбы **не идут** в `paper_results.jsonl` (skip realistic-eval) — graduation gate видит реальную картину, не считает phantom wins.

### Ключевые поля в `sx_match` блоке (per leg)

| Поле | Описание |
|---|---|
| `avg_fill_price` | weighted average taker price across matched orders |
| `best_price` / `worst_price` | spread внутри одного fill'а |
| `filled_usdc` | сколько реально покрылось |
| `shortfall_usdc` | size − filled (0 если full match) |
| `partial_fill` | bool flag |
| `matched_orders` / `available_orders` | сколько ордеров взяли vs сколько было всего |
| `slippage_cap` / `max_taker_price_accepted` | slippage config |

Все эти поля идут в `Executions/dryrun.jsonl` per-leg row, и top-level `arb` row содержит `partial_leg_count` + `worst_partial_shortfall_usdc` для аналитики.

### Тесты (17 новых, **86 всего**)

`tests/test_sx_executor.py`:
- Opposite-side filter (taker_outcome=1 ↔ maker_outcome_one=False)
- `fetch_sx_matchable_orders`: filtering, invalid pct, zero size, API error response, fetcher exceptions
- `match_sx_orders`: full fill (1 order), multi-order with sort, partial when capacity short, slippage cap stops, empty matchable
- `build_sx_order`: full match returns complete body, partial flag set, no matching = empty hashes, input validation
- End-to-end via `fire_arb`: partial leg in 2-leg arb → `aborted_reason: partial_fill_arb_broken`

---

## Network safety / VPN kill switch (Phase 8 add-on, 28.04.2026)

Защита от leak'а трафика мимо VPN-туннеля или из не-разрешённой страны.
**Три кумулятивных слоя**:

### Layer 1 — System firewall (iptables / Mullvad lockdown)
OS-level блок всего outbound трафика кроме VPN-туннеля. Если VPN падает —
бот получает `Connection refused`, leak невозможен. Реализуется на VPS
через `mullvad lockdown-mode set on` или ручные iptables правила.
См. `deploy/README.md` §8 Layer 1.

### Layer 2 — systemd dependency
`plan-kapkan-radar.service` имеет `BindsTo=mullvad-daemon.service`. Если
VPN-демон упал — radar тоже останавливается автоматически. Защищает от
случая «VPN перестал, бот продолжает торговать без него».
См. `deploy/README.md` §8 Layer 2.

### Layer 3 — Application-level (`Scripts/risk/network_check.py`)

Бот сам каждые 60с проверяет outbound IP через 2 redundant geo-IP
провайдера (`ifconfig.co/json`, `api.country.is`). Если страна не в
`ALLOWED_COUNTRIES` — все fire'ы блокируются через `risk.check_can_fire`.

```python
# .env.example / Credentials.env
ALLOWED_COUNTRIES=GE              # primary Georgia (single allowed)
ALLOWED_COUNTRIES=GE,AM,TR        # GE + same-region fallbacks
ALLOWED_COUNTRIES=                # empty = check disabled (local dev only)
```

**Fail-safe:** при любой ошибке (network down, providers blocked) — `check_country_allowed`
возвращает False → fire blocked. Доступность жертвуется ради безопасности (стоимость
account ban на Polymarket >> стоимость пропущенных сканов).

### Banner и endpoint

При старте радара banner показывает:
```
Network: ALLOWED=GE | current IP 95.X.X.X (GE) → ✓ allowed
```
или `⚠ DISALLOWED` если IP не в списке. `GET /api/network_status` —
JSON snapshot для дашборда (`?force=1` bypass cache).

### Тесты (13 новых, **100 всего**)

`tests/test_network_check.py`:
- `check_country_allowed`: disabled при пустом списке, allowed/disallowed страны, US-блок
- Fail-safe: failed fetch → блок (не allow по умолчанию)
- Caching: повторные вызовы не fetch'ат, force_refresh обходит кэш
- Provider parsers: ifconfig.co + country.is, fallback при первом провайдер failed
- `status()` shape для endpoint

### Hot standby (опционально)
Для production: 2 VPS в одной стране, external monitor, failover при падении primary.
См. `deploy/standby-setup.md`.

---

## Telegram alerts (Phase 8 add-on)

`Scripts/notify.py` — единая точка отправки уведомлений.

### Конфиг (env)
```
TELEGRAM_BOT_TOKEN=<from @BotFather>
TELEGRAM_CHAT_ID=<from /getUpdates>
```

Оба пустые → notify графefully no-op (local dev). Установлены → бот шлёт алерты на критические события.

### Где встроено

| Событие | Уровень | Dedupe key |
|---|---|---|
| Kill switch activated | crit 🚨 | `killswitch_active` |
| Kill switch cleared | success ✅ | `unkill:{reason}:{minute}` |
| Daily loss limit hit | crit 🚨 | `daily_loss_{date}` |
| Hourly losing streak | warn ⚠️ | `hourly_streak_{hour}` |
| Reconcile mismatch | crit 🚨 (через kill chain) | автоматически |
| Network check failed | warn ⚠️ | `network_check_fail` (1/min) |
| Radar startup | success ✅ | `radar_startup` |

### Дизайн

- **Non-blocking**: send → daemon thread → `urlopen` Telegram. Hot path не блокируется.
- **Rate-limited**: per-key dedupe 60с — alert storm не флудит чат.
- **Graceful degrade**: если env не задан, send возвращает False, никаких ошибок.
- **Stdlib only**: `urllib` без `python-telegram-bot` — requirements.txt не разрастается.

### Тесты (9 новых, **109 всего**)
`tests/test_notify.py` — конфиг, эмодзи-префиксы, дедупликация, network failure handling.

---

## Analytics: cleanup + per-trade history (28.04.2026)

### Что убрано
Manual decision track: кнопки `✅ Взял` / `❌ Пропустил` на карточках сделок,
endpoint `POST /api/analytics/decision`, `record_decision()` функция,
поля `decision*`/`real_net`/`taken`/`skipped` в state и aggregate'е.

**Зачем убрано:** ручная дисциплина оператора больше не нужна — Phase 2
dry-run executor авто-фейрит каждый HOT, Phase 5 paper trading измеряет
реальные fill, Phase 6+ live execution делает принятие решений за оператора.
Risk-gate (Phase 3) — единственный «discipline layer» который остался.

### Что добавлено

**`/api/analytics/history`** + UI таблица «История сделок»:
- Каждое `opened` событие с фильтрами (period/platform/structure/min_net)
- Pagination (limit + offset, ←Newer/Older→ кнопки)
- Поля: `ts (UTC), platform, arb_structure, title, sum_cents, net, roi, grade, min_liq, duration_sec, status (open/closed)`
- Сортировка: newest first

**`by_structure`** разбивка в aggregate — count/net по структурам A/B/C/binary.

### Persistence (без изменений)

`analytics_events.jsonl` — append-only, никогда не сбрасывается. Aggregate и
history просто фильтруют по `ts ≥ cutoff`. Чтобы reset'нуть статистику —
удалить файл вручную.

### endDate в истории сделок (28.04.2026)

Каждая `opened` запись теперь содержит `end_date` события (ISO-8601 UTC):
- **Polymarket**: из `event.endDate`
- **Kalshi**: из `event.close_time` (или per-market `market.close_time`)
- **SX Bet**: `gameTime` (unix ts) → ISO

Колонка «Резолв» в UI таблицы История сделок: дата + дни до резолва.
Цвет: ≤3 дня — green, ≤7 — gold, >7 — text2. Legacy строки (до этого PR)
без `end_date` → `—`.

### Окно событий (WINDOW_DAYS = 10, восстановлено 28.04.2026)

Возвращён к **10 дням** (с 30 в PR #6). 30-дневное окно блокирует капитал
на месяц ради $5-30 профита — плохой turnover. 10 дней даёт **×3 оборачиваемость**.
Теряем ~30-40% сигналов структуры A (в основном дальние праймериз) но
выигрываем в capital efficiency.

## Оценка сделок (Grading)

| Оценка | Условие (adj profit) |
|--------|---------------------|
| **A+** | > $20 и ликвидность ОК |
| **A** | > $10 |
| **B** | > $5 |
| **C** | > $2 |
| **D** | > $0 |
| **F** | ≤ $0 |

---

## Технический стек

```
Python 3.8+ / Flask
      ↓
Polymarket CLOB + Kalshi + SX Bet Orderbook API
(batch parallel fetch, ThreadPoolExecutor 40 workers)
      ↓
negRisk фильтр → deadline фильтр → rough sum фильтр
      ↓
Global batch fetch (быстрая фаза на 700 рынках, фоновая фаза на остальных)
      ↓
Расчёт: fee, slippage, ROI, grading
      ↓
Dashboard UI (HTML/JS/CSS, polling каждые 10 сек)
```

---

## Файлы проекта

| Файл | Описание |
|------|----------|
| `Scripts/arb_server.py` | Flask бэкенд — сканер + API |
| `Scripts/dashboard.html` | Real-time UI панель |
| `idea.md` | Этот файл — спецификация проекта |
| `Executions/price_history.jsonl` | Логи арбитражных окон для ретроспективного анализа |

---

## Практические рекомендации

1. **Сервер** — VPS вблизи AWS eu-west-2 (Лондон). Дублин даёт задержку до Polymarket < 2ms
2. **Протокол** — WebSocket вместо REST polling для мониторинга стакана (если поддерживается)
3. **Ордера** — batch endpoint (до 15 ордеров в одном запросе)
4. **Язык** — для критических секций (подпись, расчёты) рассмотреть Rust/C++
5. **VWAP** — для точной оценки цены заполнения использовать Volume-Weighted Average Price по глубине стакана, а не только best ask

---

## Limitless Exchange integration (Phase 9, 28.04.2026)

Добавлена 4-я платформа **Limitless Exchange** — CLOB на Base L2, no-KYC.

### Зачем
- **Без KYC** и без жёстких гео-блоков → доступна из РФ через VPN-Грузия так же как Polymarket
- **Нет платформенной комиссии** — только Base gas (~$0.01/leg) → можно ловить более тонкие арбы
- Архитектура mirror Polymarket (CLOB, EIP-712, USDC, negRisk groups) → код почти 1:1
- ~$3M/день volume vs $110M на Polymarket — меньше но менее конкурентно

### Параметры

| | Polymarket | Limitless |
|---|---|---|
| Сеть | Polygon (137) | Base (8453) |
| Collateral | USDC | USDC |
| Taker fee | 2.5% | ~0% (только gas) |
| `THETA_*` | 0.025 | 0.005 (буфер на gas + slippage) |
| `THRESH_*` | 0.97 | **0.99** (тоньше, тк нет fee) |
| API base | gamma-api / clob.polymarket.com | api.limitless.exchange |
| Подпись | EIP-712 | EIP-712 |
| WS | подключён (poly_ws.py) | **REST polling 5s** (Phase 2 → WS) |

### Flow

1. `_fetch_limitless_orderbook(slug)` → GET `/markets/{slug}/orderbook` → `(yes_ask, depth_yes, no_ask, depth_no)` (NO синтезируется как `1 − best_yes_bid`).
2. `eval_limitless(events, lim_res)` — те же 3 структуры **A/B/C** + standalone binary (вне negRisk группы → только C).
3. `classify_pools` добавляет `'lim'` пул, `near_summary` рендерит вместе с другими.
4. `limitless_micro_loop()` — RE-fetch HOT+NEAR каждые 5с (как Kalshi/SX).
5. `build_limitless_order(slug, side, price, size_usdc, wallet)` — EIP-712 body с `chainId=8453`.
6. `atomic._build_leg` диспатчит на `Limitless` платформу через slug в entry.

### Конфиг (env)
```
ENABLE_LIMITLESS=1                # 1=on, 0=skip entirely
LIMITLESS_MAIN_PAGES=10           # 10 × 100 = 1000 markets per main scan
LIMITLESS_PAGE_DELAY_S=0.1        # 100ms gap between pages = 10 req/s polite cap
LIMITLESS_MICRO_INTERVAL=5        # micro-loop poll period (sec)
LIMITLESS_API_KEY=                # optional — needed only for trade-side ops (Phase 2)
```

### NO-side нюанс

В отличие от Polymarket'a где есть отдельные `clobTokenIds` для YES и NO, Limitless кладёт ОДИН orderbook на slug (= одну сторону). NO-цена синтезируется как `1 − best_yes_bid` — это математически эквивалентно покупке NO у того, кто купил бы YES. На negRisk группах каждый child outcome имеет **свой** slug → можем читать YES/NO напрямую с разных slugs.

### Phase 2 Limitless (отложено)

- **WebSocket** subscription для real-time orderbook updates (как `poly_ws.py`). docs.limitless.exchange упоминают WS но без публичного URL — нужно довытащить из открытого исходника TS-SDK.
- **Approve flow** через `limitless.exchange` UI на каждом боте (после VPS).
- **API key** через `/auth/api-keys` POST для авто-подписей без MetaMask popup.

### Тесты

`tests/test_limitless.py` — 13 тестов:
- Builder: BUY/SELL flag, EIP-712 body shape, chain_id=8453, валидация input
- Orderbook fetcher: NO ask синтез из best YES bid, обработка 404/empty/exception
- eval: ALL_YES на 3-outcome группе, YES_NO_PAIR per-market, standalone binary, 10-day window filter, no-arb когда total ≥ 0.99

**Total: 122 unit-теста**, все проходят.