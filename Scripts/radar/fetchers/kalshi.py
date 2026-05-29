"""Kalshi REST fetcher — orderbook for YES + NO sides.

Extracted from arb_server.py in audit-28b cont 11 (29.05.2026).

Kalshi is DISABLED by default since 12.05.2026 (US-only KYC blocks
operator's IP). The fetcher is preserved for parity + future US-KYC
deployments. ENABLE_KALSHI env flag gates whether scan_loop calls it.

Owns:
    _fetch_kalshi_ob(ticker)
        GET /trade-api/v2/markets/{ticker}/orderbook → returns
        (ticker, yes_ask, yes_depth, no_ask, no_depth).

Key behaviour:
- Kalshi orderbook returns `yes_dollars` / `no_dollars` levels — size
  field is already USDC notional (size_is_usd=True in
  _top_of_book_depth_usd), no Polymarket-style raw normalisation needed.
- Phase 10 #51: top-of-book depth only (not sum across all levels).
- Phase 11 Task F: DEPTH_SLIPPAGE_TOLERANCE window for ladder books.
- Phase 19v13: typed exception + debug log (was bare `except:` that
  hid the real failure cause).
"""
from __future__ import annotations

import logging

log = logging.getLogger('arb_server')


def _fetch_kalshi_ob(ticker: str) -> tuple:
    """GET /trade-api/v2/markets/{ticker}/orderbook → YES + NO sides.

    Returns (ticker, yes_ask, yes_depth, no_ask, no_depth). NO side
    enables ALL_NO and YES_NO_PAIR arb structures (Phase 1).
    """
    from arb_server import (
        _SESS_KALSHI, _FETCH_TIMEOUT, HEADERS,
        _top_of_book_depth_usd, DEPTH_SLIPPAGE_TOLERANCE,
    )

    try:
        r = _SESS_KALSHI.get(
            f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}/orderbook",
            timeout=_FETCH_TIMEOUT, headers=HEADERS,
        )
        ob = r.json().get('orderbook_fp', {})
        yes_lvls = ob.get('yes_dollars', [])
        no_lvls = ob.get('no_dollars', [])
        # Kalshi `*_dollars` fields are already USDC notional → size_is_usd=True.
        # Phase 11 Task F — DEPTH_SLIPPAGE_TOLERANCE window for realistic
        # fillable depth across ladder books.
        yes_ask, yes_depth = _top_of_book_depth_usd(
            yes_lvls, slippage_tolerance=DEPTH_SLIPPAGE_TOLERANCE,
            tuple_idx_price=0, tuple_idx_size=1, size_is_usd=True)
        no_ask, no_depth = _top_of_book_depth_usd(
            no_lvls, slippage_tolerance=DEPTH_SLIPPAGE_TOLERANCE,
            tuple_idx_price=0, tuple_idx_size=1, size_is_usd=True)
        return ticker, yes_ask, yes_depth, no_ask, no_depth
    except Exception as e:
        # Phase 19v13 — was bare except, would swallow KeyboardInterrupt /
        # SystemExit. Narrow to Exception + log at debug.
        log.debug("kalshi_ob_fail ticker=%s err=%r", ticker, e)
        return ticker, None, 0, None, 0
