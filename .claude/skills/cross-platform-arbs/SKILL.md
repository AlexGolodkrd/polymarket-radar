# Cross-Platform Arbitrage (Polymarket × Limitless × SX Bet)

**Created Phase 12 (01.05.2026)** — design phase. **NOT YET IMPLEMENTED.**

## What this is for

When a real-world event (NBA game, election, crypto resolution) is listed on **multiple** prediction markets simultaneously, MM bots are NOT all synchronized. Polymarket might have YES Lakers @ 52c while Limitless has YES Lakers @ 56c — buying YES on Polymarket + buying NO on Limitless = guaranteed profit.

This is **the realistic arb edge** for non-HFT players. Within a single platform, MMs are sub-50ms; cross-platform spreads stay 1-5c open for 5-30 seconds.

## Why this is the priority over single-platform speed

Within Polymarket alone:
- We poll every 3s. MMs operate in 10-50ms. We're always last.
- Top-of-book arb windows close in 100-500ms.

Cross-platform:
- **Different MMs on different platforms** — they don't synchronize cross-platform inventory
- **Different liquidity providers** — Polymarket has Wintermute / GSR; Limitless has smaller players
- **Spread windows 5-30 seconds** — easily catchable at our latency
- **Less competition** — only a few bot operators do cross-platform; lots of mispricing

## Architecture (proposed)

```
┌─ Polymarket scan ──┐  ┌─ Limitless scan ──┐  ┌─ SX Bet scan ────┐
│  (existing)        │  │  (existing)        │  │  (existing)       │
│  → poly_pool       │  │  → lim_pool        │  │  → sx_pool        │
└──────┬─────────────┘  └──────┬─────────────┘  └──────┬────────────┘
       │                        │                        │
       ▼                        ▼                        ▼
┌──────────────────────────────────────────────────────────────┐
│  cross_platform_matcher.py (NEW)                             │
│                                                              │
│  • For each event in pool_A, look for matching event in      │
│    pools_B, pools_C via fuzzy title + date matching          │
│    (event_matching.py — see event-matching-fuzzy skill)      │
│                                                              │
│  • Output: list[CrossPlatformPair {                          │
│      event_id, platform_a, market_a, platform_b, market_b    │
│      yes_price_a, yes_price_b, no_price_a, no_price_b        │
│    }]                                                        │
└────────┬─────────────────────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────────────────────────┐
│  cross_platform_eval.py (NEW)                                │
│                                                              │
│  Detects 2 arb structures:                                   │
│    X1. YES_a + NO_b < 97c       (buy YES on A, NO on B)     │
│    X2. NO_a + YES_b < 97c       (buy NO on A, YES on B)     │
│                                                              │
│  Same REAL_OB_SOURCES guard. Same depth check. Same          │
│  preflight gates per leg.                                    │
└────────┬─────────────────────────────────────────────────────┘
         │
         ▼
[appears in /api/deals alongside existing per-platform deals]
```

## Critical isolation rules

1. **Existing per-platform A/B/C arbs continue working** — cross_platform is additive.
2. **No shared state between platforms** beyond the matcher output. Bug in Limitless eval can't poison Polymarket eval.
3. **Per-platform source validation** still required: each leg's source must be in REAL_OB_SOURCES.
4. **Per-platform balance check** in preflight: leg A needs pUSD on Polygon (Polymarket), leg B needs USDC on Base (Limitless), etc.
5. **Platform-specific cancel paths** — already handled per builder.

## Wallet topology problem

Each platform = different blockchain:
- Polymarket → Polygon → pUSD
- Limitless → Base → USDC
- SX Bet → SX Network → USDC
- Kalshi → US-only fiat (we're blocked)

**Capital fragmentation:** $100 split across 3 chains = $33 per chain effective. Either:
- Pre-fund equally (simple but reduces flexibility)
- Cross-chain bridges on demand (Hop / Across) — but slow (5-15 min) → not useful for arb timing
- Specialized bots per platform (1 bot = 1 chain, capital concentrated)

**Recommended approach:** **3 dedicated bots per chain**, each pre-funded. 6-bot pool now becomes: bot1-2 Polygon, bot3-4 Base, bot5-6 SX. Cross-platform arb fires bots from BOTH chains in parallel.

## Settlement timing risk

Polymarket might resolve event 1 hour BEFORE Limitless does. During that window:
- Polymarket leg = **paid out** (we got $1 per contract or $0)
- Limitless leg = **still pending**

If Polymarket paid us $1 and Limitless is still open, we hold a directional position until Limitless resolves. Acceptable as long as both eventually resolve.

**Mitigation:**
- Resolve-time check: `event.endDate_polymarket` vs `event.endDate_limitless` should be within 24h. If wider → quarantine.
- Daily P&L includes pending Limitless positions at mark-price (not realized P&L).

## Implementation plan (Phase 12+)

| PR | Scope | Tests |
|---|---|---|
| **#56** | event_matching.py module + skill | unit tests for normalization + fuzzy match |
| **#57** | cross_platform_matcher.py — pool intersection logic | mock 3-pool fixture, verify matched events |
| **#58** | cross_platform_eval.py — X1/X2 structure detection + deal builder | end-to-end with mocked depths |
| **#59** | Wallet topology: per-chain bot assignment in coordinator | assign_legs respects chain |
| **#60** | UI: add "Cross-platform" filter in /api/deals + dashboard | E2E |

## Risks / open questions

1. **What if 2 platforms have **same event** but different `endDate`?** — quarantine if delta > 24h
2. **What if outcome semantics differ?** "Lakers wins" on Polymarket = 4 quarters; on SX Bet = regulation only? — manual mapping for first 100 events, then auto
3. **Fee asymmetry** — Polymarket 270bps vs Limitless 0bps. Need per-platform fee in threshold computation per leg
4. **CLOB depth mismatch** — Polymarket has 10x more depth than Limitless. Stake limited by min_depth across legs
5. **API rate limits** — querying 3 platforms sequentially × 3s scan = much more requests. Need parallelize via httpx

## See also

- `polymarket-trading` skill — for Polymarket leg execution
- `limitless-trading` skill — for Limitless leg execution
- `sx-bet-trading` skill — for SX Bet leg execution
- `event-matching-fuzzy` skill — for cross-platform event ID
- `BUG_CATALOG.md` — Phase 12 entries (when implemented)
