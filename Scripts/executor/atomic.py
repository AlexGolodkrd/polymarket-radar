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

log = logging.getLogger(__name__)

# Dry-run is the default; flip via env DRY_RUN=0 once Phase 4 wallets land
# AND Phase 5 graduation gate (>=70% win-rate over 100 paper trades) passes.
DRY_RUN = os.environ.get('DRY_RUN', '1') != '0'

# Per-order knobs — same defaults as plan
PER_ORDER_TIMEOUT_S = 2.0
DEADMAN_TIMEOUT_S = 5.0
SLIPPAGE_TOLERANCE = 0.001     # 0.1¢ (matches idea.md slippage rule)
REALISTIC_EVAL_DELAY_S = 5.0   # delay before sampling real book for paper-trade row
TARGET_FIRE_BUDGET_MS = 100    # informational, used in logs


@dataclass
class LegResult:
    leg_idx: int
    platform: str
    status: str              # 'dry-fired', 'filled', 'cancelled', 'rejected', 'timeout', 'disabled'
    expected_price: float
    expected_size_usdc: float
    fill_price: Optional[float] = None    # only when actually filled (or post-hoc evaluated)
    fill_size_usdc: Optional[float] = None
    bot_id: Optional[str] = None
    error: Optional[str] = None
    elapsed_ms: Optional[float] = None


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
        return builders.build_poly_order(
            token_id=token_id, side='BUY',
            price=entry['price'], size_usdc=float(entry['stake']),
            wallet=wallet,
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
    log.warning("unknown platform %s — leg %d skipped", platform, leg_idx)
    return None


def _fire_one_leg_dryrun(deal: dict, leg_idx: int, wallet: builders.WalletStub,
                         arb_id: str) -> LegResult:
    """Dry-run a single leg: build the order body, log it, return as if filled.
    Real fill is evaluated later by dryrun_log.schedule_realistic_eval."""
    t0 = time.time()
    entry = deal['entries'][leg_idx]
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
    return LegResult(
        leg_idx=leg_idx, platform=built['platform'],
        status='dry-fired',
        expected_price=built['expected_price'],
        expected_size_usdc=built['expected_size_usdc'],
        bot_id=wallet.bot_id,
        elapsed_ms=(time.time() - t0) * 1000,
    )


def _assign_wallets(legs_count: int, wallets: List[builders.WalletStub]) -> List[builders.WalletStub]:
    """Round-robin one wallet per leg — anti-detection rule from plan
    (CLAUDE.md memory): never aggregate multiple legs in one wallet.

    If wallets < legs we still distribute round-robin (some bots get 2 legs
    on different events — acceptable). If wallets is empty we fall back to
    a single mock stub so dry-run still works without Phase 4 keys.
    """
    if not wallets:
        wallets = [builders.WalletStub(bot_id='mock', eth_address='0x' + '0'*40)]
    return [wallets[i % len(wallets)] for i in range(legs_count)]


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
    assigned = _assign_wallets(legs_count, wallets or [])

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

    if not dry_run:
        # Real-mode — gated. Returning early with explicit reason makes the
        # block visible until Phase 4/5 explicitly opens it.
        result.aborted_reason = ('real-mode disabled: Phase 4 wallet keys + '
                                 'Phase 5 graduation gate not yet passed')
        return result

    if legs_count == 0:
        result.aborted_reason = 'deal has no legs'
        return result

    # Parallel dry-fire — same code path real mode would take
    t_start = time.time()
    with ThreadPoolExecutor(max_workers=max(legs_count, 1)) as pool:
        futs = {pool.submit(_fire_one_leg_dryrun, deal, i, assigned[i], arb_id): i
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
    log.info("dry-fired arb %s in %.0fms (%d legs, %s structure)",
             arb_id, elapsed_ms, legs_count, result.deal_structure)

    # Top-level decision row + schedule realistic evaluation (Phase 5 input)
    dryrun_log.log_decision(result)
    dryrun_log.schedule_realistic_eval(result, deal,
                                       delay_s=REALISTIC_EVAL_DELAY_S)
    return result
