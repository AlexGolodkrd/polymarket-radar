---
name: residential-proxy-routing
description: Route order-placement HTTP requests through a residential proxy with sticky session per bot wallet. Required before flipping DRY_RUN=0 — Polymarket geoblocks/penalizes VPS IPs, Limitless rate-limits them aggressively (BUG_CATALOG 6.1 / PR #182 saga). The executor-ts currently issues every POST /order from the bare VPS IP 77.91.97.22. Use this skill when wiring proxy support, adding new exchange order paths, or debugging "order rejected — country/IP blocked".
---

# Residential proxy routing — order placement layer

## The problem

Polymarket V2 and Limitless treat order POSTs from datacenter IPs as suspect:

- **Polymarket**: full geoblock for non-US ASNs on `/order` (some markets), additional risk-scoring on shared datacenter ranges. Even with valid L2 creds, an order signed by a US-derived wallet but POSTed from a VPS will sometimes 403.
- **Limitless**: aggressive per-IP rate-limit. We hit 429 cycles at 20-page concurrent fetches (#179) and 8-page reduced concurrency (#181) on the same VPS IP 77.91.97.22. After flipping `DRY_RUN=0`, every order POST competes for the same per-IP budget as the scan reads — first crash predicted within minutes.
- **SX Bet**: less aggressive but still IP-rep checked. Less critical but same proxy infrastructure applies.

The operator derives L2 creds locally through a residential proxy (one-time op per bot). Order PLACEMENT, however, runs continuously from the radar VPS. That mismatch is the blocker.

## Design — proxy pool with sticky session

### Public surface

```typescript
// executor-ts/src/lib/proxy_pool.ts
import { Dispatcher, ProxyAgent } from 'undici';

type Platform = 'polymarket' | 'limitless' | 'sx';

export function getDispatcher(
  platform: Platform,
  botId?: string,
): Dispatcher | undefined;
```

Returns:
- `ProxyAgent` configured for the residential URL if env present
- `undefined` if `PROXY_URL_*` envs missing (transparent fallback to default fetch — preserves current behavior, zero behavioral risk during rollout)

### Sticky session per (platform, botId)

Polymarket binds L2 creds to a wallet. If bot #1 derives creds while seeing IP A, then later POSTs orders through IP B, the server can flag the session as compromised. Sticky-per-bot means:

- Each bot's outbound traffic for `polymarket` uses the SAME exit IP for the bot's lifetime
- Different bots get different exit IPs (anti-detection — looks like 6 retail users, not one farm)
- Provider must support sticky session via username variant (e.g. `user-session-bot1`) or session cookie

Implementation: keep one ProxyAgent per `${platform}:${botId}` key in a module-level Map. ProxyAgent reuses the underlying connection pool, so sticky-session keying is transparent.

### Per-platform override

Different platforms may need different proxy providers:

```
PROXY_URL_DEFAULT=http://user:pass@residential.proxy:8080
PROXY_URL_POLYMARKET=http://user:pass@us-only.proxy:8080  # US exit required
PROXY_URL_LIMITLESS=                                       # empty = use default
PROXY_URL_SX=NONE                                          # explicit: direct, no proxy
```

`NONE` sentinel forces direct (e.g. SX Bet doesn't care, save proxy bandwidth).

### Failure mode: proxy unreachable

When ProxyAgent connect fails (proxy down, bad creds), the executor must NOT silently fall back to direct IP — that would mask the geoblock and produce a wrong-IP signed-order mismatch. Instead:

- Log structured error with `proxy_unreachable: true`
- Trip the platform's circuit breaker
- Return HttpError to atomic.fireArb which aborts the leg

Operator sees in `/api/ts_metrics` that `error_reasons['proxy_unreachable']` is incrementing, knows to check proxy infra.

Env override: `PROXY_FALLBACK_TO_DIRECT=1` opts into the failed-proxy fallback for testing only — never on for real-money mode.

### Integration with http_client.ts

`postJson` already accepts `host` and per-request options. Add an optional `dispatcher` field:

```typescript
export interface PostOptions {
  // ... existing fields ...
  /** Pre-resolved dispatcher (ProxyAgent or undefined for direct). */
  dispatcher?: Dispatcher;
}
```

And in the fetch call:

```typescript
const resp = await fetch(url, {
  method: 'POST',
  headers: ...,
  body: payload,
  signal: ac.signal,
  // @ts-expect-error — undici dispatcher passed through Node's fetch
  dispatcher,
});
```

The per-platform fire modules (`fire/poly_post.ts`, `fire/lim_post.ts`, `fire/sx_post.ts`) resolve dispatcher via `getDispatcher(platform, botId)` once per call.

## What this skill does NOT cover

1. **Python radar's outbound `/book` / `/orderbook` fetches** — radar reads orderbooks from VPS IP today (not order placement). Those Cloudflare-hit endpoints might benefit from proxy too, but the impact is smaller (read APIs are looser than order endpoints) and the change touches `requests.Session.proxies` differently. Follow-up PR.
2. **L2 credential derivation** — operator does this locally with residential connection. One-time, no automation needed.
3. **Proxy provider selection** — vendor evaluation (Bright Data / Smartproxy / Oxylabs / etc.) is operator's call. The skill only documents the env contract.
4. **Anti-fingerprinting beyond IP** — TLS fingerprint, User-Agent rotation, Accept-Language headers. Out of scope; can be added incrementally if blocked.

## Env vars contract

| Env | Default | Effect |
|---|---|---|
| `PROXY_URL_DEFAULT` | unset | Fallback proxy URL for any platform without an override. If unset, direct (current behavior). |
| `PROXY_URL_POLYMARKET` | unset → use default | Per-platform override. |
| `PROXY_URL_LIMITLESS` | unset → use default | Per-platform override. |
| `PROXY_URL_SX` | unset → use default | `NONE` = explicit direct (no proxy). |
| `PROXY_FALLBACK_TO_DIRECT` | `0` | `=1` lets a proxy-unreachable failure fall back to direct VPS IP. NEVER on for real-money. |
| `PROXY_STICKY_SESSION_PATTERN` | `{platform}-{bot}` | How to template the session key into the proxy auth. Provider-specific; usually injected into `username`. |

## Test plan

`executor-ts/tests/proxy_pool.test.ts`:
- No env → `getDispatcher()` returns undefined (direct path preserved)
- `PROXY_URL_DEFAULT` set → returns ProxyAgent
- Per-platform override wins over default
- `NONE` sentinel for one platform → returns undefined despite default
- Same `(platform, botId)` returns SAME instance (sticky session — connection pool reuse)
- Different `botId` returns DIFFERENT instance

`executor-ts/tests/http_client_proxy.test.ts`:
- `postJson({dispatcher: undefined})` → behavior unchanged from baseline
- `postJson({dispatcher: stub})` → request goes through stub dispatcher (intercept + assert URL/headers reached the stub)
- ProxyAgent connect error → HttpError with `proxy_unreachable=true` reason, NO direct fallback unless env opt-in

## Rollout plan

1. Land PR with env contract but defaults preserving current behavior (no `PROXY_URL_DEFAULT` → no change)
2. Operator provisions residential proxy provider, sets `PROXY_URL_DEFAULT` in `Credentials.env`
3. Restart executor-ts, watch `/api/ts_metrics` for `error_reasons['proxy_unreachable']` count = 0 over 30+ minutes
4. Issue 1-2 manual probe orders in dry-run mode → verify they show up at Polymarket/Limitless dashboards with the proxy's IP, not VPS IP
5. Flip `DRY_RUN=0` only after both verifications above

## Risks

- **Latency**: residential proxy adds ~50-200ms per request. Order POSTs go from ~30ms to ~200ms. The atomic.fire_arb 5-second dead-man timer is well within budget.
- **Cost**: residential proxy bandwidth ~$5-15/GB depending on provider. Order POSTs are tiny (~1KB each), so even at 1000 orders/day cost is negligible (<$0.10/day). Read endpoints (if proxy'd later) are larger.
- **Sticky session expiry**: provider may rotate sticky IPs after N minutes. If a session rotates mid-arb, the second leg POST sees a different IP than the first. Tolerable since each leg is independent on the exchange side; just adds anti-detection complexity. Don't optimize for it.

## Why now (before DRY_RUN=0)

The current `paper_stats win_rate=100%` is a TS stub artefact (`realistic_pnl=simPnl`). Once `DRY_RUN=0` flips, real fee deduction (PR #187) AND real IP-based rejections take effect simultaneously. Without proxy, day-one losses won't be from bad arb math — they'll be from 403/429 wiping legs at the wrong time. Land proxy support first; the win_rate calibration with PR #187 will then reflect actual fill quality, not IP-rep noise.
