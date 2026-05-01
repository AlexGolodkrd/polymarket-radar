"""poly_derive_api_creds.py — one-time L2 API key derivation per bot wallet.

Why this script exists
======================
Polymarket CLOB requires TWO levels of auth:
  L1 = EIP-712 signature with private key  (sufficient for POST /order)
  L2 = HMAC headers POLY_API_KEY/SECRET/PASSPHRASE  (needed for
       DELETE /order/{id}, GET /data/positions, user-channel WS)

L2 creds are derived ONCE per wallet by signing a specific EIP-712 message
with L1, then calling Polymarket's `GET /auth/derive-api-key` endpoint.
Server returns {api_key, secret, passphrase} which we cache permanently
in Credentials.env per bot. After that, no derivation needed.

This is the missing piece flagged in PR #51 audit. Before this script,
DRY_RUN=0 was a hard blocker because cancel/positions/user-WS would all
401 without L2 headers.

Usage
=====
    pip install eth-account requests
    # Per bot (one-time):
    python Scripts/poly_derive_api_creds.py --bot bot1
    python Scripts/poly_derive_api_creds.py --bot bot2
    ... up to bot6

    # Show what would be sent without actually calling the API:
    python Scripts/poly_derive_api_creds.py --bot bot1 --dry-run

Reads:
    Credentials.env  →  BOT{N}_PRIVATE_KEY, BOT{N}_ETH_ADDRESS

Writes (appends if missing):
    Credentials.env  →  BOT{N}_POLY_API_KEY, BOT{N}_POLY_SECRET,
                         BOT{N}_POLY_PASSPHRASE

Verification
============
After running, the wallet's L2 creds let `Scripts/wallets/stores.py`
populate `WalletStub.poly_api_key/secret/passphrase`, which
`build_poly_cancel()` and `build_poly_hmac_headers()` use for cancel/
positions/WS. Re-running the script for an already-derived bot is a
no-op (server returns the same creds it issued before).

References
==========
- https://docs.polymarket.com/developers/CLOB/authentication
- py-clob-client `create_or_derive_api_creds()` (we follow the same
  EIP-712 message format here)
- builders.py — consumes the resulting headers via `build_poly_hmac_headers`
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from typing import Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

CREDS_FILE = os.path.join(REPO_ROOT, 'Credentials.env')
POLY_DERIVE_URL = 'https://clob.polymarket.com/auth/derive-api-key'
POLY_CREATE_URL = 'https://clob.polymarket.com/auth/api-key'
POLY_CHAIN_ID = 137                              # Polygon mainnet

# EIP-712 message format that py-clob-client signs to derive creds. Server
# accepts this fixed shape (the nonce keeps the request unique and the
# timestamp is sub-minute server-side).
CLOB_AUTH_DOMAIN = {
    'name': 'ClobAuthDomain',
    'version': '1',
    'chainId': POLY_CHAIN_ID,
}
CLOB_AUTH_TYPES = {
    'EIP712Domain': [
        {'name': 'name', 'type': 'string'},
        {'name': 'version', 'type': 'string'},
        {'name': 'chainId', 'type': 'uint256'},
    ],
    'ClobAuth': [
        {'name': 'address', 'type': 'address'},
        {'name': 'timestamp', 'type': 'string'},
        {'name': 'nonce', 'type': 'uint256'},
        {'name': 'message', 'type': 'string'},
    ],
}


def _read_env_file(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    out = {}
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.rstrip('\r\n')
            stripped = line.strip()
            if not stripped or stripped.startswith('#') or '=' not in stripped:
                continue
            k, v = stripped.split('=', 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _append_or_replace_lines(path: str, kv_pairs: dict) -> None:
    """Idempotent .env writer: replace existing keys, append new ones.
    Preserves comments and blank lines verbatim. Never logs values.
    """
    if not os.path.exists(path):
        with open(path, 'w', encoding='utf-8') as f:
            for k, v in kv_pairs.items():
                f.write(f'{k}={v}\n')
        return
    with open(path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    keys_to_set = dict(kv_pairs)
    out_lines = []
    for line in lines:
        m = re.match(r'^(\s*)([A-Z_][A-Z0-9_]*)\s*=', line)
        if m:
            key = m.group(2)
            if key in keys_to_set:
                out_lines.append(f'{key}={keys_to_set.pop(key)}\n')
                continue
        out_lines.append(line)
    if keys_to_set:
        if out_lines and not out_lines[-1].endswith('\n'):
            out_lines[-1] = out_lines[-1] + '\n'
        out_lines.append('\n# Polymarket L2 credentials (derived '
                         f'{time.strftime("%Y-%m-%d")})\n')
        for k, v in keys_to_set.items():
            out_lines.append(f'{k}={v}\n')
    with open(path, 'w', encoding='utf-8') as f:
        f.writelines(out_lines)


def _sign_clob_auth(eth_address: str, private_key: str,
                     timestamp: Optional[int] = None,
                     nonce: int = 0) -> Tuple[str, dict]:
    """Sign the ClobAuth EIP-712 message. Returns (signature_hex, payload).

    payload contains the same fields the server expects in headers:
        POLY_ADDRESS, POLY_SIGNATURE, POLY_TIMESTAMP, POLY_NONCE.
    """
    try:
        from eth_account import Account
        from eth_account.messages import encode_typed_data
    except ImportError:
        raise SystemExit('eth_account not installed: pip install eth-account')

    if timestamp is None:
        timestamp = int(time.time())
    message = {
        'address': eth_address,
        'timestamp': str(timestamp),
        'nonce': int(nonce),
        'message': 'This message attests that I control the given wallet',
    }
    full_message = {
        'types': CLOB_AUTH_TYPES,
        'primaryType': 'ClobAuth',
        'domain': CLOB_AUTH_DOMAIN,
        'message': message,
    }
    encoded = encode_typed_data(full_message=full_message)
    signed = Account.sign_message(encoded, private_key=private_key)
    sig = signed.signature
    if hasattr(sig, 'hex'):
        sig = sig.hex()
    else:
        sig = str(sig)
    if not sig.startswith('0x'):
        sig = '0x' + sig
    headers = {
        'POLY_ADDRESS': eth_address,
        'POLY_SIGNATURE': sig,
        'POLY_TIMESTAMP': str(timestamp),
        'POLY_NONCE': str(nonce),
        'Content-Type': 'application/json',
    }
    return sig, headers


def derive_creds(eth_address: str, private_key: str,
                  *, dry_run: bool = False,
                  http_get=None, http_post=None) -> dict:
    """Derive (or create-if-not-exists) Polymarket L2 credentials.

    Strategy:
      1. Try GET /auth/derive-api-key with the ClobAuth signature. If the
         server has creds for this wallet, returns them immediately.
      2. If 404 → call POST /auth/api-key (same headers). Server creates
         creds and returns them.

    Returns {api_key, secret, passphrase} on success. Raises on permanent
    failure (bad signature, network down, etc).

    `http_get` / `http_post` for tests: pass `None` for the real network.
    """
    _, headers = _sign_clob_auth(eth_address, private_key)
    if dry_run:
        return {
            'api_key': '<dry-run>',
            'secret': '<dry-run>',
            'passphrase': '<dry-run>',
            'headers_preview': {k: ('<sig>' if 'SIGNATURE' in k else v)
                                for k, v in headers.items()},
            'would_GET': POLY_DERIVE_URL,
            'would_POST': POLY_CREATE_URL,
        }
    if http_get is None or http_post is None:
        import requests
        if http_get is None: http_get = requests.get
        if http_post is None: http_post = requests.post

    r = http_get(POLY_DERIVE_URL, headers=headers, timeout=10)
    if r.status_code == 200:
        return r.json()
    if r.status_code in (404, 401):
        # Either creds don't exist yet (404) or signature accepted but
        # no creds bound — try create.
        r2 = http_post(POLY_CREATE_URL, headers=headers, timeout=10)
        if r2.status_code in (200, 201):
            return r2.json()
        raise RuntimeError(f'create_api_key failed: {r2.status_code} '
                           f'{(r2.text or "")[:200]}')
    raise RuntimeError(f'derive_api_key failed: {r.status_code} '
                       f'{(r.text or "")[:200]}')


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--bot', required=True,
                    help='Bot id (e.g. bot1, bot2 ... bot6)')
    ap.add_argument('--dry-run', action='store_true',
                    help='Show what would be sent without calling Polymarket')
    args = ap.parse_args()

    bot_match = re.match(r'^bot(\d+)$', args.bot)
    if not bot_match:
        raise SystemExit(f'invalid --bot {args.bot!r} (expected bot1..botN)')
    n = int(bot_match.group(1))

    env = _read_env_file(CREDS_FILE)
    addr_key = f'BOT{n}_ETH_ADDRESS'
    pk_key = f'BOT{n}_PRIVATE_KEY'
    eth_address = env.get(addr_key)
    private_key = env.get(pk_key)
    if not eth_address:
        raise SystemExit(f'{addr_key} not set in {CREDS_FILE}')
    if not private_key:
        raise SystemExit(f'{pk_key} not set in {CREDS_FILE} '
                         f'(needed to sign derivation request)')

    print(f'[{args.bot}] address: {eth_address}')
    print(f'[{args.bot}] deriving L2 creds from Polymarket...')
    creds = derive_creds(eth_address, private_key, dry_run=args.dry_run)

    if args.dry_run:
        print(json.dumps(creds, indent=2))
        print('--dry-run: not writing to Credentials.env')
        return

    api_key = creds.get('api_key') or creds.get('apiKey')
    secret = creds.get('secret')
    passphrase = creds.get('passphrase')
    if not (api_key and secret and passphrase):
        raise SystemExit(f'unexpected response shape: {list(creds)}')

    _append_or_replace_lines(CREDS_FILE, {
        f'BOT{n}_POLY_API_KEY': api_key,
        f'BOT{n}_POLY_SECRET': secret,
        f'BOT{n}_POLY_PASSPHRASE': passphrase,
    })
    print(f'[{args.bot}] ✅ L2 creds written to Credentials.env')
    print(f'  BOT{n}_POLY_API_KEY={api_key[:8]}…')
    print(f'  BOT{n}_POLY_SECRET=***hidden***')
    print(f'  BOT{n}_POLY_PASSPHRASE=***hidden***')


if __name__ == '__main__':
    main()
