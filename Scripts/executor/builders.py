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
import hashlib
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
    order bodies — no signing happens in dry-run."""
    bot_id: str               # "bot1" .. "bot6"
    eth_address: str          # 0x...
    private_key: Optional[str] = None  # None in dry-run / Phase 2

    @property
    def can_sign(self) -> bool:
        return bool(self.private_key)


# ── Polymarket (EIP-712 limit order) ────────────────────────────────
# Polymarket CLOB uses an off-chain order with EIP-712 signature. Order body:
#   {salt, maker, signer, taker (=zero), tokenId, makerAmount, takerAmount,
#    expiration, nonce, feeRateBps, side (BUY=0/SELL=1), signatureType (=0)}
# Endpoint: POST https://clob.polymarket.com/order
POLY_CLOB_URL = "https://clob.polymarket.com/order"

def build_poly_order(token_id: str, side: str, price: float, size_usdc: float,
                     wallet: WalletStub, expiration_secs: int = 60) -> dict:
    """Build a Polymarket CLOB order body. `side` is 'BUY' or 'SELL'.
    `size_usdc` is dollar amount the taker pays (= contracts × price for BUY).
    Returns a dict with `body` ready to POST and `sign_payload` ready to sign.

    Validation:
        0 < price < 1
        size_usdc >= 1.0  (Polymarket min)
    """
    assert side in ('BUY', 'SELL'), f"side must be BUY|SELL, got {side}"
    assert 0 < price < 1, f"price out of range: {price}"
    assert size_usdc >= 1.0, f"size below Polymarket min $1: {size_usdc}"

    contracts = size_usdc / price
    maker_amount_wei = int(round(size_usdc * 1e6))      # USDC has 6 decimals
    taker_amount_wei = int(round(contracts * 1e6))      # CTF tokens 6 decimals on Polymarket

    body = {
        'salt': uuid.uuid4().hex,
        'maker': wallet.eth_address,
        'signer': wallet.eth_address,
        'taker': '0x0000000000000000000000000000000000000000',
        'tokenId': token_id,
        'makerAmount': str(maker_amount_wei),
        'takerAmount': str(taker_amount_wei),
        'expiration': str(int(time.time()) + expiration_secs),
        'nonce': '0',
        'feeRateBps': '0',
        'side': '0' if side == 'BUY' else '1',
        'signatureType': '0',
    }
    # Canonical signing payload — EIP-712 typed data hash. We just stringify
    # for dry-run; real signing in Phase 4 will use eth_account.messages.encode_typed_data.
    sign_payload = json.dumps(body, sort_keys=True).encode('utf-8')
    return {
        'platform': 'polymarket',
        'body': body,
        'sign_payload': sign_payload,
        'would_post_url': POLY_CLOB_URL,
        'expected_price': price,
        'expected_size_usdc': size_usdc,
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

    body = {
        'marketHash': market_hash,
        'taker': wallet.eth_address,
        'takerOutcome': outcome,
        'fillAmount': str(int(round(match['filled_usdc'] * (10 ** SX_USDC_DECIMALS)))),
        'orderHashes': [m['order_hash'] for m in match['matched']],
        'takerAmounts': [
            str(int(round(m['taker_amount_usdc'] * (10 ** SX_USDC_DECIMALS))))
            for m in match['matched']
        ],
        'expiry': str(int(time.time()) + expiration_secs),
        'salt': uuid.uuid4().hex,
    }
    sign_payload = json.dumps(body, sort_keys=True, default=str).encode('utf-8')

    return {
        'platform': 'sx_bet',
        'body': body,
        'sign_payload': sign_payload,
        'would_post_url': SX_FILL_URL,
        'expected_price': taker_price,
        'expected_size_usdc': size_usdc,
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
    maker_amount_wei = int(round(size_usdc * 1e6))   # USDC has 6 decimals
    taker_amount_wei = int(round(contracts * 1e6))   # CTF outcome tokens — 6 dp on Limitless

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
    signature = ""
    signed_ok = False
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
