"""SX Bet REST fetcher — maker orderbook → taker-side prices.

Extracted from arb_server.py in audit-28b cont 11 (29.05.2026).

SX Bet uses a maker-only orderbook (no auto-matched taker book). A maker
order with `isMakerBettingOutcomeOne=True` and `percentageOdds=p` means a
market-maker is bidding for outcomeOne at implied probability p. A taker
filling that order takes the OPPOSITE side (outcomeTwo) at price `1 - p`.

Owns:
    _fetch_sx_orders(market_hash)
        GET /orders?marketHashes=... → returns (market_hash, best1, depth1,
        best2, depth2) where best1/best2 are taker ask prices.

    _fetch_sx_3way_outcomes(market_hash, sx_orders)
        STUB for 3-way 1X2 markets (type=1). Currently always returns None
        until full 3rd-outcome orderbook semantics are wired.

Key behaviour:
- Phase 14a circuit-breaker integration so CF blocks don't cause 24h+
  hammering.
- Phase 19v26 SX API breaking-change handlers:
    * `maker=true` query removed (now expects EOA filter, returns 400)
    * response `data` field is now a flat list (was {'orders': [...]})
    * `orderSizeFillable` removed → use `totalBetSize - fillAmount`
    * gate on `orderStatus == 'ACTIVE'` (new field)
- Phase 11 Task F depth: count makers within DEPTH_SLIPPAGE_TOLERANCE
  of best maker bid (= within tolerance of best taker ask on opposite
  side).
- Phase 12b Bug 6: typed exception print (was bare except) so operator
  can grep `_fetch_sx_orders` failure type/message.
"""
from __future__ import annotations


def _fetch_sx_orders(market_hash: str) -> tuple:
    """Convert SX Bet maker orderbook into taker-side best ask prices +
    depth. Returns (market_hash, best1, depth1, best2, depth2).

    `best1` = best taker price for outcomeOne (= 1 - best maker bid on
    outcomeTwo). `best2` = best taker price for outcomeTwo (= 1 - best
    maker bid on outcomeOne). depth columns are USD notional fillable
    within DEPTH_SLIPPAGE_TOLERANCE of the top taker price.
    """
    from arb_server import _SESS_SX, _FETCH_TIMEOUT, DEPTH_SLIPPAGE_TOLERANCE

    # Phase 14a — circuit breaker. SX outages otherwise cascade into
    # 24h+ radar hammering on CF blocks.
    try:
        from circuit_breaker import get_breaker
        cb = get_breaker('sx', failure_threshold=3, cool_down_seconds=300)
    except Exception:
        cb = None
    if cb is not None and not cb.allow():
        return market_hash, None, 0, None, 0
    try:
        # Phase 19v26 (06.05.2026) — SX API breaking changes:
        # 1. `maker=true` removed (expects EOA filter, returns 400).
        # 2. response shape changed: `data` is now a flat list, not
        #    `{'orders': [...]}`.
        # 3. `orderSizeFillable` removed → totalBetSize - fillAmount.
        # 4. gate on `orderStatus == 'ACTIVE'` (new field).
        # All four manifested as "no SX deals ever" until fixed.
        r = _SESS_SX.get(
            f"https://api.sx.bet/orders?marketHashes={market_hash}",
            timeout=_FETCH_TIMEOUT,
        )
        if r.status_code in (403, 429, 502, 503, 521, 522):
            if cb:
                cb.on_failure(reason=f'HTTP {r.status_code}')
            return market_hash, None, 0, None, 0
        if cb and r.status_code == 200:
            cb.on_success()
        data = r.json()
        orders = []
        if data.get('status') == 'success':
            raw = data.get('data')
            if isinstance(raw, list):
                orders = raw
            elif isinstance(raw, dict):
                orders = raw.get('orders', []) or []

        makers_one = []  # makers betting outcomeOne (give taker outcomeTwo)
        makers_two = []  # makers betting outcomeTwo (give taker outcomeOne)
        for o in orders:
            try:
                price = float(o.get('percentageOdds', '0')) / 1e20
                if ('orderSizeFillable' in o
                        and o.get('orderSizeFillable') is not None):
                    # Old shape (back-compat)
                    size = float(o.get('orderSizeFillable', '0') or '0') / 1e6
                else:
                    # New shape: totalBetSize - fillAmount
                    total = float(o.get('totalBetSize', '0') or '0')
                    filled = float(o.get('fillAmount', '0') or '0')
                    size = max(0.0, (total - filled)) / 1e6
                status = o.get('orderStatus')
                if status is not None and status != 'ACTIVE':
                    continue
            except (TypeError, ValueError):
                continue
            if price <= 0 or price >= 1 or size <= 0:
                continue
            entry = (price, size)
            if o.get('isMakerBettingOutcomeOne', True):
                makers_one.append(entry)
            else:
                makers_two.append(entry)

        def _sx_top_depth(makers):
            """Return (taker_price, depth_usd_at_top). Phase 11 Task F:
            count makers within DEPTH_SLIPPAGE_TOLERANCE of best maker
            bid (= within tolerance of best taker price on opposite side)."""
            if not makers:
                return None, 0.0
            makers.sort(key=lambda m: -m[0])  # highest maker bid first
            best_pct = makers[0][0]
            taker_price = 1 - best_pct
            cutoff_pct = best_pct - DEPTH_SLIPPAGE_TOLERANCE - 1e-9
            depth_usd = 0.0
            for p_pct, sz in makers:
                if p_pct < cutoff_pct:
                    break
                depth_usd += sz * (1 - p_pct)
            return taker_price, depth_usd

        best2, depth_taker_two = _sx_top_depth(makers_one)
        best1, depth_taker_one = _sx_top_depth(makers_two)
        return market_hash, best1, depth_taker_one, best2, depth_taker_two
    except Exception as e:
        # Phase 12b Bug 6 — typed log so operator can grep failure
        # type/message (was bare `except:` swallowing 403/429/500/timeout).
        try:
            print(f"[SX] _fetch_sx_orders {market_hash[:10]}…: "
                  f"{type(e).__name__}: {e}", flush=True)
        except Exception:
            pass
        return market_hash, None, 0, None, 0


def _fetch_sx_3way_outcomes(market_hash: str, sx_orders: dict):
    """STUB for 3-way 1X2 markets (soccer type=1 with Draw outcome).

    Currently returns None — SX 3-way needs a per-outcome orderbook fetch
    that isn't wired yet. Cross-platform pipeline handles soccer via
    Polymarket+SX pairing without needing per-SX 3-way data, so this is
    a placeholder for a future direct-SX 3-way arb path.
    """
    res = sx_orders.get(market_hash)
    if res is None:
        return None
    return None
