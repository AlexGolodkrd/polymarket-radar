# CLAUDE.md — память проекта plan-kapkan

## Проект
Два радара:
- **Arbitrage Radar** (`Scripts/arb_server.py` + `Scripts/dashboard.html`) — Flask-сканер арбитражных окон на Polymarket / Kalshi / SX Bet, дашборд на `localhost:5050`. Спецификация: [idea.md](idea.md).
- **InsiderRadar** (`insider-radar/`) — React+Vite SPA для отслеживания крупных опционных сделок ("умные деньги"). Читает `public/trades_data.json`, обновляется каждые 5с.

Репозиторий: https://github.com/AlexGolodkrd/plan-kapkan (приватный).

## Что НИКОГДА не коммитить
Файлы из корневого `.gitignore`:
- `Credentials.env`, `*.env` — API-ключи (OpenAI, Apify и т.п.)
- `insider-radar/node_modules/`
- `Executions/*.jsonl`, `Executions/*.log` — логи цен/арбитражей
- `__pycache__/`, `.venv/`, `dist/`

Перед `git add -A` всегда проверяй `git status --ignored --short | grep "^!!"` чтобы убедиться, что секреты в ignored.

## Порядок действий: создание Pull Request

**Обязательные предусловия:**
1. Работаем НЕ на `main`. Если на `main` и есть изменения → сначала `git switch -c feature/<short-name>`, потом коммит.
2. Локальная ветка должна иметь хотя бы один коммит, которого нет в `origin/main`. Проверка: `git log origin/main..HEAD --oneline` — должно быть непусто.
3. Ветка запушена с upstream: `git push -u origin <branch>`.

**Авторизация для GitHub API:**
- В этой среде нет `gh` CLI. Используем GitHub REST API через `curl` с PAT.
- **PAT хранится в `Credentials.env`** под ключом `GITHUB_TOKEN`. Файл в `.gitignore`, в репо не уходит.
- Чтение в bash: `TOKEN=$(grep '^GITHUB_TOKEN=' Credentials.env | cut -d= -f2-)`.
- В `.git/config` токен **не записывать**. Push через `https://x-access-token:$TOKEN@github.com/...` одноразово, либо через `-c http.extraHeader="Authorization: Bearer $TOKEN"`.
- На Windows Git Credential Manager может перехватывать `extraHeader` — в этом случае использовать URL-форму с `-c credential.helper=` чтобы отключить GCM на одну команду.

**Шаги создания PR:**

1. Собрать данные для описания:
   - `git log origin/main..HEAD --oneline` — список коммитов
   - `git diff --stat origin/main..HEAD` — затронутые файлы
   - `git diff origin/main..HEAD` — содержимое (для понимания "что изменено")

2. Сформировать тело PR по шаблону (см. ниже).

3. Создать PR через REST API:
   ```bash
   curl -s -X POST \
     -H "Authorization: token <PAT>" \
     -H "Accept: application/vnd.github+json" \
     https://api.github.com/repos/AlexGolodkrd/plan-kapkan/pulls \
     -d '{
       "title": "<title>",
       "head": "<branch>",
       "base": "main",
       "body": "<body>",
       "draft": false
     }'
   ```
   Из ответа взять `html_url` — это ссылка на PR.

4. **НЕ мержить.** Только создать. Мерж — отдельная команда пользователя.

5. Вернуть пользователю: ссылку `html_url`, краткое summary, статус (open / draft).

**Шаблон описания PR (русский, как в проекте):**

```markdown
## Что изменено
<1-3 предложения о сути изменений и зачем>

## Затронутые файлы
- `path/to/file1` — <что в нём поменялось>
- `path/to/file2` — <что в нём поменялось>

## Как проверить
1. <шаг 1: команда / переход на URL>
2. <шаг 2: что должно произойти>
3. <шаг 3: критерий успеха>

## Тесты
- <запущенные тесты и их результат, либо "тестов нет — проверка ручная">
```

## Языковая политика
- Код, имена веток, commit subject — английский.
- Описания PR, commit body, общение — русский.
- Имена веток: `feature/<kebab>`, `fix/<kebab>`, `chore/<kebab>`.

## Git identity
`AlexGolodkrd <aleks.golodny@gmail.com>` (глобальный конфиг Windows).
