# 📊 Анализ фильтрации арбитражных событий
**Дата:** 2026-04-26 09:59 UTC

---

## 1. Воронка фильтрации (Pipeline)

```
Этап 1: Все события на Polymarket          → 500 событий
   │ Фильтр: ≥ 2 рынков в событии
   ▼
Этап 2: Мульти-маркет события              → 355 событий (145 отсеяно)
   │ Фильтр: negRisk=true на ВСЕХ рынках
   ▼
Этап 3: Взаимоисключающие (negRisk)        → 195 событий (160 отсеяно)
   │ Фильтр: ≥ 2 исходов с ценой 0-1
   ▼
Этап 4: С ценами                           → 195 событий (0 отсеяно)
   │ Фильтр: rough sum < 98¢ (implied)
   ▼
Этап 5: Кандидаты (implied)                → 35 событий (160 отсеяно)
   │ Фильтр: не deadline-события
   ▼
Этап 6: Без дедлайнов                      → 35 событий
   │ Фильтр: CLOB best ask → sum < 95¢
   ▼
Этап 7: АРБИТРАЖ (порог 95¢)              → 1 событий
```

### Что отсеивает каждый фильтр:

| Фильтр | Описание | Отсеяно |
|--------|----------|---------|
| **negRisk** | Исходы должны быть взаимоисключающими (ровно 1 побеждает) | 160 |
| **rough sum ≥ 98¢** | Implied probability слишком высокая — нет окна | 160 |
| **deadline** | События с дедлайнами ("by January") — зависимые исходы | 0 |
| **CLOB sum ≥ 95¢** | Реальная ASK цена выше порога | 34 |

---

## 2. Отсеянные события (negRisk=false)

Топ-20 событий, отсеянных из-за negRisk=false (НЕ взаимоисключающие):

| # | Событие | Рынков | Спорт? |
|---|---------|--------|--------|
| 1 | MicroStrategy sells any Bitcoin by ___ ? | 4 |  |
| 2 | Kraken IPO by ___ ? | 4 |  |
| 3 | Macron out by...? | 3 |  |
| 4 | UK election called by...? | 4 |  |
| 5 | China x India military clash by...? | 3 |  |
| 6 | NATO/EU troops fighting in Ukraine by...? | 2 |  |
| 7 | Starmer out by...? | 7 |  |
| 8 | Ukraine recognizes Russian sovereignty over its territo | 3 |  |
| 9 | Ukraine election called by...? | 3 |  |
| 10 | Will any country leave NATO by...? | 3 |  |
| 11 | Ukraine election held by...? | 3 |  |
| 12 | Taylor Swift pregnant in 2025? | 3 |  |
| 13 | Mike Johnson out as Speaker by...? | 4 |  |
| 14 | What will happen before GTA VI? | 9 |  |
| 15 | Will OpenAI launch a consumer hardware product by...? | 3 |  |
| 16 | Will Russia capture Kostyantynivka by...? | 12 |  |
| 17 | Spain snap election called by...? | 2 |  |
| 18 | US x Russia military clash by...? | 4 |  |
| 19 | Will Russia invade a NATO country by...? | 2 |  |
| 20 | Trump eliminates capital gains tax on crypto by ___? | 2 |  |

**Всего отсеяно negRisk=false:** 160

---

## 3. «Почти арбитраж» — события 95-97¢ (CLOB)

Эти события НЕ прошли порог 95¢, но при пороге 97¢ прошли бы:

| # | Событие | CLOB sum | Исходов | Спорт? | Net profit при $100 |
|---|---------|----------|---------|--------|---------------------|
| 1 | Next James Bond actor? | 95.3¢ | 15 |  | $4.51 |
| 2 | Wisconsin Governor Election Winner | 96.0¢ | 2 |  | $3.84 |
| 3 | Alaska Governor Election Winner   | 96.3¢ | 17 |  | $3.55 |
| 4 | CA-27 House Election Winner | 96.6¢ | 2 |  | $3.26 |

**Всего при пороге 97¢:** 5 событий (vs 1 при 95¢)
**Дополнительных:** 4

### Выгодно ли заходить в 95-97¢?

При sum = 96¢, $100 баланс:
- Gross: $4.00
- Fee (Poly, с рибейтом): ~$1.20-1.50
- Slippage: ~$0.50-1.00
- **Net: $1.50-2.30** (ROI ~1.5-2.3%)

> ⚠️ При 96-97¢ чистая прибыль $1-2 на сделку. Риск проскальзывания может съесть весь профит.
> Рекомендация: заходить ТОЛЬКО при ликвидности > $1000 и slippage < 0.3%.

---

## 4. Спорт и Live события

### Спортивные события на Polymarket:
- Всего найдено: **38** спортивных событий
- Из них negRisk=true: **26**

| # | Событие | Рынков |
|---|---------|--------|
| 1 | 2026 NHL Stanley Cup Champion  | 32 |
| 2 | 2026 NBA Champion | 30 |
| 3 | 2026 FIFA World Cup Winner  | 60 |
| 4 | NBA Rookie of the Year  | 28 |
| 5 | NBA MVP  | 33 |
| 6 | NBA Eastern Conference Champion  | 16 |
| 7 | NBA Western Conference Champion  | 16 |
| 8 | UEFA Champions League Winner  | 60 |
| 9 | English Premier League Winner  | 25 |
| 10 | Serie A League Winner  | 25 |
| 11 | English Premier League – 2nd Place  | 25 |
| 12 | English Premier League – 3rd Place  | 25 |
| 13 | English Premier League – Last Place  | 25 |
| 14 | English Premier League - Top Goalscorer  | 58 |
| 15 | Bundesliga - Top Goalscorer  | 48 |

### Live-события:
Polymarket **НЕ имеет** специального тега "live" или "in-play" в API.
Спортивные события с датой матча = сегодня можно считать live, но Polymarket
в основном сфокусирован на **политику и экономику**, а не на спортивные матчи.

> **Вывод:** Спортивных событий мало (38). Основной арбитраж —
> политика, экономика, культура (выборы, Nobel, Pope и т.д.)

---

## 5. Все кандидаты с CLOB суммой (полный список)

| # | Событие | Rough (impl) | CLOB sum | Исходов | Разница |
|---|---------|-------------|----------|---------|---------|
| 1 | Nobel Peace Prize Winner 2026 | 61.8¢ | 65.4¢ | 20 | +3.6¢ |
| 2 | Next James Bond actor? | 89.6¢ | 95.3¢ | 15 | +5.7¢ |
| 3 | Wisconsin Governor Election Winner | 91.5¢ | 96.0¢ | 2 | +4.5¢ |
| 4 | Alaska Governor Election Winner   | 91.0¢ | 96.3¢ | 17 | +5.3¢ |
| 5 | CA-27 House Election Winner | 93.8¢ | 96.6¢ | 2 | +2.8¢ |
| 6 | Kansas Governor Election Winner | 94.0¢ | 97.0¢ | 2 | +3.0¢ |
| 7 | Kansas Senate Election Winner | 95.5¢ | 97.0¢ | 2 | +1.5¢ |
| 8 | Presidential Election Winner 2028 | 95.3¢ | 97.2¢ | 36 | +1.9¢ |
| 9 | Harvey Weinstein prison time? | 95.9¢ | 97.5¢ | 6 | +1.6¢ |
| 10 | Democratic Presidential Nominee 2028 | 95.4¢ | 97.9¢ | 44 | +2.5¢ |
| 11 | Serie A League Winner  | 97.2¢ | 98.1¢ | 4 | +0.9¢ |
| 12 | New Jersey Senate Election Winner | 97.5¢ | 98.2¢ | 2 | +0.7¢ |
| 13 | Iowa Democratic Senate Primary Winner | 96.3¢ | 98.4¢ | 4 | +2.1¢ |
| 14 | South Dakota Senate Election Winner | 96.7¢ | 98.5¢ | 2 | +1.8¢ |
| 15 | Oregon Governor Election Winner | 97.5¢ | 99.0¢ | 2 | +1.5¢ |
| 16 | Minnesota Senate Election Winner | 97.0¢ | 99.0¢ | 2 | +2.0¢ |
| 17 | Illinois Senate Election Winner | 97.7¢ | 99.1¢ | 2 | +1.4¢ |
| 18 | Tennessee Governor Election Winner | 97.4¢ | 99.3¢ | 2 | +1.9¢ |
| 19 | Montana Senate Election Winner | 97.0¢ | 99.4¢ | 3 | +2.4¢ |
| 20 | South Carolina Republican Senate Primary Winn | 97.9¢ | 99.7¢ | 4 | +1.8¢ |
| 21 | Which movie has biggest opening weekend in 20 | 97.4¢ | 99.8¢ | 9 | +2.4¢ |
| 22 | Arizona Governor Election Winner | 97.0¢ | 100.0¢ | 2 | +3.0¢ |
| 23 | Michigan Governor Election Winner | 96.0¢ | 100.0¢ | 3 | +4.0¢ |
| 24 | Nebraska Governor Election Winner | 97.5¢ | 100.0¢ | 2 | +2.5¢ |
| 25 | UEFA Europa League: Winner  | 97.9¢ | 100.1¢ | 4 | +2.2¢ |
| 26 | 2026 Busan Mayoral Election Winner | 97.9¢ | 100.5¢ | 12 | +2.6¢ |
| 27 | NY-17 Democratic Primary Winner | 91.9¢ | 101.0¢ | 8 | +9.1¢ |
| 28 | How many Gold Cards will Trump sell in 2026? | 93.5¢ | 101.7¢ | 8 | +8.2¢ |
| 29 | Guinea-Bissau National People's Assembly Elec | 72.4¢ | 102.3¢ | 6 | +29.9¢ |
| 30 | LPL 2026 Season Winner | 92.8¢ | 102.3¢ | 15 | +9.5¢ |

