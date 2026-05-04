"""Polymarket CLOB user-channel WebSocket listener.

Endpoint:  wss://ws-subscriptions-clob.polymarket.com/ws/user
Transport: plain WebSocket (NOT Socket.IO — different from Limitless)
Auth:      first message after open is the subscribe payload with creds:
    {
      "auth":    {"apiKey":"...", "secret":"...", "passphrase":"..."},
      "markets": ["<condition_id_1>", "<condition_id_2>", ...],
      "type":    "user"
    }

Inbound events on this channel (per docs):
  - "trade"  — full lifecycle MATCHED → MINED → CONFIRMED. We bridge MATCHED
               into fills.registry so atomic.fire_arb wakes from event.wait()
               in <250ms instead of the 5s dead-man.
  - "order"  — order placement / update / cancellation. We log but don't
               currently latch on these (atomic latches on trade).

Design mirrors `Scripts/poly_ws.py` (the public market-data WS) so behaviour
across the two Polymarket sockets stays consistent: same heartbeat,
backoff, coalesce patterns. We keep them as separate clients because the
user channel needs auth + market-id-based subscription while market data
is asset-id-based.

If `wallet.has_poly_creds` is False the client is a no-op — the radar
still works in dry-run, just without push-driven fill confirmation.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable, Iterable, Optional

import websocket   # websocket-client


log = logging.getLogger(__name__)

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
PING_INTERVAL = 10
PONG_TIMEOUT = 30
BACKOFF_SCHEDULE = [1, 2, 4, 8, 30]


class PolyUserWS:
    """Authenticated Polymarket user-channel WS client. One instance per
    bot wallet (each wallet has its own L2 credentials).

    Public API:
        update_markets(condition_ids: Iterable[str])
        get_metrics() -> dict
        start() / stop()
    Constructor takes `on_fill(event_dict)` which is fired for every
    `trade` event with status MATCHED — atomic uses this to wake from
    its event.wait(deadman_s).
    """

    def __init__(self,
                 wallet,
                 on_fill: Optional[Callable[[dict], None]] = None,
                 on_order: Optional[Callable[[dict], None]] = None,
                 verbose: bool = False):
        self.wallet = wallet
        self.on_fill = on_fill or (lambda _ev: None)
        self.on_order = on_order or (lambda _ev: None)
        self.verbose = verbose

        # Subscription state — Polymarket subscribes by condition_id (one
        # event), not by token id. _desired holds the current target set.
        self._desired: set = set()
        self._active: set = set()
        self._lock = threading.RLock()

        # Connection state
        self._ws: Optional[websocket.WebSocketApp] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._stop_flag = threading.Event()
        self._last_msg_ts = 0.0
        self._reconnect_count = 0
        self._connect_attempts = 0

        # Recent fills for /api/deals introspection (cap 100)
        self.recent_fills: list = []
        self._fills_lock = threading.Lock()

        # Metrics
        self._msg_window: list = []
        self._msg_window_lock = threading.Lock()

    # ── Public API ──────────────────────────────────────────────
    def start(self) -> None:
        """Spawn the supervisor thread. No-op without poly creds."""
        if not getattr(self.wallet, 'has_poly_creds', False):
            log.info("PolyUserWS(%s): no poly creds — skipping start",
                     getattr(self.wallet, 'bot_id', '?'))
            return
        if self._ws_thread and self._ws_thread.is_alive():
            return
        self._stop_flag.clear()
        self._ws_thread = threading.Thread(
            target=self._run_forever, daemon=True,
            name=f"PolyUserWS-{getattr(self.wallet, 'bot_id', '?')}")
        self._ws_thread.start()

    def stop(self) -> None:
        self._stop_flag.set()
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass

    def update_markets(self, condition_ids: Iterable[str]) -> None:
        """Replace the desired condition_id set. Triggers reconnect (this
        WS doesn't publish a partial sub/unsub)."""
        new_set = {c for c in condition_ids if c}
        with self._lock:
            if new_set == self._desired:
                return
            self._desired = new_set
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass

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
            'subs_active': subs,
            'subs_desired': desired,
            'msg_per_sec': mps,
            'reconnects': self._reconnect_count,
            'last_msg_age_sec': round(last_age, 1) if last_age is not None else None,
            'connected': bool(self._ws and subs > 0),
            'bot_id': getattr(self.wallet, 'bot_id', '?'),
        }

    def get_recent_fills(self, limit: int = 20) -> list:
        with self._fills_lock:
            return list(self.recent_fills[-limit:])

    # ── Internals ───────────────────────────────────────────────
    def _log(self, *args) -> None:
        if self.verbose:
            print(f"[PolyUserWS-{getattr(self.wallet,'bot_id','?')}]", *args, flush=True)

    def _run_forever(self) -> None:
        while not self._stop_flag.is_set():
            with self._lock:
                desired = list(self._desired)
            if not desired:
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
                self._ws.run_forever(ping_interval=0)
            except Exception as e:
                self._log(f"run_forever exception: {e}")
            with self._lock:
                self._active.clear()
            if self._stop_flag.is_set():
                break
            self._reconnect_count += 1
            delay = BACKOFF_SCHEDULE[min(self._connect_attempts - 1,
                                         len(BACKOFF_SCHEDULE) - 1)]
            self._log(f"backoff {delay}s before reconnect")
            self._stop_flag.wait(delay)

    def _on_open(self, ws) -> None:
        with self._lock:
            markets = list(self._desired)
            self._active = set(markets)
        payload = {
            'auth': {
                'apiKey': self.wallet.poly_api_key,
                'secret': self.wallet.poly_secret,
                'passphrase': self.wallet.poly_passphrase,
            },
            'markets': markets,
            'type': 'user',
        }
        try:
            ws.send(json.dumps(payload))
            self._log(f"subscribed to {len(markets)} markets (type=user)")
            self._connect_attempts = 0
            self._last_msg_ts = time.time()
        except Exception as e:
            self._log(f"subscribe send failed: {e}")
            try: ws.close()
            except Exception: pass
            return
        threading.Thread(target=self._heartbeat_loop, args=(ws,),
                         daemon=True, name="PolyUserWS-hb").start()

    def _heartbeat_loop(self, ws) -> None:
        while not self._stop_flag.is_set():
            time.sleep(PING_INTERVAL)
            if self._ws is not ws:
                return
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
        if msg in ("PONG", "pong"):
            return
        try:
            data = json.loads(msg)
        except Exception:
            return
        events = data if isinstance(data, list) else [data]
        for ev in events:
            self._handle_event(ev)

    def _handle_event(self, ev: dict) -> None:
        ev_type = (ev.get('event_type') or ev.get('type') or '').lower()
        # Phase 19v18 (05.05.2026) — auth-error detection. Polymarket
        # sends `{"error": "unauthorized", ...}` envelopes when API creds
        # are wrong/expired. Old code silently ignored these → reconnect
        # loop hammered the server with bad creds → Cloudflare ban risk.
        # Set a long-cooldown flag so the supervisor backs off to 1h
        # instead of 1-30s exponential.
        err = ev.get('error') or ev.get('errorCode')
        if err and isinstance(err, str):
            err_low = err.lower()
            if any(k in err_low for k in ('unauthor', 'invalid_api',
                                            'forbidden', '401', '403')):
                self._log(f"auth error from server: {err} — entering long backoff")
                self._auth_failed_at = time.time()
                # Signal the supervisor to back off
                try:
                    self.stop()
                except Exception:
                    pass
                return
        if ev_type == 'trade':
            with self._fills_lock:
                self.recent_fills.append({**ev, '_received_at': time.time()})
                if len(self.recent_fills) > 100:
                    self.recent_fills = self.recent_fills[-100:]
            try:
                self.on_fill(ev)
            except Exception as e:
                self._log(f"on_fill raised: {e}")
        elif ev_type == 'order':
            try:
                self.on_order(ev)
            except Exception as e:
                self._log(f"on_order raised: {e}")
        # Other event types (subscription confirmation, error) — ignore

    def _on_error(self, ws, err) -> None:
        self._log(f"ws error: {err}")

    def _on_close(self, ws, code, reason) -> None:
        self._log(f"ws closed: {code} {reason}")
