---
name: error-budget-policy
description: |
  SRE-style error budget policy for plan-kapkan radar. Defines SLOs, error
  budgets, and automatic decisions (auto-disable, alert, escalate). Helps
  decide "when do we panic" vs "when do we wait" without subjective gut feel.
---

# error-budget-policy — SLO и автоматические решения

## Зачем это нам

Сейчас оператор сам решает "Limitless пора отключать?" по интуиции:
- видит 403 в логах → беспокоится
- видит scan slow → думает "наверное надо что-то делать"

С error budget policy:
- **Конкретные числа**: "если 403 rate >5% за 5 минут → авто-disable"
- **Автоматизация**: машина сама делает что должна, оператор смотрит итог
- **Ясность**: при 4.9% оператор не паникует — еще в budget

## Базовые SLO для plan-kapkan

### SLO #1: Радар отвечает на API запросы

| Параметр | Target |
|---|---|
| **Что** | `/api/health`, `/api/deals`, `/api/analytics` отвечают 200 |
| **Window** | 30 дней |
| **SLO** | 99.5% запросов 200 (allowed: 0.5% errors = ~3.5 hours/month) |
| **Measurement** | Prometheus counter `radar_http_requests_total{status="2xx"}` / total |

### SLO #2: Latency скана

| Параметр | Target |
|---|---|
| **Что** | Полный scan cycle Polymarket |
| **Window** | rolling 1 hour |
| **SLO** | p95 < 30s, p99 < 60s |
| **Measurement** | `radar_scan_duration_seconds_bucket{platform="polymarket"}` |

### SLO #3: Limitless data freshness

| Параметр | Target |
|---|---|
| **Что** | `/markets/active` ответил 200 за последние N мин |
| **Window** | rolling 5 min |
| **SLO** | 95% запросов 200 (allowed: 5% errors) |
| **Measurement** | `radar_fetch_total{host="limitless",status="200"}` / total |

### SLO #4: Paper trading не пропускает арбы

| Параметр | Target |
|---|---|
| **Что** | Каждый detected arb попадает в `dryrun.jsonl` (success ИЛИ aborted с reason) |
| **Window** | per scan |
| **SLO** | 100% (нулевая толерантность) — silent drop = bug |
| **Measurement** | `len(scan_data['deals']) == lines_added_to_dryrun_log` |

### SLO #5: Wallet `can_sign` count

| Параметр | Target |
|---|---|
| **Что** | Сколько ботов готовы подписать tx |
| **SLO** | >= 3 (минимум для multi-leg arbs) |
| **Measurement** | `radar_wallet_can_sign_count` gauge |

## Error budget — как считать

```python
# Scripts/error_budget.py
from collections import deque
import time

class ErrorBudget:
    """Sliding window error budget.

    SLO 99.5% over 30 days → budget = 0.5%
    If we burn 0.5% budget in 7 days, that's 4x burn rate → RED.

    Burn rates:
      1x = exact SLO
      2x = exhausting 30-day budget in 15 days
      10x = exhausting in 3 days
      100x = exhausting in 7 hours
    """
    def __init__(self, slo_target=0.995, window_seconds=30*86400):
        self.slo = slo_target
        self.budget_pct = 1.0 - slo_target  # e.g., 0.005
        self.window = window_seconds
        self.events = deque()  # (ts, is_success: bool)

    def record(self, success: bool):
        now = time.time()
        self.events.append((now, success))
        # Trim old
        while self.events and self.events[0][0] < now - self.window:
            self.events.popleft()

    def metrics(self):
        if not self.events:
            return {'burn_rate': 0, 'budget_remaining_pct': 100}
        total = len(self.events)
        errors = sum(1 for _, s in self.events if not s)
        error_rate = errors / total
        burn_rate = error_rate / self.budget_pct  # 1x = exact SLO
        budget_consumed = min(100, burn_rate * 100)
        return {
            'total_events': total,
            'errors': errors,
            'error_rate_pct': round(error_rate * 100, 3),
            'burn_rate': round(burn_rate, 2),
            'budget_remaining_pct': round(100 - budget_consumed, 1),
        }
```

## Decision matrix (что делать при разных burn rate)

| Burn rate | Длительность | Action |
|---|---|---|
| **<1x** | — | Все ОК, ничего не делаем |
| **1x – 2x** | sustained 1h | Лог в alerts.log, оператор смотрит при следующей сессии |
| **2x – 10x** | sustained 30 min | Telegram alert, оператор смотрит ASAP |
| **10x – 100x** | sustained 5 min | **Auto-disable** платформы (через feature flag), Telegram + email |
| **>100x** | spike 1 min | **Auto-kill** + Telegram + SMS (экстренное) |

## Реализация для Limitless

```python
from error_budget import ErrorBudget
from feature_flags import flags

_LIMITLESS_BUDGET = ErrorBudget(slo_target=0.95, window_seconds=300)  # 5min window
                                  # 95% — более агрессивный SLO для rate-limit-heavy API

async def fetch_limitless_orderbook_async(slug):
    try:
        resp = await client.get(...)
        success = resp.status_code in (200, 304)
        _LIMITLESS_BUDGET.record(success)
        if not success:
            _check_burn_rate_and_act()
        return parse(resp) if success else None
    except Exception as e:
        _LIMITLESS_BUDGET.record(False)
        _check_burn_rate_and_act()
        return None

def _check_burn_rate_and_act():
    m = _LIMITLESS_BUDGET.metrics()
    if m['burn_rate'] > 100:
        # Нулевой budget за 1 минуту — немедленно
        flags.set('ENABLE_LIMITLESS', False)
        send_telegram_alert(f"🚨 Limitless auto-disabled: burn_rate={m['burn_rate']}x")
    elif m['burn_rate'] > 10:
        # 10x — pause + log
        send_telegram_alert(f"⚠ Limitless burn={m['burn_rate']}x, watching")
```

## Endpoint /api/error_budget (для оператора)

```python
@app.route('/api/error_budget')
@require_auth
def error_budget_status():
    return jsonify({
        'limitless': _LIMITLESS_BUDGET.metrics(),
        'sx': _SX_BUDGET.metrics(),
        'polymarket': _POLY_BUDGET.metrics(),
        'http_api': _HTTP_BUDGET.metrics(),
    })
```

В дашборде:
```js
const eb = await fetch('/api/error_budget').then(r=>r.json());
['limitless','sx','polymarket'].forEach(p => {
    const el = document.getElementById(`budget-${p}`);
    const m = eb[p];
    el.textContent = `${m.budget_remaining_pct}% (burn: ${m.burn_rate}x)`;
    el.style.color = m.burn_rate > 10 ? 'red'
                   : m.burn_rate > 2  ? 'orange'
                   : 'green';
});
```

## SLO review cadence

- **Weekly**: оператор смотрит `/api/error_budget` 1 раз в неделю
- **Monthly**: ретроспектива — какие SLO нарушены, корректируем targets
- **После инцидента**: обновляем SLO (если real-world показал что target нереалистичен) ИЛИ улучшаем код (если SLO правильный, но кода не хватает)

## Anti-patterns

```python
# ❌ SLO = 100% — нет места для real world
SLO_TARGET = 1.0  # любая ошибка = SLO violation = постоянный alarm

# ❌ Budget без window — вечная ошибка от месяц назад тянется
class BadBudget:
    def __init__(self): self.errors = 0
    def record_error(self): self.errors += 1  # ← никогда не уменьшается

# ❌ Auto-disable без cool-down — flapping
if errors > threshold:
    disable()
# ... 5 секунд позже:
if errors < threshold:
    enable()
# ← циклическое включение/выключение

# ✅ С hysteresis
if errors > 10 and not disabled:
    disable()
elif errors < 2 and disabled and (time.time() - disabled_at > 300):
    enable()  # должно быть стабильно <2 ошибок в течение 5 мин
```

## Refs

- `circuit-breaker-patterns/SKILL.md` — CB это локальный error budget (per-host)
- `observability-stack/SKILL.md` — где метрики живут
- `feature-flags/SKILL.md` — auto-disable использует flags
