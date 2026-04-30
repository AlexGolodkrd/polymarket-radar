# DEPLOY PLAYBOOK — Phase 9kkk

Когда оператор скажет "можно деплоить", выполнять **строго по этому списку**.

VPS: `arb@77.91.97.22:/home/arb/plan-kapkan`
Branch: `feature/phase-9kkk-cf-resilience-parallel-fetch`

---

## A. Pre-deploy локально (5 мин)

```bash
# 0. На локальной машине, в плане-капкан
git status                                    # должен быть clean (uncommitted в .tmp_*)
git branch --show-current                     # feature/phase-9kkk-...

# 1. Ещё раз syntax-check
python .tmp_syntax_check.py                   # 4 файла OK
python Scripts/lint_dashboard_js.py           # JS parse OK

# 2. Pytest локально, если есть базовые тесты
python -m unittest discover tests             # 355/355 (или новые failures смотреть)

# 3. Проверь diff что в этом deploy идёт
git log origin/main..HEAD --oneline           # список коммитов
git diff --stat origin/main..HEAD             # затронутые файлы
```

## B. Создание PR (5 мин)

Следуй процедуре из `CLAUDE.md` — секция "Порядок действий: создание Pull Request":

```bash
# Получить токен
TOKEN=$(grep '^GITHUB_TOKEN=' Credentials.env | cut -d= -f2-)

# Push на feature branch (БЕЗ -u)
git -c credential.helper= push \
  https://x-access-token:$TOKEN@github.com/AlexGolodkrd/plan-kapkan.git \
  feature/phase-9kkk-cf-resilience-parallel-fetch:feature/phase-9kkk-cf-resilience-parallel-fetch

# Проверка чистоты .git/config
grep -c "x-access-token" .git/config          # должно быть 0
git fetch origin
git branch --set-upstream-to=origin/feature/phase-9kkk-cf-resilience-parallel-fetch

# Создать PR через REST API
curl -s -X POST \
  -H "Authorization: token $TOKEN" \
  -H "Accept: application/vnd.github+json" \
  https://api.github.com/repos/AlexGolodkrd/plan-kapkan/pulls \
  -d @.tmp_pr_body.json
```

PR title: `Phase 9kkk: CF resilience + parallel Limitless fetch + Other-filter fix + 6 operator wins`

## C. Snapshot ПЕРЕД deploy на VPS (1 мин)

```bash
ssh arb@77.91.97.22 "cd /home/arb/plan-kapkan; \
TS=\$(date +%s); echo \"TS=\$TS\"; \
cp Credentials.env Credentials.env.bak.\$TS; \
git rev-parse HEAD > .deploy_snapshot.\$TS.git_head; \
cp -r Executions Executions.bak.\$TS; \
echo 'Snapshot saved'"
```

**Запиши TS** — нужен для rollback.

## D. Pull + restart на VPS (2 мин)

```bash
ssh arb@77.91.97.22 << 'EOF'
cd /home/arb/plan-kapkan

# 1. Pull merged main (если PR уже merged)
git fetch origin
git checkout main
git pull origin main

# 2. Sanity: смотрим какие env vars нужно поставить
# Phase 9kkk новые env (в дополнение к существующим):
echo "=== ENV that should be set ==="
echo "ASYNC_FETCH=1                # parallel fetcher активирован"
echo "ENABLE_LIMITLESS=1           # после этого deploy"
echo "ENABLE_SX=1                  # после этого deploy (опционально)"
echo "ARB_ALERT_MIN_NET_USD=10     # дефолт уже $10, можно не ставить"
echo "POLY_MAIN_PAGES=6            # как просил оператор (текущее = 4)"

# 3. Поправить Credentials.env: сменить только нужные значения
nano Credentials.env
# Менять:
#   ASYNC_FETCH=1
#   ENABLE_LIMITLESS=1
#   POLY_MAIN_PAGES=6
# (ENABLE_SX=1 — опционально, можно оставить 0 на этот deploy и включить отдельно)

# 4. Rebuild + up
docker compose down
docker compose up -d --build

# 5. Wait + watch logs
sleep 20
docker logs plan-kapkan-radar --since=30s 2>&1 | grep -E 'Started|ERROR|Wallets|Poly|Limitless|gunicorn|Phase' | head -30
EOF
```

## E. Post-deploy smoke test (1 мин)

```bash
# С локальной машины:
bash deploy/smoke_test.sh

# Ожидаем: ALL CHECKS PASSED ✅
```

Если хоть одна проверка упала → **rollback** через `deploy/ROLLBACK.md`.

## F. Manual verification на дашборде (5 мин)

1. Открой https://kapkan.4frdm.live (Ctrl+Shift+R для hard reload)
2. Проверь:
   - **Header**: `Lim:` показывает не `off` а `subs/N`
   - **Tab Deals**: загрузка без ошибок (deals может быть 0 — это норма)
   - **Tab NEAR**: рядом с title есть **📋 кнопки copy** (Phase 9kkk UI fix)
   - **Tab Analytics**: `period=day` показывает корректные числа
   - **3 фантомных события (West Virginia, NE-02, Nebraska Republican)** должны быть в **Карантин**, не NEAR (Phase 9kkk Other-filter fix)

3. Проверь Telegram: должно прийти `✅ Radar started` (если notify настроен)

## G. Watch first 30 min

```bash
ssh arb@77.91.97.22 "docker logs plan-kapkan-radar --since=30m -f 2>&1 | grep -E '\[CB:|\[HTTP:|\[LIM\] parallel|fetch_limitless_pages|ERROR|Telegram'"
```

Что должно происходить:
- **`[LIM] parallel fetch done: N events in X.Ys`** — parallel fetcher работает (X должно быть 2-5s)
- **`[fetch_limitless_pages] N events from K/40 pages in Xs`** — параллельный fetcher logs
- **Никаких** `[CB:limitless] OPEN` (если есть — значит CF block, breaker сработал — это OK, не паника)
- **Никаких** `wallet_assignment_failed` для арбов до 6 ног (с 3 can_sign + dry_run mock pad)
- При появлении арба >$10 net → Telegram alert `🎯 Arb >$10 ...`

## H. Rollback ready

Если что-то ломается, rollback в **2 минуты** через `deploy/ROLLBACK.md`:

```bash
# Предположим TS=1714501234, сохранённый в шаге C
ssh arb@77.91.97.22 << EOF
cd /home/arb/plan-kapkan
PREV_HEAD=\$(cat .deploy_snapshot.1714501234.git_head)
git checkout \$PREV_HEAD
cp Credentials.env.bak.1714501234 Credentials.env
docker compose down && docker compose up -d --build
EOF
```

## I. После 24 часов uptime

- Прочитай `/api/circuit_breakers` — все CB должны быть `closed` (если был OPEN — посмотри `consecutive_failures`)
- Прочитай `/api/paper_stats?window=100` — `count` должен расти быстрее (3 wallets + dry-run mock pad → 4+ ноги тоже считаются)
- Лог Limitless scan time через `docker logs ... | grep 'parallel fetch done'` — должно быть стабильно 2-5s, не дрифтить

## J. Что есть в этом deploy (краткая сводка)

| # | Изменение | Файлы |
|---|---|---|
| 1 | Other-outcome filter fix (3 фантомных события исчезнут из NEAR) | `arb_server.py` |
| 2 | Parallel Limitless fetch (65s → 3-5s) | `async_fetchers.py`, `arb_server.py` |
| 3 | Circuit breaker (Cloudflare resilience, auto-recovery) | `circuit_breaker.py` (новый) |
| 4 | Universal HTTP code handler | `http_codes.py` (новый) |
| 5 | `_resolve_lim_end_date` в `eval_limitless` | `arb_server.py` |
| 6 | SX Bet status filter (drop closed/resolved) | `arb_server.py:eval_sx` |
| 7 | search_query copy button в NEAR UI | `dashboard.html` |
| 8 | Telegram alerts на арбы >$10 | `notify.py`, `arb_server.py` |
| 9 | dryrun.jsonl auto-fire fix (mock pad для dry-run) | `executor/atomic.py` |
| 10 | 8 новых SKILL.md в `.claude/skills/` | (не в репо, локально) |
| 11 | `CHANGELOG.md` | новый |
| 12 | `deploy/ROLLBACK.md` + `deploy/smoke_test.sh` + `deploy/DEPLOY_PLAYBOOK.md` | новые |

## Refs

- [ROLLBACK.md](ROLLBACK.md) — что делать если что-то сломалось
- [smoke_test.sh](smoke_test.sh) — pass/fail проверка после deploy
- [VERIFICATION.md](VERIFICATION.md) — read-only baseline tests (Phase 5)
- `.claude/skills/deploy-pipeline/SKILL.md` — общий чек-лист
