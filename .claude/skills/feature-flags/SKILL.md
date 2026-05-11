---
name: feature-flags
description: |
  Feature flags / kill-switches for plan-kapkan. Currently we use binary
  env vars (ENABLE_LIMITLESS=0/1, DRY_RUN=0/1). This SKILL extends to
  graduated rollouts, runtime toggles without restart, and partial
  degradation patterns for when one platform breaks.
---

# feature-flags — управляемые переключатели функциональности

## Текущие флаги (из Credentials.env / env vars)

| Flag | Что делает | По умолчанию |
|---|---|---|
| `DRY_RUN` | Если `1`, executor не делает real POST /order | `1` |
| `ENABLE_POLY` | Polymarket fetch вкл/выкл | `1` |
| `ENABLE_KALSHI` | Kalshi fetch вкл/выкл | `0` (геоблок) |
| `ENABLE_SX` | SX Bet fetch вкл/выкл | `0` |
| `ENABLE_LIMITLESS` | Limitless fetch вкл/выкл | `0` (rate-limit) |
| `ENABLE_LIMITLESS_WS` | Limitless WebSocket вкл/выкл | `0` |
| `ASYNC_FETCH` | HTTP/2 + asyncio fetcher | `1` |
| `WALLET_BACKEND` | local / windows / aws | `local` |
| `GRADUATION_MIN_TRADES` | Сколько paper trades для gate | `50` |
| `MAX_WS_SUBS` | Лимит WS subs Polymarket | `1000` |
| `POLY_MAIN_PAGES` | Сколько страниц Polymarket /events | `4` |

## Уровни feature flags (от простого к сложному)

### Level 1: Binary env var (текущий)

```python
ENABLE_SX = os.environ.get('ENABLE_SX', '0') != '0'
if ENABLE_SX:
    do_sx_fetch()
```

✅ Плюсы: просто, зашито в startup, нет runtime overhead
❌ Минусы: для изменения — рестарт контейнера (теряется WS state, paper trading)

### Level 2: Runtime toggle через API endpoint

```python
# Scripts/feature_flags.py
import json
import os
from threading import Lock
from typing import Any

class FeatureFlags:
    """Persistent + runtime-mutable flags. State lives in Executions/flags.json."""
    PATH = 'Executions/flags.json'

    def __init__(self):
        self._lock = Lock()
        self._flags = self._load()

    def _load(self) -> dict:
        if os.path.exists(self.PATH):
            with open(self.PATH) as f:
                return json.load(f)
        return {}

    def _save(self):
        os.makedirs(os.path.dirname(self.PATH), exist_ok=True)
        with open(self.PATH, 'w') as f:
            json.dump(self._flags, f, indent=2)

    def get(self, name: str, default: Any = None) -> Any:
        with self._lock:
            return self._flags.get(name, os.environ.get(name, default))

    def set(self, name: str, value: Any):
        with self._lock:
            self._flags[name] = value
            self._save()
        print(f"[FLAGS] {name}={value}")

    def is_enabled(self, name: str, default: bool = False) -> bool:
        v = self.get(name, default)
        return v in (True, '1', 'true', 'TRUE', 'yes')

flags = FeatureFlags()
```

В коде:
```python
if flags.is_enabled('ENABLE_SX'):
    do_sx_fetch()
```

API endpoint:
```python
@app.route('/api/flags', methods=['GET'])
@require_auth
def get_flags():
    return jsonify(flags._flags)

@app.route('/api/flags/<name>', methods=['POST'])
@require_auth
def set_flag(name):
    """Toggle without restart. Body: {"value": true/false/<any>}"""
    value = request.json.get('value')
    flags.set(name, value)
    return jsonify({'ok': True, 'name': name, 'value': value})
```

UI: red-button + confirmation modal (как у kill switch):
```html
<button onclick="toggleFlag('ENABLE_SX')">SX Bet: <span id="flag-sx">ON</span></button>
<script>
async function toggleFlag(name) {
    if (!confirm(`Toggle ${name}?`)) return;
    const cur = await (await fetch(`/api/flags`)).json();
    const newVal = !cur[name];
    if (!confirm(`Set ${name} = ${newVal}?`)) return;
    await fetch(`/api/flags/${name}`, {method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({value: newVal})});
    location.reload();
}
</script>
```

### Level 3: Graduated rollout (для рискованных фич)

Пример: новая логика 3-way arb (для SX soccer):

```python
# Включить только для 10% deals (по hash от title)
import hashlib

def _rollout_pct(name: str, key: str) -> int:
    """Deterministic 0-100 based on flag name + key."""
    h = hashlib.md5(f"{name}:{key}".encode()).hexdigest()
    return int(h[:8], 16) % 100

if flags.is_enabled('THREE_WAY_ARB_BETA'):
    pct = flags.get('THREE_WAY_ARB_PCT', 0)  # 0..100
    if _rollout_pct('three_way_arb', deal['title']) < pct:
        # Этот deal попал в beta cohort
        deal = enhance_with_3way_logic(deal)
```

Оператор поднимает `THREE_WAY_ARB_PCT` от 0 → 5 → 25 → 100, наблюдая метрики.

### Level 4: Per-wallet / per-user flags

Когда у нас 6 ботов:
```python
flags.set('MAX_LEG_SIZE_USDC.bot1', 50)
flags.set('MAX_LEG_SIZE_USDC.bot2', 100)  # этот бот тестит больший размер
flags.set('MAX_LEG_SIZE_USDC.bot3', 50)

def get_wallet_flag(name: str, bot_id: str, default: Any = None) -> Any:
    return flags.get(f'{name}.{bot_id}', flags.get(name, default))
```

## Kill switches (особый класс flags)

Это flags, которые **выключают деньги**, поэтому:
- **Двойное подтверждение** в UI
- **Watchdog** проверяет файл-флаг каждую секунду (даже если main процесс упал)
- **Один направление**: kill быстрый, recovery — manual

```python
# Текущий kill switch
KILL_FLAG_PATH = 'Executions/.killed'

def is_killed() -> bool:
    return os.path.exists(KILL_FLAG_PATH)

@app.route('/api/kill', methods=['POST'])
@require_auth
def kill():
    """Kill switch — двойное подтверждение в UI."""
    confirm1 = request.json.get('confirm1')
    confirm2 = request.json.get('confirm2')
    if not (confirm1 and confirm2):
        return jsonify({'error': 'two confirmations required'}), 400
    with open(KILL_FLAG_PATH, 'w') as f:
        f.write(f"{int(time.time())}: killed via /api/kill\n")
    # Cancel all pending orders + log
    return jsonify({'ok': True, 'killed_at': time.time()})

@app.route('/api/unkill', methods=['POST'])
@require_auth
def unkill():
    """Manual recovery — оператор должен явно снять."""
    if os.path.exists(KILL_FLAG_PATH):
        os.unlink(KILL_FLAG_PATH)
    return jsonify({'ok': True})
```

## Feature flag governance

### Чек-лист на каждый flag

- [ ] **Назначение** документировано в коде / SKILL
- [ ] **Default** безопасный (фича OFF при старте)
- [ ] **Rollout plan** прописан (как переходим от 0 → 100)
- [ ] **Rollback** очевиден (просто перевернуть flag)
- [ ] **Sunset date** указана (когда удалять flag из кода)
- [ ] **Метрика** для измерения эффекта (есть в `observability-stack`)

### Регистр всех flags

Создать файл `Scripts/flags_registry.py`:
```python
"""Single source of truth for all feature flags.
Every flag should be registered here with metadata.
Loaders default to this registry if env not set."""

REGISTRY = {
    'ENABLE_LIMITLESS': {
        'description': 'Включить Limitless feed',
        'default': False,
        'owner': '@operator',
        'sunset': None,  # permanent flag
        'rollout': 'binary',  # 0/1
    },
    'THREE_WAY_ARB_PCT': {
        'description': '% deals processed through 3-way logic',
        'default': 0,
        'owner': '@dev',
        'sunset': '2026-06-30',  # удалить как только проверено
        'rollout': 'graduated',  # 0..100
    },
    # ...
}
```

## Anti-patterns

```python
# ❌ Flag в каждом if, нет регистра
if os.environ.get('FOO'):  # что такое FOO? кто owner?
    do_thing()

# ❌ Flag не имеет sunset — copy-paste в новый код через год
if flags.is_enabled('OLD_BEHAVIOR_2024'):
    legacy_path()  # ← мы уже в 2026, никто не помнит зачем

# ❌ Flag меняет данные нечётко (race condition)
if flags.is_enabled('USE_NEW_PRICE_LOGIC'):
    deal.price = new_calc(deal)
else:
    deal.price = old_calc(deal)
# ← если флаг переключился в середине scan'а, часть deals будет old, часть new

# ✅ Lock flag value at scan start
NEW_PRICE = flags.is_enabled('USE_NEW_PRICE_LOGIC')  # snapshot
for deal in deals:
    deal.price = new_calc(deal) if NEW_PRICE else old_calc(deal)
```

## Refs

- `deploy-pipeline/SKILL.md` — flag changes идут через тот же pipeline
- `circuit-breaker-patterns/SKILL.md` — CB это runtime feature flag (auto)
- `error-budget-policy/SKILL.md` — когда автоматически переключать flags
