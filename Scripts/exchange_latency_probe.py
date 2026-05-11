"""Phase audit-2 (11.05.2026) — exchange latency shadow probe.

Operator's pain: pipeline_timings shows Python→TS HTTP roundtrip
(~27ms) but that doesn't reflect REAL-mode "detect → fill" latency
because TS executor in dry-run skips the actual POST to Polymarket /
Limitless / SX. The real pipeline includes:

  detect →
    dispatch (27ms, measured) →
    TS build + sign (~5-15ms, TS-side, would log) →
    REAL POST to exchange API (NOT measured in dry-run) →
    exchange match + return order_id (NOT measured) →
    WS fill event (NOT measured) →
    all legs filled

This module measures the THIRD slice (REAL POST round-trip to each
exchange) via cheap no-auth GET endpoints. The measured RTT is a
lower-bound estimate for real-mode POST latency — actual POST adds
~100-300ms server-side processing on top.

NOT a perfect substitute for real-mode measurement (which needs
DRY_RUN=0 + funded wallets + L2 creds). But gives operators an
order-of-magnitude estimate without requiring deposits.

Design:
  - Background thread runs every PROBE_INTERVAL_S (default 60s)
  - For each ENABLED platform, time a single GET to a read-only
    endpoint using a SHARED requests.Session (reflects warm
    connection-pool latency, NOT cold TLS handshake)
  - Ring buffer per platform, maxlen=50
  - `/api/exchange_rtt` returns per-platform percentiles

Caveats (kept explicit so operators don't over-interpret):
  - GET is always faster than POST (no body, no matching engine)
  - Add ~100-300ms to GET RTT for realistic POST estimate
  - Limitless / SX read endpoints may use different infra than
    /orders POST endpoints; production may differ
  - Network conditions vary; this is the radar's view, not bot's
    pre-deposit view (different IP)
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from typing import Optional

import requests

log = logging.getLogger(__name__)

PROBE_INTERVAL_S = float(os.environ.get('EXCHANGE_RTT_PROBE_INTERVAL_S', '60'))
PROBE_TIMEOUT_S = float(os.environ.get('EXCHANGE_RTT_PROBE_TIMEOUT_S', '5'))
RING_BUFFER_MAXLEN = 50

# Per-platform GET probe URLs. All no-auth, lightweight responses.
PROBE_URLS = {
    'polymarket': 'https://gamma-api.polymarket.com/events?limit=1&active=true',
    'limitless': 'https://api.limitless.exchange/markets?limit=1',
    'sx_bet':    'https://api.sx.bet/leagues?testnet=false',
}

# Shared session — same connection pool the radar uses for real
# fetches. Reflects warm-pool latency the way real-mode POST would.
_session = requests.Session()
_session.headers.update({'User-Agent': 'plan-kapkan-rtt-probe/1.0'})

# Ring buffer + lock per platform (modest contention, fine to share lock)
_rtt_lock = threading.Lock()
_rtt_buffers: dict = {
    plat: deque(maxlen=RING_BUFFER_MAXLEN) for plat in PROBE_URLS
}

# Thread state — module-private, used by start/stop
_probe_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


def _record(platform: str, elapsed_ms: float, ok: bool, status_code: Optional[int]) -> None:
    """Append a single probe result. Safe across threads."""
    row = {
        'elapsed_ms': round(elapsed_ms, 1),
        'ok': bool(ok),
        'status_code': status_code,
        'ts': time.time(),
    }
    with _rtt_lock:
        _rtt_buffers[platform].append(row)


def _probe_once(platform: str, url: str) -> None:
    """Send one GET, record latency. Never raises."""
    t0 = time.time()
    try:
        r = _session.get(url, timeout=PROBE_TIMEOUT_S)
        elapsed_ms = (time.time() - t0) * 1000
        # Treat 5xx as not-ok (exchange degraded); 2xx/4xx both count as
        # "connection completed" — 4xx still reflects real network +
        # server-side request parsing time, which is what POST would
        # experience.
        ok = r.status_code < 500
        _record(platform, elapsed_ms, ok, r.status_code)
    except requests.RequestException as e:
        elapsed_ms = (time.time() - t0) * 1000
        log.debug("RTT probe %s failed: %s", platform, e)
        _record(platform, elapsed_ms, False, None)
    except Exception as e:
        elapsed_ms = (time.time() - t0) * 1000
        log.warning("RTT probe %s unexpected: %s: %s",
                     platform, type(e).__name__, e)
        _record(platform, elapsed_ms, False, None)


def _probe_loop() -> None:
    """Background loop — probes each platform every PROBE_INTERVAL_S
    until _stop_event is set."""
    log.info("exchange_latency_probe: starting (interval=%.0fs)",
              PROBE_INTERVAL_S)
    while not _stop_event.is_set():
        for platform, url in PROBE_URLS.items():
            if _stop_event.is_set():
                break
            _probe_once(platform, url)
        # Sleep with stop check — supports clean shutdown
        _stop_event.wait(PROBE_INTERVAL_S)
    log.info("exchange_latency_probe: stopped")


def start() -> None:
    """Spawn the probe thread (daemon). Idempotent — safe to call
    multiple times; subsequent calls no-op when a thread is alive.
    Called once from arb_server startup."""
    global _probe_thread
    if _probe_thread is not None and _probe_thread.is_alive():
        return
    _stop_event.clear()
    _probe_thread = threading.Thread(
        target=_probe_loop, daemon=True, name='exchange-rtt-probe',
    )
    _probe_thread.start()


def stop() -> None:
    """Signal the probe to stop. Used by tests."""
    _stop_event.set()


def _percentile(sorted_values, pct: float) -> Optional[float]:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = max(0, min(len(sorted_values) - 1,
                    int(round((pct / 100.0) * (len(sorted_values) - 1)))))
    return sorted_values[k]


def stats() -> dict:
    """Snapshot of probe data per platform — used by /api/exchange_rtt.

    Returns:
        {
          'polymarket': {count, p50, p90, p99, mean, min, max, last,
                          ok_rate_pct, errors_last_10},
          'limitless':  {...},
          'sx_bet':     {...},
          'note':       string with caveat,
        }
    """
    out: dict = {}
    with _rtt_lock:
        for plat, buf in _rtt_buffers.items():
            rows = list(buf)
            if not rows:
                out[plat] = {'count': 0, 'p50': None, 'p90': None,
                              'p99': None, 'mean': None, 'min': None,
                              'max': None, 'last': None,
                              'ok_rate_pct': None, 'errors_last_10': None}
                continue
            vals = [r['elapsed_ms'] for r in rows]
            ok = [r['ok'] for r in rows]
            sv = sorted(vals)
            last10 = rows[-10:]
            errs_10 = sum(1 for r in last10 if not r['ok'])
            out[plat] = {
                'count': len(rows),
                'p50': _percentile(sv, 50),
                'p90': _percentile(sv, 90),
                'p99': _percentile(sv, 99),
                'mean': round(sum(vals) / len(vals), 1),
                'min': sv[0],
                'max': sv[-1],
                'last': vals[-1],
                'ok_rate_pct': round(100 * sum(ok) / len(ok), 1),
                'errors_last_10': errs_10,
            }
    out['note'] = (
        'GET RTT is a lower bound for real-mode POST latency. Add '
        '~100-300ms for server-side processing (matching engine, '
        'order validation). Real POST also includes signature payload '
        'serialization (~5-15ms). The dominant latency in real-mode '
        'remains exchange-side matching, not network — use these '
        'numbers as a floor, not a precise estimate.'
    )
    out['probe_interval_s'] = PROBE_INTERVAL_S
    out['ring_buffer_maxlen'] = RING_BUFFER_MAXLEN
    return out
