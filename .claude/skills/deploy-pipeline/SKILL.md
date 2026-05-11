---
name: deploy-pipeline
description: |
  Pre-deploy + post-deploy checklist for plan-kapkan on VPS 77.91.97.22.
  Use this SKILL whenever you're about to push code that affects production
  (radar container, dashboard.html, executor, risk module). The goal is zero
  paper-trading disruption + clean rollback path.
---

# deploy-pipeline — чек-лист перед каждым деплоем

## Когда применять

- Перед `docker compose up -d` на VPS
- Перед merge PR в `main`
- Перед изменением `Credentials.env` на VPS
- При работе со скриптами в `Scripts/` (особенно `arb_server.py`, `executor/`, `risk.py`)

## ❌ Чего НИКОГДА не делать

1. `docker compose down` без снимка состояния `Executions/` (потеря paper trading данных)
2. `git push -u` (Git Credential Manager записывает PAT в `.git/config` — утечка)
3. Push с изменениями в `Credentials.env` (даже если ".env" в `.gitignore`, проверяй каждый раз)
4. `git add -A` без `git status --ignored --short | grep "^!!"` (сикреты могут быть untracked но не ignored)
5. `--no-verify` на коммитах (хуки чтобы что-то проверить, не для обхода)
6. Деплой в момент когда `paper_results.jsonl` накапливает graduation gate данные (5+ сделок за последний час)
7. Изменение `MAX_WS_SUBS`, `POLY_MAIN_PAGES` без замера CPU/memory baseline до и после

## Pre-deploy checklist (обязательный)

```bash
# 1. Working dir clean? Никаких uncommitted changes на main
git status --short
[ -z "$(git status --short)" ] || { echo "ABORT: uncommitted changes"; exit 1; }

# 2. Branch правильный? НЕ main
BRANCH=$(git branch --show-current)
[ "$BRANCH" != "main" ] || { echo "ABORT: на main"; exit 1; }

# 3. Тесты прошли локально
pytest tests/ -x --tb=short
[ $? -eq 0 ] || { echo "ABORT: тесты упали"; exit 1; }

# 4. Lint JS (catch syntax errors before browser cache breaks)
node -e "new Function(require('fs').readFileSync('Scripts/dashboard.html', 'utf8').match(/<script>([\\s\\S]*?)<\\/script>/g).map(s=>s.replace(/<\\/?script>/g, '')).join('\\n'))"
[ $? -eq 0 ] || { echo "ABORT: JS syntax error"; exit 1; }

# 5. Сикретов в diff нет
git diff main..HEAD | grep -iE "(BOT[0-9]+_PRIVATE_KEY|SECRET|TOKEN|API_KEY)=[a-zA-Z0-9]{16,}" && \
  { echo "ABORT: возможна утечка ключа"; exit 1; }

# 6. Graduation gate не на пике сбора (>40 paper trades) — лучше не прерывать
PAPER_COUNT=$(curl -s -u admin:Ts6RLPzIMQr2tKAMvNAN https://kapkan.4frdm.live/api/paper_stats?window=100 | python3 -c "import sys, json; print(json.load(sys.stdin).get('count', 0))")
[ "$PAPER_COUNT" -lt 40 ] || { echo "WARN: paper_stats=$PAPER_COUNT — критично близко к graduation, спросить оператора"; }

# 7. Снимок текущего состояния (для rollback)
TS=$(date +%s)
ssh arb@77.91.97.22 "
  cd /home/arb/plan-kapkan
  cp Credentials.env Credentials.env.bak.$TS
  git rev-parse HEAD > Credentials.env.bak.$TS.git_head
  cp -r Executions Executions.bak.$TS
"
echo "Snapshot: $TS"
```

## Deploy itself

```bash
# 1. Push на feature branch
git -c credential.helper= push https://x-access-token:$TOKEN@github.com/AlexGolodkrd/plan-kapkan.git $BRANCH:$BRANCH

# 2. Удостоверься что .git/config чистый
grep -c "x-access-token" .git/config
# Должно быть 0

# 3. Создай PR (см. CLAUDE.md для шаблона)

# 4. ТОЛЬКО ПОСЛЕ APPROVE/MERGE — pull на VPS:
ssh arb@77.91.97.22 "
  cd /home/arb/plan-kapkan
  git pull
  docker compose down
  docker compose up -d --build
  sleep 15
  docker logs plan-kapkan-radar --since=20s 2>&1 | grep -E 'Started|ERROR|Wallets|Poly' | head -20
"
```

## Post-deploy smoke test (ОБЯЗАТЕЛЬНО)

```bash
# 30 секунд после restart, проверить что всё живо:

curl -s -u admin:$BASIC_AUTH https://kapkan.4frdm.live/api/wallets | python3 -m json.tool | grep "count"
# Ожидаем: > 0

curl -s -u admin:$BASIC_AUTH https://kapkan.4frdm.live/api/deals | python3 -c "import sys, json; d=json.load(sys.stdin); print('OK' if 'deals' in d else 'FAIL')"
# Ожидаем: OK

curl -s -u admin:$BASIC_AUTH https://kapkan.4frdm.live/api/analytics?period=day | python3 -c "import sys, json; print(json.load(sys.stdin).get('closed_count'))"
# Ожидаем: число (даже 0 — ОК, главное endpoint живой)

curl -s -u admin:$BASIC_AUTH https://kapkan.4frdm.live/api/paper_stats?window=100
# Ожидаем: JSON, не 500
```

Если **любой** из 4 проверок упал → **немедленный rollback** (см. rollback runbook).

## Rollback procedure

```bash
ssh arb@77.91.97.22 "
  cd /home/arb/plan-kapkan
  PREV_HEAD=\$(cat Credentials.env.bak.\$TS.git_head)
  git checkout \$PREV_HEAD
  cp Credentials.env.bak.\$TS Credentials.env
  docker compose down
  docker compose up -d --build
"
```

## Что отслеживать после deploy

В первые 30 минут смотреть на:
- `/api/stats` — `cycle_seconds_avg` не должен подскочить >2x от baseline
- `/api/deals` — `near_count` должен быть стабильным (если упал в 0 — что-то сломалось в фильтре)
- `docker logs plan-kapkan-radar --since=30m | grep -i error | wc -l` — должно быть < 5
- `/api/paper_stats?window=100` — `count` должен расти если есть deals

## Известные грабли (исторические)

| Симптом | Причина | Решение |
|---|---|---|
| После deploy браузер показывает старый JS | Cache-Control headers отсутствуют | Hard reload (Ctrl+Shift+R) или cache-busting querystring |
| Pre-commit hook падает на `node -e` парсере | JS syntax error в dashboard.html | Lint локально перед commit (см. шаг 4) |
| `git push -u` создаёт вечную утечку PAT | Git Credential Manager на Windows | Всегда `-c credential.helper=` |
| Кошелёк не can_sign после restart | BOT*_PRIVATE_KEY с `#` или `0x` | Проверь raw bytes без префикса, без комментов |
| `403` от Limitless после увеличения POLY_MAIN_PAGES | Не связано с Polymarket — Limitless rate-limit на >40 concurrent | Включи HTTP/2 multiplexing (см. http-rate-limiting skill) |

## Refs

- `CLAUDE.md` — проект-уровневая шпаргалка
- `http-rate-limiting/SKILL.md` — для rate-limit инцидентов
- `circuit-breaker-patterns/SKILL.md` — для graceful degradation
- `secrets-management/SKILL.md` — для приватных ключей
- `deploy/ROLLBACK.md` (если есть) — детальный runbook
