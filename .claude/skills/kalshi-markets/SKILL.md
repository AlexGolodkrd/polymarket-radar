# Kalshi Markets API Skill

**Source**: disler/beyond-mcp/apps/4_skill/.claude/skills/kalshi-markets

## Overview

Read-only access to Kalshi prediction market data. **No authentication required**.

## Available scripts (10 self-contained)

| Script | Purpose |
|---|---|
| `status.py` | Exchange operational status |
| `markets.py` | List available markets |
| `market.py` | Detail of a specific market |
| `orderbook.py` | Bid/ask levels for a market |
| `trades.py` | Recent trading activity |
| `search.py` | Keyword search with caching |
| `events.py` | Groups of related markets |
| `event.py` | Specific event details |
| `series_list.py` | ~6,900 market templates |
| `series.py` | Specific template info |

## Patterns we already follow

- Our `_fetch_kalshi_ob(ticker)` matches their `orderbook.py`
- Our gamma-style discovery matches their `markets.py`/`events.py`
- We use `with_nested_markets=true` query — same as their best-practice

## Patterns we could borrow

- **Search caching** — their `search.py` uses intelligent cache (similar to our `_lim_meta_cache`/`poly_market_info_cache`)
- **`--help` and `--json` flags on every script** — useful if we expose CLI later
- **Self-contained scripts** — no shared state between modules

## Status of Kalshi in plan-kapkan

Currently `ENABLE_KALSHI=0` because:
1. Kalshi requires US-based access (geo-blocked from Georgia)
2. Trading API needs RSA cert + API key

For **read-only data** their API doesn't need geo, so we COULD enable Kalshi market data ingestion even from non-US — but trading would still be blocked. Not high priority.

## Repository

https://github.com/disler/beyond-mcp
