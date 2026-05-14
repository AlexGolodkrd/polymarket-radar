---
name: polymarket-post-v2-status
description: Audit summary of which Polymarket changes after the April 28, 2026 V2 cutover are already implemented in this codebase and which are still gaps. Reference whenever someone touches Polymarket integration code (executor signing, scan-time fetch, fee math, approve flow) so you don't redo work that's done OR assume something's done that isn't.
---

# Polymarket post-V2 migration status (12.05.2026)

## V2 cutover (28.04.2026) — DONE ✅

Polymarket launched new Exchange contracts on Polygon (replacing V1) with:
- New EIP-712 domain version `"2"`
- Order struct rewrite: dropped `nonce`, `feeRateBps`, `taker`; added `timestamp` (ms), `metadata`, `builder`
- Fee determination shifted from order-time to match-time
- New collateral token **pUSD** (replaces USDC.e — backed by USDC with onchain enforcement)
- Two new addresses: Standard CTF Exchange V2 + Neg Risk CTF Exchange V2

What's already wired in our code:

| Piece | File | Status |
|---|---|---|
| Domain `version: "2"` | [executor-ts/src/types/eip712.ts:18,25](executor-ts/src/types/eip712.ts) | ✅ |
| V2 contract addresses (standard + negRisk) | same file, lines 20 + 27 | ✅ |
| V2 Order struct (timestamp/metadata/builder, no nonce/feeRateBps/taker) | same file, `POLY_ORDER_TYPES_V2` | ✅ |
| pUSD address constant | [Scripts/polymarket_approve.py:69-72](Scripts/polymarket_approve.py) | ✅ |
| Two-step wrap USDC.e → pUSD, approve(pUSD), setApprovalForAll | [Scripts/polymarket_approve.py](Scripts/polymarket_approve.py) full file | ✅ |
| Preflight balance/allowance reads pUSD | [Scripts/preflight.py:34,67](Scripts/preflight.py) | ✅ |

**Implication**: Signing path is V2-correct. When operator flips `DRY_RUN=0` and L2 creds + pUSD are funded, orders should be accepted by the new exchange. Tests in `executor-ts/tests/` cover EIP-712 round-trip against the V2 domain.

## Other 2026 changes — partial coverage

### /events offset pagination still in use (10.04 change)
Legacy `?offset=N&limit=500` still works but is **deprecated** in favor of `/events/keyset` cursor pagination. We use offset at:
- [Scripts/arb_server.py:4913](Scripts/arb_server.py:4913) — sync chunk fallback
- [Scripts/async_fetchers.py:616](Scripts/async_fetchers.py:616) — parallel HTTP/2 fetcher

See `polymarket-keyset-pagination` skill for migration plan.

### HeartBeats API for server-side auto-cancel (06.01 feature) — NOT USED
Polymarket exposed a HeartBeats API: client pings, server auto-cancels open orders if pings stop. Different from our own `_heartbeat_loop` in `poly_ws.py` (which is just our app-level liveness check).

Without it: if the executor crashes mid-arb with one leg filled and one pending, the pending order can sit on the book until manual cancel. With it: server auto-cancels after N missed pings.

See `polymarket-heartbeats-cancel` skill for integration plan.

### feeSchedule object (31.03 change) — UNVERIFIED
Polymarket moved fee math to a `feeSchedule` object per market. Our radar reads:
```python
'maker_fee_bps': float(m.get('maker_base_fee') or 0),
'taker_fee_bps': float(m.get('taker_base_fee') or 0),
```
([Scripts/arb_server.py:1706-1707](Scripts/arb_server.py:1706))

Field names `maker_base_fee` / `taker_base_fee` may be legacy. If the new shape is nested (`m['feeSchedule']['taker']`), the `or 0` fallback silently returns 0, which means `compute_poly_threshold(0)` returns `1 - safety_buffer` — a too-tight threshold that accepts arbs that are actually negative-EV after the real fees.

See `polymarket-fee-schedule` skill for verification approach.

### Post-Only Orders (06.01 feature) — NOT USED
Order flag rejecting an order if it would immediately match. Maker mode could benefit (guaranteed maker rebate), but our maker mode (Phase 16) just uses regular limit orders with low size — orthogonal feature.

### Relayer `/submit` `transactionID` change (21.04) — NOT APPLICABLE
We POST orders directly to CLOB `/order` — we don't use the Relayer endpoint at all. The transactionID-not-transactionHash change doesn't touch our code path.

### Crypto market fees (01.03 / 05.01) — NOT APPLICABLE
Maker rebates and taker fees were extended to 15min/1h/4h/daily crypto markets. We scan sport binaries, not crypto timeframes — this doesn't affect our radar.

## Decision tree for future Polymarket changes

When a new Polymarket changelog lands:

1. Does it touch the signed order shape, domain, or verifyingContract?
   → CRITICAL. Update `executor-ts/src/types/eip712.ts` + sign tests immediately.
2. Does it deprecate a REST endpoint we use?
   → Plan migration; legacy usually lives 30-90 days.
3. Does it add a new API feature?
   → Evaluate if it solves a real problem we have (don't adopt for FOMO).
4. Does it affect fees or collateral?
   → Audit `compute_poly_threshold` and preflight balance reads.

## Sources
- [Polymarket Changelog](https://docs.polymarket.com/changelog)
- [V2 upgrade announcement (28.04.2026)](https://help.polymarket.com/en/articles/14762452-polymarket-exchange-upgrade-april-28-2026)
- Our session record: `executor-ts/tests/*.test.ts` for V2 signing round-trip tests
