---
name: time-freshness-validation
description: |
  Detect and reject stale/zombie/phantom data based on TIME validity.
  Operator hit this pattern 4+ times in one session: post-resolve markets
  leaking into NEAR, stale `closed=false` flags, cached orderbook returning
  resolved-event prices, price drift between scan and fire. Every external
  feed needs explicit time-based freshness gates — server-provided flags
  alone are NOT enough.
---

# time-freshness-validation — защита от stale/zombie данных

## Когда применять

ВСЕГДА, для ЛЮБЫХ внешних источников данных в plan-kapkan:
- Polymarket gamma-api `/events` (зомби-флаг `closed=false` после резолва)
- Polymarket clob-api orderbook (stale asks для резолвенных markets)
- Limitless `/markets/active`
- Kalshi `/markets`
- SX Bet `/markets/active`
- WebSocket book caches
- Cached meta info (`_fetch_poly_market_info`, `_fetch_limitless_market_meta`)

## Real-world инциденты (что уже произошло)

### Инцидент 1: Phase 9yy (29.04.2026) — El Gouna SC phantom
Резолвенный матч El Gouna SC показал sum=84-90¢ в NEAR. Orderbook остался активен 6-12h во время UMA dispute. **Fix**: дроп `closed=true`/`archived=true`.

### Инцидент 2: 30.04.2026 — Highest temperature in Munich/Lagos/Singapore
gamma-api возвращал `closed=false` через **5+ часов** после резолва (event endDate = 12:00 UTC, scan time = 17:46 UTC). Phase 9yy фильтр НЕ ловил, потому что `closed=false`. **Fix (Phase 9kkk #41)**: explicit endDate arithmetic, 60min grace.

### Инцидент 3: BTC/ETH Up or Down 5-min events
5-минутные события резолвятся, gamma stays `closed=false` 1-3 минуты, цены в orderbook становятся meaningless. Появляются как фантомные арбы 65-95¢.

### Инцидент 4: `'implied'` price source (Phase 9kkk #38)
Когда orderbook пуст на одной ноге, fallback на `lastTradePrice` — этот price может быть **из недели назад**. Stale time, но в коде нет timestamp. **Fix**: drop deals с source != real_orderbook.

## Канонический паттерн: 4-уровневая freshness gate

```python
# Scripts/freshness.py — рекомендуемая централизация
from datetime import datetime, timezone
from typing import Optional


def is_fresh_event(
    end_date_iso: Optional[str],
    *,
    grace_seconds: int = 3600,  # 60min UMA window для prediction markets
    max_future_seconds: Optional[int] = None,
) -> tuple[bool, str]:
    """4-уровневый check для events с известным endDate.
    Returns (is_fresh, reason).
    """
    if not end_date_iso:
        return False, "no_end_date"
    try:
        ed = end_date_iso[:-1] + '+00:00' if end_date_iso.endswith('Z') else end_date_iso
        if len(ed) == 10:
            ed += 'T00:00:00+00:00'
        end_dt = datetime.fromisoformat(ed)
    except (TypeError, ValueError):
        return False, "unparseable_end_date"
    if not end_dt.tzinfo:
        end_dt = end_dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    age_seconds = (now - end_dt).total_seconds()

    # Level 1: уже резолвлен (с grace window для UMA disputes)
    if age_seconds > grace_seconds:
        return False, f"past_resolve_+{age_seconds/60:.0f}min"

    # Level 2: too far in future (capital lockup)
    if max_future_seconds is not None and age_seconds < -max_future_seconds:
        return False, f"too_far_future_-{-age_seconds/86400:.1f}d"

    return True, "fresh"


def is_fresh_orderbook(
    fetched_at_unix: float,
    *,
    max_age_seconds: int = 30,
) -> tuple[bool, str]:
    """Orderbook freshness — Cloudflare кэширует на 30s, beyond that stale."""
    age = max(0, datetime.now().timestamp() - fetched_at_unix)
    if age > max_age_seconds:
        return False, f"orderbook_stale_{age:.0f}s"
    return True, "fresh"


def is_fresh_meta_cache(
    cached_at_unix: float,
    *,
    max_age_seconds: int = 600,  # 10min default
) -> tuple[bool, str]:
    """Meta (tick/min/fee) cache — обновлять каждые 10min или при mismatch."""
    age = max(0, datetime.now().timestamp() - cached_at_unix)
    if age > max_age_seconds:
        return False, f"meta_stale_{age:.0f}s"
    return True, "fresh"
```

## Где интегрировать в plan-kapkan

| Место | Текущее состояние | Должно быть |
|---|---|---|
| `filter_poly` | ✅ Phase 9kkk #41 — past_resolve_60min | Использовать `is_fresh_event` |
| `filter_limitless` | Не проверяет endDate explicitly | Добавить `is_fresh_event` |
| `eval_kalshi` | Использует `is_within_10_days` | Добавить strict past check |
| `eval_sx` | Phase 9kkk: status != 1 + outcome != 0 | Хорошо, плюс is_fresh_event |
| `near_summary` | ✅ Phase 9kkk #41 — past_resolve в poly_near | Применить ко всем платформам |
| `_fetch_poly_market_info` cache | TTL 10min hardcoded | OK |
| `_fetch_limitless_market_meta` cache | TTL — проверить | Should be 10min |
| `poly_clob_cache` | Не таймштампит | Добавить `last_updated` per token |
| `ws_books` | Не таймштампит | Добавить `received_at` per token |

## 5 правил (must follow)

### 1. Server flags ≠ truth
`closed=false` от gamma-api НЕ означает "событие активно". Всегда проверяй endDate явно.

```python
# ❌ ПЛОХО
if not ev.get('closed'):
    process(ev)

# ✅ ХОРОШО
fresh, reason = is_fresh_event(ev.get('endDate'))
if not fresh:
    diag[f'skip_{reason}'] += 1
    continue
```

### 2. UMA dispute grace ≠ infinite
Polymarket UMA disputes длятся 6-12h максимум. После 12h любой ask = phantom. Grace = 60min консервативно.

### 3. Orderbook cache TTL = Cloudflare TTL
CF кэширует Polymarket orderbook на 30s. Не доверяй cached данным старше 30s — пересинхронизируйся.

### 4. Different markets need different `past_days`
- Daily-resolve elections: 48h grace OK
- Sport events: 12h grace OK
- **Time-of-day events** (temp 12:00, BTC 5-min): **60min grace MAX**
- Threshold series: те же что parent

### 5. Stale meta cache vs API drift
`taker_fee_bps` может поменяться между scan и fire (governance). 10min cache TTL + revalidate-on-fire-attempt.

## Anti-patterns (что мы делали не так)

```python
# ❌ Анти-паттерн 1: silently fall back to lastTradePrice
yes_price = best_ask if best_ask else outcomes_prices[0]  # ← stale!

# ❌ Анти-паттерн 2: trust closed flag without endDate check
if ev.get('closed'): skip
# (что если closed=false но endDate в прошлом?)

# ❌ Анти-паттерн 3: WINDOW_PAST_DAYS one-size-fits-all
WINDOW_PAST_DAYS = 2  # для elections OK, для temp events КАТАСТРОФА

# ❌ Анти-паттерн 4: cache без TTL meta
meta = _meta_cache.get(slug) or fetch_meta(slug)
# (если slug сохранён 6 часов назад — fee мог измениться)
```

## Test patterns

```python
# tests/test_freshness.py
from freshness import is_fresh_event
import datetime

def test_past_endDate_with_grace():
    past = (datetime.datetime.now(datetime.timezone.utc) -
            datetime.timedelta(minutes=30)).isoformat()
    fresh, reason = is_fresh_event(past, grace_seconds=3600)
    assert fresh, f"30min past should pass grace: {reason}"

def test_past_endDate_beyond_grace():
    past = (datetime.datetime.now(datetime.timezone.utc) -
            datetime.timedelta(hours=2)).isoformat()
    fresh, reason = is_fresh_event(past, grace_seconds=3600)
    assert not fresh, f"2h past should fail: got fresh={fresh}"
    assert "past_resolve" in reason

def test_no_end_date():
    fresh, reason = is_fresh_event(None)
    assert not fresh
    assert reason == "no_end_date"

def test_unparseable():
    fresh, reason = is_fresh_event("garbage")
    assert not fresh
    assert reason == "unparseable_end_date"

def test_legitimate_future():
    future = (datetime.datetime.now(datetime.timezone.utc) +
              datetime.timedelta(days=3)).isoformat()
    fresh, reason = is_fresh_event(future)
    assert fresh
```

## Monitoring / observability

В `/api/stats` должны быть счётчики:
- `poly_skip_past_resolve` ← Phase 9kkk #41 (ready)
- `poly_skip_no_window` (existing)
- `lim_skip_past_resolve` (TODO)
- `kalshi_skip_past_resolve` (TODO)
- `sx_skip_past_resolve` (TODO)

Telegram alert если `*_skip_past_resolve > 100/scan` — значит API ведёт себя странно.

## Refs

- `circuit-breaker-patterns/SKILL.md` — partner для CF rate-limit
- `observability-stack/SKILL.md` — где счётчики живут
- `Scripts/arb_server.py:533` — `is_within_window` существующая функция
- `Scripts/arb_server.py:filter_poly` — Phase 9kkk #41 implementation
- Phase 9yy commit history — initial phantom filter
