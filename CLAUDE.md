# CLAUDE.md — память проекта plan-kapkan

> **После каждого `/compact` читать в этом порядке**: 1) [RULES.md](RULES.md) — правила оператора; 2) этот файл — проектный контекст; 3) [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — карта модулей; 4) последний `.claude/SESSION_SNAPSHOT_*.md` если есть.

## Проект

**Arbitrage Radar** на prediction-market площадках **Polymarket** + **Limitless** + **SX Bet** (Kalshi отключён с PR #177 — US-only). Архитектура:

- **Detection layer** — Python (`Scripts/arb_server.py` + новый пакет `Scripts/radar/`) + Flask + UI `dashboard.html`. Прод: https://kapkan.4frdm.live.
- **Execution layer** — TypeScript service `executor-ts/` (port 5051). EIP-712 signing, user-channel WS, real HTTP fires.
- **Wallet pool** — 6 ботов, anti-detection (1 нога арба = 1 кошелёк), auto-rebalance proposals.
- **DRY_RUN=1** по умолчанию. Real-mode после Phase 5 graduation gate (≥50 paper trades, win-rate ≥70%, drift ≤20%).

Спецификация: [idea.md](idea.md). Архитектура: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). История изменений: [CHANGELOG.md](CHANGELOG.md). Catalog багов: [BUG_CATALOG.md](BUG_CATALOG.md).

Репозиторий: https://github.com/AlexGolodkrd/plan-kapkan (приватный).

## Структура файлов уровня проекта

```
RULES.md                        — операторские правила (read first)
CLAUDE.md                       — этот файл (память агента)
README.md                       — quick start + платформенные ENV-ы
idea.md                         — изначальная спецификация (Phase 0)
CHANGELOG.md                    — PR-by-PR история (Index by PR / by Phase / by File)
BUG_CATALOG.md                  — каталог багов / phantom'ов / anti-pattern'ов

Scripts/
  arb_server.py                 — главный модуль radar (постепенно разносится в radar/)
  dashboard.html                — UI shell (CSS+JS в static/)
  static/dashboard.{css,js}     — выгруженные style/script
  config.py                     — RadarConfig (pydantic-settings) — единая точка env
  contracts.py                  — Pydantic FireRequest/LegEntry/FireResponse
  radar/                        — новый пакет (audit-28+)
    dedup.py                    — FireDedup (TTL дедуп fire'ов)
    api/                        — 8 Flask blueprints
    filters/                    — per-platform event filters
  executor/                     — fire orchestration, builders, presign, etc.
  risk/                         — limits, killswitch, reconcile, network_check
  wallets/                      — pool coordinator, stores, rebalance

executor-ts/                    — TypeScript executor service

docs/
  ARCHITECTURE.md               — карта модулей + target layout + 5-PR migration plan
  CREDENTIALS_GUIDE.md          — как настроить L2 creds + wallets
  OPERATOR_RUNBOOK.md           — daily ops, диагностика, env tuning
  ORDER_FLOW.md                 — описание order pipeline
  PUBLIC_AUDIT_ENDPOINT.md      — /api/recent_deals доступ
  DEPLOY_SETUP.md               — GitHub Actions auto-deploy setup
  TS_HISTORY.md                 — историческая запись миграции на TypeScript

deploy/
  README.md                     — primary deploy guide
  DEPLOY_PLAYBOOK.md            — checklist перед каждым деплоем
  ROLLBACK.md                   — процедура отката
  VERIFICATION.md               — post-deploy verify
  standby-setup.md              — hot standby

tests/                          — 88 pytest файлов
  conftest.py                   — autouse _reset_singletons (kill switch, CB, analytics, _fired_arb_keys, config)
```

## Что НИКОГДА не коммитить

Файлы из корневого `.gitignore`:
- `Credentials.env`, `*.env` — API-ключи + GITHUB_TOKEN
- `Executions/*.jsonl`, `Executions/*.log`, `Executions/*.json` — runtime data
- `__pycache__/`, `.venv/`, `dist/`, `node_modules/`
- `.claude/SESSION_SNAPSHOT_*.md` — локальные снапшоты
- `.claude/skills/` — локальные skill-файлы

Перед `git add -A` (но **лучше не использовать -A** — добавлять конкретные файлы):
```bash
git status --ignored --short | grep "^!!"
```
Проверяет что секреты остаются в ignored.

## Создание PR — короткая версия

Полная процедура → [RULES.md R6](RULES.md). Кратко:

1. Ветка ≠ `main`, ahead of `origin/main` хотя бы на 1 коммит.
2. Push:
   ```bash
   TOKEN=$(grep '^GITHUB_TOKEN=' Credentials.env | cut -d= -f2-)
   git -c credential.helper= push \
     "https://x-access-token:${TOKEN}@github.com/AlexGolodkrd/plan-kapkan.git" \
     <branch>
   ```
3. Проверка: `grep -c "x-access-token" .git/config` → `0`.
4. PR через REST API: `POST /repos/AlexGolodkrd/plan-kapkan/pulls`.
5. **Никогда не мержить автоматически.**

## Языковая политика

| Surface | Язык |
|---|---|
| Код, имена веток, commit subject, code-comments | English |
| Чат с оператором, commit body, PR description | Russian |
| `.md` в репо | English unless explicitly Russian-targeted |

Имена веток: `feature/<kebab>`, `fix/<kebab>`, `chore/<kebab>`, `refactor/<kebab>`.

## Git identity

`AlexGolodkrd <aleks.golodny@gmail.com>` (global config Windows).

## Поведенческие правила сессии

См. [RULES.md](RULES.md) — он канонический и обновляется оператором.

Основное:
- **R1**: Не пушить, не мержить, не деплоить без явного «да» оператора. Read-only можно.
- **R2**: Не предлагать PR, пока ВСЕ задачи из текущего запроса не закрыты. Полуработа запрещена.
- **R4**: GITHUB_TOKEN никогда в `.git/config`. Push через одноразовый URL.
- **R5**: Token rotation — оператор сам решает. Напоминать не чаще раза на 20 ответов.
