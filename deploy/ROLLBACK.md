# ROLLBACK Runbook — plan-kapkan

Что делать если деплой ушёл вкривь. **Сохрани закладку на эту страницу.**

VPS: `arb@77.91.97.22:/home/arb/plan-kapkan`
Dashboard: `https://kapkan.4frdm.live` (admin / Ts6RLPzIMQr2tKAMvNAN)

---

## 0. Когда делать rollback (триггеры)

| Симптом | Серьёзность | Действие |
|---|---|---|
| `/api/health` возвращает 500/503 более 2 минут | 🔴 кр | **немедленный rollback** |
| `paper_stats` count замёрз (не растёт) > 30 минут | 🔴 кр | rollback |
| Радар жрёт >80% CPU постоянно | 🟡 ср | проверь `docker stats`, потом rollback если не падает |
| `gunicorn` рестартится в цикле | 🔴 кр | rollback |
| Dashboard показывает "—" во всех табах | 🟡 ср | hard refresh + проверка `/api/deals` |
| Many `[CB:limitless] OPEN` в логах | 🟢 норма | **circuit breaker работает как задумано**, ждать auto-recovery 5min |

---

## 1. Снимок ПЕРЕД деплоем (обязательно)

В `deploy-pipeline` skill есть шаг 7. Вот команды:

```bash
ssh arb@77.91.97.22 << 'EOF'
cd /home/arb/plan-kapkan
TS=$(date +%s)
echo "Snapshot timestamp: $TS"
cp Credentials.env Credentials.env.bak.$TS
git rev-parse HEAD > .deploy_snapshot.$TS.git_head
docker ps --format "{{.Names}}: {{.Image}}: {{.Status}}" > .deploy_snapshot.$TS.docker
cp -r Executions Executions.bak.$TS
echo "Saved snapshot: TS=$TS"
echo "  git head: $(cat .deploy_snapshot.$TS.git_head)"
echo "  docker: $(cat .deploy_snapshot.$TS.docker)"
EOF
```

Запиши TS — он нужен для rollback'а.

---

## 2. Быстрый rollback (на git HEAD пред-деплоя)

```bash
ssh arb@77.91.97.22 << EOF
cd /home/arb/plan-kapkan
PREV_HEAD=\$(cat .deploy_snapshot.$TS.git_head)
echo "Rolling back to: \$PREV_HEAD"
git fetch origin
git checkout \$PREV_HEAD
cp Credentials.env.bak.$TS Credentials.env
docker compose down
docker compose up -d --build
sleep 15
docker logs plan-kapkan-radar --since=20s 2>&1 | grep -E 'Started|ERROR|Wallets|Poly' | head -10
EOF
```

После этого выполни smoke test (см. секцию 4).

---

## 3. Rollback на конкретный PR

Если знаешь что упало в **конкретном PR** (например PR #36 сломал что-то):

```bash
ssh arb@77.91.97.22 << 'EOF'
cd /home/arb/plan-kapkan
# Откат до PR #35 = последний хороший
git fetch origin
git checkout 6e31245   # ← коммит до плохого PR
docker compose down
docker compose up -d --build
sleep 15
EOF
```

Найти прошлый хороший commit: `git log --oneline | head -20` или [CHANGELOG.md](../CHANGELOG.md).

---

## 4. Smoke test (запустить ПОСЛЕ rollback или deploy)

**Файл:** [smoke_test.sh](smoke_test.sh)

```bash
bash deploy/smoke_test.sh
```

Что проверяет:
- ✅ `/api/health` 200
- ✅ `/api/wallets` count > 0
- ✅ `/api/deals` JSON valid
- ✅ `/api/analytics?period=day` работает
- ✅ `/api/paper_stats?window=100` отвечает
- ✅ `/api/circuit_breakers` (Phase 9kkk+) если доступен

Если **ЛЮБАЯ** проверка падает — rollback **немедленный**.

---

## 5. Recovery после aborted rollback

Если сам rollback упал (например `docker compose up` дал ошибку):

```bash
ssh arb@77.91.97.22 << 'EOF'
cd /home/arb/plan-kapkan
docker compose down -t 5  # force kill через 5s
docker rm -f plan-kapkan-radar plan-kapkan-watchdog 2>/dev/null
# Проверь disk space:
df -h
# Если /var/lib/docker полный:
docker system prune -af --volumes
# Снова поднять:
docker compose up -d --build
EOF
```

---

## 6. Восстановление потерянных данных

Если rollback стёр Executions/:

```bash
ssh arb@77.91.97.22 << EOF
cd /home/arb/plan-kapkan
ls -la Executions.bak.*  # выбери нужный TS
cp -r Executions.bak.$TS/* Executions/   # ← подставь TS
docker compose restart radar
EOF
```

**Phase 5 graduation gate** считается из `paper_results.jsonl`. Если он потерян — счётчик начнётся с 0. **Это самое опасное при rollback**.

Защита: не делай `docker compose down -v` (убивает volumes). Просто `down` без `-v` — данные на host filesystem остаются.

---

## 7. Когда НЕ ДЕЛАТЬ rollback

- 🟢 Circuit breaker открылся (это normal — ждать 5min)
- 🟢 Один deal с aborted_reason — это business logic, не deploy bug
- 🟢 Polymarket HTTP 502 на 1-2 минуты (Cloudflare flaky)
- 🟢 NEAR пуст 5-15 минут (нормальная мёртвая зона рынка)

---

## 8. После успешного rollback

1. Записать в [CHANGELOG.md](../CHANGELOG.md) под "Rollbacks":
   - Дата + причина + до какого коммита откатились
2. Создать issue / GitHub PR с фиксом (НЕ деплоить молча второй раз)
3. После фикса — повторно тест в локале + смоук на staging если есть
4. Только после успешного smoke test на VPS — снова прогнать deploy_pipeline

---

## 9. Контактный список (для эскалации)

- Оператор: AlexGolodkrd (через Telegram)
- VPS provider: 77.91.97.22 — узнай у оператора
- DNS: kapkan.4frdm.live — узнай у оператора

## Refs

- [skills/deploy-pipeline/SKILL.md](../.claude/skills/deploy-pipeline/SKILL.md)
- [skills/secrets-management/SKILL.md](../.claude/skills/secrets-management/SKILL.md)
- [smoke_test.sh](smoke_test.sh)
- [VERIFICATION.md](VERIFICATION.md)
