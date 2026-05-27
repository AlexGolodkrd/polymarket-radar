# plan-kapkan

Arbitrage radar on prediction-market платформах **Polymarket**, **Limitless Exchange** (Base L2, no KYC) и P2P-бирже ставок **SX Bet**. Сканер на Python (Flask + dashboard), исполнитель ордеров на TypeScript (`executor-ts/`), 6-bot wallet pool с anti-detection.

> **Статус (27.05.2026):** DRY_RUN=1 (paper). Live execution включается после Phase 5 graduation gate (≥50 paper trades, win-rate ≥70%, drift ≤20%). Production: https://kapkan.4frdm.live.

## Что это

Радар находит арб-окна — ситуации когда сумма ask-цен по всем взаимоисключающим исходам строго меньше $1 (минус комиссии и slippage-reserve). Три структуры:

- **A. ALL_YES** — Σ yes_ask < threshold
- **B. ALL_NO** — Σ no_ask < (N−1) · threshold  (multi-outcome events)
- **C. YES_NO_PAIR** — per-market: yes_ask + no_ask < threshold

Плюс cross-platform pairs (Polymarket+SX, Limitless+SX, Polymarket+Limitless) — структуры X1/X2.

Найденные арбы автоматически проходят через **dry-run executor**: Python сканер шлёт `POST /fire` в TypeScript executor service на :5051, тот в свою очередь либо логирует решение (`dryrun.jsonl`) либо POST'ит реальные ордера на биржи (когда оператор флипнет `DRY_RUN=0`).

## Quick start (dry-run)

```bash
git clone https://github.com/AlexGolodkrd/plan-kapkan.git
cd plan-kapkan
pip install -r requirements.txt
cp Credentials.env.example Credentials.env   # заполнить адреса 6 ботов + cold wallet
python Scripts/arb_server.py
```

Dashboard: http://localhost:5050.

С Docker (рекомендуется на VPS):
```bash
docker compose up -d
docker compose logs -f radar
```

См. [`deploy/README.md`](deploy/README.md) — AWS / DigitalOcean / Hetzner инструкции.

## Архитектура (краткий вид)

| Слой | Где |
|---|---|
| Detection (Python) | `Scripts/arb_server.py`, `Scripts/dashboard.html`, `Scripts/poly_ws.py`, `Scripts/limitless_ws.py` |
| Новый пакет (audit-28+) | `Scripts/radar/` — dedup, api blueprints, filters |
| Configuration | `Scripts/config.py` (pydantic-settings v2) |
| Python↔TS contract | `Scripts/contracts.py` (Pydantic FireRequest/LegEntry/FireResponse) |
| Execution (TypeScript) | `executor-ts/` — EIP-712 signers, user-channel WS, real HTTP fires |
| Atomic engine + paper | `Scripts/executor/` (Python side, постепенно мигрирует в `executor-ts/`) |
| Risk (Python) | `Scripts/risk/` — limits, killswitch, reconcile, network gate |
| Wallets (Python) | `Scripts/wallets/` — 6-bot pool coordinator |
| Paper trading | `Scripts/paper_trading.py` |
| Watchdog | `Scripts/watchdog.py` |
| VPS deploy | `Dockerfile`, `docker-compose.yml`, `deploy/` |
| Тесты | `tests/` (88 pytest файлов) |

Подробная карта модулей + target-layout + migration plan: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Risk-параметры (по умолчанию)

| Параметр | Значение |
|---|---|
| Max per **leg** | $5 (`MAX_PER_TRADE_USD`) |
| Daily loss limit | $35 (сброс в 00:00 UTC) |
| Losing trades/час → пауза 1ч | 5 |
| Fire cooldown TTL | 30 min (`FIRE_COOLDOWN_S`) — предотвращает 18-fire-in-1h loop |
| Close grace (analytics) | 10 scans (`CLOSE_GRACE_SCANS`) |
| Anti-detection | 1 нога арба = 1 кошелёк |
| Auto-rebalance USDC между ботами | $200 → $60 трешхолды |

Все env-параметры с типами + диапазонами + докстрингами → `Scripts/config.py::RadarConfig`.

## Документация

| Файл | Что |
|---|---|
| [`RULES.md`](RULES.md) | **Правила оператора** — read first после `/compact` |
| [`CLAUDE.md`](CLAUDE.md) | Память агента (проектный контекст) |
| [`idea.md`](idea.md) | Изначальная спецификация (Phase 0 — до TS migration) |
| [`CHANGELOG.md`](CHANGELOG.md) | PR-by-PR история |
| [`BUG_CATALOG.md`](BUG_CATALOG.md) | Каталог багов / phantom'ов / anti-pattern'ов |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Module map + migration plan audit-28a→e |
| [`docs/CREDENTIALS_GUIDE.md`](docs/CREDENTIALS_GUIDE.md) | Wallet + L2 creds setup |
| [`docs/OPERATOR_RUNBOOK.md`](docs/OPERATOR_RUNBOOK.md) | Daily ops, диагностика, env tuning |
| [`docs/ORDER_FLOW.md`](docs/ORDER_FLOW.md) | Order pipeline detail |
| [`docs/PUBLIC_AUDIT_ENDPOINT.md`](docs/PUBLIC_AUDIT_ENDPOINT.md) | `/api/recent_deals` audit access |
| [`docs/DEPLOY_SETUP.md`](docs/DEPLOY_SETUP.md) | GitHub Actions auto-deploy |
| [`docs/TS_HISTORY.md`](docs/TS_HISTORY.md) | Историческая запись TS migration |
| [`deploy/README.md`](deploy/README.md) | Primary deploy guide |
| [`deploy/DEPLOY_PLAYBOOK.md`](deploy/DEPLOY_PLAYBOOK.md) | Pre-deploy checklist |
| [`deploy/ROLLBACK.md`](deploy/ROLLBACK.md) | Откат |
| [`deploy/VERIFICATION.md`](deploy/VERIFICATION.md) | Post-deploy verify |

## Тесты

```bash
# Targeted suite — должен быть 100% green
python -m pytest tests/test_phase_9uu_concurrency.py \
                 tests/test_phase19v19_audit_fixes.py \
                 tests/test_phase_audit4_positions_open_resolved.py \
                 tests/test_phase19v33_version_endpoint.py \
                 tests/test_phase19v35_recent_deals.py \
                 tests/test_build_deal_payout_math.py \
                 tests/test_phase_9i.py \
                 tests/test_executor.py \
                 tests/test_paper_trading.py \
                 tests/test_wallets.py \
                 tests/test_polymarket.py

# Wider sweep — баseline ~898 pass / ~54 fail (test-ordering pollution)
python -m pytest tests/
```

CI gate (proposed via `pyproject.toml`): `ruff check` + `mypy` (strict на новых модулях) + `pytest` — должны быть зелёные.

## Workflow до live-торговли

1. Создать 6 hot + 1 cold кошельков (MetaMask, self-custodial).
2. Заполнить `BOT*_ETH_ADDRESS` + `COLD_WALLET_ADDRESS` в `Credentials.env`.
3. Депозит USDC: на Polygon (Polymarket pUSD) и на Base (Limitless) + газ.
4. **Polymarket V2** — wrap USDC.e → pUSD + approve через `polymarket.com` UI.
5. **Limitless** — `python Scripts/limitless_approve.py`.
6. **API credentials**:
   - `LIMITLESS_API_KEY` через limitless.exchange UI
   - `BOT*_POLY_API_KEY/SECRET/PASSPHRASE` через `Scripts/poly_derive_api_creds.py --bot bot{N}`
7. Запустить радар в dry-run, накопить ≥50 paper trades.
8. Graduation gate ✅ (≥70% win-rate, drift ≤20%) → `BOT*_PRIVATE_KEY` в `Credentials.env`, flip `DRY_RUN=0`.
9. Первые 10 сделок принудительно $5/нога (calibration), потом полный размер.

Полная процедура → [`docs/CREDENTIALS_GUIDE.md`](docs/CREDENTIALS_GUIDE.md).

## Лицензия

Приватный репозиторий. Все права защищены.
