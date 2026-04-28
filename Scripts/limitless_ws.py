"""
Limitless Exchange WebSocket client.

Limitless uses **Socket.IO** (not plain WS). Tested 28.04.2026 — connecting
directly with `websocket-client` to wss://ws.limitless.exchange returns
HTTP 503 because the upstream is a Socket.IO server, not a raw WS endpoint.

Endpoint:   wss://ws.limitless.exchange
Namespace:  /markets
Transport:  websocket only (no long-polling fallback)
Auth:       optional X-API-Key header (public market data does NOT need it)
Docs:       https://docs.limitless.exchange/developers/quickstart/websocket
            https://docs.limitless.exchange/developers/sdk/python/websocket

Subscribe — emit a Socket.IO event on the /markets namespace:
    sio.emit("subscribe_market_prices",
             {"marketSlugs": ["..."]},
             namespace="/markets")

Server pushes:
    - "orderbookUpdate"  → {marketSlug, bids:[{price,size}], asks:[{price,size}]}
    - "newPriceData"     → {marketSlug, lastPrice, ...}

Design: same public API as Polymarket's PolyMarketWS so arb_server.py wires
both clients identically. We coalesce per-slug callbacks within
COALESCE_TICK_MS to avoid fan-out storms on hot markets.

If `python-socketio` is unavailable at import time, the class still imports
cleanly (Phase 5 deployments without socketio installed degrade to REST
polling via _fetch_limitless_orderbook). `start()` becomes a no-op and
`get_metrics()` reports `connected=False`.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Dict, Iterable, Optional, Set

try:
    import socketio
    _SOCKETIO_AVAILABLE = True
except Exception:
    socketio = None
    _SOCKETIO_AVAILABLE = False


WS_URL = "wss://ws.limitless.exchange"
WS_NAMESPACE = "/markets"
COALESCE_TICK_MS = 250
DEFAULT_MAX_SUBS = 250


class LimitlessWS:
    """Background Socket.IO client for Limitless Exchange. Thread-safe.

    Public API (mirrors PolyMarketWS):
        update_subscriptions(slugs: Iterable[str])
        get_book(slug: str) -> dict | None
        get_metrics() -> dict
        start() / stop()
    """

    def __init__(
        self,
        on_update: Optional[Callable[[str], None]] = None,
        max_subs: int = DEFAULT_MAX_SUBS,
        verbose: bool = False,
        api_key: Optional[str] = None,
        on_fill: Optional[Callable[[dict], None]] = None,
    ):
        self.on_update = on_update or (lambda _slug: None)
        # `on_fill` fires for every authenticated `orderEvent` push. Phase 9c:
        # we receive both OME (matching-engine) and SETTLEMENT events here.
        # Caller can wire this into atomic.fire_arb's per-leg fill latch so
        # we don't have to wait for the 5s dead-man timer.
        self.on_fill = on_fill or (lambda _ev: None)
        self.max_subs = max_subs
        self.verbose = verbose
        self.api_key = api_key

        # Subscription state — `_active` lags `_desired` until the server ACKs
        self._desired: Set[str] = set()
        self._active: Set[str] = set()
        self._lock = threading.RLock()

        # Per-slug orderbook cache. Single-side per slug (YES leg); NO ask is
        # synthesised by consumers via no-arbitrage (1 - best_yes_bid).
        self.books: Dict[str, dict] = {}

        # Recent fills buffer — last 100 keepable for /api/deals introspection
        self.recent_fills = []
        self._fills_lock = threading.Lock()

        # Positions cache — Phase 9e. Authenticated `positions` events push
        # the current open positions for this account. We aggregate them per
        # (slug, outcome) so risk.reconcile can compare against local
        # positions.jsonl in O(1) instead of polling REST every 60s.
        # Shape: {(slug, outcome_index): {size, side, last_update_ts}}
        self.positions: Dict = {}
        self._positions_lock = threading.Lock()
        self._positions_last_update = 0.0

        # Coalescing buffer
        self._dirty: Set[str] = set()
        self._dirty_lock = threading.Lock()

        # Socket.IO client (lazy: only created on start() so import-time stays cheap)
        self._sio = None
        self._connected = False
        self._stop_flag = threading.Event()
        self._coalesce_thread: Optional[threading.Thread] = None
        self._supervisor_thread: Optional[threading.Thread] = None
        self._reconnect_count = 0
        self._last_msg_ts = 0.0
        self._order_events_subscribed = False

        # Metrics window
        self._msg_window = []
        self._msg_window_lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────
    def start(self) -> None:
        """Spawn supervisor + coalesce threads. Idempotent."""
        if not _SOCKETIO_AVAILABLE:
            self._log("python-socketio not installed — WS disabled, "
                       "falling back to REST polling")
            return
        if self._supervisor_thread and self._supervisor_thread.is_alive():
            return
        self._stop_flag.clear()
        self._supervisor_thread = threading.Thread(
            target=self._supervisor, daemon=True, name="LimitlessWS")
        self._supervisor_thread.start()
        self._coalesce_thread = threading.Thread(
            target=self._coalesce_loop, daemon=True, name="LimitlessWS-coalesce")
        self._coalesce_thread.start()

    def stop(self) -> None:
        self._stop_flag.set()
        sio = self._sio
        if sio is not None:
            try:
                sio.disconnect()
            except Exception:
                pass

    def update_subscriptions(self, slugs: Iterable[str]) -> None:
        """Update the desired subscription set. Diff against active and emit
        only the delta — Socket.IO does not require a reconnect."""
        new_set = {s for s in slugs if s}
        if len(new_set) > self.max_subs:
            new_set = set(list(new_set)[: self.max_subs])
            self._log(f"capped subs to {self.max_subs}")
        with self._lock:
            if new_set == self._desired:
                return
            self._desired = new_set
        self._sync_subscriptions()

    def get_book(self, slug: str) -> Optional[dict]:
        return self.books.get(slug)

    def get_metrics(self) -> dict:
        with self._lock:
            subs = len(self._active)
            desired = len(self._desired)
        with self._msg_window_lock:
            now = time.time()
            self._msg_window = [t for t in self._msg_window if now - t < 5]
            mps = round(len(self._msg_window) / 5.0, 1)
        last_age = (time.time() - self._last_msg_ts) if self._last_msg_ts else None
        return {
            "subs_active": subs,
            "subs_desired": desired,
            "subs_max": self.max_subs,
            "msg_per_sec": mps,
            "reconnects": self._reconnect_count,
            "last_msg_age_sec": round(last_age, 1) if last_age is not None else None,
            "connected": self._connected,
        }

    # ── Internals ─────────────────────────────────────────────────
    def _log(self, *args) -> None:
        if self.verbose:
            print("[LimitlessWS]", *args, flush=True)

    def _supervisor(self) -> None:
        """Owns the Socket.IO client lifecycle. python-socketio handles
        reconnects internally (we set reconnection=True), so this thread
        is mostly idle once connected. We re-enter on hard errors."""
        sio = socketio.Client(reconnection=True, reconnection_attempts=0,
                              logger=False, engineio_logger=False)
        self._sio = sio
        self._register_handlers(sio)

        while not self._stop_flag.is_set():
            with self._lock:
                desired = list(self._desired)
            if not desired:
                # No work — idle for a moment, then re-check.
                time.sleep(2)
                continue
            try:
                headers = {}
                if self.api_key:
                    headers["X-API-Key"] = self.api_key
                sio.connect(
                    WS_URL,
                    headers=headers or None,
                    transports=["websocket"],
                    namespaces=[WS_NAMESPACE],
                    wait=True,
                    wait_timeout=10,
                )
                # Block until disconnect (sio.wait() returns when disconnected).
                sio.wait()
            except Exception as e:
                self._log(f"connect failed: {e}")
            self._connected = False
            self._reconnect_count += 1
            if self._stop_flag.is_set():
                break
            # Backoff capped — python-socketio also has its own backoff but we
            # add a floor so a flapping endpoint doesn't burn CPU.
            time.sleep(min(2 ** min(self._reconnect_count, 5), 30))

    def _register_handlers(self, sio) -> None:
        @sio.event(namespace=WS_NAMESPACE)
        def connect():
            self._connected = True
            self._last_msg_ts = time.time()
            self._log("connected to /markets")
            # Re-subscribe to whatever is desired — server does NOT persist
            # subs across disconnects per docs.
            self._sync_subscriptions()
            # If we have an API key, also subscribe to authenticated channels:
            # subscribe_order_events (fills) — single sub per connection per
            # docs, no payload required. Position updates would need a slug
            # list; we skip that until we actually hold positions.
            self._order_events_subscribed = False
            self._subscribe_order_events()

        @sio.event(namespace=WS_NAMESPACE)
        def disconnect():
            self._connected = False
            self._order_events_subscribed = False
            with self._lock:
                self._active.clear()
            self._log("disconnected from /markets")

        @sio.on("orderbookUpdate", namespace=WS_NAMESPACE)
        def on_orderbook(payload):
            self._touch_msg()
            self._handle_orderbook(payload or {})

        @sio.on("newPriceData", namespace=WS_NAMESPACE)
        def on_price(payload):
            # Lightweight last-price tick. We don't store it (orderbook
            # updates are the source of truth) but it keeps the conn lively.
            self._touch_msg()

        @sio.on("orderEvent", namespace=WS_NAMESPACE)
        def on_order_event(payload):
            # Authenticated channel — receives OME (matching-engine) and
            # SETTLEMENT events when our orders fill. Buffer + invoke
            # the on_fill callback so atomic.fire_arb can confirm fills
            # without waiting for the 5s dead-man timer.
            self._touch_msg()
            ev = payload or {}
            with self._fills_lock:
                self.recent_fills.append({**ev, '_received_at': time.time()})
                # Cap to last 100 to keep memory bounded
                if len(self.recent_fills) > 100:
                    self.recent_fills = self.recent_fills[-100:]
            try:
                self.on_fill(ev)
            except Exception as e:
                self._log(f"on_fill raised: {e}")

        @sio.on("positions", namespace=WS_NAMESPACE)
        def on_positions(payload):
            # Authenticated push. Phase 9e: aggregate into self.positions
            # so risk.reconcile can read a fresh on-chain truth at any
            # time without an extra REST call. Server pushes the FULL
            # position set for the account on every change, so we replace
            # the cache instead of merging — no stale rows possible.
            self._touch_msg()
            self._handle_positions(payload or {})

        @sio.on("authenticated", namespace=WS_NAMESPACE)
        def on_auth(_):
            self._touch_msg()
            self._log("authenticated by server (api_key valid)")

        @sio.on("exception", namespace=WS_NAMESPACE)
        def on_exc(payload):
            self._log(f"server exception: {payload}")

    def _touch_msg(self) -> None:
        self._last_msg_ts = time.time()
        with self._msg_window_lock:
            self._msg_window.append(self._last_msg_ts)

    def _subscribe_order_events(self) -> None:
        """Subscribe to authenticated `orderEvent` stream — fires whenever
        our orders match (OME) or settle on-chain (SETTLEMENT). Per docs:
        "one subscription per connection", no payload, requires X-API-Key.

        We skip this if no api_key was supplied — the connection still works
        for public market data without it."""
        if not self.api_key:
            return
        if self._order_events_subscribed:
            return
        sio = self._sio
        if sio is None or not self._connected:
            return
        try:
            sio.emit("subscribe_order_events", {}, namespace=WS_NAMESPACE)
            self._order_events_subscribed = True
            self._log("subscribed to orderEvent (auth channel)")
        except Exception as e:
            self._log(f"order_events subscribe failed: {e}")

    def _subscribe_positions(self, slugs) -> None:
        """Subscribe to authenticated `positions` events for the given
        market slugs. Phase 9e — feeds the positions cache used by
        risk.reconcile.

        Server pushes one event per slug whenever our open positions on
        that market change (after every fill, settlement, etc). No-op
        without api_key (public data only)."""
        if not self.api_key:
            return
        sio = self._sio
        if sio is None or not self._connected:
            return
        slugs = [s for s in (slugs or []) if s]
        if not slugs:
            return
        try:
            sio.emit("subscribe_positions",
                     {"marketSlugs": slugs[: self.max_subs]},
                     namespace=WS_NAMESPACE)
            self._log(f"subscribed to positions on {len(slugs)} slugs")
        except Exception as e:
            self._log(f"positions subscribe failed: {e}")

    def get_recent_fills(self, limit: int = 20):
        """Return last `limit` orderEvent payloads — used by /api/deals to
        surface fill confirmations on the dashboard."""
        with self._fills_lock:
            return list(self.recent_fills[-limit:])

    def get_positions_snapshot(self) -> Dict:
        """Return a copy of the cached on-chain positions. risk.reconcile
        reads this every 60s. Returns dict keyed by (platform, market_id,
        outcome) so it merges naturally with positions from other platforms.

        If WS hasn't pushed any `positions` event yet (cold start, or no
        api_key), returns an empty dict — reconcile treats empty-remote
        the same as 'no positions to compare'."""
        with self._positions_lock:
            return {
                ('Limitless', slug, outcome): info['size']
                for (slug, outcome), info in self.positions.items()
            }

    def positions_age_s(self) -> Optional[float]:
        """Seconds since the last positions push. None if no push received."""
        if not self._positions_last_update:
            return None
        return time.time() - self._positions_last_update

    def _handle_positions(self, payload: dict) -> None:
        """Parse a `positions` event payload and replace the cache.

        Limitless pushes one of two shapes (per docs):
          AMM:  {account, marketAddress, positions:[...], type:'AMM'}
          CLOB: {account, marketSlug, positions:[...], type:'CLOB'}

        Each `positions[]` entry has at minimum {outcome, size}. We don't
        currently distinguish AMM vs CLOB downstream — both consolidate to
        (slug, outcome) → size. Keeps reconcile platform-agnostic."""
        try:
            slug = payload.get('marketSlug') or payload.get('marketAddress')
            if not slug:
                return
            entries = payload.get('positions') or []
            new_rows = {}
            for p in entries:
                outcome = (p.get('outcome') if 'outcome' in p
                           else p.get('outcomeIndex'))
                if outcome is None:
                    continue
                size = p.get('size') or p.get('amount') or 0
                try: size = float(size)
                except Exception: continue
                if size == 0:
                    continue
                new_rows[(slug, outcome)] = {
                    'size': size,
                    'side': p.get('side'),
                    'last_update_ts': time.time(),
                }
            with self._positions_lock:
                # REPLACE positions for this slug only — server may push
                # one slug at a time on incremental updates. We keep
                # other slugs untouched.
                # Drop any cached rows for this slug
                for k in list(self.positions.keys()):
                    if k[0] == slug:
                        del self.positions[k]
                self.positions.update(new_rows)
                self._positions_last_update = time.time()
        except Exception as e:
            self._log(f"_handle_positions error: {e}")

    def _sync_subscriptions(self) -> None:
        """Push the current desired set to the server. Socket.IO supports
        partial sub/unsub (per docs the server tracks slugs per session) so
        we send the full desired set on every change — server treats as a
        replace. If we're not yet connected, the on-connect handler picks
        this up via the same code path."""
        sio = self._sio
        if sio is None or not self._connected:
            return
        with self._lock:
            slugs = list(self._desired)[: self.max_subs]
        try:
            sio.emit("subscribe_market_prices",
                      {"marketSlugs": slugs},
                      namespace=WS_NAMESPACE)
            with self._lock:
                self._active = set(slugs)
            self._log(f"subscribed to {len(slugs)} slugs")
        except Exception as e:
            self._log(f"subscribe emit failed: {e}")
        # Phase 9e: also (re)subscribe to authenticated positions for the
        # same slug set — keeps the local positions cache in sync with the
        # currently-watched markets. No-op without api_key.
        self._subscribe_positions(slugs)

    def _handle_orderbook(self, payload: dict) -> None:
        slug = payload.get("marketSlug") or payload.get("slug")
        if not slug:
            return
        book = self._parse_orderbook(payload)
        if not book:
            return
        self.books[slug] = book
        with self._dirty_lock:
            self._dirty.add(slug)

    def _parse_orderbook(self, payload: dict) -> Optional[dict]:
        try:
            asks = payload.get("asks") or []
            bids = payload.get("bids") or []
            best_yes_ask = float(asks[0]["price"]) if asks else None
            best_yes_bid = float(bids[0]["price"]) if bids else None
            depth_yes = sum(float(o["price"]) * float(o["size"]) for o in asks[:5])
            depth_no_synth = sum(float(o["price"]) * float(o["size"]) for o in bids[:5])
            return {
                "best_yes_ask": best_yes_ask,
                "best_yes_bid": best_yes_bid,
                "depth_yes": depth_yes,
                "depth_no": depth_no_synth,
                "ts": time.time(),
            }
        except Exception:
            return None

    def _coalesce_loop(self) -> None:
        tick_s = COALESCE_TICK_MS / 1000.0
        while not self._stop_flag.is_set():
            time.sleep(tick_s)
            with self._dirty_lock:
                if not self._dirty:
                    continue
                batch = list(self._dirty)
                self._dirty.clear()
            for slug in batch:
                try:
                    self.on_update(slug)
                except Exception as e:
                    self._log(f"on_update({slug}) raised: {e}")

    # ── Test helpers ──────────────────────────────────────────────
    def _handle_event(self, ev: dict) -> None:
        """Test shim — accept the same event shape unit tests use against
        the v1 plain-WS implementation, so test_limitless WS suite keeps
        passing without socket.io stubbing."""
        et = ev.get("event") or ev.get("type")
        data = ev.get("data") or ev
        if et in ("orderbookUpdate", "orderbook_update"):
            self._handle_orderbook(data)
