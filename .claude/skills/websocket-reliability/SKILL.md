---
name: websocket-reliability
description: |
  Robust WebSocket client patterns for Polymarket WS, future Limitless WS,
  and any prediction-market user-channel feed. Covers reconnect with jitter,
  backpressure, heartbeat, dead-man timeout, subscription budgets.
---

# websocket-reliability — устойчивые WebSocket клиенты

## Где WS у нас

| Feed | Status | Cap | Path |
|---|---|---|---|
| `wss://ws-subscriptions-clob.polymarket.com/ws/market` | Active | `MAX_WS_SUBS=1000` | `Scripts/poly_ws.py` |
| `wss://ws-subscriptions-clob.polymarket.com/ws/user` | Active | per wallet | `Scripts/poly_user_ws.py` |
| Limitless socketio | DISABLED | `LIMITLESS_MAX_WS_SUBS=250` | `Scripts/limitless_ws.py` |
| SX Bet WS | НЕ реализован | — | API does have WS — TODO |

## Базовые требования к любому WS клиенту в нашем проекте

1. **Reconnect с exponential backoff + jitter** (от 1s до 60s)
2. **Heartbeat / ping каждые 10-30s** (большинство WS закрывают idle через 60s)
3. **Subscription budget** — никогда не больше чем `MAX_WS_SUBS`
4. **Dead-man timeout** — если 60s нет ни одного message → reconnect
5. **Backpressure** — если internal queue >1000 messages → drop old + alert
6. **Graceful shutdown** — SIGTERM → close socket → drain queue → exit

## Шаблон реализации (на примере poly_ws.py)

```python
import asyncio
import json
import random
import time
import websockets

class ResilientWS:
    def __init__(self, url, on_message, on_state_change=None,
                 max_reconnect_backoff=60, dead_man_timeout=60):
        self.url = url
        self.on_message = on_message
        self.on_state_change = on_state_change or (lambda s: None)
        self.max_backoff = max_reconnect_backoff
        self.dead_man_timeout = dead_man_timeout
        self.subs = set()  # все subscription messages, repeating on reconnect
        self._task = None
        self._running = False
        self._last_message_at = time.time()

    def add_subscription(self, sub_msg):
        self.subs.add(json.dumps(sub_msg, sort_keys=True))

    async def _run(self):
        backoff = 1
        while self._running:
            try:
                self.on_state_change('connecting')
                async with websockets.connect(self.url, ping_interval=15,
                                                ping_timeout=10) as ws:
                    self.on_state_change('connected')
                    backoff = 1  # reset after success
                    # Re-subscribe on every reconnect
                    for sub in self.subs:
                        await ws.send(sub)
                    print(f"[WS] subscribed to {len(self.subs)} channels")
                    self._last_message_at = time.time()
                    # Main loop with dead-man check
                    async for msg in self._iter_messages(ws):
                        self._last_message_at = time.time()
                        try:
                            self.on_message(json.loads(msg))
                        except Exception as e:
                            print(f"[WS] message handler error: {e}")
            except (websockets.ConnectionClosed,
                    asyncio.TimeoutError,
                    OSError) as e:
                self.on_state_change('disconnected')
                print(f"[WS] closed: {e}, backoff {backoff}s")
                await asyncio.sleep(backoff + random.uniform(0, backoff * 0.3))
                backoff = min(backoff * 2, self.max_backoff)

    async def _iter_messages(self, ws):
        """Yield messages with dead-man timeout."""
        while True:
            try:
                msg = await asyncio.wait_for(ws.recv(),
                                              timeout=self.dead_man_timeout)
                yield msg
            except asyncio.TimeoutError:
                age = time.time() - self._last_message_at
                if age > self.dead_man_timeout:
                    raise asyncio.TimeoutError(
                        f"dead_man: no msg for {age:.0f}s")

    def start(self):
        self._running = True
        self._task = asyncio.create_task(self._run())

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
```

## Subscription budget management

Для Polymarket / Limitless: **YES + NO токены = 2 subs per market**.

```python
# Phase 1 (PR #12) расширение: ALL_NO + YES_NO_PAIR требуют NO subs
MAX_WS_SUBS = 1000  # cap. Если HOT+NEAR пул > 500, нужно или поднимать
                    # cap или приоритезировать HOT-only

def collect_poly_tokens(deals_active):
    """Phase 9X — выбираем TOP N tokens by net edge."""
    tokens = set()
    # Sort by attractiveness (highest net edge first)
    sorted_deals = sorted(deals_active, key=lambda d: d.get('net', 0),
                          reverse=True)
    for deal in sorted_deals:
        for entry in deal.get('entries', []):
            token_id = entry.get('token_id_yes') or entry.get('token_id')
            if token_id:
                tokens.add(token_id)
            # Phase 1: ALSO add NO token for structure B/C
            no_id = entry.get('token_id_no')
            if no_id:
                tokens.add(no_id)
            if len(tokens) >= MAX_WS_SUBS:
                break
        if len(tokens) >= MAX_WS_SUBS:
            break
    return list(tokens)[:MAX_WS_SUBS]
```

## Backpressure (queue overflow)

```python
from asyncio import Queue, QueueFull

class BoundedQueue:
    def __init__(self, maxsize=1000):
        self.q = Queue(maxsize=maxsize)
        self.dropped = 0

    def put_nowait(self, item):
        try:
            self.q.put_nowait(item)
        except QueueFull:
            # Strategy: drop oldest, push newest
            try:
                _ = self.q.get_nowait()
            except: pass
            try:
                self.q.put_nowait(item)
                self.dropped += 1
                if self.dropped % 100 == 0:
                    print(f"[WS] backpressure: dropped {self.dropped} msgs")
            except: pass

    async def get(self):
        return await self.q.get()
```

## Heartbeat / ping handling

`websockets` library делает auto-ping (`ping_interval=15`), НО:
- Polymarket требует **application-level** "PING" сообщения каждые 10s (иначе закрывает)
- SX Bet — TBD
- Limitless socketio — встроенный socketio heartbeat

```python
# Polymarket: явный application-level ping
async def _send_app_ping(ws):
    while True:
        await asyncio.sleep(10)
        try:
            await ws.send("PING")
        except websockets.ConnectionClosed:
            return  # connection died, outer loop will reconnect
```

## State transitions visibility

```python
# Опубликовать state в /api/ws_status
WS_STATE = {'polymarket': 'unknown', 'polymarket_user': 'unknown',
            'limitless': 'unknown'}

def on_state_change(host, state):
    WS_STATE[host] = state
    print(f"[WS:{host}] {state}", flush=True)

# В arb_server.py
@app.route('/api/ws_status')
def ws_status():
    return jsonify({'states': WS_STATE,
                    'last_seen': {h: WS.last_message_at(h) for h in WS_STATE}})
```

## Anti-patterns: НЕ делать

```python
# ❌ ПЛОХО — reconnect без backoff (DoS на свой же API)
while True:
    try:
        ws = await connect(url)
        # ...
    except:
        continue  # retry instantly!

# ❌ ПЛОХО — subscriptions хранятся только в одном месте, теряются при reconnect
async with connect(url) as ws:
    await ws.send(sub_msg)  # subscriptions потеряны при следующем connect
    async for msg in ws: ...

# ❌ ПЛОХО — message handler синхронный, блокирует receive loop
async for msg in ws:
    expensive_compute(msg)  # ← receive loop встал на 500ms

# ✅ Правильно — handler через asyncio.create_task
async for msg in ws:
    asyncio.create_task(handle(msg))  # параллельно
```

## Тестирование reconnect

```python
# tests/test_ws_resilience.py
@pytest.mark.asyncio
async def test_reconnect_after_close(mock_ws_server):
    """Server kills connection — клиент должен переподключиться."""
    received = []
    ws = ResilientWS(mock_ws_server.url, on_message=received.append)
    ws.add_subscription({'type': 'subscribe', 'channel': 'ticker'})
    ws.start()
    await asyncio.sleep(0.1)
    mock_ws_server.kill_connection()
    await asyncio.sleep(2)  # wait for reconnect + backoff
    assert mock_ws_server.connection_count >= 2
    assert mock_ws_server.last_subscriptions == ['{"channel":"ticker","type":"subscribe"}']
```

## SX Bet WS (TODO)

SX Bet API имеет WebSocket feed: `wss://api.sx.bet/socket.io/`. Не реализовано в plan-kapkan. План:
1. Использовать `python-socketio` (как `limitless_ws.py`)
2. Subscribe на `markets:{marketHash}` каналы
3. Обрабатывать `orderbook.update` events
4. Cap = `SX_MAX_WS_SUBS` (рекомендую 200 — SX markets имеют меньше depth)

## Refs

- `circuit-breaker-patterns/SKILL.md` — для WS reconnect logic (CB → cool down)
- `observability-stack/SKILL.md` — как лог'ить WS events
- `async-python-patterns/SKILL.md` — общие async patterns
