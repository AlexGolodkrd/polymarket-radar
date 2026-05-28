"""Wallet pool + rebalance proposal blueprints.

Extracted from arb_server.py::api_wallets + api_rebalance_proposals
in audit-28b cont 4 (28.05.2026). Both endpoints read from the
in-process wallet pool (`_wallet_pool` global on arb_server) + the
`wallets` module's rebalance helpers.

Lazy-import to avoid cyclic deps.
"""
from __future__ import annotations

from typing import Any

from flask import Blueprint, jsonify

bp = Blueprint('radar_wallets', __name__)


@bp.route('/api/wallets')
def api_wallets() -> Any:
    """Snapshot of wallet pool — bots, balances, signing capability,
    pool backend. Polled by the dashboard's wallets panel."""
    try:
        from arb_server import _wallet_pool
    except Exception as e:
        return jsonify({'error': f'wallet pool unavailable: {e}'}), 500
    return jsonify({
        'backend': _wallet_pool.backend,
        'cold_address': _wallet_pool.cold_address,
        'count': len(_wallet_pool.wallets),
        'bots': [{
            'bot_id': w.bot_id,
            'eth_address': w.eth_address,
            'store_name': w.store_name,
            'can_sign': w.can_sign,
            'usdc': round(w.last_known_usdc, 2),
            'last_balance_unix': w.last_balance_check_unix,
        } for w in _wallet_pool.wallets],
    })


@bp.route('/api/rebalance/proposals')
def api_rebalance_proposals() -> Any:
    """Compute rebalance proposals against current pool. Read-only —
    nothing transferred. Auto-loop runs separately and logs to
    Executions/rebalance.jsonl."""
    try:
        from arb_server import _wallet_pool
        import wallets as wallets_mod
    except Exception as e:
        return jsonify({'error': f'rebalance unavailable: {e}'}), 500
    proposals = wallets_mod.propose_rebalances(_wallet_pool)
    return jsonify({
        'count': len(proposals),
        'proposals': [{
            'from': p.from_bot, 'to': p.to_bot,
            'amount_usdc': p.amount_usdc, 'reason': p.reason,
        } for p in proposals],
        'history': wallets_mod.rebalance_history(limit=20),
    })
