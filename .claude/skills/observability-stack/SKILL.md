---
name: observability-stack
description: |
  Structured logging, metrics, and health-checking for plan-kapkan radar.
  Replace print() spaghetti with structured logs that operator can grep.
  Add Prometheus-style metrics so we know WHAT broke before we know WHY.
---

# observability-stack — видимость в production

## Где сейчас слепые зоны

| Что | Симптом | Почему слепо |
|---|---|---|
| 403 от Limitless | "арбы исчезли" | Тихий return None в fetcher |
| WS reconnect cycle | "subscribed to N tokens" в логе, но не видно если N упало | Нет метрики WS health |
| Cycle time spike | UI лагает | Нет /api/stats counter timeline |
| Memory growth | Контейнер OOM-killed через неделю | Нет per-loop memory tracking |
| Reconcile mismatch | Risk halt'нулся, но без причины | Нет разбора что не сошлось |

## Минимальный stack для нашего проекта

1. **Structured logging** через `logging` (stdlib) или `loguru`
2. **Prometheus metrics** через `prometheus_client` + `/metrics` endpoint
3. **Healthcheck endpoint** `/api/health` для Docker healthcheck + uptime monitor
4. **Error rate alerts** через простой Python timer

## 1. Structured logging (вместо print)

```python
# Scripts/logging_setup.py — новый файл
import logging
import json
import sys
from datetime import datetime, timezone

class JSONFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            'ts': datetime.now(timezone.utc).isoformat(),
            'level': record.levelname,
            'logger': record.name,
            'msg': record.getMessage(),
        }
        if record.exc_info:
            payload['exc'] = self.formatException(record.exc_info)
        # Custom fields from extra={...}
        for k in ('host', 'http_status', 'arb_id', 'wallet', 'cycle_ms'):
            if k in record.__dict__:
                payload[k] = record.__dict__[k]
        return json.dumps(payload, ensure_ascii=False)

def setup_logging(level=logging.INFO):
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
```

В коде:
```python
# Вместо:
print(f"[FETCH] Poly={n} ({elapsed:.1f}s)")

# Использовать:
log = logging.getLogger('fetch')
log.info('fetch_complete', extra={
    'host': 'polymarket',
    'count': n,
    'cycle_ms': int(elapsed * 1000),
})
# → в логе: {"ts":"...","level":"INFO","logger":"fetch","msg":"fetch_complete","host":"polymarket","count":2000,"cycle_ms":6200}
```

Профит: оператор делает `docker logs ... | grep '"host":"limitless"' | jq '.cycle_ms'` и видит чистую timeline.

## 2. Prometheus metrics

```python
# Scripts/metrics.py — новый файл
from prometheus_client import Counter, Histogram, Gauge, generate_latest

# Counters — что произошло
fetch_total = Counter('radar_fetch_total', 'Total fetches', ['host', 'status'])
arbs_detected_total = Counter('radar_arbs_detected_total', 'Arbs found',
                               ['platform', 'structure'])
fires_total = Counter('radar_fires_total', 'Fire attempts',
                      ['result'])  # success / aborted / error

# Histograms — распределения
fetch_duration_seconds = Histogram('radar_fetch_duration_seconds',
                                    'Fetch latency', ['host'],
                                    buckets=(0.5, 1, 2, 5, 10, 30, 60, 120))

# Gauges — текущее значение
deals_open_count = Gauge('radar_deals_open_count', 'Open deals', ['platform'])
ws_subs_count = Gauge('radar_ws_subs_count', 'Active WS subscriptions', ['platform'])
circuit_state = Gauge('radar_circuit_state', 'Circuit breaker state',
                      ['host'])  # 0=closed, 1=open, 2=half_open

# Использование:
from metrics import fetch_total, fetch_duration_seconds
with fetch_duration_seconds.labels(host='limitless').time():
    resp = await client.get(...)
    fetch_total.labels(host='limitless', status=resp.status_code).inc()
```

В `arb_server.py`:
```python
@app.route('/metrics')
def prometheus_metrics():
    from metrics import generate_latest
    return generate_latest(), 200, {'Content-Type': 'text/plain'}
```

## 3. Healthcheck endpoint

```python
@app.route('/api/health')
def healthcheck():
    """Used by Docker HEALTHCHECK + UptimeRobot/Healthchecks.io."""
    import time
    checks = {
        'gunicorn': True,  # If we can answer, gunicorn is alive
        'last_scan_age_s': time.time() - scan_data.get('last_complete', 0),
        'wallet_pool_loaded': len(_wallet_pool.wallets) > 0,
        'paper_trading_writes': os.path.exists(PAPER_RESULTS_PATH),
    }
    healthy = (
        checks['last_scan_age_s'] < 120  # scan не старше 2 мин
        and checks['wallet_pool_loaded']
    )
    return jsonify({**checks, 'status': 'ok' if healthy else 'degraded'}), \
           (200 if healthy else 503)
```

В `docker-compose.yml`:
```yaml
services:
  radar:
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:5050/api/health"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 60s
```

## 4. Error rate alerts (без внешних сервисов)

```python
# Scripts/alerts.py — простой rate watcher
from collections import deque
import time
from threading import Lock

class ErrorRateWatcher:
    def __init__(self, threshold_per_min=10, window_seconds=60):
        self.threshold = threshold_per_min
        self.window = window_seconds
        self.events = deque()
        self.lock = Lock()
        self.alerted_until = 0  # unix ts; suppress dupes within 5 min

    def record_error(self, kind, detail):
        with self.lock:
            now = time.time()
            self.events.append((now, kind, detail))
            # Clean up old events
            while self.events and self.events[0][0] < now - self.window:
                self.events.popleft()
            if len(self.events) >= self.threshold and now > self.alerted_until:
                self.alerted_until = now + 300  # 5 min suppression
                self._trigger_alert(len(self.events), kind)

    def _trigger_alert(self, count, latest_kind):
        # Phase: send to Telegram, OR write to alerts.log
        from telegram_alerts import send_alert  # see other skill
        send_alert(f"⚠ Error rate: {count} errors/{self.window}s, latest={latest_kind}")
```

Использовать в fetcher:
```python
from alerts import ErrorRateWatcher
_LIM_ERRORS = ErrorRateWatcher(threshold_per_min=20)

async def fetch_limitless_orderbook_async(...):
    try:
        ...
    except Exception as e:
        _LIM_ERRORS.record_error('limitless_fetch', str(e))
        raise
```

## Как читать логи в production

```bash
# Tail с фильтром по платформе
docker logs plan-kapkan-radar --since=10m -f 2>&1 | grep '"host":"limitless"'

# Распределение error codes за час
docker logs plan-kapkan-radar --since=1h 2>&1 | \
  grep '"level":"ERROR"' | \
  jq -r '.http_status // "no_status"' | \
  sort | uniq -c

# Cycle time timeline (median + p95)
docker logs plan-kapkan-radar --since=10m 2>&1 | \
  grep '"msg":"fetch_complete"' | \
  jq '.cycle_ms' | \
  sort -n | \
  awk '{a[NR]=$1} END {print "p50:", a[int(NR*0.5)], "p95:", a[int(NR*0.95)]}'

# Все WS reconnects
docker logs plan-kapkan-radar --since=24h 2>&1 | grep 'reconnect\|backoff'
```

## SLO targets (для radar)

| Metric | Target | Alert at |
|---|---|---|
| `/api/health` 200 rate | 99.5% | <99% |
| Cycle time p95 | <30s | >60s |
| Polymarket fetch errors | <1% | >5% |
| Limitless 403 rate | <0.1% | >1% |
| Wallet `can_sign` count | >=3 | <3 |
| Open deals (sum) | any | unchanged for 30 min — possible freeze |

## Refs

- `circuit-breaker-patterns/SKILL.md` — partner для error handling
- `error-budget-policy/SKILL.md` — как targets превратить в decisions
- `deploy-pipeline/SKILL.md` — где это интегрируется в смок-тест
