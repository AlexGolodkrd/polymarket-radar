---
name: limitless-hmac-auth
description: HMAC-SHA256 request signing for Limitless Exchange authenticated endpoints (POST /orders, DELETE /orders/{id}, GET positions, user-WS auth). Replaces the old `X-API-Key` bearer header our code originally used. Without this, every authenticated Limitless request 401s. Required before DRY_RUN=0.
---

# Limitless HMAC authentication scheme

## What changed

Limitless's V2 API requires HMAC-signed requests, not a simple `X-API-Key` bearer.
Original code (pre-14.05.2026) sent only:

```http
X-API-Key: yMgNkJxBzzRUetgb
```

That returns 401 for any authenticated endpoint (POST /orders, DELETE /orders/{id},
GET /positions, subscribe_order_events on WS). The actual scheme uses three
headers and HMAC-SHA256.

## Required headers

| Header | Value |
|---|---|
| `lmts-api-key` | Token ID returned at API key creation (looks like `yMgNkJxBzzRUetgb`) |
| `lmts-timestamp` | ISO-8601 with millisecond precision, must be within ±30s of server time |
| `lmts-signature` | base64(HMAC-SHA256(secret, message)) — see below |

## Canonical message format

```
{ISO-8601 timestamp}\n{HTTP METHOD}\n{path including query string}\n{request body}
```

- `\n` = literal `0x0A` newline
- Path includes query: `/orders/all/btc-100k?onBehalfOf=42`
- For GET requests, body component is the empty string
- For POST with JSON body, body is the EXACT serialized string sent on the wire
  (any reformatting between sign-time and send-time breaks the signature)

## Algorithm

- HMAC key: `base64.decode(secret)` — secret is delivered as base64, must decode
  to raw bytes before HMAC
- HMAC algorithm: HMAC-SHA256
- Output encoding: base64 (standard, not URL-safe; padding included)

## Reference implementations

### Python

```python
import hmac, hashlib, base64
from datetime import datetime, timezone

def sign_lmts(token_id: str, secret: str, method: str,
              path: str, body: str = '') -> dict:
    """Returns the 3 required headers for an authenticated Limitless request.

    `token_id` = api_key from /auth/api-keys response (treat as bearer ID)
    `secret`   = base64-encoded HMAC key from same response
    `method`   = 'GET' / 'POST' / 'DELETE' / etc. uppercase
    `path`     = absolute path INCLUDING query string, e.g. '/orders?market=x'
    `body`     = JSON string sent in request body (empty '' for GET)
    """
    ts = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
    if not ts.endswith('Z') and '+' not in ts[-6:]:
        ts = ts + 'Z'
    msg = f'{ts}\n{method}\n{path}\n{body}'
    sig = base64.b64encode(
        hmac.new(base64.b64decode(secret), msg.encode('utf-8'),
                 hashlib.sha256).digest()
    ).decode('ascii')
    return {
        'lmts-api-key': token_id,
        'lmts-timestamp': ts,
        'lmts-signature': sig,
    }
```

### TypeScript

```typescript
import { createHmac } from 'crypto';

export function signLmtsRequest(
  tokenId: string, secret: string,
  method: string, path: string, body: string = '',
): Record<string, string> {
  const ts = new Date().toISOString();
  const msg = `${ts}\n${method}\n${path}\n${body}`;
  const sig = createHmac('sha256', Buffer.from(secret, 'base64'))
    .update(msg)
    .digest('base64');
  return {
    'lmts-api-key': tokenId,
    'lmts-timestamp': ts,
    'lmts-signature': sig,
  };
}
```

## Clock skew — the silent fail

Server enforces `|now_server - lmts-timestamp| ≤ 30s`. If your machine's clock
drifts (Docker host with stale NTP, container with frozen-at-build time),
every request 401s with no useful error. Mitigation:

- VPS should run NTP / chrony — verify with `timedatectl status` (look for
  `System clock synchronized: yes`)
- Don't pre-compute timestamps far in advance of the actual send
- For batched requests (e.g. cancel-all + new placement), re-sign each one

## Integration points in this repo

`Scripts/limitless_hmac.py` (NEW) — the signer.

Touched files when wiring it in:

- `Scripts/executor/builders.py` — `build_limitless_cancel()` constructs the
  DELETE request; must use HMAC headers instead of `X-API-Key`.
- `Scripts/executor/atomic.py` — any direct Limitless calls that were
  manually adding `X-API-Key`.
- `Scripts/limitless_ws.py` — `subscribe_order_events` requires HMAC auth
  on the WS handshake (separate scheme: pass headers at socket.io connect).
- `executor-ts/src/fire/lim_post.ts` — POST /orders, the real-money fire path.
- `executor-ts/src/lib/limitless_hmac.ts` (NEW) — TS signer.

## Common failure modes

| Symptom | Cause |
|---|---|
| 401 "invalid signature" | Body string reformatted between sign and send (e.g. JSON.stringify with different key order) |
| 401 "timestamp out of range" | Clock skew > 30s; check NTP |
| 401 "invalid api key" | Token ID was revoked (re-creating a token revokes the previous) |
| 200 on GET, 401 on POST | Empty-string body component for GET, but accidentally sending `'null'` or `'undefined'` for POST when body should be `''` |

## Why this skill exists

When we wrote the original Limitless integration (Phase 9b, 28.04.2026), the
docs we found described only `X-API-Key` bearer auth — which seemed to work
for read endpoints. Live verification on 14.05.2026 when operator first
provisioned an API token revealed the real scheme is HMAC. This skill pins
the actual contract so future contributors don't lose the same hour
re-discovering it.

## Sources

- [docs.limitless.exchange/developers/authentication](https://docs.limitless.exchange/developers/authentication.md) — verbatim source for the canonical-string format and TS sample
- Operator's first API token (14.05.2026) — empirical verification
