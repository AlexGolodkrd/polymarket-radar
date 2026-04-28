# Hot Standby VPS — отказоустойчивость для production

Этот гайд для сценария когда у тебя уже **месяц+ стабильно** работает primary VPS и хочется снизить downtime с часов до минут при падении провайдера/железа.

> ⚠️ **Не ставь это первым шагом.** Сначала 2-4 недели с одним VPS. Hot standby — оптимизация после того как основной flow подтверждён.

---

## Архитектура

```
                ┌─────────────────────┐
                │ External Monitor    │
                │ (твой ПК / 3-й VPS) │
                │ — health check      │
                │ — failover trigger  │
                │ — Telegram alerts   │
                └──────────┬──────────┘
                           │ ping every 30s
                ┌──────────┴──────────┐
                ▼                     ▼
    ┌─────────────────────┐ ┌─────────────────────┐
    │ VPS Tbilisi #1      │ │ VPS Tbilisi #2      │
    │  (active)           │ │  (standby, idle)    │
    │  — radar running    │ │  — radar STOPPED    │
    │  — :5050 OK         │ │  — code identical   │
    │  — IP: 95.X.X.1     │ │  — IP: 95.X.X.2     │
    └─────────────────────┘ └─────────────────────┘
                ↑                     ↑
                └─── Polymarket sees ─┘
                  (different IPs but
                   same country)
```

**Ключевые правила:**
1. Оба VPS **в одной стране** (и желательно у одного провайдера) — IP разные но регион тот же.
2. Standby реально **запущен** (`docker compose up -d` в фоне) но без активного scan_loop. Готов мгновенно стать active.
3. Monitor — на **отдельной** машине (твой домашний ПК или третий VPS).
4. Failover **манипулирует одним и тем же wallet** — приватники одинаковые на обоих VPS.

---

## Шаги setup

### 1. Купить второй VPS

У того же провайдера, в том же датацентре, в идеале — отличается одной цифрой в IP-range. Это усиливает консистентность для Polymarket.

```bash
# На provider'е заказываешь VPS с такой же конфигурацией
# Получаешь 2-й SSH endpoint, например vps2.example.com
```

### 2. Зеркальная установка кода + ключей

```bash
# На primary VPS (vps1):
cd /opt/plan-kapkan
git remote add standby user@vps2.example.com:plan-kapkan.git    # bare repo для пуша

# На standby VPS (vps2):
git clone https://github.com/AlexGolodkrd/plan-kapkan.git /opt/plan-kapkan
# Скопируй Credentials.env с primary через scp (через свой ПК):
scp user@vps1:/opt/plan-kapkan/Credentials.env /tmp/cred.env
scp /tmp/cred.env user@vps2:/opt/plan-kapkan/Credentials.env
shred -u /tmp/cred.env    # удали с твоего ПК!
```

### 3. Standby в режиме «warm but idle»

На standby VPS — radar запущен но **сам не торгует** до промоушена. Самый простой способ — флаг `Executions/.killed`:

```bash
# На vps2 (standby):
cd /opt/plan-kapkan
touch Executions/.killed                 # kill switch активен → radar блокирует все fire'ы
docker compose up -d                     # все остальные процессы работают (scan, WS, etc.)
```

### 4. External monitor

Скрипт на твоём ПК или третьей машине. Каждые 30 секунд проверяет healthcheck primary; если 3 раза подряд fail — promote standby.

`monitor.sh`:
```bash
#!/usr/bin/env bash
PRIMARY="https://vps1.example.com:5050/api/risk_status"
STANDBY_HOST="user@vps2.example.com"
TELEGRAM_TOKEN="..."
TELEGRAM_CHAT="..."

fail_count=0
while true; do
    if ! curl -fsS --max-time 5 "$PRIMARY" > /dev/null; then
        ((fail_count++))
        echo "[$(date)] primary fail $fail_count/3"
        if [ $fail_count -ge 3 ]; then
            # PROMOTE standby
            ssh "$STANDBY_HOST" 'rm -f /opt/plan-kapkan/Executions/.killed && \
                                  cd /opt/plan-kapkan && docker compose restart radar'
            curl -s "https://api.telegram.org/bot$TELEGRAM_TOKEN/sendMessage" \
                 -d "chat_id=$TELEGRAM_CHAT&text=⚠ FAILOVER: primary vps1 down, promoted vps2"
            fail_count=0
            sleep 600    # cooldown 10 min before next cycle
        fi
    else
        fail_count=0
    fi
    sleep 30
done
```

Запусти как daemon:
```bash
nohup ./monitor.sh > monitor.log 2>&1 &
```

Или в systemd как `monitor.service`.

### 5. Восстановление primary

Когда vps1 восстановился — **не делай auto-failback**. Лучше:
1. Получить алерт что vps1 ожил
2. Руками решить: оставить vps2 active (если он стабильно работает) ИЛИ переключить обратно на vps1
3. Тот что стал idle → `touch Executions/.killed`, готов к следующему failover

---

## Cost estimate

| | $ /мес |
|---|---|
| Primary VPS (Tbilisi, $20) | $20 |
| Standby VPS (тот же tier) | $20 |
| 3rd machine для monitor (или твой домашний ПК — $0) | $0-10 |
| **Итого** | **$40-50/мес** |

Если ты делаешь $200-1000/сутки на боте, $50/мес страховки — уместно.

---

## Альтернативы

### Snapshot-based recovery (дешевле)

Вместо живого standby — раз в час делается snapshot диска primary VPS (большинство провайдеров это умеет $1-5/мес дополнительно). При падении — recreate из snapshot.

| Pros | Cons |
|---|---|
| $1-5/мес vs $20 | Recovery 5-15 минут вместо секунд |
| Только один VPS живой | Нужно кому-то нажать кнопку «restore» |

### Cloud-managed (Kubernetes etc)

Запускать всё в managed K8s (DigitalOcean, AWS EKS) с auto-scaling и health-checks. Слишком сложно для нашего размера, **не рекомендую** для арб-бота на $200-1000/сутки.

---

## Telegram alerts (must-have для любой архитектуры)

Даже без hot standby — обязательно поставь алерты на критические события радара. В `Scripts/watchdog.py` уже есть hook для них. Добавь:

```python
# В Scripts/watchdog.py — внутри _on_kill_detected
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_CHAT = os.environ.get('TELEGRAM_CHAT_ID')
if TELEGRAM_TOKEN and TELEGRAM_CHAT:
    requests.post(f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
                  data={'chat_id': TELEGRAM_CHAT,
                        'text': f'⚠ KILL SWITCH activated: {reason}'})
```

Создать бота: говори `@BotFather` → `/newbot` → получаешь токен. Чтобы узнать `chat_id`: пиши боту `/start`, потом `curl https://api.telegram.org/bot<TOKEN>/getUpdates` — увидишь `chat.id`.

Минимальный набор алертов:
- Kill switch activated (любой)
- Reconcile mismatch (Position не сошлась с биржей)
- Daily loss limit hit
- Hourly losing streak (5 проигрышей за час)
- Network check failed > 5 минут (VPN отвалился)
- VPS не отвечает > 3 минут (только если есть external monitor)
