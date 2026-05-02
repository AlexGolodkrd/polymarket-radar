"""BotConnector — thin wrapper for external bots that don't deal with
the radar's deal/entries arb structure.

Радар внутри оперирует "сделкой" (`deal` dict с N legs из `entries`),
потому что вся защита (preflight, anti-detection, position logging,
revert) построена вокруг МНОГО-ногой атомарной сделки. Внешним ботам
типа gabagool / copy-trade / single-leg directional нужен plain API:

    connector.place_order(
        platform='Polymarket', market_id='12345...',
        side='BUY', price=0.45, size=10.0, wallet_id='bot1',
    )

Внутри одна-нога заворачивается в синтетический deal + проходит ровно
тот же fire_arb pipeline → preflight → builders → POST. Все защиты
(балансы, allowance, kill-switch, dry-run) работают одинаково.

Платформо-специфичные поля передавать через **extras**:
    Polymarket: neg_risk (bool), tick_size (float), condition_id (str)
    SX Bet:     outcome (1|2 — обязательно)
    Limitless:  token_id (str), verifying_contract (str)
"""
from __future__ import annotations
from typing import List, Optional, Dict, Any
from . import atomic, builders


class BotConnector:
    def __init__(self, wallets: List[builders.WalletStub], dry_run: bool = True):
        self.wallets = list(wallets)
        self.dry_run = dry_run

    def _wallet_by_id(self, bot_id: str) -> Optional[builders.WalletStub]:
        return next((w for w in self.wallets if w.bot_id == bot_id), None)

    def place_order(self, platform: str, market_id: str, side: str,
                    price: float, size: float, wallet_id: str,
                    **extras: Any) -> Dict[str, Any]:
        """Single-leg order via fire_arb pipeline. Returns simplified result."""
        if side.upper() not in ('BUY', 'SELL'):
            return {'status': 'rejected', 'error': f'bad side: {side}'}
        wallet = self._wallet_by_id(wallet_id)
        if wallet is None:
            return {'status': 'rejected',
                    'error': f'wallet {wallet_id} not in pool'}
        # Build entry shape matching atomic._build_leg expectations
        entry: Dict[str, Any] = {
            'price': float(price), 'stake': float(size),
            'side': side.upper(), 'accepting_orders': True,
        }
        if platform == 'Polymarket':
            entry['token_id'] = market_id
            entry['neg_risk'] = bool(extras.get('neg_risk', False))
            if 'tick_size' in extras: entry['tick_size'] = float(extras['tick_size'])
            if 'condition_id' in extras: entry['condition_id'] = extras['condition_id']
        elif platform == 'SX Bet':
            outcome = extras.get('outcome')
            if outcome not in (1, 2):
                return {'status': 'rejected',
                        'error': 'SX Bet requires outcome=1|2 in extras'}
            entry['market_hash'] = market_id
            entry['outcome_index'] = outcome
        elif platform == 'Limitless':
            entry['slug'] = market_id
            if 'token_id' in extras: entry['token_id'] = extras['token_id']
            if 'verifying_contract' in extras:
                entry['verifying_contract'] = extras['verifying_contract']
        else:
            return {'status': 'rejected',
                    'error': f'unsupported platform: {platform}'}
        # Synthetic single-leg deal
        deal = {
            'title': f'{platform}:{market_id}:{side}',
            'platform': platform, 'arb_structure': 'binary',
            'payout_target': 1.0 / max(price, 1e-6) * size,
            'sum_cents': price * 100, 'entries': [entry],
        }
        if platform == 'SX Bet': deal['market_hash'] = market_id
        # Fire — single wallet pinned
        result = atomic.fire_arb(deal, [wallet], dry_run=self.dry_run)
        leg = result.legs[0] if result.legs else None
        return {
            'status': leg.status if leg else 'aborted',
            'fill_price': leg.fill_price if leg else None,
            'fill_size_usdc': leg.fill_size_usdc if leg else None,
            'error': (result.aborted_reason or (leg.error if leg else None)),
            'arb_id': result.arb_id, 'platform': platform,
            'market_id': market_id, 'side': side, 'wallet_id': wallet_id,
        }
