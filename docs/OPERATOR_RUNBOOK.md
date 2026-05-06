# Operator runbook — состояния, тюнинг, диагностика

Краткий справочник по тому что обычно встречается на дашборде и в логах. Дополняй по мере появления новых паттернов.

## 1. Полностью пустой дашборд (HOT 0 / NEAR 0 / Deals 0)

**Причины и проверки:**

| Причина | Проверка |
|---|---|
| Сканер только запустился (warm-up ~30-60s) | `docker logs --tail 30 plan-kapkan-radar` — должны идти строки про subscribed tokens |
| Все три API вернули 0 markets (партнёрский outage) | `docker exec plan-kapkan-radar python3 -c "import requests; print(requests.get('https://gamma-api.polymarket.com/events?limit=1').status_code)"` |
| Kill switch активен | в `Executions/.killed` файл существует. Снять: `rm Executions/.killed` |
| `ENABLE_STRUCT_*` все = 0 в env | проверить `Credentials.env` — `ENABLE_STRUCT_A/B/C` |

**Норма:** 5-50 candidates в HOT pool, 50-300 в NEAR. Полностью пустой пул > 5 минут после рестарта = что-то сломалось.

## 2. История сделок забивается одинаковыми deals

**Это нормально** — каждый scan-cycle пишет одну и ту же активную сделку как `opened` event в `analytics_events.jsonl`. На дашборде «История сделок» показывает все scan-events. Реальное количество уникальных арбов ≪ количества строк.

Чтобы посмотреть реально-уникальные deals:
```bash
docker exec plan-kapkan-radar python3 -c "
import json
seen = set()
for line in open('/app/Executions/dryrun.jsonl'):
    try:
        r = json.loads(line)
        if r.get('arb_id') and r.get('arb_id') not in seen:
            seen.add(r['arb_id'])
            print(r['arb_id'], r.get('title','?'))
    except: pass
"
```

## 3. paper_stats count = 0 даже спустя сутки

**Проверки:**

| Что | Проверка | Норма |
|---|---|---|
| Активные fire'ы | `cat Executions/dryrun.jsonl \| wc -l` | растёт каждый scan-cycle |
| Сколько rejected | `grep -c '"rejected"' Executions/paper_results.jsonl` | большая часть — это OK |
| Сколько realistic_fill | `grep -c '"realistic_fill": [^n]' Executions/paper_results.jsonl` | редко но должны быть |

paper_stats считает только сделки которые **прошли** все фильтры и были «исполнены» в dry-run sense (с `realistic_fill: <number>`). Если все rejected — значит на текущих рынках реальных арбов нет, ждём.

**Если хочется ускорить накопление статистики** — можно временно ослабить `_quality_ok` через env (Phase 19v31):
```bash
# В Credentials.env:
QUALITY_TIGHT_MIN_LIQ=200       # было 600 default
QUALITY_LIM_TIGHT_MIN_LIQ=50    # было 130
QUALITY_TIGHT_MAX_SLIP=0.5      # было 0.3
QUALITY_TIGHT_CUTOFF_CENTS=98   # было 95 — гейт срабатывает только на ≥98¢

docker restart plan-kapkan-radar
```
Это **не** делает сделки лучше, просто пропускает больше потенциальных кандидатов в paper-trading evaluator. После 50 trades graduation gate всё равно требует ≥70% win rate и ≤20% drift — фейки отсеются.

## 4. Polymarket NEAR pool выкидывает события: `poly_strict_all_implied=1`

**Это observability-only, не bug.**

NEAR pool классификация требует чтобы хотя бы одна нога была **не** `implied` (т.е. была реальная котировка из orderbook). Если у Polymarket event'а **все** outcomes имеют price только через `outcomePrices` (стале snapshot из gamma-api), а свежий `/book?token_id=` вернул пусто → событие помечается `poly_strict_all_implied=1` и не promote'ится в HOT.

Phase 19v26 добавил **just-in-time re-fetch** для до 8 missing tokens прямо при HOT classification — это смягчает редкий race между scan-time и /api/near time. Но если orderbook реально пустой (нет активных market makers) — событие остаётся в NEAR observability.

**Когда волноваться:** если **все** Polymarket events помечены `poly_strict_all_implied` ≥ 30 минут — значит CLOB endpoint лёг или token_ids протухли. Лог будет полон `_fetch_clob` 5xx ошибок.

## 5. SX Bet никогда не достигает HOT/NEAR

**Это рыночная реальность, не bug.**

SX Bet markets имеют **широкие спреды** (~5-15¢ на 1X2 outcomes) потому что MM-ов мало и они квотят с большим запасом. Sum YES + NO ≈ 1.05-1.15 вместо 1.00 на ликвидных событиях. Это **выше** наших порогов (`THRESH_SX = 0.97-0.98`), поэтому ALL_YES / YES_NO_PAIR per-market структуры на SX никогда не срабатывают.

**Реалистичный edge SX:**
- Cross-platform (Polymarket+SX или Limitless+SX) на тех же фикстурах — **есть**, через `cp_complement_cover` (Phase 19v29b)
- Per-platform per-market spread — **нет**

Если хочешь убедиться что SX вообще функционирует:
```bash
curl -s 'https://api.sx.bet/markets/active?leagueId=1631&betGroup=1X2' | python -c "import json,sys; d=json.load(sys.stdin); print(len(d.get('data', {}).get('markets', [])), 'markets')"
```

## 6. Известные классы phantom-deals и как они закрыты

| Phantom | Phase | Закрыт |
|---|---|---|
| Halftime vs full-match cross-platform | v28 | scope guard (`detect_market_scope` + `scopes_compatible`) |
| Cross-team cross-platform (Santa Fe+Corinthians, Rivadavia+Fluminense) | v29a | `outcomes_compatible` 4-уровневое сравнение |
| Threshold-series ALL_YES (Reddit DAUq «above N») | 9o | `is_threshold_series` parent-title regex |
| Threshold-series ALL_YES (SOL «above $X», title без comparator) | v30 | child-slug-based detection в `is_threshold_series` |
| Mosquito arbs (sum < 50¢) | 19v10 | `_CP_MIN_REALISTIC_SUM` |
| Past-resolved Limitless events | 19v17 | adaptive grace gate |
| Limitless «Other» hidden outcome | 9k | `OTHER_RE` filter |

Если на дашборде появляется **новый** «Net > 10%» deal который не из этих категорий — скорее всего новый класс phantom. Проверь leg-by-leg через `dryrun.jsonl` и пингани разработчика.

## 7. Команды быстрой диагностики

```bash
# Текущий commit на VPS
docker exec plan-kapkan-radar git log --oneline -1

# Что сейчас в HOT pool
curl -s http://localhost:5050/api/pool_visible | python -m json.tool | head -40

# Active deals
curl -s http://localhost:5050/api/deals | python -m json.tool | head -50

# Risk snapshot
curl -s http://localhost:5050/api/risk_status | python -m json.tool

# Последние 20 строк radar log
docker logs --tail 20 plan-kapkan-radar

# Reset analytics + dryrun + paper_results (после deploy большого фикса)
curl -X POST http://localhost:5050/api/analytics/reset
```

## 8. Когда ничего не помогает — clean restart

```bash
cd ~/plan-kapkan
git pull
docker compose down
docker compose up -d --build
docker logs -f plan-kapkan-radar  # смотрим warm-up
```

Это пересобирает image и стартует с нуля. Анализ при этом **не** теряется — `Executions/` смонтирован как volume, файлы выживают между рестартами. Терять реально ничего: jsonl растут до бесконечности, периодически их можно ротировать (но не критично).
