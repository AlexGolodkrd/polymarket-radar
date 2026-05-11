---
name: fillregistry-pattern
description: The fillRegistry pattern that bridges WebSocket fill events into atomic.fireArb's expectFill promise — a producer-consumer queue with TTL eviction, used in executor-ts to make order-fill awaiting deterministic across reconnects. Use when adding a new exchange's fill stream, debugging "expectFill timed out but order DID fill", or designing the wait-for-fill leg in a new atomic execution path.
---

# fillRegistry — bridging WS fills into atomic fire-and-await

The executor-ts pipeline has two halves that don't talk directly:
- **Producer:** `poly_user_ws.ts` / `limitless_user_ws.ts` listen to per-bot WS user-channels, parse fill messages, push them into the registry.
- **Consumer:** `atomic.fireArb` POSTs an order, gets back an `orderId`, then awaits a fill within `EXPECT_FILL_TIMEOUT_MS`.

The registry is the bus that connects them. Done right, leg-fills are tracked deterministically even across WS reconnects. Done wrong, you get the most expensive bug in the system: an order that fills on-chain but the executor times out and double-orders.

## The data model

```ts
type Pending = {
  orderId: string;       // the exchange's orderId we expect
  slug?: string;         // optional secondary key (Polymarket market slug)
  platform: 'polymarket' | 'limitless' | 'sx';
  registeredAt: number;  // ms; for TTL eviction
  resolve: (event: FillEvent) => void;
  reject: (reason: Error) => void;
};

class FillRegistry {
  private byOrderId = new Map<string, Pending>();
  private bySlug = new Map<string, Pending>();
  // ...
}
```

Two indices because Polymarket's WS includes `slug` but not always `orderId`; SX uses `orderHash`; Limitless uses `orderId`. The producer side falls back to the best available key.

## The producer side (WS listener)

```ts
// poly_user_ws.ts
private handleMessage(msg: PolyUserChannelMessage): void {
  if (msg.event !== 'trade_filled') return;
  const fill: FillEvent = {
    platform: 'polymarket',
    orderId: msg.order_id,
    slug: msg.market,
    filledPrice: parseFloat(msg.price),
    filledSize: parseFloat(msg.size),
    ts: Date.now(),
  };
  // Try by orderId first, slug fallback
  if (!fillRegistry.consumeByOrderId('polymarket', fill.orderId, fill)) {
    fillRegistry.consumeBySlug('polymarket', fill.slug, fill);
  }
  this.recentFills.push(fill);
}
```

**Critical:** the WS listener doesn't `await` anything. It pushes the event and moves on. atomic.ts's promise resolves out-of-band.

## The consumer side (atomic.fireArb)

```ts
// atomic.ts
async function fireLeg(spec: LegSpec, wallet: Wallet, dryRun: boolean): Promise<LegResult> {
  // Pre-subscribe if Polymarket so WS sees the market when fill arrives
  if (spec.platform === 'polymarket' && spec.conditionId) {
    const ws = getPolyUserWS(wallet.botId);
    if (ws) {
      const merged = ws.getDesiredMarkets();  // MERGE, not replace
      merged.add(spec.conditionId);
      ws.updateMarkets(merged);
    }
  }

  // POST the order; get back orderId
  const orderId = await builder.postOrder(signedOrder);

  // Register expectFill BEFORE awaiting — race window is open until then
  const fillPromise = fillRegistry.register({
    orderId,
    slug: spec.marketSlug,
    platform: spec.platform,
    timeoutMs: EXPECT_FILL_TIMEOUT_MS,  // typically 5000
  });

  // Wait — promise resolves when WS pushes fill, or rejects on timeout
  try {
    const fill = await fillPromise;
    return { ok: true, fill };
  } catch (e) {
    return { ok: false, reason: 'fill_timeout', orderId };
  }
}
```

## The 3 race conditions (and how to close each)

### Race 1: WS pushes fill BEFORE atomic.ts registers

Sequence:
1. atomic.ts POSTs order → gets orderId
2. Exchange fills instantly (rare but happens with deep books)
3. WS pushes fill → registry has no pending entry → fill is dropped
4. atomic.ts calls `register(orderId)` → waits 5s → times out

**Fix:** the WS listener also keeps a `recentFills` ring buffer (capacity 100). On `register()`, the registry scans recent fills for a matching orderId before installing the pending entry. If found, resolves immediately.

### Race 2: WS pushes fill DURING reconnect

Sequence:
1. atomic.ts registers `expectFill(orderId=X)`
2. WS user-channel disconnects mid-flight
3. Exchange fills X
4. WS reconnects, re-subscribes — but missed the fill event during the gap
5. atomic.ts times out

**Fix:** on reconnect, the WS listener does a REST `GET /positions` (or equivalent) to reconcile. Any new fills since last seen orderId get pushed through `consumeByOrderId` retrospectively. This is in `risk/reconcile.ts` for the Polymarket path; Limitless equivalent is TS-5e.

### Race 3: TTL eviction races with fill

Sequence:
1. atomic.ts registers with timeoutMs=5000
2. 4990ms in, the registry's janitor fires `expireStale()`, evicts the entry
3. 4992ms, WS pushes fill — registry has no pending entry → drops it

**Fix:** the janitor uses `registeredAt + timeoutMs + GRACE_MS` (grace = 500ms). Plus the fill listener checks `recentFills` on register, so a slightly-late fill registration still wins.

## TTL eviction janitor

```ts
// server.ts startServer()
const janitor = setInterval(() => fillRegistry.expireStale(), 10_000);
app.addHook('onClose', async () => clearInterval(janitor));
```

Without the janitor, registry pending entries leak forever (memory growth ≈ N orders/hour). With it, anything older than `now - timeoutMs - GRACE_MS` gets rejected with `Error('expired')` and removed.

## Testing — the deterministic seam

Tests inject a fake fillRegistry to control timing:

```ts
// vitest test
const fakeReg = {
  register: vi.fn(() => ({
    promise: Promise.resolve({ filledPrice: 0.42, filledSize: 100 }),
  })),
  consumeByOrderId: vi.fn(() => true),
};
const result = await fireLeg(spec, wallet, false, fakeReg);
expect(result.fill.filledPrice).toBe(0.42);
```

See `vitest-mocks` skill for the full pattern.

## Metrics for /metrics endpoint

```ts
metrics() {
  return {
    pending: this.byOrderId.size,
    byOrderId: this.byOrderId.size,
    bySlug: this.bySlug.size,
  };
}
```

If `pending > 5` for more than 30s, the WS listener is either disconnected or filtering out the relevant fills. Alert.

## Anti-pattern: don't wait for fill in the WS handler

Wrong:
```ts
ws.on('trade_filled', async (msg) => {
  // DON'T do this — blocks the WS read loop
  await processFillSlowly(msg);
});
```

Right:
```ts
ws.on('trade_filled', (msg) => {
  fillRegistry.consumeByOrderId('polymarket', msg.order_id, parsed);
  // Move on. Anything slow happens on atomic's side via the promise resolution.
});
```

The WS handler must be O(1) work — anything slower starves the next inbound message and you start dropping fills.

## See also

- `ws-listener-lifecycle` — the producer side
- `cross-exchange-execution` — atomic.ts's built-dict contract
- `polymarket-v2-troubleshoot` — when fills are silently dropped exchange-side
- `vitest-mocks` — testing patterns for this exact bridge
