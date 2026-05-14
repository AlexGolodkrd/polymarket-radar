---
name: polymarket-heartbeats-cancel
description: Polymarket's HeartBeats API for server-side auto-cancel of open orders when the executor stops responding. Different from our internal `_heartbeat_loop` (which is an app-level liveness ping). Critical for real-money mode (`DRY_RUN=0`) — without it, an executor crash mid-arb leaves the unfilled leg sitting on the book until manual cancel.
---

# Polymarket HeartBeats API — server-side cancel-on-disconnect

## Two heartbeats — don't confuse them

| Layer | Where | What it does | What it doesn't do |
|---|---|---|---|
| **App-level** (existing) | `Scripts/poly_ws.py:235-244`, `Scripts/poly_user_ws.py:209-212` | Send WS ping every 10s to keep TCP alive; force-reconnect if silent >30s | **Not** known to Polymarket's exchange — server can't tell our connection died |
| **HeartBeats API** (new, NOT IMPLEMENTED) | Polymarket REST: `/heartbeat` register + periodic ping | Server tracks our liveness; auto-cancels open orders if pings stop for N seconds | Not a transport-level mechanism — pure REST |

## Why we need it (real-money mode only)

In dry-run, we don't post orders, so no risk. After `DRY_RUN=0`:

1. Executor sends a 4-leg arb: 2 Polymarket legs + 2 SX legs.
2. Polymarket legs go to `/order` and sit on the book pending match.
3. Executor crashes (OOM, container restart, etc.) before SX legs fill.
4. Polymarket legs **stay on the book** until the price changes enough to fill them — but by then the arb is gone and the fills are pure exposure (unhedged).
5. Operator has to manually cancel via Polymarket UI.

With HeartBeats: when executor stops pinging for N seconds, the server cancels its open orders automatically. Bounded loss.

Per [Polymarket changelog 06.01.2026](https://docs.polymarket.com/changelog): the feature exists for exactly this reason.

## API shape (per docs research, verify before implementing)

Approximate endpoints (read docs to confirm exact paths and payload):

```
POST /heartbeat/register  →  { heartbeatId, intervalMs }   # session start
POST /heartbeat           →  { heartbeatId }              # ping
POST /heartbeat/stop      →  { heartbeatId }              # graceful shutdown
```

Server cancels all open orders signed by the registered wallet if no ping for `intervalMs * grace_factor` (typically 3x).

## Integration design

### Where to wire it

The executor is `executor-ts/` (Fastify on `:5051`). It's the process that signs and POSTs orders. It owns the open-orders state. Logical home:

```typescript
// executor-ts/src/heartbeat/polymarket_heartbeat.ts
export class PolymarketHeartbeat {
  private heartbeatId: string | null = null;
  private intervalMs = 5000;
  private timer: NodeJS.Timeout | null = null;
  private wallet: WalletPool;

  async register(wallet: WalletPool) {
    const r = await fetch('https://clob.polymarket.com/heartbeat/register', {
      method: 'POST',
      headers: this.l2AuthHeaders(wallet),
      body: JSON.stringify({}),
    });
    const body = await r.json();
    this.heartbeatId = body.heartbeatId;
    this.intervalMs = body.intervalMs ?? 5000;
    this.timer = setInterval(() => this.ping(), this.intervalMs / 2);
  }

  async ping() { /* POST /heartbeat */ }
  async stop() { clearInterval(this.timer); /* POST /heartbeat/stop */ }
}
```

### Lifecycle

- On executor boot with L2 creds present → call `register()` for each bot wallet
- On every successful `/order` POST → ensure heartbeat is running (idempotent)
- On graceful shutdown (SIGTERM in container) → call `stop()` for each
- On uncaught exception → DON'T call stop() — let the server auto-cancel

### Failure modes

- **Heartbeat register fails on boot** — log + continue. Don't fail-stop the executor; we can still trade, just without the safety net.
- **Heartbeat ping times out** — log + retry on next interval. If misses cross the grace threshold, the server cancels (which is the WANTED behavior under "executor degraded").
- **Multiple wallets** — each bot wallet needs its own heartbeat session. 6 bots = 6 ping loops. Each interval is independent.

### Observability

Add to `/metrics`:
```
heartbeats_registered: 6
heartbeats_pings_total: 12345
heartbeats_ping_failures: 3
heartbeats_last_ping_ago_ms: 1247
```

When `last_ping_ago_ms` > intervalMs*2, alert.

## NOT implementing this now

Per operator's direction (paper trading still accumulating, DRY_RUN=0 hasn't flipped). This skill is a placeholder for when real-money mode is imminent.

**Prerequisites before implementing**:
- L2 creds in `Credentials.env` for all 6 bots (currently empty per `SESSION_SNAPSHOT_2026-05-12.md:104`)
- Paper trade graduation gate passed (`win_rate >= 70%` over 100 trades) — currently 100/100 ✅
- Operator decision to flip `DRY_RUN=0`

When those align, this skill becomes the implementation guide.

## Sources
- [Polymarket Changelog 06.01.2026](https://docs.polymarket.com/changelog) — HeartBeats API entry
- Our own [Scripts/poly_user_ws.py](Scripts/poly_user_ws.py) for L2 auth header pattern
- Risk runbook: `BUG_CATALOG.md:593` (5.Y — no revert of filled legs) is the closely-related failure mode
