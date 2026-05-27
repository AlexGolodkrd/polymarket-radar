# RULES.md — operator's rules for Claude agent

> **READ THIS FIRST after every `/compact`** and before starting any new task. These are operator-set rules that override default agent behaviour. They are project-specific to plan-kapkan but the spirit applies session-wide.
>
> Last updated: 2026-05-27 by operator (AlexGolodkrd).

---

## R1 — Permission gates

**Я (агент) выполняю деплой, мерж и работу с сервером — это часть моей работы**, НЕ задача оператора. Оператор не должен запускать SSH / `git push` / `gh pr merge` / `docker restart` сам. **Но перед каждым таким действием я спрашиваю «да/нет» через `AskUserQuestion`**, не выполняю на автомате.

Принципы:
- Read-only действия — делаю без спроса.
- State-mutating действия (push / merge / deploy / server) — **сам, после явного «да» оператора**.
- Если оператор сказал «делай всё сам» в текущем запросе — это покрывает все state-mutating действия в этом запросе, спрашивать каждый раз не надо. Но повторно — только перед явно-опасным шагом (force-push, merge to main, prod restart).

### Что я делаю сам (с явным «да» оператора)

**GitHub**:
- `git push` (включая feature-ветки)
- `git merge` (через REST PATCH / gh pr merge)
- Force-push, branch delete, rebase shared branches
- Создание новых веток на `origin`
- Закрытие PR

**Production VPS (`77.91.97.22` / `kapkan.4frdm.live`)**:
- SSH session, `docker exec`, container restarts
- `unkill` / `kill` / любые killswitch мутации
- Правки `Credentials.env` на VPS, env updates, configs
- Триггер `.github/workflows/deploy.yml` или любого workflow
- Всё, что влияет на текущий paper-trading state или живые контейнеры

### Что делаю без спроса (read-only)

- `git status`, `git log`, `git diff`, локальные коммиты
- `curl GET` на production endpoints (read-only probes)
- Локальные тесты (`pytest`)
- Чтение любых файлов
- Обновление PR body через REST PATCH на PR, который я только что открыл (title/body bump — часть финализации задачи)

### Как спрашивать

`AskUserQuestion` с конкретными вариантами, не free-form chat. Оператор хочет быстрое yes/no, не переписку.

### Один-раз-делегирование

Если оператор пишет фразы вроде «делай всё сам», «приступай», «merge it», «deploy» — это **широкая делегация** на текущий запрос. Не переспрашивать каждое действие в его рамках. Спросить дополнительно только если:
- Действие выходит за scope текущего запроса.
- Действие необратимое (force-push на main, delete branch с unmerged commits, prod-data wipe).
- Появилась новая угроза (red regression в тестах, прод-метрика упала).

---

## R2 — No half-work, no premature PR

> Until ALL items in the operator's current request are done, **do not propose a PR, do not push, do not announce "ready"**. Keep working until the request is fully closed.

Implications:
- If the request has 5 items, complete items 1-5 before any final commit / push / PR step.
- Clarification questions are allowed (use `AskUserQuestion`).
- Scope is allowed (operator may set scope per item).
- Stopping mid-stream and saying "what's next?" is forbidden when the request was already concrete.

When in doubt about scope, **ask once at the start**, not after partial work.

---

## R3 — Language policy

| Surface | Language |
|---|---|
| Code identifiers, branches, commit subject | English |
| Operator chat | Russian |
| Commit body | Russian |
| PR title | English-leading, can mix |
| PR description | Russian |
| Code comments / docstrings | English (project standard) |
| `.md` files in repo | English unless explicitly Russian-targeted |

Branch naming: `feature/<kebab>`, `fix/<kebab>`, `chore/<kebab>`, `refactor/<kebab>`. Lowercase, hyphens.

---

## R4 — Secrets handling

- `Credentials.env` is **gitignored**. Never `git add` it.
- `GITHUB_TOKEN` stored under `GITHUB_TOKEN=` line in `Credentials.env`. **Never write it into `.git/config`**.
- Push pattern that does NOT leak token:
  ```bash
  TOKEN=$(grep '^GITHUB_TOKEN=' Credentials.env | cut -d= -f2-)
  git -c credential.helper= push \
    "https://x-access-token:${TOKEN}@github.com/AlexGolodkrd/plan-kapkan.git" \
    <branch>
  ```
- After push, verify: `grep -c "x-access-token" .git/config` must return `0`.
- Before `git add -A`, check `git status --ignored --short | grep "^!!"` to confirm secrets are still in ignored.
- Plain-text token backups (`Credentials.env.bak*`) are forbidden — delete on sight.

---

## R5 — Token rotation

- Operator decides when to rotate `GITHUB_TOKEN`. Don't nag.
- May remind about rotation **at most once per 20 operator messages**.
- If a `git push` returns 401 / "Bad credentials" — say so directly, explain refresh procedure (see `RULES.md` R4), and stop.

---

## R6 — PR procedure

PR creation lives in operator's hands until R1 changes. When operator asks me to open a PR:

1. Confirm branch is ahead of `origin/main`: `git log origin/main..HEAD --oneline` must be non-empty.
2. Push using the R4 no-leak pattern. **Ask permission before this step.**
3. Open via REST API:
   ```bash
   curl -s -X POST \
     -H "Authorization: Bearer $TOKEN" \
     -H "Accept: application/vnd.github+json" \
     https://api.github.com/repos/AlexGolodkrd/plan-kapkan/pulls \
     -d '{"title": "...", "head": "...", "base": "main", "body": "...", "draft": false}'
   ```
4. PR body in Russian, template:
   ```markdown
   ## Что изменено
   <1-3 sentences>

   ## Затронутые файлы
   - `path/to/file` — <что поменялось>

   ## Как проверить
   1. <step>
   2. <step>
   3. <success criterion>

   ## Тесты
   <запущенные тесты + результат, либо "тестов нет — ручная проверка">
   ```
5. **Never merge**. Only create. Merge — separate operator command.
6. Return: `html_url`, summary, state.

---

## R7 — Test hygiene

- Targeted suite must be green before commit.
- Wider regression run — pre-existing fails OK, **new fails are NOT OK**. Always compare against baseline via `git stash`.
- State pollution between tests is a real risk — see `tests/conftest.py::_reset_singletons`. Don't shortcut it.
- Don't use `del sys.modules['arb_server']` reload pattern in new tests — it breaks downstream test ordering. Use `monkeypatch.setenv('EXECUTIONS_DIR', ...)` + `importlib.reload(analytics)` instead.

---

## R8 — Architectural direction

- `Scripts/arb_server.py` is being dismantled into `Scripts/radar/` package. See `docs/ARCHITECTURE.md` for the map + migration plan (audit-28a → e).
- `scan_loop` body extraction is **HIGH RISK** without staging. Don't attempt without operator's explicit "staging is ready" signal.
- All new env vars go via `Scripts/config.py::RadarConfig` (pydantic-settings). No more `os.environ.get(...)` scattered across files.
- Python↔TS wire format: `Scripts/contracts.py::FireRequest`/`LegEntry`/`FireResponse`. Mirror in `executor-ts/src/types/deal.ts`.

---

## R9 — Skill files

The following live in `.claude/skills/` (gitignored, local to each operator's machine):

- `polymarket-fee-schedule` — fee model verification after 31.03.2026 migration
- `time-freshness-validation` — TTL gates on external feeds
- `circuit-breaker-patterns` — 3-state CB recovery
- `eip712-typescript-parity` — Python↔TS signing parity
- `ws-listener-lifecycle` — WebSocket reconnect/teardown
- `fillregistry-pattern` — fill confirmation tracking
- `vitest-mocks` — TS test patterns

When relevant, invoke them. Don't reinvent.

---

## R10 — Memory files

- `CLAUDE.md` — project memory (commit-able, in repo)
- `RULES.md` — this file (commit-able, in repo)
- `~/.claude/projects/<project>/memory/MEMORY.md` — user memory (NOT in repo)
- `.claude/SESSION_SNAPSHOT_*.md` — operator may keep these locally (gitignored)

After `/compact`, the order to read is:
1. **RULES.md** — what's allowed and what isn't
2. `CLAUDE.md` — project context
3. `docs/ARCHITECTURE.md` — module map
4. Latest `.claude/SESSION_SNAPSHOT_*.md` if present

---

## R11 — Communication style

- Status updates: ≤200 words. Tables and bullet points beat prose.
- Reports: each section labeled, concrete numbers, no marketing language.
- "Done" claims: cite the test count and the file paths.
- Mistakes: admit immediately, explain root cause, fix. Don't hide behind weasel words.

---

## R12 — Branch hygiene

Operator runs many sessions in parallel (via Claude Code worktrees). Don't:
- Pollute `feat/positions-open-resolved-real-pnl` with unrelated commits unless explicitly told the PR is the catch-all.
- Create `chore/audit-28X` branches off `main` without operator's nod — they pile up.
- Delete remote branches without operator's "delete".

After merge, operator may want me to clean local branches. Ask first.

---

## Change log of this file

- **2026-05-27** — initial creation per operator's R1+R2+R3 request after audit-28b cont.
- **2026-05-27 (revision)** — R1 переписан: ЯВНО указано, что агент сам выполняет push/merge/deploy/server actions (это не задача оператора), но перед каждым state-mutating действием спрашивает «да/нет». Добавлен раздел «Один-раз-делегирование» — широкие фразы оператора («делай всё сам», «merge it») покрывают все действия в текущем запросе без переспроса.
