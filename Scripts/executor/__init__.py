"""Atomic execution engine for the arb radar (Phase 2 — dry-run only).

Architecture:
    builders.py  — produce platform-specific order bodies (Polymarket EIP-712,
                   SX Bet pre-signed orders, Kalshi REST). Real signing is
                   gated on wallet.private_key being set; dry-run returns
                   unsigned bodies.
    atomic.py    — fire_arb(deal, wallets) = parallel order placement with
                   <100ms target, slippage check, 2s per-order timeout,
                   5s dead-man switch, reversal of partial fills.
    fills.py     — WS user-channel listener mapping fill events back to
                   in-flight fire_arb calls. Stub in Phase 2 (no real keys
                   yet); fully wired in Phase 4 once wallets land.
    dryrun_log.py — append decision lines to Executions/dryrun.jsonl,
                   schedule a post-hoc realistic-fill evaluator that
                   re-fetches the orderbook 1-5s later and writes the
                   "what would have happened" row to Executions/paper_results.jsonl.

Default mode is dry-run (DRY_RUN=True). Real firing is unlocked by Phase 4
(wallet keys) + Phase 5 (paper-trading graduation gate).
"""
from .builders import (
    build_poly_order, build_sx_order, build_kalshi_order, build_limitless_order,
    build_limitless_cancel, build_limitless_cancel_batch,
    build_limitless_cancel_all_market,
    build_poly_cancel, build_poly_cancel_all, build_poly_hmac_headers,
)
from .atomic import fire_arb, ArbFireResult
from .dryrun_log import log_decision, schedule_realistic_eval, paper_stats

__all__ = [
    'build_poly_order', 'build_sx_order', 'build_kalshi_order', 'build_limitless_order',
    'build_limitless_cancel', 'build_limitless_cancel_batch',
    'build_limitless_cancel_all_market',
    'build_poly_cancel', 'build_poly_cancel_all', 'build_poly_hmac_headers',
    'fire_arb', 'ArbFireResult',
    'log_decision', 'schedule_realistic_eval', 'paper_stats',
]
