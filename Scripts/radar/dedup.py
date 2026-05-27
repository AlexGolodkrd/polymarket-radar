"""Fire-deduplication TTL store.

Extracted from `Scripts/arb_server.py` in audit-28a (27.05.2026). The
module owns the global `_fired_arb_keys` dict (key → last_fire_ts) that
prevents the radar from re-firing the same arb across consecutive scan
ticks.

History:
    Phase 9i (28.04.2026)   — initial set, two-phase commit fixed
                              lock-held-across-fire TOCTOU race.
    Phase 9uu (29.04.2026)  — added "evict when deal leaves active list"
                              + hard cap + hard-cap-clears-all safety net.
    Phase audit-27.05       — REPLACED active-list eviction with TTL.
                              Active-list eviction caused 18-fires-in-1h
                              re-detection loop (operator screenshot).
                              Hard cap now drops oldest 20%, not all.

Public API (new):
    fire_dedup: FireDedup   — module-level singleton
    fire_dedup.key(deal)    — stable (structure, platform, title) key
    fire_dedup.reserve(deals) → list[(key, deal)]  — claim, return new
    fire_dedup.is_tracked(key) → bool
    fire_dedup.evict_expired() → int  — count of dropped keys
    fire_dedup.clear()              — for tests / hard reset

Legacy aliases (for backward compat with existing tests and arb_server
internals):
    _fired_arb_keys, _fired_arb_keys_lock, _FIRED_KEYS_HARD_CAP,
    FIRE_COOLDOWN_S, _arb_fire_key.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Optional

# ── Configuration ────────────────────────────────────────
# Read via central config (with env fallback for legacy boot paths).
try:
    from config import config as _radar_config
    _DEFAULT_COOLDOWN = _radar_config.fire_cooldown_s
except Exception:
    import os
    _DEFAULT_COOLDOWN = int(os.environ.get('FIRE_COOLDOWN_S', '1800'))


def _arb_fire_key(deal: dict[str, Any]) -> str:
    """Stable key for fire-dedup.

    Phase audit-27.05: kept verbatim from arb_server.py for binary
    compatibility — the same key produced by legacy callers must match.
    """
    return f"{deal.get('arb_structure', '?')}::{deal.get('platform', '?')}::{deal.get('title', '?')}"


class FireDedup:
    """TTL-based fire deduplication store.

    Thread-safe. Internally uses one Lock; readers (`is_tracked`) take
    it briefly. Writers (`reserve`, `evict_expired`, `clear`) hold it
    only for dict mutation, not for any side effects.

    A key stays "tracked" for `cooldown_s` seconds after the last
    `reserve()` call. Within that window the same key cannot be
    reserved again — caller skips firing.

    Hard cap (`hard_cap`, default 5000): when the dict exceeds it, the
    oldest 20% (by last_fire_ts) are dropped. This is a safety net for
    the case where TTL eviction has a bug; it does NOT happen in
    healthy operation because TTL keeps the dict bounded.
    """

    def __init__(self,
                 cooldown_s: Optional[int] = None,
                 hard_cap: int = 5000) -> None:
        self.cooldown_s: int = cooldown_s if cooldown_s is not None else _DEFAULT_COOLDOWN
        self.hard_cap: int = hard_cap
        self._lock: threading.Lock = threading.Lock()
        self._tracked: dict[str, float] = {}     # key -> last_fire_ts

    # ── Public API ───────────────────────────────────────

    @staticmethod
    def key(deal: dict[str, Any]) -> str:
        """Compute the stable fire-key for a deal."""
        return _arb_fire_key(deal)

    def reserve(self, deals: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
        """Claim fire slots atomically for every new (un-tracked) deal.

        Returns the list of `(key, deal)` tuples the caller should fire.
        Quarantined deals (`deal.is_quarantine == True`) are excluded.

        Side effects under lock:
          1. Drop expired keys (TTL).
          2. Drop oldest 20% if hard cap exceeded.
          3. For each non-quarantined deal not currently tracked:
             insert into `_tracked` with now-ts and append to result.
        """
        now = time.time()
        to_fire: list[tuple[str, dict[str, Any]]] = []
        with self._lock:
            # TTL eviction
            if self._tracked:
                expired = [k for k, last_ts in self._tracked.items()
                           if now - last_ts > self.cooldown_s]
                for k in expired:
                    self._tracked.pop(k, None)
            # Hard-cap safety net (oldest 20% gone)
            if len(self._tracked) > self.hard_cap:
                sorted_keys = sorted(self._tracked.items(), key=lambda kv: kv[1])
                drop_count = len(self._tracked) - int(self.hard_cap * 0.8)
                for k, _ in sorted_keys[:drop_count]:
                    self._tracked.pop(k, None)
                print(f"[DRYFIRE] FireDedup exceeded hard cap "
                      f"{self.hard_cap} — dropped {drop_count} oldest.",
                      flush=True)
            # Reserve new keys
            for d in deals:
                if d.get('is_quarantine'):
                    continue
                k = _arb_fire_key(d)
                if k in self._tracked:
                    continue
                self._tracked[k] = now
                to_fire.append((k, d))
        return to_fire

    def is_tracked(self, key: str) -> bool:
        """True iff `key` is within the cooldown window."""
        with self._lock:
            return key in self._tracked

    def evict_expired(self) -> int:
        """Drop expired keys; return count removed. Useful for tests
        and for forensic inspection."""
        now = time.time()
        with self._lock:
            expired = [k for k, last_ts in self._tracked.items()
                       if now - last_ts > self.cooldown_s]
            for k in expired:
                self._tracked.pop(k, None)
        return len(expired)

    def clear(self) -> None:
        """Reset to empty. Called by tests' conftest reset fixture."""
        with self._lock:
            self._tracked.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._tracked)

    # ── Backward-compat shims ────────────────────────────
    # These properties let legacy code that touched the raw dict +
    # lock keep working. New code should NOT use these.

    @property
    def _raw_dict(self) -> dict[str, float]:
        """Direct access to the underlying dict. Do NOT mutate without
        holding `self._lock`. Provided only for migration shims in
        arb_server.py."""
        return self._tracked

    @property
    def _raw_lock(self) -> threading.Lock:
        """Direct access to the internal lock. Same caveat as above."""
        return self._lock


# Module-level singleton — every caller in the radar uses this one
# instance. Tests can call `fire_dedup.clear()` to reset (handled by
# conftest.py autouse fixture).
fire_dedup: FireDedup = FireDedup()
