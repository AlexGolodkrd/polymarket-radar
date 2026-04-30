"""Universal HTTP status code handler — Phase 9kkk (30.04.2026).

Centralizes the policy for "what does this status code mean for the radar".
Used by all 4 platform fetchers: Polymarket, Kalshi, SX Bet, Limitless.

Without this, every fetcher had its own ad-hoc handling: some retried 503,
some didn't, some logged 403, some swallowed it. The result was that a
Limitless 403 outage looked silent while a SX Bet 502 burst spammed logs.

Categories
----------
SUCCESS    (2xx) → on_success(), parse body
NOT_FOUND  (404) → on_success() (semantic — "no such resource"), return None
CLIENT_BAD (400, 401, 422) → log once, do NOT retry (config bug, not transient)
RATE_LIMIT (429, 503) → backoff with Retry-After, retry up to N
SERVER_TRANSIENT (502, 504, 521, 522, 524) → exponential backoff, retry up to N
CIRCUIT_OPEN (403, 520, 525, 526) → trip circuit breaker, do NOT retry
CLIENT_REDIR (3xx) → follow (handled by httpx by default)

The dispatcher returns an Action enum: caller decides whether to retry,
log, or short-circuit based on the verdict.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional


class Action(Enum):
    SUCCESS = "success"            # parse body normally
    NOT_FOUND = "not_found"        # treat as success but body is empty
    SKIP_CLIENT_ERR = "skip_client_err"  # log once, do not retry
    RETRY_BACKOFF = "retry_backoff"  # 429 or 503 — wait Retry-After
    RETRY_TRANSIENT = "retry_transient"  # 5xx transient — exponential
    OPEN_BREAKER = "open_breaker"  # 403 / unusual — circuit breaker open
    UNKNOWN = "unknown"            # haven't seen this — log and treat as transient


def classify(status_code: int) -> Action:
    """Map HTTP status to Action. Pure function."""
    if 200 <= status_code < 300:
        return Action.SUCCESS
    if status_code == 404:
        return Action.NOT_FOUND
    if status_code in (400, 401, 405, 410, 422):
        return Action.SKIP_CLIENT_ERR
    if status_code in (429, 503):
        return Action.RETRY_BACKOFF
    if status_code in (502, 504, 521, 522, 524):
        return Action.RETRY_TRANSIENT
    if status_code in (403, 520, 525, 526):
        return Action.OPEN_BREAKER
    if 300 <= status_code < 400:
        return Action.SUCCESS  # httpx auto-follows; if we see this we're not redirecting
    return Action.UNKNOWN


# Human-readable labels for log messages / dashboard
ACTION_LABEL = {
    Action.SUCCESS: 'OK',
    Action.NOT_FOUND: 'NOT FOUND (resource missing)',
    Action.SKIP_CLIENT_ERR: 'CLIENT ERROR (config issue, not retried)',
    Action.RETRY_BACKOFF: 'RATE LIMITED (backing off)',
    Action.RETRY_TRANSIENT: 'TRANSIENT 5xx (retry)',
    Action.OPEN_BREAKER: 'CIRCUIT OPEN (Cloudflare-style block)',
    Action.UNKNOWN: 'UNKNOWN STATUS',
}


# Status code → human-readable hint (for logs and operator dashboard)
STATUS_HINT = {
    400: 'Bad Request — likely a code-side bug (wrong param)',
    401: 'Unauthorized — missing/invalid auth header',
    403: 'Forbidden — Cloudflare adaptive block or geo-block',
    404: 'Not Found — resource removed or wrong URL',
    405: 'Method Not Allowed',
    410: 'Gone — endpoint deprecated',
    422: 'Unprocessable Entity — request body bad shape',
    429: 'Too Many Requests — rate limit hit, honour Retry-After',
    500: 'Internal Server Error — origin bug',
    502: 'Bad Gateway — Cloudflare↔origin connection failed',
    503: 'Service Unavailable — origin under load, honour Retry-After',
    504: 'Gateway Timeout — origin slow (>100s)',
    520: 'Cloudflare Generic — origin returned weird response',
    521: 'Web Server Down — origin offline',
    522: 'Connection Timed Out — origin not accepting',
    524: 'Origin Timeout — origin took >100s',
    525: 'SSL Handshake Failed — origin TLS broken',
    526: 'Invalid SSL Certificate — origin cert issue',
}


def format_log(host: str, status: int, url: str, attempt: int = 1) -> str:
    """Pretty log line for an HTTP error."""
    action = classify(status)
    hint = STATUS_HINT.get(status, '')
    return (f"[HTTP:{host}] {status} ({ACTION_LABEL[action]}) "
            f"attempt={attempt} url={url[:80]} {('— ' + hint) if hint else ''}")


# Per-action default retry policy (caller may override)
DEFAULT_MAX_RETRIES = {
    Action.RETRY_BACKOFF: 3,        # 429/503 — try up to 3 with backoff
    Action.RETRY_TRANSIENT: 2,      # 5xx transient — try up to 2
    Action.OPEN_BREAKER: 0,         # 403 — open breaker, do not retry
    Action.SKIP_CLIENT_ERR: 0,      # 4xx config — do not retry
    Action.NOT_FOUND: 0,
    Action.SUCCESS: 0,
    Action.UNKNOWN: 1,              # 1 retry, then give up
}


def compute_backoff(action: Action, attempt: int, retry_after: Optional[float] = None) -> float:
    """Seconds to sleep before next attempt.

    For RETRY_BACKOFF: prefer server's Retry-After header, fallback to
        exponential 2^attempt capped at 30s.
    For RETRY_TRANSIENT: exponential 2^attempt capped at 10s (more aggressive).
    For UNKNOWN: 5s flat.
    """
    import random
    if action == Action.RETRY_BACKOFF:
        if retry_after is not None:
            return min(float(retry_after), 30.0) + random.uniform(0, 0.3)
        return min(2 ** attempt, 30.0) + random.uniform(0, 0.5)
    if action == Action.RETRY_TRANSIENT:
        return min(2 ** attempt, 10.0) + random.uniform(0, 0.3)
    if action == Action.UNKNOWN:
        return 5.0
    return 0.0
