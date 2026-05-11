# Maker / Taker Orders — Polymarket V2

**Created Phase 12 (01.05.2026)**, **partial implementation Phase 15 (01.05.2026)**.

## Status

| Component | Status | File |
|---|---|---|
| `build_poly_maker_order` | ✅ implemented | `executor/builders.py:208` |
| `select_fire_mode` (hybrid selector) | ✅ implemented | `executor/atomic.py` |
| `maker_supervise` (fill+adverse loop) | ✅ implemented | `executor/atomic.py` |
| Wire-up in `fire_arb` | ❌ TODO Phase 16 | — |
| Cancel-and-replace on adverse | ❌ TODO Phase 16 | — |
| Real-mode integration | ❌ Phase 17 (after graduation gate) | — |

Tests: 14/14 passing in `tests/test_phase_15_maker_orders.py`.

Activated via env `MAKER_MODE_ENABLED=1` (default off — safe baseline).

## Definitions

- **Maker** — places a **limit order** that rests in the orderbook waiting for someone to take it. Provides liquidity.
- **Taker** — submits an order that **immediately matches** existing maker orders. Removes liquidity.

Polymarket V2 fee structure (verified 28.04.2026):
- Taker fee: starts at 270 bps, decreases by volume tier (down to 100 bps at $1M+ monthly)
- Maker fee: **0 bps** at all tiers (no rebates either, but no fee)

## How orders are determined as maker/taker

When you POST an order with `orderType: GTC`:
- If your price MATCHES existing best opposite-side → **immediate fill as TAKER** → fee charged
- If your price is BETTER than existing book → rests as **MAKER** → no fee
- If your price is WORSE than best → **partial as taker, rest as maker**

You don't choose maker/taker explicitly — the price determines it. To force maker, set price 1 tick INSIDE the spread (between best bid and best ask).

## Why we currently use TAKER

Arb requires **synchronous, atomic** position across N legs. If one leg waits 30 seconds for fill, the arb window closes and we hold directional risk.

Taker pros:
- Immediate fill or fail (parsable)
- Atomic with `FOK` orderType
- Predictable

Taker cons:
- Pay 270 bps × N legs = significant on tight arbs
- Limited to depth at best ask (Phase 11 #51 fix)

## Why MAKER would help

If we set price RIGHT AT current best ask:
- Order matches as TAKER → fee applies
If we set price 1 tick UNDER current best ask:
- We're now best ask → **maker**
- Anyone wanting to buy at current best ask **must** take our order (we improved the price)
- Rest of book still there at worse prices
- Net: **we take ALL the volume** that would have gone to old best ask, pay 0 fee

This works ONLY if:
- We're FIRST to the new tick (race condition — 100ms window typically)
- Depth at our price > our stake (otherwise partial fill)
- Spread > 1 tick (otherwise no room to be maker)

## Why MAKER is risky for arbs

| Scenario | What happens |
|---|---|
| Maker order placed; price moves AGAINST us | Adverse selection — only takers who think we're WRONG hit our order. We always lose vs informed flow |
| Maker order rests; other arb legs FILL as taker | Now we have N-1 legs filled, 1 waiting. Directional exposure. |
| Maker order CANCELLED by exchange | Polymarket V2 occasionally rate-limits cancels under load → leg might fill AFTER we tried to cancel |
| News hits during maker-wait | MMs cancel within 50ms. We cancel within 200ms (POST + sign + RTT). They beat us → our maker fills at stale price |

## Hybrid mode design (when we eventually implement)

Per-arb decision based on **arb spread**:

| spread | fire mode | reasoning |
|---|---|---|
| < 1c (sum 96-97c) | **FOK taker** | Window closes in <1s, no time to maker |
| 1-3c (sum 94-96c) | **GTC taker first; if rejected → maker retry** | Try fast path first |
| 3-5c (sum 92-94c) | **Maker FIRST**; if not filled in 5s → cancel + taker fallback | Capture maker rebate when we have buffer |
| 5+c (sum < 92c) | **Maker only**, longer hold | Edge so wide we don't need atomicity urgency |

## Cancel-on-fill-of-other-legs

Critical maker-mode rule: if any leg of arb fills AS TAKER while another leg is RESTING AS MAKER:
- Either let the maker leg complete (if same arb is still profitable at current prices)
- Or cancel the maker leg AND revert the filled leg

Logic per arb:
```python
filled_legs = [l for l in legs if l.status == 'filled']
resting_legs = [l for l in legs if l.status == 'maker_resting']
if filled_legs and resting_legs:
    if recompute_arb_still_profitable(filled, resting) >= MIN_PROFIT_USD:
        wait()  # let maker fill or timeout naturally
    else:
        for l in resting_legs: cancel_maker(l)
        revert_filled_legs(...)  # existing path
```

## Adverse selection guard

Before letting a maker order rest > 1 second, monitor cross-platform / WS for SAME outcome's price drift on OTHER markets. If price moves > 1c in 500ms → cancel our maker (we're being picked off).

```python
def maker_supervise(order_id, expected_price):
    while not filled and time.time() < deadline:
        time.sleep(0.5)
        current = get_best_ask_from_other_source(token)
        if abs(current - expected_price) > 0.01:
            cancel_order(order_id)
            return 'adverse_selection_cancelled'
        if filled: return 'filled'
    cancel_order(order_id)
    return 'timeout'
```

## Order matching priority on Polymarket V2

Polymarket uses **price-time priority**:
1. Best price wins
2. Among same price, FIRST submitted wins

To be ROBUST first at a tick:
- Pre-sign during NEAR pool (already done — Phase 9zz `presign.py`)
- POST under 100ms when sum drops to threshold
- Use HTTP/2 multiplexing (already done — Phase 9fff)

We already have most maker-mode infrastructure ready. The missing pieces:
- Per-arb mode selector
- Maker_supervise loop
- Cancel-and-replace logic
- Adverse selection guard

Estimated work: ~5 PRs over 2-3 weeks.

## Why we deferred

User confirmed (01.05.2026): focus on **cross-platform first** (better edge per LoC), maker-mode revisit after Phase 12-13 stabilization.

## Implementation plan (Phase 14+, NOT scheduled)

| PR | Scope |
|---|---|
| Per-arb mode selector in atomic.fire_arb |
| Maker_supervise daemon thread per resting order |
| Adverse selection cross-source price monitor |
| Cancel-and-replace on price drift |
| Hybrid mode metrics in /api/paper_stats |

## See also

- `polymarket-trading` skill — V2 EIP-712, GTC/GTD/FOK
- `Scripts/executor/builders.py:build_poly_order` — already supports `order_type='GTC'` for maker
- Polymarket fee tiers: `https://docs.polymarket.com/fees`
