# 🔍 Полный аудит проекта plan-kapkan — 11.05.2026

**Когда:** сессия 11.05.2026 после TS-5/TS-6 sprint + 19 PR'ов (#128-#146).
**Кто:** автоматический пересмотр с нуля каждого ключевого файла.
**Цель:** выявить баги, слепые зоны, code quality issues — НЕ ради ругани, ради roadmap'а на cleanup.

---

## 📊 Метрики проекта

| Метрика | Значение |
|---|---|
| **Python LoC** | 18,683 (в `Scripts/`) |
| **TypeScript LoC** | 5,040 (в `executor-ts/src/`) |
| **Dashboard JS+HTML** | 1,490 |
| **Python tests** | 68 файлов |
| **TS tests** | 16 файлов (147 test cases) |
| **PR'ов всего** | 146 |
| **Skills** | 38 (37 unique + 1 новый этой сессии) |

**Крупнейшие файлы (рефактор-кандидаты):**

| Файл | LoC | Комментарий |
|---|---|---|
| `Scripts/arb_server.py` | **6,533** | МОНОЛИТ — 100 функций, Flask routes, scan loops, eval logic |
| `Scripts/executor/atomic.py` | 1,433 | Python execution layer (TS executor wraps это) |
| `Scripts/executor/builders.py` | 1,111 | EIP-712 builders (TS port уже есть) |
| `Scripts/async_fetchers.py` | 1,009 | httpx parallel fetchers |
| `Scripts/cross_platform.py` | 763 | X1/X2 cross-platform pairing |
| `executor-ts/src/executor/atomic.ts` | 538 | TS executor entry |
| `Scripts/dashboard.html` | 1,490 | UI — HTML + ~77 JS funcs |

---

## 🐛 Найденные баги

### 🔴 Высокий приоритет

#### BUG-A1: Bare `except:` блоки в arb_server.py (2 места)
**Где:**
- `Scripts/arb_server.py:4156` — `except: continue` в Polymarket outcome parsing
- `Scripts/arb_server.py:4198` — `except: pass` в token_id_no extraction

**Проблема:** Bare except ловит `KeyboardInterrupt`, `SystemExit`, любые Python errors. Замаскирует баги типа AttributeError, ImportError, etc. Anti-pattern.

**Fix:** Заменить на `except Exception:` или конкретные типы.

#### BUG-A2: Полное отсутствие тестов для cross-platform fee math (Phase 19v34)
**Проблема:** PR #19v34 (cross-platform fee/gross/roi/adj_roi) — много новой math'и, ноль unit tests. Возможные баги в `total_fee = leg_cash * theta_leg` summation, в `slip_pct` formula — всплывут только в проде.

**Fix:** Создать `tests/test_phase19v34_cp_fee_roi.py` с golden examples.

#### BUG-A3: Test pytest I/O capture бьётся на Windows+anaconda
**Симптом:** `test_phase19v35_recent_deals.py`, `test_ts_metrics_proxy.py` падают локально с `ValueError: I/O operation on closed file` на Windows+anaconda Python 3.12.

**Root cause:** import `arb_server` запускает фоновые threads/daemons которые flush'ат stdout после teardown'а pytest. Pytest's capture mechanism уже закрыл tmpfile → exception.

**Workaround:** запускать с `-p no:cacheprovider --capture=no`. Не идеально.

**Fix:** Refactor `arb_server.py` чтобы import не стартовал background threads. Сейчас при `import arb_server` стартует gunicorn workers + WS clients. Нужен `if __name__ == '__main__': main()` guard.

### 🟡 Средний приоритет

#### BUG-B1: TS executor WS listeners `subsDesired=0` для всех ботов
**Симптом:** `/api/ts_metrics` показывает `poly_user_ws[].subsDesired=0`, `connected=false` для всех 6 ботов. WS клиенты подняты, но никаких маркетов не подписали.

**Root cause:** TS-5b1/b2 inсtance'ы PolyUserWS / LimitlessUserWS, вызывают `start()`, но `updateMarkets()` НИКТО не вызывает. atomic.ts должен вызывать `getPolyUserWS(botId).updateMarkets([...prevSet, conditionId])` перед каждым fire — это TS-5c.3, не выполнено.

**Fix:** **Это приоритет #1 в todo.** ~150 LoC в `atomic.ts.fireLeg` real-mode branch.

#### BUG-B2: `_snapshot()` НЕ ловит NEAR events
**Симптом:** analytics_events.jsonl содержит только `opened`/`closed` для HOT pool. NEAR pool drift'ы (когда сделка ходит близко к threshold но не пересекла) — не записаны.

**Эффект:** Cannot do post-hoc threshold investigation для NEAR-only events (типа Kilmarnock с порогом 94.8).

**Fix:** Phase audit-extras добавил `/api/recent_near` (PR #146) — public endpoint для view, но запись в analytics всё равно нет. Можно ввести `_append_event('neared', ...)` для NEAR transitions.

#### BUG-B3: Unused imports (10 модулей)
**Где:** автоматический AST scan нашёл:
- `Scripts/arb_server.py`: `_json`, `timedelta` (unused)
- `Scripts/async_fetchers.py`: `Tuple`, `os` (unused)
- `Scripts/cross_platform.py`: `Dict`, `MatchCandidate`, `canonicalize_outcome_name`, `field`, `match_event` (unused)
- `Scripts/paper_trading.py`: `Counter`
- `Scripts/poly_ws.py`: `defaultdict`
- `Scripts/executor/__init__.py`: 5 unused exports
- `Scripts/risk/limits.py`: `threading`
- `Scripts/risk/__init__.py`: 5 unused exports
- `Scripts/wallets/rebalance.py`: `Wallet`
- `Scripts/wallets/stores.py`: `win32cred` (вероятно conditional Windows-only — OK)

**Fix:** Простой sweep `ruff --fix --select F401`. ~10 минут.

#### BUG-B4: 79 `except Exception` блоков в arb_server.py
**Проблема:** Слишком много catch-all. Маскирует баги. Часто после `except Exception as e:` идёт просто `pass` или `print(e)` без структурированного логирования.

**Fix:** Audit each — оставить только там где **realistic recovery path** есть. Остальные заменить на конкретные exception types.

### 🟢 Низкий приоритет

#### BUG-C1: `Scripts/wallets/stores.py` импортит `win32cred` безусловно
**Симптом:** Linux/MacOS прод упадёт на import если `WALLET_BACKEND=windows_cred` указан. Конкретно `__pycache__` показывает `pyc` файлы что значит import был успешен где-то.

**Fix:** Wrap в `try: import win32cred / except ImportError: win32cred = None`.

#### BUG-C2: Stale TODO в arb_server.py
- Line 501: `# via separate pipeline (TODO Phase 17)` — Phase 17 был месяц назад
- Line 2381-2386: `# TODO: query separate "Draw No Bet"` — SX 3-way orderbook investigation

**Fix:** Решить — делать или удалить TODO.

#### BUG-C3: `executor-ts/src/executor/atomic.ts:402` TODO `(TODO TS-3 follow-up)` — устарел
TS-3 был месяц назад. Текущая фаза TS-6.

**Fix:** Удалить или переименовать в `(TODO TS-7)`.

#### BUG-C4: `dashboard.html` имеет 0 TODO/FIXME — хорошо, но 1490 LoC inline JS — тяжело тестировать
**Fix:** Долгосрочно — выделить JS в отдельный `.js` файл, hash для cache-busting (уже частично сделано). Сейчас не критично.

---

## 🔍 Слепые зоны (что мы НЕ можем увидеть)

### SZ-1: TS executor /fire actually called?
Радар вызывает `_fire_arb_via_ts(deal)` если `EXECUTOR_URL` задан. Но сейчас deals тoyne — попадают ли они в TS executor?

**Узнать:** `docker exec plan-kapkan-radar tail -100 /app/Executions/*.log | grep "ts-bridge\|_fire_arb_via_ts"`. Нет публичного endpoint показывающего сколько /fire запросов TS executor получил.

**Fix:** Добавить counter в TS executor `/metrics` — `total_fire_requests`, `successful_fires`, `fallback_to_python`.

### SZ-2: Real-time scan health
**Что не видно:** сколько кандидатов отбрасывается на quality gate, какой fetch latency сейчас, чем занят radar process.

**Сейчас есть:** `/api/scan_state` (auth'd), `/api/circuit_breakers` (auth'd).

**Не хватает:** Public `/api/scan_health` snapshot — tick count, last_scan_ts, fetch_latency_p50/p99, current pool sizes.

**Fix:** Phase audit-extras follow-up — это в моём списке как pending.

### SZ-3: Paper trade evaluation accuracy
**Проблема:** `executor.atomic.evaluate_paper_trade` schedules re-fetch через 5 секунд и пишет `realistic_pnl`. Но НИКАКИХ метрик про сам evaluator — сколько evaluated, сколько skipped (e.g., orderbook empty at evaluation time)?

**Fix:** Counter в `/api/paper_stats` — `evaluated_count`, `skipped_no_orderbook`, `skipped_market_closed`.

### SZ-4: Cross-platform pairing diagnostics
**Что:** `find_cross_platform_arbs(pool_a, pool_b, min_confidence)` есть, но **счётчики rejection reasons** недоступны — почему 10000 событий → 5 deals?

**Fix:** Diag dict в cross_platform.py с counters: low_confidence, settlement_diff_too_large, complement_threshold_fail, etc. Surface через `scan_state['cp_diag']`.

---

## 🎨 Code quality observations

### Q-1: `arb_server.py` это **monolith** (6,533 LoC)
**Что:** Routes + scan loops + filter funcs + eval funcs + NEAR summary + WS supervision — всё в одном файле.

**Compare to:** `cross_platform.py` (763) выделен. `analytics.py` (424) выделен. `executor/atomic.py` (1,433) выделен.

**Что НЕ выделено:** filter_poly/limitless/kalshi/sx, eval_*, near_summary, classify_pools, scan_loop, micro_loop'ы. ~3,500 LoC из 6,500.

**Рекомендация:** Будущий рефактор — выделить `Scripts/scanner/` с filter.py / eval.py / pools.py / scan_loop.py. Сейчас в этом боль и пользы нет, но грязно.

### Q-2: TS executor — солидный код
`executor-ts/src/` структурно правильный:
- `builders/` — pure EIP-712 builders
- `fire/` — HTTP transport
- `ws/` — user-channel WS listeners
- `executor/` — orchestration (atomic, fills, slippage, revert, paper)
- `risk/` — gates (limits, killswitch, state)
- `lib/` — utility (http_client, paths, poly_hmac)
- `types/` — type definitions

100/100 tests pass + typecheck clean + строгий tsconfig. **Это качественный код.** Без замечаний.

### Q-3: Tests asymmetric
**Python:** 68 test files, но многие на устаревшие версии PR'ов (Phase 9-19), некоторые fail локально (Windows pytest issue).

**TS:** 16 test files но **147 test cases** — coverage gold-standard (EIP-712 parity, hermetic WS mocks, slippage edge cases).

**Рекомендация:** Aim for **same density** на Python — особенно для critical paths (filter_poly, eval_poly, build_deal).

### Q-4: Скиллы — несколько подозрительно коротких
| Скилл | LoC | Подозрение |
|---|---|---|
| `opus-4-7-migration` | 43 | Возможно устарел (Opus 4.7 release notes на ту-же сессию когда мы мигрировали) |
| `polymarket-query` | 44 | Скорее всего просто URL + headers — пусть будет |
| `kalshi-markets` | 46 | Kalshi disabled — может удалить |
| `dr-manhattan` | 83 | Нелогичное имя, не ясно что внутри |

**Fix:** Audit каждый — сохранить нужные, удалить мёртвые.

---

## 🟢 Что работает хорошо

1. **TS executor** — отдельная папка, чистая архитектура, golden parity тесты, typecheck strict
2. **CHANGELOG.md** — структурированный, easy to navigate
3. **BUG_CATALOG.md** — 6 секций, anti-patterns documented
4. **Skill system** — 37+ скиллов покрывают практически все технологии
5. **Auto-deploy с version-gate** — PR #141 фикс закрыл silent-staleness класс
6. **Risk gates** в Python `risk/*.py` — kill switch fail-CLOSED, daily loss tracking, hourly losing rate
7. **Cross-platform pairing** — separate module, clean abstraction
8. **Public audit endpoints** — `/api/recent_deals`, `/api/ts_metrics`, `/api/recent_near` (после workflow) — agent visibility без operator basic auth

---

## 📋 Roadmap из аудита

| Приоритет | Что | Effort |
|---|---|---|
| **1** | TS-5c.3 — atomic.ts ↔ fillRegistry wiring (SZ-1, BUG-B1) | ~150 LoC |
| **2** | Bare except sweep — BUG-A1 + 79 except-Exception audit | ~50 LoC |
| **3** | Cross-platform fee math golden tests — BUG-A2 | ~100 LoC test |
| **4** | `arb_server.py` import side-effects fix — BUG-A3 | ~30 LoC |
| **5** | Unused imports cleanup — BUG-B3 | ~10 LoC, `ruff --fix` |
| **6** | `_snapshot()` NEAR events — BUG-B2 | ~20 LoC |
| **7** | `/api/scan_health` public endpoint — SZ-2 | ~80 LoC |
| **8** | Counters в `/api/paper_stats` — SZ-3 | ~50 LoC |
| **9** | Cross-platform diag dict — SZ-4 | ~30 LoC |
| **10** | Skill cleanup — стары unused (opus-4-7, kalshi-markets) | ~5 минут |
| **11** | `arb_server.py` рефакторинг в `Scripts/scanner/` модули — Q-1 | большой, отдельно |

---

## 📌 Summary

**Хорошо:**
- TS executor — солидный модульный код, проходит typecheck strict, 147 тестов
- Risk gates fail-CLOSED, kill switch работает
- Auto-deploy stable после #141 fixes
- Документация (CHANGELOG/BUG_CATALOG/Skills) хорошо ведётся

**Что чинить:**
- `arb_server.py` слишком большой и имеет bare excepts
- Python тесты не симметричны TS — нужно бы increase coverage
- WS listeners в TS подняты но никем не fed'ятся (TS-5c.3)
- Несколько устаревших TODO

**Что добавить:**
- `/api/scan_health` public для blind-spot #5
- Counter в TS `/metrics` для fire requests
- Cross-platform diag dict

**Сейчас критичных багов нет** — система DRY_RUN работает корректно. Real money trading заблокирован тремя НЕ-кодовыми вещами:
1. Operator должен заполнить `BOT*_LIMITLESS_API_KEY` + `BOT*_POLY_*` L2 креды
2. Run `polymarket_approve.py` per bot (on-chain)
3. 100 paper trades graduation gate
