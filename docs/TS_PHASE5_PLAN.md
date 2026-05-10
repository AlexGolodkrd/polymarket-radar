# Phase TS-5 — real fires, fill confirmation, position management

**Дата:** 09.05.2026
**Статус:** черновик, ждёт зелёного TS executor контейнера в проде (run #21 success)
**Цель:** довести TypeScript executor до состояния когда **можно** переключить `DRY_RUN=0` без риска потерять деньги.

## Почему отложено

TS-3 (что мы делаем сейчас) — только **скелет**: TS принимает `POST /fire` от Python detector, **логирует** decision в `dryrun.jsonl`, не делает реальных POST на биржи. Эквивалент Python `Scripts/executor/atomic.py` в DRY_RUN=1 mode.

TS-5 — **real fires + observability + recovery**. После него можно реально торговать (после Phase 6 approvals + Phase 7 cutover).

## Что входит

### TS-5a — real HTTP firing (~500 LoC)

**Файлы:**
- `executor-ts/src/fire/poly_post.ts` — POST /order на Polymarket с retries + circuit breaker
- `executor-ts/src/fire/sx_post.ts` — POST /orders/fill на SX Bet
- `executor-ts/src/fire/lim_post.ts` — POST /orders на Limitless
- `executor-ts/src/lib/undici_client.ts` — shared HTTP client (keep-alive, HTTP/2, per-host pool)

**Что делает:**
- Принимает signed order body от builder, делает реальный POST
- Per-order timeout 2s (как в Python)
- Retry on 502/503/504/timeout — max 1 retry с jitter
- Logs response headers (X-Order-Id, etc.) в audit
- Returns `{orderId, status, raw}` или throws structured error

**Тесты:** mock undici stubs, проверка retry counts, parsed response shapes.

**Из Python для портации:**
- `Scripts/executor/atomic.py:_fire_one_leg` (lines ~150-280) — основная HTTP logic
- `Scripts/executor/atomic.py:_handle_fire_error` — error classification

### TS-5b — WS fill listeners (~600 LoC)

**Файлы:**
- `executor-ts/src/ws/poly_user.ts` — Polymarket `wss://ws-subscriptions-clob.polymarket.com/ws/user`
- `executor-ts/src/ws/sx_user.ts` — SX Bet user channel (wss://api.sx.bet/v1/orders/user)
- `executor-ts/src/ws/lim_user.ts` — Limitless user (если есть, иначе REST polling)
- `executor-ts/src/ws/client.ts` — shared `WsClient` с reconnect+backoff+jitter

**Что делает:**
- При старте сервера — подписаться на user channel каждой биржи (по wallet)
- Парсить fill events: `{orderId, status:'filled', price, size}`
- Резолвить `FillRegistry.consume_by_order_id(platform, orderId, result)` — это разблокирует ожидающий Promise в `fireArb`
- На WS disconnect — автоматический reconnect с exponential backoff (200ms→30s, jitter ±20%)
- Heartbeat ping каждые 10s

**Тесты:** mock WS server в vitest, проверка reconnect, корректность fill mapping → registry.

**Из Python для портации:**
- `Scripts/poly_user_ws.py` — Polymarket WS subscriber
- `Scripts/executor/fills.py` — registry pattern (уже частично на TS)

### TS-5c — slippage enforcement + position revert (~400 LoC)

**Файлы:**
- `executor-ts/src/executor/depth_recheck.ts` — pre-fire re-fetch /book на /orders, проверка depth ≥ stake × 0.8
- `executor-ts/src/executor/revert.ts` — sell filled legs если arb сломан
- `executor-ts/src/executor/atomic.ts` (расширить) — orchestrator с deadman timer

**Что делает:**
- **Pre-fire** (within last 100ms before POST):
  - Refetch real-time orderbook depth для каждой ноги
  - Если ≥ 20% drop в depth ИЛИ ≥ 0.5¢ price drift — abort арб, log `slippage_aborted`
- **During fire** (2s deadman):
  - Promise.all per leg
  - Если хоть одна нога вернула 4xx ИЛИ timeout — пошёл revert path
- **Revert path**:
  - Для каждой leg где fill confirmed — отправить counter-order (sell back)
  - Записать `revert.jsonl` с original + counter pair для аудита
  - Если revert не удался — alert + halt trading

**Тесты:** simulated partial fill scenarios, depth shock, full revert flow.

**Из Python для портации:**
- `Scripts/executor/atomic.py:_last_ms_depth_recheck` (lines ~80-120)
- `Scripts/executor/atomic.py:_revert_filled_legs` (TODO в Python)

### TS-5d — wallet pool + coordinator (~500 LoC)

**Файлы:**
- `executor-ts/src/wallets/pool.ts` (расширить) — load 6 ботов из env, signing helpers
- `executor-ts/src/wallets/coordinator.ts` — anti-detection round-robin с reservation TTL 15s + jitter 0-50ms между legs
- `executor-ts/src/wallets/balance_check.ts` — pre-fire balance check per bot

**Что делает:**
- При запуске executor: loadWalletsFromEnv → 6 wallets с private keys
- На каждый `/fire`: coordinator выбирает 1 bot per leg (anti-detection)
- Pre-fire: balance check (≥ stake + gas + buffer) на каждом боте
- На fire failure: освобождает reservation, ROUNDROBIN counter increments
- Auto-rebalance proposals (без auto-execute) при low balance

**Тесты:** 6-bot dispatch, reservation TTL, balance edge cases.

**Из Python для портации:**
- `Scripts/wallets/pool.py` + `Scripts/wallets/coordinator.py`

### TS-5e — risk integration (~300 LoC)

**Файлы:**
- `executor-ts/src/risk/limits.ts` (расширить) — daily P&L state, hourly losing-streak counter
- `executor-ts/src/risk/killswitch.ts` (есть) — read flag file, refuse fires
- `executor-ts/src/risk/state.ts` — persistent state в `Executions/risk_state_ts.json`

**Что делает:**
- Pre-fire: `checkCanFire(deal)` returns `{allowed, reason}`
- Post-fire: `recordPnl(realized)` updates daily P&L
- Если daily_pnl ≤ -$35 → `paused_until = next_utc_midnight`
- Если 5 losing trades в час → `paused_until = +1h`
- Каждые 60s: reconcile loop (sync local positions vs exchange GET /positions)

**Тесты:** simulated daily limit hit, hourly losing streak, reconcile mismatch.

**Из Python для портации:**
- `Scripts/risk/limits.py`, `Scripts/risk/state.py`, `Scripts/risk/reconcile.py`

## Pre-conditions (что должно быть прежде чем стартовать TS-5)

| # | Что | Кто | Статус |
|---|---|---|---|
| 1 | TS executor контейнер стабильно работает в проде | Auto-deploy | ⏳ run #21 |
| 2 | TS dryrun.jsonl schema = Python schema (bit-equal) | Я после verify | ⏳ |
| 3 | 22/22 TS unit tests green локально | Operator (`npm test`) | ⏳ |
| 4 | 50 paper trades с TS executor | Время + paper trading | ⏳ |
| 5 | Operator sign-off на real-mode | Operator | ⏳ |

## Post-conditions для live deposit (Phase TS-6 + TS-7)

После TS-5 ещё нужно:

| # | Что | Phase |
|---|---|---|
| 1 | Polymarket L2 HMAC creds derived | TS-6 |
| 2 | Polymarket USDC→pUSD wrap on-chain | TS-6 |
| 3 | CTF `setApprovalForAll(Exchange, true)` on-chain | TS-6 |
| 4 | Limitless USDC approve | TS-6 |
| 5 | SX Bet TokenTransferProxy approve | TS-6 |
| 6 | Geo-IP gate `ALLOWED_COUNTRIES` env set on VPS | TS-6 |
| 7 | Telegram alerts on every failure path | TS-6 |
| 8 | 50 paper trades win rate ≥70% drift ≤20% | Phase 5 |
| 9 | First 10 real trades $5/leg (not $55) | Phase 7 |
| 10 | Python executor cleanup (remove `Scripts/executor/`) | Phase 7 |

## Estimated effort

| Phase | LoC | Tests | Time |
|---|---|---|---|
| TS-5a | 500 | 15 | 4h |
| TS-5b | 600 | 20 | 6h |
| TS-5c | 400 | 12 | 4h |
| TS-5d | 500 | 15 | 4h |
| TS-5e | 300 | 10 | 3h |
| **TS-5 total** | **2300** | **72** | **~21h работы** |
| TS-6 | 800 | 20 | 8h |
| TS-7 | 200 (cleanup) | — | 2h |

Обычная работа = 1 PR за 30-60 минут с тестами + auto-deploy. **Реалистично TS-5 целиком за 1-2 дня плотной работы**.

## Что DELETED после TS-7

```
Scripts/executor/atomic.py          (1000 LoC) — fireArb logic
Scripts/executor/builders.py        (1100 LoC) — EIP-712 sign
Scripts/executor/fills.py           (200 LoC)  — fill registry
Scripts/executor/presign.py         (250 LoC)  — pre-signed cache
Scripts/executor/dryrun_log.py      (270 LoC)  — paper logging
Scripts/executor/bot_connector.py   (90 LoC)   — bot API
Scripts/risk/state.py               (160 LoC)  — moved to TS
Scripts/risk/limits.py              (260 LoC)  — moved to TS
Scripts/risk/reconcile.py           (430 LoC)  — moved to TS
Scripts/wallets/pool.py             (165 LoC)  — moved to TS
Scripts/wallets/coordinator.py      (175 LoC)  — moved to TS
Scripts/wallets/stores.py           (330 LoC)  — moved to TS
Scripts/poly_user_ws.py             (300 LoC)  — moved to TS WS
─────────────────────────────────────────
Total Python deleted: ~5030 LoC
TS replacement:       ~3500 LoC (with tests)
```

Detection (`arb_server.py`, `event_matching.py`, `cross_platform.py`, `analytics.py`) **остаётся Python** навсегда — там сложная стабилизированная логика, нет смысла переписывать.

## Risks

| # | Risk | Mitigation |
|---|---|---|
| 1 | TS подписи bit-equal Python — но при live mode сервер реджектит | Run TS golden tests + run shadow-mode with `EXECUTOR_URL` set, compare dryrun.jsonl выходы |
| 2 | WS fill events приходят с задержкой → fireArb deadman exit раньше | Configurable `DEADMAN_TIMEOUT_S` (default 5s, real-mode 10s) |
| 3 | Revert path сам падает (counter-order rejected) | Hard halt + Telegram critical alert + manual operator intervention |
| 4 | Rate limiting на per-host (Polymarket 100 req/min) | Circuit breaker + per-host token bucket |
| 5 | Wallet private keys в env file → lost on container rebuild | Backup + AWS Secrets Manager backend (Phase 4 stub already exists) |

## How TS-5 will be shipped

Каждая sub-phase = отдельный PR. Auto-deploy подкатывает после merge. До TS-5d **никаких реальных POST'ов** не делается — TS executor продолжает писать dryrun.jsonl (как сейчас). После TS-5d можно врубить shadow mode (Python detector POST'ит deals в TS executor, TS делает full pipeline ВКЛЮЧАЯ real fires но всё ещё в DRY_RUN — возвращает rejected без actual POST). После 50 shadow-trades с matching paper_results — graduation к `DRY_RUN=0`.

Это **самый осторожный путь** — каждая фаза тестируется в изоляции, real-mode только после observable parity.
