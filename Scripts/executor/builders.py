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
# SX Bet flow: taker calls POST /orders/fill with target marketHash + outcome
# + size, the API matches it to existing maker orders. Signed payload is an
# EIP-712 over (marketHash, baseToken, betSize, percentageOdds, expiry, salt,
# isMakerBettingOutcomeOne, ...). For taker side we sign a "fill commitment".
# Endpoint: POST https://api.sx.bet/orders/fill
SX_FILL_URL = "https://api.sx.bet/orders/fill"

def build_sx_order(market_hash: str, outcome: int, taker_price: float,
                   size_usdc: float, wallet: WalletStub,
                   expiration_secs: int = 60) -> dict:
    """Build SX Bet taker-fill payload. `outcome` is 1 or 2 (which side the
    taker is buying). `taker_price` is what the taker pays per $1 contract
    (= 1 - maker_percentageOdds). Min size on SX Bet is $1.

    Note: real fill API needs the matched maker order hash list — we resolve
    that at fire-time inside atomic.py via a fresh /orders fetch (the
    builder produces a "request body" the firer fills in with matched orders).
    """
    assert outcome in (1, 2), f"outcome must be 1 or 2, got {outcome}"
    assert 0 < taker_price < 1, f"taker_price out of range: {taker_price}"
    assert size_usdc >= 1.0, f"size below SX min $1: {size_usdc}"

    body = {
        'marketHash': market_hash,
        'taker': wallet.eth_address,
        'takerOutcome': outcome,            # 1 or 2
        'fillAmount': str(int(round(size_usdc * 1e6))),  # USDC 6 decimals
        'maxPercentageOdds': str(int(round((1 - taker_price) * 1e20))),
        'expiry': str(int(time.time()) + expiration_secs),
        'salt': uuid.uuid4().hex,
        # `orderHashes` is filled at fire-time by the atomic engine
        # (after fetching live /orders for this marketHash)
        'orderHashes': None,
    }
    sign_payload = json.dumps(body, sort_keys=True, default=str).encode('utf-8')
    return {
        'platform': 'sx_bet',
        'body': body,
        'sign_payload': sign_payload,
        'would_post_url': SX_FILL_URL,
        'expected_price': taker_price,
        'expected_size_usdc': size_usdc,
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
