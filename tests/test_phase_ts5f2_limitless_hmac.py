"""Phase TS-5f.2 (14.05.2026) — Python Limitless HMAC signer.

Mirrors executor-ts/tests/lib/limitless_hmac.test.ts. Verified live on
14.05.2026: /portfolio/positions returned 200 OK using these exact
inputs against operator's first real API token.
"""
import base64
import hashlib
import hmac
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))

from limitless_hmac import (  # noqa
    sign_lmts_request,
    path_for_signing,
    lmts_headers_or_legacy,
)

# Known test key — 32 zero bytes base64-encoded.
TEST_TOKEN = 'testTokenIdAbCd1'
TEST_SECRET = base64.b64encode(b'\x00' * 32).decode()


def test_returns_three_required_headers():
    h = sign_lmts_request(TEST_TOKEN, TEST_SECRET, 'GET', '/portfolio')
    assert set(h.keys()) == {'lmts-api-key', 'lmts-timestamp', 'lmts-signature'}
    assert h['lmts-api-key'] == TEST_TOKEN


def test_timestamp_is_iso_with_ms_z_suffix():
    h = sign_lmts_request(TEST_TOKEN, TEST_SECRET, 'GET', '/portfolio')
    assert re.match(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$',
                    h['lmts-timestamp'])


def test_signature_is_base64():
    h = sign_lmts_request(TEST_TOKEN, TEST_SECRET, 'GET', '/portfolio')
    # base64 chars
    assert re.match(r'^[A-Za-z0-9+/]+=*$', h['lmts-signature'])
    # SHA256 → 32 bytes → base64 → 44 chars
    assert len(h['lmts-signature']) == 44


def test_signature_matches_manual_hmac():
    """Signature must equal HMAC-SHA256 over `{ts}\\n{METHOD}\\n{path}\\n{body}`
    with `base64.b64decode(secret)` as the key."""
    h = sign_lmts_request(TEST_TOKEN, TEST_SECRET, 'GET', '/portfolio',
                          body='')
    expected = base64.b64encode(
        hmac.new(base64.b64decode(TEST_SECRET),
                 f"{h['lmts-timestamp']}\nGET\n/portfolio\n".encode(),
                 hashlib.sha256).digest()
    ).decode()
    assert h['lmts-signature'] == expected


def test_method_lowercase_normalized_to_upper():
    """Passing 'get' should yield same canonical message as 'GET'."""
    h_lo = sign_lmts_request(TEST_TOKEN, TEST_SECRET, 'get', '/x')
    # Manually compute with uppercase GET to confirm
    expected_msg = f"{h_lo['lmts-timestamp']}\nGET\n/x\n"
    expected = base64.b64encode(
        hmac.new(base64.b64decode(TEST_SECRET),
                 expected_msg.encode(), hashlib.sha256).digest()
    ).decode()
    assert h_lo['lmts-signature'] == expected


def test_post_with_body():
    body = '{"foo":"bar"}'
    h = sign_lmts_request(TEST_TOKEN, TEST_SECRET, 'POST', '/orders', body)
    expected_msg = f"{h['lmts-timestamp']}\nPOST\n/orders\n{body}"
    expected = base64.b64encode(
        hmac.new(base64.b64decode(TEST_SECRET),
                 expected_msg.encode(), hashlib.sha256).digest()
    ).decode()
    assert h['lmts-signature'] == expected


def test_secret_never_leaks_into_headers():
    """Defense in depth — the secret bytes must not appear in any
    header value (signature is HMAC output, not the key itself)."""
    h = sign_lmts_request(TEST_TOKEN, TEST_SECRET, 'GET', '/x')
    for v in h.values():
        assert TEST_SECRET not in v


def test_path_for_signing_strips_host():
    assert path_for_signing('https://api.limitless.exchange/orders') == '/orders'


def test_path_for_signing_preserves_query():
    p = path_for_signing('https://api.limitless.exchange/orders?market=btc&limit=10')
    assert p == '/orders?market=btc&limit=10'


def test_path_for_signing_handles_port():
    assert path_for_signing('http://localhost:8080/api/v1/x') == '/api/v1/x'


def test_lmts_headers_or_legacy_with_secret_returns_hmac():
    h = lmts_headers_or_legacy(TEST_TOKEN, TEST_SECRET, 'GET',
                                'https://api.limitless.exchange/portfolio')
    assert 'lmts-api-key' in h
    assert 'lmts-timestamp' in h
    assert 'lmts-signature' in h
    assert 'X-API-Key' not in h


def test_lmts_headers_or_legacy_without_secret_returns_xapikey():
    """Migration adapter: callers without a secret get the legacy header.
    Will 401 against the current API for Trading-scope tokens but
    preserves call sites that haven't been updated."""
    h = lmts_headers_or_legacy(TEST_TOKEN, None, 'GET',
                                'https://api.limitless.exchange/portfolio')
    assert h == {'X-API-Key': TEST_TOKEN}


def test_lmts_headers_or_legacy_no_creds_returns_empty():
    """Public endpoint path — no auth needed."""
    h = lmts_headers_or_legacy('', None, 'GET', 'https://api.limitless.exchange/markets/active')
    assert h == {}
