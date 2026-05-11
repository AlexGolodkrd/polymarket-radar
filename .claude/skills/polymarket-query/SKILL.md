# Polymarket Read-Only Query

**Source**: nousresearch/hermes-agent/skills/research/polymarket

## What it is

Read-only access to 3 public Polymarket APIs:

| API | Purpose | URL |
|---|---|---|
| **Gamma** | Discovery/search/events | `gamma-api.polymarket.com/events` |
| **CLOB** | Real-time prices, orderbooks | `clob.polymarket.com/book` |
| **Data** | Trades, open interest history | `data-api.polymarket.com` |

## Rate limits

**Generous**: ~4,000-9,000 req per 10s depending on endpoint.

We're nowhere near these limits in plan-kapkan (we run ~600 req/scan, every 90s = ~7 req/s peak).

## Critical rule

> **"Prices ARE probabilities."**
> 
> price 0.65 = market thinks 65% likely
> 
> Always format for users as **65.2% Yes / 34.8% No**, not raw "0.65".

We follow this in `dashboard.html` (`{(sum_cents * 100).toFixed(1)}¢`).

## Read-only ≠ trading

This skill is **READ-only**. Order placement requires:
- EIP-712 signed orders
- Polymarket-specific token approvals (USDC.e, CTF)
- Polygon wallet with funded gas

We do this in `Scripts/executor/` with our own implementation.

## Application to plan-kapkan

Already covered. Our `_fetch_clob`, `_fetch_poly_market_info`, gamma `/events` calls match this skill's patterns.

**Could borrow**: dr-manhattan (separate skill) wraps all 3 APIs in a single client class — would simplify our code.
