# Maker Order Testing

**Created Phase 15 (01.05.2026)** — testing patterns for maker-mode arb fires.

## What this is for

When implementing maker-mode arb logic (`build_poly_maker_order`, `maker_supervise`, hybrid mode selector), the tests need to cover scenarios that DON'T happen with taker-only:

1. **Spread-too-tight fallback** — when there's no room for a maker price
2. **Maker fill at expected price** — happy path
3. **Adverse selection** — price moves against us, must cancel
4. **Maker timeout** — no fill, must cancel-and-retry
5. **Hybrid fallback** — maker tried, didn't fill, switch to taker

## Test infrastructure pieces

### Mock fill via Event

Maker fills are detected via WebSocket → `fills.registry.event.set()`. In tests use `threading.Timer` to simulate fill arrival:

```python
class _Reg:
    event = threading.Event()
reg = _Reg()

# Simulate fill in 100ms
threading.Timer(0.1, reg.event.set).start()

result = atomic.maker_supervise(reg, expected_price=0.30, deadline_s=2.0)
assert result == 'filled'
```

### Mock cross-source price check

For adverse selection guard, inject `other_source_check` callable:

```python
def _check_price():
    return 0.32           # 2c drift from expected 0.30 → adverse selection

result = atomic.maker_supervise(
    reg, expected_price=0.30,
    other_source_check=_check_price,
    deadline_s=2.0,
)
assert result == 'adverse_selection'
```

### Mock spread

For builder tests, supply `best_ask` and `best_bid`:

```python
res = build_poly_maker_order(
    token_id='T', side='BUY',
    best_ask=0.40, best_bid=0.30,    # 10c spread, plenty of room
    size_usdc=10.0, wallet=mock_wallet,
)
assert res['is_maker'] is True
assert res['maker_price'] == pytest.approx(0.31)  # bid + tick
```

### Tight-spread fallback

Test that builder falls back to taker when there's no maker room:

```python
res = build_poly_maker_order(
    token_id='T', side='BUY',
    best_ask=0.40, best_bid=0.395,   # 0.5c spread, tick=1c → too tight
    ...
)
assert res['is_maker'] is False
assert res['will_revert_to_taker'] is True
```

## Why these specifically matter

| Test | Bug it catches |
|---|---|
| spread_too_tight | maker order at exact best_ask = no improvement → no maker fee benefit |
| best_ask=None | KeyError on missing market data; fallback path |
| adverse_selection | maker order picked off by informed flow → guaranteed loss |
| timeout no drift | normal timeout → cancel-and-retry path |
| timeout with drift | FALSE adverse — still timeout but for different reason |
| check_exception_handled | broken price source must not crash supervisor |

## Mode selector tests

```python
def test_select_fire_mode_wide_arb_picks_maker(monkeypatch):
    monkeypatch.setattr(atomic, 'MAKER_MODE_ENABLED', True)
    assert atomic.select_fire_mode({'sum_cents': 88}) == 'maker'
```

Boundary cases (88, 92, 94, 96, 96.5) must each be tested explicitly — the
thresholds are tunable, regression tests catch unintended drift.

## What NOT to test

- Real Polymarket V2 sign + POST — that's integration territory; let the
  per-leg taker tests cover the signing path. Maker uses same builder
  with different price.
- Real WebSocket fills — too flaky for unit tests; the supervisor uses
  Event polling which is testable independently.
- Production volume tier rebates — depends on Polymarket account state,
  not a logic test.

## Test file location

`tests/test_phase_15_maker_orders.py` — single suite covering all 14 cases.

## See also

- `maker-taker-orders` skill — design rationale + hybrid mode policy
- `polymarket-trading` skill — V2 EIP-712 details
- `Scripts/executor/builders.py:build_poly_maker_order` — reference implementation
- `Scripts/executor/atomic.py:maker_supervise` — supervisor loop
- `Scripts/executor/atomic.py:select_fire_mode` — hybrid selector
