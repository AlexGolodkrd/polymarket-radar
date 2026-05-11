# Uvicorn — ASGI Production Server

**Source**: diegosouzapw/awesome-omni-skill/skills/development/uvicorn

## When relevant

If we migrate Flask → FastAPI/Starlette (Phase 9eee+) for async support, uvicorn becomes the production server.

For now: we picked **gunicorn** (Phase 9ccc) because Flask is WSGI (sync), not ASGI.

## Quick reference

### Run dev
```bash
uvicorn app:app --reload --host 0.0.0.0 --port 5050
```

### Production (multiple workers)
```bash
uvicorn app:app --workers 4 --host 0.0.0.0 --port 5050
```

### Combined with gunicorn (recommended for ASGI prod)
```bash
gunicorn app:app -k uvicorn.workers.UvicornWorker -w 4 -b 0.0.0.0:5050
```
gunicorn handles process supervision; uvicorn handles ASGI per-process.

## Why we use gunicorn (not uvicorn) currently

Flask is WSGI, not ASGI. uvicorn won't run Flask directly. If we migrate Flask routes to FastAPI:

```python
# Before (Flask, sync):
@app.route('/api/deals')
def api_deals(): ...

# After (FastAPI, async):
@app.get('/api/deals')
async def api_deals(): ...
```

Then switch to uvicorn workers. Until then, gunicorn + sync Flask is the right answer.

## Repository

https://github.com/diegosouzapw/awesome-omni-skill
