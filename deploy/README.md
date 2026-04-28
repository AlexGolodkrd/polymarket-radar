# Deploy guide — plan-kapkan на VPS

Phase 6 готовит образ к деплою. Этот файл — пошаговая инструкция для двух
рекомендованных площадок: AWS us-east-2 (рядом с Polymarket) и DigitalOcean
NYC. Стоимость, IAM-роли, ключи — всё здесь.

> ⚠️ **Не запускайте `DRY_RUN=0` пока Phase 5 graduation gate не подтвердит
> готовность** (≥ 100 paper trades, win rate ≥ 70%, drift ≤ 20%). См.
> `/api/graduation` или клик на `paper:` в шапке дашборда.

---

## 1. Локальный smoke-test перед деплоем

```bash
# Build the image
docker build -t plan-kapkan-radar .

# Bring up radar + watchdog
docker compose up -d

# View logs
docker compose logs -f radar

# Health
curl http://localhost:5050/api/risk_status
curl http://localhost:5050/api/graduation
curl http://localhost:5050/api/wallets

# Stop
docker compose down
```

`Executions/` смонтирован bind-volume → state переживает рестарт.

---

## 2. AWS us-east-2 (рекомендация: нога Polymarket в этом регионе)

**Площадка:** Fargate Spot или t4g.small EC2.

### Стоимость
- t4g.small (2 vCPU, 2 GB RAM): **~$15/мес** (Reserved 1y), **~$25/мес** spot.
- Fargate (0.5 vCPU, 1 GB): **~$12/мес** при 24/7.

### IAM (для AWS Secrets Manager backend)

Создайте IAM Role `plan-kapkan-task-role` с inline policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["secretsmanager:GetSecretValue"],
    "Resource": "arn:aws:secretsmanager:us-east-2:*:secret:plan-kapkan/*"
  }]
}
```

Секреты в Secrets Manager (например `plan-kapkan/bot1`):
```json
{"eth_address": "0x...", "private_key": "0x..."}
```

(Phase 4 `AwsSecretsStore` — skeleton; в текущем виде надо доразвернуть
методы `addresses()` и `sign()` под реальный JSON-формат секретов.)

### Запуск (EC2)

```bash
# 1. SSH на инстанс
ssh ubuntu@<ec2-ip>

# 2. Установить Docker
sudo apt update && sudo apt install -y docker.io docker-compose-v2

# 3. Склонировать
git clone https://github.com/AlexGolodkrd/plan-kapkan.git
cd plan-kapkan

# 4. Настроить Credentials.env (см. .env.example)
cp .env.example Credentials.env
# Заполните WALLET_BACKEND=aws, AWS_REGION=us-east-2 (без приватных ключей —
# секреты тянутся через IAM)

# 5. Запустить
sudo docker compose up -d

# 6. Открыть дашборд (через SSH-туннель или Security Group rule на :5050)
ssh -L 5050:localhost:5050 ubuntu@<ec2-ip>
# → открыть http://localhost:5050 в браузере
```

### Запуск (Fargate)

В `task-definition.json` использовать образ из ECR (push'нуть локально
собранный `plan-kapkan-radar:latest`). Назначить `plan-kapkan-task-role`,
`taskRoleArn`. Volume для `Executions/` — EFS mount (state-persistence
между рестартами).

---

## 3. DigitalOcean NYC

**Площадка:** Basic Droplet 2GB ($12/мес).

```bash
doctl compute droplet create plan-kapkan-radar \
  --image docker-20-04 --size s-1vcpu-2gb --region nyc3 \
  --ssh-keys $YOUR_KEY_ID

# SSH, потом то же что для EC2 пунктами 3-6.
```

DO не имеет Secrets Manager — используйте `WALLET_BACKEND=local` и шифруйте
`Credentials.env` через GPG (или храните на отдельном инстансе).

---

## 4. Latency budget

С локальной машины Москва ↔ Polymarket ~250мс. С VPS:
- AWS us-east-2: 5-15мс до Polymarket Polygon nodes (Polymarket API в AWS).
- DO NYC: 20-40мс — приемлемо для most arbs, но толстые арбы (1+ цент,
  держатся секунды) ловятся надёжнее с us-east-2.

Если latency критична — добавить `web3.py` с собственным RPC-нодом
(Alchemy/QuickNode/Infura) на той же AWS-зоне. См. `POLYGON_RPC_URL` в
`.env.example`.

---

## 5. Operational checklist

| Что | Где |
|---|---|
| Кошелёк адреса | `Credentials.env` (BOT*_ETH_ADDRESS) |
| Приватные ключи | AWS Secrets Manager или зашифрованный Credentials.env |
| Polygon RPC | `POLYGON_RPC_URL` в env (для balance reads + transfers) |
| Cold storage | `COLD_WALLET_ADDRESS` в env (auto-sweep destination, Phase 5+) |
| Дашборд | `http://<vps>:5050` (через VPN/SSH-туннель, не публикуйте на public IP) |
| Kill switch | UI red 🛑 STOP кнопка ИЛИ `touch Executions/.killed` ИЛИ `POST /api/kill {confirm:'YES'}` |
| Resume | UI ↺ RESUME ИЛИ `POST /api/risk_resume` |
| Логи | `docker compose logs -f radar`, `docker compose logs -f watchdog` |
| State backup | `Executions/` — копировать раз в день (рост ~10 MB/день в активной фазе) |

---

## 6. Что **не** в Phase 6

- **Real cancel API в watchdog** — wired в Phase 4 после `BOT*_PRIVATE_KEY`
- **Real USDC.transfer для auto-rebalance** — wired в Phase 4 + `POLYGON_RPC_URL`
- **AwsSecretsStore.addresses()/sign() реализация** — skeleton, доразвернуть
  под JSON-формат секретов когда они появятся
- **WindowsCredStore** — оставлен skeleton, актуален для local dev на
  Windows; production VPS = Linux + AwsSecretsStore

Эти доделки делаются **поверх** Phase 6 без изменений в Dockerfile или
docker-compose.yml — модули pluggable, ключи через env, контейнер
перезапускается с новым кодом.

---

## 7. VPS в Грузии (без VPN) — рекомендуемая архитектура

Если регистрация на Polymarket была сделана с грузинского IP, **самый надёжный
вариант — VPS прямо в Грузии**: IP консистентный, VPN не нужен, latency
~140мс до Polymarket. Меньше точек отказа.

### Локальные провайдеры

| Провайдер | Цена/мес | Notes |
|---|---|---|
| **PROServiceGE** (proservice.ge) | $15-25 | KVM VPS Linux, RU/EN сайт, известен среди русскоязычных |
| **Hostpark.ge** | $10-20 | Меньше известен но работает |
| **Caucasus Online** (caucasus.net) | $15-30 | Старый игрок, dedicated также |
| **MagtiCom** | $30+ | Корпоративный класс, надёжно но дорого |

Альтернативы — **Армения** (`hosting.am`), **Турция** (BulutHosting / Turhost) —
если регистрировался не из Грузии, выбирай VPS в той же стране что и регистрация.

### Latency-baseline

| Откуда | До Polymarket API | Fill rate (см. §4) |
|---|---|---|
| Tbilisi VPS direct | ~140мс | ~30% |
| Frankfurt + Mullvad GE exit | ~170мс | ~25% (хуже из-за double-hop) |
| Frankfurt direct (DE IP) | ~85мс | бан риск (DE серая зона + mismatch с регистрацией GE) |

---

## 8. VPN kill switch — три слоя защиты

Если **не получается** взять VPS в той же стране что регистрация и
вынужден использовать VPN — обязательно настроить **kill switch** чтобы
трафик не утёк мимо VPN-туннеля при его падении.

### Layer 1 — System firewall (iptables) или встроенный VPN client kill-switch

#### Mullvad (рекомендую — €5/мес, есть CLI, kill switch встроен)

```bash
# Install Mullvad on Ubuntu/Debian VPS
curl -fsSLO https://mullvad.net/media/app/MullvadVPN.deb
sudo dpkg -i MullvadVPN.deb

# Login + configure
mullvad account login <YOUR_MULLVAD_TOKEN>
mullvad relay set location ge tbilisi    # primary GE exit
mullvad lockdown-mode set on              # KILL SWITCH: blocks ALL non-VPN traffic
mullvad auto-connect set on               # auto-connect on boot
mullvad connect

# Verify
mullvad status                             # should show "Connected" + Georgia
curl https://api.country.is/               # should return {"ip": "...", "country": "GE"}
```

При обрыве VPN: lockdown-mode + iptables drop → бот получает `Connection refused`,
не leakнет на bare provider IP.

#### ProtonVPN (альтернатива — есть free tier, slower)

```bash
sudo apt install protonvpn-cli
protonvpn-cli login <YOUR_USERNAME>
protonvpn-cli c --cc GE                   # connect to Georgia
protonvpn-cli ks --on                     # kill switch on
```

#### Manual iptables (если VPN-провайдер без встроенного kill switch)

```bash
# Default DROP all outgoing
sudo iptables -P OUTPUT DROP
sudo iptables -A OUTPUT -o lo -j ACCEPT                                # loopback
sudo iptables -A OUTPUT -d <VPN_SERVER_IP> -p udp --dport 51820 -j ACCEPT  # WireGuard handshake
sudo iptables -A OUTPUT -o wg0 -j ACCEPT                              # all VPN tunnel traffic
sudo apt install netfilter-persistent
sudo netfilter-persistent save                                         # survive reboot
```

### Layer 2 — systemd dependency

Сервис радара **запускается только если VPN активен** и **падает если VPN падает**.

`/etc/systemd/system/plan-kapkan-radar.service`:
```ini
[Unit]
Description=plan-kapkan arbitrage radar
After=mullvad-daemon.service
Requires=mullvad-daemon.service       # we need mullvad to start at all
BindsTo=mullvad-daemon.service        # we DIE if mullvad dies

[Service]
WorkingDirectory=/opt/plan-kapkan
ExecStart=/usr/bin/python3 Scripts/arb_server.py
EnvironmentFile=/opt/plan-kapkan/Credentials.env
Restart=on-failure
RestartSec=30s
User=radar
Group=radar

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable plan-kapkan-radar.service
sudo systemctl start plan-kapkan-radar.service
sudo journalctl -u plan-kapkan-radar -f    # follow logs
```

### Layer 3 — Application-level IP/country check (built into the bot)

В `Scripts/risk/network_check.py`. Бот сам каждые 60с проверяет свой IP
и страну через ipinfo-провайдеров; если страна не в `ALLOWED_COUNTRIES` —
все fire'ы блокируются через risk gate.

Конфиг через env:
```
ALLOWED_COUNTRIES=GE                       # primary Georgia
ALLOWED_COUNTRIES=GE,AM,TR                 # Georgia + fallback regions (smae country!)
```

⚠️ **Не вписывай страны разных регионов** — Polymarket видит каждый smene IP. GE+AM+TR
это всё кавказский регион, выглядит для них как «один провайдер с
переменными exit'ами». А вот `GE,DE` — уже подозрительно.

При старте радара banner покажет:
```
Network: ALLOWED=GE | current IP 95.X.X.X (GE) → ✓ allowed
```
Или, если что-то не так:
```
⚠ Network: ALLOWED=GE | current IP 178.Y.Y.Y (DE) → ✗ DISALLOWED. Fires WILL be blocked.
```

Endpoint: `GET /api/network_status` — текущий IP, страна, age cache.
`GET /api/network_status?force=1` — bypass cache, fresh fetch.

### Все 3 слоя одновременно (defense in depth)

Они **не альтернативы**, а кумулятивная защита:

| Слой | Защищает от |
|---|---|
| **1. iptables/Mullvad lockdown** | Любой leak трафика на bare IP при падении VPN |
| **2. systemd dependency** | Запуск бота без VPN (например после ребута) |
| **3. Application IP-check** | Случаи когда L1+L2 misconfigured/bypass'нулись |

Layer 3 — самый «мягкий», но он **независим** от Linux/sysadmin layer'ов и
работает даже если ты ошибся с iptables.

---

## 9. Hot standby VPS (опционально, после стабильной работы)

См. отдельный гайд — [`deploy/standby-setup.md`](standby-setup.md).
