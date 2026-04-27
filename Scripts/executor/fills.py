"""Fill confirmation listener — Phase 2 stub.

In real (post-graduation) execution, each bot wallet keeps an open WS
connection to the platform's user channel:
    Polymarket:  wss://ws-subscriptions-clob.polymarket.com/ws/user
    SX Bet:      wss://api.sx.bet/orders/user
On a fill event, we map (order_id → arb_id, leg_idx) via an in-memory
registry and notify atomic.fire_arb's blocked future so it can decide
slippage / dead-man / reversal.

Phase 2 scope: skeleton only. The hot path (dry-run) does NOT need fills —
it logs the decision and post-hoc evaluates against a fresh orderbook
fetch in dryrun_log. Fills come online in Phase 4 once wallet keys are
provisioned and the user channel is reachable.

We expose enough of the API surface that atomic.py can import it without
ImportError, and Phase 4 can drop the real implementation in without
touching atomic.py.
"""
import logging
import threading
import time
from typing import Dict, Optional, Callable

log = logging.getLogger(__name__)


class FillRegistry:
    """In-memory map: platform_order_id → (arb_id, leg_idx, callback).
    Phase 4 will populate this on every real POST /order. Phase 2 — empty."""
    def __init__(self):
        self._lock = threading.Lock()
        self._map: Dict[str, dict] = {}

    def register(self, platform: str, order_id: str, arb_id: str,
                 leg_idx: int, on_fill: Optional[Callable] = None):
        with self._lock:
            self._map[f"{platform}:{order_id}"] = {
                'arb_id': arb_id, 'leg_idx': leg_idx,
                'on_fill': on_fill, 'registered_at': time.time(),
            }

    def consume(self, platform: str, order_id: str) -> Optional[dict]:
        """Pop and return the registration on fill. None if unknown."""
        with self._lock:
            return self._map.pop(f"{platform}:{order_id}", None)

    def pending_count(self) -> int:
        with self._lock:
            return len(self._map)


# Singleton — atomic.py imports this when wiring real fire (Phase 4)
registry = FillRegistry()


def start_listeners(wallets, registry=registry):
    """Phase 2 stub — no-op. Phase 4 will spin up one WS thread per wallet
    and dispatch fill events into `registry.consume()`. Returning early is
    intentional so atomic.py can call this without crashing in dry-run."""
    log.info("fills.start_listeners() called with %d wallets — Phase 2 stub, "
             "no WS connections opened", len(wallets) if wallets else 0)
    return None


def stop_listeners():
    """Phase 2 stub — no-op. Phase 4 will close WS connections + flush
    pending registrations (kill-switch path from Phase 3)."""
    log.info("fills.stop_listeners() — Phase 2 stub")
    return None
