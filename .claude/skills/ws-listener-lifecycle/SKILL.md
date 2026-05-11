---
name: ws-listener-lifecycle
description: Robust lifecycle management for WebSocket user-channel listeners in the TS executor — connect, heartbeat, reconnect with jitter, dead-man timeout, market-subscription set diffing, clean shutdown. Use when writing or debugging poly_user_ws / limitless_user_ws / any future user-channel listener (SX Bet, Kalshi). Covers the bugs operator hit during TS-5b1/b2: connected:true but silent zombies, market subset diffing that drops in-flight subscribes, fillRegistry race on reconnect.
---

# WebSocket User-Channel Lifecycle

Operator's mental model: "the WS is connected" should mean "fills will arrive." The real story has 6+ failure modes that look the same from the outside. This skill is the playbook for making a user-channel WS that **really** delivers fills, not one that lies.

## The 6 things every WS listener has to handle

1. **Initial connect** — opening the socket, sending auth, waiting for subscription confirmation
2. **Heartbeat** — proving the socket is alive (server-side may be silent for hours on a low-traffic channel)
3. **Reconnect with jitter** — never use a fixed backoff (synchronized reconnect storms after server hiccup)
4. **Subscription set diffing** — when desired-markets changes, sub/unsub the delta; don't churn the whole set
5. **Dead-man timeout** — if no message in N seconds even when connected, force reconnect (zombie sockets)
6. **Clean shutdown** — `stop()` must terminate the runForever loop AND close the socket AND clear handlers

## The state machine

```
[idle] --start()--> [connecting]
[connecting] --on('open')--> [authenticating]
[authenticating] --auth_ok--> [subscribing]
[subscribing] --sub_ok--> [streaming]
[streaming] --on('message')--> [streaming] (reset deadman)
[streaming] --deadman_expired--> [reconnecting]
[streaming] --on('close')--> [reconnecting]
[reconnecting] --backoff_elapsed--> [connecting]
[any] --stop()--> [stopped]
```

**Critical:** `connected: boolean` should track the `[streaming]` state, NOT `[connecting]` or `[authenticating]`. Operator's instinct ("WS connected = working") is right; make the metric match the instinct.

## Heartbeat vs dead-man timeout — both, not either

- **Heartbeat** (active): send `PING` every 10s, expect `PONG` back. Catches dead sockets where the OS hasn't yet noticed.
- **Dead-man** (passive): if no message of ANY kind in 90s, force reconnect. Catches "subscribed but silent" — usually the auth token expired but the server didn't drop the conn.

Polymarket WS goes silent for hours when no trades happen on subscribed markets — dead-man with 90s window will trigger false reconnects. Solution: send PING and count any message (including PONG) as keep-alive. Reset dead-man on every inbound frame, including PONGs.

## Subscription set diffing — replace, don't recreate

When `updateMarkets(newSet)` is called:
1. Compute `add = newSet - this.desired`
2. Compute `remove = this.desired - newSet`
3. Emit `subscribe` for each `add`, `unsubscribe` for each `remove`
4. Update `this.desired = newSet`

**Why not just close and reopen?** Polymarket's user channel has no atomic subscribe — every reconnect re-sends auth. If atomic.ts pre-subscribes a market 100ms before POST /order, then market subscribe arrives, then fill arrives, then atomic.ts moves on, then the user-channel sees a 5th market and reconnects (closing the entire subscription set), the fill we just got might be dropped if the reconnect is in flight.

**Real bug from TS-5c.3:** `updateMarkets` replaced the desired set wholesale. atomic.ts merged in a new conditionId but the previous N markets were dropped → user-channel reconnected → in-flight fill races with reconnect → fillRegistry never sees the fill → expectFill times out → atomic.ts marks the leg as failed even though the order filled. Fix: callers must read `getDesiredMarkets()`, add to it, then call `updateMarkets(merged)`.

## Reconnect backoff with jitter

```ts
const baseMs = 1000;
const maxMs = 30_000;
const attempt = this.reconnectCount;
const expBackoff = Math.min(baseMs * 2 ** Math.min(attempt, 5), maxMs);
const jitter = Math.random() * expBackoff * 0.3;  // 0-30% jitter
const delayMs = expBackoff + jitter;
```

**Why jitter:** without it, 1000 WS clients reconnecting after a server bounce all hit the server at exactly `T + 1s`, `T + 2s`, `T + 4s` → another DoS. With 30% jitter, the bursts smear over the same window — server stays alive.

## Subscription budget — Polymarket-specific

Polymarket user-channel WS has a hard cap of ~500 subs per connection. If you exceed, the server silently drops your oldest subs without notice. Symptoms: `metrics.subsDesired = 600`, `metrics.subsActive = 500`, and you can't tell which 100 markets are silently dead.

Fix: cap your desired set at 450 (leave headroom) and log a warning when you trim. Better: split into multiple WS connections, one per 400-market shard. See `cross-platform-arbs` skill for sharding strategy.

## Clean shutdown — the `stop()` checklist

```ts
async stop(): Promise<void> {
  this.stopFlag = true;
  if (this.ws) {
    try { this.ws.close(); } catch { /* ignore */ }
    this.ws = null;
  }
  // Don't await runForever — it might be in a sleepInterruptible that checks stopFlag.
  // The fillRegistry janitor in server.ts handles cleanup of stale pending entries.
}
```

**Mistake we made in TS-5b1:** awaited `runForever` Promise in stop() → deadlock because runForever was waiting on `sleepInterruptible(60_000, ...)`. Fix: use a CancellationToken (or just a boolean checked inside the sleep loop) and don't await.

## Metrics that catch real bugs

```ts
getMetrics() {
  return {
    connected: this.streaming,           // [streaming] state, not just socket.open
    subsActive: this.active.size,        // confirmed subscribed
    subsDesired: this.desired.size,      // we wanted to subscribe
    msgPerSec: rolling 5s avg,           // is data flowing?
    reconnects: this.reconnectCount,     // is the connection stable?
    lastMsgAgeSec: now - this.lastMsgTs, // dead-man indicator
    authFailedAt: timestamp or null,     // permanent auth failure
    botId: this.wallet.botId,            // disambiguate in /metrics
  };
}
```

`connected:true / msgPerSec:0 / lastMsgAgeSec:300` = zombie. Operator should set up an alert.

## See also

- `websocket-reliability` — sibling for Python WS clients (poly_ws.py, limitless_ws.py)
- `fillregistry-pattern` — how to bridge WS fills into atomic.fireArb
- `vitest-mocks` — how to test all this without hitting real WS servers
- `circuit-breaker-patterns` — when to give up reconnecting and degrade gracefully
