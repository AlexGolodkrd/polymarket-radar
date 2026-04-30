# 🐛 BUG_CATALOG.md — каталог всех багов / фиксов / фантомов

**Назначение:** не возвращаться к одним и тем же проблемам. Если что-то странное появляется — сначала ищи здесь.

**Структура:** каждый баг = симптом + root cause + где (file:line) + PR/Phase + fix + как проверить.

**Refs:** [CHANGELOG.md](CHANGELOG.md), [.claude/skills/](. claude/skills/), [idea.md](idea.md).

---

## 📑 Оглавление

1. [Filter bypass — фантомные сделки](#1-filter-bypass--фантомные-сделки)
2. [Time-related phantoms (zombie events)](#2-time-related-phantoms-zombie-events)
3. [Source/Price phantoms](#3-sourceprice-phantoms)
4. [Threshold / NEAR pool baги](#4-threshold--near-pool-баги)
5. [Wallet / executor / dry-fire](#5-wallet--executor--dry-fire)
6. [HTTP errors (403/429/502/504/521/522)](#6-http-errors)
7. [Concurrency / lock / race](#7-concurrency--lock--race)
8. [UI / cache / deploy](#8-ui--cache--deploy)
9. [Cross-platform parity](#9-cross-platform-parity)
10. [Risk / safety](#10-risk--safety)

---

## 1. Filter bypass — фантомные сделки

### 1.1 ALL_YES / ALL_NO с неполным покрытием outcomes (Leeds-Burnley)

**Симптом:** EPL Leeds vs Burnley с 3 outcomes (Leeds, Draw, Burnley). Draw имел `volume=0`, его orderbook пустой. Радар показал `sum_yes = 80.5¢` как ALL_YES net $10.61. **Реальный sum по 3 outcomes = 101.1¢ — НЕ арб, гарантированный убыток** если Draw побеждает.

**Root cause:** `eval_limitless` / `eval_poly` молча выкидывали outcomes без `yes_ask` и считали ALL_YES суммой по оставшимся. Полное покрытие не валидировалось.

**Где:** `arb_server.py:eval_limitless` / `eval_poly` / `eval_kalshi`

**Phase / PR:** Phase 9g / **PR #26** (28.04.2026)

**Fix:** Track `outcomes_missing_yes/no`. Если `len(per_market) != len(ev.markets)` → `full_coverage = False` → ALL_YES/ALL_NO суппрессированы. Только структура C (per-market YES+NO) валидна.

**Тест:** `tests/test_limitless.py::test_leeds_burnley_no_longer_reports_phantom_arb`

**Как проверить:** перед merge — добавь test с partial coverage (один outcome без price).

---

### 1.2 Hidden "Other" outcome (West Virginia / NE-02 / Nebraska Republican)

**Симптом:** 3 события в NEAR/Карантине были не в Карантине: West Virginia Democratic Senate Primary, NE-02 Democratic Primary, Nebraska Governor Republican Primary. У всех был child market `groupItemTitle='Other'` → должны быть в Karantine, но попадали в обычный поток. Если бы fire'или — Other побеждает → теряем все ноги.

**Root cause:**
1. `m.get('question') or m.get('groupItemTitle')` — `or` short-circuit'ил на truthy `question`. `groupItemTitle='Other'` молча игнорировался.
2. OTHER_RE regex не ловил `another candidate` (только `other`). А в question стояло "Will another candidate be...".

**Где:** `arb_server.py:filter_poly` (line 2656), `OTHER_RE` (line 320)

**Phase / PR:** Phase 9kkk / **PR #36** (30.04.2026)

**Fix:**
1. Передавать ОБА поля + event title в `has_other_outcome`:
```python
for m in markets:
    q = m.get('question') or ''
    gt = m.get('groupItemTitle') or ''
    if q: market_names.append(q)
    if gt: market_names.append(gt)
if title: market_names.append(title)
```
2. OTHER_RE расширен: `another (candidate|player|person|team|option|nominee|contender|entrant)`, `someone else`, рус варианты.
3. Safety net на exact match `gt in ('Other', 'другое', 'иное', 'остальные')`.

**Тест:** `5/5 pass` на real Polymarket data (3 should quarantine, 2 sport binary should pass).

**Как проверить:** при добавлении новых OTHER variants — обнови regex + добавь real-world example в test.

---

### 1.3 El Gouna SC phantom (post-resolve A/B/C deals)

**Симптом:** 38 events за 4 часа в `analytics_events.jsonl` от резолвенного матча `El Gouna SC vs. Haras El Hodood SC`. Sum 84-90¢, net $5-30. Все unfillable — orderbook был активен 6-12h во время UMA dispute window, но MM orders never filled.

**Root cause:** Phase 9yy filter дропал `closed=True`/`archived=True`, но Polymarket gamma-api держал event'ы как `closed=False` несколько часов после реального резолва. Stale ghost asks для losing outcomes падали до 0.4-2¢ → sum_yes выглядел как big arb.

**Где:** `arb_server.py:filter_poly` (Phase 9yy)

**Phase / PR:** Phase 9yy → enhanced PR #41 / Phase 9kkk

**Fix:** Phase 9kkk #41 — explicit endDate arithmetic (60min grace, потом adaptive в #42).

**Как проверить:** в diag должен быть счётчик `poly_skip_past_resolve > 0` если есть резолвенные events в фиде.

---

### 1.4 Threshold-series события (Reddit DAUq 104% ROI)

**Симптом:** Multi-outcome events типа "Reddit DAUq above 65M / above 70M / above 75M / ..." попадали в Deals как ALL_YES sum=0.7 = 30¢ "арб" — но outcomes **не mutually exclusive**. Если real value = 72M, выигрывают и above-65M, и above-70M. Sum-identity не работает.

**Root cause:** ALL_YES / ALL_NO предполагают exactly one YES wins → sum_yes ≈ $1. Threshold series ломает это.

**Где:** `arb_server.py:THRESHOLD_SERIES_RE` (line 348), `is_threshold_series` (line 357)

**Phase / PR:** Phase 9o / commit `7bb99ae` (28.04.2026)

**Fix:** Regex flagging events с `above N` / `below X` / `more than` / `≥` / `≤` (EN+RU). При detected → drop ALL_YES/ALL_NO; structure C (per-market YES+NO pair) остаётся валидной.

**Как проверить:** title типа `"Reddit DAUq above 65M"` → `is_threshold_series('Reddit DAUq', ['above 65M', 'above 70M']) == True`.

---

### 1.5 Outcome закрылся между detection и fire (Leeds Draw)

**Симптом:** Outcome был открыт когда сканер увидел его. К моменту POST orders Limitless закрыл его (suspended/closed). Файрим Leeds + Burnley, Draw отказывает — **нет YES_DRAW**. Если Draw победит → теряем 2 ноги.

**Root cause:** Per-child status не проверялся.

**Где:** `arb_server.py:filter_limitless` / `filter_poly`

**Phase / PR:** **PR #27** (28.04.2026)

**Fix:** Per-child gates на `closed/expired/hidden/enableOrderBook=False/acceptingOrders=False`. Если ANY child имеет status — drop event.

**Как проверить:** new_test fixture с 1 child closed → event must drop.

---

## 2. Time-related phantoms (zombie events)

### 2.1 Highest temperature in Munich/Lagos/Singapore (5h post-resolve)

**Симптом:** В NEAR table 25+ events типа "Highest temperature in Munich on April 30?" с endDate `30 Apr 12:00 UTC`, при wall clock `17:46 UTC` (5+ часов после резолва). Все unfillable.

**Root cause (двойной):**
1. gamma-api возвращал `closed=false` часами после time-resolved events
2. `is_within_10_days` имел `WINDOW_PAST_DAYS=2` (48h grace) — fine для elections, **катастрофа для time-of-day events**

**Где:** `arb_server.py:filter_poly` (after `is_within_window`)

**Phase / PR:** Phase 9kkk / **PR #41**

**Fix:** Explicit endDate arithmetic, 60min grace независимо от `closed` flag:
```python
if age_minutes > 60:
    diag['poly_skip_past_resolve'] += 1
    continue
```

**Как проверить:** diag counter `poly_skip_past_resolve` должен расти на ~25 events/scan когда есть резолвенные temp events.

---

### 2.2 BTC/ETH Up or Down 1PM ET (5-min intraday)

**Симптом:** "Bitcoin Up or Down - April 30, 1PM ET" попал в Deals **через 56 минут** после endDate=17:00 UTC. Sum=94¢, net=$5.07. Phase 9kkk #41 60min flat grace **пропустил** этот edge case.

**Root cause:** 60min grace **слишком велик для 5-минутных intraday events**. Crypto-oracle (Chainlink/Pyth) резолвит мгновенно, никакого dispute. Stale orderbook 5-60 минут после резолва.

**Где:** `arb_server.py:filter_poly` past-resolve check

**Phase / PR:** Phase 9kkk / **PR #42**

**Fix:** Adaptive grace based on event duration:
| duration | grace |
|---|---|
| ≤10min (5-min crypto) | **1 min** |
| ≤1h | 5 min |
| ≤24h | 30 min |
| >24h (elections) | 60 min |

Title heuristic fallback если `startDate` отсутствует:
- `'1PM ET'` / `'10AM ET'` / `'5min'` / `'minutely'` → 1 min
- `'highest/lowest temperature'` → 30 min
- default → 30 min

**Как проверить:** event с `startDate` and `endDate` 5min apart должен дропнуться через 1 min после endDate.

---

### 2.3 Zombie events Dec-2025 (133 days post-resolve, closed=false)

**Симптом:** gamma-api offset=2500+ возвращал events типа "Bitcoin Up or Down - December 19, 11:35AM ET" с `closed=false`, **резолвлено 133 дня назад**.

**Root cause:** Polymarket internal cleanup не удалял старые intraday events немедленно. Они "висят" в активных feed weeks/months.

**Где:** API behaviour, не наш bug. Но фильтр должен ловить.

**Phase / PR:** Phase 9v WINDOW_DAYS=13 + Phase 9kkk #41 past-resolve filter

**Fix:** `is_within_window` (`-86400*past_days <= diff <= 86400*max_days`) + explicit past-resolve check.

**Как проверить:** sample event с endDate -130d должен dropп'аться через `is_within_window` → `poly_skip_no_window`.

---

### 2.4 Stale orderbook cache (CF 30s TTL)

**Симптом:** Polymarket orderbook возвращает stale ask цены до 30s после реального обновления. Cache-Control `public, max-age=30, s-maxage=30` через Cloudflare.

**Root cause:** CF edge cache. Не наш bug, но нужно учитывать.

**Где:** `_fetch_clob`, `_fetch_limitless_orderbook`

**Phase / PR:** Phase 9kkk skill `time-freshness-validation`

**Fix:** Не доверять cached orderbook >30s. На fire — re-fetch.

**Как проверить:** `cf-cache-status: HIT` в response headers говорит что данные из CF cache.

---

## 3. Source/Price phantoms

### 3.1 `implied` source (lastTradePrice fallback)

**Симптом:** "Highest temperature in Munich 14°C" в Deals с YES@0.1¢, NO@65.5¢, sum=65.6¢, net=$28.41, источник **MID**, NO liquidity = $0. **Невозможно купить**.

**Root cause:** `eval_poly` fall back'ался на `outcomePrices[0]` (= lastTradePrice / midpoint) когда orderbook пустой. Synthetic `1 - yes_implied` для NO когда нет real ask. Source tag `'implied'` принимался как валидный.

**Где:** `arb_server.py:_poly_per_market` (yes_src/no_src logic, line 1115-1147), `build_deal` (line 1041)

**Phase / PR:** Phase 9kkk / **PR #38** (initial), **PR #43** (strict CLOB-only), **PR #44** (NEAR тоже)

**Fix:** `REAL_OB_SOURCES = {'clob_ask', 'kalshi_ob', 'sx_ob', 'lim_clob'}` — **только direct REST CLOB**. Дропнули `'ws'` и `'lim_ws'` (могут быть stale без notification). Применено в `build_deal` и `_best_near_structure`.

**Как проверить:** после fix — UI badge должен показывать только 🟢 CLOB/KALSHI/SX/LIM. Если 🔴 ⚠ MID — баг в filter.

---

### 3.2 WS book stale без notification

**Симптом:** Polymarket WS не шлёт `market_closed` events. После резолва WS book остаётся со stale ценами часами. Phase 9kkk #38 включал `'ws'` в `REAL_OB_SOURCES` → пропустил BTC 1PM ET phantom (PR #43 fix).

**Root cause:** Polymarket WS protocol limitation — нет market lifecycle events.

**Где:** `Scripts/poly_ws.py`

**Phase / PR:** Phase 9kkk / **PR #43**

**Fix:** Drop `ws` и `lim_ws` из `REAL_OB_SOURCES`. Trade-off: теряем <100ms WS-driven freshness ради zero stale-WS phantoms.

**Как проверить:** event с `closed=true`, but WS book ещё не updated → не должен попадать в deals.

---

### 3.3 Liquidity = 0 на одной ноге

**Симптом:** Deal accepted с YES liquidity $158k но **NO liquidity $0**. На скрине было видно "Min liq $0" — невозможно купить.

**Root cause:** `build_deal` не проверял `liquidity > 0` per leg.

**Где:** `arb_server.py:build_deal` (line 1041)

**Phase / PR:** Phase 9kkk / **PR #38**

**Fix:**
```python
for o in outcomes:
    if not (o.get('liquidity') or 0) > 0:
        return None  # cannot place taker order
```

**Как проверить:** test fixture с 0-liquidity leg → `build_deal` returns None.

---

### 3.4 SX Bet maker→taker price interpretation (silent zero detection)

**Симптом:** Phase 1 (PR #12) revealed: `_fetch_sx_orders` хранил `best1 = max(maker_bid where isMakerBettingOutcomeOne=True)` — это НЕ taker ask. SX Bet **не показывал ни одной сделки** хотя сканировал 638+ markets.

**Root cause:** API SX Bet даёт `percentageOdds` мейкера для стороны на которую он ставит. Тейкер берёт **противоположную** сторону по цене `1 − maker_price`.

**Где:** `arb_server.py:_fetch_sx_orders`

**Phase / PR:** Phase 1 / **PR #12** (27.04.2026)

**Fix:**
```
best1 = 1 − max(maker_bid_на_outcomeTwo)
best2 = 1 − max(maker_bid_на_outcomeOne)
```

**Как проверить:** на live SX market с двусторонним стаканом — `best1 + best2 < 1.0` если есть margin для taker.

---

## 4. Threshold / NEAR pool баги

### 4.1 White House posts April 28-May 5 (24¢ distance в NEAR)

**Симптом:** Multi-outcome event с 8 исходами в NEAR table с sum=120.9¢, distance **+23.9¢** (далеко за пределами NEAR_BUFFER=7¢). Структура показана **A** (ALL_YES).

**Root cause:** Двух-ступенчатая логика была inconsistent:
1. `classify_pools` принимал событие через `_sum_poly_cand` = `min(A_norm, B_norm, C_norm)` — для этого events `B_normalized = sum_no/(N-1) = 0.97` прошёл buffer
2. `_best_near_structure` для UI выбирал opt по `min(sum - threshold)` без buffer check → A с raw sum=121¢

**Где:** `arb_server.py:_best_near_structure` (line 2252)

**Phase / PR:** Phase 9kkk / **PR #45**

**Fix:** Buffer guard inside `_best_near_structure`:
```python
# A.ALL_YES
if s <= 1.5 and (s - threshold) <= NEAR_BUFFER:
    options.append({...})

# B.ALL_NO
b_threshold = (N - 1) * threshold
if s <= (N - 0.5) and (s - b_threshold) <= NEAR_BUFFER * (N - 1):
    options.append({...})
```

Также NEAR_BUFFER `0.07 → 0.03` (operator request).

**Как проверить:** event с raw distance > 3¢ не должен показываться в NEAR.

---

### 4.2 NEAR использовал static THRESH_POLY=0.97 (не dynamic)

**Симптом:** В NEAR table все Polymarket events показывали порог 97¢, независимо от market fee. 0% promo events должны были показывать 99¢.

**Root cause:** Phase 9k (PR #30) добавил `compute_poly_threshold(taker_fee_bps)` для main scan path. В `near_summary` остался legacy `THRESH_POLY=0.97`.

**Где:** `arb_server.py:near_summary` (line ~2371)

**Phase / PR:** Phase 9kkk / **PR #40**

**Fix:** Compute `cand_max_fee_bps` из rough markets (как в classify_pools line 2150), потом `compute_poly_threshold(fee)`.

**Как проверить:** 0%-fee promo event в NEAR должен показывать 99.7¢ (после PR #46), не 97¢.

---

### 4.3 POLY_SAFETY_BUFFER (страховка)

**Симптом (operator request):** "с порога 97 убери страхующее значение".

**Root cause:** Phase 9l (PR #31) добавил `POLY_SAFETY_BUFFER = 0.007` ко всем порогам. Оператор посчитал страховку избыточной (есть `POLY_SLIPPAGE_RESERVE = 0.003`).

**Где:** `arb_server.py:compute_poly_threshold` (line 199), `POLY_SAFETY_BUFFER` (line 196)

**Phase / PR:** Phase 9kkk / **PR #46**

**Fix:** `POLY_SAFETY_BUFFER = 0.0`. Threshold = `1 - (fee + slippage_reserve)`. Other platforms (Kalshi 0.93 / SX 0.97 / Limitless 0.988) не тронуты — у них static thresholds с встроенными margins.

**Per-fee thresholds после фикса:**
| fee | THRESH | UI |
|---|---|---|
| 0% | 0.997 | **99.7¢** |
| 2% (sport) | 0.977 | **97.7¢** |
| 2.5% (politics) | 0.972 | **97.2¢** |
| 4% | 0.957 | **95.7¢** |

---

### 4.4 NEAR_BUFFER одинаковый round() стирает разницу

**Симптом:** sport (2% fee) и politics (2.5% fee) события показывали оба 97¢ — после `round(_, 0)` 97.2 и 96.7 round до 97.

**Где:** `arb_server.py:near_summary` `'threshold_cents': round(best['threshold'] * 100, 0)`

**Phase / PR:** Phase 9kkk / **PR #45**

**Fix:** `round(_, 1)` — 1 decimal place. Теперь видно 97.2 vs 96.7.

---

### 4.5 Quarantined events утекли в NEAR (Nebraska Republican Primary)

**Симптом:** Live verification 16/17 PASS — Nebraska Governor Republican Primary visible в NEAR с sum=99.1¢, dist=+2.1¢. Должен быть в Карантине (есть groupItemTitle='Other').

**Root cause:** `filter_poly` корректно ставил `is_quarantine=True`, но `near_summary` распаковывал tuple как `ev, rough, _` — discarding flag. Quarantined events рендерились в NEAR как обычные.

**Где:** `arb_server.py:near_summary` poly_near loop (line ~2399)

**Phase / PR:** Phase 9kkk / **PR #48** (30.04.2026)

**Fix:**
```python
for cand in poly_near:
    ev, rough, is_quarantine = cand  # was: ev, rough, _
    if is_quarantine:
        continue  # quarantine ONLY in Карантин tab
    ...
```

**Как проверить:** events с `has_other_outcome` детектом не должны появляться в `/api/near`.

---

### 4.6 ALL_NO с N scaling buffer (Chongqing 8.8¢ distance)

**Симптом:** Live verification: "Highest temperature in Chongqing on May 1?" ALL_NO sum=299.8¢, distance=+8.8¢ — далеко за NEAR_BUFFER=3¢.

**Root cause:** PR #45 buffer guard для ALL_NO использовал `(s - b_threshold) <= NEAR_BUFFER * (N-1)`. Math correct (normalized), но визуально пользователь видит "8.8¢" что **не** "3¢ от threshold". Для N=3 buffer был 9¢ raw.

**Где:** `arb_server.py:_best_near_structure` ALL_NO branch

**Phase / PR:** Phase 9kkk / **PR #49** (30.04.2026)

**Fix:** dropнуть `* (N-1)` scaling — `(s - b_threshold) <= NEAR_BUFFER` strict 3¢ raw distance независимо от N.

**Как проверить:** ALL_NO sum=raw 295c при threshold=292c (N=3, distance=3c) — pass. distance >3c — drop.

---

### 4.7 Negative distance NEAR rows (stale snapshot mismatch)

**Симптом:** NEAR rows с `distance_cents < 0` — это ARB должен быть в HOT не NEAR.

**Root cause:** Async между classify_pools и near_summary — orderbook обновился, distance теперь negative.

**Где:** `arb_server.py:near_summary`

**Phase / PR:** Phase 9xx (commit `cdef83d`)

**Fix:** Drop rows с negative distance (filter в `near_summary`).

---

## 5. Wallet / executor / dry-fire

### 5.1 KeyError: 'price' (silent dry-fire failure)

**Симптом:** dryrun.jsonl пустой 32 часа. Каждый dry-fire падал с `[DRYFIRE] error firing yes_no_pair... 'price'`.

**Root cause:** `build_deal` создавал entries с `'price_cents'` (для UI), но `executor/atomic.py` читал `entry['price']` (raw 0-1) в 4 местах. Под старым строгим `_assign_wallets` (отказ при <N wallets) проблема была silent — fire abortится до builder. Phase 9kkk mock-pad для dry-run пропустил все деals → KeyError surfaced.

**Где:** `arb_server.py:build_deal` (entries dict), `executor/atomic.py:114, 129, 134, 149, 193, 312`

**Phase / PR:** Phase 9kkk / **PR #37**

**Fix:** Хранить ОБА поля:
```python
entries.append({
    'price': o['price'],          # raw 0-1 для executor
    'price_cents': round(o['price']*100, 1),  # UI
    ...
})
```

---

### 5.2 4+ leg arbs aborted (3 wallets, anti-detection)

**Симптом:** dryrun.jsonl пустой. С 3 can_sign wallets все Polymarket ALL_YES (3 ноги) ОК, но 4-leg аutorbs `aborted_reason: wallet_assignment_failed`.

**Root cause:** Phase 9i (PR #28) `_assign_wallets` отказывал если `len(wallets) < legs_count`. Логично для live mode (anti-detection: 1 нога = 1 wallet), но **в dry-run anti-detection не нужен** (нет реального POST).

**Где:** `executor/atomic.py:_assign_wallets` (line 349)

**Phase / PR:** Phase 9kkk / **PR #36** (initial mock-pad in `_maybe_dry_fire` flow)

**Fix:** В `dry_run=True` mode — pad pool with mock stubs:
```python
if len(wallets) < legs_count:
    if dry_run:
        padded = list(wallets)
        while len(padded) < legs_count:
            padded.append(WalletStub(bot_id=f'mock{i}', eth_address='0x'+...))
        return padded
    return []  # live mode strict
```

---

### 5.3 Risk-aware sizing (BALANCE $100 vs MAX_PER_TRADE_USD $55)

**Симптом:** paper_results.jsonl пустой 32 часа (другой root cause). Каждая сделка блокировалась.

**Root cause:**
1. `BALANCE = 100` в `arb_server.py`, `MAX_PER_TRADE_USD = 55` в risk module → каждая сделка превышала лимит
2. Pre-trade check предполагал 100% loss; для арба это неверно (max 5-15% slippage)
3. Блокировки **silent** — не писались в dryrun.jsonl

**Где:** `arb_server.py:build_deal`, `executor/atomic.py:fire_arb`, `risk/limits.py:check_can_fire`

**Phase / PR:** **PR #19** (28.04.2026)

**Fix:**
- `build_deal` capит `actual_balance = min(BALANCE × scale, MAX_PER_TRADE_USD)`
- `fire_arb` `log_decision` на ВСЕХ early-return paths (не silent)
- `check_can_fire` различает арбы (15% worst case) vs направленные позиции (100% loss)

---

### 5.4 jitter_ms_for_leg не вызывалась (anti-detection fingerprint)

**Симптом:** Все ноги fire'ились в API ±1ms — фингерпринт arb-bot.

**Root cause:** Функция `jitter_ms_for_leg` определена, но не вызывалась в `fire_arb`.

**Где:** `executor/atomic.py:fire_arb`

**Phase / PR:** Phase 9i / **PR #28**

**Fix:** wrap каждой ноги в `time.sleep(random.uniform(0, ASSIGN_JITTER_MAX_MS/1000))`.

---

### 5.5 Round-robin clob 2 ног на wallet0 (фингерпринт = бан)

**Симптом:** Биржа видит один адрес на обеих сторонах одного арба → бан.

**Root cause:** `_assign_wallets` использовал `wallets[i % len(wallets)]` round-robin. Если pool < legs_count → wallet[0] получал 2 ноги.

**Где:** `executor/atomic.py:_assign_wallets`

**Phase / PR:** Phase 9i / **PR #28**

**Fix:** Если `len(wallets) < legs_count` → return [] (caller aborts). Strict 1-leg-1-wallet.

---

### 5.6 ALL_NO gross_pct формула (отрицательный показатель)

**Симптом:** ALL_NO 3-leg arb sum=190.6¢ показывал `gross_pct = -90.5%` (катастрофа в UI), хотя real economics +1.8% (payout 200 - cost 190.6).

**Root cause:** Формула `(1 - total_price) / total_price` использовала payout=$1, но для ALL_NO payout = N-1.

**Где:** `arb_server.py:build_deal`

**Phase / PR:** Phase 9q / **PR #33**

**Fix:**
```python
'gross_pct': round((payout_target - total_price) / total_price * 100, 1)
```

---

### 5.7 killswitch fail-OPEN на permission error

**Симптом:** Operator жмёт STOP. Killswitch файл не создан из-за `PermissionError` → executor продолжает файрить.

**Root cause:** `is_killed()` возвращал `False` на любую exception (fail-OPEN).

**Где:** `risk/killswitch.py:is_killed`

**Phase / PR:** Phase 9i / **PR #28**

**Fix:** `try/except` fail-CLOSED — на permission error возвращаем `True` (assume kill).

---

## 6. HTTP errors

### 6.1 Limitless 403 (Cloudflare adaptive block)

**Симптом:** Все Limitless requests возвращали 403. Sklep сначала "пропадал на час", потом дольше. Без notification.

**Root cause:** Cloudflare adaptive rate-limit. Triggered by burst of >40 concurrent requests OR pattern matching DDoS heuristic.

**Где:** `_fetch_limitless_orderbook`, `async_fetchers.py:fetch_limitless_orderbook_async`

**Phase / PR:** Phase 9iii (initial HTTP/2) → Phase 9kkk (CB)

**Fix:**
1. **HTTP/2 multiplexing**: один TCP, N streams через httpx[http2]+h2 → не выглядит как burst
2. **Circuit breaker** (`Scripts/circuit_breaker.py`): 3 consecutive 403 → OPEN 5min → HALF_OPEN probe → CLOSED on success
3. **Retry-After honoring** на 429/503

**Как проверить:** на 403 → `[CB:limitless] CLOSED → OPEN: HTTP 403`. Через 5 min → HALF_OPEN → если success → CLOSED.

**Replay test:** имитировать 3 подряд 403 в mock client → CB должен open'нуться.

---

### 6.2 SX Bet pageSize=200 → HTTP 400

**Симптом:** SX Bet возвращал `400 Bad Request "pageSize must not be greater than 100"`. Ни одной сделки.

**Root cause:** SX Bet API внезапно ужесточил лимит (был 200), мы не заметили без диагностики.

**Где:** `arb_server.py:run_scan` SX block

**Phase / PR:** **PR #5** (26.04.2026, после **PR #4** diagnostics)

**Fix:** `SX_PAGE_SIZE = 100`. Также вынес в config константу + увеличил `SX_MAX_PAGES_MAIN = 10` (1000 markets).

---

### 6.3 Polymarket 403 (Cloudflare burst)

**Симптом:** Polymarket gamma-api начал возвращать 403 при concurrent requests >40.

**Root cause:** То же что и Limitless — CF rate-limit.

**Где:** `_fetch_clob`, `_fetch_poly_market_info`

**Phase / PR:** Phase 9kkk skill `http-rate-limiting`

**Fix:** Circuit breaker + http_codes classifier (хотя пока только Limitless wired). Polymarket работает на 30 concurrent stable.

---

### 6.4 Limitless 5min handshake hangs (TLS)

**Симптом:** Лимитлесс scan 761 секунд. Запросы зависали на TLS handshake.

**Root cause:** Polymarket socketio reconnect storm + meta-fetcher без Session pooling = новый TLS handshake на каждый request. На flaky Limitless connection — handshake занимал 4341ms max.

**Где:** `_fetch_limitless_market_meta` (без Session), `_fetch_clob` (без Session)

**Phase / PR:** Phase 9rr / Phase 9ss / **PR #33**

**Fix:** `requests.Session` + sized HTTPAdapter с `_make_session(MAX_WORKERS)`. TLS handshake reused → 5x faster.

---

### 6.5 Universal HTTP code handling (13 codes)

**Phase / PR:** Phase 9kkk / **PR #36**

**File:** `Scripts/http_codes.py` (новый)

**Catalog:**

| Status | Action | Retry? | Comment |
|---|---|---|---|
| 200 | SUCCESS | — | Parse body |
| 304 | SUCCESS | — | Not Modified, use cache |
| 400 | SKIP_CLIENT_ERR | NO | Config bug (e.g. SX pageSize) |
| 401 | SKIP_CLIENT_ERR | NO | Missing auth header |
| 403 | OPEN_BREAKER | NO | CF block / geo-block |
| 404 | NOT_FOUND | — | Resource removed |
| 422 | SKIP_CLIENT_ERR | NO | Bad body shape |
| 429 | RETRY_BACKOFF | YES (3) | Honour Retry-After |
| 502 | RETRY_TRANSIENT | YES (2) | CF↔origin failed |
| 503 | RETRY_BACKOFF | YES (3) | Origin overload |
| 504 | RETRY_TRANSIENT | YES (2) | Origin timeout |
| 521 | RETRY_TRANSIENT | YES (2) | Origin offline |
| 522 | RETRY_TRANSIENT | YES (2) | Connection timed out |
| 524 | RETRY_TRANSIENT | YES (2) | Origin took >100s |
| 525 | OPEN_BREAKER | NO | SSL handshake failed |
| 526 | OPEN_BREAKER | NO | Invalid SSL cert |

---

### 6.6 negRisk gating отбрасывал ~100% Polymarket кандидатов

**Симптом:** На 1000 Polymarket events, `poly_neg_risk: 0` всегда. Ноль deals.

**Root cause:** `filter_poly` проверял `market.negRisk` (всегда False), но Polymarket кладёт `negRisk` на уровне **event** (один из 20).

**Где:** `arb_server.py:filter_poly`

**Phase / PR:** **PR #3** (26.04.2026)

**Fix:** `event.negRisk OR all(market.negRisk)` — disjunction.

---

### 6.7 SX Bet геоблок (потенциальный, не подтверждён)

**Симптом:** Phase 6 audit показал что VPS в Frankfurt должен иметь доступ. Empirical: Phase 9kkk verified `https://api.sx.bet/markets/active` = HTTP/2 200 на VPS 77.91.97.22.

**Где:** Phase 9kkk / `deploy/VERIFICATION.md`

**Fix:** Verification curl baseline в Phase 5.

---

## 7. Concurrency / lock / race

### 7.1 `_fired_arb_keys` unbounded leak

**Симптом:** Memory grew across 24h+ uptime.

**Root cause:** `_fired_arb_keys` set добавлял ключи навсегда, никогда не evict.

**Где:** `arb_server.py:_maybe_dry_fire`

**Phase / PR:** Phase 9uu / **PR #33**

**Fix:** Eviction logic — drop keys whose deal not in active deals. Hard cap `_FIRED_KEYS_HARD_CAP=5000` safety net.

---

### 7.2 `_maybe_dry_fire` lock during fire (5s serialization)

**Симптом:** WS callbacks блокировались 5 секунд за раз. Race window для double-fire.

**Root cause:** Lock держался во время `fire_arb` (5s dead-man timeout).

**Где:** `arb_server.py:_maybe_dry_fire`

**Phase / PR:** Phase 9i / **PR #28**

**Fix:** Two-phase commit — reserve keys atomically inside lock, fire **outside** lock.

---

### 7.3 WS book locks (poly_ws / limitless_ws)

**Симптом:** Race condition при WS update — book mid-modification, eval reads inconsistent state.

**Где:** `Scripts/poly_ws.py`, `Scripts/limitless_ws.py`

**Phase / PR:** Phase 9uu / **PR #33**

**Fix:** Per-token locks при update.

---

### 7.4 ThreadPoolExecutor shutdown race

**Симптом:** Hung worker блокировал scan_loop indefinitely.

**Root cause:** `with ThreadPoolExecutor()` `__exit__` ждёт ВСЕХ workers. Один зависший worker = scan never completes.

**Где:** `arb_server.py:batch_fetch`

**Phase / PR:** Phase 9qq.4 / **PR #33**

**Fix:** Manual `pool = ThreadPoolExecutor(...)`, `as_completed(timeout=...)` actually fires, `pool.shutdown(wait=False)` — не ждём hung workers.

---

### 7.5 `_next_utc_midnight` 31-го числа (ValueError)

**Симптом:** Killswitch fail на последний день месяца.

**Root cause:** `datetime(now.year, now.month, now.day+1)` → ValueError 31 Apr.

**Где:** `risk/state.py`

**Phase / PR:** Phase 9tt / **PR #33**

**Fix:** Use `datetime + timedelta(days=1)` который правильно обрабатывает month boundary.

---

## 8. UI / cache / deploy

### 8.1 Browser cache stale dashboard.html (deploy invisible)

**Симптом:** Deploy лendsetc на VPS, оператор открывает kapkan.4frdm.live и видит вчерашний UI. Chrome кэширует dashboard.html.

**Root cause:** Нет `Cache-Control: no-cache` header.

**Где:** `arb_server.py:dashboard route`

**Phase / PR:** Phase 9eee.1

**Fix:** `Cache-Control: no-cache, must-revalidate`. Также в skill `browser-cache-busting`.

**Workaround для оператора:** Ctrl+Shift+R (hard reload).

---

### 8.2 'const deals' double-declared SyntaxError

**Симптом:** dashboard JS не загружается.

**Где:** `dashboard.html`

**Phase / PR:** Phase 9eee.2

**Fix:** Rename second declaration to avoid `const deals` collision.

**Prevention:** `Scripts/lint_dashboard_js.py` (PR #33 Phase 9ggg) — pre-commit JS lint via `new Function()`.

---

### 8.3 Defensive null-checks (cascade UI failure)

**Симптом:** Один отсутствующий field ломал ВСЕ tabs.

**Where:** `dashboard.html` various render functions

**Phase / PR:** Phase 9eee.1

**Fix:** Helper `_setText(elId, value, fallback='—')`. Wrap `updateUI(data)` в `try/catch`.

---

### 8.4 Кнопки Approve/Reject 404 (legacy from PR #23)

**Симптом:** Operator click → ничего не происходит.

**Root cause:** PR #23 удалил `/api/approve` / `/api/reject` endpoints (manual decision flow). Кнопки в dashboard.html остались, ссылались на 404.

**Где:** `dashboard.html:createDealCard`, `actionDeal()` JS function

**Phase / PR:** Phase 9kkk / **PR #39**

**Fix:** Убрал кнопки + dead `actionDeal()`. Replaced с label "Авто-блок: executor не файрит карантинные сделки".

---

### 8.5 Docker `restart` НЕ rebuild image (старый код в контейнере)

**Симптом:** После `docker compose restart radar` фиксы не применяются. Контейнер крутит старый image.

**Root cause:** `restart` использует existing image. Только `up --build` пересобирает.

**Где:** Deploy procedure (мой workflow bug)

**Phase / PR:** Phase 9kkk discovery

**Fix:** Всегда `docker compose down + up -d --build` после изменения Python кода. Verify через `md5sum host vs container`.

**Critical:** добавлено в `deploy/DEPLOY_PLAYBOOK.md`.

---

### 8.6 PowerShell `Set-Content -Encoding UTF8` adds BOM

**Симптом:** Python script error `SyntaxError: invalid non-printable character U+FEFF`.

**Root cause:** PS UTF8 encoding includes BOM. Python не любит.

**Где:** Мой workflow.

**Fix:** Использовать **Edit tool** для file edits (UTF-8 без BOM, LF endings). Если PS — `[IO.File]::WriteAllBytes()` с raw bytes.

---

## 9. Cross-platform parity

### 9.1 `_resolve_lim_end_date` не использовался в `eval_limitless`

**Симптом:** Limitless events в NEAR показывали "Конец: —" для events с newer API формате (`expirationDate` field).

**Root cause:** `filter_limitless` использовал helper, `eval_limitless` имел inline 2-field parse.

**Где:** `arb_server.py:eval_limitless` (line ~1574)

**Phase / PR:** Phase 9kkk / **PR #36**

**Fix:** Replaced inline parse с `_resolve_lim_end_date(ev)` helper (8 fields probe).

---

### 9.2 SX Bet status filter (closed/resolved missing)

**Симптом:** SX Bet markets с `status != 1` (closed/resolved) попадали в eval.

**Root cause:** `eval_sx` не проверял `status` field.

**Где:** `arb_server.py:eval_sx` (line ~1433)

**Phase / PR:** Phase 9kkk / **PR #36**

**Fix:**
```python
status = m.get('status')
if status is not None and status != 1:
    continue
if m.get('outcome') is not None and m.get('outcome') != 0:
    continue  # already settled
```

---

### 9.3 Limitless implementation parity (added в PR #25)

Limitless добавлен как 4-я платформа в **PR #25** с full A/B/C structures + EIP-712 + filter parity с Polymarket.

---

## 10. Risk / safety

### 10.1 MAX_PER_TRADE_USD как `sum(legs)` не per-leg

**Симптом:** 3-leg arb $20/нога ($60 total) блокировался при `MAX_PER_TRADE_USD=$55`. P&L резался ×3.

**Root cause:** `check_can_fire` считал total cost не per-leg.

**Где:** `risk/limits.py:check_can_fire`

**Phase / PR:** Phase 9i / **PR #28**

**Fix:** Per-leg cap. `_trade_total_cost` для рассмотрения.

---

### 10.2 Daily loss limit reset timing

**Симптом:** `paused_until` подсчёт мог быть неправильным на month boundary (см. 7.5).

**Phase / PR:** Phase 9tt / **PR #33**

---

### 10.3 Position reconciliation halt

**Симптом:** Mismatch between local `positions.jsonl` и `/positions` API на бирже → `halt_trading()`.

**Phase / PR:** Phase 3 / **PR #14**

**Где:** `risk/reconcile.py`

**Threshold:** `> $0.01` mismatch → halt.

---

## 📊 Метрики и счётчики (`/api/stats`)

**Diagnostic counters** (добавлены в PR #4):
- `poly_in`, `poly_pass` — общее количество событий
- `poly_skip_blacklist` — title в blacklist
- `poly_skip_no_window` — endDate вне `WINDOW_DAYS=13`
- `poly_skip_lt2_markets` — <2 outcomes (для multi)
- `poly_skip_no_negrisk` — отсутствует negRisk
- `poly_skip_lt2_rough` — после filtering <2 priced outcomes
- `poly_skip_sum_high` — sum > threshold
- `poly_skip_deadline_text` — title contains deadline text
- `poly_skip_closed` — `closed=True`
- `poly_skip_outcome_closed` — child closed
- **`poly_skip_past_resolve`** ← Phase 9kkk #41 zombie filter

**Аналогично** для kalshi/sx/limitless.

**Pool sizes:**
- `pool_poly_hot/near` — events в HOT/NEAR pool
- Аналогично для kalshi/sx/lim

**Health:**
- `pool_total` — общее число активных кандидатов
- `arb_found` — текущие deals (HOT)
- `quarantine_count` — карантин

---

## 🔧 Quick reference: где искать

| Симптом на дашборде | Где смотреть |
|---|---|
| Phantom in Deals/NEAR | secs 1-3 (filter bypass + time + source) |
| Источник MID или 0$ liquidity | sec 3 (source phantoms) |
| Distance > NEAR_BUFFER в NEAR | sec 4 (threshold) |
| dryrun.jsonl пустой | sec 5 (executor) |
| 403/429/502 в логах | sec 6 (HTTP) |
| Lock contention / hang | sec 7 (concurrency) |
| Stale UI после deploy | sec 8 (cache/deploy) |
| Limitless "Конец —" | sec 9 (cross-platform) |
| Killswitch не работает | sec 10 (risk) |

---

## 🎯 Anti-pattern checklist (запомнить)

1. **`closed=false` ≠ "событие активно"** — всегда чек endDate явно
2. **Server-provided flags ≠ truth** — добавляй explicit time arithmetic
3. **`m.get('a') or m.get('b')`** — short-circuits, используй list-collection
4. **Generic regex** — добавляй "another", "different", language variants
5. **WS book without lifecycle events** — может быть stale бесконечно
6. **Cache TTL = CDN TTL (30s)** — beyond that stale
7. **`docker compose restart` без `--build`** — старый image
8. **PS `Set-Content -Encoding UTF8`** — BOM ломает Python
9. **`raw_sum > 1.0`** для multi-outcome — может быть threshold-series
10. **`liquidity == 0`** — нельзя купить, не deal
11. **`source = 'implied'`** — это lastTradePrice, не ask
12. **Round() стирает разницу** — используй 1+ decimal для cents
13. **Pool entry хранится между scans** — eviction MUST run
14. **Unbounded set** — leak. Hard cap всегда.
15. **Session per host_key** — TLS reuse экономит 200ms/call

---

## 📋 Refs

- **CHANGELOG.md** — навигация по PR/Phase/File
- **deploy/DEPLOY_PLAYBOOK.md** — поэтапная инструкция
- **deploy/ROLLBACK.md** — что делать если упало
- **deploy/smoke_test.sh** — 10 проверок post-deploy
- **deploy/VERIFICATION.md** — Phase 5 baseline tests
- **.claude/skills/time-freshness-validation/SKILL.md** — патерны для time-related
- **.claude/skills/circuit-breaker-patterns/SKILL.md** — для CF rate-limit
- **.claude/skills/http-rate-limiting/SKILL.md** — backoff + multiplexing
- **.claude/skills/secrets-management/SKILL.md** — wallet keys
- **CLAUDE.md** — project memory + PR procedure

---

## 📅 Поседняя сессия (30.04.2026): 11 PR'ов

| # | Симптом | Root cause | Fix |
|---|---|---|---|
| #36 | bundle Phase 9kkk | parallel fetch + Other-filter + ... | 6 ops wins |
| #37 | KeyError 'price' | entries had 'price_cents' only | both fields |
| #38 | MID source phantoms | implied accepted as ask | REAL_OB_SOURCES check |
| #39 | dead Approve/Reject buttons | PR #23 removed endpoints | replaced with label |
| #40 | NEAR all 97¢ | static THRESH_POLY in near_summary | dynamic per-fee |
| #41 | Munich 12:00 zombies | gamma closed=false hours after | endDate >60min drop |
| #42 | BTC 1PM ET phantom | flat 60min grace too long | adaptive by duration |
| #43 | strict CLOB-only | ws/lim_ws cаn be stale | drop from REAL_OB_SOURCES |
| #44 | NEAR also strict | _best_near_structure no source check | filter pm by source |
| #45 | White House 24¢ distance | no buffer guard in _best_near_structure | (s-thr) <= NEAR_BUFFER |
| #46 | drop POLY_SAFETY_BUFFER | operator request | 0.007 → 0 |
| #47 | docs catalog | needed reference | BUG_CATALOG.md (957 строк) |
| #48 | Nebraska Republican в NEAR | unpacked tuple discarded is_quarantine | unpack + skip |
| #49 | Chongqing 8.8¢ ALL_NO | scaled buffer let N×3 raw distance | drop scaling, strict 3¢ |

---

**Последнее обновление:** 30.04.2026 (Phase 9kkk completed, **14 PR'ов в main за день**, 49 за всю историю проекта). Live regression check: **17/17 PASS**.

---

## 🔁 Memo: процесс работы с этим каталогом

**Каждый раз когда ловим новый bug:**

1. Добавить запись в соответствующую секцию (1-10) с полями:
   - **Симптом** — что увидел оператор (с конкретным примером)
   - **Root cause** — техническая причина
   - **Где** — `file:line`
   - **Phase / PR** — для git navigation
   - **Fix** — code excerpt
   - **Как проверить** — regression test или live check

2. Добавить строку в "Поседняя сессия" table (PR + симптом + root + fix)

3. Если новый класс ошибок — обновить `Anti-pattern checklist`

4. **CHANGELOG.md** — параллельная запись (PR # + branch + summary)

**При повторении уже задокументированного bug:**
- Не реверти fix
- Проверь — может новый sub-case того же класса (тогда расширить existing entry)
- Поверни к skill docs (`.claude/skills/`) если pattern recurring

**Перед каждым deploy:**
- `bash deploy/smoke_test.sh` — 10 проверок
- `python .tmp_bug_verify.py` — 17 BUG_CATALOG regression checks (если запускаешь живую verification)

См. также `deploy/DEPLOY_PLAYBOOK.md` для полного flow.
