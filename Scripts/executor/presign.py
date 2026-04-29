"""Pre-sign EIP-712 orders for NEAR-pool candidates.

Latency optimization (idea.md Phase 6):

    Without pre-signing — every fire pays:
      sign_time = ~50ms × N_legs ≈ 100-150ms cold

    With pre-signing — signing happens during NEAR classification,
    N seconds before the order fires:
      sign_time at fire ≈ 0ms (cached signed body, just POST it)

    Net effect: 100-150ms removed from critical fire path.

Strategy
--------
1. After classify_pools, for every NEAR candidate, schedule a
   background pre-signing pass.
2. Pre-signing builds an EIP-712 signed order at:
     price = current_ask + SAFETY_MARGIN
     size  = MAX_PER_TRADE_USD-derived stake
   Reasoning: limit orders match at BEST available price up to limit.
   Signing slightly above current ask guarantees fill if the arb still
   exists at fire time, and fails-safe (no fill, no lock-in) if it doesn't.
3. Bundle is cached by `cand_id` with TTL=30s. Polymarket orders have
   60s expiration anyway (GTD), so stale signatures auto-cancel.
4. At fire time, atomic.py calls `consume_presigned(cand_id, structure)`.
   Cache hit → instant POST. Cache miss → fall back to inline signing.
5. Pre-signing is a no-op when wallets are not configured (dry-run
   default) — the cache stores wallet-less builders that the executor
   already handles.

Thread safety
-------------
- `_cache` is a dict, all reads/writes go through `_cache_lock`.
- `pre_sign_for_pool` is called from a daemon thread; it never holds
  the lock during signing (only when reading/writing the cache slot).
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

# Module-level cache. Key: cand_id (str). Value: PreSignedBundle.
_cache: Dict[str, "PreSignedBundle"] = {}
_cache_lock = threading.Lock()

# How long a pre-signed bundle stays valid. Must be < Polymarket order
# expiration (60s GTD default) to leave a safety margin for the actual
# match-engine processing.
TTL_SECONDS = 30.0

# Safety margin added to price when pre-signing — guarantees fill if
# arb still exists, fails-safe (no fill) if MM closed the gap. 0.5¢ is
# 0.005 in price space. Empirically: most fast-closing arbs move 1-2¢
# in the close window; 0.5¢ keeps us in the fillable zone.
PRICE_SAFETY_MARGIN = 0.005

# Stats for /api/risk_status visibility.
_stats = {
    "presigned_total": 0,    # total bundles ever cached
    "presign_failures": 0,
    "cache_hits": 0,         # consume_presigned found valid bundle
    "cache_misses": 0,       # consume_presigned fell back to inline sign
    "cache_expired": 0,      # bundle was found but past TTL
}
_stats_lock = threading.Lock()


@dataclass
class PreSignedBundle:
    """One pre-signed bundle covers ALL applicable structures (A/B/C) for
    a single candidate. The fire path picks the right structure at deal
    time."""
    cand_id: str
    deal_title: str
    platform: str                # 'polymarket' | 'limitless' | 'kalshi' | 'sx'
    signed_orders: dict          # {structure_key: [signed_order_body, ...]}
                                 # structure_key in {'all_yes', 'all_no', 'yes_no_pair:N'}
    expires_at: float            # unix timestamp; bundle dropped after this
    wallets_used: List[str] = field(default_factory=list)  # eth addresses

    def is_valid(self) -> bool:
        return time.time() < self.expires_at


def cand_id_for_deal(deal: dict) -> str:
    """Deterministic id used as cache key. Same id used at NEAR-pre-sign
    time and at fire time so both halves see the same bundle.

    For candidates we have only the event title + platform + structure;
    that triple is unique per arb opportunity at one moment in time.
    """
    return f"{deal.get('platform','?')}::{deal.get('title','?')}::{deal.get('arb_structure','?')}"


def consume_presigned(cand_id: str, structure: str) -> Optional[List[dict]]:
    """Atomic 'pop' — return signed orders if cache hit and not expired,
    else None. Bundle is REMOVED from cache after consumption to prevent
    accidental double-fire with the same signature (Polymarket would reject
    the second one but we shouldn't even try)."""
    with _cache_lock:
        bundle = _cache.get(cand_id)
        if bundle is None:
            with _stats_lock:
                _stats["cache_misses"] += 1
            return None
        if not bundle.is_valid():
            _cache.pop(cand_id, None)
            with _stats_lock:
                _stats["cache_expired"] += 1
                _stats["cache_misses"] += 1
            return None
        orders = bundle.signed_orders.get(structure)
        if orders is None:
            with _stats_lock:
                _stats["cache_misses"] += 1
            return None
        # Pop on consume — single use.
        _cache.pop(cand_id, None)
        with _stats_lock:
            _stats["cache_hits"] += 1
        return orders


def evict_expired() -> int:
    """Remove all bundles past TTL. Called periodically by a janitor
    thread or directly before pre_sign_for_pool to free memory.
    Returns number removed."""
    now = time.time()
    removed = 0
    with _cache_lock:
        stale = [k for k, b in _cache.items() if now >= b.expires_at]
        for k in stale:
            _cache.pop(k, None)
            removed += 1
    return removed


def pre_sign_for_near_candidate(
    cand_id: str,
    deal_title: str,
    platform: str,
    structure_orders: dict,   # {struct_key: [(token_id, side, price, size, neg_risk), ...]}
    wallet_assignments: dict, # {struct_key: [wallet_obj, ...]}  parallel to orders
) -> bool:
    """Build EIP-712 signed bodies for all structures we might fire on
    this candidate, cache the bundle by `cand_id`. Returns True on success.

    `structure_orders` shape — example for ALL_YES with 3 outcomes:
        {
            'all_yes': [
                ('token_id_1', 'BUY', 0.30, 18.0, False),
                ('token_id_2', 'BUY', 0.25, 18.0, False),
                ('token_id_3', 'BUY', 0.40, 18.0, False),
            ],
        }

    The caller is responsible for sizing legs (we don't re-derive stake
    from balance here — that's coordinator/atomic territory).

    Phase: this function is platform-aware via dispatch on `platform`.
    Currently implemented: polymarket. Limitless / Kalshi / SX shells
    fall through to no-op (their signing flow differs).
    """
    if platform != 'polymarket':
        # Non-Polymarket pre-signing not implemented yet. Fire path
        # falls back to inline signing for these — fine, their inline
        # cost is also lower (different cryptography).
        return False

    try:
        from . import builders
    except ImportError:
        log.warning("presign: cannot import builders — module load order issue")
        return False

    signed_bundle: dict = {}
    addrs_used: list = []
    for struct_key, leg_specs in structure_orders.items():
        wallets = wallet_assignments.get(struct_key) or []
        if len(wallets) < len(leg_specs):
            # Fewer wallets than legs — coordinator failed; skip this struct
            continue
        signed_legs = []
        all_ok = True
        for i, (token_id, side, price, size, neg_risk) in enumerate(leg_specs):
            wallet = wallets[i]
            try:
                # Sign at price + SAFETY_MARGIN so the limit doesn't bind
                # on small adverse moves. Server matches at BEST <= limit.
                limit_price = min(0.99, price + PRICE_SAFETY_MARGIN)
                order = builders.build_poly_order(
                    token_id=token_id, side=side,
                    price=limit_price, size_usdc=size,
                    wallet=wallet, neg_risk=neg_risk,
                    order_type='GTD', expiration_secs=60,
                )
                signed_legs.append(order)
                if wallet.eth_address not in addrs_used:
                    addrs_used.append(wallet.eth_address)
            except Exception as e:
                log.warning("presign[%s][%s] leg %d failed: %s",
                            cand_id[:30], struct_key, i, e)
                all_ok = False
                break
        if all_ok and signed_legs:
            signed_bundle[struct_key] = signed_legs

    if not signed_bundle:
        with _stats_lock:
            _stats["presign_failures"] += 1
        return False

    bundle = PreSignedBundle(
        cand_id=cand_id, deal_title=deal_title,
        platform=platform,
        signed_orders=signed_bundle,
        expires_at=time.time() + TTL_SECONDS,
        wallets_used=addrs_used,
    )
    with _cache_lock:
        _cache[cand_id] = bundle
    with _stats_lock:
        _stats["presigned_total"] += 1
    return True


def get_stats() -> dict:
    """Return a copy of the stats dict for /api/risk_status display.
    Includes cache size + hit-rate."""
    with _stats_lock:
        s = dict(_stats)
    with _cache_lock:
        s["cache_size"] = len(_cache)
    total = s["cache_hits"] + s["cache_misses"]
    s["hit_rate_pct"] = round(s["cache_hits"] / total * 100, 1) if total else 0.0
    return s


def clear_cache() -> int:
    """Wipe everything — used on radar restart and in tests. Returns
    number cleared."""
    with _cache_lock:
        n = len(_cache)
        _cache.clear()
    return n
