# Auto-deploy setup (GitHub Actions → VPS)

Один раз настраивается за ~10 минут. После — каждый merge в `main` автоматически деплоит на VPS за ~30 секунд.

## 0. Когда это **не** нужно

- Если ты предпочитаешь ручной контроль (например хочешь читать diff до деплоя). Текущий ручной flow:
  ```bash
  cd ~/plan-kapkan && git fetch origin && git checkout main && git pull && docker restart plan-kapkan-radar
  ```
  отлично работает и не требует никаких secrets.

- Если параноидально не хочешь хранить SSH-ключ в GitHub Secrets. Альтернатива — self-hosted runner на самом VPS (см. §6 ниже).

## 1. Создать SSH ключ для деплоя (отдельный от твоего личного)

На локальной машине:

```bash
ssh-keygen -t ed25519 -C "github-actions-deploy" -f ~/.ssh/plan_kapkan_deploy_key -N ""
```

Создаст два файла:
- `~/.ssh/plan_kapkan_deploy_key` — private key, его положим в GitHub Secret
- `~/.ssh/plan_kapkan_deploy_key.pub` — public key, его положим на VPS

## 2. Положить public key на VPS

```bash
ssh arb@77.91.97.22
mkdir -p ~/.ssh && chmod 700 ~/.ssh
# Затем вставить содержимое plan_kapkan_deploy_key.pub в:
echo 'ssh-ed25519 AAA... github-actions-deploy' >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
exit
```

Проверка с локальной машины:
```bash
ssh -i ~/.ssh/plan_kapkan_deploy_key arb@77.91.97.22 "echo OK"
# Должно вывести: OK
```

## 3. Добавить secrets в GitHub

`https://github.com/AlexGolodkrd/plan-kapkan/settings/secrets/actions` → **New repository secret** для каждого:

| Name | Value | Notes |
|---|---|---|
| `VPS_HOST` | `77.91.97.22` | IP или DNS |
| `VPS_USER` | `arb` | юзер на VPS |
| `VPS_SSH_KEY` | содержимое `~/.ssh/plan_kapkan_deploy_key` | **полный** private key включая `-----BEGIN/END-----` |
| `VPS_PORT` | `22` | необязательно, если порт нестандартный |
| `VPS_REPO_DIR` | `/home/arb/plan-kapkan` | необязательно, default `~/plan-kapkan` |

Опционально для уведомлений о падении деплоя:

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | твой бот-токен (тот же что для radar alerts) |
| `TELEGRAM_CHAT_ID` | твой chat_id |

## 4. Положить workflow в репо

Создать файл `.github/workflows/deploy.yml` с содержимым ниже. Можно просто скопировать из этого блока:

```yaml
name: Deploy radar to VPS

on:
  push:
    branches: [main]
    paths-ignore:
      - 'docs/**'
      - '*.md'
      - '.gitignore'
      - 'insider-radar/**'
  workflow_dispatch:
    inputs:
      reason:
        description: 'Why are you running this manually?'
        required: false
        default: 'manual deploy'

concurrency:
  group: deploy-vps
  cancel-in-progress: false

jobs:
  deploy:
    name: SSH to VPS, pull main, restart radar
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - name: Sanity-check secrets
        run: |
          for s in VPS_HOST VPS_USER VPS_SSH_KEY; do
            if [ -z "${!s}" ]; then
              echo "::error::Missing secret $s — see docs/DEPLOY_SETUP.md"
              exit 1
            fi
          done
        env:
          VPS_HOST: ${{ secrets.VPS_HOST }}
          VPS_USER: ${{ secrets.VPS_USER }}
          VPS_SSH_KEY: ${{ secrets.VPS_SSH_KEY }}

      - name: Deploy via SSH
        uses: appleboy/ssh-action@v1.2.0
        with:
          host: ${{ secrets.VPS_HOST }}
          username: ${{ secrets.VPS_USER }}
          key: ${{ secrets.VPS_SSH_KEY }}
          port: ${{ secrets.VPS_PORT || 22 }}
          command_timeout: 4m
          script_stop: true
          script: |
            set -euo pipefail
            REPO_DIR="${{ secrets.VPS_REPO_DIR || '~/plan-kapkan' }}"
            cd "$REPO_DIR"
            echo "== current commit =="
            git rev-parse --short HEAD
            git fetch origin --quiet
            git checkout main
            git pull --ff-only origin main
            NEW_SHA=$(git rev-parse --short HEAD)
            echo "== new commit: $NEW_SHA =="
            docker restart plan-kapkan-radar
            sleep 3
            docker ps --filter name=plan-kapkan-radar --format 'table {{.Names}}\t{{.Status}}'
            for i in 1 2 3 4 5 6; do
              if curl -sf -m 3 http://localhost:5050/api/risk_status >/dev/null; then
                echo "✓ /api/risk_status responding (attempt $i)"
                break
              fi
              echo "  waiting for radar... (attempt $i/6)"
              sleep 5
              if [ "$i" = "6" ]; then
                echo "::error::Radar did not respond after 30s"
                docker logs --tail 50 plan-kapkan-radar
                exit 1
              fi
            done
            echo "== deploy success: $NEW_SHA =="

      - name: Notify Telegram on failure
        if: failure() && env.TELEGRAM_BOT_TOKEN != ''
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
        run: |
          curl -s -X POST \
            "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            -d "chat_id=${TELEGRAM_CHAT_ID}" \
            -d "text=⚠ VPS deploy FAILED on ${{ github.sha }} — see Actions log: ${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}"
```

После добавления — закоммить:
```bash
git add .github/workflows/deploy.yml
git commit -m "chore: add auto-deploy workflow"
git push
```

## 5. Проверка

1. Открыть `https://github.com/AlexGolodkrd/plan-kapkan/actions` — workflow появится.
2. Сделать любой trivial PR в main (например пометить что-то в `docs/`) и смерджить.
3. Через ~30s в Actions tab появится зелёная галочка → радар уже на новом коммите.

Если хочешь triggered run вручную (без push) — Actions → «Deploy radar to VPS» → «Run workflow» → выбрать main → Run.

## 6. Альтернатива: self-hosted runner на VPS

Если паранойишь хранить private SSH key в GitHub Secrets — можно поставить GitHub Actions runner прямо на VPS. Тогда workflow будет выполнять `git pull && docker restart` локально, без внешнего SSH.

Минусы:
- Ещё один процесс на VPS, ~50MB RAM
- Если runner упадёт — деплои встанут до его рестарта

Плюсы:
- Никаких credentials в GitHub
- Можно ограничить runner правами что он не лазит за пределы `/home/arb/plan-kapkan`

Setup инструкция: GitHub → Settings → Actions → Runners → New self-hosted runner → Linux. Процедура занимает 5 минут, GitHub генерирует команды, копируешь, выполняешь на VPS.

## 7. Безопасность

- SSH key — **только** для деплоя, не используй его для своих личных сессий
- На VPS можно ограничить authorized_keys одной командой:
  ```
  command="cd ~/plan-kapkan && git fetch && git checkout main && git pull && docker restart plan-kapkan-radar",no-port-forwarding,no-X11-forwarding,no-pty ssh-ed25519 AAA... github-actions-deploy
  ```
  Тогда даже с этим key'ом нельзя получить shell — только запустить deploy команду.
- Token rotation: раз в N месяцев пересоздавай ключ (повторить шаги 1-3).

## 8. Откат

Если деплой сломал прод — workflow автоматический, но откат руками. На VPS:

```bash
cd ~/plan-kapkan
git log --oneline -5    # найти предыдущий хороший commit, скажем abc1234
git checkout abc1234
docker restart plan-kapkan-radar
```

Чтобы main оставался в покое — не `git push --force`. Потом пишешь revert PR через GitHub UI и автодеплой накатит fix.
