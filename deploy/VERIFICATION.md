# VERIFICATION — Phase 5 baseline (read-only)

5 диагностических curl с VPS — выполнить **до** деплоя для baseline metrics.

Все тесты read-only, не трогают paper trading.

## Запуск

```bash
ssh arb@77.91.97.22 << 'EOF'
set +e

# TEST 1: Limitless reachability
echo "=== TEST 1: Limitless reachability ==="
curl -sIS --max-time 15 -H "User-Agent: plan-kapkan-test" \
  "https://api.limitless.exchange/markets/active?limit=1&page=1" | head -10

echo
# TEST 2: SX Bet geo-block
echo "=== TEST 2: SX Bet geo-block ==="
curl -sIS --max-time 15 -H "User-Agent: plan-kapkan-test" \
  "https://api.sx.bet/markets/active?onlyMainLine=true&pageSize=1" | head -10

echo
# TEST 3: HTTP/2 negotiation
echo "=== TEST 3: HTTP/2 negotiate ==="
curl -sS -o /dev/null -w "version=%{http_version} http=%{http_code} time=%{time_total}s\n" \
  --max-time 10 \
  "https://api.limitless.exchange/markets/active?limit=1&page=1"

echo
# TEST 4: Concurrency burst (60 parallel)
echo "=== TEST 4: 60 concurrent burst ==="
T0=$(date +%s.%N)
for i in $(seq 1 60); do
  curl -sS -o /dev/null --max-time 30 \
    -H "User-Agent: plan-kapkan-test" \
    "https://api.limitless.exchange/markets/active?limit=1&page=$((i % 10 + 1))" &
done
wait
T1=$(date +%s.%N)
echo "60 concurrent took $(echo "$T1 - $T0" | bc -l)s"

echo
# TEST 5: SX Bet types catalog (drift check)
echo "=== TEST 5: SX type-catalog ==="
curl -sS --max-time 15 -H "User-Agent: plan-kapkan-test" \
  "https://api.sx.bet/markets/active?onlyMainLine=true&pageSize=100" | \
python3 -c "
import sys, json
d = json.load(sys.stdin)
markets = d.get('data', {}).get('markets', [])
from collections import Counter
types = Counter(m.get('type') for m in markets)
print(f'  total: {len(markets)} markets, {len(types)} unique types')
for t, c in types.most_common(10):
    print(f'    type={t}: {c}')
"
EOF
```

## Эталонные значения (baseline 30.04.2026)

Чтобы знать что **healthy**:

| Test | Expected |
|---|---|
| 1. Limitless | `HTTP/2 200`, `cf-cache-status: HIT` |
| 2. SX Bet | `HTTP/2 200` (если 403 → VPS забанен) |
| 3. HTTP/2 negotiate | `version=2`, `http=200`, `time<0.2s` |
| 4. 60 concurrent | < 5 секунд total. Если >10s — Cloudflare стал throttle'ить |
| 5. SX types | type=1 (3-way soccer) ~45% — норма; type=52 (DNB) ~25%; type=2,3 (Total/Spread) — рестт |

## Если что-то отклоняется

| Симптом | Причина | Действие |
|---|---|---|
| Test 1 → 403 | Cloudflare adaptive block нашему VPS | Подождать 1-24h, circuit breaker (Phase 9kkk) защитит |
| Test 2 → 403 | SX Bet geo-block | Не включать `ENABLE_SX=1` пока не разрешится |
| Test 3 → version=1.1 | curl на VPS не имеет h2 | `apt install curl` или ставит `--http2-prior-knowledge` |
| Test 4 → >10s | CF throttle / network deg. | Не паниковать, но не запускать ENABLE_LIMITLESS=1 пока |
| Test 5 → новые type IDs | SX добавил рынки, не в SX_BINARY_TYPES | Добавить в `arb_server.py:274-301`, потерянный потенциал |

## Запуск через ScP (без SSH login)

```bash
scp deploy/VERIFICATION_runner.sh arb@77.91.97.22:/tmp/
ssh arb@77.91.97.22 "bash /tmp/VERIFICATION_runner.sh"
```

## История запусков

| Дата | Результат | Заметки |
|---|---|---|
| 2026-04-30 | All ✅ | Phase 9kkk pre-deploy baseline. 60 concurrent = 2.7s. |
