# Dr. Manhattan — CCXT-style unified prediction-market API

**Source**: guzus/dr-manhattan
**HUGE relevance to plan-kapkan**: covers Polymarket + Kalshi + Limitless + Opinion + Predict.fun

## What it is

Single Python library that abstracts trading on 5 prediction-market platforms behind a **unified interface** (just like CCXT does for crypto exchanges).

```python
import dr_manhattan as dm

# Same API surface for ALL exchanges
client = dm.Polymarket(private_key=..., funder=...)
markets = client.fetch_markets()  
orderbook = client.fetch_orderbook(market_id)
order_id = client.create_order(...)
client.cancel_order(order_id)
balance = client.fetch_balance()
positions = client.fetch_positions()
```

## Supported exchanges

| Exchange | Network | Auth |
|---|---|---|
| **Polymarket** | Polygon | private_key + funder address |
| **Kalshi** | regulated CEX | API key + RSA cert |
| **Limitless** | Base L2 | private_key |
| Opinion | BNB Chain | API key + private_key + multisig |
| Predict.fun | BNB Chain | API key + private_key |

## What we could borrow

Three things our `arb_server.py` could adopt from dr-manhattan:

### 1. Abstract `Exchange` base class
Currently we have `_fetch_clob`, `_fetch_kalshi_ob`, `_fetch_sx_orders`, `_fetch_limitless_orderbook` — all per-exchange. With dr-manhattan style:

```python
class Exchange(ABC):
    @abstractmethod
    def fetch_orderbook(self, market_id: str) -> OrderBook: ...
    @abstractmethod
    def fetch_markets(self) -> list[Market]: ...
    @abstractmethod
    def create_order(self, ...) -> str: ...
```

Then a single arb evaluator works against any exchange.

### 2. CCXT-style price/probability normalization
> "Prices range from 0 to 1 (exclusive)"

Dr-manhattan enforces this in the `Market` dataclass — we currently scatter `0 < p < 1` checks throughout.

### 3. Async I/O via `httpx.AsyncClient`
For our scan loop — instead of ThreadPoolExecutor with 30 workers, use `asyncio.gather` over httpx's connection pool. Eliminates GIL contention (one of our suspected hangs).

## Direct integration option

We could **import dr-manhattan as a dependency** and replace our hand-rolled clients:

```bash
pip install dr-manhattan
```

```python
from dr_manhattan import Polymarket, Limitless, Kalshi
poly = Polymarket(private_key=..., funder=...)
lim = Limitless(private_key=...)
# ... use unified API
```

This would eliminate ~600 lines of our custom fetcher code and inherit:
- Battle-tested error handling
- Built-in retries
- Async support
- Order lifecycle management (place/cancel/fill watchers)

## Repository

https://github.com/guzus/dr-manhattan
