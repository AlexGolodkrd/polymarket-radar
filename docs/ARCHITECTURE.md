# Architecture — plan-kapkan

Last updated: 2026-05-27 (Phase audit-27.05)

This document describes the **current** module layout and the **target**
layout. Use it as a map when finding "where does X live" and as a
reference for the planned refactor.

---

## Current layout (as-shipped, 27.05.2026)

```
plan-kapkan/
├── Scripts/                       # All Python. Monolith-leaning.
│   ├── arb_server.py              # 7742 lines — radar + Flask + dispatcher
│   ├── dashboard.html             # 2007 lines — UI (HTML+CSS+JS together)
│   ├── async_fetchers.py          # httpx + HTTP/2, gated by ASYNC_FETCH
│   ├── poly_ws.py                 # Polymarket book WebSocket
│   ├── poly_user_ws.py            # Polymarket user-channel WS (fills)
│   ├── limitless_ws.py            # Limitless book WS (Socket.IO)
│   ├── limitless_hmac.py          # HMAC signer for Limitless
│   ├── cross_platform.py          # X1/X2 cross-platform arb matching
│   ├── event_matching.py          # Fuzzy event name match + scope guards
│   ├── circuit_breaker.py         # Generic 3-state breaker per-host
│   ├── http_codes.py              # 13-code HTTP classifier + retry policy
│   ├── notify.py                  # Telegram alerts (stdlib urllib)
│   ├── analytics.py               # opened/closed/fire_filled lifecycle log
│   ├── paper_trading.py           # Phase 5 graduation gate
│   ├── preflight.py               # Pre-fire balance/depth/allowance checks
│   ├── exchange_latency_probe.py  # GET-RTT shadow probe per platform
│   ├── poly_derive_api_creds.py   # One-shot L2 HMAC creds derive
│   ├── polymarket_approve.py      # On-chain USDC.e → pUSD wrap helper
│   ├── limitless_approve.py       # Limitless on-chain approve helper
│   ├── watchdog.py                # Separate process; reads .killed flag
│   ├── lint_dashboard_js.py       # Pre-commit JS lint
│   ├── config.py                  # ⭐ NEW (27.05) — centralised env config
│   ├── contracts.py               # ⭐ NEW (27.05) — Python↔TS contract
│   ├── executor/
│   │   ├── atomic.py              # Per-arb fire orchestration
│   │   ├── builders.py            # Per-platform order body builders
│   │   ├── dryrun_log.py          # JSONL append for paper-trade decisions
│   │   ├── fills.py               # Realistic-fill simulation (paper)
│   │   ├── presign.py             # NEAR-pool pre-signing (latency fix)
│   │   ├── pipeline_timing.py     # Per-stage timing metrics
│   │   └── bot_connector.py       # Wallet adapter
│   ├── risk/
│   │   ├── limits.py              # Per-trade / daily / hourly caps
│   │   ├── killswitch.py          # File-based fail-closed
│   │   ├── reconcile.py           # On-chain position vs internal book
│   │   ├── state.py               # Atomic JSON persistence
│   │   └── network_check.py       # IP / country gate
│   └── wallets/
│       ├── coordinator.py         # 6-bot wallet pool + assignLegs
│       ├── stores.py              # LocalEnv + KMS backends
│       ├── rebalance.py           # USDC auto-rebalance proposals
│       └── config.py              # Per-bot env parsing
│
├── executor-ts/                   # TypeScript executor service (port 5051)
│   ├── src/
│   │   ├── server.ts              # Fastify-style HTTP server
│   │   ├── executor/atomic.ts     # Per-arb fire orchestration (TS side)
│   │   ├── fire/{poly,sx,lim}_post.ts  # HTTP fire modules
│   │   ├── lib/{poly_hmac,http_client,limitless_hmac}.ts
│   │   ├── ws/{poly_user_ws,limitless_user_ws}.ts
│   │   ├── wallets/{pool,signers}.ts
│   │   └── types/deal.ts          # ⭐ MIRRORS Scripts/contracts.py
│   └── tests/                     # vitest-driven unit tests
│
├── tests/                         # 88 pytest files + conftest.py
├── deploy/                        # VPS playbooks, smoke tests, rollback
├── docs/                          # Operator runbook, deploy setup
└── Executions/                    # Runtime data (jsonl, json, .killed)
```

### Module ownership matrix

| Concern | Owner module | Notes |
|---|---|---|
| Configuration | `Scripts/config.py` | Singleton; reload() for tests. Use everywhere new. |
| Wire contract | `Scripts/contracts.py` | Mirror in `executor-ts/src/types/deal.ts`. |
| Fire dedup | `arb_server._fired_arb_keys` | TTL-based, env `FIRE_COOLDOWN_S`. |
| Open-deal lifecycle | `analytics._open_deals` | Grace = `CLOSE_GRACE_SCANS`. |
| Kill switch | `risk/killswitch.py` | File-based (`.killed`). Fail-closed. |
| Daily loss / per-trade | `risk/limits.py` | Reads central config (port pending). |
| Order signing (Poly V2 / SX / Lim) | `executor-ts/src/lib/` | EIP-712 in TypeScript. |
| Order POST | `executor-ts/src/fire/` | Through residential proxy. |
| Fill confirmation | `executor-ts/src/ws/` | User-channel WS per platform. |

---

## Target layout (proposed Phase audit-28+)

The big change: split the `arb_server.py` monolith. Proposed cuts
(can be done one PR per row, each ≤500 lines of move + minimal change):

```
Scripts/
├── radar/
│   ├── __init__.py
│   ├── app.py             # Flask app factory + endpoints registration
│   ├── scan_loop.py       # main scan tick
│   ├── pools.py           # HOT / NEAR / COLD classification
│   ├── filters/
│   │   ├── polymarket.py  # filter_poly + V2 metadata fetch
│   │   ├── limitless.py
│   │   ├── sx.py
│   │   └── common.py      # incomplete-coverage gate, end_date guards
│   ├── eval/
│   │   ├── per_platform.py  # ALL_YES / ALL_NO / YES_NO_PAIR
│   │   └── cross.py         # X1 / X2
│   ├── orderbook/
│   │   ├── clob.py         # Polymarket REST + WS
│   │   ├── limitless.py    # Limitless REST + Socket.IO
│   │   └── sx.py           # SX Bet REST
│   ├── api/
│   │   ├── deals.py        # /api/deals + /api/near + /api/recent_deals
│   │   ├── stats.py        # /api/scan_health + /api/ts_metrics
│   │   ├── analytics.py    # /api/analytics + /api/history
│   │   └── admin.py        # /api/kill + /api/reset (ACL'd)
│   ├── fire_dispatcher.py  # arb → TS executor HTTP (uses contracts.py)
│   └── dedup.py            # _fired_arb_keys TTL logic, extracted
├── config.py               # (already extracted)
└── contracts.py            # (already extracted)
```

**Why this split**:

1. **`radar/filters/` per platform** — operator can `git log -p radar/filters/polymarket.py` and see only Polymarket changes. Today, debugging Polymarket means grepping a 7742-line file.
2. **`radar/api/` per endpoint group** — Flask blueprint registration moves out of `arb_server.py`. Each blueprint is independently testable with `app.test_client()`.
3. **`radar/fire_dispatcher.py`** — currently this logic is in `arb_server._fire_arb_via_ts`. Extracting it lets us replace HTTP with gRPC later without touching scan logic.
4. **`radar/dedup.py`** — `_fired_arb_keys` becomes a class with explicit `lock()`/`evict_expired()`/`reserve()`/`is_within_cooldown()` methods. The TTL fix from Phase audit-27.05 stays, but is documented as a public API.

**Migration plan** (no big-bang move):

| PR | Move | Risk |
|---|---|---|
| audit-28a | Create `radar/` package, move `_fired_arb_keys` + `_arb_fire_key` to `radar/dedup.py`. Re-export from `arb_server.py` for legacy tests. | Low — pure data structure. |
| audit-28b | Move all `filter_*` functions to `radar/filters/`. Re-import to keep arb_server endpoints working. | Medium — filters touch many caches. |
| audit-28c | Move scan_loop body. | High — scan_loop reads many globals. |
| audit-28d | Move Flask endpoints to `radar/api/`. | Low — endpoints are leaf functions. |
| audit-28e | Make `radar.app.create_app()` the WSGI entry; delete arb_server.py. | Low (if all prior PRs landed). |

Each PR must keep all 87+ tests green. Add fresh test for each newly-extracted module to lock in its interface.

---

## Cross-cutting concerns

### Configuration

Source of truth: `Scripts/config.py::RadarConfig`. Reads `Credentials.env`.
Defaults reflect current production. Any new tunable must:

1. Add a field with `Field(default=..., description=...)`.
2. Reference any prior PR / phase / lesson in the description.
3. If env-overridable (almost always), keep the `os.environ.get` fallback in caller during the transition.

### Testing

`tests/conftest.py::_reset_singletons` autouse fixture clears:

- `killswitch.killed` flag (the `.killed` file)
- `circuit_breaker.all_breakers()` states
- `analytics._open_deals`, `_near_logged`
- `arb_server._fired_arb_keys`
- `config.config` singleton (via `config.reload()`)

This means **no test should rely on inherited state**. If a test needs
specific state, it sets it up explicitly in `setUp()` / function body.

### Wire contract Python↔TS

Adding a new field to a `FireRequest`:

1. Edit `Scripts/contracts.py` — add field with validators.
2. Edit `executor-ts/src/types/deal.ts` — same field name, equivalent type.
3. Reference each other in the file-header comment.
4. Build a `tests/test_contract_parity.py` shape test (planned, not yet implemented).

### Linting / type-checking

`pyproject.toml` configures:

- **ruff** — pycodestyle, pyflakes, bugbear, comprehensions, pyupgrade.
- **mypy** — strict on `config.py` + `contracts.py`. Legacy files still
  in `ignore_errors` mode; the goal is to tighten one module per PR.

CI gate (proposed):

```yaml
- run: pip install ruff mypy pytest
- run: ruff check Scripts/ tests/
- run: mypy --config-file pyproject.toml Scripts/config.py Scripts/contracts.py Scripts/analytics.py
- run: pytest tests/
```

---

## Hot paths — where the code spends most time

Operator question "почему scan tick 30s?" — here's the breakdown
(from `/api/scan_breakdown_ms` at 27.05.2026 12:00 UTC):

| Stage | p50 | p99 | Owner |
|---|---|---|---|
| Polymarket fetch + filter | 2.5s | 33s | `arb_server.py::filter_poly` + `async_fetchers.py` |
| Limitless fetch | 11s | 44s | `async_fetchers.py::fetch_limitless_pages` |
| SX Bet fetch | 3.7s | 4.3s | `arb_server.py::filter_sx` |
| Cross-platform eval | <1s | <1s | `cross_platform.py` |
| Pool classify | <1s | <1s | `arb_server.py::classify_pools` |
| _maybe_dry_fire dispatch | n×2.3s | n×2.3s | `arb_server.py::_maybe_dry_fire` (n = #arbs) |

Limitless dominates. The TS-5 WebSocket-only orderbook plan would
cut Limitless p99 from 44s → near zero.
