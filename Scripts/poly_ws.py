"""
Polymarket WebSocket client for the CLOB market channel.

Endpoint: wss://ws-subscriptions-clob.polymarket.com/ws/market (no auth).
Docs: https://docs.polymarket.com/developers/CLOB/websocket/wss-overview

Design notes
────────────
- We push the full set of token_ids to (re)subscribe whenever the HOT+NEAR pool
  changes. The server expects an `assets_ids` payload — there is no documented
  partial subscribe/unsubscribe, so we close+reopen the socket when the set
  changes. This is the same pattern most production trackers use.
- Strict caps to avoid a ban:
    * MAX_SUBS  — never subscribe to more than this many tokens at once.
    * Backoff   — exponential 1→2→4→8→30s, never tighter than 1s.
    * Heartbeat — PING (plain text) every 10s; if no PONG for 30s, reconnect.
    * Throttle  — per-token callbacks coalesced via a 250ms tick to avoid
                  flooding the rest of the app on hot markets.
- The client owns nothing about pricing logic — it just maintains a dict
  `books[token_id] = {"best_ask": float, "depth": float, "ts": float}` and
  fires `on_update(token_id)` after coalescing.
"""
from __future__ import annotations

import json
import threading
import time
from collections import defaultdict
from typing import Callable, Dict, Iterable, Optional, Set

import websocket  # websocket-client


WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PING_INTERVAL = 10               # seconds — Polymarket spec
PONG_TIMEOUT = 30                # seconds — declare dead if no traffic this long
BACKOFF_SCHEDULE = [1, 2, 4, 8, 30]
COALESCE_TICK_MS = 250           # batch callbacks within this window
DEFAULT_MAX_SUBS = 200


class PolyMarketWS:
    """Background WS client. Thread-safe.

    Public API:
        update_subscriptions(token_ids: Iterable[str])
        get_book(token_id: str) -> dict | None
        get_metrics() -> dict
        start() / stop()
    """

    def __init__(
        self,
        on_update: Optional[Callable[[str], None]] = None,
        max_subs: int = DEFAULT_MAX_SUBS,
        verbose: bool = False,
    ):
        self.on_update = on_update or (lambda _tid: None)
        self.max_subs = max_subs
        self.verbose = verbose

        # Subscription state
        self._desired: Set[str] = set()
        self._active: Set[str] = set()
        self._lock = threading.RLock()

        # Order books: token_id -> {"best_ask": float, "depth": float, "ts": float}
        self.books: Dict[str, dict] = {}

        # Coalescing buffer: token_ids that changed since last flush
        self._dirty: Set[str] = set()
        self._dirty_lock = threading.Lock()

        # Connection state
        self._ws: Optional[websocket.WebSocketApp] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._coalesce_thread: Optional[threading.Thread] = None
        self._stop_flag = threading.Event()
        self._last_msg_ts = 0.0
        self._reconnect_count = 0
        self._connect_attempts = 0

        # Metrics
        self._msg_window = []      # timestamps of recent messages for msg/s rate
        self._msg_window_lock = threading.Lock()

    # ── Public ────────────────────────────────────────────────
    def start(self) -> None:
        """Spawn background threads. Idempotent."""
        if self._ws_thread and self._ws_thread.is_alive():
            return
        self._stop_flag.clear()
        self._ws_thread = threading.Thread(target=self._run_forever, daemon=True, name="PolyWS")
        self._ws_thread.start()
        self._coalesce_thread = threading.Thread(target=self._coalesce_loop, daemon=True, name="PolyWS-coalesce")
        self._coalesce_thread.start()

    def stop(self) -> None:
        self._stop_flag.set()
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass

    def update_subscriptions(self, token_ids: Iterable[str]) -> None:
        """Set the desired subscription set. Triggers reconnect if it changed."""
        new_set = {t for t in token_ids if t}
        if len(new_set) > self.max_subs:
            # Keep deterministic order — preserve hottest first if caller sorted
            new_set = set(list(new_set)[: self.max_subs])
            self._log(f"capped subs to {self.max_subs}")
        with self._lock:
            if new_set == self._desired:
                return
            self._desired = new_set
        # Force a reconnect to apply new subscription list
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass

    def get_book(self, token_id: str) -> Optional[dict]:
        # Phase 9uu: lock the read. Without this, a concurrent _handle_event
        # mutating `books` from the WS callback thread could trigger
        # `RuntimeError: dictionary changed size during iteration` if the
        # caller iterates books.keys() then calls get_book in a tight loop.
        with self._lock:
            return self.books.get(token_id)

    def get_metrics(self) -> dict:
        with self._lock:
            subs = len(self._active)
            desired = len(self._desired)
        with self._msg_window_lock:
            now = time.time()
            self._msg_window = [t for t in self._msg_window if now - t < 5]
            msg_per_sec = round(len(self._msg_window) / 5.0, 1)
        last_age = (time.time() - self._last_msg_ts) if self._last_msg_ts else None
        return {
            "subs_active": subs,
            "subs_desired": desired,
            "subs_max": self.max_subs,
            "msg_per_sec": msg_per_sec,
            "reconnects": self._reconnect_count,
            "last_msg_age_sec": round(last_age, 1) if last_age is not None else None,
            "connected": bool(self._ws and subs > 0),
        }

    # ── Internals ─────────────────────────────────────────────
    def _log(self, *args) -> None:
        if self.verbose:
            print("[PolyWS]", *args, flush=True)

    def _run_forever(self) -> None:
        """Connect → run → on disconnect, backoff and retry."""
        while not self._stop_flag.is_set():
            with self._lock:
                desired = list(self._desired)
            if not desired:
                # Nothing to subscribe — idle wait, do not open socket
                time.sleep(2)
                continue

            self._connect_attempts += 1
            try:
                self._ws = websocket.WebSocketApp(
                    WS_URL,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                # ping_interval/ping_payload are NOT used here — Polymarket expects
                # plain "PING" text, not a WS control frame. We send manually.
                self._ws.run_forever(ping_interval=0)
            except Exception as e:
                self._log(f"run_forever exception: {e}")

            # Disconnected — clear active set, schedule backoff
            with self._lock:
                self._active.clear()
            if self._stop_flag.is_set():
                break
            self._reconnect_count += 1
            delay = BACKOFF_SCHEDULE[min(self._connect_attempts - 1, len(BACKOFF_SCHEDULE) - 1)]
            self._log(f"backoff {delay}s before reconnect")
            self._stop_flag.wait(delay)

    def _on_open(self, ws) -> None:
        with self._lock:
            tokens = list(self._desired)[: self.max_subs]
            self._active = set(tokens)
        payload = {
            "assets_ids": tokens,
            "type": "market",
            "custom_feature_enabled": True,
        }
        try:
            ws.send(json.dumps(payload))
            self._log(f"subscribed to {len(tokens)} tokens")
            self._connect_attempts = 0  # successful handshake — reset backoff
            self._last_msg_ts = time.time()
        except Exception as e:
            self._log(f"subscribe send failed: {e}")
            try: ws.close()
            except Exception: pass
            return

        # Start watchdog + ping thread for this connection
        threading.Thread(target=self._heartbeat_loop, args=(ws,), daemon=True, name="PolyWS-hb").start()

    def _heartbeat_loop(self, ws) -> None:
        while not self._stop_flag.is_set():
            time.sleep(PING_INTERVAL)
            if self._ws is not ws:
                return  # connection has been replaced
            # Watchdog: kill stale connections
            if self._last_msg_ts and (time.time() - self._last_msg_ts) > PONG_TIMEOUT:
                self._log("pong timeout — forcing reconnect")
                try: ws.close()
                except Exception: pass
                return
            try:
                ws.send("PING")
            except Exception:
                return

    def _on_message(self, ws, msg) -> None:
        self._last_msg_ts = time.time()
        with self._msg_window_lock:
            self._msg_window.append(self._last_msg_ts)

        # Server PONG comes back as plain text "PONG" — ignore
        if msg in ("PONG", "pong"):
            return

        try:
            data = json.loads(msg)
        except Exception:
            return

        # API may send a single object or a list of events
        events = data if isinstance(data, list) else [data]
        for ev in events:
            self._handle_event(ev)

    def _handle_event(self, ev: dict) -> None:
        ev_type = ev.get("event_type") or ev.get("type")
        token_id = ev.get("asset_id") or ev.get("market") or ev.get("asset")
        if not token_id:
            return

        # Phase 9uu: every books mutation must hold _lock — same dict that
        # get_book reads under lock. RLock allows nesting if _mark_dirty
        # ever needs to take it too.
        if ev_type == "book":
            asks = ev.get("asks") or []
            best_ask, depth = self._calc_book(asks)
            if best_ask is not None:
                with self._lock:
                    self.books[token_id] = {"best_ask": best_ask, "depth": depth, "ts": time.time()}
                self._mark_dirty(token_id)

        elif ev_type == "price_change":
            # changes = list of {price, size, side}; rebuild ask side from current snapshot is heavy.
            # Cheap path: refresh best_ask from delta if it touches asks.
            changes = ev.get("changes") or []
            asks_changed = [c for c in changes if c.get("side", "").upper() == "SELL" or c.get("side") == "ask"]
            if not asks_changed:
                return
            # Conservative: take the lowest sell price from this delta as candidate best_ask
            candidate = min((float(c["price"]) for c in asks_changed if float(c.get("size", 0)) > 0), default=None)
            with self._lock:
                cur = self.books.get(token_id)
                if candidate is not None and (cur is None or candidate <= cur.get("best_ask", 1.0) + 1e-9):
                    depth = cur["depth"] if cur else 0.0
                    self.books[token_id] = {"best_ask": candidate, "depth": depth, "ts": time.time()}
                    self._mark_dirty(token_id)

        elif ev_type == "best_bid_ask":
            ask = ev.get("best_ask")
            if ask is not None:
                with self._lock:
                    cur = self.books.get(token_id)
                    depth = cur["depth"] if cur else 0.0
                    self.books[token_id] = {"best_ask": float(ask), "depth": depth, "ts": time.time()}
                self._mark_dirty(token_id)

        # last_trade_price / tick_size_change / new_market / market_resolved — ignored for now

    @staticmethod
    def _calc_book(asks: list) -> tuple:
        """Phase 10 #51 (30.04.2026) — top-of-book depth only, NOT
        sum-of-all-levels. Old code over-stated depth 5-10x by counting
        liquidity sitting 1-3c above best ask, which becomes "walking the
        book" if a $stake order tries to fill it. For arb sizing we only
        want USD notional at exactly the best ask price.
        """
        if not asks:
            return None, 0.0
        parsed = []
        for a in asks:
            try:
                p = float(a.get("price"))
                s = float(a.get("size", 0))
            except Exception:
                continue
            if p <= 0 or s <= 0:
                continue
            parsed.append((p, s))
        if not parsed:
            return None, 0.0
        parsed.sort(key=lambda x: x[0])
        best = parsed[0][0]
        depth = 0.0
        for p, s in parsed:
            if p > best + 1e-9:
                break
            depth += p * s
        return best, depth

    def _on_error(self, ws, err) -> None:
        self._log(f"error: {err}")

    def _on_close(self, ws, code, reason) -> None:
        self._log(f"closed (code={code} reason={reason})")
        with self._lock:
            self._active.clear()

    def _mark_dirty(self, token_id: str) -> None:
        with self._dirty_lock:
            self._dirty.add(token_id)

    def _coalesce_loop(self) -> None:
        """Flush dirty token_ids to user callback at most every COALESCE_TICK_MS."""
        interval = COALESCE_TICK_MS / 1000.0
        while not self._stop_flag.is_set():
            time.sleep(interval)
            with self._dirty_lock:
                if not self._dirty:
                    continue
                batch = list(self._dirty)
                self._dirty.clear()
            for tid in batch:
                try:
                    self.on_update(tid)
                except Exception as e:
                    self._log(f"on_update raised for {tid}: {e}")
