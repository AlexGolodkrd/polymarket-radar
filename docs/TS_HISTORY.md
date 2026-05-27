# TypeScript executor — historical migration record

This file consolidates three earlier planning documents (TS_REWRITE_PLAN,
TS_MIGRATION, TS_PHASE5_PLAN). They were "live" planning docs in
April-May 2026 while the executor was being moved from Python to
TypeScript. The migration is **complete**: the TS executor runs in
production as the `plan-kapkan-executor-ts` container.

This file exists as a historical record. For current architecture,
see `docs/ARCHITECTURE.md`.

---

## Phase TS-1 → TS-3 (06.05.2026 → 11.05.2026): skeleton + cutover

Origin: `docs/TS_REWRITE_PLAN.md` and `docs/TS_MIGRATION.md`.

### Why only the executor (not the detector)

The detector — `arb_server.py`, `event_matching.py`, `cross_platform.py`,
`circuit_breaker.py`, `analytics.py`, `paper_trading.py` aggregator,
`dashboard.html` — stayed in Python. Rewriting the detector would have
been thousands of lines of complex streaming logic for negligible
gain. The executor (signing + posting + fill-tracking) had a much
better Python→TS ROI:

| Concern | TS advantage |
|---|---|
| EVM signing (EIP-712) | viem / ethers — first-class, type-safe |
| WebSocket fills | ws + typed events, no threading.Event brittleness |
| BigInt math (wei, hex, bytes32) | native BigInt, no float-rounding bugs |
| HTTP latency | undici keep-alive + HTTP/2 ≈ 3× faster than requests.Session |
| EIP-712 type 1:1 with Solidity | compile-time guarantee |
| `fire_arb` concurrency | async/await + Promise.all, no GIL |
| SDKs | `@polymarket/clob-client-v2`, `@sx-bet/sportx-js`, `@limitless-exchange/sdk` |

### Architecture topology

Before TS-3:
```
plan-kapkan-radar (Python) — detector + executor in same process
```

After TS-3 cutover (the current shape):
```
plan-kapkan-radar (Python)   POST /fire   plan-kapkan-executor-ts (Node)
   detector + dispatch       ─────────>      atomic engine + Fastify on :5051
```

The radar switches between the two by setting `EXECUTOR_URL` in
`Credentials.env`. Empty = legacy Python in-process. `http://executor-ts:5051`
= the TS service.

### TS-1 / TS-2 / TS-3 PR list

- **TS-1** PR #18 (skeleton)
- **TS-2** PR #108 (builders)
- **TS-3** PR #128-#149 (atomic engine + Fastify + cutover)

---

## Phase TS-5 (09.05.2026): real fires + observability + recovery

Origin: `docs/TS_PHASE5_PLAN.md`.

### Why this phase

TS-3 was a **dry-run-only** skeleton — it received `POST /fire` from the
Python detector, logged the decision to `dryrun.jsonl`, but did NOT
actually POST orders to exchanges. TS-5 wires real fires + observability
so that `DRY_RUN=0` becomes safe to flip.

### What TS-5 included

| Sub-phase | LoC | Files |
|---|---|---|
| **TS-5a** real HTTP firing | ~500 | `fire/{poly,sx,lim}_post.ts` — POST /order with retries + CB |
| **TS-5b** wallet pool + signer registry | ~300 | `wallets/{pool,signers}.ts` |
| **TS-5b2** Limitless user-channel Socket.IO listener | ~250 | `ws/limitless_user_ws.ts` |
| **TS-5c** slippage check + revert planner | ~200 | `executor/{slippage,revert}.ts` |
| **TS-5c.2** real-mode fires + revert execution | ~400 | `executor/{atomic,revert}.ts` |
| **TS-5d** signer registry + expectFill helper | ~150 | `wallets/signers.ts`, `executor/fills.ts` |
| **TS-6** Polymarket L2 HMAC + DELETE /order cancel-on-timeout | ~250 | `lib/poly_hmac.ts`, `fire/poly_post.ts` |
| **TS-6.2** Limitless DELETE /orders/{id} cancel-on-timeout | ~150 | `fire/lim_post.ts` |

PR list: #128 → #137, with #138-#143 wiring observability (`/api/ts_metrics`,
nginx whitelist, etc.).

### Result

The TS executor reached production-ready state on 11.05.2026 with
`/api/ts_metrics` showing `signers_registered=6` and `total_fires` 
incrementing through paper-trade dispatches. The blocking work before
`DRY_RUN=0` flip is operator-side: fund the 6 wallets + derive L2 creds +
record 100 paper-trade graduation gate.

---

## Phase audit-2 / audit-3 / audit-4 (11.05.2026 → 15.05.2026)

After TS-5/TS-6 deploy, three audit waves identified gaps:

- **audit-2** (11.05.2026) — pipeline_timing + CP leg identifiers +
  scan_tick instrumentation + exchange_rtt probe + analytics
  unique_count + scan_breakdown.
- **audit-3** (15.05.2026) — Polymarket WS required mode + Limitless WS
  required mode + negrisk conditional-binary scope guard + Polymarket
  fee_schedule object readiness + scan_health sparkline + async-zero
  scan_breakdown bug + positions open/resolved split (PR-E).
- **audit-4** (15.05.2026 PM) — fire_filled events carry end_date +
  per-leg identifiers (slug/market_hash/condition_id/token_id) so the
  dashboard can split positions into open / resolved and compute Real
  P&L client-side.

For PR-by-PR detail, see `CHANGELOG.md`.

---

## Phase audit-28 (27.05.2026): post-TS refactor

After audit-2/3/4 stabilised the data plane, audit-28 dismantles the
`arb_server.py` monolith into the new `Scripts/radar/` package. See
`docs/ARCHITECTURE.md` for current state + migration plan.

---

## Sources (preserved from original planning docs)

- [@polymarket/clob-client-v2 on npm](https://www.npmjs.com/package/@polymarket/clob-client-v2)
- [Polymarket V2 Migration Guide](https://docs.polymarket.com/v2-migration)
- [SX Bet API Documentation](https://api.docs.sx.bet/)
- [@limitless-exchange/sdk](https://www.npmjs.com/package/@limitless-exchange/sdk)
- [viem signTypedData](https://viem.sh/docs/actions/wallet/signTypedData.html)
- [Fastify vs Express 2026](https://www.pkgpulse.com/blog/express-vs-fastify-2026)
