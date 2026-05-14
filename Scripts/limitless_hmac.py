"""Limitless Exchange HMAC-SHA256 request signing (Python).

Phase TS-5f.2 (14.05.2026) — Python-side mirror of
executor-ts/src/lib/limitless_hmac.ts. Discovered via live test
against operator's first API token: Limitless V2 requires HMAC-signed
requests, NOT bearer X-API-Key as our older Python code assumed.

Verified working live on 14.05.2026 against `/portfolio/positions`
endpoint (200 OK with empty position list).

See .claude/skills/limitless-hmac-auth/SKILL.md for the contract.

Canonical message (newline-separated):

    <ISO-8601 timestamp>\\n<METHOD>\\n<path?query>\\n<body>

Signature: base64(HMAC-SHA256(base64.decode(secret), message))
"""
import base64
import hashlib
import hmac
from datetime import datetime, timezone
from typing import Dict, Optional
from urllib.parse import urlparse


def sign_lmts_request(token_id: str, secret: str, method: str,
                       path: str, body: str = '') -> Dict[str, str]:
    """Build the 3 HMAC-authenticated headers Limitless V2 expects.

    Parameters
    ----------
    token_id
        The `apiKey` value returned by /auth/api-keys (despite the
        legacy name it's the public identifier, sent as `lmts-api-key`).
    secret
        Base64-encoded HMAC key returned alongside the token at
        creation. The raw key bytes = `base64.b64decode(secret)`.
    method
        HTTP method, uppercase ('GET', 'POST', 'DELETE', ...).
    path
        Absolute path INCLUDING query string. E.g. '/orders?market=x'.
    body
        Exact JSON string to be sent in request body. Empty string ('')
        for GET / DELETE without body. Server signs over the bytes
        we send on the wire — reformatting between sign and send breaks
        the signature.

    Returns
    -------
    Dict with keys `lmts-api-key`, `lmts-timestamp`, `lmts-signature`.

    Notes
    -----
    Server requires timestamp within ±30s of server time. If the host
    clock drifts (no NTP), every request 401s with no useful error.
    """
    ts = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
    # Normalize to '...Z' form (TS new Date().toISOString() output).
    # Python's isoformat may emit '+00:00' instead of 'Z'.
    if ts.endswith('+00:00'):
        ts = ts[:-6] + 'Z'
    elif not ts.endswith('Z'):
        # Fallback if a non-UTC timezone leaks through somehow.
        ts = ts + 'Z'

    msg = f'{ts}\n{method.upper()}\n{path}\n{body}'
    sig = base64.b64encode(
        hmac.new(base64.b64decode(secret), msg.encode('utf-8'),
                 hashlib.sha256).digest()
    ).decode('ascii')

    return {
        'lmts-api-key': token_id,
        'lmts-timestamp': ts,
        'lmts-signature': sig,
    }


def path_for_signing(url: str) -> str:
    """Extract path+query from a full URL — what the HMAC message
    needs in the third newline-separated component.

    Example:
        >>> path_for_signing('https://api.limitless.exchange/orders?m=btc')
        '/orders?m=btc'
    """
    u = urlparse(url)
    if u.query:
        return f'{u.path}?{u.query}'
    return u.path


def lmts_headers_or_legacy(api_key: str,
                            api_secret: Optional[str],
                            method: str, url: str,
                            body: str = '') -> Dict[str, str]:
    """Convenience adapter for callers mid-migration.

    - If `api_secret` is set → returns the 3 HMAC headers (new auth).
    - If only `api_key` is set → returns legacy `X-API-Key` header.
      This 401s on the current Limitless API for Trading-scope tokens
      but is preserved for code paths that haven't migrated yet.
    - If neither → returns empty dict (caller hits public endpoint).
    """
    if api_secret:
        return sign_lmts_request(api_key, api_secret, method,
                                  path_for_signing(url), body)
    if api_key:
        return {'X-API-Key': api_key}
    return {}
