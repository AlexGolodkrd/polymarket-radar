# Polymarket V2 Connector — Drop-in Reference

**Создан 02.05.2026** — single-file справка по V2 CLOB на Polygon. Консолидирует адреса/домены/типы/шаблоны, ранее размазанные по `polymarket-trading` SKILL.md, комментариям в `executor/builders.py`, `polymarket_approve.py` и `poly_derive_api_creds.py`. Для глубокого "как и почему" — см. `polymarket-trading/SKILL.md`.

## When to use

При написании любого кода, который **подписывает или отправляет ордер на Polymarket V2**: в радаре, во внешнем боте, в скрипте миграции. Если копируешь адрес/домен/тип — копируй отсюда, не из памяти.

---

## Адреса (Polygon mainnet, chainId 137, верифицированы 28.04.2026)

| Контракт | Адрес | Назначение |
|---|---|---|
| **CTF Exchange (standard)** | `0xE111180000d2663C0091e4f400237545B87B996B` | бинарные YES/NO рынки |
| **CTF Exchange (negRisk)** | `0xe2222d279d744050d28e00520010520000310F59` | multi-outcome (например, 5+ кандидатов) |
| **CTF (1155)** | `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045` | контракт outcome-токенов; `setApprovalForAll` |
| **NegRisk Adapter** | `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296` | дополнительный spender для negRisk операций |
| **Collateral Onramp (wrap/unwrap)** | `0x93070a847efEf7F70739046A929D47a521F5B8ee` | USDC.e ⇄ pUSD |
| **pUSD ERC-20** | `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB` | торговая валюта V2 (env override: `POLY_PUSD_ADDRESS` если governance меняет) |
| **USDC.e (legacy)** | `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174` | стартовая валюта; пользователь депонирует USDC.e, потом `wrap()` |

**Проверь на Polygonscan** перед прод-ботом: pUSD/onramp адреса governance-controlled, могут смениться. Все адреса в `Scripts/polymarket_approve.py` overridable через env.

---

## EIP-712 домены (V2)

```python
POLY_DOMAIN_STANDARD = {
    "name": "Polymarket CTF Exchange",
    "version": "2",
    "chainId": 137,
    "verifyingContract": "0xE111180000d2663C0091e4f400237545B87B996B",
}
POLY_DOMAIN_NEGRISK = {
    "name": "Polymarket Neg Risk CTF Exchange",
    "version": "2",
    "chainId": 137,
    "verifyingContract": "0xe2222d279d744050d28e00520010520000310F59",
}
```

`market.negRisk` (из `GET /markets/{condition_id}` на gamma-api) — single source of truth. Wrong domain = подпись валидна криптографически, но сервер `404 invalid signature`.

---

## V2 Order типы (11 полей, без `feeRateBps`/`nonce`/`expiration`/`taker`)

```python
POLY_ORDER_TYPES_V2 = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "Order": [
        {"name": "salt",          "type": "uint256"},   # uuid4 → int
        {"name": "maker",         "type": "address"},
        {"name": "signer",        "type": "address"},   # = maker для EOA
        {"name": "tokenId",       "type": "uint256"},   # CTF outcome token (decimal string)
        {"name": "makerAmount",   "type": "uint256"},   # USDC wei (6 dp)
        {"name": "takerAmount",   "type": "uint256"},   # outcome токены wei (6 dp)
        {"name": "side",          "type": "uint8"},     # 0=BUY, 1=SELL
        {"name": "signatureType", "type": "uint8"},     # 0=EOA
        {"name": "timestamp",     "type": "uint256"},   # int(time.time()*1000) ms
        {"name": "metadata",      "type": "bytes32"},   # ZERO_BYTES32 если нет
        {"name": "builder",       "type": "bytes32"},   # ZERO_BYTES32 для solo (см. ниже)
    ],
}
ZERO_BYTES32 = "0x" + ("0" * 64)
```

**Builder bytes32:** оставлять `ZERO_BYTES32` для solo-трейдинга. Builder Program — для приложений-агрегаторов; они **берут** дополнительный fee на топ платформенного, не дают rebate. Нет смысла регистрировать builderCode для собственного бота. Источник: docs.polymarket.com/builders/{overview,tiers,fees}.

---

## REST endpoints

| URL | Метод | Назначение |
|---|---|---|
| `https://clob.polymarket.com/order` | POST | place order — **single route для standard + negRisk** (роутинг по EIP-712 domain) |
| `https://clob.polymarket.com/order/{order_id}` | DELETE | cancel single order |
| `https://clob.polymarket.com/orders` | DELETE | cancel all orders для аккаунта |
| `https://clob.polymarket.com/markets/{condition_id}` | GET | per-market metadata (tick, min_size, **fee_rate_bps**, neg_risk) |
| `https://clob.polymarket.com/data/positions?user=0x...` | GET | текущие позиции (требует L2 HMAC headers) |
| `https://clob.polymarket.com/auth/derive-api-key` | POST | one-time derive L2 API key/secret/passphrase (EIP-712 ClobAuthDomain) |

**`feeRateBps` теперь динамический per-market.** Запрашивать через `/markets/{condition_id}`, не хардкодить. Реальные V2 fee варьируются 0–2.5%.

---

## Шаблон: подписать + отправить V2 ордер

```python
import time, json, uuid, requests
from eth_account import Account
from eth_account.messages import encode_typed_data

POLY_DOMAIN_STANDARD = {...}    # см. выше
POLY_DOMAIN_NEGRISK  = {...}
POLY_ORDER_TYPES_V2  = {...}
ZERO_BYTES32 = "0x" + "0" * 64

def build_and_send_poly_order(*, eth_address, private_key, token_id,
                               side, price, size_usdc, neg_risk=False,
                               order_type='GTC', tick_size=0.01):
    assert side in ('BUY', 'SELL')
    assert 0 < price < 1
    # Snap to tick (server reject otherwise)
    price = round(price / tick_size) * tick_size
    contracts = size_usdc / price
    order = {
        'salt':          str(int(uuid.uuid4().hex, 16)),
        'maker':         eth_address,
        'signer':        eth_address,
        'tokenId':       str(token_id),
        'makerAmount':   str(int(round(size_usdc * 1e6))),
        'takerAmount':   str(int(round(contracts * 1e6))),
        'side':          '0' if side == 'BUY' else '1',
        'signatureType': '0',
        'timestamp':     str(int(time.time() * 1000)),
        'metadata':      ZERO_BYTES32,
        'builder':       ZERO_BYTES32,
    }
    domain = POLY_DOMAIN_NEGRISK if neg_risk else POLY_DOMAIN_STANDARD
    # All uint256 must be ints for the encoder
    msg = {k: (int(v) if k in (
        'salt', 'tokenId', 'makerAmount', 'takerAmount',
        'side', 'signatureType', 'timestamp',
    ) else v) for k, v in order.items()}
    encoded = encode_typed_data(full_message={
        'types': POLY_ORDER_TYPES_V2,
        'primaryType': 'Order',
        'domain': domain,
        'message': msg,
    })
    sig = Account.sign_message(encoded, private_key=private_key).signature.hex()
    if not sig.startswith('0x'): sig = '0x' + sig

    body = {
        'order': {**order, 'signature': sig},
        'owner': eth_address,
        'orderType': order_type,
    }
    if order_type == 'GTD':
        body['expiration'] = str(int(time.time()) + 60)   # body, NOT signed

    r = requests.post('https://clob.polymarket.com/order',
                      json=body, timeout=5)
    return r.status_code, r.json()
```

---

## Шаблон: cancel order (требует L2 HMAC)

```python
import hmac, hashlib, base64, time, requests

def build_l2_headers(api_key, secret, passphrase, *,
                     method, path, body=''):
    ts = str(int(time.time()))
    msg = ts + method + path + body
    mac = hmac.new(base64.b64decode(secret), msg.encode(),
                   hashlib.sha256).digest()
    return {
        'POLY_ADDRESS':    eth_address,        # из контекста
        'POLY_API_KEY':    api_key,
        'POLY_PASSPHRASE': passphrase,
        'POLY_SIGNATURE':  base64.b64encode(mac).decode(),
        'POLY_TIMESTAMP':  ts,
    }

# Cancel single
path = f'/order/{order_id}'
hdrs = build_l2_headers(api_key, secret, passphrase,
                        method='DELETE', path=path)
r = requests.delete('https://clob.polymarket.com' + path,
                    headers=hdrs, timeout=5)
```

L2 creds получаются один раз через `Scripts/poly_derive_api_creds.py --bot botN` (EIP-712 `ClobAuthDomain`, version='1' — это **отдельный** домен от Order, не путать).

---

## Pre-real-mode чек-лист (per bot)

1. **`Scripts/polymarket_approve.py --bot botN`**:
   - проверяет USDC.e баланс → `approve(USDC.e, onramp)` → `Onramp.wrap(amount)` → pUSD на кошельке
   - `approve(pUSD, exchange_standard)` MAX_UINT256
   - `approve(pUSD, exchange_negRisk)` MAX_UINT256
   - `CTF.setApprovalForAll(exchange_standard, true)`
   - `CTF.setApprovalForAll(exchange_negRisk, true)`
2. **`Scripts/poly_derive_api_creds.py --bot botN`**: дописывает `BOT{N}_POLY_API_KEY/SECRET/PASSPHRASE` в `Credentials.env`.
3. **Smoke**: GET `/data/positions?user={addr}` с L2 headers — должен вернуть `200 []` (пустые позиции).
4. **DRY_RUN=0** только после паперов 100 сделок и валидной аналитики (см. `Scripts/analytics.py`).

## Распространённые ошибки

- **400 invalid signature** → проверь правильность домена (negRisk vs standard) и что `signer == maker == eth_address`.
- **400 price not on tick** → `_round_to_tick(price, market.tick_size)`. Default tick 0.01, но sport markets часто 0.001.
- **400 insufficient allowance** → re-run `polymarket_approve.py`. Allowance = MAX_UINT256, иначе одна сделка съедает.
- **403 unauthorized на cancel/positions** → L2 timestamp drift > 60s; пересинхронизируй системные часы.
- **Order поставился но не матчится** → `feeRateBps` устарел. Пере-fetch `/markets/{condition_id}` каждые 5 мин или per-trade.

## Связанные файлы

- `Scripts/executor/builders.py:294 build_poly_order` — production-grade signed builder.
- `Scripts/executor/builders.py:120 _sign_poly_eip712` — низкий уровень.
- `Scripts/polymarket_approve.py` — wrap+approve.
- `Scripts/poly_derive_api_creds.py` — L2 creds.
- `Scripts/poly_user_ws.py` — user-channel WS (HMAC auth).
- `Scripts/executor/bot_connector.py` — внешним ботам plain `place_order(...)` без deal/entries обёртки.
