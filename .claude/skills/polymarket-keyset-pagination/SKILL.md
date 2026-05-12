---
name: polymarket-keyset-pagination
description: Migration plan from offset-based to cursor (keyset) pagination on Polymarket's /events and /markets gamma-api endpoints. Use when modifying event/market discovery loops in arb_server.py or async_fetchers.py, or when Polymarket eventually removes legacy offset support.
---

# Polymarket /events keyset pagination — migration plan

## Why migrate

April 10, 2026: Polymarket added `/markets/keyset` and `/events/keyset` with cursor pagination. Legacy `?offset=N&limit=L` still works but is **deprecated**. The new endpoints:
- Reject `offset` parameter outright
- Use opaque `after_cursor` / `next_cursor` tokens
- Response wrapper: `{ "events": [...], "next_cursor": "..." }`
- Pages have no fixed size — server chooses

Legacy pages don't have a date for removal but expect 3-6 month window before they 410. Plan migration before that.

## Where we use offset today

| File | Line | Endpoint | Pattern |
|---|---|---|---|
| [Scripts/arb_server.py](Scripts/arb_server.py:4913) | 4913 | `gamma-api.polymarket.com/events` | `?closed=false&active=true&limit=500&offset={offset}` sequential fetch |
| [Scripts/async_fetchers.py](Scripts/async_fetchers.py:616) | 616 | same | parallel HTTP/2 — N pages dispatched concurrently, each with its own offset |

The parallel path is the harder one: keyset pagination is **inherently sequential** (you can't dispatch page N+1 until page N's cursor returns). Migration drops some of the perf win from `fetch_poly_events_pages_async`.

## Migration design

### Step 1 — keep legacy + add cursor path, gated by env

```python
USE_KEYSET = os.environ.get('POLY_KEYSET_PAGINATION', '0') != '0'
```

When OFF: existing offset code runs (current behavior).
When ON: new cursor loop runs (sequential, but no rate-limit pressure since fewer concurrent requests).

### Step 2 — sync cursor loop in arb_server.py

```python
def _fetch_poly_events_keyset(max_events=5000):
    events = []
    cursor = None
    while len(events) < max_events:
        url = "https://gamma-api.polymarket.com/events/keyset?closed=false&active=true&limit=500"
        if cursor:
            url += f"&after_cursor={cursor}"
        r = _SESS_POLY.get(url, timeout=_FETCH_TIMEOUT)
        if r.status_code != 200:
            break
        body = r.json() or {}
        page = body.get('events') or []
        if not page:
            break
        events.extend(page)
        cursor = body.get('next_cursor')
        if not cursor:
            break
    return events
```

### Step 3 — async_fetchers.py: drop parallelism for keyset

The parallel path doesn't translate to cursor pagination. Options:
1. **Drop parallel entirely on keyset path** — sequential is fine if endpoint is fast (Polymarket's `/events/keyset` should be <500ms/page).
2. **Hybrid** — keep parallel for the first few "warm" pages, then sequential. Complex.

Recommend option 1. Empirical: 10 sequential pages × 500 events × 300ms = 3s total. Current parallel = ~0.5s. We give up ~2.5s. Worth it for being future-proof.

### Step 4 — tests

Add `tests/test_phase_audit3_poly_keyset_pagination.py`:
- Stub `_SESS_POLY.get` to return canned cursor responses
- Verify the loop terminates on `next_cursor: null`
- Verify it stops at `max_events` cap
- Verify it handles 5xx mid-stream gracefully (returns partial)

### Step 5 — rollout

1. Land PR with `POLY_KEYSET_PAGINATION=0` default.
2. Smoke test in production with `=1` for one scan: compare event counts vs. offset path. Should match within ±5% (some events may be added/closed during the scan).
3. After ≥7 days clean, flip default to `=1`.
4. After Polymarket EOLs legacy offset, remove offset code path.

## Risks

- **Cursor opacity**: We can't predict page boundaries. A scan paused mid-page can't "resume" — we'd refetch from start.
- **Sequential is slower**: ~2-3s extra per scan, but our scan budget (`RUN_SCAN_BUDGET_S`) is 30s+ so this is fine.
- **Server bugs**: New endpoint may have edge cases we hit at scale (e.g., duplicate events across pages, missing `next_cursor` field on empty page). Defensive parsing required.

## Why we kept offset until now

When TS-5 went out, offset pagination was still primary. We had no signal that legacy would EOL soon. Now we have visibility (Polymarket says deprecated 10.04), so plan the migration before they pull the rug.

## Sources
- [Polymarket Changelog 10.04.2026](https://docs.polymarket.com/changelog) — keyset endpoints
- Empirical scan timing: live `/api/scan_health` p50 ~42s — 3s overhead is <8% impact
