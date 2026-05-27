"""Per-platform fee math.

Extracted from arb_server.py in audit-28b cont (27.05.2026). Pure
function, no shared state.

History:
    Phase 19v18 (05.05.2026) — fee model is platform-specific.
    The original formula `theta * contracts * p * (1-p)` was Kalshi-only
    (variance-style: peaks at p=0.5). Polymarket/Limitless/SX charge a
    flat % on filled notional. Old code applied variance uniformly,
    under-reporting fees 4-20× — let losing deals slip past `net > 0`.
"""
from __future__ import annotations

from typing import Optional


def calc_fee(price: float, contracts: float, theta: float,
             platform: Optional[str] = None) -> float:
    """Taker fee for a single leg.

    Args:
        price:     leg ask price, 0..1 (clamped to 0.001..0.999).
        contracts: number of shares bought at `price`.
        theta:     decimal fee rate (e.g. 0.025 = 2.5%).
        platform:  'Polymarket' / 'Limitless' / 'SX Bet' / 'Kalshi' / 'cross_platform'.
                   Case-insensitive; only the prefix 'kalshi' branches to the
                   variance formula. Everything else is flat % on notional.

    Returns:
        Fee in the same units as `contracts * price` (USDC for our use).
    """
    p = max(0.001, min(0.999, price))
    plat = (platform or '').lower()
    if plat.startswith('kalshi'):
        # Variance fee: peaks at p=0.5, zero at p→0/1
        return theta * contracts * p * (1 - p)
    # Flat % on notional (Polymarket / Limitless / SX Bet / cross-platform)
    return theta * contracts * p
