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
