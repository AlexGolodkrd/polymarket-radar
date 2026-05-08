# TS Migration — Phase TS-3 cutover guide

After PRs #108 (TS-2 builders) and the upcoming TS-3 (atomic + Fastify
service) merge, the radar can switch its execution path from in-process
Python to the standalone TypeScript service with a single env var.

## Topology

```
Before TS-3:
  ┌────────────────────────────────────────┐
  │ plan-kapkan-radar  (Python)            │
  │   detector + executor in same process  │
  └────────────────────────────────────────┘

After TS-3 cutover (this PR):
  ┌──────────────────────────┐    POST     ┌────────────────────────────┐
  │ plan-kapkan-radar (Py)   │  /fire      │ plan-kapkan-executor-ts    │
  │   detector only          │  ────────>  │   atomic engine + Fastify  │
  │   classify + classify    │             │   on :5051                 │
  │   POST deal to TS        │  result     │   writes Executions/jsonl  │
  └──────────────────────────┘  <────────  └────────────────────────────┘
            │                                        │
            └────────── shared volume ───────────────┘
                       Executions/
```

Both services write the same `dryrun.jsonl` / `paper_results.jsonl`
schema so the radar's analytics aggregator reads the union.

## Cutover steps

### 1. Verify executor-ts service is up

After auto-deploy completes (run #N in GitHub Actions tab green):
```bash
curl -s http://localhost:5051/healthz | jq
# {"status": "ok", "wallets_loaded": <N>}

curl -s http://localhost:5051/version | jq
# {"commit": "<sha>", "phase": "TS-3", "runtime": "node-typescript", ...}

curl -s http://localhost:5051/risk_status | jq
# Same shape as the radar's /api/risk_status
```

### 2. Flip the radar's switch

In `Credentials.env` on the VPS:
```env
EXECUTOR_URL=http://executor-ts:5051
```

Restart the radar (auto-deploy doesn't trigger on env changes — operator
runs once):
```bash
docker compose restart radar
```

After this, every `fire_arb()` call inside `arb_server.py` POSTs to the
TS executor. `Scripts/arb_server.py` falls back to the in-process Python
executor if the TS service is unreachable — so a brief Node restart
doesn't pause radar operations.

### 3. Verify deals are flowing through TS

Monitor both:
- `https://kapkan.4frdm.live` dashboard — should look identical (CP deals
  still appear, paper-trading still active)
- `docker logs -f plan-kapkan-executor-ts` — every fired arb shows up
  here as a request log entry (Fastify's pino logger)
- `curl -s http://localhost:5051/metrics | jq` — pending fills counter

### 4. Roll back if anything looks off

Just unset the env and restart:
```bash
# Comment out EXECUTOR_URL in Credentials.env
docker compose restart radar
```
The Python in-process executor is still wired and ready — no data loss,
no state delta.

## Phase TS-3 scope (this PR)

Implemented:
- ✅ `executor-ts/src/executor/atomic.ts` — `fireArb` orchestrator
- ✅ `executor-ts/src/executor/paper.ts` — dryrun.jsonl + paper_results
- ✅ `executor-ts/src/executor/fills.ts` — FillRegistry (EventEmitter)
- ✅ `executor-ts/src/risk/state.ts` + `limits.ts` + `killswitch.ts`
- ✅ `executor-ts/src/wallets/pool.ts` — env-loaded wallet pool
- ✅ `executor-ts/src/server.ts` — Fastify with /fire, /version,
     /risk_status, /kill, /healthz, /metrics
- ✅ `executor-ts/Dockerfile` + compose service `executor-ts`
- ✅ `Scripts/arb_server.py` — `EXECUTOR_URL` switch + Python fallback

NOT yet implemented (deferred to TS-5+):
- Real-mode HTTP firing (current TS executor is dry-run only — flips
  the same `DRY_RUN` env as Python to gate real POSTs)
- WS user-channel listeners for fill confirmation (atomic.ts marks
  legs as `dry-fired` without waiting for actual fill events)
- Reconcile loop (Python `Scripts/risk/reconcile.py`)
- Maker mode + presign cache
- On-chain approvals + L2 derivation

## Smoke test

After TS-3 deploys, on the VPS:
```bash
# 1. /healthz returns ok
curl -sf http://localhost:5051/healthz

# 2. /fire accepts a malformed body with 400 (input validation works)
curl -sf -X POST http://localhost:5051/fire \
  -H 'Content-Type: application/json' \
  -d '{}' || echo "expected 400"

# 3. /fire accepts a valid FireRequest and returns ArbFireResult
curl -sf -X POST http://localhost:5051/fire \
  -H 'Content-Type: application/json' \
  -d '{"arbId":"smoke-1","dealTitle":"smoke test","structure":"all_yes",
       "entries":[
         {"platform":"polymarket","tokenId":"123","side":"BUY",
          "expectedPrice":0.5,"expectedSizeUsdc":1.0}
       ],"dryRun":true}'

# Should return JSON with arb_id="smoke-1", leg_status_counts, etc.
# Note: this writes a real row into dryrun.jsonl — operator should
# clean up via the reset workflow afterward if desired.
```

## Rollback strategy

If anything breaks after the env flip:

1. Unset `EXECUTOR_URL` in `Credentials.env`
2. `docker compose restart radar`
3. Inspect `docker logs plan-kapkan-radar` for the bridge errors

The bridge in `arb_server.py:_fire_arb_via_ts` catches all exceptions
and falls back to the Python in-process executor, so even with
`EXECUTOR_URL` set, a downed TS service degrades gracefully rather
than blocks fires.

## TS test suite

```bash
cd executor-ts
npm install
npm test       # Phase TS-1 (poly/sx/limitless builders) +
               # Phase TS-3 (paper, fills) — should be ~22 tests
npm run typecheck
```
