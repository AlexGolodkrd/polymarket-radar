---
name: circuit-breaker-patterns
description: |
  Circuit breaker pattern for graceful degradation when external APIs
  (Limitless, SX Bet, Polymarket) start rate-limiting or 403'ing. Prevents
  silent death-spirals where the radar keeps hammering a broken endpoint.
  Use this for any external HTTP fetcher.
---

# circuit-breaker-patterns — защита от каскадных сбоев

## Когда применять

- Любой fetcher hitting external API: `_fetch_limitless_orderbook`, `_fetch_sx_orders`, `_fetch_clob` (Polymarket)
- Любой WS клиент с reconnect логикой
- Любой outbound HTTP с возможным 403/429/502/503

## Базовый паттерн (3-state machine)

```
   ┌──────────┐  3+ consecutive errors   ┌────────┐
   │  CLOSED  │ ────────────────────────▶│  OPEN  │
   │ (normal) │                           │(blocked)│
   └──────────┘                           └────────┘
        ▲                                      │
        │                                      │ cool_down_seconds
        │                                      ▼
        │  1 success                    ┌────────────┐
        └─────────────────────────── ───│ HALF_OPEN  │
                                         │ (probing)  │
                                         └────────────┘
```

- **CLOSED** — всё работает, запросы идут
- **OPEN** — последние N запросов фейлились, не пускаем больше, возвращаем cached/empty
- **HALF_OPEN** — прошёл cool_down, пускаем 1 пробный, если success → CLOSED, если fail → снова OPEN

## Реализация в plan-kapkan (рекомендуемая)

```python
# Scripts/circuit_breaker.py — новый файл
import time
from enum import Enum
from threading import Lock

class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

class CircuitBreaker:
    """Per-host circuit breaker. Thread-safe.

    Usage:
        cb = CircuitBreaker(host='limitless', failure_threshold=3,
                            cool_down_seconds=300, success_threshold=2)
        if cb.allow():
            try:
                response = requests.get(...)
                cb.on_success()
            except (RequestException, HTTPError) as e:
                cb.on_failure(reason=str(e))
                return None  # graceful degradation
    """
    def __init__(self, host, failure_threshold=3, cool_down_seconds=300,
                 success_threshold=2):
        self.host = host
        self.failure_threshold = failure_threshold
        self.cool_down = cool_down_seconds
        self.success_threshold = success_threshold
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._opened_at = None
        self._lock = Lock()

    def allow(self) -> bool:
        with self._lock:
            now = time.time()
            if self._state == CircuitState.CLOSED:
                return True
            if self._state == CircuitState.OPEN:
                if now - self._opened_at >= self.cool_down:
                    self._state = CircuitState.HALF_OPEN
                    self._consecutive_successes = 0
                    print(f"[CB:{self.host}] OPEN → HALF_OPEN (cool_down done)")
                    return True
                return False
            if self._state == CircuitState.HALF_OPEN:
                return True

    def on_success(self):
        with self._lock:
            self._consecutive_failures = 0
            if self._state == CircuitState.HALF_OPEN:
                self._consecutive_successes += 1
                if self._consecutive_successes >= self.success_threshold:
                    self._state = CircuitState.CLOSED
                    print(f"[CB:{self.host}] HALF_OPEN → CLOSED (recovered)")

    def on_failure(self, reason=None):
        with self._lock:
            self._consecutive_failures += 1
            self._consecutive_successes = 0
            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                self._opened_at = time.time()
                print(f"[CB:{self.host}] HALF_OPEN → OPEN ({reason})")
            elif self._state == CircuitState.CLOSED:
                if self._consecutive_failures >= self.failure_threshold:
                    self._state = CircuitState.OPEN
                    self._opened_at = time.time()
                    print(f"[CB:{self.host}] CLOSED → OPEN: "
                          f"{self._consecutive_failures} consecutive failures "
                          f"({reason})")

    @property
    def state(self):
        return self._state.value

    def metrics(self):
        return {
            'host': self.host,
            'state': self._state.value,
            'consecutive_failures': self._consecutive_failures,
            'consecutive_successes': self._consecutive_successes,
            'opened_at': self._opened_at,
        }
```

## Интеграция в Limitless fetcher

```python
# Scripts/async_fetchers.py — обновить fetch_limitless_orderbook_async
from circuit_breaker import CircuitBreaker

_LIMITLESS_CB = CircuitBreaker(
    host='limitless',
    failure_threshold=3,
    cool_down_seconds=300,  # 5 min
    success_threshold=2,
)

async def fetch_limitless_orderbook_async(slug: str, ...):
    if not _LIMITLESS_CB.allow():
        # Circuit open — return cached or empty
        return _get_cached_or_empty(slug)
    try:
        client = _get_client('limitless')
        resp = await client.get(f"{BASE}/markets/{slug}/orderbook")
        if resp.status_code == 403:
            _LIMITLESS_CB.on_failure(reason='HTTP 403')
            return None
        if resp.status_code == 429:
            _LIMITLESS_CB.on_failure(reason='HTTP 429 rate-limited')
            return None
        resp.raise_for_status()
        _LIMITLESS_CB.on_success()
        return _parse_orderbook(resp.json())
    except (httpx.HTTPError, asyncio.TimeoutError) as e:
        _LIMITLESS_CB.on_failure(reason=str(e))
        return None
```

## Интеграция с радаром (status visibility)

```python
# В arb_server.py добавить endpoint:
@app.route('/api/circuit_breakers')
@require_auth
def api_circuit_breakers():
    from circuit_breaker import _LIMITLESS_CB, _SX_CB, _POLY_CB
    return jsonify({
        'limitless': _LIMITLESS_CB.metrics(),
        'sx': _SX_CB.metrics(),
        'polymarket': _POLY_CB.metrics(),
    })
```

И в `dashboard.html` показывать badge на каждой платформе:
```js
// Phase 9X — circuit breaker visibility
const cbState = await fetch('/api/circuit_breakers').then(r=>r.json());
['limitless', 'sx', 'polymarket'].forEach(p => {
    const el = document.getElementById(`badge-${p}`);
    el.textContent = cbState[p].state;
    el.style.background = cbState[p].state === 'closed' ? 'green'
                        : cbState[p].state === 'open'   ? 'red'
                        : 'orange';
});
```

## Параметры для разных API

| API | failure_threshold | cool_down | success_threshold | Reasoning |
|---|---|---|---|---|
| **Limitless** | 3 | 300s (5min) | 2 | API rate-limit'ит на >40 concurrent. 5min хватает чтобы окно закрылось |
| **SX Bet** | 5 | 60s | 1 | Live sport — нельзя долго ждать. Менее агрессивный |
| **Polymarket** | 5 | 60s | 1 | Cloudflare throttle, обычно быстро отпускает |
| **Polymarket WS** | 3 | 30s | 1 | Reconnect быстро |

## Anti-pattern: НЕ делать

```python
# ❌ ПЛОХО — silent retry на любую ошибку, без backoff
while True:
    try:
        resp = requests.get(url)
        return resp
    except:
        continue  # ← infinite loop!

# ❌ ПЛОХО — нет различия между transient (502) и permanent (403)
try:
    return requests.get(url)
except:
    return None  # ← теряем сигнал что 403 = persistent проблема
```

## Тестирование circuit breaker

```python
# tests/test_circuit_breaker.py
def test_breaker_opens_after_threshold():
    cb = CircuitBreaker('test', failure_threshold=3, cool_down_seconds=10)
    assert cb.allow()  # CLOSED
    cb.on_failure(); cb.on_failure(); cb.on_failure()
    assert not cb.allow()  # OPEN
    assert cb.state == 'open'

def test_breaker_recovers_via_half_open():
    cb = CircuitBreaker('test', failure_threshold=2, cool_down_seconds=0,
                        success_threshold=1)
    cb.on_failure(); cb.on_failure()  # OPEN
    time.sleep(0.01)
    assert cb.allow()  # HALF_OPEN
    cb.on_success()
    assert cb.state == 'closed'

def test_breaker_reopens_on_half_open_failure():
    cb = CircuitBreaker('test', failure_threshold=2, cool_down_seconds=0)
    cb.on_failure(); cb.on_failure()
    time.sleep(0.01)
    cb.allow()  # HALF_OPEN
    cb.on_failure()
    assert cb.state == 'open'  # снова OPEN
```

## Refs

- `http-rate-limiting/SKILL.md` — для exponential backoff (партнёр CB)
- `observability-stack/SKILL.md` — как лог'ировать state transitions
- `error-budget-policy/SKILL.md` — как параметры подбирать на основе SLO
