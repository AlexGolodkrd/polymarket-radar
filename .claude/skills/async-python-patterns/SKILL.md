# Async Python Patterns

**Source**: lifangda/claude-plugins/cli-tool/skills-library/python-development/async-python-patterns

## When to use

Migrating sync I/O bottlenecks to async — exactly our Phase 9eee plan for orderbook fetching.

## Core concepts

### Event loop
Single-threaded scheduler that runs coroutines. `asyncio.run(main())` boots one.

### Coroutines vs Tasks
```python
async def fetch_one(url): ...        # coroutine — lazy, must be awaited

# Just calling does nothing:
coro = fetch_one(url)                # no work yet

# Schedule on event loop:
task = asyncio.create_task(coro)     # runs concurrently with siblings
result = await task                  # collect result
```

### Concurrent fetches (the pattern we need)
```python
async def fetch_all_orderbooks(token_ids):
    async with httpx.AsyncClient() as client:
        tasks = [fetch_book(client, tid) for tid in token_ids]
        results = await asyncio.gather(*tasks, return_exceptions=True)
    return results
```
Single TCP/TLS connection pool, all fetches run interleaved. No GIL contention.

## Patterns we'd use

### 1. Rate limiting via Semaphore
```python
sem = asyncio.Semaphore(30)  # max 30 concurrent (replace MAX_WORKERS=30)
async def fetch_with_limit(client, url):
    async with sem:
        return await client.get(url, timeout=8)
```

### 2. Timeout per task
```python
try:
    async with asyncio.timeout(8.0):
        result = await fetch(url)
except asyncio.TimeoutError:
    return None  # graceful skip
```

### 3. as_completed equivalent
```python
for coro in asyncio.as_completed(tasks, timeout=45):
    try:
        result = await coro
    except asyncio.TimeoutError:
        break  # bail with partial
```

### 4. Run async from sync code
```python
def run_scan_sync():
    return asyncio.run(run_scan_async())
```

## Pitfalls (avoid these)

- ❌ Forgetting `await` — coroutine never runs, returns coroutine object
- ❌ Blocking call inside async function — `time.sleep(1)` blocks event loop. Use `await asyncio.sleep(1)`
- ❌ CPU-bound work in async — `asyncio.run_in_executor()` for that
- ❌ Mixing `requests` (sync) with `httpx` (async) — pick one stack

## Migration plan (Phase 9eee, next session)

1. Replace `requests.Session` → `httpx.AsyncClient` in fetchers
2. Convert `_fetch_*` to `async def`
3. Replace `ThreadPoolExecutor.map` → `asyncio.gather` with semaphore
4. Wrap `run_scan` in `asyncio.run` — Flask route handlers stay sync (they call `asyncio.run` internally for one scan)
5. Expected speedup: 2-3x on full scan (no GIL contention between workers)

## Repository

https://github.com/lifangda/claude-plugins
