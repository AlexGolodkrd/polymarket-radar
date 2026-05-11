# 🔑 Credentials.env — полный гайд

Что вставлять, **где взять**, что для какой стадии (paper trading → real money).

**Расположение:** `Credentials.env` в корне репозитория. **НЕ коммитится** (есть в `.gitignore`).

## TL;DR — минимум по стадиям

| Стадия | Что нужно |
|---|---|
| 🟢 **Локальный dev / paper trading с mocks** | Ничего. Радар запустится с synthesized mock wallets (`canSign=false`) |
| 🟡 **Paper trading с реальными wallet addresses** (для on-chain reads только) | `BOT1..BOT6_ETH_ADDRESS` |
| 🟠 **Paper trading c подписями для proof-of-concept** | + `BOT*_PRIVATE_KEY`, `POLYGON_RPC_URL` |
| 🔴 **Real money trading** | Всё ниже + on-chain approvals (one-time) + L2 API keys |

---

## 🔵 ОБЯЗАТЕЛЬНО для любого режима

### `GITHUB_TOKEN`
**Что:** Personal Access Token (PAT) для GitHub API.
**Где взять:**
1. https://github.com/settings/tokens → "Generate new token (classic)"
2. Scopes: `repo` (full control of private repos), `workflow` (deploy.yml triggers)
3. Срок: пока не отзовёшь
4. Копируй сразу — больше не покажет

**Используется:** локальные скрипты для создания PR'ов, авто-деплоя.

---

## 🟡 6-bot wallet pool (для paper trading с реальными adresses)

Каждому из 6 ботов нужны эти переменные. Минимум — `ETH_ADDRESS` (для on-chain reads, например USDC баланса).

### `BOT1_ETH_ADDRESS` ... `BOT6_ETH_ADDRESS`
**Что:** EIP-55 checksummed Polygon/Ethereum addresses (`0xABcD...`).
**Где взять:** создай 6 новых wallet'ов в MetaMask / любом keystore.
- **Важно:** разные wallet'ы для anti-detection (мы не хотим один кошелёк × 6 ног арба)
- Polygon (chainId 137) для Polymarket+Limitless
- Base (chainId 8453) для Limitless если на Base
- Mainnet (chainId 4162) для SX Bet

**Безопасность:** address — публичная инфа, OK хранить в env file.

### `BOT1_PRIVATE_KEY` ... `BOT6_PRIVATE_KEY`
**Что:** 0x + 64 hex chars (`0xabcd...`).
**Где взять:** Export from MetaMask / Hardhat keystore (Settings → Account Details → Export Private Key).
**Безопасность:** **КРИТИЧНО**. Если файл утечёт — кошельки опустошены. Никогда не комитить, никогда не отправлять в чат.

С PR #142 (signer normalization) формат принимается с/без `0x`, с/без whitespace, в любом регистре. Но рекомендую канонический: `0x` + 64 lowercase hex.

### `BOT*_FUNDER_ADDRESS` (опционально, для proxy/safe wallets)
**Что:** Address, который **держит pUSD** на Polymarket V2 (отличается от signer'а для type 1 = POLY_PROXY / type 2 = POLY_GNOSIS_SAFE).
**Где взять:** Если используешь Polymarket через MetaMagic proxy — это адрес твоего pUSD proxy contract. Можно увидеть в Polymarket UI → Account → "On-chain address".
**Когда нужно:** только для **non-EOA** wallets (если ты подписываешь через прокси).

### `BOT*_SIGNATURE_TYPE`
**Что:** `0` / `1` / `2`.
**Значения:**
- `0` (default) = EOA (стандартный кошелёк, signer == maker)
- `1` = POLY_PROXY (Magic-derived прокси, signer != maker)
- `2` = POLY_GNOSIS_SAFE (Gnosis Safe держит средства)

**Где взять:** определяется тем, как ты завёл свой Polymarket аккаунт. Если через email/social (Magic) → 1. Если через MetaMask напрямую → 0.

### `WALLET_BACKEND`
**Что:** `local` / `aws` / `windows_cred`.
**Default:** `local` (читает из Credentials.env).
**Когда менять:**
- `aws` если используешь AWS Secrets Manager на VPS (требует `AWS_REGION` + IAM роль на инстансе)
- `windows_cred` для Windows Credential Manager (Phase 6 stub)

---

## 🟠 Polymarket L2 API credentials (для real-mode trading)

L2 креды нужны **только** для:
1. POST `/order` (real money order submission)
2. DELETE `/order/{id}` (cancel — TS-6 уже это умеет)
3. GET `/balance-allowance` (on-chain reads через API)

В paper trading (`DRY_RUN=1`) они НЕ нужны — builder создаёт unsigned order, который не отправляется.

### `BOT*_POLY_API_KEY` / `BOT*_POLY_SECRET` / `BOT*_POLY_PASSPHRASE`
**Что:** Тройка для HMAC-SHA256 auth на L2 endpoint'ах.
**Где взять (one-time setup, нужен private key уже задан):**
```bash
cd Scripts
python poly_derive_api_creds.py --bot 1
# → distt 3 values, copy into Credentials.env
# Repeat for bots 2..6
```

Утилита делает L1 EIP-712 sign и обменивает на L2 creds через Polymarket API.

**Безопасность:** Эти creds — write access к L2 endpoints конкретного бота. Утечка = риск отправки ордеров от твоего имени (хотя без private key подписать нельзя).

---

## 🟠 Limitless API key

### `LIMITLESS_API_KEY` (global) или `BOT*_LIMITLESS_API_KEY` (per-bot)
**Что:** Bearer token для Limitless API auth.
**Где взять:**
1. https://limitless.exchange → подключи MetaMask
2. Account settings → API keys → Generate
3. Один ключ можно использовать для всех ботов (`LIMITLESS_API_KEY=...`), или отдельные (`BOT1_LIMITLESS_API_KEY=...` etc.)

**Поиск приоритета:** код сначала проверяет `BOT{i}_LIMITLESS_API_KEY`, иначе глобальный `LIMITLESS_API_KEY`.

**Без этого ключа:** Limitless user-channel WS не подпишется на `orderEvent`, не сможет confirm'ить fills. Реальная торговля на Limitless невозможна.

---

## 🟠 On-chain RPC (для balance reads + real cancel transactions)

### `POLYGON_RPC_URL`
**Что:** Polygon mainnet RPC endpoint.
**Где взять:** Alchemy / Infura / Quicknode (бесплатные тиры хватает).
- Alchemy: https://www.alchemy.com → Create app → Polygon Mainnet → copy HTTPS URL
- Пример: `https://polygon-mainnet.g.alchemy.com/v2/YOUR_KEY`

**Используется:** preflight checks (USDC balance), executor revert (on-chain если pUSD), reconcile.

### `BASE_RPC_URL` (для Limitless если на Base)
**Что:** Base mainnet RPC.
**Где взять:** Alchemy → Base → HTTPS URL.
**Используется:** Limitless CTF Exchange (chainId 8453) approval checks.

### `SX_RPC_URL` (для SX Bet — SX Network)
**Что:** SX Network RPC (chainId 4162).
**Где взять:** https://sx.technology → docs → RPC endpoint (`https://rpc.sx.technology`).

---

## ⚪ Опциональные runtime tuning

### `DRY_RUN`
**Что:** `1` (paper trading, default) / `0` (real money).
**КРИТИЧНО:** не флипай в `0` без graduation gate (100 paper trades, win rate ≥70%, drift ≤20%).

### `EXECUTOR_URL`
**Что:** URL TS executor для dispatcher в radar.
**Default:** не задано → радар использует Python in-process executor.
**Рекомендация:** `EXECUTOR_URL=http://executor-ts:5051` (внутри docker-compose сети).

### `ALLOWED_COUNTRIES`
**Что:** Comma-separated ISO-2 country codes, откуда боту разрешено торговать.
**Пример:** `ALLOWED_COUNTRIES=GE,AM`
**Что делает:** каждые 60s проверяет outbound IP geo. Если страна не в списке — все fires заблокированы (VPN kill switch L3).
**Когда установить:** на VPS, ОБЯЗАТЕЛЬНО когда `DRY_RUN=0`. Локально/dev можно оставить пустым.

### Platform toggles (по умолчанию все `1` = enabled)
- `ENABLE_POLY=1` — Polymarket сканирование
- `ENABLE_KALSHI=0` — США-only KYC, не доступен из не-US
- `ENABLE_SX=1` — SX Bet
- `ENABLE_LIMITLESS=1` (default через отсутствие переменной)

### Structure toggles
- `ENABLE_STRUCT_A=1` — ALL_YES (multi-outcome event sum<threshold)
- `ENABLE_STRUCT_B=1` — ALL_NO (N≥3 multi-outcome)
- `ENABLE_STRUCT_C=1` — YES+NO pair per market
- `CROSS_PLATFORM_ENABLED=1` — pairwise platform арбы (X1/X2)

### Risk gates (defaults уже в коде, переписывай только если знаешь что делаешь)
- `MAX_PER_TRADE_USD` — default $55
- `DAILY_LOSS_LIMIT_USD` — default $35
- `LOSING_TRADES_PER_HOUR` — default 5 → пауза 1h
- `MIN_NET_PER_ARB_USD` — default $0.50 (mosquito reject)
- `SLIPPAGE_TOLERANCE` — default 0.005 (50 bps)

### Telegram alerts (опционально)
- `TELEGRAM_BOT_TOKEN` — для notify.py
- `TELEGRAM_CHAT_ID` — твой chat id

---

## 🔴 Real money: one-time on-chain setup (ПЕРЕД `DRY_RUN=0`)

Полностью one-time, не через env vars — через утилиты:

### 1. Финансирование кошельков
```bash
# Каждому из 6 ботов нужно:
# Polygon:   ~$50 USDC + ~$2 MATIC (gas) → BOT*_ETH_ADDRESS
# Base:      ~$50 USDC + ~$0.5 ETH (gas) если Limitless
# SX:        ~$50 USDC + tiny native gas если SX
```

### 2. Polymarket approvals (1 раз per bot)
```bash
cd Scripts
python polymarket_approve.py --bot 1
# Делает: USDC → pUSD wrap (deposit) + CTF approveForAll (~$0.50 gas)
# Repeat for bots 2..6
```

### 3. Derive L2 creds (1 раз per bot)
```bash
python poly_derive_api_creds.py --bot 1
# Output: BOT1_POLY_API_KEY=..., SECRET=..., PASSPHRASE=...
# Скопируй вывод в Credentials.env
```

### 4. Verify everything
```bash
python preflight.py
# Проверит: balance >= $50, allowance set, L2 creds work, RPC reachable
```

---

## 📁 Полный шаблон Credentials.env (для real money trading)

```bash
# ── Operations / GitHub ──────────────────────────────────────
GITHUB_TOKEN=ghp_xxxxx

# ── Wallets (6 bots) ─────────────────────────────────────────
WALLET_BACKEND=local
COLD_WALLET_ADDRESS=0x...  # destination for sweep (optional)

BOT1_ETH_ADDRESS=0x...
BOT1_PRIVATE_KEY=0xabc...64hex
BOT1_FUNDER_ADDRESS=         # only if SIGNATURE_TYPE != 0
BOT1_SIGNATURE_TYPE=0
BOT1_POLY_API_KEY=
BOT1_POLY_SECRET=
BOT1_POLY_PASSPHRASE=
BOT1_LIMITLESS_API_KEY=      # OR use global LIMITLESS_API_KEY below
# ... (повторить BOT2..BOT6)

# ── Optional: shared keys ─────────────────────────────────────
LIMITLESS_API_KEY=           # used if BOT*_LIMITLESS_API_KEY not set

# ── On-chain RPCs ────────────────────────────────────────────
POLYGON_RPC_URL=https://polygon-mainnet.g.alchemy.com/v2/YOUR_KEY
BASE_RPC_URL=https://base-mainnet.g.alchemy.com/v2/YOUR_KEY
SX_RPC_URL=https://rpc.sx.technology

# ── Modes / Routing ──────────────────────────────────────────
DRY_RUN=1                    # 0 only after graduation gate
EXECUTOR_URL=http://executor-ts:5051

# ── Network safety (L3) ──────────────────────────────────────
ALLOWED_COUNTRIES=GE,AM      # ОБЯЗАТЕЛЬНО на VPS когда DRY_RUN=0

# ── Platform toggles ─────────────────────────────────────────
ENABLE_POLY=1
ENABLE_KALSHI=0
ENABLE_SX=1
ENABLE_LIMITLESS=1

# ── Cross-platform ───────────────────────────────────────────
CROSS_PLATFORM_ENABLED=1
CP_MIN_CONFIDENCE=0.75

# ── Optional alerts ──────────────────────────────────────────
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

---

## 🚨 Что СЕЙЧАС не задано на проде (по моим probes)

На основе `/api/ts_metrics`:
```json
{
  "wallets": 6,             ← 6 ботов имеют BOT*_ETH_ADDRESS ✅
  "can_sign": 3,            ← только 3 имеют BOT*_PRIVATE_KEY  ⚠
  "signers_registered": 3,  ← все 3 ключа валидны после PR #142 ✅
  "using_mock_wallets": false,
  "limitless_user_ws": [{hasApiKey: false} × 6]  ← LIMITLESS_API_KEY НЕТ ❌
}
```

**Что добавить чтобы можно было real-mode trading:**
1. **3 оставшихся `BOT*_PRIVATE_KEY`** (бот 4-5-6 без ключей)
2. **`LIMITLESS_API_KEY`** или `BOT*_LIMITLESS_API_KEY` — без неё real-mode на Limitless невозможен
3. **`BOT*_POLY_API_KEY/SECRET/PASSPHRASE`** для каждого из 6 ботов (через `poly_derive_api_creds.py`)
4. **`POLYGON_RPC_URL`** (Alchemy/Infura) — для preflight checks
5. **`ALLOWED_COUNTRIES`** на VPS — обязательно перед `DRY_RUN=0`

---

## 🛡️ Безопасность хранения

**НИКОГДА:**
- Не коммитить в git (.gitignore это покрывает, но всё равно проверяй `git status --ignored`)
- Не показывать содержимое в чате
- Не отправлять в Telegram/Discord
- Не оставлять в clipboard после копирования (clear после)

**МОЖНО:**
- Хранить на VPS в `/home/<user>/plan-kapkan/Credentials.env` с `chmod 600`
- Резервная копия в зашифрованном password manager (1Password / Bitwarden) с тегом "kapkan-creds-prod"
- AWS Secrets Manager (если `WALLET_BACKEND=aws`)

**ПРИ КОМПРОМЕТАЦИИ:**
1. Немедленно отправь все средства с pwn'нутых wallet'ов на cold storage
2. Сгенерируй новые wallet'ы + новые L2 креды
3. Отзови GITHUB_TOKEN (https://github.com/settings/tokens → Revoke)
4. Обнови `Credentials.env` с новыми значениями
