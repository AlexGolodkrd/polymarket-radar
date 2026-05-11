---
name: secrets-management
description: |
  Safe handling of private keys, API tokens, and credentials in plan-kapkan.
  Includes rotation policies, git-leak prevention, and the safe path for
  delivering wallet private keys to the VPS without ever putting them in
  Anthropic chat or git history.
---

# secrets-management — безопасная работа с секретами

## Что считается секретом в этом проекте

| Тип | Где живёт | Severity при утечке |
|---|---|---|
| `BOT*_PRIVATE_KEY` (EVM) | `Credentials.env` (VPS только) | 🔴 **КРИТИЧНО** — деньги уйдут с кошелька |
| `GITHUB_TOKEN` (PAT) | `Credentials.env` (локально) | 🟡 средне — push access на репо |
| `OPENAI_API_KEY` / `APIFY_API_KEY` | `Credentials.env` | 🟡 средне — биллинг |
| Basic auth на dashboard (`admin:Ts6RLPzIMQr2tKAMvNAN`) | `nginx-config/...` или env | 🟢 низко — доступ к UI, не к ключам |
| SSH private key | `~/.ssh/id_rsa` | 🔴 **КРИТИЧНО** — root access на VPS |

## Pre-commit / pre-push проверки

```bash
# Перед каждым commit:
git status --ignored --short | grep "^!!" | grep -E "Credentials\.env|\.pem|id_rsa"
# Эти файлы должны быть в "!!" (ignored). Если в "??" (untracked) — НЕ committed,
# но при `git add .` могут попасть. Лучше явно в .gitignore.

# Перед push на public branch:
git diff origin/main..HEAD | grep -iE "(0x[a-f0-9]{64}|BOT[0-9]+_PRIVATE_KEY=|sk-[a-zA-Z0-9]{20,}|ghp_[a-zA-Z0-9]{36})"
# Если что-то нашлось → НЕ ПУШИМ. rebase + filter-branch удалить из истории.
```

## Что в `.gitignore` обязательно

```gitignore
# Секреты
Credentials.env
*.env
!example.env  # пример без значений — можно committit'ить
**/secrets/
**/private/

# Логи (могут содержать ключи в трейсбэках)
Executions/*.jsonl
Executions/*.log
Executions/*.bak.*

# SSH/TLS
*.pem
*.key
id_rsa*
*.cert
*.p12

# Local artifacts
.venv/
__pycache__/
*.pyc
.DS_Store
```

## Безопасная доставка private key на VPS

### ❌ НЕ ДЕЛАТЬ
- Send в Anthropic chat (попадает в conversation log → в training data, theoretically)
- `scp` с домашней машины (если файл записан plain → засветится в shell history)
- `git push` (даже если потом `git rm` + commit — ключ остаётся в git history навсегда)
- `echo $KEY | tee Credentials.env` (key в bash history)

### ✅ ПРАВИЛЬНО (одна из 3 опций)

**Опция A: SSH + nano (самый простой)**
```bash
ssh arb@77.91.97.22
cd /home/arb/plan-kapkan
nano Credentials.env
# Добавляешь BOT*_PRIVATE_KEY=<paste>
# Ctrl+O, Enter, Ctrl+X
chmod 600 Credentials.env
docker compose restart radar
```

**Опция B: Generate prv key directly on VPS (для новых кошельков)**
```bash
# На VPS:
docker exec plan-kapkan-radar python3 -c "
from eth_account import Account
acct = Account.create()
print(f'address: {acct.address}')
print(f'private_key: {acct.key.hex()}')  # сохраняем ТОЛЬКО в Credentials.env
"
# → копируем в Credentials.env, address — куда угодно (публичный)
```

**Опция C: AWS Secrets Manager (production-grade)**
```bash
# 1. Положить ключ:
aws secretsmanager create-secret \
  --name plan-kapkan/bot1-private-key \
  --secret-string "0x..." \
  --region us-east-2

# 2. В коде использовать:
import boto3
client = boto3.client('secretsmanager')
key = client.get_secret_value(SecretId='plan-kapkan/bot1-private-key')['SecretString']

# 3. IAM role на VPS гарантирует что ключ доступен только из конкретного контейнера
```

## Rotation policy

| Секрет | Частота rotation | Trigger | Процедура |
|---|---|---|---|
| `BOT*_PRIVATE_KEY` | По мере подозрений / раз в 6 мес | Утечка / увольнение оператора / странные tx | 1) Создать новый кошелёк 2) Пере-funded 3) Replace в env 4) Restart 5) Send старого + tx history в hot wallet |
| `GITHUB_TOKEN` (PAT) | После каждого использования | После ~20 использований | GitHub UI → Settings → Tokens → Revoke + recreate |
| `OPENAI_API_KEY` | Раз в месяц или после биллинг-аномалий | Spike в usage | OpenAI dashboard → Generate new → Replace |
| Basic auth dashboard | Раз в 3 мес | Любые признаки brute-force | nginx config + env update + restart |
| SSH key | Раз в год + при смене машины | Утеря/Compromise лаптопа | `ssh-keygen -t ed25519` → add to `authorized_keys` → revoke старый |

## Github Token specific

В этом проекте PAT хранится в `Credentials.env` под `GITHUB_TOKEN=`. **НИКОГДА**:

- Не записывать в `.git/config` (Git Credential Manager на Windows этому помогает)
- Не передавать через `git push -u` (записывается в `[remote "origin"].url`)

**ВСЕГДА**:
```bash
TOKEN=$(grep '^GITHUB_TOKEN=' Credentials.env | cut -d= -f2-)
git -c credential.helper= push https://x-access-token:$TOKEN@github.com/AlexGolodkrd/plan-kapkan.git $BRANCH:$BRANCH

# Verify .git/config clean:
grep -c "x-access-token" .git/config  # → 0
```

## Что делать при утечке

### 1. Утёк private key кошелька
1. **Mute** кошелёк сразу: переведи остатки в hot wallet (USDC + любой ETH/MATIC)
2. Удали ключ из `Credentials.env` на VPS
3. Restart радара
4. Создай новый кошелёк (см. опцию B выше)
5. Обнови `BOT*_ETH_ADDRESS` локально + sync на VPS
6. Funded заново
7. Если ключ был в git → `git filter-repo --invert-paths --path Credentials.env` + force push (НО только на private repo!)

### 2. Утёк GITHUB_TOKEN
1. Revoke на GitHub UI: https://github.com/settings/tokens
2. Создай новый
3. Update в `Credentials.env`
4. Никаких force-push — токен не в git history

### 3. Утёк SSH key
1. Зайди на VPS через Termius password (если не отключил password auth)
2. Удали публичную часть из `~/.ssh/authorized_keys`
3. Сгенерируй новую пару `ssh-keygen -t ed25519 -f ~/.ssh/id_rsa_new`
4. Добавь публичный ключ на VPS
5. Удали старый `id_rsa` локально

## Secret scanning automation

```bash
# В pre-commit hook (.git/hooks/pre-commit):
#!/bin/bash
PATTERNS=(
  "0x[a-fA-F0-9]{64}"     # EVM private key
  "BOT[0-9]+_PRIVATE_KEY=[^#]"  # без # (комментарий допустим)
  "ghp_[a-zA-Z0-9]{36}"   # GitHub PAT
  "sk-[a-zA-Z0-9]{20,}"   # OpenAI key
)
for pat in "${PATTERNS[@]}"; do
  if git diff --cached | grep -qE "$pat"; then
    echo "❌ Possible secret leak matching $pat"
    exit 1
  fi
done
```

## Refs

- `CLAUDE.md` — проектный обзор PR-flow + GitHub auth
- `deploy-pipeline/SKILL.md` — где этот скилл вписан в общий процесс
- `feature-flags/SKILL.md` — для kill-switch'ей (которые тоже секрет в env)
