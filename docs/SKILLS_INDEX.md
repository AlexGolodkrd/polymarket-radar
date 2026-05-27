# Skills index — `.claude/skills/`

> Каталог локальных Claude skill-файлов проекта. **Сами skills gitignored** (operator-local), этот index в репо для навигации. Категории + статус + краткое описание.
>
> Последний аудит: 27.05.2026 (audit-28b cont 2).

## Категории

### 🔴 Critical — Platform-specific (Polymarket V2 era)

| Skill | Status | Назначение |
|---|---|---|
| `polymarket-v2-auth` | ✅ active | L2 HMAC + EIP-712 ClobAuth для Polymarket V2 |
| `polymarket-v2-connector` | ✅ active | py-clob-client-v2 use patterns + Order struct V2 |
| `polymarket-v2-troubleshoot` | ✅ active | 422/401 разборы post-V2 cutover |
| `polymarket-fee-schedule` | ✅ active | feeSchedule object (31.03.2026 change) + fallback на maker_base_fee/taker_base_fee |
| `polymarket-post-v2-status` | ✅ active | Состояние V2 endpoints + breaking changes по неделям |
| `polymarket-heartbeats-cancel` | ✅ active | DELETE /order patterns (см. drift audit — URL mismatch TS vs Python) |
| `polymarket-keyset-pagination` | ⚠️ pending | После 14.05.2026 keyset стал обязателен, но мы пока используем legacy gamma-api offset |
| `polymarket-query` | ✅ active | Гамма-API events + markets query patterns |
| `polymarket-trading` | ✅ active | Order placement workflow |
| `limitless-trading` | ✅ active | Limitless v3 API workflow |
| `limitless-hmac-auth` | ✅ active | HMAC-SHA256 для REST + WS handshake |
| `sx-bet-trading` | ✅ active | SX OrderFill v2 protocol + nested Details/FillObject |
| `cross-exchange-execution` | ✅ active | Cross-platform arb execution patterns |
| `cross-platform-arbs` | ✅ active | X1/X2 structures + complement_cover |
| `event-matching-fuzzy` | ✅ active | Fuzzy event name + league + scope guards |
| `time-freshness-validation` | ✅ active | Adaptive grace minutes по event duration |

### 🟡 Infrastructure / patterns

| Skill | Status | Назначение |
|---|---|---|
| `eip712-typescript-parity` | ✅ active | Python↔TS golden vectors для EIP-712 |
| `fillregistry-pattern` | ✅ active | Pre-subscribe to WS + fill confirmation registry |
| `ws-listener-lifecycle` | ✅ active | Reconnect/teardown + handshake p99 |
| `websocket-reliability` | ✅ active | Backoff + heartbeat + circuit breaker |
| `circuit-breaker-patterns` | ✅ active | 3-state CB (CLOSED/OPEN/HALF_OPEN) + recovery |
| `http-rate-limiting` | ✅ active | Rate-limit handling (Limitless 429-resistant patterns) |
| `residential-proxy-routing` | ✅ active | Proxy on POST only, not fetch |
| `secrets-management` | ✅ active | Credentials.env handling + token rotation |
| `feature-flags` | ✅ active | Env-driven gates |
| `error-budget-policy` | ✅ active | SLO + alerting thresholds |
| `observability-stack` | ✅ active | Метрики + percentiles + dashboards |
| `auto-deploy-gotchas` | ✅ active | GitHub Actions → VPS pitfalls |
| `deploy-pipeline` | ✅ active | Deploy procedure runbook |
| `browser-cache-busting` | ✅ active | Cache-Control + ETag patterns |

### 🟢 Language / tooling

| Skill | Status | Назначение |
|---|---|---|
| `async-python-patterns` | ✅ active | asyncio + queue patterns |
| `python-automation` | ✅ active | Stdlib utilities |
| `python-execution-sandbox` | ✅ active | Sandboxed eval |
| `pytest-setup` | ✅ active | Test configuration |
| `vitest-mocks` | ✅ active | TS test mocking patterns |
| `flask-best-practices` | ✅ active | Flask app structure |
| `uvicorn-production` | ✅ active | WSGI production |
| `docker-management` | ✅ active | Docker compose ops |
| `docker-patterns` | ✅ active | Multi-stage builds |
| `javascript-style` | ✅ active | JS code style guide |
| `cloudflare-platform` | ✅ active | CF Workers / KV |
| `web3-onchain-prep` | ✅ active | On-chain approve/wrap procedures |
| `systematic-debugging` | ✅ active | Debugging methodology |

### 🟣 Meta / ops

| Skill | Status | Назначение |
|---|---|---|
| `dr-manhattan` | ✅ active | Multi-agent orchestration |
| `opus-4-7-migration` | ✅ active | Claude Opus 4.7 / 1M context patterns |

## Сводка

| Категория | Кол-во | Все active? |
|---|---|---|
| Critical / Platform | 16 | 15 active, 1 pending (keyset) |
| Infrastructure / patterns | 13 | 13 |
| Language / tooling | 13 | 13 |
| Meta / ops | 2 | 2 |
| **TOTAL** | **44** | **43 active, 1 pending** |

## Action items (audit-28 follow-up)

1. **`polymarket-keyset-pagination`** — после реальной depreciation legacy offset на gamma-api сделать активным + обновить `async_fetchers.py`.
2. Никаких duplicates / outdated не обнаружено в этом audit pass.
3. Skills работают как **referenced docs** — Claude вызывает их по контексту. Если оператор добавил новые / удалил — обновлять этот index.

## Update procedure

После добавления / удаления / обновления skill в `.claude/skills/`:

```bash
ls .claude/skills/ > /tmp/skills.txt
# diff с верхней секцией этого файла
```
Если изменилось — пересинхронизировать таблицу. Skills сами не коммитятся в репо (gitignored), но index здесь — да.
