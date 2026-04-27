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