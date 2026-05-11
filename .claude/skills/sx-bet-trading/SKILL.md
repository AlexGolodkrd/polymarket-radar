# SX Bet Trading

**Created Phase 12 (01.05.2026)** — for SX Bet (sx.bet) maker-fill arbitrage.

## What this is for

SX Bet is a **decentralized sports betting** exchange running on its own L1 chain (SX Network, fork of Polygon Edge). Sport-only — NBA, NFL, MLB, soccer, etc. **No politics, no crypto, no weather.** Volume is much smaller than Polymarket but spreads are wider.

US-based traders are blocked by IP geofence (we hit 403 from non-US VPS). Works from EU/Asia VPS.

## Architecture (different from Polymarket)

SX Bet is **maker-only** orderbook — you can't place new orders, only **TAKE** existing maker orders. Maker orders are signed by professional MMs and broadcast on-chain.

Buy flow as taker:
1. `GET /orders?marketHashes={hash}&maker=true` → list of live maker orders
2. Filter to opposite-side orders (taker on outcome 1 needs makers on outcome 2)
3. Sort by best taker price
4. Greedy match until target stake covered
5. Build POST `/orders/fill` body with matched `orderHashes` + per-order taker amounts
6. Sign EIP-712 commitment (different chain — SX Network chainId 4162)
7. POST → instant on-chain fill

We already have this in `Scripts/executor/builders.py::build_sx_order` + `match_sx_orders`.

## Why "maker-only"

SX Bet decided that retail can't be trusted to provide liquidity (they place bad-priced orders → MM bots arb them). So only registered MMs can post; everyone else takes.

For us this means:
- **Latency tolerable** — the MM order is committed; can't be cancelled by them mid-fill (sequence number locking)
- **No race condition** — first to call /orders/fill with the orderHash wins; deterministic
- **Slippage = function of MM order set** — if we fill orderA + orderB + orderC, average price is weighted

## REST endpoints

```
Base: https://api.sx.bet/

GET  /markets/active?onlyMainLine=true&pageSize=100
     → list of markets (binary or 3-way)

GET  /orders?marketHashes={hash}&maker=true
     → live maker orders for this market (both outcomes)

POST /orders/fill
     body: {
       marketHash, taker, takerOutcome,
       fillAmount,             // raw USDC, 6 decimals
       orderHashes: [...],     // matched maker orders
       takerAmounts: [...],    // per-order USDC (in same order as hashes)
       expiry, salt
     }
     headers: signature in body, no L2 HMAC (different from Polymarket)

GET  /v1/orders/user
     → WebSocket for fill confirmations (we don't subscribe yet)
```

## EIP-712 signing for SX Bet fill

Domain:
```
{
  name: "SX Bet Token Swap",
  version: "1",
  chainId: 4162,                 // SX Network mainnet
  verifyingContract: 0x...        // depends on deployment, fetch from API
}
```

Types include `OrderFill` with `orderHashes`, `takerAmounts`, `expiry`, `salt`. **NOT IMPLEMENTED YET** — `Scripts/executor/builders.py::build_sx_order` builds the body but doesn't sign EIP-712. Real-mode SX trading would require this.

## Type catalog (Phase 5 baseline drift check)

SX Bet markets have different "types" (lines):
- type=1 — 3-way soccer (1X2)
- type=52 — Draw No Bet (DNB)
- type=2 — Asian total (over/under)
- type=3 — Asian handicap (spread)
- type=88 — straight up (NBA moneyline)
- ... 20+ other types

Per `idea.md` Phase 5 baseline (30.04.2026): when SX adds new types not in `SX_BINARY_TYPES`, our `_fetch_sx_orders` skips those markets silently → lost potential. Drift-check: every week diff `types.most_common(10)` against expected list.

## Geofence (CRITICAL)

SX Bet IP-geofences for US-based traffic. Our VPS at 77.91.97.22 (Germany) is OK; us-east-1 AWS is BLOCKED.

Symptom: 403 with `cf-mitigated: challenge` header on every request. **Not a circuit breaker problem** — it's a hard block.

If we ever migrate VPS to US: SX Bet must be disabled (`ENABLE_SX=0`) or we'd need a residential proxy in non-US.

## Common gotchas

1. **percentageOdds field is uint256 with 18 decimals scaled by 1e2** — i.e. `0.45` arrives as `45000000000000000000` (45 × 1e18). Divide by 1e20 to get implied probability. (Our code does this correctly in `_fetch_sx_orders:671`.)

2. **isMakerBettingOutcomeOne semantics** — maker betting outcome 1 means **TAKER will fill outcome 2**. Cross-side rule already handled in `_opposite_side_filter`.

3. **orderSizeFillable** is RAW USDC (6 decimals). Divide by 1e6 to get USD. Some old API responses had 18-decimal scaling — verify per integration.

4. **Cloudflare 403 vs geo 403** — both same status. Header `cf-ray` present on Cloudflare; `x-amz-cf-id` on AWS-CloudFront. Use to distinguish for circuit_breaker policy.

5. **No partial-fill recovery** — if our build_sx_order matches 5 maker orders and only 3 filled (others taken concurrently by another taker), we get partial fill = arb broken. atomic.py treats `partial_fill` correctly; revert_filled_legs needs SX-specific handling (taker-fill on opposite outcome) — currently TODO.

6. **Settlement delay** — SX Network has ~2s block time. Fill confirmations come via WebSocket but on-chain finality at +6s. For arb purposes 2s is fine.

7. **expiry field** in fill body — must be Unix timestamp seconds (NOT ms like Polymarket V2). Server rejects expired fills.

## Skills overlap

- `polymarket-trading` — different chain (Polygon), different fee model (taker fee)
- `limitless-trading` — same model as Polymarket (both CLOB on EVM L2)
- `sx-bet-trading` (this) — maker-only fill on SX Network L1

## Audit checklist (Phase 12 PR)

- [ ] `_fetch_sx_orders` — handles HTTP errors → returns `(hash, None, 0, None, 0)`. Verify: ✅ done.
- [ ] `_fetch_sx_orders` returns top-of-book taker depth (Phase 10 #51) ✅ done
- [ ] `match_sx_orders` greedy match — verify ordering is by ASCENDING taker_price (lowest = best). ✅ done.
- [ ] `match_sx_orders` slippage cap — `max_taker_price`. ✅ done.
- [ ] `build_sx_order` constructs body with correct schema. ⚠ needs EIP-712 sign step.
- [ ] partial_fill detection — `match_sx_orders.partial`. ✅ done.
- [ ] revert flow — taker-fill on opposite outcome. ❌ TODO Phase 12.
- [ ] Circuit breaker on 403/429. Need to verify config in `Scripts/circuit_breaker.py`.
- [ ] Type catalog drift detection — needs scheduled job.

## See also

- `Scripts/executor/builders.py::fetch_sx_matchable_orders` — REST list + filter
- `Scripts/executor/builders.py::match_sx_orders` — greedy match logic
- `Scripts/executor/builders.py::build_sx_order` — fill body builder
- `Scripts/arb_server.py::_fetch_sx_orders` — radar-side scanner
- BUG_CATALOG.md sections 6.* (HTTP errors), 9.* (cross-platform parity)

---

## May 2026 docs verification (02.05.2026)

### Rate limits (api.sx.bet)
- General: **500 req/min**
- Order submissions: 5,500 req/min
- Order queries: **20 req/10s** (= 2 RPS — strict!)
- Trades: 200 req/min
- Market list: 500 req/min

При hot+near scan каждые 3s наш `sx_micro_loop` шлёт 1 запрос — лимита не достигаем. Но если расширим до per-market /orders per HOT outcome → следить за 20/10s ceiling на `/orders`.

### EIP-712 OrderFill (verified)
- **Domain**: `name="SX Bet Order Fill"`, `version="6.0"`, `chainId=4162`, `verifyingContract=0xBe9F69dab98C1Ddee5BF31a9b1f5DBe88869B5d4`
- Type "Details" struct: `{action, market, betting, stake, worstOdds, executor}` — соответствует нашему `Scripts/executor/builders.py:478-499`
- v6.0 active с минимум Phase 12 (01.05.2026) — никаких contract upgrades с тех пор не зафиксировано

### CRITICAL: EIP-55 checksum addresses
SX силentno отклоняет lowercase ETH адреса в order body. **Все wallet addresses должны быть mixed-case checksum** перед отправкой.

Текущий код передаёт `wallet.eth_address` напрямую — без `Web3.to_checksum_address()`. **Это потенциальный source of silent failures для real-mode.**

Фикс (отдельный PR): добавить нормализацию в `WalletStub.__post_init__` или в `build_sx_order` перед заполнением body.

### Taker fee assumption
Документация SX утверждает "no fees on single bets" — это применяется к makers. Для takers fee не явно опубликована. Наш код пока assume **0% taker fee на fills**. Pre-real-mode TODO: verify на первом live trade и зафиксировать в `compute_sx_threshold`.

### USDC адрес на SX Network
ChainId 4162. Standard ERC-20, 6 decimals. **Точный адрес контракта НЕ найден через docs**, нужна верификация на https://explorerl2.sx.technology/ перед DRY_RUN=0.

### Geofence (verified for our VPS)
- IP-level геофенс на US (403 + `cf-mitigated` header).
- Наш VPS Германия (77.91.97.22) — **clear** (200 OK ответы зафиксированы 02.05.2026).
- Если переедем в US — `ENABLE_SX=0` обязателен.

### Market types — drift risk
Доки **не enumerate** все 20+ types. Мы покрываем: type=1 (3-way), 2 (total), 3 (handicap), 52 (DNB), 88 (moneyline) + расширение Phase 16. Новые types появляются → `filter_sx` их silently skip'ит. **Weekly check**: запросить `/markets/active`, сравнить distinct types vs `SX_BINARY_TYPES + SX_THREE_WAY_TYPES`.
