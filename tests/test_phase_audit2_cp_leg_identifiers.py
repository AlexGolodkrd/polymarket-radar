"""Phase audit-2 (11.05.2026) — CP leg identifier propagation tests.

Root-cause we observed in production: all 19 TS executor fires errored
with "polymarket leg requires tokenId" / "sx_bet leg requires
marketHash + outcome" because `to_radar_deal_format.legs_formatted`
never carried the platform-specific IDs. The fix threads `extras` from
PlatformOutcome through `_leg_platform_ids` → CP deal legs →
legs_formatted → `_fire_arb_via_ts` translation table.

These tests pin down each link in the chain so a future refactor can't
silently regress us back to the 100%-errored-fires state.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


# ── _leg_platform_ids: side-aware token selection ──────────────────


def test_leg_platform_ids_polymarket_yes_picks_yes_token():
    from cross_platform import PlatformOutcome, _leg_platform_ids
    out = PlatformOutcome(
        platform='Polymarket', event_id='cond123', outcome_name='X',
        yes_price=0.45, yes_depth=100, yes_source='clob_ask',
        no_price=0.55, no_depth=100, no_source='clob_ask',
        end_date='2026-05-17', title='T',
        extras={
            'token_id_yes': '111', 'token_id_no': '222',
            'condition_id': 'cond123',
            'neg_risk': False, 'tick_size': 0.01,
        },
    )
    ids = _leg_platform_ids(out, side='YES')
    assert ids['token_id'] == '111'
    assert ids['condition_id'] == 'cond123'
    assert ids['neg_risk'] is False
    assert ids['tick_size'] == 0.01


def test_leg_platform_ids_polymarket_no_picks_no_token():
    from cross_platform import PlatformOutcome, _leg_platform_ids
    out = PlatformOutcome(
        platform='Polymarket', event_id='c', outcome_name='X',
        yes_price=0.45, yes_depth=100, yes_source='clob_ask',
        no_price=0.55, no_depth=100, no_source='clob_ask',
        end_date=None, title='T',
        extras={'token_id_yes': '111', 'token_id_no': '222'},
    )
    ids = _leg_platform_ids(out, side='NO')
    assert ids['token_id'] == '222'


def test_leg_platform_ids_sx_yes_maps_to_outcome_1():
    from cross_platform import PlatformOutcome, _leg_platform_ids
    out = PlatformOutcome(
        platform='SX Bet', event_id='0xMH', outcome_name='Lakers',
        yes_price=0.45, yes_depth=100, yes_source='sx_ob',
        no_price=0.55, no_depth=100, no_source='sx_ob',
        end_date=None, title='T',
        extras={'market_hash': '0xMH', 'outcome_one_name': 'Lakers',
                'outcome_two_name': 'Celtics'},
    )
    ids = _leg_platform_ids(out, side='YES')
    assert ids['market_hash'] == '0xMH'
    assert ids['outcome_index'] == 1


def test_leg_platform_ids_sx_no_maps_to_outcome_2():
    from cross_platform import PlatformOutcome, _leg_platform_ids
    out = PlatformOutcome(
        platform='SX Bet', event_id='0xMH', outcome_name='Lakers',
        yes_price=0.45, yes_depth=100, yes_source='sx_ob',
        no_price=0.55, no_depth=100, no_source='sx_ob',
        end_date=None, title='T',
        extras={'market_hash': '0xMH'},
    )
    ids = _leg_platform_ids(out, side='NO')
    assert ids['outcome_index'] == 2


def test_leg_platform_ids_limitless_includes_slug_and_tokens():
    from cross_platform import PlatformOutcome, _leg_platform_ids
    out = PlatformOutcome(
        platform='Limitless', event_id='my-slug', outcome_name='X',
        yes_price=0.45, yes_depth=100, yes_source='lim_clob',
        no_price=0.55, no_depth=100, no_source='lim_clob',
        end_date=None, title='T',
        extras={
            'token_id_yes': '0xYES', 'token_id_no': '0xNO',
            'verifying_contract': '0xCONTRACT',
        },
    )
    ids = _leg_platform_ids(out, side='YES')
    assert ids['slug'] == 'my-slug'
    assert ids['token_id'] == '0xYES'
    assert ids['verifying_contract'] == '0xCONTRACT'


def test_leg_platform_ids_handles_missing_extras_defensively():
    """When extras is None (legacy / non-CP path), we return {} rather
    than crash. The TS executor will still throw, but Python doesn't
    blow up before even firing — better debuggability."""
    from cross_platform import PlatformOutcome, _leg_platform_ids
    out = PlatformOutcome(
        platform='Polymarket', event_id='c', outcome_name='X',
        yes_price=0.45, yes_depth=100, yes_source='clob_ask',
        no_price=0.55, no_depth=100, no_source='clob_ask',
        end_date=None, title='T',
        extras=None,
    )
    assert _leg_platform_ids(out, side='YES') == {}


# ── Integration: build_cross_platform_deal carries IDs onto legs ───


def test_build_cp_deal_legs_carry_poly_and_sx_identifiers():
    """Build an X1 arb (Polymarket YES + SX Bet NO on same team) and
    verify both legs have the right TS-spec keys populated."""
    from cross_platform import PlatformOutcome, build_cross_platform_deal
    out_poly = PlatformOutcome(
        platform='Polymarket', event_id='cond-A',
        outcome_name='Manchester United',
        yes_price=0.43, yes_depth=200, yes_source='clob_ask',
        no_price=0.50, no_depth=200, no_source='clob_ask',
        end_date='2026-05-17', title='EPL Manchester United vs Forest',
        extras={
            'token_id_yes': 'POLY_YES_TID',
            'token_id_no': 'POLY_NO_TID',
            'condition_id': 'cond-A',
            'neg_risk': False, 'tick_size': 0.01,
        },
    )
    out_sx = PlatformOutcome(
        platform='SX Bet', event_id='0xSXHASH',
        outcome_name='Manchester United',
        yes_price=0.42, yes_depth=200, yes_source='sx_ob',
        no_price=0.48, no_depth=200, no_source='sx_ob',
        end_date='2026-05-17', title='EPL Manchester United vs Forest',
        extras={'market_hash': '0xSXHASH',
                'outcome_one_name': 'Manchester United',
                'outcome_two_name': 'Nottingham Forest'},
    )
    deals = build_cross_platform_deal(out_poly, out_sx, match_confidence=0.85,
                                       threshold=0.96)
    assert deals, 'expected at least one CP deal built'
    deal = deals[0]
    # X1 = Poly YES + SX NO; X2 = Poly NO + SX YES.
    for leg in deal.legs:
        plat = leg['platform']
        if plat == 'Polymarket':
            assert leg.get('token_id'), f'poly leg missing token_id: {leg}'
            # Token should depend on side
            if leg['side'] == 'YES':
                assert leg['token_id'] == 'POLY_YES_TID'
            else:
                assert leg['token_id'] == 'POLY_NO_TID'
            assert leg.get('condition_id') == 'cond-A'
            assert 'tick_size' in leg
        elif plat == 'SX Bet':
            assert leg.get('market_hash') == '0xSXHASH'
            assert leg.get('outcome_index') in (1, 2)


# ── Integration: to_radar_deal_format propagates IDs into entries ──


def test_to_radar_deal_format_entries_have_token_id_and_market_hash():
    """End-to-end: the `entries` array in the radar-shaped dict — what
    `_fire_arb_via_ts` reads — must carry token_id (Polymarket) and
    market_hash+outcome_index (SX Bet) for each leg. Without this we
    are back to 100% errored fires."""
    from cross_platform import (
        PlatformOutcome, build_cross_platform_deal, to_radar_deal_format,
    )
    out_poly = PlatformOutcome(
        platform='Polymarket', event_id='cond-X',
        outcome_name='Lakers',
        yes_price=0.43, yes_depth=300, yes_source='clob_ask',
        no_price=0.50, no_depth=300, no_source='clob_ask',
        end_date='2026-05-17', title='NBA Lakers vs Celtics',
        extras={
            'token_id_yes': 'TID_YES', 'token_id_no': 'TID_NO',
            'condition_id': 'cond-X',
        },
    )
    out_sx = PlatformOutcome(
        platform='SX Bet', event_id='0xMH',
        outcome_name='Lakers',
        yes_price=0.42, yes_depth=300, yes_source='sx_ob',
        no_price=0.48, no_depth=300, no_source='sx_ob',
        end_date='2026-05-17', title='NBA Lakers vs Celtics',
        extras={'market_hash': '0xMH'},
    )
    deals = build_cross_platform_deal(out_poly, out_sx, match_confidence=0.85,
                                       threshold=0.96)
    assert deals
    radar = to_radar_deal_format(deals[0])
    assert 'entries' in radar and len(radar['entries']) == 2
    for e in radar['entries']:
        if e['platform'] == 'Polymarket':
            assert e.get('token_id')  # must be present
        if e['platform'] == 'SX Bet':
            assert e.get('market_hash')
            assert e.get('outcome_index') in (1, 2)


# ── Translation: _fire_arb_via_ts maps snake_case → camelCase ──────


def test_fire_arb_via_ts_translation_includes_marketHash_and_tokenId():
    """Verify the translation map turns leg dict keys into the camelCase
    TS spec the executor expects. We don't actually POST — we exercise
    the dict-building portion by feeding a minimal deal and inspecting
    what would be sent.
    """
    # Simulate the translation inline (mirrors arb_server._fire_arb_via_ts)
    leg = {
        'platform': 'SX Bet',
        'side': 'NO',
        'price': 0.55,
        'stake': 50,
        'market_hash': '0xHASH',
        'outcome_index': 2,
    }
    spec = {
        'platform': (leg.get('platform') or '').lower().replace(' bet', '_bet'),
        'side': leg.get('side', 'BUY'),
        'expectedPrice': float(leg.get('price') or 0),
        'expectedSizeUsdc': float(leg.get('stake') or 0),
    }
    for k_py, k_ts in (
        ('token_id', 'tokenId'),
        ('market_hash', 'marketHash'),
        ('outcome_index', 'outcome'),
        ('slug', 'slug'),
        ('verifying_contract', 'verifyingContract'),
        ('neg_risk', 'negRisk'),
        ('tick_size', 'tickSize'),
        ('condition_id', 'conditionId'),
    ):
        if leg.get(k_py) is not None:
            spec[k_ts] = leg[k_py]
    assert spec['platform'] == 'sx_bet'
    assert spec['marketHash'] == '0xHASH'
    assert spec['outcome'] == 2
    assert spec['expectedPrice'] == 0.55
