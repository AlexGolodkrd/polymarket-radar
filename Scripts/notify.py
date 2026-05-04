"""Telegram notifications for the radar/executor (Phase 8 add-on, 28.04.2026).

Single entry point: `notify.send(text, level='info')`. Reads
TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID from env at module load.

Design choices:
    - **Non-blocking**: send runs in a daemon thread so the hot path (fire,
      risk check, reconcile) never blocks on Telegram's API.
    - **Graceful degrade**: if env vars missing, send() is a no-op. Local
      dev keeps working without setup; production VPS gets alerts only
      when token is configured.
    - **Rate-limited**: dedupe messages with the same key within a window
      (default 60s) so an alert storm doesn't flood the chat. E.g. if
      reconcile fires every second during an outage we don't want 60
      identical messages — one is enough until it changes.
    - **Single dependency**: uses urllib (stdlib) so we don't need
      python-telegram-bot. Keeps requirements.txt small.

Levels just choose an emoji prefix — no semantic meaning otherwise:
    info     ℹ️
    warn     ⚠️
    crit     🚨
    success  ✅
"""
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

log = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '').strip()
TELEGRAM_API_TIMEOUT_S = 5.0
DEDUPE_WINDOW_S = 60.0    # don't repeat the same key within this window

_PREFIXES = {
    'info': 'ℹ️',
    'warn': '⚠️',
    'crit': '🚨',
    'success': '✅',
}

# Rate-limit cache: dedupe_key -> last_sent_unix
_last_sent_lock = threading.Lock()
_last_sent: dict = {}


def is_configured() -> bool:
    """True iff both env vars are set. Other modules can use this to
    decide whether a notification path is meaningful (vs falling through
    to log-only)."""
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


# Phase 19v15 (05.05.2026) — Telegram 429 rate-limit cooldown.
# Telegram caps Bot API at ~30 msg/sec globally. During alert storms
# (kill switch + reconcile fail + arb burst) we'd hit 429 with
# `Retry-After: N` and the old code silently dropped the message.
# Track a global cooldown so subsequent sends within the window are
# deferred to a single retry rather than each spawning their own
# failed thread.
_TELEGRAM_COOLDOWN_LOCK = threading.Lock()
_TELEGRAM_COOLDOWN_UNTIL: float = 0.0
TELEGRAM_MAX_RETRIES = 3
TELEGRAM_BACKOFF_BASE_S = 1.0


def _post_blocking(text: str, _attempt: int = 0) -> Optional[dict]:
    """Synchronous send via Bot API. Returns parsed response dict or None
    on error. Used internally by the daemon thread; callers should use
    send() instead.

    Phase 19v15 — 429 backoff + global cooldown. On HTTP 429 we read
    `Retry-After` (or fall back to exponential backoff) and try again
    up to TELEGRAM_MAX_RETRIES; subsequent calls during the cooldown
    window are skipped (so kill-switch alerts don't pile up worker
    threads while the rate-limit clears).
    """
    global _TELEGRAM_COOLDOWN_UNTIL
    if not is_configured():
        return None
    # Skip if a previous send hit 429 and we're still cooling down
    with _TELEGRAM_COOLDOWN_LOCK:
        cd = _TELEGRAM_COOLDOWN_UNTIL
    if cd and time.time() < cd:
        log.info("telegram send skipped — cooldown %.1fs remaining",
                 cd - time.time())
        return None
    try:
        data = urllib.parse.urlencode({
            'chat_id': TELEGRAM_CHAT_ID,
            'text': text,
            'parse_mode': 'Markdown',
            'disable_web_page_preview': 'true',
        }).encode('utf-8')
        url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'
        req = urllib.request.Request(url, data=data,
                                     headers={'User-Agent': 'plan-kapkan/1.0'})
        with urllib.request.urlopen(req, timeout=TELEGRAM_API_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        if e.code == 429 and _attempt < TELEGRAM_MAX_RETRIES:
            # Try to read Retry-After (Telegram sets this), fall back
            # to exponential backoff
            try:
                retry_after = float(e.headers.get('Retry-After', 0))
            except (TypeError, ValueError):
                retry_after = 0.0
            wait_s = max(retry_after,
                         TELEGRAM_BACKOFF_BASE_S * (2 ** _attempt))
            with _TELEGRAM_COOLDOWN_LOCK:
                _TELEGRAM_COOLDOWN_UNTIL = time.time() + wait_s
            log.warning("telegram 429 — sleeping %.1fs before retry "
                        "(attempt %d/%d)", wait_s, _attempt + 1,
                        TELEGRAM_MAX_RETRIES)
            time.sleep(wait_s)
            return _post_blocking(text, _attempt=_attempt + 1)
        log.warning("telegram send failed: HTTP %d: %s", e.code, e)
        return None
    except Exception as e:
        log.warning("telegram send failed: %s: %s", type(e).__name__, e)
        return None


def send(text: str, level: str = 'info', dedupe_key: Optional[str] = None) -> bool:
    """Fire-and-forget notification. Returns True if a send was scheduled.

    `dedupe_key`: if provided, suppresses repeats of the same key within
    DEDUPE_WINDOW_S. Use a stable identifier per event class
    (e.g. 'kill_switch', 'reconcile_mismatch') so an outage that fires
    the same handler 60 times only sends ONE alert.

    `text` is sent as Markdown — backticks render as code, *bold*, etc.
    """
    if not is_configured():
        return False

    # Dedupe check
    if dedupe_key:
        now = time.time()
        with _last_sent_lock:
            last = _last_sent.get(dedupe_key, 0)
            if (now - last) < DEDUPE_WINDOW_S:
                return False
            _last_sent[dedupe_key] = now

    prefix = _PREFIXES.get(level, '')
    msg = f"{prefix} {text}".strip()

    # Spawn daemon thread — don't block the caller (hot paths should never
    # wait on a network call to Telegram)
    threading.Thread(
        target=_post_blocking, args=(msg,),
        daemon=True, name=f'notify-{level}',
    ).start()
    return True


def send_alert(level: str = 'info', key: Optional[str] = None,
               msg: str = '') -> bool:
    """Phase 9kkk — keyword-style API used by circuit_breaker.py and
    other modules. Forwards to send().
    """
    return send(msg, level=level, dedupe_key=key)


# ── Phase 9kkk: high-value arb alert ──────────────────────────────

ARB_ALERT_MIN_NET_USD = float(os.environ.get('ARB_ALERT_MIN_NET_USD', '10'))
ARB_ALERT_DEDUPE_WINDOW_S = float(os.environ.get('ARB_ALERT_DEDUPE_WINDOW_S',
                                                  '300'))  # 5min same arb
_arb_alerts_lock = threading.Lock()
_arb_last_sent: dict = {}


# ── Phase 10 Task E (01.05.2026): low-balance bot alerts ──────────
# Per-bot dedupe so a chronically-low bot doesn't spam Telegram every
# scan — same key=bot_id, dedupe_window 1h.
LOW_BALANCE_THRESHOLD_USD = float(os.environ.get('LOW_BALANCE_THRESHOLD_USD', '30'))
LOW_BALANCE_DEDUPE_WINDOW_S = float(os.environ.get('LOW_BALANCE_DEDUPE_WINDOW_S',
                                                     '3600'))    # 1h
_low_bal_last_sent: dict = {}
_low_bal_lock = threading.Lock()


def alert_low_balance(bot_id: str, eth_address: str, balance_usd: float,
                       threshold: Optional[float] = None) -> bool:
    """Phase 10 Task E: alert when a bot's pUSD balance falls below threshold.
    The coordinator skips wallets with insufficient balance silently — without
    this alert, operators wouldn't know a bot is starving until reconcile
    catches missing fills.

    Returns True if Telegram message sent (False on dedupe / unconfigured).
    """
    if not is_configured():
        return False
    thr = threshold if threshold is not None else LOW_BALANCE_THRESHOLD_USD
    if balance_usd >= thr:
        return False
    now = time.time()
    with _low_bal_lock:
        last = _low_bal_last_sent.get(bot_id, 0)
        if now - last < LOW_BALANCE_DEDUPE_WINDOW_S:
            return False
        _low_bal_last_sent[bot_id] = now
    msg = (f"⚠ LOW BALANCE: {bot_id} ({eth_address[:10]}…) "
           f"pUSD ${balance_usd:.2f} < ${thr:.2f}\n"
           f"Coordinator will skip this bot until refunded. "
           f"Top up via Bybit/OKX → Polygon → wrap to pUSD.")
    return send(msg, level='warning', dedupe_key=f'low_bal_{bot_id}')


def alert_high_value_arb(deal: dict) -> bool:
    """Send Telegram alert for arbs with `net` >= ARB_ALERT_MIN_NET_USD ($10).
    Per-arb dedupe by key (platform::title::structure) for ARB_ALERT_DEDUPE_WINDOW_S
    so an arb visible across 5 scans doesn't fire 5 alerts.

    Idempotent. Safe to call from scan loop / WS callback.
    """
    if not is_configured():
        return False
    try:
        net = float(deal.get('net') or 0)
    except (TypeError, ValueError):
        return False
    if net < ARB_ALERT_MIN_NET_USD:
        return False
    key = (
        f"{deal.get('platform', '?')}::"
        f"{(deal.get('title') or '?')[:80]}::"
        f"{deal.get('arb_structure', '?')}"
    )
    now = time.time()
    with _arb_alerts_lock:
        last = _arb_last_sent.get(key, 0)
        if (now - last) < ARB_ALERT_DEDUPE_WINDOW_S:
            return False
        _arb_last_sent[key] = now
    # Build alert message
    plat = deal.get('platform', '?')
    title = deal.get('title') or '?'
    struct = {'all_yes': 'A·ALL_YES', 'all_no': 'B·ALL_NO',
              'yes_no_pair': 'C·YES+NO', 'binary': '◑binary'
              }.get(deal.get('arb_structure', '?'), deal.get('arb_structure', '?'))
    sum_cents = deal.get('sum_cents', '?')
    grade = deal.get('grade', '?')
    min_liq = deal.get('min_liq', 0)
    text = (
        f"🎯 *Arb >${ARB_ALERT_MIN_NET_USD:.0f}*\n"
        f"{plat} · {struct}\n"
        f"`{title[:90]}`\n"
        f"sum={sum_cents}¢ · net=${net:.2f} · grade={grade} · liq=${min_liq:.0f}"
    )
    return send(text, level='success', dedupe_key=f'arb_high_{key}')


def reset_for_test():
    """Test helper — clear dedupe cache so each test starts fresh."""
    with _last_sent_lock:
        _last_sent.clear()
    with _arb_alerts_lock:
        _arb_last_sent.clear()
