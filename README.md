# plan-kapkan

Радар арбитражных окон на prediction-market площадках **Polymarket**, **Kalshi** и P2P-бирже ставок **SX Bet**, плюс автоматический исполнитель ордеров с защитой капитала и paper-trading валидацией.

> **Статус:** dry-run only. Реальная торговля включается после Phase 5 graduation gate (≥100 paper-trades, win rate ≥70%, drift ≤20%).

## Что это

Сканер находит арб-окна — ситуации когда сумма ask-цен по всем взаимоисключающим исходам строго меньше $1 (с учётом комиссий). Поддерживает **три структуры арбитража**:
- **A. ALL_YES** — Σ yes_ask < threshold
- **B. ALL_NO** — Σ no_ask < (N−1) · threshold (multi-outcome events)
- **C. YES_NO_PAIR** — per-market: yes_ask + no_ask < threshold

Найденные арбы автоматически проходят через **dry-run executor** (Phase 2), который записывает решения в `Executions/dryrun.jsonl` и через 5 секунд переснимает orderbook чтобы посчитать реалистичный fill в `Executions/paper_results.jsonl`.

## Quick start (dry-run)

```bash
git clone https://github.com/AlexGolodkrd/plan-kapkan.git
cd plan-kapkan
pip install -r requirements.txt
cp .env.example Credentials.env   # заполнить адреса 6 ботов + cold wallet
python Scripts/arb_server.py
```

Дашборд: http://localhost:5050

Только Polymarket (отключить Kalshi/SX, расширить Polymarket):
```bash
ENABLE_KALSHI=0 ENABLE_SX=0 POLY_MAIN_PAGES=4 python Scripts/arb_server.py
```

## Docker (для VPS-деплоя)

```bash
docker compose up -d
docker compose logs -f radar
```

См. `deploy/README.md` для AWS / DigitalOcean инструкций.

## Архитектура

| Слой | Где |
|---|---|
| Сканер + детектор + дашборд | `Scripts/arb_server.py`, `Scripts/dashboard.html`, `Scripts/poly_ws.py` |
| Atomic execution engine | `Scripts/executor/` |
| Risk management (limits, kill switch, reconcile) | `Scripts/risk/` |
| Multi-bot wallet pool (6 ботов + auto-rebalance) | `Scripts/wallets/` |
| Paper trading + graduation gate | `Scripts/paper_trading.py` |
| Watchdog для kill switch | `Scripts/watchdog.py` |
| VPS deployment | `Dockerfile`, `docker-compose.yml`, `deploy/` |
| Тесты | `tests/` (87 unit-тестов) |

## Risk-параметры (по умолчанию)

| | |
|---|---|
| Max per trade | $55 |
| Daily loss limit | $35 (сброс в 00:00 UTC) |
| Hourly losing trades → пауза 1ч | 5 |
| Anti-detection | 1 нога арбитража = 1 кошелёк |
| Auto-rebalance USDC между ботами | $200 → $60 трешхолды |

## Документация

- **`idea.md`** — полная спецификация: архитектура, формулы арбитража, параметры всех фаз, deployment guide
- **`CLAUDE.md`** — инструкции для AI-ассистентов, работающих с репо (язык, процедуры, секреты)
- **`deploy/README.md`** — пошаговый VPS-деплой (AWS / DigitalOcean / Hetzner)
- **`.env.example`** — шаблон для `Credentials.env`

## Тесты

```bash
python -m unittest tests.test_executor tests.test_risk    # 43
python tests/test_wallets.py                              # 16
python tests/test_paper_trading.py                        # 11
python tests/test_sx_executor.py                          # 17
```

**Итого 87 тестов.** Все проходят.

## Workflow до live-торговли

1. Создать 6 hot + 1 cold кошельков (MetaMask, self-custodial)
2. Заполнить `BOT*_ETH_ADDRESS` + `COLD_WALLET_ADDRESS` в `Credentials.env`
3. Депозит USDC + MATIC на bot1 через Polygon-сеть
4. Approve Polymarket CLOB (одна on-chain транзакция)
5. Запустить радар в dry-run, накопить ≥100 paper trades
6. Если graduation gate ✅ — добавить `BOT*_PRIVATE_KEY` в `Credentials.env`, флипнуть `DRY_RUN=0`
7. Первые 10 сделок принудительно $5/нога (calibration), потом полный размер

## Лицензия

Приватный репозиторий. Все права защищены.
