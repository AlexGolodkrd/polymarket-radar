# Python REST API Automation Patterns

**Source**: anthonylee991/gemini-superpowers-antigravity (734 ⭐)

## When to use

Building Python that talks to one or more REST APIs:
- ETL/sync jobs
- Webhook handlers
- CLI tools
- Background workers (like our arb-radar)

## Recommended stack

- **httpx** as HTTP client (async-capable, sane timeouts) OR **requests.Session** for sync
- **pydantic-settings** + env vars for config
- stdlib **logging** (no third-party logger frameworks)
- **pytest** for tests

## HTTP requirements (mandatory)

1. **Explicit timeouts on EVERY request** — never default. Tuple form `(connect, read)` to protect against SSL_read C-level hangs.
2. **Centralize HTTP logic** in a single module/class — uniform retry, logging, instrumentation.
3. **Never log Authorization headers** or raw secrets.

## Retry strategy

Retry on:
- Network errors / timeouts (connection refused, read timeout)
- HTTP **429** — honor `Retry-After` header; exponential backoff if absent
- HTTP **5xx** — exponential backoff with jitter

NEVER retry on:
- 4xx errors (except 429) — request is malformed, retry won't help
- Unsafe operations (POST creating duplicates) unless idempotency-keyed

## Pagination

Build helpers that handle **all 3 styles**:
- `next` URLs (HAL/JSON-API)
- Cursor tokens (Stripe, GraphQL)
- `page=N&limit=K` (REST classic — what Polymarket uses)

**Hard limits**: max pages, max items, max elapsed time. Pagination loops MUST be bounded.

## Idempotency

- Use `Idempotency-Key` header when API supports it
- Upserts with stable external IDs
- Lightweight state store (SQLite for OSS, Redis for prod)

## Observability

Every run logs:
- `run_id` (UUID per scan cycle)
- Per-request: method, URL, status, elapsed_ms
- Aggregated: processed / created / updated / skipped / failed

## Verification (test discipline)

- Unit tests for transformation logic
- At least one mocked test for pagination
- At least one mocked test for retry behavior
- Dry-run flag everywhere

## Application to plan-kapkan

Our `arb_server.py` follows MOST of these:
- ✅ Session pool (Phase 9rr)
- ✅ Tuple timeouts (Phase 9rr/9ss)
- ✅ Centralized fetchers (`_fetch_*`)
- ✅ batch_fetch with budget (Phase 9qq.4)
- ✅ Pagination with hard limit (POLY_MAIN_PAGES)
- ✅ Dry-run flag (DRY_RUN=1)
- ❌ No `Retry-After` honoring on 429
- ❌ No structured logging with run_id
- ❌ No idempotency keys on order placement
