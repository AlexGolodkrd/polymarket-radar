"""Network/geo safety check — refuses to fire trades from disallowed regions.

This is the **third layer** of VPN/network safety (Phase 8 add-on, 28.04.2026):
    Layer 1 — System firewall (iptables / Mullvad lockdown) — see deploy/README
    Layer 2 — systemd dependency on VPN service — see deploy/README
    Layer 3 — THIS — application-level IP/country verification before each fire

Even if Layers 1+2 are misconfigured or fail in some edge case, Layer 3 catches
the bot trying to talk to Polymarket from a disallowed IP. It calls a public
geo-IP service to discover its own outbound IP and country, then matches
against ALLOWED_COUNTRIES.

Configuration (env vars, set on VPS):
    ALLOWED_COUNTRIES=GE,AM,TR,KZ,AE,CH   # comma-separated ISO-2 codes
    NETWORK_CHECK_INTERVAL_S=60           # cache the result this long
    NETWORK_CHECK_TIMEOUT_S=5             # per-provider HTTP timeout

When ALLOWED_COUNTRIES is empty (default), the check is **disabled** — useful
for local dev where you'd be on a domestic IP. On VPS, set it explicitly to
the country you registered Polymarket from (e.g. GE).

Fail-safe: if all geo providers fail (network down, providers blocked), the
function returns False — the bot will **block** new fires until network is back.
This trades availability for safety, which is correct for an arb bot whose
expected loss from a leak (account ban / fund freeze) is much larger than
expected loss from a few missed scans.
"""
import logging
import os
import threading
import time
from typing import Optional, Tuple

log = logging.getLogger(__name__)

# Config from env (read at module load — set them in Credentials.env on VPS)
_ALLOWED_RAW = os.environ.get('ALLOWED_COUNTRIES', '').strip()
ALLOWED_COUNTRIES = {c.strip().upper() for c in _ALLOWED_RAW.split(',') if c.strip()}
CHECK_INTERVAL_S = float(os.environ.get('NETWORK_CHECK_INTERVAL_S', '60'))
CHECK_TIMEOUT_S = float(os.environ.get('NETWORK_CHECK_TIMEOUT_S', '5'))


# ── Cached state ────────────────────────────────────────────────────
_cache_lock = threading.Lock()
_cache = {
    'ip': None,           # last known outbound IP
    'country': None,      # ISO-2 country code (uppercase)
    'fetched_at': 0.0,    # unix ts
    'last_error': None,   # str describing last failure (for diagnostics)
}


# ── Providers — 2 redundant sources to survive single-provider outage ──
def _from_ifconfig_co():
    """ifconfig.co/json — returns IP + country_iso. Free, no auth, ~50ms."""
    import requests
    r = requests.get('https://ifconfig.co/json', timeout=CHECK_TIMEOUT_S,
                     headers={'Accept': 'application/json'})
    j = r.json()
    return j.get('ip'), (j.get('country_iso') or '').upper() or None


def _from_country_is():
    """api.country.is — minimalist, returns IP + country (ISO-2)."""
    import requests
    r = requests.get('https://api.country.is/', timeout=CHECK_TIMEOUT_S)
    j = r.json()
    return j.get('ip'), (j.get('country') or '').upper() or None


_PROVIDERS = [_from_ifconfig_co, _from_country_is]


def _fetch_now() -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Try each provider in order. Returns (ip, country, error)."""
    last_err = None
    for provider in _PROVIDERS:
        try:
            ip, country = provider()
            if ip and country and len(country) == 2:
                return ip, country, None
            last_err = f'{provider.__name__}: bad response ip={ip!r} country={country!r}'
        except Exception as e:
            last_err = f'{provider.__name__}: {type(e).__name__}: {e}'
    return None, None, last_err or 'all providers failed'


# ── Public API ──────────────────────────────────────────────────────
def get_current_ip_country(force_refresh: bool = False) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Returns (ip, country_iso, error). Cached for CHECK_INTERVAL_S so the
    fire hot path doesn't hit the network on every call. `force_refresh=True`
    bypasses cache (used by /api/network_status endpoint)."""
    now = time.time()
    with _cache_lock:
        if not force_refresh and (now - _cache['fetched_at']) < CHECK_INTERVAL_S:
            return _cache['ip'], _cache['country'], _cache['last_error']
    ip, country, err = _fetch_now()
    with _cache_lock:
        _cache['ip'] = ip
        _cache['country'] = country
        _cache['fetched_at'] = now
        _cache['last_error'] = err
    if err:
        log.warning("network_check fetch failed: %s", err)
    elif country not in ALLOWED_COUNTRIES and ALLOWED_COUNTRIES:
        log.warning("network_check: current country %s NOT in ALLOWED_COUNTRIES %s",
                    country, sorted(ALLOWED_COUNTRIES))
    return ip, country, err


def check_country_allowed() -> Tuple[bool, Optional[str]]:
    """Returns (allowed, reason). Used by risk.check_can_fire as a hard gate.

    Behaviour:
        - ALLOWED_COUNTRIES is empty (default): check is DISABLED, returns (True, None).
        - Cached result < CHECK_INTERVAL_S old: use it without re-fetching.
        - Fresh fetch fails (network down, providers blocked): block — fail-safe.
        - Country not in ALLOWED_COUNTRIES: block.
    """
    if not ALLOWED_COUNTRIES:
        return True, None  # disabled
    ip, country, err = get_current_ip_country()
    if err:
        return False, f'network_check_failed: {err[:80]}'
    if country not in ALLOWED_COUNTRIES:
        return False, (f'ip_geo_disallowed: country={country!r} ip={ip!r} '
                       f'(allowed: {",".join(sorted(ALLOWED_COUNTRIES))})')
    return True, None


def status() -> dict:
    """Diagnostics endpoint payload. Returns current cached state plus config."""
    with _cache_lock:
        c = dict(_cache)
    age_s = time.time() - c['fetched_at'] if c['fetched_at'] else None
    return {
        'enabled': bool(ALLOWED_COUNTRIES),
        'allowed_countries': sorted(ALLOWED_COUNTRIES),
        'current_ip': c['ip'],
        'current_country': c['country'],
        'cache_age_seconds': round(age_s, 1) if age_s is not None else None,
        'cache_interval_seconds': CHECK_INTERVAL_S,
        'last_error': c['last_error'],
    }


def reset_for_test():
    """Test helper — wipe cache so next call re-fetches."""
    with _cache_lock:
        _cache.update({'ip': None, 'country': None, 'fetched_at': 0.0, 'last_error': None})
