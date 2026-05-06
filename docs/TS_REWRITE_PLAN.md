# TS Rewrite Plan — Execution Layer of plan-kapkan

**Дата:** 06.05.2026
**Статус:** черновик, готов к ревью оператора
**Скоуп:** перенести с Python на TypeScript весь код, отвечающий за **подписание, отправку, сопровождение и контроль ордеров и сделок**. Детектор арбитража (`arb_server.py` без exec-врапперов, `event_matching.py`, `cross_platform.py`, `circuit_breaker.py`, `analytics.py`, `paper_trading.py` агрегатор, `dashboard.html`) **остаётся на Python** — переписывать его сейчас не имеет смысла: он стабилизирован 16 PR, активно дебажится оператором, и переписывание тысяч строк сложной потоковой логики введёт новые баги без выгоды.

---

## 0. Зачем переписывать только executor

| Аргумент | Pro TS | Pro Python |
|---|---|---|
| **EVM signing** | viem/ethers — first-class, идеоматичный, типобезопасный | eth_account работает, но Python EIP-712 wrapper падал в 4 фазах (v17, v19, v23, v24) |
| **WebSocket fills** | ws + типизированные события | websockets лежит, но threading.Event паттерн ломкий |
| **Вычисление ордеров (BigInt math, hex/bytes32)** | native BigInt, никаких float-ошибок при usdc_wei = size_usdc * 1e6 | int() round-trip уже однажды дал багу с округлением (Phase 19v19) |
| **HTTP latency** | undici keep-alive + HTTP/2 = ~3× быстрее requests.Session | requests держит соединения но pool по host часто рвёт |
| **Концепция «один тип для одной EIP-712 структуры»** | TS-типы 1:1 = compile-time гарантия совпадения с Solidity | Python тайпинг noisy, mypy не везде |
| **Параллелизм fire_arb** | async/await + Promise.all естественно | ThreadPoolExecutor + GIL |
| **Готовые SDK** | `@polymarket/clob-client-v2`, `@sx-bet/sportx-js`, `@limitless-exchange/sdk` — официальные | py-clob-client уже использовали, но не для V2 |
| **Стек проекта** | insider-radar уже Node 19 + Vite + ESLint | Сборка docker image `python:3.11-slim` для radar остаётся |

**Вывод:** перенос даёт измеримый выигрыш на критическом пути (signing + fire) и снимает весь риск EIP-712 расхождения. Detector — оставляем.

---

## 1. Что переписываем (inventory)

Из аудита 06.05.2026, общий объём **~5 700 LoC** Python:

### 1.1 `Scripts/executor/` → `executor-ts/src/executor/`
| Python | LoC | TS | Ответственность |
|---|---|---|---|
| `builders.py` | ~1100 | `builders/poly.ts`, `builders/sx.ts`, `builders/limitless.ts`, `builders/kalshi.ts` (stub) | EIP-712 domain+types+signing, HMAC, USDC wei, salt, tick rounding |
| `atomic.py` | ~1000 | `atomic.ts` | `fireArb`, parallel POSTs, slippage check, deadman, partial-fill revert, depth recheck |
| `fills.py` | ~200 | `fills.ts` | FillRegistry с EventEmitter вместо threading.Event |
| `presign.py` | ~250 | `presign.ts` | NEAR-pool pre-signed orders (TTL cache) |
| `dryrun_log.py` | ~270 | `paper.ts` (часть) | append jsonl + delayed re-fetch для paper-trade evaluator |
| `bot_connector.py` | ~90 | `bot_connector.ts` | Тонкий API для внешних ботов (gabagool/copy-trade) |

### 1.2 `Scripts/risk/` → `executor-ts/src/risk/`
| Python | LoC | TS | Ответственность |
|---|---|---|---|
| `state.py` | ~160 | `state.ts` | RiskState dataclass + atomic JSON write |
| `limits.py` | ~260 | `limits.ts` | check_can_fire, record_pnl, paused_until |
| `killswitch.py` | ~190 | `killswitch.ts` | flag-файл `.killed`, audit jsonl, register cancel cb |
| `network_check.py` | ~210 | `network.ts` | geo-IP gate (ifconfig.co + country.is) |
| `reconcile.py` | ~430 | `reconcile.ts` | 60s loop sync local vs exchange positions |

### 1.3 `Scripts/wallets/` → `executor-ts/src/wallets/`
| Python | LoC | TS | Ответственность |
|---|---|---|---|
| `config.py` | ~165 | `wallet.ts`, `pool.ts` | Wallet dataclass + V2 topology validation |
| `coordinator.py` | ~175 | `coordinator.ts` | assignLegs (anti-detection, balance-aware, jitter, reservations) |
| `rebalance.py` | ~200 | `rebalance.ts` | propose transfers (cooldown, low/high thresholds) |
| `stores.py` | ~330 | `stores/local.ts`, `stores/aws.ts`, `stores/keystore.ts` | LocalEnvStore, AwsSecretsStore, optional encrypted keystore |

### 1.4 Прочее
| Python | LoC | TS | Ответственность |
|---|---|---|---|
| `paper_trading.py` (graduation gate) | ~250 | `paper_gate.ts` | 50-trade gate, win rate / drift |
| `circuit_breaker.py` | ~200 | `circuit_breaker.ts` | CLOSED/OPEN/HALF_OPEN per-host |
| `notify.py` | ~150 | `notify.ts` | Telegram alerts |
| `watchdog.py` | ~100 | отдельный entry `watchdog.ts` | поллит `.killed`, кенселит pending |
| `poly_l2_http.py`, `poly_derive_api_creds.py`, `poly_user_ws.py`, `poly_verify_funder.py`, `polymarket_approve.py`, `limitless_approve.py`, `poly_proxy_check.py`, `preflight.py`, `http_codes.py` | ~400 общим | `auth/poly_l2.ts`, `auth/poly_derive.ts`, `ws/poly_user.ts`, `approvals/poly.ts`, `approvals/limitless.ts`, `preflight.ts`, `http_codes.ts` | L2 HMAC, derive API creds (one-time L1 signature), user-channel WS, on-chain approvals (USDC → Exchange, pUSD wrap/unwrap, CTF safeTransferFrom), preflight checks |

**ИТОГО:** ~5 700 LoC Python → ожидаемо ~6 500–7 500 LoC TS (типы + чуть более многословный async).

---

## 2. Что **НЕ** переписываем

| Останется на Python | Почему |
|---|---|
| `arb_server.py` детектор (~6 500 LoC до строки 5500) | Стабилизирован 16 PRs, переписывание = новые баги без выгоды |
| `event_matching.py`, `cross_platform.py` | Сложная fuzzy-логика scope-guard, 50+ тестов, нет преимуществ TS |
| `analytics.py` | sim P&L агрегатор, читает jsonl, без сети |
| `dashboard.html` | Vanilla JS поллер, переписывать нечего |
| `poly_ws.py`, `limitless_ws.py`, `async_fetchers.py` | Detector-side WS — кормят кэш, не торгуют |

**Граница:** Python radar пишет «hot» сделку в Redis/файл/HTTP-эндпоинт → Node executor читает, файрит, отдаёт результат назад. Контракт между сервисами — JSON по Unix-сокету или REST-эндпоинт `POST /executor/fire`.

---

## 3. Архитектура «двух процессов»

```
┌─────────────────────────────────────┐         ┌────────────────────────────────┐
│  arb_server.py  (Python)            │         │  executor-ts  (Node)           │
│  ─ scanner WS пулы                  │         │  ─ /fire (POST)                │
│  ─ Polymarket / SX / Limitless / WS │  HTTP   │  ─ /risk_status (GET)          │
│  ─ event matching, scope-guard      │ ──────▶ │  ─ /kill (POST, double-conf)   │
│  ─ HOT / NEAR классификация         │  +unix  │  ─ /paper_stats (GET)          │
│  ─ /api/near, /api/deals dashboard  │   sock  │  ─ /reconcile_status (GET)     │
│  ─ analytics, sim P&L, history      │ ◀────── │  ─ /api/fills (внутр.)         │
│                                     │  fills  │                                │
│  Записывает: deals.jsonl,           │  jsonl  │  Записывает: positions.jsonl,  │
│              near.jsonl             │         │              dryrun.jsonl,     │
│                                     │         │              paper_results,   │
│                                     │         │              risk_state.json, │
│                                     │         │              killswitch.jsonl │
│  Читает: dryrun.jsonl (для UI),     │         │  Читает: deals.jsonl (для     │
│          risk_state.json (для UI)   │         │           reconcile)           │
└─────────────────────────────────────┘         └────────────────────────────────┘
                       ▲                                        ▲
                       └────── shared volume Executions/ ───────┘
                              (jsonl + json + .killed flag)
```

**Контракт `POST /executor/fire`:**
```typescript
// Request — тот же deal dict что radar собирает в build_cross_platform_deal
type FireRequest = {
  arbId: string;
  dealTitle: string;
  structure: 'all_yes' | 'all_no' | 'yn_pair' | 'X1' | 'X2';
  platform: string;                  // или 'cross-platform'
  entries: LegSpec[];                // 1..N legs, по одной на платформу
  dryRun?: boolean;                  // override DRY_RUN env (тесты)
};

type LegSpec = {
  platform: 'Polymarket' | 'SX Bet' | 'Limitless' | 'Kalshi';
  tokenId?: string;                  // Polymarket / Limitless
  marketHash?: string;               // SX
  outcome?: 1 | 2;                   // SX
  slug?: string;                     // Limitless
  side: 'BUY' | 'SELL';
  expectedPrice: number;             // 0..1
  expectedSizeUsdc: number;
  negRisk?: boolean;                 // Polymarket
  tickSize?: number;
};

// Response — то что сейчас возвращает atomic.fire_arb
type FireResponse = ArbFireResult;   // все ноги + статус + dry_run + abort_reason
```

---

## 4. Ключевые TypeScript-библиотеки

### 4.1 EVM signing
| Что нужно | Выбор | Почему |
|---|---|---|
| EIP-712 typed data sign | **`viem`** (≥ 2.x) | Чище API, лучше типы, быстрее, чем ethers v6. `signTypedData` принимает domain/types/message 1:1 как у нас в Python |
| Адрес из приватного ключа | `viem/accounts.privateKeyToAccount` | |
| keccak256, hex utils | `viem/utils.keccak256, toHex, hexToBytes` | |
| Backup signer (если нужен ABI на on-chain approval) | **`ethers` v6** только для `polymarket_approve` / `limitless_approve` | ABI tooling и contract calls удобнее в ethers |

> Решение: **viem основной**, ethers v6 только для on-chain approve-скриптов (одноразовые, не на горячем пути).

### 4.2 Платформенные SDK
| Платформа | Пакет | Версия | Используем |
|---|---|---|---|
| Polymarket | `@polymarket/clob-client-v2` | ≥ 5.8 | Sign + post + cancel + L2 derive. Под капотом тот же EIP-712 V2 что у нас в Python. |
| SX Bet | `@sx-bet/sportx-js` | latest | OrderFill EIP-712 sign + match makers. Если SDK устарел — пишем сами через viem (домен в `builders.py:577` уже выверен). |
| Limitless | `@limitless-exchange/sdk` | ≥ 1.0.5 | CLOB + NegRisk, X-API-Key auth, FAK/GTC. |
| Kalshi | — | — | Disabled (US-only KYC), оставляем stub. |

> Если SDK ломается / меняет схему (как SX 3 раза за неделю!) — у нас уже есть fallback: ручной билдер на viem. Этот билдер **должен пройти те же 56 тестов**, что текущий Python.

### 4.3 HTTP
| Что | Выбор |
|---|---|
| HTTP-клиент | **`undici`** напрямую (`request`, не `fetch`) — 3× быстрее axios, keep-alive, HTTP/2, connection pooling |
| Per-host circuit breaker | свой `circuit_breaker.ts` (1:1 с Python) |
| Retry with backoff | `p-retry` (proven, маленький) |

### 4.4 WebSocket
| Что | Выбор |
|---|---|
| Базовый клиент | **`ws`** (raw, без socket.io) |
| Reconnect + backoff | свой wrapper — `WsClient` с exponential backoff + jitter (как в `oneuptime` блог-посте, не reuse) |
| User-channel fills (Poly + SX) | по wallet — отдельный WsClient на wallet |

### 4.5 Process / runtime
| Что | Выбор |
|---|---|
| Web framework для `/fire`, `/kill`, `/risk_status` | **Fastify** (~70k req/s vs 25k Express, нативные TS-типы для роутов) |
| Логи | `pino` (built-in в Fastify, JSON logs) |
| Конфиг + env | `dotenv` + типизированный wrapper `env.ts` |
| Тесты | `vitest` (быстрее jest, Vite-нативный, ESM-готовый) |
| Линтер | `@biomejs/biome` (быстрее eslint+prettier, всё-в-одном) — или ESLint 9 как в insider-radar |
| TS компилятор / runtime | `tsx` для dev, `tsc --noEmit` для CI, **Bun не используем** (eth_account/SX SDK иногда падают на Bun) |

### 4.6 Secrets
| Backend | Пакет |
|---|---|
| Local dev | свой `LocalEnvStore` (читает `Credentials.env`) |
| AWS Secrets Manager | `@aws-sdk/client-secrets-manager` |
| Optional: encrypted keystore | `keytar` (нативная связка ОС-уровневого хранилища) |

### 4.7 Observability
| Что | Выбор |
|---|---|
| Метрики (P&L, fire latency, slippage гистограммы) | `prom-client` (pull-based Prometheus) — или просто jsonl как сейчас |
| Telegram alerts | свой `notify.ts` через `undici.request` (Telegram Bot API) |

---

## 5. Фазы реализации

Каждая фаза = отдельный PR. Фаза N не мержится пока N-1 не зелёная в CI + не отработала ≥24h в shadow mode (Node executor работает параллельно с Python, оба пишут в jsonl, оператор сравнивает).

### Phase TS-1 — Skeleton + builders (1 неделя)
**Scope:** `executor-ts/` папка, package.json, tsconfig, vitest, базовый Fastify. Перенос **только builders** — Polymarket / SX / Limitless / Kalshi-stub. Никаких сетевых вызовов, никакого fire — pure functions.

**Тесты:** 1:1 портируем `tests/test_polymarket.py`, `test_limitless.py`, `test_sx_executor.py`. Shared **golden fixtures** — те же ордеры что Python подписывает, должны давать те же сигнатуры (детерминистично, salt + ts фиксированы в тестах). Это контракт-тест на корректность EIP-712.

**Verification:**
- `npm test` зелёное в CI
- Ручной diff: запустить Python `build_poly_order(...)` и TS `buildPolyOrder(...)` с одним приватным ключом → bit-identical signature.

**PR #18 (TS-1).**

### Phase TS-2 — Wallet pool + stores (1 неделя)
**Scope:** `wallets/` целиком, включая LocalEnvStore + Wallet validation для V2 (signature_type=0/1/2, funder проверки). AWS Secrets Manager backend готов но dormant. Coordinator с anti-detection и reservations.

**Тесты:** портируем `test_wallets.py`. Добавляем mock-Wallets без приватников — coordinator должен distribute, sign не должен срабатывать.

**Verification:**
- Запустить `executor-ts` без `BOT*_PRIVATE_KEY` → процесс встаёт, `/risk_status` отвечает «keys not configured, dry-run only».
- Mock-keys → 6 ботов в `/wallets`.

**PR #19 (TS-2).**

### Phase TS-3 — fire engine + dry-run pipeline (1.5 недели)
**Scope:** `atomic.ts`, `fills.ts` (только in-process EventEmitter, без WS листенеров пока), `dryrun_log.ts`, `paper.ts`. Реализуем `POST /fire` endpoint в Fastify, читает FireRequest, вызывает `fireArb`, пишет в `dryrun.jsonl`, schedules realistic-eval через 5s.

**Контракт-парность:** Python `arb_server.py` теперь умеет ходить в `executor-ts` через env `EXECUTOR_URL=http://localhost:5051`. Если URL не задан — fallback на in-process Python executor (старый код). Двойной режим для shadow-trading.

**Тесты:**
- Портируем `test_executor.py`: slippage check, deadman timeout, partial fill revert, depth recheck.
- Portируем `test_phase_9rr.py`, `test_phase_9zz_presign.py`, `test_phase15_maker_orders.py` (16 файлов с 9-фазой).
- Shadow-test: запустить Python detector + оба executor, сравнить `dryrun.jsonl` файлы — записи должны совпадать по `expected_price`, `expected_size_usdc`, `arb_id`, и сигнатуры по token+wallet.

**Verification:**
- `vitest` зелёное.
- Shadow-mode 24h без расхождений > 0.01¢ в expected_price.
- `Executions/dryrun_ts.jsonl` пишется параллельно.

**PR #20 (TS-3).**

### Phase TS-4 — risk layer (1 неделя)
**Scope:** `state.ts`, `limits.ts`, `killswitch.ts`, `network_check.ts`, `reconcile.ts`. UI kill switch (двойное подтверждение) уже в dashboard.html — TS просто реализует `POST /kill`.

**Тесты:** портируем `test_risk.py`, `test_network_check.py`. Добавляем e2e: `kill /api/kill {confirmation:1}` без второго клика → 400, второй клик с правильным `nonce` → 200, `.killed` файл создан, executor отказывает на `/fire`.

**Verification:**
- Симуляция $35 убытка → executor пауза до 00:00 UTC.
- Reconcile mismatch → `kill()` автоматом.
- Ручной kill → cancel pending, флаг создан.

**PR #21 (TS-4).**

### Phase TS-5 — fills via WS + presign + maker mode (1.5 недели)
**Scope:** `ws/poly_user.ts` (Polymarket user channel), `ws/sx_user.ts`, `ws/limitless_user.ts`. FillRegistry получает события из WS, не из threading.Event. Presign-cache TTL = 8s, греет NEAR кандидатов. Maker mode флаг + `MAKER_FILL_TIMEOUT_S` гард.

**Тесты:** портируем `test_phase_15_maker_orders.py`, `test_phase_9zz_presign.py`. WS mock через `ws` сервер в тесте.

**Verification:**
- Shadow-trade с maker mode → fill confirmation по WS приходит до deadman таймера.

**PR #22 (TS-5).**

### Phase TS-6 — auth + on-chain approvals (1 неделя)
**Scope:** `auth/poly_l2.ts` (HMAC headers), `auth/poly_derive.ts` (one-time EIP-712 derive API key), `approvals/poly.ts` (USDC.e → pUSD wrap, CTF approveForAll), `approvals/limitless.ts`. Это уже **hot path для real-mode** — без этих модулей real-trading не запустится.

**Тесты:** mock RPC (anvil или локальный hardhat), проверяем что approve-tx сформирован с правильным spender + amount.

**Verification:**
- На testnet (если возможно): прогон approve-flow от лица одного бота.
- На mainnet: только в read-only mode (eth_call), без отправки.

**PR #23 (TS-6).**

### Phase TS-7 — graduation cutover (1 неделя)
**Scope:** Python radar полностью переключается на `executor-ts` — fallback in-process executor удаляется. Watchdog становится отдельный Node процесс. Docker-compose добавляет `executor` сервис рядом с `radar` и `watchdog`.

**Verification:**
- 100 paper trades через TS executor → graduation gate проходит.
- Первые 10 real-mode trades по $5/leg.
- При >70% win rate → полный размер.

**PR #24 (TS-7).**

---

## 6. Скелет проекта (executor-ts/)

```
executor-ts/
├── package.json
├── tsconfig.json
├── biome.json (или .eslintrc + .prettierrc)
├── vitest.config.ts
├── Dockerfile
├── .env.example
├── src/
│   ├── index.ts                    # Fastify entry, читает env, регистрирует routes
│   ├── env.ts                      # типизированный config
│   ├── types/
│   │   ├── deal.ts                 # FireRequest, LegSpec, ArbFireResult
│   │   ├── platform.ts             # 'polymarket' | 'sx_bet' | 'limitless' | 'kalshi'
│   │   └── eip712.ts               # Domain, TypedData generic
│   ├── builders/
│   │   ├── poly.ts
│   │   ├── sx.ts
│   │   ├── limitless.ts
│   │   ├── kalshi.ts               # stub
│   │   ├── usdc.ts                 # to_wei / from_wei helpers (BigInt-safe)
│   │   └── tick.ts                 # snap_to_tick
│   ├── executor/
│   │   ├── atomic.ts
│   │   ├── fills.ts
│   │   ├── presign.ts
│   │   ├── paper.ts                # dryrun_log + paper_results
│   │   └── bot_connector.ts
│   ├── risk/
│   │   ├── state.ts
│   │   ├── limits.ts
│   │   ├── killswitch.ts
│   │   ├── network.ts
│   │   └── reconcile.ts
│   ├── wallets/
│   │   ├── wallet.ts
│   │   ├── pool.ts
│   │   ├── coordinator.ts
│   │   ├── rebalance.ts
│   │   └── stores/
│   │       ├── local.ts            # Credentials.env reader
│   │       ├── aws.ts              # AWS Secrets Manager
│   │       └── keystore.ts         # keytar-based (optional)
│   ├── auth/
│   │   ├── poly_l2.ts              # HMAC headers
│   │   └── poly_derive.ts          # one-time L1 derive
│   ├── ws/
│   │   ├── client.ts               # generic WsClient with reconnect + jitter
│   │   ├── poly_user.ts            # Polymarket user-channel fills
│   │   ├── sx_user.ts              # SX Bet user-channel fills
│   │   └── limitless_user.ts
│   ├── approvals/
│   │   ├── poly.ts                 # USDC→pUSD wrap, CTF approveForAll (ethers v6)
│   │   └── limitless.ts
│   ├── http/
│   │   ├── client.ts               # undici wrapper + circuit breaker
│   │   └── circuit_breaker.ts
│   ├── routes/
│   │   ├── fire.ts                 # POST /fire
│   │   ├── kill.ts                 # POST /kill (double-confirm)
│   │   ├── risk_status.ts          # GET /risk_status
│   │   ├── paper_stats.ts          # GET /paper_stats
│   │   ├── reconcile_status.ts     # GET /reconcile_status
│   │   └── wallets.ts              # GET /wallets (sanitized — no keys)
│   ├── notify.ts                   # Telegram
│   ├── watchdog.ts                 # отдельный entry, polls .killed
│   └── preflight.ts
├── tests/
│   ├── builders/
│   │   ├── poly.test.ts
│   │   ├── sx.test.ts
│   │   └── limitless.test.ts
│   ├── executor/
│   │   ├── fire_arb.test.ts
│   │   ├── slippage.test.ts
│   │   ├── deadman.test.ts
│   │   └── revert.test.ts
│   ├── risk/
│   │   ├── limits.test.ts
│   │   ├── killswitch.test.ts
│   │   └── reconcile.test.ts
│   ├── wallets/
│   │   ├── coordinator.test.ts
│   │   └── stores.test.ts
│   └── golden/
│       ├── poly_signature.test.ts  # bit-identical с Python
│       ├── sx_signature.test.ts
│       └── limitless_signature.test.ts
└── README.md
```

### package.json (черновик)
```json
{
  "name": "@plan-kapkan/executor",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "engines": { "node": ">=20.10" },
  "scripts": {
    "dev": "tsx watch src/index.ts",
    "build": "tsc",
    "start": "node dist/index.js",
    "test": "vitest run",
    "test:watch": "vitest",
    "lint": "biome check src tests",
    "format": "biome format --write src tests",
    "typecheck": "tsc --noEmit"
  },
  "dependencies": {
    "@aws-sdk/client-secrets-manager": "^3",
    "@limitless-exchange/sdk": "^1.0.5",
    "@polymarket/clob-client-v2": "^5.8",
    "@sx-bet/sportx-js": "latest",
    "ethers": "^6.13",
    "fastify": "^5",
    "p-retry": "^6",
    "pino": "^9",
    "undici": "^7",
    "viem": "^2",
    "ws": "^8",
    "zod": "^3"
  },
  "devDependencies": {
    "@biomejs/biome": "^1.9",
    "@types/node": "^22",
    "@types/ws": "^8",
    "tsx": "^4",
    "typescript": "^5.6",
    "vitest": "^2"
  }
}
```

### tsconfig.json
```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "ESNext",
    "moduleResolution": "bundler",
    "lib": ["ES2022"],
    "strict": true,
    "noUncheckedIndexedAccess": true,
    "exactOptionalPropertyTypes": true,
    "esModuleInterop": true,
    "resolveJsonModule": true,
    "isolatedModules": true,
    "skipLibCheck": true,
    "outDir": "dist",
    "rootDir": "src"
  },
  "include": ["src/**/*"]
}
```

---

## 7. Риски и митигации

| Риск | Вероятность | Митигация |
|---|---|---|
| EIP-712 сигнатура TS != Python (один бит = реджект ордера) | Высокая если без golden tests | Phase TS-1 заводит **bit-identical** golden tests с фиксированным salt+ts. Сравнение с действующим Python — обязательное условие для merge PR #18. |
| `@polymarket/clob-client-v2` ломается на breaking change (как SX 3 раза за неделю) | Средняя | В каждом builder параллельно держим **manual viem path** с тестами. Если SDK падает — переключаем флагом `USE_SDK=0`. |
| Latency регрессия Python → Node на холодном пути | Низкая | undici быстрее requests. Если latency регрессит — `pino` логи покажут где. |
| `ws` reconnect шторма после WS обрыва | Средняя | Свой `WsClient` с exponential backoff + jitter (от 100ms до 30s) + ratelimit на reconnects. |
| Two-process архитектура усложняет дебаг | Средняя | Все три сервиса в одном `docker-compose.yml`, общий volume `Executions/`, единый log-stream через `docker compose logs -f`. |
| AWS Secrets Manager недоступен на VPS старте | Низкая | `LocalEnvStore` — fallback. На VPS можно поставить позже, ключи дам в `Credentials.env` как сейчас. |
| Кросс-сервисный контракт ломается (radar пишет deal, executor парсит иначе) | Средняя | `zod` schema на `FireRequest` обеих сторон. Python пишет JSON через `pydantic` модель, TS читает через тот же JSON-Schema (генерится из zod). |
| Двойной writes в `Executions/positions.jsonl` (Python+TS) → race | Средняя | На время Phase TS-3 + TS-4 — Python пишет в `positions.jsonl`, TS в `positions_ts.jsonl`, reconcile сравнивает. После cutover (Phase TS-7) — только TS пишет. |

---

## 8. Метрики успеха

| Метрика | Сейчас (Python) | Цель (TS) |
|---|---|---|
| **Время от детекта до POST /order** | ~120-200ms | < 80ms |
| **Test suite прогон** | ~45s pytest | < 15s vitest |
| **EIP-712 регрессии за фазу** | 1-2 (v17, v19, v23, v24) | 0 (golden tests) |
| **WS reconnect глюки за неделю** | 2-3 в logs | < 1 |
| **paper-trade graduation** | не достигнута | 50 trades, ≥70% win rate, ≤20% drift |
| **Покрытие тестами builders+executor** | ~75% | ≥ 90% |

---

## 9. Что нужно от оператора до старта

1. **Решение go/no-go** на этот план целиком (или фаза за фазой).
2. **VPS-апгрейд** (если делать): Node 20.10+ установить рядом с Python 3.11 в Docker. Памяти на t4g.small хватит, проверим.
3. **Ничего не блокирующего:** Phase TS-1 можно стартовать прямо сейчас в worktree, не трогая прод. PR #18 пишется и тестируется параллельно с paper trading радара.

---

## 10. Чеклист готовности к старту

- [x] Аудит execution-кода готов (см. §1)
- [x] TS/JS infra survey готов (insider-radar показывает что Node 20+ на VPS уже работает)
- [x] Список TS-библиотек выбран (§4)
- [x] Архитектура двух процессов согласована (§3)
- [x] Скелет проекта расписан (§6)
- [x] Риски посчитаны (§7)
- [ ] Оператор подтвердил план (TODO)
- [ ] Создан worktree `feature/executor-ts-skeleton`
- [ ] PR #18 (TS-1) открыт

---

## Sources

- [@polymarket/clob-client-v2 на npm](https://www.npmjs.com/package/@polymarket/clob-client-v2)
- [Polymarket V2 Migration Guide](https://tradoxvps.com/polymarket-v2-migration-how-to-update-your-trading-bots-before-they-stop-working/)
- [Polymarket clob-client GitHub](https://github.com/Polymarket/clob-client-v2)
- [SX Bet API Documentation](https://api.docs.sx.bet/)
- [@sx-bet/sportx-js на npm](https://www.npmjs.com/package/@sx-bet/sportx-js)
- [Limitless Exchange TypeScript SDK на npm](https://www.npmjs.com/package/@limitless-exchange/sdk)
- [viem signTypedData](https://viem.sh/docs/actions/wallet/signTypedData.html)
- [Viem vs Ethers.js Comparison (MetaMask)](https://metamask.io/news/viem-vs-ethers-js-a-detailed-comparison-for-web3-developers)
- [undici производительность vs axios](https://dev.to/alex_aslam/why-undici-is-faster-than-nodejs-s-core-http-module-and-when-to-switch-1cjf)
- [Fastify vs Express 2026](https://www.pkgpulse.com/blog/express-vs-fastify-2026)
- [WebSocket reconnection 2026 best practices](https://oneuptime.com/blog/post/2026-01-27-websocket-reconnection/view)
- [AWS Secrets Manager + Node.js](https://docs.aws.amazon.com/sdk-for-javascript/v3/developer-guide/javascript_secrets-manager_code_examples.html)
