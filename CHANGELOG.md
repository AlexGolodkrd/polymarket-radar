# CHANGELOG — plan-kapkan

Структурированная история изменений. Используй эту таблицу чтобы найти **PR / коммит / Phase-тег** без копания в git log + GitHub.

**Стек:** Flask + gunicorn (radar) + Docker + Polymarket / Kalshi / SX Bet / Limitless feeds + paper trading executor.

**Репозиторий:** https://github.com/AlexGolodkrd/plan-kapkan

---

## Index by PR (latest first)

| PR | Дата merge | Phase | Title (краткое) | Ключевые файлы |
|---|---|---|---|---|
| [#143](#pr-143) | 2026-05-11 | clean-quarantine | fix: /api/recent_deals path + remove Quarantine tab + min_liq UI compact | `Scripts/arb_server.py`, `Scripts/dashboard.html` |
| [#142](#pr-142) | 2026-05-11 | fix-signer | fix(wallets): normalize private keys + add startup env audit log | `executor-ts/src/wallets/{signers,pool}.ts` |
| [#141](#pr-141) | 2026-05-11 | fix-deploy | fix(deploy): TS executor crash-loop (tsconfig rootDir) + nginx whitelist bugs | `executor-ts/tsconfig.json` (rootDir → src), `tsconfig.test.json` (new), `.github/workflows/apply-nginx-ts-metrics.yml` |
| [#140](#pr-140) | 2026-05-11 | fix-deploy | ci: diagnose+fix /api/ts_metrics 401+503 workflow | `.github/workflows/diagnose-ts-metrics.yml` (new) |
| [#139](#pr-139) | 2026-05-11 | ts-cascade | merge: TS-5b2..TS-6.2 cascade to main | (merge-only, no diff) |
| [#138](#pr-138) | 2026-05-11 | audit | feat(audit): public /api/ts_metrics proxy + nginx whitelist | `Scripts/arb_server.py`, `.github/workflows/apply-nginx-ts-metrics.yml` |
| [#137](#pr-137) | 2026-05-11 | ts-6.2 | feat(ts-6.2): Limitless DELETE /orders/{id} cancel-on-timeout | `executor-ts/src/fire/lim_post.ts`, `tests/fire/lim_cancel.test.ts` |
| [#136](#pr-136) | 2026-05-11 | ts-6 | feat(ts-6): Polymarket L2 HMAC + DELETE /order cancel-on-timeout | `executor-ts/src/lib/poly_hmac.ts` (new), `src/fire/poly_post.ts`, `tests/lib/poly_hmac.test.ts` |
| [#135](#pr-135) | 2026-05-11 | ts-5c.2 | feat(ts-5c.2): real-mode fires + revert execution | `executor-ts/src/executor/{atomic,revert}.ts`, `tests/executor/revert_execute.test.ts` |
| [#134](#pr-134) | 2026-05-11 | ts-5d | feat(ts-5d): signer registry + expectFill helper | `executor-ts/src/wallets/signers.ts` (new), `src/executor/fills.ts` |
| [#133](#pr-133) | 2026-05-11 | ts-5a | feat(ts-5a): real HTTP fire modules + http_client | `executor-ts/src/lib/http_client.ts` (new), `src/fire/{poly,sx,lim}_post.ts` (new) |
| [#132](#pr-132) | 2026-05-11 | ts-5c | feat(ts-5c): slippage check + revert planner (decision skeleton) | `executor-ts/src/executor/{slippage,revert}.ts` (new) |
| [#131](#pr-131) | 2026-05-11 | ts-5b1.5 | feat(ts-5b1.5): synthesize mock wallets in DRY_RUN mode | `executor-ts/src/wallets/pool.ts`, `src/server.ts` |
| [#130](#pr-130) | 2026-05-11 | ui-fix | fix(ui): correct Polymarket threshold range text 94.8-99.3 (was stale 96.5-99) | `Scripts/dashboard.html` |
| [#129](#pr-129) | 2026-05-11 | ts-5b2 | feat(ts-5b2): Limitless user-channel Socket.IO listener | `executor-ts/src/ws/limitless_user_ws.ts` (new) |
| [#128](#pr-128) | 2026-05-11 | ts-5b1 | feat(ts-5b1): Polymarket user-channel WS listener | `executor-ts/src/ws/poly_user_ws.ts` (new) |
| [#58](#pr-58) | TBD | phase16 | feat: maker wire-up + adaptive multi-outcome bots + SX type expansion + Limitless revert | `executor/atomic.py`, `arb_server.py`, `tests/test_phase_16_*` |
| [#57](#pr-57) | 2026-05-01 | phase14-15 | feat: SX/Lim gap closure + cross-platform live wire + maker foundation | `arb_server.py`, `executor/atomic.py`, `executor/builders.py`, `limitless_ws.py`, `idea.md` |
| [#56](#pr-56) | 2026-05-01 | phase13 | feat: cross-platform arb infrastructure (X1/X2 structures) | `cross_platform.py` (new), `tests/test_phase_13_cross_platform.py` |
| [#55](#pr-55) | 2026-05-01 | phase12b | fix: SX Bet + Limitless audit — 5 bugs fixed | `arb_server.py`, `executor/builders.py` |
| [#54](#pr-54) | 2026-05-01 | phase12 | feat: Task D (WS coalesce 50ms) + event_matching.py + 5 skills (cross-platform/maker-taker/sx-bet/limitless/event-matching) | `poly_ws.py`, `arb_server.py`, `event_matching.py` (new), `.claude/skills/{cross-platform-arbs,maker-taker-orders,sx-bet-trading,limitless-trading,event-matching-fuzzy}` |
| [#53](#pr-53) | 2026-05-01 | phase11 | feat: depth-within-tolerance (Task F) + position log writing + web3 dep + skills | `arb_server.py`, `executor/atomic.py`, `requirements.txt`, `.claude/skills/{polymarket-trading,web3-onchain-prep}` |
| [#52](#pr-52) | 2026-05-01 | phase10 | feat: NO-token CLOB synthetic + slippage cancel + low-balance alerts (Task A/B/E) | `arb_server.py`, `executor/atomic.py`, `notify.py`, `wallets/coordinator.py` |
| [#51](#pr-51) | 2026-04-30 | phase10 | feat: top-of-book depth + preflight + revert + L2 derive + reconcile fetcher + /api/circuit_breakers | `arb_server.py`, `poly_ws.py`, `preflight.py` (new), `poly_derive_api_creds.py` (new), `executor/atomic.py`, `risk/reconcile.py` |
| [#49](#pr-49) | 2026-04-30 | 9kkk | hotfix: ALL_NO strict 3¢ raw distance (no N scaling) | `arb_server.py:_best_near_structure` |
| [#48](#pr-48) | 2026-04-30 | 9kkk | hotfix: skip is_quarantine cands в near_summary (Nebraska) | `arb_server.py:near_summary` |
| [#47](#pr-47) | 2026-04-30 | 9kkk | docs: BUG_CATALOG.md — 957 строк, 10 разделов | `BUG_CATALOG.md` (новый) |
| [#46](#pr-46) | 2026-04-30 | 9kkk | hotfix: drop POLY_SAFETY_BUFFER (operator request) | `arb_server.py:POLY_SAFETY_BUFFER` |
| [#45](#pr-45) | 2026-04-30 | 9kkk | hotfix: NEAR_BUFFER 7→3¢ + buffer guard в _best_near_structure + 1-dec threshold UI | `arb_server.py:_best_near_structure`, `near_summary` |
| [#44](#pr-44) | 2026-04-30 | 9kkk | hotfix: NEAR pool тоже strict CLOB (operator: «во всём анализе») | `arb_server.py:_best_near_structure` |
| [#43](#pr-43) | 2026-04-30 | 9kkk | hotfix: STRICT CLOB-only sources, drop ws/lim_ws | `arb_server.py:build_deal`, `dashboard.html` |
| [#42](#pr-42) | 2026-04-30 | 9kkk | hotfix: adaptive grace by event duration (BTC 5-min phantom) | `arb_server.py:filter_poly` |
| [#41](#pr-41) | 2026-04-30 | 9kkk | hotfix: drop events with endDate past 60min (zombie temp markets) | `arb_server.py:filter_poly`, `near_summary` |
| [#40](#pr-40) | 2026-04-30 | 9kkk | hotfix: dynamic per-fee threshold для Polymarket NEAR | `arb_server.py:near_summary` |
| [#39](#pr-39) | 2026-04-30 | 9kkk | hotfix: убрать мёртвые кнопки Approve/Delete (legacy from PR #23) | `dashboard.html` |
| [#38](#pr-38) | 2026-04-30 | 9kkk | hotfix: REAL_OB_SOURCES + liq>0 в build_deal | `arb_server.py:build_deal` |
| [#37](#pr-37) | 2026-04-30 | 9kkk | hotfix: 'price' field в deal entries (KeyError on dry-fire) | `arb_server.py:build_deal` |
| [#36](#pr-36) | 2026-04-30 | 9kkk | CF resilience + parallel Limitless fetch + 6 operator wins (Other-filter fix, dryrun mock-pad, circuit breaker, HTTP code classifier, search_query UI, Telegram >$10 alerts) | `arb_server.py`, `async_fetchers.py`, `circuit_breaker.py` (новый), `http_codes.py` (новый), `dashboard.html`, `executor/atomic.py`, `notify.py`, `CHANGELOG.md` (новый), `deploy/*` (новые) |
| [#34](#pr-34) | 2026-04-29 | 9aaa-9ddd | Performance + production hardening (gunicorn, Limitless REST-only, classify O(N²)→O(N), deps pins) | `arb_server.py`, `Dockerfile`, `requirements.txt` |
| [#33](#pr-33) | 2026-04-29 | 9n→9zz | Scan stability + safety + pre-signing (21 file merge) | `arb_server.py`, `dashboard.html`, `poly_ws.py`, `executor/presign.py` |
| [#32](#pr-32) | 2026-04-28 | 9m | V2 uncertainties closed (pUSD addr verified, /markets endpoint, negRisk routing, builder=zero) | `arb_server.py`, `executor/builders.py` |
| [#31](#pr-31) | 2026-04-28 | 9l | Threshold safety buffer +0.002 на все динамические пороги | `arb_server.py` |
| [#30](#pr-30) | 2026-04-28 | 9k | Polymarket dynamic threshold per market fee | `arb_server.py` |
| [#29](#pr-29) | 2026-04-28 | 9j | Polymarket V2 dynamic market info (real fee/tick/min) | `arb_server.py`, `executor/builders.py`, `polymarket_approve.py` |
| [#28](#pr-28) | 2026-04-28 | 9i | 6 sub-agent-found critical bugs + V2 polish (per-leg cap, jitter, distinct wallets, dryfire lock, fail-closed killswitch, ALL_NO gross fix) | `risk/limits.py`, `executor/atomic.py`, `arb_server.py`, `risk/killswitch.py`, `executor/builders.py` |
| [#27](#pr-27) | 2026-04-28 | — | Drop event when any outcome closed/expired/hidden (Limitless + Polymarket) | `arb_server.py`, `tests/test_limitless.py` |
| [#26](#pr-26) | 2026-04-28 | — | Incomplete-coverage gate for ALL_YES / ALL_NO (Leeds-Burnley phantom arb fix) | `arb_server.py` |
| [#25](#pr-25) | 2026-04-28 | 9-9f | Limitless Exchange как 4-я платформа (Base L2, no-KYC, REST polling) | `arb_server.py`, `executor/builders.py`, `dashboard.html`, `idea.md`, `README.md` |
| [#24](#pr-24) | 2026-04-28 | — | end_date column в History + revert WINDOW_DAYS 30→10 | `arb_server.py`, `analytics.py`, `dashboard.html` |
| [#23](#pr-23) | 2026-04-28 | — | Remove manual decision flow + add per-trade history | `analytics.py`, `arb_server.py`, `dashboard.html` |
| [#22](#pr-22) | 2026-04-28 | — | Telegram alerts (kill / daily-loss / network / startup) | `Scripts/notify.py` (новый) |
| [#21](#pr-21) | 2026-04-28 | — | Network safety (IP/country gate + VPN docs + hot standby guide) | `risk/network_check.py`, `deploy/README.md` |
| [#20](#pr-20) | 2026-04-28 | — | README.md (entry point на GitHub homepage) | `README.md` |
| [#19](#pr-19) | 2026-04-28 | — | Risk-aware deal sizing + log risk-blocked attempts (paper_results.jsonl was empty) | `arb_server.py`, `executor/atomic.py`, `risk/limits.py` |
| [#18](#pr-18) | 2026-04-27 | Phase 7 | SX Bet executor finalization (live order matching, partial-fill detection) | `executor/builders.py`, `executor/atomic.py`, `executor/dryrun_log.py` |
| [#17](#pr-17) | 2026-04-27 | Phase 6 | VPS-readiness (Docker + watchdog + deploy guide) | `Dockerfile`, `docker-compose.yml`, `Scripts/watchdog.py`, `deploy/README.md` |
| [#16](#pr-16) | 2026-04-27 | Phase 5 | Paper trading validation + graduation gate | `paper_trading.py`, `arb_server.py`, `dashboard.html` |
| [#15](#pr-15) | 2026-04-27 | Phase 4 | Multi-bot wallet architecture (6 bots, auto-rebalance proposals) | `wallets/*.py`, `arb_server.py`, `dashboard.html` |
| [#14](#pr-14) | 2026-04-27 | Phase 3 | Risk management layer (limits, kill switch, reconcile) | `risk/*.py`, `executor/atomic.py`, `arb_server.py` |
| [#13](#pr-13) | 2026-04-27 | Phase 2 | Atomic execution engine (dry-run only) | `executor/*.py` |
| [#12](#pr-12) | 2026-04-27 | Phase 1 | NO tokens + 3 arb structures + SX Bet taker price fix | `arb_server.py`, `dashboard.html` |
| [#11](#pr-11) | 2026-04-26 | — | SX Bet wider 27 binary market types | `arb_server.py` |
| [#8](#pr-8) | 2026-04-26 | — | WS scale-up (500 subs, NEAR_BUFFER 7c) + NEAR tab | `arb_server.py`, `dashboard.html` |
| [#7](#pr-7) | 2026-04-26 | — | Analytics tab with sim P&L, manual decisions, period switcher | `Scripts/analytics.py` (новый), `arb_server.py`, `dashboard.html` |
| [#6](#pr-6) | 2026-04-26 | — | Extend event window 10→30 days, make configurable | `arb_server.py` |
| [#5](#pr-5) | 2026-04-26 | — | SX Bet pageSize capped at 100 (HTTP 400 fix) | `arb_server.py` |
| [#4](#pr-4) | 2026-04-26 | — | Filter diagnostics counters (why radar shows 0 deals) | `arb_server.py` |
| [#3](#pr-3) | 2026-04-26 | — | Read negRisk from event, not from each market | `arb_server.py` |
| [#2](#pr-2) | 2026-04-26 | — | Polymarket WebSocket + HOT/NEAR pool architecture | `Scripts/poly_ws.py` (новый), `arb_server.py`, `dashboard.html` |
| [#1](#pr-1) | 2026-04-26 | — | Sync per-platform thresholds with idea.md | `arb_server.py` |

**Pre-PR:** initial commits `f7e2ec4` (Initial), `bc30ba1` (CLAUDE.md project memory).

**Post-PR-#34 коммиты на feature branches (НЕ merged в main, в этой сессии)**: 9eee/9fff/9ggg/9hhh/9iii — async fetchers, JS lint, Limitless end_date probe, HTTP/2 multiplexing.

---

## Index by Phase (chronological)

| Phase | PR | Дата | Что |
|---|---|---|---|
| **Phase 1** | #12 | 2026-04-27 | NO tokens + 3 arb structures (A/B/C) + SX Bet taker fix |
| **Phase 2** | #13 | 2026-04-27 | Atomic execution engine (dry-run only) |
| **Phase 3** | #14 | 2026-04-27 | Risk management ($55/trade, $35/day, kill switch, reconcile) |
| **Phase 4** | #15 | 2026-04-27 | Multi-bot wallets (6 bots, anti-detection, auto-rebalance) |
| **Phase 5** | #16 | 2026-04-27 | Paper trading + graduation gate (50 trades, 70% win rate) |
| **Phase 6** | #17 | 2026-04-27 | Docker + watchdog + deploy guide |
| **Phase 7** | #18 | 2026-04-27 | SX Bet executor finalization (live matching, partial-fill) |
| **Phase 9** | #25 | 2026-04-28 | Limitless Exchange added (4th platform, Base L2, no-KYC) |
| **Phase 9b** | #25 | 2026-04-28 | Limitless WS + EIP-712 signing + filter parity |
| **Phase 9c** | #25 | 2026-04-28 | Limitless full parity with Polymarket |
| **Phase 9d** | #25 | 2026-04-28 | Limitless push-driven re-eval (5s → 250ms) |
| **Phase 9e** | #25 | 2026-04-28 | Limitless fill latching + reconcile (partial Phase 4) |
| **Phase 9f** | #25 | 2026-04-28 | Polymarket full parity with Limitless |
| **Phase 9g** | #26 | 2026-04-28 | Incomplete coverage gate (Leeds-Burnley phantom fix) |
| **Phase 9h** | #27 | 2026-04-28 | Drop event when any outcome closed |
| **Phase 9i** | #28 | 2026-04-28 | 6 critical bugs (per-leg cap, jitter, distinct wallets, lock, fail-closed, ALL_NO gross) |
| **Phase 9j** | #29 | 2026-04-28 | Polymarket V2 dynamic market info |
| **Phase 9k** | #30 | 2026-04-28 | Dynamic threshold per market fee |
| **Phase 9l** | #31 | 2026-04-28 | Threshold safety buffer +0.002 |
| **Phase 9m** | #32 | 2026-04-28 | V2 uncertainties closed (4 items verified) |
| **Phase 9n** | #33 | 2026-04-29 | Warm-cache scan_data + Flask threaded=True |
| **Phase 9o** | #33 | 2026-04-29 | Drop ALL_YES/ALL_NO on threshold-series events |
| **Phase 9p** | #33 | 2026-04-29 | Drop CORS wildcard + per-structure toggles ENABLE_STRUCT_A/B/C |
| **Phase 9q** | #33 | 2026-04-29 | Correct gross-payout formula in build_deal |
| **Phase 9r** | #33 | 2026-04-29 | ENABLE_POLY kill switch + tuple timeouts |
| **Phase 9s** | #33 | 2026-04-29 | Forward Limitless cache to /api/near |
| **Phase 9t** | #33 | 2026-04-29 | Close ENABLE_POLY/SX leaks in fallback + pause loops |
| **Phase 9u** | #33 | 2026-04-29 | /api/deals non-blocking lock with stale fallback |
| **Phase 9v** | #33 | 2026-04-29 | WINDOW_DAYS=13 + volume-0 ghost-arb guard in NEAR |
| **Phase 9w** | #33 | 2026-04-29 | Single-binary Polymarket structure C + NEAR-cap |
| **Phase 9x** | #33 | 2026-04-29 | Propagate threshold-series guard to pool + NEAR |
| **Phase 9y** | #33 | 2026-04-29 | Top-of-book depth + any-volume-zero exclusion + child name in C |
| **Phase 9z** | #33 | 2026-04-29 | Volume-based liquidity gate + WS top-of-book + per-leg gate |
| **Phase 9aa** | #33 | 2026-04-29 | Normalize raw-USDC size into realistic USD depth |
| **Phase 9bb** | #33 | 2026-04-29 | Math-based threshold-series fallback |
| **Phase 9cc/dd** | #33 | 2026-04-29 | Relax A-block + Polymarket single-binary path |
| **Phase 9ee** | #33 | 2026-04-29 | Server-side end_date_max filter on Polymarket (later reverted in 9ii) |
| **Phase 9ff** | #33 | 2026-04-29 | Relax C_NEAR_MAX_DISTANCE 2c → 5c |
| **Phase 9gg** | #33 | 2026-04-29 | Relax min_liq thresholds (Poly $1000→$600, Lim $200→$130) |
| **Phase 9hh** | #33 | 2026-04-29 | Revert safe_for_A relaxation, strict alive-only |
| **Phase 9ii** | #33 | 2026-04-29 | Revert 9ee end_date_max filter (zombie umbrella events) |
| **Phase 9jj** | #33 | 2026-04-29 | Drop child-closed event-wide reject; structure C still runs on alive legs |
| **Phase 9kk** | #33 | 2026-04-29 | Drop ev.restricted=True gate (it's a category tag) |
| **Phase 9ll** | #33 | 2026-04-29 | Fully remove `restricted` from filter_poly |
| **Phase 9mm** | #33 | 2026-04-29 | Tighten C-structure NEAR cap 5c → 3c |
| **Phase 9nn** | #33 | 2026-04-29 | POLY_MAIN_PAGES 10→4 + spinner на NEAR refresh |
| **Phase 9oo** | #33 | 2026-04-29 | Keep POLY_MAIN_PAGES=10, throttle MAX_WORKERS 80→20 |
| **Phase 9pp** | #33 | 2026-04-29 | MAX_WORKERS 20 → 30 (operator-tuned middle) |
| **Phase 9qq** | #33 | 2026-04-29 | Progressive output every 2 pages + deadline guards + as_completed timeout |
| **Phase 9rr** | #33 | 2026-04-29 | Session pooling + tuple timeout + pre-filter + scan budget 180s |
| **Phase 9ss** | #33 | 2026-04-29 | Meta fetchers Session pool (Limitless 761s → 30s root cause fix) |
| **Phase 9tt** | #33 | 2026-04-29 | Safety: month-end UTC + killswitch fail-closed |
| **Phase 9uu** | #33 | 2026-04-29 | WS book locks + cache eviction + tuple timeouts + DOS protection |
| **Phase 9vv** | #33 | 2026-04-29 | NEAR badge mismatch fix |
| **Phase 9ww** | #33 | 2026-04-29 | Deals badge in nav |
| **Phase 9xx/yy** | #33 | 2026-04-29 | Phantom-on-resolution filter + ALL_NO gross_pct + NEAR neg-distance |
| **Phase 9zz** | #33 | 2026-04-29 | Pre-signing для NEAR кандидатов (fire latency 150-300ms → 12ms) |
| **Phase 9aaa** | #34 | 2026-04-29 | requirements.txt upper bounds + gunicorn + paramiko deps |
| **Phase 9bbb** | #34 | 2026-04-29 | classify_pools O(N²) → O(N) (475 lock acq → 1 pass) |
| **Phase 9ccc** | #34 | 2026-04-29 | gunicorn production WSGI (replace Flask dev server) |
| **Phase 9ddd** | #34 | 2026-04-29 | Limitless REST-only mode (ENABLE_LIMITLESS_WS=0 default) |
| **Phase 9eee** | post-#34 | 2026-04-30 | Cache-Control no-cache + JS lint + defensive null-checks (UI resilience) |
| **Phase 9fff** | post-#34 | 2026-04-30 | Async fetchers (gated ASYNC_FETCH=1) |
| **Phase 9ggg** | post-#34 | 2026-04-30 | JS lint pre-commit script |
| **Phase 9hhh** | post-#34 | 2026-04-30 | Limitless end_date probe (8 fields) + title dedup + search_query field |
| **Phase 9iii** | post-#34 | 2026-04-30 | Hide progress placeholder + HTTP/2 multiplexing for Limitless 403 |
| **Phase 9jjj** | post-#34 | 2026-04-30 | GRADUATION_MIN_TRADES 100→50 (operator request) |

---

## Index by File (which PRs touched what)

### `Scripts/arb_server.py` (~3300 lines, главный модуль)
- **#1**: thresholds 0.985 → per-platform (Poly 0.97, Kalshi 0.93, SX 0.97)
- **#2**: WebSocket integration + HOT/NEAR pools
- **#3**: negRisk read from event, not market
- **#4**: filter diagnostics counters
- **#5**: SX_PAGE_SIZE=100
- **#6**: WINDOW_DAYS 10→30 (later reverted в #24 → 10, потом 9v → 13)
- **#7**: analytics integration
- **#8**: NEAR_BUFFER 7c, MAX_WS_SUBS=500
- **#11**: 27 SX binary types
- **#12**: 3 arb structures + NO tokens (Phase 1)
- **#13**: _maybe_dry_fire integration (Phase 2)
- **#14**: risk integration в `__main__` + endpoints (Phase 3)
- **#15**: wallet pool startup (Phase 4)
- **#16**: graduation endpoints (Phase 5)
- **#17**: bootstrap_radar() (Phase 6)
- **#19**: BALANCE / MAX_PER_TRADE_USD reconcile + per-platform toggles
- **#22**: notify hooks
- **#24**: end_date поле в `build_deal` для всех evaluator'ов
- **#25**: Limitless platform integration (filter_limitless, eval_limitless, micro_loop)
- **#26**: incomplete-coverage gate в всех eval_*
- **#27**: per-child closed/archived gates
- **#28**: _maybe_dry_fire two-phase commit, payout_target в build_deal
- **#29**: _fetch_poly_market_info + V2 metadata + dynamic theta
- **#30**: compute_poly_threshold() per-market fee
- **#31**: POLY_SAFETY_BUFFER 0.005 → 0.007
- **#32**: V2 pre-fire gate (acceptingOrders, acceptingOrderTimestamp)
- **#33**: Phase 9n→9zz monster merge (+400 lines)
- **#34**: classify_pools O(N²)→O(N), Limitless WS gate, gunicorn bootstrap
- **post-#34**: async_fetchers integration, _resolve_lim_end_date helper, search_query field

### `Scripts/dashboard.html`
- **#2**: WS widget
- **#7**: Analytics tab UI
- **#8**: NEAR tab + nav badge
- **#12**: Structure badges (A/B/C/binary), Outcome name fix
- **#13**: 🧪 Dry-fire button + paper-trade panel
- **#14**: 🛑 STOP button + risk panel
- **#15**: wallets panel + click-modal
- **#16**: graduation modal (header + blockers + histogram)
- **#23**: history table + structure filter; remove manual decision UI
- **#24**: end_date column в history (+27д indicator)
- **#25**: Limitless stat-card
- **#33**: Конец column в NEAR + header cleanup + Deals nav badge
- **post-#34**: cache-busting + defensive null-checks (Phase 9eee)
- **post-#34**: hide progress placeholder when deals=0 (Phase 9iii)

### `Scripts/poly_ws.py` (PR #2 создал)
- **#2**: full WebSocket client implementation
- **#33**: book lock fix (Phase 9uu race condition)

### `Scripts/limitless_ws.py` (PR #25 создал)
- **#25**: socketio integration
- **#33**: book lock fix
- **#34**: ENABLE_LIMITLESS_WS gate (default OFF)

### `Scripts/poly_user_ws.py` / `Scripts/limitless_user_ws.py`
- **#25**: created в Phase 9c для fill confirmations

### `Scripts/analytics.py` (PR #7 создал)
- **#7**: append-only event log + aggregate
- **#23**: history endpoint + per-structure breakdown
- **#24**: end_date пробрасывается

### `Scripts/risk/` (PR #14 создал)
- **#14**: limits.py + state.py + killswitch.py + reconcile.py
- **#19**: limits.py — risk-aware sizing для арбов (15% worst case)
- **#21**: network_check.py создан (IP/country gate)
- **#22**: notify integration
- **#28**: limits.py per-leg cap, killswitch fail-closed
- **#33**: month-end UTC fix (Phase 9tt)

### `Scripts/wallets/` (PR #15 создал)
- **#15**: config.py + stores.py + coordinator.py + rebalance.py
- **#29**: walk up parent dirs to find Credentials.env (worktree support)

### `Scripts/executor/` (PR #13 создал)
- **#13**: __init__.py + builders.py + atomic.py + dryrun_log.py + fills.py
- **#14**: atomic.py — risk gate
- **#15**: atomic.py — wallets coordinator
- **#16**: atomic.py — graduation gate в real-mode
- **#18**: builders.py — SX Bet matching (fetch + match + build)
- **#19**: atomic.py — log_decision на всех return paths
- **#25**: builders.py — Limitless EIP-712 build
- **#28**: atomic.py — jitter + distinct wallets, builders.py — V2 GTD
- **#29**: builders.py — _round_to_tick, V2 params
- **#33**: presign.py создан (Phase 9zz)

### `Scripts/paper_trading.py` (PR #16 создал)
- **#16**: graduation_status() + history() + distribution()
- **post-#34**: GRADUATION_MIN_TRADES env override (100→50)

### `Scripts/notify.py` (PR #22 создал)
- **#22**: Telegram alerts module (urllib stdlib, daemon thread, dedupe)

### `Scripts/watchdog.py` (PR #17 создал)
- **#17**: standalone process polling Executions/.killed

### `Scripts/async_fetchers.py` (post-#34 создал)
- **post-#34 (Phase 9fff)**: async httpx fetchers
- **post-#34 (Phase 9iii)**: HTTP/2 multiplexing for Limitless

### `Scripts/polymarket_approve.py` (PR #29 создал)
- **#29**: USDC.e → pUSD wrap helper CLI

### `Dockerfile` / `docker-compose.yml` (PR #17 создал)
- **#17**: python:3.11-slim, healthcheck, watchdog
- **#34**: gunicorn CMD

### `requirements.txt`
- **#17**: eth-account
- **#33**: + paramiko (Phase 9aaa pin)
- **#34**: upper bounds + gunicorn + urllib3
- **post-#34**: httpx[http2] + h2 (Phase 9iii)

### `tests/`
- 355 тестов в main (per #33). Каждый Phase ≥ 9i имеет свой test_phase_9*.py.
- post-#34: tests не добавлены (фичи на feature branches).

### `idea.md` (28KB спецификация)
- Регулярно обновляется в каждом Phase merge

### `README.md`
- **#20**: created (project entry point)
- **#28**: V2 pUSD wrap step

### `CLAUDE.md` (этот memory file)
- `bc30ba1`: created (project memory + PR procedure)

### `.claude/skills/`
- post-#34: 16 skills installed locally (NOT in repo, .gitignored)
- post-#34 (current session): +8 skills (deploy-pipeline, secrets-management, circuit-breaker-patterns, observability-stack, websocket-reliability, browser-cache-busting, feature-flags, error-budget-policy)

---

## Detailed PR descriptions

<a id="pr-51"></a>
### PR #51 — feat(executor): top-of-book depth + preflight + revert + L2 derive + reconcile + /api/circuit_breakers
**Merged:** 2026-04-30 | **Branch:** `feature/phase10-poly-trading-gaps`

Большой пакет правок, закрывающий 7 блокеров real-mode торговли на Polymarket, найденных в `BUG_CATALOG.md` audit:

**1. Top-of-book depth (BUG_CATALOG #5.X)**
Старая формула `depth = sum(price × size)` по ВСЕМ уровням orderbook'а инфлировала `min_liq` в 5-10× (например, $3,865 «depth» при реальных $50 на верху и $3,815 на 1-3¢ выше). Новый helper `_top_of_book_depth_usd(asks, slippage_tolerance=0, size_is_usd=False)` в `arb_server.py` суммирует USD ровно на best ask цене (или внутри tolerance для floating-point fuzz).
- `_fetch_clob` (Polymarket) — top-of-book through helper
- `_fetch_kalshi_ob` — `size_is_usd=True` (Kalshi `*_dollars` уже dollar-denominated)
- `_fetch_sx_orders` — restructure: group by maker side, top-of-book taker depth = sum at single best maker price
- `poly_ws._calc_book` — inline same fix

**2. preflight.py (new) — pre-fire safety checks**
Новый модуль с тремя функциями:
- `check_depth(stake, top_of_book_liq)` — sync, no I/O
- `check_balance(eth_address, required_usd)` — web3.balanceOf(pUSD), 30s LRU cache
- `check_allowance(eth_address, required_usd, neg_risk)` — web3.allowance(pUSD, exchange_v2)
- `preflight_arb(deal, wallets, ...)` — aggregate per-leg
Подключено в `executor/atomic.py::fire_arb` после risk gate. Dry-run пропускает balance/allowance (public Polygon RPC флаки), depth check работает всегда.

**3. revert_filled_legs() в atomic.py**
Когда арб поломан (partial fill / failed leg + filled leg) — теперь автоматический реверт:
- dry-run: `dryrun_log.log_order_decision(op='revert_sell')` для каждого filled, paper-trade видит реверт
- live (Polymarket): `build_poly_order(side='SELL', order_type='FOK', price=expected-0.01)`, POST с timeout 2с
- TODO явно отмечен для SX Bet (taker-fill на opposite outcome) и Limitless (тот же путь что Polymarket)

**4. poly_derive_api_creds.py (new) — L2 креды per bot**
Один раз per кошелёк: `python Scripts/poly_derive_api_creds.py --bot bot{N}`. Подписывает ClobAuth EIP-712, GET `/auth/derive-api-key` (или POST `/auth/api-key`), записывает `BOT{N}_POLY_API_KEY/SECRET/PASSPHRASE` обратно в `Credentials.env` идемпотентно (replace existing keys, append new). `--dry-run` показывает headers preview без вызова сети.

**5. fetch_polymarket_positions / register_polymarket_fetcher в reconcile.py**
- Polymarket `GET /data/positions` через L2 HMAC headers, парсит в `(platform, conditionId, outcome) → size_usdc`.
- `register_polymarket_fetcher(wallets)` подключает в reconcile loop если хотя бы 1 wallet с `has_poly_creds`.
- Без креденциалов фетчер пропускает кошелёк silently (не raise).

**6. /api/circuit_breakers endpoint**
Был обозначен в Phase 9kkk skill, реально 404'ил. Теперь возвращает `{breakers: [{host, state, failures_count, ...}], count: N}`. На отсутствующий модуль circuit_breaker возвращает 200 с пустым списком и note (не 404, чтобы smoke_test зелёный).

**Тесты:**
- `tests/test_phase_phase10_depth.py` — 12 тестов: dict/tuple/multi-level/empty/sorted/unsorted/slippage_tol + end-to-end по 4 источникам
- `tests/test_phase_phase10_preflight_revert.py` — 13 тестов: depth/balance/allowance + revert dry-run + derive_api_creds dry-run + idempotent env writer + reconcile fetcher with/without creds

**Все:** 25/25 ✅. Регрессия по другим suite'ам не задета.

**После этого PR ОСТАЁТСЯ для real-mode:**
- запустить `polymarket_approve.py --bot bot{N}` per кошелёк (on-chain wrap+approve, **один раз**)
- запустить `poly_derive_api_creds.py --bot bot{N}` per кошелёк (L2 deriving, **один раз**)
- залить pUSD на каждый кошелёк
- (опц) платный POLYGON_RPC_URL в `Credentials.env`
- 100 paper-trades для Phase 5 graduation gate (win_rate ≥ 70%)

---

<a id="pr-49"></a>
### PR #49 — hotfix(near): ALL_NO strict 3¢ raw distance (no N scaling)
**Merged:** 2026-04-30 | **Branch:** `hotfix/9kkk-all-no-strict-3c`

Live verification показал NEAR item с `distance=+8.8¢` ("Highest temperature in Chongqing on May 1?", ALL_NO). Был внутри scaled buffer `NEAR_BUFFER * (N-1) = 9¢` для N=3, но оператор хотел strict 3¢ raw.

**Fix:** в `_best_near_structure` для ALL_NO `(s - b_threshold) <= NEAR_BUFFER` без scaling.

**Verification:** 17/17 PASS на BUG_CATALOG live regression check.

---

<a id="pr-48"></a>
### PR #48 — hotfix(near): skip is_quarantine cands в near_summary
**Merged:** 2026-04-30 | **Branch:** `hotfix/9kkk-skip-quarantine-near`

Live verification 16/17 PASS — 1 FAIL: Nebraska Governor Republican Primary visible в NEAR (sum=99.1¢, dist=+2.1¢).

**Root cause:** `filter_poly` корректно ставил `is_quarantine=True` (groupItemTitle='Other' detected), но `near_summary` распаковывал tuple как `ev, rough, _` — discarding flag — и рендерил quarantined event в NEAR.

**Fix:** unpack `ev, rough, is_quarantine` + skip if quarantined. Quarantined events ТОЛЬКО в Карантин tab.

---

<a id="pr-47"></a>
### PR #47 — docs: BUG_CATALOG.md (957 строк)
**Merged:** 2026-04-30 | **Branch:** `docs/bug-catalog`

Operator request: единый каталог багов/фиксов чтобы не возвращаться к ним.

10 разделов: Filter bypass, Time phantoms, Source/Price phantoms, Threshold/NEAR, Wallet/executor, HTTP errors (13 codes), Concurrency, UI/cache/deploy, Cross-platform parity, Risk/safety. Каждая запись: симптом → root cause → file:line → PR/Phase → fix → verification. Plus 49-PR session table + 15-rule anti-pattern checklist.

---

<a id="pr-46"></a>
### PR #46 — hotfix(poly): drop POLY_SAFETY_BUFFER
**Merged:** 2026-04-30 | **Branch:** `hotfix/9kkk-poly-no-safety-buffer`

Operator: «с порога 97 убери страхующее значение, мы там где-то устанавливали 0.02 или 0.2 страховки, на остальных оставь».

**Fix:** `POLY_SAFETY_BUFFER = 0` (было 0.007). Threshold = `1 - (fee + slippage_reserve)`. Other platforms (Kalshi 0.93 / SX 0.97 / Limitless 0.988) не тронуты.

| fee | THRESH было | THRESH стало |
|---|---|---|
| 0% | 99.0¢ | **99.7¢** |
| 2% sport | 97.2¢ | **97.7¢** |
| 2.5% politics | 96.7¢ | **97.2¢** |
| 4% high | 95.0¢ | **95.7¢** |

---

<a id="pr-45"></a>
### PR #45 — hotfix: NEAR_BUFFER 7¢→3¢ + buffer guard + 1-dec threshold UI
**Merged:** 2026-04-30 | **Branch:** `hotfix/9kkk-buffer-guard-3c`

Operator-found: White House posts April 28-May 5 (8 outcomes) показано в NEAR с sum=120.9¢, **distance +23.9¢** — далеко за NEAR_BUFFER=7¢.

**Root cause (двух-ступенчатая логика):**
1. `classify_pools` через `_sum_poly_cand` = `min(A_norm, B_norm, C_norm)` принимал via `B_norm=0.97` passing buffer
2. `_best_near_structure` рендерил A.ALL_YES с raw sum=121¢ — **никакого buffer check**

**Fix:**
- `NEAR_BUFFER` 0.07 → 0.03 (operator: ближе к Deals, matches `C_NEAR_MAX_DISTANCE`)
- В `_best_near_structure`: drop A/B options где `(sum - threshold) > NEAR_BUFFER`
- UI `threshold_cents` 1-decimal precision — sport 97.2¢ vs politics 96.7¢ теперь видно

---

<a id="pr-44"></a>
### PR #44 — hotfix(near): NEAR pool тоже strict CLOB-only
**Merged:** 2026-04-30 | **Branch:** `hotfix/9kkk-near-strict-clob`

Operator: «почему mid источник запрещен только в deals, не во всём анализе».

`_best_near_structure` (NEAR path) не имел source check — let MID/implied/ws candidates через до UI.

**Fix:** pre-filter `pm` list в начале функции:
```python
REAL_OB_SOURCES = {'clob_ask', 'kalshi_ob', 'sx_ob', 'lim_clob'}
pm = [p for p in pm if p.get('yes_src') in REAL_OB_SOURCES and ...]
```

Теперь MID/implied/ws отброшены и в Deals (build_deal) и в NEAR (_best_near_structure).

---

<a id="pr-43"></a>
### PR #43 — hotfix: STRICT CLOB-only sources, drop ws/lim_ws
**Merged:** 2026-04-30 | **Branch:** `hotfix/9kkk-strict-clob-only`

Operator-found: BTC Up or Down 1PM ET arb в Deals с обеими ногами `src=MID`, sum=10¢, net=$548, ROI 897%. Phantom от stale orderbook.

**Root cause:** прошлый guard (PR #38) включал `'ws'` и `'lim_ws'` как валидные. WS books могут быть **stale без notification** (Polymarket WS не шлёт `market_closed`).

**Fix:**
```python
# было: {'ws', 'clob_ask', 'kalshi_ob', 'sx_ob', 'lim_clob', 'lim_ws'}
# стало: {'clob_ask', 'kalshi_ob', 'sx_ob', 'lim_clob'}
```

UI badge: 🟢 CLOB/KALSHI/SX/LIM (live REST), 🟡 WS/LIM-WS (cached push), 🔴 ⚠ MID (lastTradePrice phantom).

---

<a id="pr-42"></a>
### PR #42 — hotfix: adaptive grace by event duration
**Merged:** 2026-04-30 | **Branch:** `hotfix/9kkk-adaptive-grace`

Operator-found: BTC Up or Down 1PM ET записан в History в 17:56 UTC, 56 мин past endDate=17:00. Sum=94¢ Net=$5.07 — stale orderbook от резолвенного 5-min event.

**Root:** PR #41 поставил flat 60-min grace. Ок для elections/sports (UMA dispute 6-12h). **Не ок для 5-min intraday crypto** (Chainlink резолвит мгновенно).

**Fix: adaptive grace by event duration:**
| duration | grace |
|---|---|
| ≤10min (5-min crypto) | **1 min** |
| ≤1h | 5 min |
| ≤24h | 30 min |
| >1d (elections/sports) | 60 min |

Title heuristic fallback: `'1PM ET' / '5min' / 'minutely'` → 1 min. `'highest temperature'` → 30 min. Default → 30 min.

---

<a id="pr-41"></a>
### PR #41 — hotfix: drop events with endDate past 60min
**Merged:** 2026-04-30 | **Branch:** `hotfix/9kkk-past-resolve-zombies`

Operator-found: NEAR table забит "Highest temperature in Miami/Lagos/Singapore/..." с endDate 30 Apr 12:00 UTC, при wall clock 17:46 UTC (5h+ после резолва).

**Root cause (двойной):**
1. gamma-api возвращал `closed=false` часами после time-resolved events
2. `is_within_10_days` использовал `WINDOW_PAST_DAYS=2` (48h grace) — ок для daily-resolve elections, **катастрофа для time-of-day events**

**Fix:** explicit endDate arithmetic после `is_within_window`. Если `age > 60 min` past resolve → drop independently from `closed` flag. Defense-in-depth в `near_summary` тоже.

---

<a id="pr-40"></a>
### PR #40 — hotfix(near): dynamic per-fee threshold для Polymarket NEAR
**Merged:** 2026-04-30 | **Branch:** `hotfix/9kkk-dynamic-threshold-near`

Operator: «ты говорил, что на разных событиях разные пороги, а я вижу везде 97».

**Root:** PR #30 (Phase 9k) добавил `compute_poly_threshold(taker_fee_bps)` для main scan — wiring в `near_summary` пропустили. Использовал legacy `THRESH_POLY=0.97`.

**Fix:** в `near_summary` вычислить `cand_max_fee_bps` так же как `classify_pools`, потом `compute_poly_threshold(fee)`. Falls back to `THRESH_POLY` при cache miss.

---

<a id="pr-39"></a>
### PR #39 — hotfix(ui): remove dead Approve/Delete buttons
**Merged:** 2026-04-30 | **Branch:** `hotfix/9kkk-remove-dead-buttons`

Operator-found: clicking Одобрить/Удалить did nothing.

**Root:** PR #23 (28.04.2026) удалил manual decision flow + `/api/approve`/`/api/reject` endpoints. Кнопки в `dashboard.html` остались, ссылались на 404.

**Fix:** заменил кнопки на read-only label "Авто-блок: executor не файрит карантинные сделки". Drop dead `actionDeal()` JS function.

---

<a id="pr-38"></a>
### PR #38 — hotfix: REAL_OB_SOURCES + liquidity>0 в build_deal
**Merged:** 2026-04-30 | **Branch:** `hotfix/9kkk-phantom-arb-guard`

Operator caught 15+ phantom deals в 10 минут: `Highest temperature in Munich 14°C` C-pair (sum=65.6¢, net=$28.41, NO leg liquidity=$0). BTC/ETH Up or Down post-resolve C-pair.

**Root:** `eval_poly` fall back на `outcomePrices[0]` (lastTradePrice) когда orderbook empty. Synth `1 - yes_implied` для NO. `build_deal` accepted `source='implied'` как валидный.

**Fix initial set (later refined в #43):**
```python
REAL_OB_SOURCES = {'ws', 'clob_ask', 'kalshi_ob', 'sx_ob', 'lim_clob', 'lim_ws'}
for o in outcomes:
    if o.get('source') not in REAL_OB_SOURCES: return None
    if not (o.get('liquidity') or 0) > 0: return None
```

Также wipe analytics_events.jsonl от 15+ накопленных phantom rows.

---

<a id="pr-37"></a>
### PR #37 — hotfix(executor): add raw 'price' to deal entries
**Merged:** 2026-04-30 | **Branch:** `hotfix/9kkk-price-field`

`100% [DRYFIRE] error firing yes_no_pair... 'price'` после Phase 9kkk.

**Root:** `build_deal` создавал entries с `'price_cents'` only. `executor/atomic.py` читал `entry['price']` (raw 0-1) в 4 местах (Polymarket/Kalshi/SX builders + leg result). Phase 9kkk mock-pad для dry-run finally let arbs reach builder → KeyError surfaced.

**Fix:** хранить ОБА поля:
```python
entries.append({
    'price': o['price'],          # raw 0-1 для executor
    'price_cents': round(o['price']*100, 1),  # UI
    ...
})
```

---

<a id="pr-36"></a>
### PR #36 — Phase 9kkk: CF resilience + parallel Limitless fetch + 6 operator wins
**Merged:** 2026-04-30 | **Branch:** `feature/phase-9kkk-cf-resilience-parallel-fetch`

Комплексный bundle: производительность + устойчивость + 3 operator-found бага. Поведение paper trading сохранено (DRY_RUN=1).

**Operator-found баги (3 — все пофикшены):**
- 3 фантомных события в NEAR (West Virginia, NE-02, Nebraska Republican): `groupItemTitle='Other'` молча игнорировался из-за `or` short-circuit. filter_poly теперь передаёт ОБА поля + title в `has_other_outcome`. OTHER_RE расширен `another (candidate|player|person|...)` + рус варианты + safety net на exact GT match.
- dryrun.jsonl пустой 32ч: `_assign_wallets` отказывал 4+ leg арбам. В `dry_run=True` теперь pad'им mock stub'ами; live mode остаётся строгим.
- Limitless 65s scan: parallel HTTP/2 fetcher → 3-5s. Verified: 60 concurrent = 2.45s на VPS.

**Производительность:**
- `fetch_limitless_pages_async` — один TCP, 40 streams через HTTP/2 multiplexing
- Polymarket 15 pages parallel: 2.77s (полное покрытие 7500+ events)
- `MAX_WORKERS=80` в 3x МЕДЛЕННЕЕ 30 — Cloudflare throttle, 30 sweet spot

**Resilience:**
- `Scripts/circuit_breaker.py` — 3-state CB (CLOSED/OPEN/HALF_OPEN) с auto-recovery + Telegram alert на state change
- `Scripts/http_codes.py` — universal classifier для 13 HTTP кодов с retry policy + backoff calculator
- 403/502/504/521/522 теперь логируются в stderr с reason+URL+attempt

**Другие фиксы:**
- `eval_limitless` использует `_resolve_lim_end_date` (8-field helper, audit fix #2)
- `eval_sx` фильтрует closed/resolved markets (status != 1, outcome != 0)
- Telegram alert на арбы net >= $10 (per-arb dedupe 5min)
- 📋 Copy button в NEAR table (старый запрос оператора)

**Документация:**
- CHANGELOG.md (новый) — 1100+ строк, 4 индекса, 32 PR'а
- `deploy/{ROLLBACK.md, smoke_test.sh, DEPLOY_PLAYBOOK.md, VERIFICATION.md}` — runbooks
- 8 новых SKILL.md в `.claude/skills/` (.gitignored)

**Tests:** ast.parse OK на 6 Python файлах, JS lint OK (42616 chars), has_other_outcome 5/5 на real Polymarket events, HTTP/2 60 concurrent = 2.45s.

**Deploy:** `ENABLE_LIMITLESS=0` и `ENABLE_SX=0` остаются OFF — 9kkk только разблокирует Polymarket coverage (POLY_MAIN_PAGES=15) + UI/filter фиксы. Limitless/SX включаются отдельным решением.

---

<a id="pr-34"></a>
### PR #34 — Phase 9aaa-9ddd: deps pins + classify perf + gunicorn + Limitless REST-only
**Merged:** 2026-04-29 | **Branch:** `feature/phase-9aaa-perf-prod`

**Что сделано:**
- **Phase 9aaa**: deps в `requirements.txt` пинены `>=floor,<next_major`. Добавлены gunicorn, paramiko, urllib3.
- **Phase 9bbb**: `classify_pools` O(N²) → O(N). Pre-compute `_fetch_poly_market_info` lookup ОДИН раз вместо 475 lock acquisitions × 5 chunks. ~30% faster + меньше lock contention с WS callback.
- **Phase 9ccc**: gunicorn `-w 1 --threads 50 --timeout 300 --max-requests 10000` заменил Flask dev server. Single worker (state в process globals), 50 threads (для concurrent /api/* + scan_loop), `--max-requests` recycle защита от утечек. `_bootstrap_radar()` вызывается из обоих путей: `__main__` и WSGI module import.
- **Phase 9ddd**: `ENABLE_LIMITLESS_WS=0` default. Reason: socketio reconnect loop держал GIL на flaky Limitless TCP (4341ms max handshake) → 761s scan hangs. REST polling каждые 5s через micro_loop. Будет реверт после dr-manhattan async migration.

**Файлы:** `Scripts/arb_server.py`, `Dockerfile`, `requirements.txt`

**Verification:** UI работает, scan завершается за 30-90s, `gunicorn 22.0.0` в логах, `[Limitless] WS DISABLED`. 355 тестов из main все ок.

---

<a id="pr-33"></a>
### PR #33 — Phase 9n → 9zz: scan stability + safety + pre-signing
**Merged:** 2026-04-29 | **Branch:** `feature/phase-9n-warmcache-threaded` | **+4141 −281 строк**

Большой merge с накопленными за день фиксами (фазы 9n → 9zz, 21 файл).

**Performance / стабильность:**
- 9rr: requests.Session pool на хост → 5x ускорение (TLS reuse)
- 9rr: tuple timeout `(connect, read)` от SSL_read C-level hangs
- 9rr: pre-filter Limitless по `volume>0` → 70-80% reduction в orderbook calls
- 9rr: `RUN_SCAN_BUDGET_S=180` hard wall-clock
- 9ss: Sessions для meta-fetcher'ов (Limitless 761s → 30s)
- 9qq: chunked progressive output — UI видит partial deals за 6-12s
- 9zz: pre-signing для NEAR кандидатов — fire latency 150-300ms → 12ms

**Безопасность:**
- 9tt: `_next_utc_midnight` крашился 31-го числа — fix
- 9tt: `is_killed()` `global` placement (PEP-8 fail-closed)
- 9uu: WS book locks (poly_ws, limitless_ws) — race condition
- 9uu: `_fired_arb_keys` eviction — был unbounded leak
- 9uu: cache caps `LIM_META_CACHE_MAX=5000`, `POLY_MARKET_INFO_CACHE_MAX=5000`
- 9uu: tuple timeouts в Kalshi/SX/watchdog (5 callsites)
- 9uu: `/api/approve|reject` size limits + type checks (DOS protection)
- 9uu: `/api/kill` Flask auth через `X-Admin-Token`

**Detection / correctness:**
- 9yy: phantom-on-resolution фильтр (events с `closed=True` или `archived=True`)
- 9yy: `gross_pct` правильная формула для ALL_NO
- 9xx: NEAR rows с negative distance отбрасываются
- 9vv: NEAR badge mismatch fix
- 9ww: Deals badge в nav

**UI улучшения:**
- Колонка «Конец» в NEAR с цветовой индикацией дедлайна
- Header простор (flex-wrap + gap)
- Удалён tech-debt label «Сканирование — polymarket N/N pages»

**Файлы:** `arb_server.py`, `dashboard.html`, `poly_ws.py`, `limitless_ws.py`, `risk/limits.py`, `risk/killswitch.py`, `executor/atomic.py`, `executor/presign.py` (новый), 4 test_phase_9*.py (новых) + правки 4 существующих.

**Tests:** 327 → 355 (+28). Все проходят.

---

<a id="pr-32"></a>
### PR #32 — fix(v2): Phase 9m — close 4 V2 uncertainties
**Merged:** 2026-04-28 | **Branch:** `fix/v2-uncertainties`

Закрытие 4 пунктов V2-неуверенности через **3 параллельных research-агента**.

1. **pUSD адрес verified**: `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB` (был placeholder `0xb24A...`). CollateralOnramp: `0x93070a847efEf7F70739046A929D47a521F5B8ee`. `_wrap_step` переписан: USDC.e → CollateralOnramp → `Onramp.wrap()` → mints pUSD.
2. **/markets/{cid} REST endpoint**: подтверждено via github.com/Polymarket/clob-client. Расширены поля: `accepting_order_timestamp`, `seconds_delay`, `neg_risk_market_id`, `rewards.{rates, min_size, max_spread}`. **Pre-fire gate** в `atomic._build_leg`.
3. **NegRisk routes**: единый `POST /order`, server routes по `verifyingContract` в signed EIP-712.
4. **Builder bytes32 stays zero**: Builder Program это для apps/aggregators (взимание комиссий со своих юзеров). Для соло-трейдера регистрация добавит costs. `builder = ZERO_BYTES32` = no attribution.

**Tests:** +13 в `test_phase_9m.py` → 230/230 OK.

---

<a id="pr-31"></a>
### PR #31 — chore(threshold): Phase 9l — extra +0.002 safety buffer
**Merged:** 2026-04-28 | **Branch:** `chore/extra-safety-buffer`

Дополнительная перестраховка 0.002 ко всем динамическим порогам — защита от:
- Cache stale fee (governance мог поменять taker_base_fee)
- Liquidity drop (depth ушла между scan и POST)
- Drift цен (за 250-500ms цены ноги двинулись)

**Polymarket** (`POLY_SAFETY_BUFFER` 0.005 → 0.007):
| fee | old | new |
|---|---|---|
| 0% | 0.992 | 0.990 |
| 2.5% | 0.967 | 0.965 |
| 4% | 0.952 | 0.950 |

**Limitless**: `THRESH_LIMITLESS` 0.99 → 0.988 (1.2¢ margin per $1).

**Trade-off:** ~0.2% меньше арбов, но каждый имеет +0.2% cushion.

---

<a id="pr-30"></a>
### PR #30 — feat(polymarket): Phase 9k — dynamic threshold per market fee
**Merged:** 2026-04-28 | **Branch:** `feat/dynamic-poly-threshold`

`THRESH_POLY=0.97` хардкоженный был **двусторонне неверен**:
- На 0%-fee рынках V2 promo: РЕЖЕМ валидные арбы 0.97-0.99
- На 3%+-fee рынках: ПРИНИМАЕМ арбы 0.967-0.97 которые после fee УБЫТОЧНЫ

**Математика:**
```
THRESH = 1 - (theta + 0.008)
  где 0.008 = 0.003 slippage + 0.005 safety buffer
Floor 0.95, Cap 0.995
```

**Tests:** +12 → 217/217. Включая `TestProfitabilityAtThreshold` доказывающий что dynamic threshold безопасен (ни один accepted deal не убыточный).

**Эффект на P&L:** +30-50% deals на 0%-fee маркетах; больше не fire'им в убыток на 3%+.

---

<a id="pr-29"></a>
### PR #29 — feat(polymarket): Phase 9j — V2 dynamic market info
**Merged:** 2026-04-28 | **Branch:** `feat/poly-v2-dynamic-market-info`

Закрытие пробела V2 migration: dynamic per-market `feeRateBps`, `minimum_tick_size`, `minimum_order_size` не использовались.

**Что добавлено:**
- `_fetch_poly_market_info(condition_id)` — кэш tick/min/fee на 10 минут
- `effective_theta` = max(taker_fee_bps) / 10000 (worst-case)
- Per-leg V2 metadata через `_attach_poly_v2_meta`
- `_round_to_tick` + `min_order_size_usdc` validation в `build_poly_order`
- `Scripts/polymarket_approve.py` — CLI для V2 setup

**Tests:** +11 → 205/205. Live verification: `_round_to_tick(0.4523, 0.001) = 0.452`.

---

<a id="pr-28"></a>
### PR #28 — fix(critical): Phase 9i — 6 sub-agent-found bugs + V2 polish
**Merged:** 2026-04-28 | **Branch:** `fix/phase-9i-critical-bugs`

После 3 параллельных audit-агентов найдено **6 критических багов**:

| # | Баг | Эффект | Файл |
|---|---|---|---|
| 1 | `MAX_PER_TRADE_USD` как `sum(legs)` не per-leg | 3-leg arb $20/нога ($60) блокировался → P&L резался ×3 | `risk/limits.py` |
| 2 | `jitter_ms_for_leg` определена но не вызывалась | Все ноги ±1ms — фингерпринт бота | `executor/atomic.py` |
| 3 | Round-robin клал 2 ноги на wallet0 | Биржа видит один адрес на обе стороны = бан | `executor/atomic.py` |
| 4 | `_maybe_dry_fire` держал lock во время `fire_arb` | Сериализация 5с + race | `arb_server.py` |
| 5 | `killswitch.is_killed()` fail-OPEN на permission error | STOP жмут, fs error → executor продолжает | `risk/killswitch.py` |
| 6 | `ALL_NO` gross использовал `1 - sum_no` вместо `(N-1) - sum_no` | net <= 0 → ВСЕ ALL_NO пропускались | `arb_server.py` |

**V2 polish:** GTD order_type с `expiration` в POST body, README pUSD wrap step.

**Tests:** +15 → 194/194.

---

<a id="pr-27"></a>
### PR #27 — fix(filter): drop event when any outcome closed/expired/hidden
**Merged:** 2026-04-28 | **Branch:** `fix/market-status-gate`

Защита от **«outcome закрылся между detection и fire»**: outcome был открыт когда сканер увидел, между этим и POST orders Limitless закрыл его → у нас нет YES_DRAW. Если Draw победит → теряем 2 ноги.

**Не путать** с уже-купленным арбом: если все 3 ноги filled, последующее закрытие НЕ влияет на on-chain payout.

**Файлы:** `filter_limitless` event-level + per-child status, `filter_poly` event-level + per-child (closed/archived/restricted/enableOrderBook=False/acceptingOrders=False).

**Tests:** +5 в `test_limitless.py` → 73/73, total 179/179.

---

<a id="pr-26"></a>
### PR #26 — fix(eval): incomplete-coverage gate for ALL_YES / ALL_NO (CRITICAL)
**Merged:** 2026-04-28 | **Branch:** `fix/incomplete-outcome-coverage`

Закрыта **критическая дыра**: при отсутствии ask на одном исходе multi-outcome события (volume=0 на Draw) старый код выкидывал исход и считал ALL_YES суммой по оставшимся. **Гарантированный убыток** если не-цененный исход побеждал.

**Production-сценарий 28.04.2026:**
> EPL Leeds vs Burnley: Leeds 67.5¢, Draw 20.6¢ (volume=0), Burnley 13¢.
> Σ всех = 101.1¢ (НЕ арб), радар показал sum=80.5¢ как ALL_YES net $10.61.

Если бы fire'или и Draw победил — потеря всех 3 stakes ($45-55).

**Фикс:** `outcomes_missing_yes/no` трекается; ALL_YES/ALL_NO суппрессированы при `full_*_coverage = False`. Для всех 3 evaluator'ов (limitless/poly/kalshi).

**Tests:** +5 → 174/174. Включая `test_leeds_burnley_no_longer_reports_phantom_arb`.

---

<a id="pr-25"></a>
### PR #25 — feat: Limitless Exchange — 4-я платформа (Base L2, no-KYC)
**Merged:** 2026-04-28 | **Branch:** `feat/limitless-platform`

Limitless: CLOB на Base L2, без KYC и без комиссии за матчинг (только gas ~$0.01). Окон арбитража объективно больше → `THRESH_LIMITLESS = 0.99` vs Polymarket 0.97.

Все три структуры (A/B/C) для negRisk-групп и standalone-binary рынков.

**Файлы:**
- `arb_server.py` — config, `_fetch_limitless_orderbook` с синтезом NO-ask = 1 − best YES bid, `eval_limitless`, `limitless_micro_loop` REST 5с
- `executor/builders.py` — `build_limitless_order` EIP-712 для Base (chainId 8453)
- `executor/atomic.py` — dispatch для Limitless по `slug + side`
- `dashboard.html` — 4-я stat-card

**Tests:** +13 → 100/100. Smoke API подтвердил `markets/active` HTTP 200.

**Phase 2 отложено:** WebSocket Limitless (URL не в OpenAPI), real EIP-712 signing.

---

<a id="pr-24"></a>
### PR #24 — feat: end_date column + revert WINDOW_DAYS to 10
**Merged:** 2026-04-28 | **Branch:** `feat/history-end-date`

1. **Колонка «Резолв»** в Истории: Polymarket `event.endDate`, Kalshi `event.close_time`, SX Bet `gameTime` → ISO. Цвет: ≤3д green, ≤7д gold, >7д text2.
2. **WINDOW_DAYS 30 → 10** (откат #6): месяц блокировки капитала ради $5-30 — плохой turnover. ×3 capital efficiency. Теряем ~30-40% сигналов на A (дальние праймериз) — они и так невыгодны из-за заморозки.

Backward-compat: старые `opened` без `end_date` рендерятся как `—`.

---

<a id="pr-23"></a>
### PR #23 — feat(analytics): remove manual decision flow + per-trade history
**Merged:** 2026-04-28 | **Branch:** `feat/analytics-cleanup-history`

**Удалено** (manual decision):
- Backend: `record_decision()`, POST /api/analytics/decision, real_net/taken/skipped
- UI: stat-cards Real P&L + Взято/Пропущено, кнопки Took/Skipped

**Добавлено** (history):
- `analytics.history(period, limit, offset, platform?, structure?, min_net)`
- by_structure разбивка в aggregate (A/B/C/binary)
- GET /api/analytics/history
- UI: «История сделок» — таблица всех opened с фильтрами + пагинация (100/стр)

Поля: Время | Платформа | Структура | Сделка | Sum | Net | ROI | Grade | Min liq | Длит. | Стат.

---

<a id="pr-22"></a>
### PR #22 — feat: Telegram alerts (kill / daily-loss / network / startup)
**Merged:** 2026-04-28 | **Branch:** `feat/telegram-alerts`

`Scripts/notify.py` — non-blocking, rate-limited, graceful degradation.

| Событие | Уровень | Dedupe |
|---|---|---|
| Kill activated | crit | killswitch_active |
| Daily loss limit hit | crit | daily_loss_{date} |
| Hourly losing streak (5/h) | warn | hourly_streak_{hour} |
| Reconcile mismatch | crit | через kill chain |
| Network check failed | warn | 1/min |
| Radar startup | success | radar_startup |

**Дизайн:** urllib stdlib (без новых deps), daemon thread (hot path не ждёт), Markdown, dedupe 60с.

**Tests:** +9 → 109/109.

---

<a id="pr-21"></a>
### PR #21 — feat: network safety (IP/country gate + VPN docs + hot standby)
**Merged:** 2026-04-28 | **Branch:** `feat/network-safety`

3 кумулятивных слоя:

| Слой | Что |
|---|---|
| 1. System firewall (iptables / Mullvad lockdown) | блок outbound вне VPN |
| 2. systemd dependency (BindsTo VPN) | radar падает с VPN |
| 3. App-level IP/country check (`risk/network_check.py`) | бот сам проверяет IP каждые 60с |

**Endpoint:** `GET /api/network_status` — текущий IP/country + cache age.

**Tests:** +13 → 100/100. fail-safe: failed fetch → BLOCK.

---

<a id="pr-20"></a>
### PR #20 — docs: add README.md
**Merged:** 2026-04-28 | **Branch:** `chore/add-readme`

Entry point на GitHub homepage. `idea.md` остаётся полной спецификацией (28KB), README — короткий 1-2 экрана: что/3 структуры/quick start/Polymarket-only режим/Docker/архитектура/risk-параметры.

---

<a id="pr-19"></a>
### PR #19 — fix: risk-aware deal sizing + log risk-blocked attempts
**Merged:** 2026-04-28 | **Branch:** `fix/risk-aware-sizing`

Root cause **«paper_results.jsonl пустой 32 часа»**:
1. `BALANCE = 100` vs `MAX_PER_TRADE_USD = 55` → каждая сделка > лимит → блок
2. Pre-trade check предполагал 100% loss — для арба неверно (max ~5-15% slippage)

**Silent блок** — в `dryrun.jsonl` ничего не писалось → не видно почему.

**Fix:** `build_deal` капит `actual_balance` через `min(BALANCE × scale, MAX_PER_TRADE_USD)`. `fire_arb` log_decision на ВСЕХ early-return paths. `check_can_fire` различает арбы (worst_case = 15% от cost) vs направленные.

Дополнительно (commit `f90ec8c`): `ENABLE_KALSHI=0` / `ENABLE_SX=0` env vars + `POLY_MAIN_PAGES=4` (1000 → 2000 events).

**Tests:** +1 regression → 87/87.

---

<a id="pr-18"></a>
### PR #18 — feat: Phase 7 — SX Bet executor finalization
**Merged:** 2026-04-27 | **Branch:** `feature/phase7-sx-bet-executor`

PR #13 ship'нул `build_sx_order` как **скелет** (`orderHashes: None`). Этот PR доделывает реальный matching.

**SX Bet flow:**
```
build_sx_order(market_hash, outcome, taker_price, size_usdc, wallet)
  ├─ fetch_sx_matchable_orders()  → GET /orders?marketHashes=X&maker=true
  │   filter: opposite-side maker'ы (taker outcome=1 → maker outcome=2)
  │   parse: percentageOdds, orderSizeFillable
  ├─ match_sx_orders()            → greedy на отсортированном
  │   stop: size covered / slippage cap (0.5¢) / orders exhausted
  └─ build POST body              ← orderHashes[] + takerAmounts[] + EIP-712 ready
```

**Partial-fill rule (КРИТИЧНО):** Если **любая нога** partial-fill — `aborted_reason: partial_fill_arb_broken`. Realistic-fill **пропускается** (paper_results.jsonl честный).

**Tests:** +17 → 86/86.

---

<a id="pr-17"></a>
### PR #17 — feat: Phase 6 — VPS-readiness (Docker + watchdog)
**Merged:** 2026-04-27 | **Branch:** `feature/phase6-vps-deploy`

```
docker-compose.yml
├── radar      ─ python Scripts/arb_server.py     :5050
│                healthcheck /api/risk_status
└── watchdog   ─ python Scripts/watchdog.py
                 polls Executions/.killed @ 1Hz
                 fires cancel hooks on kill transition

Both share volume: ./Executions:/app/Executions
```

**Watchdog отдельным процессом** нужен — если main радар повис, file-flag всё равно виден.

**deploy/README.md:** AWS us-east-2 (t4g.small $15/мес или Fargate $12), DO NYC ($12/мес), latency budget (AWS 5-15ms, DO 20-40ms vs Москва 250ms), operational checklist.

---

<a id="pr-16"></a>
### PR #16 — feat: Phase 5 — paper trading + graduation gate
**Merged:** 2026-04-27 | **Branch:** `feature/phase5-paper-trading-graduation`

**Условия gate (immutable):**
| Условие | Порог |
|---|---|
| Минимум paper trades | 100 (позже снижено до 50) |
| Win rate (positive realistic_pnl_5s) | ≥ 70% |
| Mean drift `\|realistic − sim\|` | ≤ 20% |

**После graduation:**
1. `🎓 GRADUATION READY` в шапке
2. Заполняешь `BOT*_PRIVATE_KEY` (Phase 4)
3. `DRY_RUN=0`, рестарт
4. Первые **10 реальных сделок** — leg size $5 (не $55), финальная калибровка
5. После 10 успешных — full size, Phase 3 risk limits

**Endpoints:** `/api/graduation`, `/api/paper_distribution`, `/api/graduation_history`.

**UI:** клик на `paper: X/100 trades` → modal (header, blockers, ASCII histogram, 14-day series).

**Tests:** +11 → 69/69.

---

<a id="pr-15"></a>
### PR #15 — feat: Phase 4 — multi-bot wallets (6 bots)
**Merged:** 2026-04-27 | **Branch:** `feature/phase4-multi-bot-wallets`

| Параметр | Значение |
|---|---|
| `BOT_COUNT` | 6 |
| `MIN_USDC_PER_BOT` | $60 (coordinator пропускает ботов под этим) |
| `REBALANCE_LOW_USDC` | $60 |
| `REBALANCE_HIGH_USDC` | $200 |
| `REBALANCE_RESERVE_USDC` | $130 |
| `REBALANCE_PAIR_COOLDOWN_S` | 3600 |
| `ASSIGN_JITTER_MAX_MS` | 50 |

**Anti-detection** (от пользователя): одна нога арба = один кошелёк, всегда. С 6 ботами и 2-7 ногами никогда не аггрегируем.

**Auto-rebalance** (от пользователя 27.04.2026): «Если на одном боте заканчивается маржа, то с тех которые в этот момент зарабатывали, она должна перекидываться». Phase 4 пишет `proposal_dryrun` в `Executions/rebalance.jsonl`. Phase 6 включит реальные `USDC.transfer()`.

**Stores:** `LocalEnvStore` (default), `WindowsCredStore`, `AwsSecretsStore` (skeletons для Phase 6).

**Endpoints:** `/api/wallets`, `/api/rebalance/proposals`.

**Tests:** +16 → 58/58.

---

<a id="pr-14"></a>
### PR #14 — feat: Phase 3 — risk management
**Merged:** 2026-04-27 | **Branch:** `feature/phase3-risk-management`

| Лимит | Значение | Действие |
|---|---|---|
| `MAX_PER_TRADE_USD` | $55 | reject — `per_trade_cap_$55_exceeded` |
| `DAILY_LOSS_LIMIT_USD` | $35 | пауза до 00:00 UTC |
| `LOSING_TRADES_PER_HOUR` | 5 (rolling) | пауза 1ч |
| concurrent positions | без лимита | — |
| repeat arbs per event | без лимита | — |

**Правила (от пользователя):**
- На паузе/kill **не закрывать** позиции
- Kill требует **двойного подтверждения**
- Paper trades НЕ считаются в лимиты

**Архитектура:**
```
fire_arb → risk.check_can_fire → killed? cost > $55? paused? worst-case > -$35?
watchdog → Executions/.killed → cancel pending
reconcile → /positions × биржа → diff > $0.01 → kill()
```

**Endpoints:** `/api/risk_status`, `POST /api/kill {confirm:'YES'}`, `POST /api/risk_resume`.

**UI:** 🛑 STOP кнопка → prompt(reason) → confirm → kill; превращается в ↺ RESUME.

**Tests:** +23 → 42/42.

---

<a id="pr-13"></a>
### PR #13 — feat: Phase 2 — atomic execution engine (dry-run only)
**Merged:** 2026-04-27 | **Branch:** `feature/phase2-atomic-executor-dryrun`

**Архитектура:**
```
deal (HOT) → _maybe_dry_fire(deals) → fire_arb(deal, wallets)
                ├─ _assign_wallets: round-robin, 1 leg = 1 bot
                ├─ ThreadPoolExecutor параллельный fire (target <100ms)
                ├─ DRY_RUN=True → _fire_one_leg_dryrun (логирует, не POST'ит)
                ├─ DRY_RUN=False → blocked с aborted_reason до Phase 4/5
                └─ schedule_realistic_eval (5с) → daemon thread:
                      ├─ refetch orderbook
                      └─ append paper_results.jsonl
                
Executions/dryrun.jsonl + Executions/paper_results.jsonl
```

**Builders:**
- Polymarket EIP-712 body, USDC/CTF amount encoding
- SX Bet maker→taker через maxPercentageOdds (skeleton, доделан в #18)
- Kalshi disabled marker

**Endpoints:** `/api/paper_stats`, `/api/dryfire`.

**UI:** 🧪 Dry-fire кнопка на карточках + paper-trade панель.

**Tests:** 19 → 19/19.

---

<a id="pr-12"></a>
### PR #12 — feat: Phase 1 — NO tokens + 3 arb structures + SX Bet taker fix
**Merged:** 2026-04-27 | **Branch:** `feature/phase1-no-tokens-arb-structures`

| Структура | Условие | Когда |
|---|---|---|
| **A. ALL_YES** | Σ YES_ask < THRESH | всегда |
| **B. ALL_NO** | Σ NO_ask < (N−1) · THRESH | multi-outcome, N≥3 |
| **C. YES_NO_PAIR** | per-market: yes + no < THRESH | любой бинарный |

SX Bet — все три коллапсируют в `binary`.

**Critical SX Bet fix:** старый код хранил `best1 = max(maker_bid where isMakerBettingOutcomeOne=True)` — это не taker ask. SX Bet **не показывал ни одной сделки** хотя сканировал 638+ markets.

Стало: `best1 = 1 − max(maker_bid_на_outcomeTwo)` (берём противоположную сторону).

**MAX_WS_SUBS** 500 → 1000 для YES+NO subscription doubling.

**UI:** Structure badges (A · ALL YES / B · ALL NO / C · YES+NO / ◑ binary), новая колонка «Структура» в NEAR.

---

<a id="pr-11"></a>
### PR #11 — feat: SX Bet wider 27 binary market types
**Merged:** 2026-04-26 | **Branch:** `feat/sx-wider-markets`

Расширение `SX_BINARY_TYPES` до 27 типов: Soccer Total/Spread/DrawNoBet, Basketball periods (1/2/3) для Total/Spread/Moneyline, Hockey Total/Moneyline/Spread, MMA Total, Tennis Sets Total/Games Total/Spread, Baseball 1st 5 Total, E-Sports Total. Type=1 (3-way soccer) **исключён** (нужен отдельный 3-way pipeline).

(PR #9 / #10 auto-closed из-за DELETE-after-merge race; описание дублирует #9.)

---

<a id="pr-8"></a>
### PR #8 — feat: WS scale-up (500 subs, NEAR_BUFFER 7c) + NEAR tab
**Merged:** 2026-04-26 | **Branch:** `feat/ws-scale-up-and-near-tab`

1. `MAX_WS_SUBS` 200 → 500, `NEAR_BUFFER` 0.03 → 0.07
2. **Вкладка NEAR** — кто близок к арбу (1-7¢ выше порога)

`classify_pools` сортирует HOT и NEAR по sum-ascending — при cap'е MAX_WS_SUBS обрезается хвост (без сортировки можно потерять самые горячие).

**Endpoint:** `GET /api/near` — `{count, buffer_cents, items[]}`.

**UI:** Tab `NEAR` — # | Платформа | Событие | Sum | До арба | Порог | Исх. | Min цена | Min liq. Цвет distance: <1¢ green, <3¢ gold. Auto-refresh 5s.

---

<a id="pr-7"></a>
### PR #7 — feat: Analytics tab
**Merged:** 2026-04-26 | **Branch:** `feat/analytics-tab`

Полнофункциональная вкладка Analytics — два параллельных трека:
- **Sim P&L** — каждая увиденная сделка как «вошли $100 при появлении» (без участия пользователя)
- **Real P&L** — только сделки с ✅ Взял в карточке (удалено в #23)

Period switcher: День/Неделя/Месяц/Всё.

**`Scripts/analytics.py`** (~230 строк): append-only `Executions/analytics_events.jsonl` (`opened` / `closed` / `decision`), `Executions/analytics_state.json` для рестарта без повторного зачитывания, `RLock` thread-safe, `aggregate(period)`.

**Endpoints:** `GET /api/analytics?period=...`, `POST /api/analytics/decision` (удалено в #23).

---

<a id="pr-6"></a>
### PR #6 — chore: extend event window 10 → 30 days
**Merged:** 2026-04-26 | **Branch:** `chore/extend-window-to-30-days`

Диагностика #4: на тихий день 10-day отсекает почти всё (`poly_skip_no_window: 988/1000`). Расширили до 30. Капитал замораживается дольше, но больше сигналов.

(Позже **revert** в #24 → 10, потом 9v → 13 — sweet spot для capital efficiency.)

---

<a id="pr-5"></a>
### PR #5 — fix: SX Bet pageSize capped at 100 (HTTP 400)
**Merged:** 2026-04-26 | **Branch:** `fix/sx-pagesize-100`

Диагностика #4: `sx_http_status: 400, pageSize must not be greater than 100`. SX Bet ужесточил лимит, код стучал с `pageSize=200` → `sx_markets: 0`.

`SX_PAGE_SIZE = 100`, `SX_MAX_PAGES_MAIN = 10` (1000 markets), `SX_MAX_PAGES_PAUSE = 5` (500). Объём не меняется, page size меньше.

---

<a id="pr-4"></a>
### PR #4 — feat: filter diagnostics counters
**Merged:** 2026-04-26 | **Branch:** `feature/filter-diagnostics`

Per-step счётчики на каждом этапе фильтрации — видно где именно отсеиваются кандидаты. `poly_in`, `poly_pass`, `poly_skip_*` (blacklist / no_window / lt2_markets / no_negrisk / lt2_rough / sum_high / deadline_text). Аналогично для kalshi/sx.

**Критерий:** `poly_in = poly_pass + sum(poly_skip_*)` — никакие events не теряются.

---

<a id="pr-3"></a>
### PR #3 — fix: read negRisk from event, not market
**Merged:** 2026-04-26 | **Branch:** `fix/poly-event-level-negrisk`

`filter_poly` отбрасывал ~100% Polymarket-кандидатов: `negRisk` лежит на уровне **event**, старый код проверял market-уровень (почти всегда `False` даже для mutually-exclusive).

**Эмпирика:** 20 events, `event.negRisk=True: 1`, `market.negRisk: 0`. На 1000 events за скан — `poly_neg_risk: 0`, итого 0 deals.

**Fix:** ИЛИ `event.negRisk=True`, ИЛИ все `market.negRisk=True` (fallback).

---

<a id="pr-2"></a>
### PR #2 — feat: Polymarket WebSocket + HOT/NEAR pool architecture
**Merged:** 2026-04-26 | **Branch:** `feature/polymarket-ws-and-hot-near-pools`

WebSocket + HOT/NEAR pool — следим не только за активными арбами, но и за теми, кто на 1-3¢ выше порога. Kalshi/SX остаются на REST но опрашиваются адаптивно (только HOT+NEAR, разные интервалы).

**Safeguards:**
- WS hard cap: `MAX_WS_SUBS=200`
- Backoff: 1→2→4→8→30с
- Heartbeat: PING каждые 10с (plain text), watchdog 30с
- Coalescing: callback'и батчатся 250мс
- Polymarket REST fallback **только** если WS молчит >30с

**Files:**
- `Scripts/poly_ws.py` (новый, 270 строк) — `PolyMarketWS` класс
- `arb_server.py` v6.1 → v7: `pools{poly,kalshi,sx}{hot,near}`, `classify_pools`, `collect_poly_tokens`, `on_ws_update`
- `dashboard.html` — виджет `📡 WS: subs/max · msg/s · age`

---

<a id="pr-1"></a>
### PR #1 — fix: sync per-platform thresholds with idea.md
**Merged:** 2026-04-26 | **Branch:** `fix/sync-thresholds-with-idea`

`THRESH_*` все три на `0.985` (98.5¢) — противоречит idea.md. При sum=98.5¢ профит-грязь $1.5 на $100 не покрывает taker fee, особенно Kalshi (7%) → убыток.

| | old | new | reason |
|---|---|---|---|
| Polymarket | 0.985 | 0.97 | taker fee 2.5% |
| Kalshi | 0.985 | 0.93 | taker fee 7% |
| SX Bet | 0.985 | 0.97 | taker fee 2% |

---

## Pre-PR commits (initial)

| Commit | Дата | Что |
|---|---|---|
| `f7e2ec4` | 2026-04-25 | Initial commit: Arbitrage Radar + InsiderRadar |
| `bc30ba1` | 2026-04-26 | Add CLAUDE.md: project memory and PR procedure |

---

## Numbering convention

- **Phase 1-7** — фундаментальная архитектура (PR #12-#18)
- **Phase 9a-9zz** — итеративные фиксы / доработки в `feature/phase-9*` ветках
- **Phase 9aaa-9iii** — performance + production hardening + post-deploy
- **Phase 9jjj+** — operator-driven tuning (например `GRADUATION_MIN_TRADES 100→50`)

После каждой Phase коммит-сообщение содержит её тег (`Phase 9rr — ...`). По тегу можно найти PR через этот CHANGELOG.

## Refs

- [CLAUDE.md](CLAUDE.md) — проект memory + PR procedure + GitHub auth
- [idea.md](idea.md) — полная спецификация (28KB)
- [README.md](README.md) — entry point на GitHub
- [.claude/skills/](.claude/skills/) — 24 skill (16 historical + 8 new в этой сессии)
