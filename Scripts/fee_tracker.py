"""Fee tracker — realtime EMA of effective fee bps per platform.

Phase audit-3 (15.05.2026) — replaces hardcoded threshold assumptions
with a per-platform exponential moving average of the fee actually
deducted at fill time.

Why this matters
----------------
Limitless documented `feeRateBps=300` (3% Bronze rank) but the LIVE
response from a real fill on 2026-05-15 carried
`execution.effectiveFeeBps: 0` — a promo for new accounts that the API
doesn't advertise. We've been using THRESH_LIMITLESS=0.99 which assumes
0% fee; that worked by accident. The moment Limitless turns off the
promo silently, threshold becomes too generous and we ship fake arbs.

This tracker reads `effective_fee_bps` out of each `fire_filled` event,
exponentially-smooths it per platform, and exposes the smoothed value
so the threshold calculators can adapt automatically.

Lifecycle
---------
- On import: state loads from `Executions/fee_ema.json` if present
  (single JSON file, safe to delete to reset).
- After each fill (`record_fee_observation`): EMA updates atomically.
- Periodic flush to disk every N updates so we don't lose state on
  container restart.
- Caller-side: `get_effective_fee_bps(platform, default_bps)` returns
  the smoothed value if `samples >= MIN_SAMPLES`, else `default_bps`.

Data model (in-memory + on-disk)
--------------------------------
{
  'limitless': {'ema_bps': 0.0, 'samples': 12, 'last_update': 1778...},
  'polymarket': {'ema_bps': 42.5, 'samples': 8, 'last_update': 1778...},
  'sx': {'ema_bps': 200.0, 'samples': 3, 'last_update': 1778...},
}

Per-market granularity is intentionally NOT supported in this first
revision — most platforms charge a single global fee tier per wallet
(Limitless rank, Polymarket V2 feeSchedule per category). If we later
need per-market granularity we extend keys to `(platform, market_id)`.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Optional


# ── Tunables (env-overridable) ─────────────────────────────────────

# EMA smoothing factor. alpha=0.3 means each new sample contributes
# ~30%; ~5 samples to reach 80% influence. Picked to react within a
# trading session to a promo flip (e.g. Limitless turns Bronze fee
# 0%→3%) without overreacting to single-fill outliers.
EMA_ALPHA = float(os.environ.get('FEE_EMA_ALPHA', '0.3'))

# Minimum samples before we trust the EMA enough to override the
# static threshold default. Below this, get_effective_fee_bps()
# returns the supplied default. 3 was chosen so a single anomalous
# fill (e.g. zero-fee promo first trade followed by normal 3%) can't
# bias the threshold significantly.
MIN_SAMPLES = int(os.environ.get('FEE_EMA_MIN_SAMPLES', '3'))

# Flush state to disk every N updates. Small enough that a container
# crash loses at most a few observations; large enough to avoid
# 1-write-per-fill IO overhead.
FLUSH_EVERY = int(os.environ.get('FEE_EMA_FLUSH_EVERY', '5'))

# Persistence file. Lives next to other Executions/ jsonl logs.
_BASE_DIR = os.environ.get('EXECUTIONS_DIR', 'Executions')
STATE_PATH = os.path.join(_BASE_DIR, 'fee_ema.json')


# ── State (process-local, thread-safe) ─────────────────────────────

_state: dict = {}     # {platform: {'ema_bps', 'samples', 'last_update'}}
_lock = threading.Lock()
_unflushed_count = 0
_loaded = False


def _normalize_platform(platform: str) -> str:
    """Canonicalize platform name to a flat lowercase key. Accepts the
    operator-facing strings ('Limitless', 'SX Bet', 'Polymarket',
    composite 'Limitless+SX Bet') and the TS-side strings ('limitless',
    'sx_bet', 'polymarket'). Composite forms are returned as-is so the
    caller can decide whether to split per leg before recording.
    """
    if not platform:
        return ''
    s = platform.strip().lower()
    # Strip whitespace inside multi-word names
    s = s.replace(' ', '_')
    # Common aliases
    aliases = {
        'sx': 'sx_bet',
        'sxbet': 'sx_bet',
        'sx-bet': 'sx_bet',
        'lim': 'limitless',
        'poly': 'polymarket',
    }
    return aliases.get(s, s)


def _load() -> None:
    global _state, _loaded
    with _lock:
        if _loaded:
            return
        if os.path.exists(STATE_PATH):
            try:
                with open(STATE_PATH, 'r', encoding='utf-8') as f:
                    data = json.load(f) or {}
                if isinstance(data, dict):
                    _state.update(data)
            except (OSError, json.JSONDecodeError):
                pass
        _loaded = True


def _flush_locked() -> None:
    """Caller must already hold _lock."""
    try:
        os.makedirs(_BASE_DIR, exist_ok=True)
        tmp_path = STATE_PATH + '.tmp'
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(_state, f, separators=(',', ':'))
        os.replace(tmp_path, STATE_PATH)
    except OSError:
        # Best-effort persistence — don't crash trading loop on a
        # filesystem hiccup; the value stays correct in memory until
        # the next successful flush.
        pass


# ── Public API ─────────────────────────────────────────────────────

def record_fee_observation(platform: str, effective_fee_bps: float) -> None:
    """Update the per-platform EMA with one observation from a real fill.

    `effective_fee_bps` should be the value the exchange actually
    deducted (Limitless: response.execution.effectiveFeeBps;
    Polymarket: response.fee / response.size * 10000; SX: TBD).
    Negative values (maker rebate) are accepted; they'll naturally
    lower the EMA and the threshold becomes more generous.

    A null/None observation is silently ignored — callers don't need
    to guard, just pass whatever the exchange sent.
    """
    if effective_fee_bps is None:
        return
    try:
        v = float(effective_fee_bps)
    except (TypeError, ValueError):
        return
    key = _normalize_platform(platform)
    if not key:
        return
    _load()
    global _unflushed_count
    with _lock:
        entry = _state.get(key) or {'ema_bps': v, 'samples': 0, 'last_update': 0.0}
        prev_ema = float(entry.get('ema_bps', v))
        samples = int(entry.get('samples', 0))
        # Standard EMA: new = alpha*observation + (1-alpha)*prev
        if samples == 0:
            new_ema = v
        else:
            new_ema = EMA_ALPHA * v + (1.0 - EMA_ALPHA) * prev_ema
        _state[key] = {
            'ema_bps': new_ema,
            'samples': samples + 1,
            'last_update': time.time(),
        }
        _unflushed_count += 1
        if _unflushed_count >= FLUSH_EVERY:
            _flush_locked()
            _unflushed_count = 0


def get_effective_fee_bps(platform: str, default_bps: float) -> float:
    """Return the smoothed effective fee in bps, or `default_bps` if we
    haven't accumulated MIN_SAMPLES observations yet for this platform.

    Use this from threshold calculators so they adapt to live conditions
    without overreacting to the first observation post-restart.
    """
    _load()
    key = _normalize_platform(platform)
    with _lock:
        entry = _state.get(key)
    if not entry:
        return default_bps
    if int(entry.get('samples', 0)) < MIN_SAMPLES:
        return default_bps
    try:
        return float(entry.get('ema_bps', default_bps))
    except (TypeError, ValueError):
        return default_bps


def snapshot() -> dict:
    """Return a copy of the in-memory state. Useful for /api/fee_tracker
    dashboards or debugging."""
    _load()
    with _lock:
        return {k: dict(v) for k, v in _state.items()}


def reset_for_tests() -> None:
    """Wipe in-memory state and reset persistence path. ONLY for tests —
    not exposed via HTTP."""
    global _state, _unflushed_count, _loaded
    with _lock:
        _state = {}
        _unflushed_count = 0
        _loaded = True  # mark loaded so next call doesn't re-read disk
