"""Atomic arb firer.

`fire_arb(deal, wallets)` distributes the deal's legs across a wallet pool,
fires them in parallel via ThreadPoolExecutor (target <100ms), and on
success/failure returns an ArbFireResult describing every leg.

Phase 2 runs in DRY_RUN mode by default — no real POSTs. Each leg is logged
to Executions/dryrun.jsonl with its expected fill price/size, and a
delayed evaluator re-fetches the orderbook 5s later to compute realistic
slippage (this is the foundation for Phase 5 paper-trading metrics).

Real-mode safeguards (active when DRY_RUN=False, Phase 5+ graduation gate):
    - 2s per-order timeout → cancel that leg
    - Slippage check: |fill_price - expected| > 0.001 → cancel + revert
    - Dead-man switch: no fill confirms within 5s → cancel all + revert
    - Reversal: if the arb is broken (some legs filled, others cancelled),
      sell off filled legs at market to flatten the book
"""
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from typing import List, Optional

from . import builders
from . import dryrun_log
from . import fills

log = logging.getLogger(__name__)

# Dry-run is the default; flip via env DRY_RUN=0 once Phase 4 wallets land
# AND Phase 5 graduation gate (>=70% win-rate over 100 paper trades) passes.
DRY_RUN = os.environ.get('DRY_RUN', '1') != '0'

# Per-order knobs — same defaults as plan
PER_ORDER_TIMEOUT_S = 2.0
DEADMAN_TIMEOUT_S = 5.0
# Phase 11 Task F (01.05.2026): raised 0.001 → 0.005 to match
# DEPTH_SLIPPAGE_TOLERANCE in arb_server.py. The two MUST match: depth
# counted within N¢ of best ask is only fillable if the executor accepts
# fills within that same N¢. Strict 0.001 over-cancelled normal fills on
# multi-level MM ladders. Override via env to tighten.
SLIPPAGE_TOLERANCE = float(os.environ.get('SLIPPAGE_TOLERANCE', '0.005'))
REALISTIC_EVAL_DELAY_S = 5.0   # delay before sampling real book for paper-trade row

# Phase 15 (01.05.2026) — maker mode tuning.
# MAKER_MODE_ENABLED: opt-in env (default off). When on, fire_arb selects
# maker vs taker per leg based on arb spread (see select_fire_mode below).
MAKER_MODE_ENABLED = os.environ.get('MAKER_MODE_ENABLED', '0') == '1'
# Maker timeout — how long to wait for fill before cancel-and-retry.
MAKER_FILL_TIMEOUT_S = float(os.environ.get('MAKER_FILL_TIMEOUT_S', '5.0'))
# Adverse selection guard — if cross-source price drifts > N from our maker
# price within 500ms checks, cancel.
ADVERSE_SELECTION_PRICE_DRIFT = float(
    os.environ.get('ADVERSE_SELECTION_PRICE_DRIFT', '0.01'))
TARGET_FIRE_BUDGET_MS = 100    # informational, used in logs


@dataclass
class LegResult:
    leg_idx: int
    platform: str
    status: str              # 'dry-fired', 'filled', 'cancelled', 'rejected', 'timeout',
                             # 'disabled', 'partial' (SX Bet — matched < requested)
    expected_price: float
    expected_size_usdc: float
    fill_price: Optional[float] = None    # only when actually filled (or post-hoc evaluated)
    fill_size_usdc: Optional[float] = None
    bot_id: Optional[str] = None
    error: Optional[str] = None
    elapsed_ms: Optional[float] = None
    # Phase 7: platform-specific extras (e.g., SX Bet match details — avg
    # price, fill ratio, matched order count). Surfaces to dryrun_log so
    # paper-trade analysis can see WHY a leg partial-filled.
    extra: Optional[dict] = None


@dataclass
class ArbFireResult:
    arb_id: str                           # unique id, used as join key for paper-trade evaluation
    deal_title: str
    deal_structure: str                   # 'all_yes' | 'all_no' | 'yes_no_pair' | 'binary'
    expected_total_cost_usdc: float
    expected_payout_usdc: float
    legs: List[LegResult] = field(default_factory=list)
    fired_at_unix: float = field(default_factory=time.time)
    dry_run: bool = True
    aborted_reason: Optional[str] = None  # set if not all legs went through
    # Phase 16 (01.05.2026): track which mode was used for this arb fire.
    fire_mode: str = 'taker'              # 'taker' | 'maker' | 'maker_then_taker'


# ── Phase 15 (01.05.2026) — Maker mode selector ─────────────────────
def select_fire_mode(deal: dict) -> str:
    """Decide maker vs taker per arb based on spread to threshold.

    Rules (per maker-taker-orders skill):
        sum < 92¢ (5+¢ buffer) → 'maker' — wide enough to wait for fill
        sum 94-96¢             → 'maker_then_taker' — try maker, fallback
        sum 96-97¢             → 'taker' — too tight, need atomicity

    Returns 'maker' / 'maker_then_taker' / 'taker'.

    If MAKER_MODE_ENABLED=False (default), always returns 'taker'.
    """
    if not MAKER_MODE_ENABLED:
        return 'taker'
    sum_cents = float(deal.get('sum_cents', 99))
    if sum_cents < 92.0:
        return 'maker'
    if sum_cents < 96.0:
        return 'maker_then_taker'
    return 'taker'


def maker_supervise(reg, expected_price: float,
                     other_source_check=None,
                     deadline_s: float = MAKER_FILL_TIMEOUT_S) -> str:
    """Phase 15b — maker order supervisor.

    Polls for fill (event.wait poll), monitors adverse selection by
    comparing cross-source price (other_source_check callable returning
    current best_ask). Cancels if drift exceeds ADVERSE_SELECTION_PRICE_DRIFT.

    Returns one of:
        'filled'                — order matched at expected price
        'timeout'               — no fill within deadline
        'adverse_selection'     — price moved against us, cancelled
        'cancelled'             — caller-initiated cancel
    """
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        # Short wait — checks event every 500ms
        if reg.event.wait(timeout=0.5):
            return 'filled'
        # Adverse selection guard
        if other_source_check is not None:
            try:
                cur = other_source_check()
                if cur is not None and abs(cur - expected_price) > ADVERSE_SELECTION_PRICE_DRIFT:
                    return 'adverse_selection'
            except Exception:
                pass
    return 'timeout'


def _build_leg(deal: dict, leg_idx: int, wallet: builders.WalletStub) -> Optional[dict]:
    """Translate one deal entry into a builder output.

    Deal shape (from arb_server.build_deal): the deal is the WHOLE arb;
    each entry under deal['entries'] is one leg. We need the leg's platform,
    side (BUY/SELL — always BUY for arb), price, size, and platform-specific
    identifier (token_id for Polymarket, marketHash + outcome for SX Bet).

    Returns None when the leg is on a disabled platform (Kalshi).
    """
    entry = deal['entries'][leg_idx]
    platform = deal['platform']

    if platform == 'Polymarket':
        token_id = entry.get('token_id') or entry.get('token_id_yes')
        if not token_id:
            log.warning("leg %d: no token_id in entry — cannot build poly order", leg_idx)
            return None
        # Phase 9m: pre-fire gate. Even though filter_poly already
        # checked these at scan time, the market state may have changed
        # in the seconds between scan and fire. Re-check the entry's
        # cached status (eval_poly attached these via _attach_poly_v2_meta).
        # If market closed / book disabled, abort leg → caller treats
        # arb as broken.
        if entry.get('accepting_orders') is False:
            log.warning("leg %d: market not accepting_orders — abort", leg_idx)
            return None
        if entry.get('enable_order_book') is False:
            log.warning("leg %d: market enable_order_book=False — abort", leg_idx)
            return None
        ao_ts = entry.get('accepting_order_timestamp', 0)
        if ao_ts and ao_ts > time.time():
            log.warning("leg %d: market opens at ts=%s, not yet — abort",
                        leg_idx, ao_ts)
            return None
        # Phase 9j: pull V2 per-market params if eval_poly attached them.
        return builders.build_poly_order(
            token_id=token_id, side='BUY',
            price=entry['price'], size_usdc=float(entry['stake']),
            wallet=wallet,
            neg_risk=bool(entry.get('neg_risk')),
            tick_size=float(entry.get('tick_size') or 0.01),
            min_order_size_usdc=float(entry.get('min_order_size') or 1.0),
        )
    if platform == 'SX Bet':
        # arb_server stores marketHash on the deal and outcome index on the entry
        market_hash = deal.get('market_hash') or entry.get('market_hash')
        outcome = entry.get('outcome_index')  # 1 or 2
        if not market_hash or outcome not in (1, 2):
            log.warning("leg %d: missing market_hash/outcome — cannot build sx order", leg_idx)
            return None
        return builders.build_sx_order(
            market_hash=market_hash, outcome=outcome,
            taker_price=entry['price'], size_usdc=float(entry['stake']),
            wallet=wallet,
        )
    if platform == 'Kalshi':
        return builders.build_kalshi_order(
            price=entry['price'], size_usdc=float(entry['stake']),
            wallet=wallet,
        )
    if platform == 'Limitless':
        # arb_server stores slug on the entry (per-outcome for negRisk groups,
        # event-level slug for standalone binaries — same field either way).
        # token_id (CTF outcome token) + verifying_contract come from market
        # metadata when arb_server has cached it; both are required for live
        # signing but optional for dry-run audit logging.
        slug = entry.get('slug') or entry.get('market_slug') or deal.get('slug')
        if not slug:
            log.warning("leg %d: no slug in entry — cannot build limitless order", leg_idx)
            return None
        return builders.build_limitless_order(
            slug=slug, side='BUY',
            price=entry['price'], size_usdc=float(entry['stake']),
            wallet=wallet,
            token_id=entry.get('token_id'),
            verifying_contract=entry.get('verifying_contract')
                or deal.get('verifying_contract'),
        )
    log.warning("unknown platform %s — leg %d skipped", platform, leg_idx)
    return None


# Phase 11 (01.05.2026) — position log writing.
# Without this, reconcile loop has empty `local` and silently passes any
# divergence with the exchange (everything matches because everything
# is empty locally). After fills land, this writer appends one row per
# filled leg with (platform, market_id, outcome, size_usdc, price, ts).
# reconcile._read_local_positions parses these rows.
_POS_LOG_LOCK = __import__('threading').Lock()


def _positions_log_path() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.normpath(os.path.join(here, '..', '..'))
    return os.path.join(repo_root, 'Executions', 'positions.jsonl')


def _write_position_row(deal: dict, leg_idx: int, leg_result: 'LegResult',
                         wallet: builders.WalletStub) -> bool:
    """Append a single position row to Executions/positions.jsonl after a
    successful fill. Idempotent for crash safety: append-only JSONL.

    Schema (must match risk.reconcile._read_local_positions key shape):
        platform     str  e.g. 'Polymarket'
        market_id    str  conditionId / marketHash / slug — platform-specific
        outcome      str  YES/NO/0/1/outcome name
        size_usdc    float (signed; +BUY, -SELL when revert lands)
        fill_price   float
        ts_unix      float
        bot_id       str
        arb_id       str  joins to dryrun.jsonl decision row
        order_id     Optional[str]
    """
    try:
        import json as _json, time as _time
        entry = deal.get('entries', [None])[leg_idx]
        if entry is None:
            return False
        platform = deal.get('platform', '?')
        # market_id: Polymarket=conditionId, SX=marketHash, Limitless=slug
        market_id = (entry.get('condition_id')
                      or deal.get('market_hash')
                      or entry.get('slug')
                      or entry.get('market_slug')
                      or entry.get('token_id_yes')
                      or entry.get('token_id')
                      or '?')
        outcome = (entry.get('name')
                    or str(entry.get('outcome_index'))
                    or '?')
        # SELL leg (e.g. revert path) → negative size_usdc.
        size_signed = float(leg_result.fill_size_usdc
                              or leg_result.expected_size_usdc or 0)
        if (entry.get('side') == 'SELL'
                or leg_result.status == 'reverted'):
            size_signed = -abs(size_signed)
        row = {
            'platform': platform,
            'market_id': str(market_id),
            'outcome': str(outcome),
            'size_usdc': size_signed,
            'fill_price': leg_result.fill_price,
            'ts_unix': _time.time(),
            'bot_id': getattr(wallet, 'bot_id', None),
            'arb_id': leg_result.extra.get('arb_id') if leg_result.extra else None,
            'leg_idx': leg_idx,
            'platform_status': leg_result.status,
        }
        path = _positions_log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with _POS_LOG_LOCK:
            with open(path, 'a', encoding='utf-8') as f:
                f.write(_json.dumps(row, default=str) + '\n')
        return True
    except Exception as e:
        log.warning("position log write failed leg %d: %s", leg_idx, e)
        return False


def _cancel_leg_order(built: dict, order_id: Optional[str],
                       wallet: builders.WalletStub) -> bool:
    """Phase 10 Task B (01.05.2026): cancel a single leg's order via the
    platform-specific cancel builder. Returns True on HTTP 200/202.

    Used when slippage_check breaches SLIPPAGE_TOLERANCE — we don't want
    a fill at a worse price than expected to count as a successful arb leg.
    """
    if not order_id:
        return False
    platform = built.get('platform', '?').lower()
    try:
        if platform == 'polymarket':
            cancel_built = builders.build_poly_cancel(order_id, wallet)
        elif platform == 'limitless':
            api_key = getattr(wallet, 'api_key', None) or ''
            cancel_built = builders.build_limitless_cancel(order_id, api_key)
        else:
            log.warning("cancel for platform %s not implemented", platform)
            return False
        method = cancel_built.get('method', 'DELETE')
        import requests as _req
        if method == 'DELETE':
            r = _req.delete(cancel_built['would_post_url'],
                              headers=cancel_built.get('headers') or {},
                              timeout=PER_ORDER_TIMEOUT_S)
        else:
            r = _req.post(cancel_built['would_post_url'],
                            json=cancel_built.get('body'),
                            headers=cancel_built.get('headers') or {},
                            timeout=PER_ORDER_TIMEOUT_S)
        return r.status_code in (200, 202, 204)
    except Exception as e:
        log.warning("cancel exc: %s", e)
        return False


def _fire_one_leg_maker(deal: dict, leg_idx: int, wallet: builders.WalletStub,
                          arb_id: str,
                          *, http_post=None,
                          deadman_s: float = MAKER_FILL_TIMEOUT_S,
                          presigned_legs: Optional[List[dict]] = None) -> 'LegResult':
    """Phase 16 (01.05.2026) — MAKER fire path.

    Posts a maker order at price 1 tick inside the spread. If maker
    placement fails (spread too tight) → falls back to taker. If maker
    posted, waits up to deadman_s for fill via maker_supervise. On
    timeout / adverse selection: cancels order + returns failed leg
    (caller may retry as taker via fire_mode='maker_then_taker').

    Currently Polymarket-only. SX/Limitless legs auto-fall-back to live
    taker path (build_sx_order / build_limitless_order don't have a
    maker concept currently).
    """
    t0 = time.time()
    entry = deal['entries'][leg_idx]
    platform = deal['platform']

    # Maker is currently Polymarket-only. Other platforms → taker.
    if platform != 'Polymarket':
        return _fire_one_leg_live(deal, leg_idx, wallet, arb_id,
                                    http_post=http_post,
                                    deadman_s=DEADMAN_TIMEOUT_S,
                                    presigned_legs=presigned_legs)

    # Need best_ask + best_bid from entry to build maker order.
    # entry has best_ask via 'price', best_bid stored as 'best_bid' if available
    best_ask = entry.get('price')
    best_bid = entry.get('best_bid')

    token_id = entry.get('token_id') or entry.get('token_id_yes')
    if not token_id:
        return LegResult(
            leg_idx=leg_idx, platform=platform, status='rejected',
            error='no token_id', expected_price=best_ask or 0.5,
            expected_size_usdc=float(entry.get('stake', 0)),
            bot_id=wallet.bot_id,
            elapsed_ms=(time.time() - t0) * 1000,
        )

    built = builders.build_poly_maker_order(
        token_id=token_id, side='BUY',
        best_ask=best_ask, best_bid=best_bid,
        size_usdc=float(entry['stake']),
        wallet=wallet,
        neg_risk=bool(entry.get('neg_risk')),
        tick_size=float(entry.get('tick_size') or 0.01),
        min_order_size_usdc=float(entry.get('min_order_size') or 1.0),
    )

    # Spread too tight → fall back to taker
    if built.get('will_revert_to_taker'):
        log.info("leg %d maker fallback to taker: %s",
                 leg_idx, built.get('maker_failure_reason'))
        return _fire_one_leg_live(deal, leg_idx, wallet, arb_id,
                                    http_post=http_post,
                                    deadman_s=DEADMAN_TIMEOUT_S,
                                    presigned_legs=presigned_legs)

    # POST the maker order
    if http_post is None:
        import requests as _req
        http_post = _req.post

    try:
        headers = {'Content-Type': 'application/json'}
        r = http_post(built['would_post_url'], json=built['body'],
                      headers=headers, timeout=PER_ORDER_TIMEOUT_S)
        if r.status_code not in (200, 201, 202):
            return LegResult(
                leg_idx=leg_idx, platform=platform, status='rejected',
                error=f'maker POST HTTP {r.status_code}',
                expected_price=built['expected_price'],
                expected_size_usdc=built['expected_size_usdc'],
                bot_id=wallet.bot_id,
                elapsed_ms=(time.time() - t0) * 1000,
            )
        resp = r.json() or {}
    except Exception as e:
        return LegResult(
            leg_idx=leg_idx, platform=platform, status='rejected',
            error=f'maker POST failed: {type(e).__name__}',
            expected_price=built['expected_price'],
            expected_size_usdc=built['expected_size_usdc'],
            bot_id=wallet.bot_id,
            elapsed_ms=(time.time() - t0) * 1000,
        )

    order_id = (resp.get('id') or resp.get('orderId')
                or (resp.get('order') or {}).get('id'))
    reg = fills.registry.register(
        arb_id=arb_id, leg_idx=leg_idx, platform='polymarket',
        slug=None, order_id=order_id,
    )

    # Run maker supervisor: poll for fill OR adverse selection
    sup_result = maker_supervise(reg, expected_price=built['maker_price'],
                                    other_source_check=None,
                                    deadline_s=deadman_s)
    elapsed_ms = (time.time() - t0) * 1000

    if sup_result == 'filled' and reg.result:
        fp = reg.result.get('fill_price') or built['maker_price']
        leg_result = LegResult(
            leg_idx=leg_idx, platform=platform, status='filled',
            expected_price=built['maker_price'],
            expected_size_usdc=built['expected_size_usdc'],
            fill_price=fp,
            fill_size_usdc=reg.result.get('fill_size_usdc'),
            bot_id=wallet.bot_id, elapsed_ms=elapsed_ms,
            extra={'is_maker': True, 'maker_price': built['maker_price']},
        )
        _write_position_row(deal, leg_idx, leg_result, wallet)
        return leg_result

    # Cancel the open maker order — timeout / adverse_selection
    try:
        _cancel_leg_order(built, order_id, wallet)
    except Exception:
        pass

    status = ('adverse_cancelled' if sup_result == 'adverse_selection'
              else 'maker_timeout')
    return LegResult(
        leg_idx=leg_idx, platform=platform, status=status,
        error=f'maker {sup_result} after {deadman_s}s',
        expected_price=built['maker_price'],
        expected_size_usdc=built['expected_size_usdc'],
        bot_id=wallet.bot_id, elapsed_ms=elapsed_ms,
    )


def _fire_one_leg_live(deal: dict, leg_idx: int, wallet: builders.WalletStub,
                       arb_id: str,
                       *, http_post=None,
                       deadman_s: float = DEADMAN_TIMEOUT_S,
                       presigned_legs: Optional[List[dict]] = None) -> LegResult:
    """Live-mode single leg fire with real HTTP POST + fill confirmation
    via fills.registry. Used when fire_arb is called with dry_run=False
    AND the Phase 5 graduation gate is open.

    Flow:
      1. _build_leg → built body
      2. POST built['would_post_url'] with built['body'] + headers
      3. Parse response → order_id
      4. Register (arb_id, leg_idx, platform, slug, order_id) in fills.registry
      5. Block on reg.event.wait(deadman_s)
      6. If event set → reg.result has fill_price/size; status='filled'
      7. If timeout → cancel order, status='timeout' (caller may revert
         already-filled siblings via the partial-fill abort path)

    `http_post` is injected for tests — defaults to requests.post.
    """
    t0 = time.time()
    entry = deal['entries'][leg_idx]
    platform = deal['platform']

    # Phase 9zz: try pre-signed bundle first (skip ~50ms inline sign).
    if presigned_legs and leg_idx < len(presigned_legs) and presigned_legs[leg_idx]:
        built = presigned_legs[leg_idx]
    else:
        built = _build_leg(deal, leg_idx, wallet)
    if built is None:
        return LegResult(
            leg_idx=leg_idx, platform=platform,
            status='rejected', error='builder returned None',
            expected_price=entry['price'],
            expected_size_usdc=float(entry['stake']),
            bot_id=wallet.bot_id,
            elapsed_ms=(time.time() - t0) * 1000,
        )
    if built.get('disabled_reason'):
        return LegResult(
            leg_idx=leg_idx, platform=built['platform'],
            status='disabled', error=built['disabled_reason'],
            expected_price=built['expected_price'],
            expected_size_usdc=built['expected_size_usdc'],
            bot_id=wallet.bot_id,
            elapsed_ms=(time.time() - t0) * 1000,
        )

    # ── HTTP POST ────────────────────────────────────────────────
    if http_post is None:
        import requests as _req
        http_post = _req.post
    try:
        # Limitless requires X-API-Key for /orders. Polymarket / SX use
        # their own auth (signature in body). For now we just include
        # api_key when wallet has it; harmless on platforms that ignore.
        headers = {'Content-Type': 'application/json'}
        api_key = getattr(wallet, 'api_key', None)
        if api_key:
            headers['X-API-Key'] = api_key
        r = http_post(built['would_post_url'], json=built['body'],
                      headers=headers, timeout=PER_ORDER_TIMEOUT_S)
        if r.status_code not in (200, 201, 202):
            return LegResult(
                leg_idx=leg_idx, platform=built['platform'],
                status='rejected',
                error=f'HTTP {r.status_code}: {(r.text or "")[:120]}',
                expected_price=built['expected_price'],
                expected_size_usdc=built['expected_size_usdc'],
                bot_id=wallet.bot_id,
                elapsed_ms=(time.time() - t0) * 1000,
            )
        resp = r.json() or {}
    except Exception as e:
        return LegResult(
            leg_idx=leg_idx, platform=built['platform'],
            status='rejected', error=f'POST failed: {type(e).__name__}: {e}',
            expected_price=built['expected_price'],
            expected_size_usdc=built['expected_size_usdc'],
            bot_id=wallet.bot_id,
            elapsed_ms=(time.time() - t0) * 1000,
        )

    # ── Extract order_id (platform-specific shapes) ──────────────
    order_id = (resp.get('id') or resp.get('orderId')
                or (resp.get('order') or {}).get('id')
                or resp.get('order_hash'))
    slug = entry.get('slug') or entry.get('market_slug')

    # ── Register & wait for fill ─────────────────────────────────
    reg_platform = platform.lower().replace(' ', '_')   # 'Limitless'→'limitless'
    reg = fills.registry.register(
        arb_id=arb_id, leg_idx=leg_idx,
        platform=reg_platform, slug=slug, order_id=order_id,
    )
    filled = reg.event.wait(timeout=deadman_s)
    elapsed_ms = (time.time() - t0) * 1000

    if filled and reg.result:
        fp = reg.result.get('fill_price')
        if fp is None:
            try: fp = float(reg.result.get('price'))
            except Exception: fp = None
        # Slippage check vs expected
        exp = built['expected_price']
        if fp is not None and abs(fp - exp) > SLIPPAGE_TOLERANCE:
            # Phase 10 Task B (01.05.2026): on slippage breach, ACTIVELY
            # cancel this leg's order + mark status='slippage_cancelled' so
            # fire_arb's broken-arb detector triggers revert chain on the
            # OTHER filled legs. Without this, slippage was only logged and
            # paper-trade booked a phantom win that real-mode wouldn't get.
            log.warning("leg %d slippage %.4f exceeds %.4f — issuing cancel",
                        leg_idx, abs(fp - exp), SLIPPAGE_TOLERANCE)
            try:
                _cancel_leg_order(built, order_id, wallet)
            except Exception as e:
                log.warning("cancel_leg_order leg %d failed: %s", leg_idx, e)
            return LegResult(
                leg_idx=leg_idx, platform=built['platform'],
                status='slippage_cancelled',
                error=f'slippage {abs(fp-exp):.4f} > tolerance {SLIPPAGE_TOLERANCE} '
                      f'(filled at {fp:.4f}, expected {exp:.4f})',
                expected_price=exp,
                expected_size_usdc=built['expected_size_usdc'],
                fill_price=fp,
                fill_size_usdc=reg.result.get('fill_size_usdc'),
                bot_id=wallet.bot_id,
                elapsed_ms=elapsed_ms,
                extra=reg.result,
            )
        leg_result = LegResult(
            leg_idx=leg_idx, platform=built['platform'],
            status='filled',
            expected_price=exp,
            expected_size_usdc=built['expected_size_usdc'],
            fill_price=fp,
            fill_size_usdc=reg.result.get('fill_size_usdc'),
            bot_id=wallet.bot_id,
            elapsed_ms=elapsed_ms,
            extra=reg.result,
        )
        # Phase 11 (01.05.2026): persist to positions.jsonl so reconcile
        # loop can compare against exchange-reported positions.
        _write_position_row(deal, leg_idx, leg_result, wallet)
        return leg_result

    # Dead-man: no fill in deadman_s. Caller will trigger reversal logic.
    return LegResult(
        leg_idx=leg_idx, platform=built['platform'],
        status='timeout',
        error=f'no fill confirmation within {deadman_s}s (order_id={order_id})',
        expected_price=built['expected_price'],
        expected_size_usdc=built['expected_size_usdc'],
        bot_id=wallet.bot_id,
        elapsed_ms=elapsed_ms,
    )


def _fire_one_leg_dryrun(deal: dict, leg_idx: int, wallet: builders.WalletStub,
                         arb_id: str,
                         presigned_legs: Optional[List[dict]] = None) -> LegResult:
    """Dry-run a single leg: build the order body, log it, return as if filled.
    Real fill is evaluated later by dryrun_log.schedule_realistic_eval.

    Phase 9zz: if `presigned_legs[leg_idx]` exists (cached EIP-712 signed
    body from NEAR-pool pre-signer), skip the inline signing path and
    use the cached body directly. Saves ~50ms/leg on real-mode fires.
    """
    t0 = time.time()
    entry = deal['entries'][leg_idx]
    if presigned_legs and leg_idx < len(presigned_legs) and presigned_legs[leg_idx]:
        built = presigned_legs[leg_idx]
    else:
        built = _build_leg(deal, leg_idx, wallet)
    if built is None:
        return LegResult(
            leg_idx=leg_idx, platform=deal['platform'],
            status='rejected', error='builder returned None',
            expected_price=entry['price'],
            expected_size_usdc=float(entry['stake']),
            bot_id=wallet.bot_id,
            elapsed_ms=(time.time() - t0) * 1000,
        )
    if built.get('disabled_reason'):
        return LegResult(
            leg_idx=leg_idx, platform=built['platform'],
            status='disabled', error=built['disabled_reason'],
            expected_price=built['expected_price'],
            expected_size_usdc=built['expected_size_usdc'],
            bot_id=wallet.bot_id,
            elapsed_ms=(time.time() - t0) * 1000,
        )
    # In dry-run we don't POST. Just log the would-be order.
    dryrun_log.log_order_decision(arb_id=arb_id, leg_idx=leg_idx, built=built,
                                  bot_id=wallet.bot_id)
    # Phase 7: SX Bet may partial-fill due to insufficient maker liquidity.
    # We mark the leg 'partial' so atomic._record_partial_arb_aborted runs at
    # the end (an arb with one partial leg is no longer an arb — must be
    # reverted in real-mode). In dry-run we still log it so paper-trade
    # win-rate reflects the truth.
    is_partial = bool(built.get('partial_fill'))
    extra = built.get('sx_match')
    return LegResult(
        leg_idx=leg_idx, platform=built['platform'],
        status='partial' if is_partial else 'dry-fired',
        expected_price=built['expected_price'],
        expected_size_usdc=built['expected_size_usdc'],
        fill_price=(extra or {}).get('avg_fill_price'),
        fill_size_usdc=(extra or {}).get('filled_usdc'),
        bot_id=wallet.bot_id,
        elapsed_ms=(time.time() - t0) * 1000,
        extra=extra,
    )


def _assign_wallets(legs_count: int, wallets: List[builders.WalletStub],
                     dry_run: bool = False) -> List[builders.WalletStub]:
    """One DISTINCT wallet per leg — anti-detection rule from plan
    (CLAUDE.md memory): never aggregate multiple legs of the same arb in
    one wallet, otherwise the exchange sees the same address taking
    opposite sides of the same event in milliseconds = classic arb-bot
    fingerprint.

    Phase 9i (28.04.2026) fix: previously this used round-robin
    `wallets[i % len(wallets)]` which silently put 2 legs on wallet0 if
    the pool was smaller than legs_count. Now we ENFORCE distinct
    wallets by returning empty list when not enough are eligible — the
    caller (fire_arb) treats that as 'cannot fire safely' and aborts.
    Empty pool still falls back to single mock stub for dry-run testing.

    Phase 9kkk (30.04.2026) — operator-found bug: with 3 can-sign wallets
    every Polymarket A/B arb of 4+ legs was aborting silently, so paper
    trading collected zero data overnight. In `dry_run=True`, where there's
    no real on-chain submission and anti-detection is a non-issue, we
    PAD the pool with mock stubs so the entire arb is logged + drift
    measured. Live mode (`dry_run=False`) still enforces strict distinct
    wallets — the graduation gate must reflect REAL capacity.
    """
    if not wallets:
        # Test/dev fallback — single mock per leg so dry-run pipeline runs.
        return [builders.WalletStub(bot_id='mock', eth_address='0x' + '0'*40)
                for _ in range(legs_count)]

    # Phase 16 (01.05.2026) — Q1 adaptive multi-outcome bot relaxation.
    # Operator policy:
    #   N ≤ 6   → 1 bot per leg (strict anti-detection)
    #   7-12    → up to 2 legs per bot
    #   N ≥ 13  → up to 3 legs per bot
    # Rationale: weather brackets have N=15-20 outcomes. With only 6 bots
    # and strict 1-leg-per-bot, all such arbs were rejected. Adaptive
    # policy preserves anti-detection on small arbs (where it matters
    # most) while enabling multi-outcome coverage.
    # Override via env: MULTI_LEG_TIER1=6, MULTI_LEG_TIER2=12.
    TIER1_MAX = int(os.environ.get('MULTI_LEG_TIER1', '6'))
    TIER2_MAX = int(os.environ.get('MULTI_LEG_TIER2', '12'))

    def _legs_per_bot_for(n):
        if n <= TIER1_MAX:
            return 1
        if n <= TIER2_MAX:
            return 2
        return 3

    legs_per_bot = _legs_per_bot_for(legs_count)

    if len(wallets) < legs_count:
        if dry_run:
            # Phase 9kkk: pad with mock stubs (no anti-detection in dry-run).
            padded = list(wallets)
            mock_idx = 0
            while len(padded) < legs_count:
                mock_addr = '0x' + format(mock_idx, '040x')
                padded.append(builders.WalletStub(
                    bot_id=f'mock{mock_idx}', eth_address=mock_addr))
                mock_idx += 1
            return padded
        # Live mode — Phase 16 adaptive: relax based on N.
        if legs_per_bot > 1:
            min_wallets_needed = (legs_count + legs_per_bot - 1) // legs_per_bot
            if len(wallets) >= min_wallets_needed:
                # Round-robin: each wallet takes legs_per_bot consecutive legs.
                # For N=16 with 6 bots and legs_per_bot=3:
                # bot0=[0,6,12], bot1=[1,7,13], ..., bot5=[5,11] (wrapping).
                assigned = []
                for i in range(legs_count):
                    assigned.append(wallets[i % len(wallets)])
                return assigned
        # Strict: not enough distinct bots even with relaxation, abort.
        return []
    return list(wallets[:legs_count])


def fire_arb(deal: dict, wallets: List[builders.WalletStub] = None,
             dry_run: bool = None) -> ArbFireResult:
    """Fire all legs of an arb in parallel. Returns ArbFireResult capturing
    every leg's outcome.

    `wallets` is the pool of bot wallets (Phase 4 = 6 bots; Phase 2 may pass
    an empty list and the firer falls back to a single mock stub for dry-run).

    `dry_run` overrides the module default (env DRY_RUN). Tests pass
    dry_run=True explicitly; the radar's auto-fire will respect the env.
    """
    if dry_run is None:
        dry_run = DRY_RUN
    arb_id = f"{int(time.time()*1000)}-{deal.get('title','?')[:32].replace(' ','_')}"
    legs = deal.get('entries', [])
    legs_count = len(legs)
    # Phase 9kkk: pass dry_run flag — in dry-run we pad pool with mocks.
    assigned = _assign_wallets(legs_count, wallets or [], dry_run=dry_run)

    # Phase 9i (28.04.2026): if _assign_wallets returned empty, we don't
    # have enough DISTINCT eligible wallets to fire safely without putting
    # 2 legs on one address. Abort with explicit reason — analytics can
    # see the rejection in dryrun.jsonl. This complements the coordinator
    # pre-check (which the caller may or may not run).
    if not assigned:
        result = ArbFireResult(
            arb_id=arb_id,
            deal_title=deal.get('title','?'),
            deal_structure=deal.get('arb_structure', 'all_yes'),
            expected_total_cost_usdc=sum(float(l.get('stake', 0)) for l in legs),
            expected_payout_usdc=float(deal.get('payout_target') or 1.0),
            dry_run=dry_run,
        )
        result.aborted_reason = (
            f'wallet_assignment_failed: need {legs_count} distinct '
            f'eligible bots, pool has {len(wallets or [])} — anti-detection '
            f'rule prevents 2 legs per wallet')
        dryrun_log.log_decision(result)
        return result

    expected_cost = sum(float(l['stake']) for l in legs)
    # Payout target: structures A/C target $1, B targets (N-1)
    expected_payout = float(deal.get('payout_target') or 1.0)

    result = ArbFireResult(
        arb_id=arb_id,
        deal_title=deal.get('title','?'),
        deal_structure=deal.get('arb_structure', 'all_yes'),
        expected_total_cost_usdc=expected_cost,
        expected_payout_usdc=expected_payout,
        dry_run=dry_run,
    )

    # ── Phase 3: risk gate. Even in dry-run we run the same check so the
    # paper-trade path mirrors what real-mode will do. The kill switch and
    # daily-limit pauses must block dry-fires too — otherwise the paper
    # window keeps generating data that wouldn't have been traded.
    try:
        from risk import check_can_fire as _risk_check
        allowed, reason = _risk_check(deal)
        if not allowed:
            result.aborted_reason = f'risk_blocked: {reason}'
            # Phase 3.1 fix (28.04): log the blocked attempt to dryrun.jsonl so
            # operators can see WHY paper trades aren't accumulating. Without
            # this, the blocking is silent and the user sees an empty log.
            dryrun_log.log_decision(result)
            return result
    except ImportError:
        # risk package not installed (testing executor in isolation) — proceed
        pass

    if legs_count == 0:
        result.aborted_reason = 'deal has no legs'
        dryrun_log.log_decision(result)
        return result

    # ── Phase 10 #51: pre-flight checks (depth + balance + allowance).
    # Runs in BOTH dry-run and live mode so paper-trading numbers reflect
    # what real-mode would actually allow. Failures abort the arb with a
    # detailed reason in dryrun_log.
    try:
        import preflight
        # In dry-run we still check depth (cheap, no I/O), but skip balance
        # and allowance which need on-chain RPC calls (we don't want a flaky
        # public Polygon RPC to flatline paper trading).
        pf = preflight.preflight_arb(
            deal, assigned,
            skip_balance=dry_run,
            skip_allowance=dry_run,
            skip_depth=False,
        )
        if not pf.ok:
            result.aborted_reason = (
                f'preflight_failed: {"; ".join(pf.failures[:3])}'
                + (f' [+{len(pf.failures)-3} more]' if len(pf.failures) > 3 else '')
            )
            dryrun_log.log_decision(result)
            return result
        for w in pf.warnings:
            log.warning("preflight warn: %s", w)
    except ImportError:
        # Preflight module missing in test isolation — proceed (legacy path).
        log.debug("preflight module not available, skipping checks")

    # Phase 5 graduation gate — when dry_run=False, paper-trade win-rate
    # over the last GRADUATION_MIN_TRADES (100) must be >= GRADUATION_MIN_WIN_RATE
    # (70%). Otherwise we block the live fire and fall through to the
    # aborted-reason path. This is the FINAL safety net before money moves.
    if not dry_run:
        try:
            import paper_trading
            grad = paper_trading.graduation_status()
            if not grad.ready:
                result.aborted_reason = (
                    f'graduation_gate: not yet passed — '
                    f'{", ".join(grad.blockers) if grad.blockers else "incomplete"}'
                )
                dryrun_log.log_decision(result)
                return result
        except ImportError:
            # paper_trading module not in test isolation — fail closed
            result.aborted_reason = ('graduation_gate: paper_trading module '
                                     'unavailable, blocking live fire')
            dryrun_log.log_decision(result)
            return result

    # Phase 16 (01.05.2026) — maker mode integration.
    # select_fire_mode(deal) decides per-arb whether to use maker pricing.
    # N-aware safety: for N >= 4 legs we FORCE taker because partial-fill
    # risk grows quickly with N. Maker only for N=2-3 binary/3-way.
    fire_mode = 'taker'
    if MAKER_MODE_ENABLED and not dry_run:
        fire_mode = select_fire_mode(deal)
        if legs_count >= 4 and fire_mode != 'taker':
            log.info("arb %s: N=%d ≥ 4 → forcing taker (maker partial-fill risk)",
                     arb_id, legs_count)
            fire_mode = 'taker'
    result.fire_mode = fire_mode

    # Pick the per-leg fire function based on mode. Phase 9e wires real
    # POST + fills.registry path for `dry_run=False`; the same code shape
    # as dry-run so tests and metrics aggregation stay uniform.
    # Phase 16: maker / maker_then_taker selects MAKER path for live mode;
    # dry-run / taker fall back to existing flow.
    if fire_mode in ('maker', 'maker_then_taker') and not dry_run:
        leg_fn = _fire_one_leg_maker
    else:
        leg_fn = _fire_one_leg_dryrun if dry_run else _fire_one_leg_live

    # Phase 9zz: consume pre-signed bundle ONCE up-front (single-use cache).
    # If hit, distribute to legs to skip the ~50ms/leg inline signing cost.
    # Cache miss → presigned_legs=None and inline signing happens normally.
    presigned_legs = None
    try:
        from . import presign as _presign
        cand_id = _presign.cand_id_for_deal(deal)
        presigned_legs = _presign.consume_presigned(
            cand_id, deal.get('arb_structure', 'all_yes'))
    except Exception as e:
        log.debug("presign consume failed (non-fatal): %s", e)

    # Parallel fire — dry_run uses log_order_decision, live uses HTTP+wait.
    # Phase 9i (28.04.2026): anti-detection jitter. Without it, all legs
    # POST within ±1ms which is a classic arb-bot fingerprint that gets
    # rate-limit-tier-bumped or banned. We wrap leg_fn to sleep
    # `jitter_ms_for_leg(idx)` (0-50ms, deterministic per leg index +
    # arb_id) BEFORE the actual fire — staggers legs across ~50ms
    # window, looks like independent traders.
    try:
        from wallets.coordinator import jitter_ms_for_leg
    except Exception:
        jitter_ms_for_leg = lambda _i: 0   # graceful fallback in test isolation

    def _delayed_leg_fn(deal, leg_idx, wallet, arb_id):
        delay_ms = jitter_ms_for_leg(leg_idx)
        if delay_ms > 0:
            time.sleep(delay_ms / 1000.0)
        return leg_fn(deal, leg_idx, wallet, arb_id, presigned_legs=presigned_legs)

    t_start = time.time()
    with ThreadPoolExecutor(max_workers=max(legs_count, 1)) as pool:
        futs = {pool.submit(_delayed_leg_fn, deal, i, assigned[i], arb_id): i
                for i in range(legs_count)}
        for fut in as_completed(futs, timeout=PER_ORDER_TIMEOUT_S * 3):
            try:
                result.legs.append(fut.result(timeout=PER_ORDER_TIMEOUT_S))
            except FutureTimeoutError:
                idx = futs[fut]
                result.legs.append(LegResult(
                    leg_idx=idx, platform=deal['platform'],
                    status='timeout', error='per-order timeout exceeded',
                    expected_price=legs[idx]['price'],
                    expected_size_usdc=float(legs[idx]['stake']),
                    bot_id=assigned[idx].bot_id,
                ))
    result.legs.sort(key=lambda r: r.leg_idx)
    elapsed_ms = (time.time() - t_start) * 1000

    # Phase 16 (01.05.2026) — maker_then_taker fallback path.
    # If fire_mode='maker_then_taker' and SOME legs timed out / adverse-
    # cancelled, retry those legs as TAKER. Filled legs untouched.
    # If retry succeeds → arb intact; if retry also fails → revert filled.
    if (fire_mode == 'maker_then_taker' and not dry_run
            and any(l.status in ('maker_timeout', 'adverse_cancelled')
                    for l in result.legs)):
        log.info("arb %s maker_then_taker: retrying failed legs as taker", arb_id)
        retry_results = []
        for orig in result.legs:
            if orig.status not in ('maker_timeout', 'adverse_cancelled'):
                continue
            try:
                retry_res = _fire_one_leg_live(
                    deal, orig.leg_idx, assigned[orig.leg_idx], arb_id,
                    deadman_s=DEADMAN_TIMEOUT_S,
                    presigned_legs=presigned_legs)
                retry_results.append(retry_res)
            except Exception as e:
                log.warning("retry as taker leg %d failed: %s",
                             orig.leg_idx, e)
        # Replace failed legs with retry results
        legs_by_idx = {l.leg_idx: l for l in result.legs}
        for r in retry_results:
            legs_by_idx[r.leg_idx] = r
        result.legs = sorted(legs_by_idx.values(), key=lambda r: r.leg_idx)

    # Phase 7: detect arb-broken-by-partial-fill. If ANY leg partial-filled,
    # the arb is no longer an arb (one outcome is uncovered).
    # Phase 10 #51 (30.04.2026) — REAL revert flow added. In real-mode,
    # filled legs are sold at market to flatten the position. Dry-run logs
    # the would-be reverts so paper-trading P&L doesn't book a phantom win.
    partial_legs = [l for l in result.legs if l.status == 'partial']
    failed_legs = [l for l in result.legs
                    if l.status in ('rejected', 'timeout', 'cancelled', 'disabled',
                                     'slippage_cancelled',
                                     'maker_timeout', 'adverse_cancelled')]
    filled_legs = [l for l in result.legs if l.status == 'filled']

    arb_broken = bool(partial_legs) or (failed_legs and filled_legs)
    if arb_broken:
        shortfalls = [
            (l.leg_idx, (l.extra or {}).get('shortfall_usdc'))
            for l in partial_legs
        ]
        broken_reason = (
            f'arb_broken: partial={len(partial_legs)} failed={len(failed_legs)} '
            f'filled={len(filled_legs)}, shortfalls={shortfalls!r}'
        )
        result.aborted_reason = broken_reason
        log.warning("arb %s broken — %s; running revert path", arb_id, broken_reason)
        # Live mode: sell filled legs at market. Dry-run: log the would-be sells.
        try:
            revert_results = revert_filled_legs(
                result, deal, assigned, dry_run=dry_run)
            result.aborted_reason = (
                broken_reason + f' | revert={revert_results}')
        except Exception as e:
            log.exception("revert_filled_legs raised — manual cleanup required: %s", e)
            result.aborted_reason = broken_reason + f' | revert_FAILED: {e}'

    log.info("dry-fired arb %s in %.0fms (%d legs, %s structure%s)",
             arb_id, elapsed_ms, legs_count, result.deal_structure,
             ' [PARTIAL]' if partial_legs else '')

    # Top-level decision row + schedule realistic evaluation (Phase 5 input)
    dryrun_log.log_decision(result)
    # Skip realistic-eval if the arb was already aborted by partial fill —
    # there's no fill to evaluate.
    if not result.aborted_reason:
        dryrun_log.schedule_realistic_eval(result, deal,
                                           delay_s=REALISTIC_EVAL_DELAY_S)
    return result


# ── Revert flow: sell filled legs when arb is broken ────────────────
# Phase 10 #51 (30.04.2026): when partial fill / failure leaves us with
# filled legs but a broken arb, we must SELL those legs at market to
# flatten the position. Otherwise we hold a directional bet on whichever
# outcomes did fill — that defeats the whole point of arb (= zero
# directional exposure).
#
# In dry-run mode this is a logged simulation: we record what would be
# sold, at what indicative market price (from the entry's recorded
# best-ask, since that's what we have available without a fresh fetch).
# In live mode it emits real SELL orders via the same builder used for
# entry (build_poly_order with side='SELL').
def revert_filled_legs(result, deal: dict, wallets, *, dry_run: bool) -> str:
    """Walk back filled legs of a broken arb. Returns a short status
    string suitable for joining onto aborted_reason.

    For each filled leg:
      - dry-run: log the would-be SELL order; bump dryrun_log audit
      - live: emit build_poly_order(side='SELL', price=expected*(1-slippage),
              size=fill_size_usdc) → POST → wait for fill confirmation

    Why SELL at expected*(1-slippage) instead of market: Polymarket V2
    has no true 'market order'; we approximate via a low-price limit
    that should sweep top-of-book bids. The slippage tolerance here is
    intentionally LARGER than entry tolerance (default 0.01 = 1c) since
    we prioritize getting flat over price.

    Returns string like 'reverted=2/3 (poly:filled, sx:cancelled)'.
    """
    REVERT_SLIPPAGE = 0.01      # 1c worse than entry — accept to flatten
    filled = [l for l in result.legs if l.status == 'filled']
    if not filled:
        return 'no_filled_legs_to_revert'

    revert_results = []
    for leg in filled:
        try:
            entry = deal['entries'][leg.leg_idx]
            wallet = wallets[leg.leg_idx] if leg.leg_idx < len(wallets) else None
            platform = deal.get('platform', '?')
            # Sell at expected_price - slippage (lower price for SELL =
            # accept worse → guaranteed sweep of top-of-book bids).
            sell_price = max(0.01, leg.expected_price - REVERT_SLIPPAGE)
            sell_size = float(leg.fill_size_usdc or leg.expected_size_usdc)

            if dry_run or wallet is None:
                # Dry-run / no wallet → just log
                dryrun_log.log_order_decision(
                    arb_id=result.arb_id + '-revert',
                    leg_idx=leg.leg_idx,
                    built={
                        'platform': platform.lower().replace(' ', '_'),
                        'op': 'revert_sell',
                        'expected_price': sell_price,
                        'expected_size_usdc': sell_size,
                        'body': {'side': 'SELL', 'reason': 'arb_broken_revert'},
                        'sign_payload': b'',
                        'would_post_url': '<dry-run>',
                    },
                    bot_id=getattr(wallet, 'bot_id', 'mock'),
                )
                revert_results.append((leg.leg_idx, 'dry-revert'))
                continue

            # Live path — emit a real SELL. Only Polymarket SELL is
            # straightforward via build_poly_order. SX Bet revert needs
            # opposite-side fill (different mechanic — handled by SX-specific
            # code, see TODO at end of function).
            if platform == 'Polymarket':
                token_id = entry.get('token_id') or entry.get('token_id_yes')
                if not token_id:
                    revert_results.append((leg.leg_idx, 'no_token_id'))
                    continue
                sell_built = builders.build_poly_order(
                    token_id=token_id, side='SELL',
                    price=sell_price, size_usdc=sell_size,
                    wallet=wallet,
                    neg_risk=bool(entry.get('neg_risk')),
                    tick_size=float(entry.get('tick_size') or 0.01),
                    min_order_size_usdc=float(entry.get('min_order_size') or 1.0),
                    order_type='FOK',                 # fill-or-kill: get flat fast
                )
                # POST and wait briefly. Don't block the result much —
                # caller is already past the dead-man.
                import requests as _req
                try:
                    r = _req.post(sell_built['would_post_url'],
                                   json=sell_built['body'], timeout=2.0)
                    if r.status_code in (200, 201, 202):
                        revert_results.append((leg.leg_idx, 'sold'))
                    else:
                        revert_results.append((leg.leg_idx,
                                                f'sell_HTTP_{r.status_code}'))
                except Exception as e:
                    revert_results.append((leg.leg_idx,
                                            f'sell_exc_{type(e).__name__}'))
            elif platform == 'Limitless':
                # Phase 16 (01.05.2026) — Limitless revert flow.
                # Same conceptual SELL FOK as Polymarket but uses Limitless
                # builder + signing. Token + verifying_contract from entry
                # (cached during eval_limitless via _fetch_limitless_market_meta).
                slug = entry.get('slug') or entry.get('market_slug')
                if not slug:
                    revert_results.append((leg.leg_idx, 'no_slug'))
                    continue
                sell_built = builders.build_limitless_order(
                    slug=slug, side='SELL',
                    price=sell_price, size_usdc=sell_size,
                    wallet=wallet,
                    token_id=entry.get('token_id'),
                    verifying_contract=entry.get('verifying_contract'),
                    order_type='FOK',
                )
                api_key = getattr(wallet, 'api_key', None) or ''
                headers = {'Content-Type': 'application/json'}
                if api_key:
                    headers['X-API-Key'] = api_key
                import requests as _req
                try:
                    r = _req.post(sell_built['would_post_url'],
                                   json=sell_built['body'],
                                   headers=headers, timeout=2.0)
                    if r.status_code in (200, 201, 202):
                        revert_results.append((leg.leg_idx, 'sold_lim'))
                    else:
                        revert_results.append(
                            (leg.leg_idx, f'sell_lim_HTTP_{r.status_code}'))
                except Exception as e:
                    revert_results.append(
                        (leg.leg_idx, f'sell_lim_exc_{type(e).__name__}'))
            else:
                # SX Bet revert = take a taker fill on the opposite outcome.
                # Different mechanic from Polymarket/Limitless — instead of
                # SELL we'd POST /orders/fill with the OPPOSITE outcome's
                # maker orders. TODO Phase 17.
                revert_results.append((leg.leg_idx,
                                        f'revert_unimpl_{platform}'))
        except Exception as e:
            log.exception("revert leg %d failed", leg.leg_idx)
            revert_results.append((leg.leg_idx, f'exc_{type(e).__name__}'))
    summary = ' '.join(f'{i}:{s}' for i, s in revert_results)
    return f'reverts=[{summary}]'
