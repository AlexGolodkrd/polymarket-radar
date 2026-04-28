"""Fill confirmation registry — real implementation, Phase 9e (28.04.2026).

Each platform pushes fill events via its user-channel WS. We map
`(platform, order_id) → (arb_id, leg_idx, threading.Event, result_slot)`
so atomic.fire_arb's per-leg future can block on the Event with a
configurable timeout instead of relying solely on the 5s dead-man timer.

Status as of Phase 9e:
  - **Limitless**: fully wired. arb_server.on_lim_fill() calls
    registry.consume_by_order_id(...) → atomic wakes up immediately.
    Optionally falls back to consume_by_slug(...) when the server gives
    us a SETTLEMENT event without our exact orderId (rare but happens
    on partial-match settlement events).
  - **Polymarket / SX Bet**: register-side is in place; consume side
    requires the per-platform user-channel WS, which needs wallet
    private keys (Phase 4). The registry survives unfilled regs via
    `expire_stale()` so memory doesn't leak.

The registry is process-local. For multi-instance deployments (HA on
VPS), Phase 6 will swap this for a Redis-backed shared registry —
the public API stays the same.
"""
import logging
import threading
import time
from typing import Callable, Dict, Optional

log = logging.getLogger(__name__)

# How long a registration may live before we garbage-collect it. Anything
# over this means atomic gave up via dead-man and the registration is
# orphaned; we drop it on the floor.
REG_TTL_S = 30.0


class FillRegistration:
    """One leg's fill expectation. atomic.py stuffs an Event in here and
    waits on it; the WS handler sets the Event when it sees the fill."""
    __slots__ = ('arb_id', 'leg_idx', 'platform', 'slug', 'order_id',
                 'event', 'result', 'registered_at', 'on_fill')

    def __init__(self, arb_id: str, leg_idx: int, platform: str,
                 slug: Optional[str], order_id: Optional[str],
                 on_fill: Optional[Callable] = None):
        self.arb_id = arb_id
        self.leg_idx = leg_idx
        self.platform = platform
        self.slug = slug
        self.order_id = order_id
        self.event = threading.Event()
        # `result` is populated by consume() before set(). atomic reads it
        # after wait() returns True.
        self.result: Optional[dict] = None
        self.registered_at = time.time()
        self.on_fill = on_fill   # legacy callback path, optional


class FillRegistry:
    """Thread-safe map for fill confirmation.

    Two lookup axes:
      - `(platform, order_id)` — primary, used when the WS event has the
        exact orderId we sent.
      - `(platform, slug)` — secondary, used by SETTLEMENT-style events
        that key on marketSlug. Multiple regs may share a slug; consume
        by slug pops the OLDEST first (FIFO) so partial fills get matched
        in the order legs were fired.
    """
    def __init__(self):
        self._lock = threading.Lock()
        self._by_order_id: Dict[str, FillRegistration] = {}
        self._by_slug: Dict[str, list] = {}   # platform:slug → [reg, ...]

    def register(self, arb_id: str, leg_idx: int, platform: str,
                 slug: Optional[str] = None, order_id: Optional[str] = None,
                 on_fill: Optional[Callable] = None) -> FillRegistration:
        """Create and store a registration. Returns the FillRegistration so
        atomic.py can `wait()` on its event. At least ONE of slug or
        order_id should be set, otherwise the consume side has nothing
        to look up by."""
        reg = FillRegistration(arb_id, leg_idx, platform, slug, order_id, on_fill)
        with self._lock:
            if order_id:
                self._by_order_id[f"{platform}:{order_id}"] = reg
            if slug:
                self._by_slug.setdefault(f"{platform}:{slug}", []).append(reg)
        return reg

    def consume_by_order_id(self, platform: str, order_id: str,
                            result: dict) -> Optional[FillRegistration]:
        """Pop the registration matching (platform, order_id), set its
        Event with the given result. Returns the consumed reg or None."""
        if not order_id:
            return None
        key = f"{platform}:{order_id}"
        with self._lock:
            reg = self._by_order_id.pop(key, None)
            if reg and reg.slug:
                # Also remove from by-slug list to avoid double-consume
                slug_key = f"{platform}:{reg.slug}"
                bucket = self._by_slug.get(slug_key) or []
                if reg in bucket:
                    bucket.remove(reg)
                if not bucket:
                    self._by_slug.pop(slug_key, None)
        if reg is None:
            return None
        reg.result = result
        reg.event.set()
        if reg.on_fill:
            try: reg.on_fill(result)
            except Exception as e: log.warning("on_fill raised: %s", e)
        return reg

    def consume_by_slug(self, platform: str, slug: str,
                        result: dict) -> Optional[FillRegistration]:
        """Pop the OLDEST registration on (platform, slug). Used by
        SETTLEMENT events that don't carry an orderId."""
        if not slug:
            return None
        key = f"{platform}:{slug}"
        with self._lock:
            bucket = self._by_slug.get(key) or []
            if not bucket:
                return None
            reg = bucket.pop(0)
            if not bucket:
                self._by_slug.pop(key, None)
            if reg.order_id:
                self._by_order_id.pop(f"{platform}:{reg.order_id}", None)
        reg.result = result
        reg.event.set()
        if reg.on_fill:
            try: reg.on_fill(result)
            except Exception as e: log.warning("on_fill raised: %s", e)
        return reg

    def expire_stale(self, ttl_s: float = REG_TTL_S) -> int:
        """Drop registrations older than ttl_s. atomic should always wake
        on its dead-man timer and consume its own reg, but just-in-case
        we GC anything still hanging around. Returns count purged."""
        cutoff = time.time() - ttl_s
        purged = 0
        with self._lock:
            for key in list(self._by_order_id.keys()):
                if self._by_order_id[key].registered_at < cutoff:
                    del self._by_order_id[key]
                    purged += 1
            for key in list(self._by_slug.keys()):
                kept = [r for r in self._by_slug[key]
                        if r.registered_at >= cutoff]
                if not kept:
                    del self._by_slug[key]
                    purged += len(self._by_slug.get(key, []))
                else:
                    self._by_slug[key] = kept
        return purged

    def pending_count(self) -> int:
        with self._lock:
            return len(self._by_order_id)

    def metrics(self) -> dict:
        with self._lock:
            return {
                'pending_by_order_id': len(self._by_order_id),
                'slug_buckets': len(self._by_slug),
            }


# Singleton — atomic.py and arb_server import this.
registry = FillRegistry()


# ── Phase 2/4 compat surface ────────────────────────────────────────
def start_listeners(wallets, registry=registry):
    """Phase 4 will spin up one WS thread per wallet for Polymarket /
    SX Bet user channels. Phase 9e: Limitless fills land via the
    existing market-data WS (limitless_ws.LimitlessWS) → arb_server's
    on_lim_fill bridge → registry.consume_by_order_id. So this entry
    point only needs to handle Polymarket / SX (Phase 4)."""
    log.info("fills.start_listeners(): Limitless wired via on_lim_fill bridge; "
             "Polymarket/SX still Phase 4 (need wallet private keys)")
    return None


def stop_listeners():
    """Cleanup hook for kill-switch / shutdown."""
    log.info("fills.stop_listeners()")
    return None
