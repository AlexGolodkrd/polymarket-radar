# HTTP Rate Limiting & 403/429 Handling

**Sources**: HTTPX docs, Tenacity, ScrapeOps playbooks, will-ockmore/httpx-retries.

## When to use

Backend returns 403 / 429 / TLS hangs at high concurrency. Symptoms:
- Sometimes works fine, sometimes hangs for minutes
- Concurrent N+ requests trigger throttle
- Single-thread fine, parallel fails

This is exactly Limitless API: 30 concurrent → 4-second TLS hangs + 403 Forbidden.

## Strategies (in order of effectiveness)

### 1. HTTP/2 multiplexing (what dr-manhattan uses)

One TCP+TLS connection, many parallel HTTP requests inside it via streams.
Server sees ONE client connection; rate limit is "per connection" → bypassed.

```python
import httpx
async with httpx.AsyncClient(http2=True, limits=httpx.Limits(
    max_keepalive_connections=1,    # ONE connection
    max_connections=1,
)) as client:
    tasks = [client.get(url) for url in urls]
    results = await asyncio.gather(*tasks)
```
Caveat: server must support HTTP/2 (Limitless does — Cloudflare front).

### 2. Exponential backoff with jitter

When 429/503 hits, wait `2^attempt * (1 + random.random())` seconds before retry.
Honor `Retry-After` header if present.

```python
import asyncio, random, httpx

async def fetch_with_backoff(client, url, max_retries=4):
    for attempt in range(max_retries + 1):
        try:
            r = await client.get(url, timeout=8.0)
            if r.status_code in (429, 503):
                # Server says wait
                wait = float(r.headers.get('Retry-After', 2 ** attempt))
                wait += random.random() * 0.5  # jitter
                await asyncio.sleep(wait)
                continue
            r.raise_for_status()
            return r
        except (httpx.TimeoutException, httpx.NetworkError):
            if attempt == max_retries: raise
            await asyncio.sleep(2 ** attempt + random.random())
```

### 3. Concurrency limit (semaphore)

Cap N parallel requests to keep below rate limit. Find ceiling experimentally:

```python
sem = asyncio.Semaphore(8)   # NOT 30 — start low, raise until 429s
async def bounded_fetch(url):
    async with sem:
        return await fetch_with_backoff(client, url)
```

### 4. Token bucket (rate limiter)

When rate limit is "N requests per second", enforce client-side. `aiolimiter`:

```python
from aiolimiter import AsyncLimiter
limiter = AsyncLimiter(max_rate=10, time_period=1.0)  # 10 req/s
async def rate_limited_fetch(url):
    async with limiter:
        return await client.get(url)
```

### 5. Connection pool tuning

Default urllib3 pool is small (10). Up the pool size, but DON'T match concurrency
1:1 — keep some buffer:

```python
adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20, pool_block=True)
session.mount('https://', adapter)
```

### 6. Tenacity decorator (sync + async)

For sync code, use `tenacity` library:

```python
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

@retry(
    retry=retry_if_exception_type((requests.Timeout, requests.ConnectionError)),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    stop=stop_after_attempt(4),
)
def fetch(url):
    return requests.get(url, timeout=5)
```

## Identifying root cause

**403 Forbidden** ≠ rate limit. Possible causes:
- Geo-block (different from IP rate limit) — VPN won't help
- Missing User-Agent → some Cloudflare configs reject empty UA
- Bot detection (cookies, JS challenge) — needs real browser
- Actual rate limit (429 typical, but some servers return 403)

**Diagnostic queries**:
```python
r = await client.get(url, headers={'User-Agent': 'Mozilla/5.0 ...'})
print(r.status_code, r.headers.get('CF-RAY'), r.headers.get('Retry-After'))
```

## Anti-detection patterns (use sparingly — server is enforcing for a reason)

- **Realistic User-Agent**: real browser string, not "python-httpx/0.28"
- **Accept-Language**: "en-US,en;q=0.9"
- **Referer**: api referrer should be the related web domain
- **Time-jitter**: don't send all at exactly :00 millisecond
- **HTTP/2 priority hints**: low priority for non-critical fetches

## Application to plan-kapkan / Limitless

Current symptom: 30 concurrent → 4s TLS hangs → 403.

**Recommended fix path**:
1. **Try HTTP/2 first** (least invasive) — set `http2=True` in our `async_fetchers.py`. ONE connection with N parallel streams.
2. **If still 403** → drop concurrency to 8-10 (semaphore)
3. **Add exponential backoff** on 429/503/timeouts in the async fetcher
4. **dr-manhattan** is essentially HTTP/2 + connection pool best-practices wrapped in their library — replicating their approach in our code is equivalent

## Repository / refs

- HTTPX retries: https://will-ockmore.github.io/httpx-retries/
- HTTPX async: https://www.python-httpx.org/async/
- Tenacity: https://tenacity.readthedocs.io/
- aiolimiter: https://github.com/mjpieters/aiolimiter
- ScrapeOps Python playbook: https://scrapeops.io/python-web-scraping-playbook/
