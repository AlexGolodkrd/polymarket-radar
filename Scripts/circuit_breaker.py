"""Circuit breaker for external HTTP services — Phase 9kkk (30.04.2026).

Why we need this
----------------
Limitless (and any external API) can hit transient outages:
  - Cloudflare adaptive rate-limit (1-24h block windows on suspect patterns)
  - Upstream Google Cloud Run cold-start (502/503 bursts)
  - Origin offline (521/522)

Without a breaker the radar keeps hammering the dead endpoint, wasting CPU,
filling logs with errors, and (worst) the 30-day Cloudflare reputation gets
nuked further. With a breaker:
  CLOSED  → normal traffic
    ↓ N consecutive failures
  OPEN    → block all requests, return cached/None instantly
    ↓ cool_down_seconds elapsed
  HALF_OPEN → allow 1 probe; success → CLOSED, failure → OPEN again

Usage
-----
    from circuit_breaker import get_breaker

    cb = get_breaker('limitless')      # singleton per host_key
    if cb.allow():
        resp = await client.get(url)
        if resp.status_code in (200, 304, 404):
            cb.on_success()
        elif resp.status_code in (403, 502, 503, 504, 521, 522):
            cb.on_failure(reason=f'HTTP {resp.status_code}')

The radar surfaces state via /api/circuit_breakers (see arb_server.py).

Phase 9kkk auto-recovery + Telegram alerts
-------------------------------------------
On state transitions we ping the existing notify.py module so the operator
sees a Telegram message:
    ⚠ CB:limitless OPEN — 3 consecutive HTTP 403 (cool-down 300s)
    ✅ CB:limitless CLOSED — recovered after 312s

Operator does NOT need to do anything to recover — half-open auto-probes.
"""
from __future__ import annotations

import time
from enum import Enum
from threading import Lock
from typing import Callable, Optional


class CircuitState(Enum):
    CLOSED = "closed"      # normal traffic
    OPEN = "open"          # blocking — too many failures
    HALF_OPEN = "half_open"  # probing — let one through


class CircuitBreaker:
    """Per-host circuit breaker. Thread-safe.

    Tunable per host:
      failure_threshold  — consecutive failures before OPEN (default 3)
      cool_down_seconds  — wait before HALF_OPEN probe (default 300 = 5min)
      success_threshold  — consecutive HALF_OPEN successes before CLOSED (default 2)
    """

    def __init__(
        self,
        host: str,
        failure_threshold: int = 3,
        cool_down_seconds: int = 300,
        success_threshold: int = 2,
        on_state_change: Optional[Callable[[str, str, str], None]] = None,
    ):
        self.host = host
        self.failure_threshold = failure_threshold
        self.cool_down = cool_down_seconds
        self.success_threshold = success_threshold
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._opened_at: Optional[float] = None
        self._last_failure_reason: Optional[str] = None
        self._total_failures = 0
        self._total_successes = 0
        self._lock = Lock()
        # Optional callback: (host, old_state, new_state, reason) → None
        self._on_state_change = on_state_change
        # Phase 19v16 — pending callbacks queue for deferred dispatch.
        # `_transition` runs inside `self._lock`; the callback (Telegram
        # POST) must NOT block other lock-holders. We stash the args
        # here and `_drain_pending_callbacks()` dispatches outside.
        self._pending_callbacks: list = []

    # ── Hot path ────────────────────────────────────────────────────

    def allow(self) -> bool:
        """Returns True if request should be sent. False = circuit open,
        caller MUST short-circuit and return cached/None without I/O."""
        with self._lock:
            now = time.time()
            if self._state == CircuitState.CLOSED:
                return True
            if self._state == CircuitState.OPEN:
                if self._opened_at and (now - self._opened_at) >= self.cool_down:
                    self._transition(CircuitState.HALF_OPEN, reason='cool_down_elapsed')
                    allowed = True
                else:
                    allowed = False
            else:
                # HALF_OPEN — multiple probes are fine (we'll converge fast).
                allowed = True
        # Phase 19v16 — fire callbacks AFTER releasing the lock
        self._drain_pending_callbacks()
        return allowed

    def on_success(self):
        with self._lock:
            self._total_successes += 1
            self._consecutive_failures = 0
            if self._state == CircuitState.HALF_OPEN:
                self._consecutive_successes += 1
                if self._consecutive_successes >= self.success_threshold:
                    self._transition(CircuitState.CLOSED, reason='recovered')
        self._drain_pending_callbacks()

    def on_failure(self, reason: Optional[str] = None):
        with self._lock:
            self._total_failures += 1
            self._consecutive_successes = 0
            self._consecutive_failures += 1
            self._last_failure_reason = reason
            if self._state == CircuitState.HALF_OPEN:
                # Probe failed — open again immediately
                self._transition(CircuitState.OPEN, reason=f'half_open_probe_failed: {reason}')
                self._opened_at = time.time()
            elif self._state == CircuitState.CLOSED:
                if self._consecutive_failures >= self.failure_threshold:
                    self._transition(CircuitState.OPEN, reason=reason)
                    self._opened_at = time.time()
        self._drain_pending_callbacks()

    # ── Introspection ───────────────────────────────────────────────

    @property
    def state(self) -> str:
        return self._state.value

    def is_open(self) -> bool:
        return self._state == CircuitState.OPEN

    def metrics(self) -> dict:
        with self._lock:
            uptime_pct = None
            total = self._total_successes + self._total_failures
            if total > 0:
                uptime_pct = round(100 * self._total_successes / total, 2)
            opened_for = (time.time() - self._opened_at) if self._opened_at else None
            return {
                'host': self.host,
                'state': self._state.value,
                'consecutive_failures': self._consecutive_failures,
                'consecutive_successes': self._consecutive_successes,
                'total_failures': self._total_failures,
                'total_successes': self._total_successes,
                'uptime_pct': uptime_pct,
                'last_failure_reason': self._last_failure_reason,
                'opened_at': self._opened_at,
                'opened_for_seconds': round(opened_for, 1) if opened_for else None,
                'cool_down_seconds': self.cool_down,
                'failure_threshold': self.failure_threshold,
            }

    # ── Internal ────────────────────────────────────────────────────

    def _transition(self, new_state: CircuitState, reason: str = ''):
        # Phase 19v16 (05.05.2026) — callback dispatch DEFERRED. Callers
        # invoke `_transition` while holding `self._lock`; the default
        # callback POSTs to Telegram (5-10s timeout). Holding the lock
        # across the network call serialised every parallel `allow()` /
        # `on_failure()` for that duration → during a bad-host spike,
        # 30 parallel fetchers stalled. Now `_transition` only mutates
        # state under the lock and stashes the callback args; callers
        # invoke `_drain_pending_callbacks()` AFTER releasing the lock.
        old = self._state.value
        self._state = new_state
        new = new_state.value
        if new_state == CircuitState.CLOSED:
            # Recovery — reset opened_at, but keep totals for metrics
            self._opened_at = None
            self._consecutive_failures = 0
            self._consecutive_successes = 0
        elif new_state == CircuitState.HALF_OPEN:
            self._consecutive_successes = 0
        # Defer side-effect — caller must call _drain_pending_callbacks()
        # outside the lock.
        if self._on_state_change:
            self._pending_callbacks.append((old, new, reason))

    def _drain_pending_callbacks(self):
        """Invoke any pending state-change callbacks. MUST be called
        outside `self._lock` (typically right after a `with self._lock:`
        block in `allow`/`on_success`/`on_failure`)."""
        if not self._pending_callbacks:
            return
        # Snapshot+clear under lock to avoid double-fire
        with self._lock:
            pending = list(self._pending_callbacks)
            self._pending_callbacks.clear()
        for old, new, reason in pending:
            try:
                self._on_state_change(self.host, old, new, reason)
            except Exception as e:
                # Never let callback errors break the breaker
                print(f"[CB:{self.host}] on_state_change error: {e}", flush=True)


# ── Default state-change handler: prints + Telegram via notify.py ──

def _default_state_change_handler(host: str, old_state: str, new_state: str, reason: str):
    """Logs to stdout + (if available) sends Telegram alert via notify.py.

    notify.py is created by PR #22; if it's not importable (testing in
    isolation, or notify removed), we fall back to print-only.
    """
    msg = f"[CB:{host}] {old_state} → {new_state}: {reason}"
    print(msg, flush=True)
    try:
        # Lazy import — notify.py may not be available in tests
        import notify  # type: ignore
        if new_state == 'open':
            notify.send_alert(
                level='warn',
                key=f'cb_{host}_open',
                msg=f'⚠ CB:{host} OPEN — {reason}. Auto-retry in 5 min.',
            )
        elif new_state == 'closed' and old_state == 'half_open':
            notify.send_alert(
                level='success',
                key=f'cb_{host}_recovered',
                msg=f'✅ CB:{host} CLOSED — recovered.',
            )
        # half_open → no alert (transient probe state)
    except ImportError:
        pass
    except Exception as e:
        print(f"[CB:{host}] notify error: {e}", flush=True)


# ── Singleton registry ──────────────────────────────────────────────

_BREAKERS: dict = {}
_BREAKERS_LOCK = Lock()


def get_breaker(
    host: str,
    failure_threshold: int = 3,
    cool_down_seconds: int = 300,
    success_threshold: int = 2,
) -> CircuitBreaker:
    """Get-or-create singleton breaker for `host`. Thread-safe.

    First call wins on parameters; later calls return the existing breaker
    regardless of args (matches singleton intent — params are bootstrap-time).
    """
    with _BREAKERS_LOCK:
        cb = _BREAKERS.get(host)
        if cb is None:
            cb = CircuitBreaker(
                host=host,
                failure_threshold=failure_threshold,
                cool_down_seconds=cool_down_seconds,
                success_threshold=success_threshold,
                on_state_change=_default_state_change_handler,
            )
            _BREAKERS[host] = cb
        return cb


def all_breakers() -> dict:
    """For /api/circuit_breakers endpoint."""
    with _BREAKERS_LOCK:
        return {host: cb.metrics() for host, cb in _BREAKERS.items()}


def reset_all():
    """For tests."""
    with _BREAKERS_LOCK:
        _BREAKERS.clear()
