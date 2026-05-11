"""Order builders per platform. In Phase 2 the radar runs dry-run only,
so builders return unsigned order bodies — real EIP-712 / RSA signing is
gated on `wallet.private_key` being set, which only happens once Phase 4
provisions keys via the wallet manager.

Each builder is a pure function: it takes the deal leg + wallet metadata
and returns a `dict` with `{platform, body, sign_payload, would_post_url}`.
The `body` is what would be POSTed; `sign_payload` is the canonical bytes
that need signing; `would_post_url` is the endpoint.

Why pure: keeps these unit-testable without a network or wallet, and the
atomic firer can build all legs of an arb in parallel with no I/O.
"""
import json
import time
import uuid
from dataclasses import dataclass
from typing import Optional


@dataclass
class WalletStub:
    """Minimal wallet shape needed by builders. Phase 4 swaps in the real
    Wallet class from `Scripts/wallets/` (with private_key, signing methods,
    balance fetcher, etc). For Phase 2 we only need eth_address to populate
    order bodies — no signing happens in dry-run.

    Phase 9e: `api_key` for Limitless X-API-Key (single string).
    Phase 9f: `poly_api_key` / `poly_secret` / `poly_passphrase` for
    Polymarket L2 HMAC headers. Polymarket needs all three to authenticate
    POST /order, the user-channel WS, and DELETE cancel calls. They're
    derived from a one-time L1 EIP-712 signature via py-clob-client's
    `create_or_derive_api_creds()` and stored in Credentials.env per bot."""
    bot_id: str               # "bot1" .. "bot6"
    eth_address: str          # 0x...
    private_key: Optional[str] = None      # None in dry-run / Phase 2
    api_key: Optional[str] = None          # Phase 9e — Limitless X-API-Key
    poly_api_key: Optional[str] = None     # Phase 9f — Polymarket L2 creds
    poly_secret: Optional[str] = None
    poly_passphrase: Optional[str] = None

    @property
    def can_sign(self) -> bool:
        return bool(self.private_key)

    @property
    def has_poly_creds(self) -> bool:
        return bool(self.poly_api_key and self.poly_secret
                    and self.poly_passphrase)


# ── Polymarket (EIP-712 V2 limit order) ─────────────────────────────
# Polymarket CLOB V2 uses an off-chain Order signed via EIP-712. Two domains:
#   Standard markets:  "Polymarket CTF Exchange",        v2, chainId 137,
#                      verifyingContract 0xE111180000d2663C0091e4f400237545B87B996B
#   NegRisk markets:   "Polymarket Neg Risk CTF Exchange", v2, chainId 137,
#                      verifyingContract 0xe2222d279d744050d28e00520010520000310F59
# Order struct (V2): {salt, maker, signer, tokenId, makerAmount, takerAmount,
#                     side, signatureType, timestamp(ms), metadata, builder}
# Note: V2 dropped `expiration`, `nonce`, `feeRateBps`, `taker` (always zero
# for limit orders). `metadata` and `builder` are bytes32; we use zero by
# default (no app metadata, no builder attribution).
# Source: github.com/cengizmandros/polymarket-cheatsheet (V2 reference).
# Endpoint: POST https://clob.polymarket.com/order
POLY_API_BASE = "https://clob.polymarket.com"
# IMPORTANT (Phase 9m, verified 28.04.2026):
# `POST /order` is the SINGLE route for both standard and negRisk markets.
# Source confirmed by reading github.com/Polymarket/clob-client main branch:
#   - endpoints.ts: POST_ORDER = "/order"  (only constant)
#   - client.ts: postOrder() always hits this URL regardless of negRisk
#   - utilities.ts orderToJson(): no `neg_risk` field in HTTP body
# Differentiation between standard and negRisk is encoded in the EIP-712
# `verifyingContract` of the signed Order — server reads the signature
# domain to route. Hence our build_poly_order(neg_risk=...) which switches
# verifyingContract is sufficient; no URL change needed.
POLY_CLOB_URL = POLY_API_BASE + "/order"
POLY_CANCEL_URL = POLY_API_BASE + "/order"           # DELETE /order/{id}
POLY_CANCEL_ALL_URL = POLY_API_BASE + "/orders"      # DELETE /orders
POLY_POSITIONS_URL = POLY_API_BASE + "/data/positions"

POLY_DOMAIN_STANDARD = {
    "name": "Polymarket CTF Exchange",
    "version": "2",
    "chainId": 137,
    "verifyingContract": "0xE111180000d2663C0091e4f400237545B87B996B",
}
POLY_DOMAIN_NEGRISK = {
    "name": "Polymarket Neg Risk CTF Exchange",
    "version": "2",
    "chainId": 137,
    "verifyingContract": "0xe2222d279d744050d28e00520010520000310F59",
}

POLY_ORDER_TYPES_V2 = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "Order": [
        {"name": "salt", "type": "uint256"},
        {"name": "maker", "type": "address"},
        {"name": "signer", "type": "address"},
        {"name": "tokenId", "type": "uint256"},
        {"name": "makerAmount", "type": "uint256"},
        {"name": "takerAmount", "type": "uint256"},
        {"name": "side", "type": "uint8"},
        {"name": "signatureType", "type": "uint8"},
        {"name": "timestamp", "type": "uint256"},
        {"name": "metadata", "type": "bytes32"},
        {"name": "builder", "type": "bytes32"},
    ],
}

ZERO_BYTES32 = "0x" + ("0" * 64)


def _sign_poly_eip712(order: dict, neg_risk: bool, private_key: str) -> Optional[str]:
    """Sign a Polymarket V2 Order via EIP-712. Returns hex signature or None
    on any failure — atomic.fire_arb falls back to dry-run audit cleanly."""
    try:
        from eth_account import Account
        from eth_account.messages import encode_typed_data
    except Exception:
        return None
    try:
        domain = POLY_DOMAIN_NEGRISK if neg_risk else POLY_DOMAIN_STANDARD
        # uint256 fields must be ints for the encoder; bytes32 must be bytes.
        msg = {}
        for k, v in order.items():
            if k in ("metadata", "builder"):
                # bytes32 — accept hex string or bytes
                if isinstance(v, (bytes, bytearray)):
                    msg[k] = bytes(v)
                else:
                    s = str(v) if v is not None else ZERO_BYTES32
                    if s.startswith("0x"): s = s[2:]
                    msg[k] = bytes.fromhex(s.rjust(64, '0'))
            elif k in ("salt", "tokenId", "makerAmount", "takerAmount",
                       "side", "signatureType", "timestamp"):
                msg[k] = int(v)
            else:
                msg[k] = v
        full_message = {
            "types": POLY_ORDER_TYPES_V2,
            "primaryType": "Order",
            "domain": domain,
            "message": msg,
        }
        encoded = encode_typed_data(full_message=full_message)
        signed = Account.sign_message(encoded, private_key=private_key)
        sig = signed.signature
        if hasattr(sig, "hex"):
            sig = sig.hex()
        else:
            sig = str(sig)
        return sig if sig.startswith("0x") else "0x" + sig
    except Exception:
        return None


def build_poly_hmac_headers(method: str, path: str, body: str,
                             api_key: str, api_secret: str, passphrase: str,
                             eth_address: str,
                             ts: Optional[int] = None) -> dict:
    """Build the L2 auth headers for Polymarket CLOB. Phase 9f.

    Per cheatsheet:
        prehash    = timestamp + method.upper() + path + body
        signature  = base64(hmac_sha256(api_secret, prehash))
        ALL endpoints requiring L2 auth send these 5 headers + Content-Type.

    py-clob-client uses base64 (not hex) for the signature; we follow that.
    """
    import base64
    import hashlib
    import hmac as _hmac
    if ts is None:
        ts = int(time.time())
    prehash = f"{ts}{method.upper()}{path}{body or ''}"
    # api_secret is base64-url-encoded per py-clob-client convention
    try:
        secret_bytes = base64.urlsafe_b64decode(api_secret)
    except Exception:
        secret_bytes = api_secret.encode('utf-8')
    sig_bytes = _hmac.new(secret_bytes, prehash.encode('utf-8'),
                          hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig_bytes).decode('ascii')
    # Phase 19v13 (05.05.2026) — checksum-normalize POLY_ADDRESS.
    # Polymarket's L2 server compares the header address against an
    # internal index that uses EIP-55 mixed-case checksum form. If the
    # caller passes a fully-lowercase or fully-uppercase address (common
    # when reading from an env file), some endpoints reject the request
    # with `INVALID_API_KEY`. Normalize defensively.
    try:
        from eth_utils import to_checksum_address  # type: ignore
        addr = to_checksum_address(eth_address)
    except Exception:
        addr = eth_address  # fall back if eth_utils unavailable
    return {
        "POLY_ADDRESS": addr,
        "POLY_TIMESTAMP": str(ts),
        "POLY_API_KEY": api_key,
        "POLY_PASSPHRASE": passphrase,
        "POLY_SIGNATURE": sig_b64,
        "Content-Type": "application/json",
    }


def _round_to_tick(price: float, tick_size: float) -> float:
    """Snap price to nearest tick. V2 markets enforce this server-side —
    if you submit 0.4523 on a tick=0.01 market the order is rejected.
    Phase 9j gate."""
    if tick_size <= 0:
        return price
    return round(round(price / tick_size) * tick_size, 6)


def build_poly_maker_order(token_id: str, side: str,
                            best_ask: float, best_bid: float,
                            size_usdc: float, wallet: WalletStub, *,
                            neg_risk: bool = False,
                            tick_size: float = 0.01,
                            min_order_size_usdc: float = 1.0,
                            expiration_secs: int = 60) -> dict:
    """Phase 15a (01.05.2026) — MAKER order builder for Polymarket V2.

    Places a GTC limit order ONE TICK INSIDE the spread (between best_bid and
    best_ask). This makes us the new best ask/bid at our price → zero taker
    fee on any matches against this order.

    Pricing rules:
        BUY  side: price = best_ask - tick_size  (we improve buy = LOWER bid? wait,
                  for BUY we want to BID — wait this is confusing)
                  Actually for BUY (we want to buy YES):
                  - we post at price BETWEEN best_bid and best_ask
                  - someone willing to SELL at our price will hit us as taker
                  - we pay 0 fee, they pay taker fee
                  Best: price = (best_bid + best_ask) / 2 if spread > tick, else
                        best_bid + tick (just above current best bid)
        SELL side: symmetric — price = best_ask - tick_size
                  (slightly under current ask → we become new best ask)

    Falls back to taker via build_poly_order if spread is too tight (< 1 tick)
    — there's no room to be maker.

    Returns same shape as build_poly_order with extra keys:
        is_maker: True
        maker_price: actual posted price
        spread_used_cents: spread we were operating in
        will_revert_to_taker: True if no maker room, caller should taker-fire.
    """
    # Validate spread
    if (best_ask is None or best_bid is None
            or not (0 < best_bid < best_ask < 1)):
        # Cannot determine spread — fall back to taker at provided price.
        # Caller (atomic) treats this as 'maker not possible'.
        order_dict = build_poly_order(
            token_id=token_id, side=side, price=best_ask or 0.5,
            size_usdc=size_usdc, wallet=wallet,
            neg_risk=neg_risk, tick_size=tick_size,
            min_order_size_usdc=min_order_size_usdc, order_type='GTC')
        order_dict['is_maker'] = False
        order_dict['will_revert_to_taker'] = True
        order_dict['maker_failure_reason'] = 'invalid_spread'
        return order_dict

    spread = best_ask - best_bid
    # Need at LEAST 1 tick of room — otherwise just be taker.
    if spread < tick_size + 1e-9:
        order_dict = build_poly_order(
            token_id=token_id, side=side, price=best_ask,
            size_usdc=size_usdc, wallet=wallet,
            neg_risk=neg_risk, tick_size=tick_size,
            min_order_size_usdc=min_order_size_usdc, order_type='GTC')
        order_dict['is_maker'] = False
        order_dict['will_revert_to_taker'] = True
        order_dict['maker_failure_reason'] = (
            f'spread_too_tight_{spread:.4f}<{tick_size:.4f}')
        return order_dict

    # Compute maker price: 1 tick inside spread.
    if side == 'BUY':
        maker_price = best_bid + tick_size
    elif side == 'SELL':
        maker_price = best_ask - tick_size
    else:
        raise ValueError(f"side must be BUY|SELL, got {side}")

    # Build via standard builder with computed maker price + GTC type.
    order_dict = build_poly_order(
        token_id=token_id, side=side, price=maker_price,
        size_usdc=size_usdc, wallet=wallet,
        neg_risk=neg_risk, tick_size=tick_size,
        min_order_size_usdc=min_order_size_usdc, order_type='GTC')
    order_dict['is_maker'] = True
    order_dict['maker_price'] = maker_price
    order_dict['spread_used_cents'] = round(spread * 100, 2)
    order_dict['will_revert_to_taker'] = False
    return order_dict


def build_poly_order(token_id: str, side: str, price: float, size_usdc: float,
                     wallet: WalletStub, *,
                     neg_risk: bool = False,
                     fee_rate_bps: int = 0,
                     expiration_secs: int = 60,
                     order_type: str = 'GTC',
                     tick_size: float = 0.01,
                     min_order_size_usdc: float = 1.0) -> dict:
    """Build a Polymarket CLOB V2 order ready for POST /order.

    `side`: 'BUY' or 'SELL'.
    `size_usdc`: dollar amount the taker pays.
    `neg_risk`: True for negRisk markets (different EIP-712 domain).
    `order_type`: 'GTC' (default), 'GTD' (good-till-date — uses
                  expiration_secs), 'FOK' (fill-or-kill).

    V2 migration notes (28.04.2026):
      - `feeRateBps` is dynamic per-market and queried via
        getClobMarketInfo(conditionID); it is NOT a signed Order field.
      - `nonce` removed; uniqueness comes from `timestamp` (ms) instead.
      - `taker` removed from signed struct — always zero address.
      - `expiration` removed from signed struct BUT kept in POST body
        for GTD orders (server uses it to enforce expiry).
      - Builder attribution moved into the signed `builder` bytes32
        field (zero default = no attribution). HMAC builder headers
        (POLY_BUILDER_*) are deprecated.
      - Collateral migrated USDC.e → pUSD: pre-trade `wrap()` on the
        Collateral Onramp; post-trade `unwrap()` to withdraw. See
        `Scripts/polymarket_approve.py` (Phase 9i+) for helper.

    Phase 9f: real EIP-712 signature when wallet.can_sign. Otherwise
    body['signature'] stays empty and atomic dryrun path consumes it.
    """
    assert side in ('BUY', 'SELL'), f"side must be BUY|SELL, got {side}"
    assert 0 < price < 1, f"price out of range: {price}"
    assert size_usdc >= min_order_size_usdc, (
        f"size ${size_usdc:.2f} below Polymarket min ${min_order_size_usdc:.2f}")

    # Phase 9j: snap price to V2 per-market tick. Server enforces this —
    # mis-aligned price → 400. tick_size defaults to 0.01 which is the
    # most common Polymarket tick; specific markets may use 0.001 (high-
    # liquidity sport books) or 0.005.
    snapped_price = _round_to_tick(price, tick_size)
    if abs(snapped_price - price) > 1e-9:
        price = snapped_price

    contracts = size_usdc / price
    usdc_wei = int(round(size_usdc * 1e6))           # USDC has 6 decimals
    contracts_wei = int(round(contracts * 1e6))      # CTF tokens use 1e6 scaling on Polymarket V2
    # Phase 19v19 (05.05.2026) — fix maker/taker amount semantics for SELL.
    # Polymarket V2 CTF Exchange convention:
    #   BUY  (side=0): maker gives USDC, takes CTF tokens
    #                  → makerAmount=USDC, takerAmount=CTF
    #   SELL (side=1): maker gives CTF, takes USDC
    #                  → makerAmount=CTF, takerAmount=USDC
    # Old code unconditionally set makerAmount=USDC, takerAmount=CTF →
    # SELL orders were ALWAYS rejected by the server (and the on-chain
    # CTF Exchange contract would reject too — maker has no USDC delta
    # to satisfy a CTF withdrawal). This blocked the entire revert /
    # SELL flatten flow in real mode.
    if side == 'BUY':
        maker_amount_wei = usdc_wei
        taker_amount_wei = contracts_wei
    else:  # SELL
        maker_amount_wei = contracts_wei
        taker_amount_wei = usdc_wei
    salt = int(uuid.uuid4().hex, 16)
    timestamp_ms = int(time.time() * 1000)

    order = {
        'salt': str(salt),
        'maker': wallet.eth_address,
        'signer': wallet.eth_address,
        'tokenId': str(token_id),
        'makerAmount': str(maker_amount_wei),
        'takerAmount': str(taker_amount_wei),
        'side': '0' if side == 'BUY' else '1',
        'signatureType': '0',         # 0 = EOA
        'timestamp': str(timestamp_ms),
        'metadata': ZERO_BYTES32,
        # `builder` stays ZERO. Phase 9m research (28.04.2026) confirmed:
        # Polymarket Builder Program is for apps/aggregators routing
        # external user flow — they CHARGE additional fees on top of
        # platform fee (max 100 bps taker / 50 bps maker), no rebate to
        # builder from Polymarket's own taker fee. For a solo trader
        # firing on own account, registering a builderCode would only
        # add cost. Default zero = no attribution = no extra fees.
        # Source: docs.polymarket.com/builders/{overview,tiers,fees}.
        'builder': ZERO_BYTES32,
    }

    signature = ""
    signed_ok = False
    if wallet.can_sign:
        sig = _sign_poly_eip712(order, neg_risk=neg_risk,
                                 private_key=wallet.private_key)
        if sig:
            signature = sig
            signed_ok = True

    order_with_sig = dict(order)
    order_with_sig['signature'] = signature

    # Polymarket CLOB POST body wraps the order with optional `owner` field
    # (= maker address) and orderType. V2: `expiration` lives here in the
    # body (NOT in the signed Order struct) for GTD orders; server enforces.
    api_body = {
        'order': order_with_sig,
        'owner': wallet.eth_address,
        'orderType': order_type,
    }
    if order_type == 'GTD':
        api_body['expiration'] = str(int(time.time()) + expiration_secs)

    # Deterministic JSON for dry-run audit logs and tests
    sign_payload = json.dumps(order, sort_keys=True).encode('utf-8')

    return {
        'platform': 'polymarket',
        'body': api_body,
        'order': order,                # convenience: unsigned order body
        'sign_payload': sign_payload,
        'would_post_url': POLY_CLOB_URL,
        'expected_price': price,
        'expected_size_usdc': size_usdc,
        'signed': signed_ok,
        'neg_risk': neg_risk,
        'eip712': {
            'domain': POLY_DOMAIN_NEGRISK if neg_risk else POLY_DOMAIN_STANDARD,
            'primaryType': 'Order',
            'types': POLY_ORDER_TYPES_V2,
        },
    }


def build_poly_cancel(order_id: str, wallet: WalletStub) -> dict:
    """DELETE /order/{id} with L2 HMAC auth headers."""
    assert order_id, "order_id required"
    path = f"/order/{order_id}"
    headers = {}
    if wallet.has_poly_creds:
        headers = build_poly_hmac_headers(
            method='DELETE', path=path, body='',
            api_key=wallet.poly_api_key,
            api_secret=wallet.poly_secret,
            passphrase=wallet.poly_passphrase,
            eth_address=wallet.eth_address,
        )
    return {
        'platform': 'polymarket',
        'op': 'cancel',
        'method': 'DELETE',
        'would_post_url': POLY_API_BASE + path,
        'headers': headers,
        'body': None,
    }


def build_poly_cancel_all(wallet: WalletStub) -> dict:
    """DELETE /orders — cancel every open order for this account."""
    path = "/orders"
    headers = {}
    if wallet.has_poly_creds:
        headers = build_poly_hmac_headers(
            method='DELETE', path=path, body='',
            api_key=wallet.poly_api_key,
            api_secret=wallet.poly_secret,
            passphrase=wallet.poly_passphrase,
            eth_address=wallet.eth_address,
        )
    return {
        'platform': 'polymarket',
        'op': 'cancel_all',
        'method': 'DELETE',
        'would_post_url': POLY_API_BASE + path,
        'headers': headers,
        'body': None,
    }


# ── SX Bet (taker fill of pre-signed maker orders) ──────────────────
# SX Bet flow:
#   1. Taker decides which marketHash + outcome they want to buy at what max price.
#   2. Taker fetches GET /orders?marketHashes=X&maker=true → live maker orders.
#   3. Taker filters to opposite-side orders (taker fills makers on the OTHER outcome)
#      and sorts by best taker price (= highest maker_percentageOdds).
#   4. Greedy match: pick orders top-down until cumulative fillable size >= taker's target.
#      If exhausted before target → partial fill (caller must decide whether to accept).
#   5. Build POST /orders/fill body with the matched orderHashes + per-order taker amounts.
#   6. Sign EIP-712 commitment, POST.
# Endpoint: POST https://api.sx.bet/orders/fill
# Phase 17 (01.05.2026) — SX EIP-712 OrderFill signing.
# Operator-flagged blocker: real-mode SX trading impossible without sig.
# Per SX Bet docs (https://docs.sx.bet/api/types#fill):
#   chainId 4162 (SX Network mainnet)
#   verifyingContract — fixed deployment per SX team
#   primaryType "Details" — array of orderHash + fillAmount per matched maker
SX_NETWORK_CHAIN_ID = 4162
SX_VERIFYING_CONTRACT = (
    "0xBe9F69dab98C1Ddee5BF31a9b1f5DBe88869B5d4"  # SX OrderFill contract
)
SX_DOMAIN = {
    "name": "SX Bet Order Fill",
    "version": "6.0",
    "chainId": SX_NETWORK_CHAIN_ID,
    "verifyingContract": SX_VERIFYING_CONTRACT,
}
SX_FILL_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "Details": [
        {"name": "action", "type": "string"},
        {"name": "market", "type": "string"},
        {"name": "betting", "type": "string"},
        {"name": "stake", "type": "string"},
        {"name": "worstOdds", "type": "string"},
        {"name": "executor", "type": "address"},
    ],
}


def _sign_sx_order_fill(taker_address: str, market_hash: str,
                        outcome: int, fill_amount: int,
                        worst_taker_price: float,
                        private_key: str) -> Optional[str]:
    """Sign SX Bet OrderFill EIP-712 message. Returns hex signature or None
    on failure (eth_account missing, etc.)."""
    try:
        from eth_account import Account
        from eth_account.messages import encode_typed_data
    except ImportError:
        return None
    try:
        message = {
            "action": "N/A",
            "market": market_hash,
            "betting": "Outcome 1" if outcome == 1 else "Outcome 2",
            "stake": str(fill_amount),
            # SX expects worstOdds as 1e20-scaled like percentageOdds.
            # Convert taker_price → maker_pct → 1e20 form.
            "worstOdds": str(int((1 - worst_taker_price) * 1e20)),
            "executor": taker_address,
        }
        full_message = {
            "types": SX_FILL_TYPES,
            "primaryType": "Details",
            "domain": SX_DOMAIN,
            "message": message,
        }
        encoded = encode_typed_data(full_message=full_message)
        signed = Account.sign_message(encoded, private_key=private_key)
        sig = signed.signature
        if hasattr(sig, "hex"):
            sig = sig.hex()
        else:
            sig = str(sig)
        return sig if sig.startswith("0x") else "0x" + sig
    except Exception:
        return None


SX_FILL_URL = "https://api.sx.bet/orders/fill"
SX_ORDERS_URL = "https://api.sx.bet/orders"

# Network-side note: SX uses different USDC bases on different chains.
# Mainline orders use USDC on SX Network (6 decimals, like Polygon USDC).
SX_USDC_DECIMALS = 6


def _opposite_side_filter(taker_outcome: int, is_maker_one: bool) -> bool:
    """Taker on outcome=1 needs maker on outcome=2 (and vice versa).
    Returns True if this maker order is fillable by this taker."""
    if taker_outcome == 1:
        return not is_maker_one        # maker on outcome 2
    return is_maker_one                 # taker on 2 needs maker on 1


def fetch_sx_matchable_orders(market_hash: str, taker_outcome: int,
                              fetcher=None) -> list:
    """Fetch live SX Bet orders for `market_hash`, return only those a taker
    on `taker_outcome` can fill (i.e. on the OPPOSITE outcome).

    `fetcher` is for tests — pass a callable returning the parsed `/orders`
    response. Default uses the real SX Bet API.

    Returns a list of dicts:
        {order_hash, maker_pct, taker_price, fillable_usdc, raw_order}
    The raw_order is kept so the firer can include it in the fill body
    if SX Bet adds required fields later.
    """
    if fetcher is None:
        import requests
        def _default():
            r = requests.get(
                f"{SX_ORDERS_URL}?marketHashes={market_hash}&maker=true",
                timeout=4,
            )
            return r.json()
        fetcher = _default

    try:
        data = fetcher() or {}
    except Exception:
        return []
    if data.get('status') != 'success':
        return []
    orders = (data.get('data') or {}).get('orders', []) or []

    matchable = []
    for o in orders:
        try:
            is_maker_one = bool(o.get('isMakerBettingOutcomeOne', True))
            if not _opposite_side_filter(taker_outcome, is_maker_one):
                continue
            maker_pct = float(o.get('percentageOdds', '0')) / 1e20
            if not (0 < maker_pct < 1):
                continue
            fillable = float(o.get('orderSizeFillable', '0')) / (10 ** SX_USDC_DECIMALS)
            if fillable <= 0:
                continue
            matchable.append({
                'order_hash': o.get('orderHash', ''),
                'maker_pct': maker_pct,
                'taker_price': 1 - maker_pct,        # what taker pays per $1 contract
                'fillable_usdc': fillable,           # taker-side capacity at this price
                'raw_order': o,
            })
        except Exception:
            continue
    return matchable


def match_sx_orders(matchable: list, target_size_usdc: float,
                    max_taker_price: float) -> dict:
    """Greedy match: take orders sorted by best taker price (lowest), filling
    `target_size_usdc` from each. Stops when target is covered OR when next
    order's price exceeds `max_taker_price` (slippage cap).

    Returns:
        {
          'matched':       [ {order_hash, taker_price, taker_amount_usdc}, ...],
          'filled_usdc':   total USDC the taker would pay,
          'filled_size':   total contract face value won (approximated as
                           the sum of taker_amounts since each $X taker leg
                           buys $X face per maker convention),
          'avg_price':     weighted average taker price,
          'partial':       True iff filled < target,
          'shortfall_usdc': target - filled (0 if fully matched),
          'best_price':    best taker price among matched orders,
          'worst_price':   worst taker price among matched orders,
        }
    """
    sorted_orders = sorted(matchable, key=lambda o: o['taker_price'])
    matched = []
    filled = 0.0
    cost_weighted_price = 0.0
    best_price = None
    worst_price = None

    for o in sorted_orders:
        if filled >= target_size_usdc:
            break
        if o['taker_price'] > max_taker_price:
            break  # all remaining orders are worse than our cap

        remaining = target_size_usdc - filled
        # Take min(needed, available) from this maker
        take = min(remaining, o['fillable_usdc'])
        matched.append({
            'order_hash': o['order_hash'],
            'taker_price': o['taker_price'],
            'taker_amount_usdc': round(take, 6),
        })
        filled += take
        cost_weighted_price += o['taker_price'] * take
        if best_price is None or o['taker_price'] < best_price:
            best_price = o['taker_price']
        if worst_price is None or o['taker_price'] > worst_price:
            worst_price = o['taker_price']

    avg_price = (cost_weighted_price / filled) if filled > 0 else None
    return {
        'matched': matched,
        'filled_usdc': round(filled, 6),
        'filled_size': round(filled, 6),
        'avg_price': round(avg_price, 6) if avg_price is not None else None,
        'partial': filled < target_size_usdc - 0.000001,
        'shortfall_usdc': round(max(0.0, target_size_usdc - filled), 6),
        'best_price': best_price,
        'worst_price': worst_price,
    }


def build_sx_order(market_hash: str, outcome: int, taker_price: float,
                   size_usdc: float, wallet: WalletStub,
                   expiration_secs: int = 60,
                   slippage_tolerance: float = 0.005,
                   fetcher=None) -> dict:
    """Build SX Bet taker-fill payload with live order matching.

    Phase 7 changes vs Phase 2 skeleton:
      - Fetches live /orders, filters to opposite-side maker orders, greedy-
        matches enough capacity to cover `size_usdc` at taker_price+slippage.
      - Returns a full body with `orderHashes[]` and `takerAmounts[]`,
        ready to sign and POST.
      - If partial fill (matched < requested), returns the partial body
        plus `partial_fill: True` so the firer can decide. atomic.py treats
        partial-fill arbs as failed (one leg short = no longer arb).

    `slippage_tolerance` (default 0.5¢) caps how far above `taker_price` we
    accept matched orders. Matches the radar's classify-pool buffer mood.

    `fetcher` is for tests — bypass the network with a callable returning
    the parsed /orders response.
    """
    assert outcome in (1, 2), f"outcome must be 1 or 2, got {outcome}"
    assert 0 < taker_price < 1, f"taker_price out of range: {taker_price}"
    assert size_usdc >= 1.0, f"size below SX min $1: {size_usdc}"

    matchable = fetch_sx_matchable_orders(market_hash, outcome, fetcher=fetcher)
    max_taker = taker_price + slippage_tolerance
    match = match_sx_orders(matchable, target_size_usdc=size_usdc,
                            max_taker_price=max_taker)

    fill_amount_int = int(round(match['filled_usdc'] * (10 ** SX_USDC_DECIMALS)))
    body = {
        'marketHash': market_hash,
        'taker': wallet.eth_address,
        'takerOutcome': outcome,
        'fillAmount': str(fill_amount_int),
        'orderHashes': [m['order_hash'] for m in match['matched']],
        'takerAmounts': [
            str(int(round(m['taker_amount_usdc'] * (10 ** SX_USDC_DECIMALS))))
            for m in match['matched']
        ],
        'expiry': str(int(time.time()) + expiration_secs),
        'salt': uuid.uuid4().hex,
    }

    # Phase 17 (01.05.2026) — EIP-712 sign. Without this, SX real-mode
    # was impossible (operator's blocker). Signed only when wallet has
    # private_key — dry-run path stays sig-empty for audit logging.
    signature = ""
    signed_ok = False
    if wallet.can_sign:
        # Phase 19v19 (05.05.2026) — `worstOdds` MUST be the slippage CAP
        # (max_taker), not the OBSERVED worst price among already-matched
        # makers (`match['worst_price']`). The signed `worstOdds` is the
        # contract-enforced slippage threshold at fill time: if any maker
        # whose order is filled has price worse than `worstOdds`, the
        # transaction reverts. Using `worst_price` (backward-looking)
        # provided no protection against MM withdrawal between snapshot
        # and fill: server re-matches at next-best, signed worstOdds is
        # already the older worst, fill goes through at WORSE odds than
        # operator intended.
        sig = _sign_sx_order_fill(
            taker_address=wallet.eth_address,
            market_hash=market_hash,
            outcome=outcome,
            fill_amount=fill_amount_int,
            worst_taker_price=max_taker,
            private_key=wallet.private_key,
        )
        if sig:
            signature = sig
            signed_ok = True
    body['takerSig'] = signature

    sign_payload = json.dumps(body, sort_keys=True, default=str).encode('utf-8')

    return {
        'platform': 'sx_bet',
        'body': body,
        'sign_payload': sign_payload,
        'would_post_url': SX_FILL_URL,
        'expected_price': taker_price,
        'expected_size_usdc': size_usdc,
        'signed': signed_ok,
        'eip712': {
            'domain': SX_DOMAIN,
            'primaryType': 'Details',
            'types': SX_FILL_TYPES,
        },
        # Phase 7 match details — atomic.py / dryrun_log surface these
        'sx_match': {
            'avg_fill_price': match['avg_price'],
            'best_price': match['best_price'],
            'worst_price': match['worst_price'],
            'filled_usdc': match['filled_usdc'],
            'shortfall_usdc': match['shortfall_usdc'],
            'partial_fill': match['partial'],
            'matched_orders': len(match['matched']),
            'available_orders': len(matchable),
            'slippage_cap': slippage_tolerance,
            'max_taker_price_accepted': max_taker,
        },
        'partial_fill': match['partial'],
    }


# ── Kalshi (disabled) ───────────────────────────────────────────────
# Kalshi requires US KYC + RSA-signed REST orders. The user is non-US so
# this builder is intentionally a no-op that returns a marker dict — the
# atomic engine refuses to fire any leg with platform='kalshi' to make the
# block visible at runtime instead of silently skipping.
def build_kalshi_order(*args, **kwargs) -> dict:
    """Disabled. Kalshi requires US-resident KYC; builder kept for symmetry
    with other platforms but never actually fires. Returning the marker
    surfaces this clearly in logs/UI rather than dropping the leg silently."""
    return {
        'platform': 'kalshi',
        'body': None,
        'sign_payload': None,
        'would_post_url': None,
        'expected_price': kwargs.get('price'),
        'expected_size_usdc': kwargs.get('size_usdc'),
        'disabled_reason': 'Kalshi requires US KYC — non-US user, blocked at builder',
    }


# ── Limitless Exchange (Base L2) ────────────────────────────────────
# CLOB on Base, EIP-712 signed orders, USDC collateral. Architecture
# mirrors Polymarket but the EIP-712 domain + verifyingContract differ:
#
#   domain = {
#     name: "Limitless CTF Exchange",
#     version: "1",
#     chainId: 8453,                       # Base mainnet
#     verifyingContract: <venue.exchange>  # comes from market metadata
#   }
#   primary type = "Order"
#   fields = salt(u256), maker(addr), signer(addr), taker(addr),
#            tokenId(u256), makerAmount(u256), takerAmount(u256),
#            expiration(u256), nonce(u256), feeRateBps(u256),
#            side(u8), signatureType(u8)
#
# Source: https://docs.limitless.exchange/developers/eip712-signing
# REST:   POST https://api.limitless.exchange/orders
#         Body wraps the signed Order: {order:{...,signature}, marketSlug,
#         orderType, ownerId?, clientOrderId?}
LIMITLESS_API_BASE = "https://api.limitless.exchange"
LIMITLESS_ORDER_URL = LIMITLESS_API_BASE + "/orders"
LIMITLESS_CANCEL_BATCH_URL = LIMITLESS_API_BASE + "/orders/cancel-batch"
LIMITLESS_DOMAIN_NAME = "Limitless CTF Exchange"
LIMITLESS_DOMAIN_VERSION = "1"
LIMITLESS_CHAIN_ID = 8453     # Base mainnet
ZERO_ADDR = "0x0000000000000000000000000000000000000000"

# Default exchange contract — overridden per-market via venue.exchange when
# available. Sourced from limitless-cli config.
LIMITLESS_DEFAULT_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"

LIMITLESS_ORDER_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "Order": [
        {"name": "salt", "type": "uint256"},
        {"name": "maker", "type": "address"},
        {"name": "signer", "type": "address"},
        {"name": "taker", "type": "address"},
        {"name": "tokenId", "type": "uint256"},
        {"name": "makerAmount", "type": "uint256"},
        {"name": "takerAmount", "type": "uint256"},
        {"name": "expiration", "type": "uint256"},
        {"name": "nonce", "type": "uint256"},
        {"name": "feeRateBps", "type": "uint256"},
        {"name": "side", "type": "uint8"},
        {"name": "signatureType", "type": "uint8"},
    ],
}


def _sign_limitless_eip712(order: dict, verifying_contract: str,
                            private_key: str) -> Optional[str]:
    """Sign a Limitless Order via EIP-712. Returns hex signature or None on
    any failure (missing eth_account, bad inputs, etc.) so the caller can
    fall back to dry-run cleanly without crashing the radar.

    Why None on failure (not raise): in dry-run mode signing is optional
    and we never want a missing dep / bad address to take down the scanner.
    """
    try:
        from eth_account import Account
        from eth_account.messages import encode_typed_data
    except Exception:
        return None
    try:
        domain = {
            "name": LIMITLESS_DOMAIN_NAME,
            "version": LIMITLESS_DOMAIN_VERSION,
            "chainId": LIMITLESS_CHAIN_ID,
            "verifyingContract": verifying_contract,
        }
        # All uint256 fields must be ints for typed-data encoding (strings
        # are accepted on the JSON wire but not by the encoder).
        msg = {k: (int(v) if k in (
            "salt", "tokenId", "makerAmount", "takerAmount",
            "expiration", "nonce", "feeRateBps", "side", "signatureType",
        ) else v) for k, v in order.items()}
        full_message = {
            "types": LIMITLESS_ORDER_TYPES,
            "primaryType": "Order",
            "domain": domain,
            "message": msg,
        }
        encoded = encode_typed_data(full_message=full_message)
        signed = Account.sign_message(encoded, private_key=private_key)
        sig_bytes = getattr(signed, "signature", None)
        if sig_bytes is None:
            return None
        if hasattr(sig_bytes, "hex"):
            sig = sig_bytes.hex()
        else:
            sig = str(sig_bytes)
        return sig if sig.startswith("0x") else "0x" + sig
    except Exception:
        return None


def build_limitless_order(slug: str, side: str, price: float, size_usdc: float,
                          wallet: WalletStub,
                          *,
                          token_id: Optional[str] = None,
                          verifying_contract: Optional[str] = None,
                          expiration_secs: int = 60,
                          fee_rate_bps: int = 0,
                          order_type: str = "GTC",
                          owner_id: Optional[int] = None,
                          client_order_id: Optional[str] = None) -> dict:
    """Build a Limitless Exchange CLOB order ready for POST /orders.

    Required positional args mirror the other builders so the atomic firer
    can call them uniformly. The keyword-only args carry Limitless-specific
    metadata (token_id, verifying_contract) that are gated behind real-mode:
      * In dry-run / Phase 2: token_id may be None — we still produce the
        wrapper body shape and a deterministic JSON sign_payload so the
        paper-trade pipeline can reason about what *would* be sent.
      * In real-mode (wallet.private_key set, token_id + verifying_contract
        present): we sign EIP-712 and embed the signature in body.order.

    Validation:
        0 < price < 1
        size_usdc >= 1.0 (Limitless min)
        side in {BUY, SELL}
    """
    assert side in ('BUY', 'SELL'), f"side must be BUY|SELL, got {side}"
    assert 0 < price < 1, f"price out of range: {price}"
    assert size_usdc >= 1.0, f"size below Limitless min $1: {size_usdc}"

    contracts = size_usdc / price
    usdc_wei = int(round(size_usdc * 1e6))           # USDC has 6 decimals
    contracts_wei = int(round(contracts * 1e6))      # CTF outcome tokens — 6 dp on Limitless
    # Phase 19v23 (05.05.2026) — fix maker/taker semantics for SELL
    # (parity with Phase 19v19 Polymarket fix). Limitless CTF Exchange
    # follows the same convention:
    #   BUY  (side=0): maker gives USDC, takes CTF
    #   SELL (side=1): maker gives CTF, takes USDC
    # Old code unconditionally built BUY-shape (`makerAmount=USDC,
    # takerAmount=CTF`) → SELL FOK orders rejected by server AND
    # on-chain CTF Exchange (insufficient USDC delta to satisfy CTF
    # withdrawal). Triggered on `revert_filled_legs` for cross-platform
    # arbs with a filled Limitless leg → directional Limitless exposure
    # left open after revert "completes" with `sell_lim_HTTP_4xx`.
    if side == 'BUY':
        maker_amount_wei = usdc_wei
        taker_amount_wei = contracts_wei
    else:  # SELL
        maker_amount_wei = contracts_wei
        taker_amount_wei = usdc_wei

    # salt must fit uint256; uuid4 hex is 128-bit which is plenty.
    salt = int(uuid.uuid4().hex, 16)

    order = {
        'salt': str(salt),
        'maker': wallet.eth_address,
        'signer': wallet.eth_address,
        'taker': ZERO_ADDR,
        'tokenId': str(token_id) if token_id is not None else '0',
        'makerAmount': str(maker_amount_wei),
        'takerAmount': str(taker_amount_wei),
        'expiration': str(int(time.time()) + expiration_secs),
        'nonce': '0',
        'feeRateBps': str(fee_rate_bps),
        'side': '0' if side == 'BUY' else '1',
        'signatureType': '0',
    }

    # Real EIP-712 sign only when we have everything. Otherwise leave the
    # signature empty — atomic.fire_arb in dry-run mode never POSTs anyway.
    # Phase 12b (01.05.2026) — Bug 8: explicit warning when can_sign but
    # token_id missing. Old code silently produced an unsigned order with
    # tokenId='0' that server would reject without operator visibility.
    signature = ""
    signed_ok = False
    if wallet.can_sign and token_id is None:
        import logging as _lg
        _lg.getLogger(__name__).warning(
            "build_limitless_order: wallet can sign but token_id=None for "
            "slug=%s — order will be UNSIGNED with tokenId='0' (rejected "
            "by server). Caller must fetch market_meta first.", slug)
    if (wallet.can_sign and token_id is not None
            and (verifying_contract or LIMITLESS_DEFAULT_EXCHANGE)):
        vc = verifying_contract or LIMITLESS_DEFAULT_EXCHANGE
        sig = _sign_limitless_eip712(order, vc, wallet.private_key)
        if sig:
            signature = sig
            signed_ok = True
    order_with_sig = dict(order)
    order_with_sig['signature'] = signature

    api_body = {
        'order': order_with_sig,
        'orderType': order_type,
        'marketSlug': slug,
    }
    if owner_id is not None:
        api_body['ownerId'] = owner_id
    if client_order_id is not None:
        api_body['clientOrderId'] = client_order_id

    # Deterministic JSON of the unsigned order — useful for dry-run audit
    # logs and to let tests assert the exact bytes that would be signed.
    sign_payload = json.dumps(order, sort_keys=True).encode('utf-8')

    return {
        'platform': 'limitless',
        'body': api_body,
        'order': order,                       # convenience: unsigned order
        'sign_payload': sign_payload,
        'would_post_url': LIMITLESS_ORDER_URL,
        'expected_price': price,
        'expected_size_usdc': size_usdc,
        'signed': signed_ok,
        'eip712': {
            'domain': {
                'name': LIMITLESS_DOMAIN_NAME,
                'version': LIMITLESS_DOMAIN_VERSION,
                'chainId': LIMITLESS_CHAIN_ID,
                'verifyingContract': verifying_contract or LIMITLESS_DEFAULT_EXCHANGE,
            },
            'primaryType': 'Order',
            'types': LIMITLESS_ORDER_TYPES,
        },
    }


# ── Limitless cancel helpers ────────────────────────────────────────
# Three flavours per docs:
#   DELETE /orders/{orderId}                 — single
#   POST   /orders/cancel-batch  {orderIds}  — batch
#   DELETE /orders/all/{slug}                — every order on a market
# All require X-API-Key header (no signature). The watchdog and risk
# killswitch use these to flush pending orders when the operator panics.

def build_limitless_cancel(order_id: str, api_key: str = "") -> dict:
    """Single-order cancel. Returns request bundle for an HTTP DELETE call.
    `api_key` is optional in dry-run; required in real-mode (server rejects
    without X-API-Key)."""
    assert order_id, "order_id required"
    return {
        'platform': 'limitless',
        'op': 'cancel',
        'method': 'DELETE',
        'would_post_url': f"{LIMITLESS_API_BASE}/orders/{order_id}",
        'headers': {'X-API-Key': api_key} if api_key else {},
        'body': None,
    }


def build_limitless_cancel_batch(order_ids, api_key: str = "") -> dict:
    """Batch cancel — preferred over N single calls when killswitch triggers.
    Server expects `{orderIds: [...]}` payload. We wrap so the watchdog can
    treat the bundle uniformly with the single-cancel and slug-cancel paths."""
    assert order_ids, "order_ids must be non-empty"
    return {
        'platform': 'limitless',
        'op': 'cancel_batch',
        'method': 'POST',
        'would_post_url': LIMITLESS_CANCEL_BATCH_URL,
        'headers': ({'X-API-Key': api_key, 'Content-Type': 'application/json'}
                    if api_key else {'Content-Type': 'application/json'}),
        'body': {'orderIds': list(order_ids)},
    }


def build_limitless_cancel_all_market(slug: str, api_key: str = "") -> dict:
    """Cancel every open order on a single market slug. Useful when one leg
    of an arb fails and we need to walk back any other legs already placed
    on the same market."""
    assert slug, "slug required"
    return {
        'platform': 'limitless',
        'op': 'cancel_all_market',
        'method': 'DELETE',
        'would_post_url': f"{LIMITLESS_API_BASE}/orders/all/{slug}",
        'headers': {'X-API-Key': api_key} if api_key else {},
        'body': None,
    }
