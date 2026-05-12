---
name: polymarket-fee-schedule
description: Verify and (if needed) fix the Polymarket fee model after the 31.03.2026 change that moved fee data into a `feeSchedule` object on each market. Our code reads `maker_base_fee` / `taker_base_fee` — if those field names became legacy, the fallback returns 0 and threshold math silently allows negative-EV arbs.
---

# Polymarket fee schedule — verify current behavior

## The change

Polymarket changelog 31.03.2026: "Fee calculations source: `feeSchedule` object within market data."

Before: flat fields on the market object (`maker_base_fee`, `taker_base_fee` in bps).
After: nested `feeSchedule` object containing fee rates, possibly varying by market type or notional band.

## Our code today

[Scripts/arb_server.py:1706-1707](Scripts/arb_server.py:1706):
```python
'maker_fee_bps': float(m.get('maker_base_fee') or 0),
'taker_fee_bps': float(m.get('taker_base_fee') or 0),
```

Downstream consumers:
- [Scripts/arb_server.py:490](Scripts/arb_server.py:490) — `compute_poly_threshold(taker_fee_bps, n_legs)` derives the arb-detection threshold from fees. If `taker_fee_bps=0`, threshold = `1.0 - SAFETY_BUFFER` (very generous).
- [Scripts/arb_server.py:2256](Scripts/arb_server.py:2256) — per-leg threshold uses the same field.

## Risk if field names changed silently

If Polymarket renamed `maker_base_fee` → `feeSchedule.maker` (or moved them into a nested object), then `m.get('maker_base_fee')` returns None, fallback is 0, threshold is too loose, and we generate "arbs" that are actually negative-EV after the real 2% taker fee deducted at match.

In dry-run, no money lost — `paper_results.jsonl` would show win_rate < expected. In real money, every fired arb is a small loss.

## How to verify (5 minutes)

1. **Probe a current Polymarket market response**:
   ```bash
   curl -s "https://gamma-api.polymarket.com/events?closed=false&active=true&limit=1" \
     | python -m json.tool | head -100
   ```
   Look at the first event's `markets[0]`. Check whether:
   - `maker_base_fee` / `taker_base_fee` are present as numeric fields (old shape — we're fine)
   - `feeSchedule` is present (new shape — we need to update reader)
   - Both present (transition period — old still works but plan to migrate)

2. **Cross-check with our log**: scan logs already print `[POLY] info ...` containing fee_bps. If those are 0 across the board, fees aren't being read — confirmation of the bug.

3. **Probe live `/api/scan_health`**: not directly informative, but you can query `/api/recent_deals?limit=50` and look at sum_cents vs threshold_cents for Polymarket-leg deals. If many sums are near 100c but get flagged, the threshold is loose — fees not subtracted.

## Migration if shape changed

Replace the legacy read with a defensive multi-shape reader:

```python
def _read_poly_fee_bps(market, side):
    """Read fee bps for 'maker' or 'taker' from either legacy flat
    fields or new feeSchedule object. Returns 0 only if BOTH paths
    are missing — defensive against silent rename."""
    # New shape (post-31.03.2026)
    fs = market.get('feeSchedule')
    if isinstance(fs, dict):
        v = fs.get(side) or fs.get(f'{side}_bps') or fs.get(f'{side}Bps')
        if v is not None:
            try: return float(v)
            except (TypeError, ValueError): pass
    # Legacy shape
    legacy_key = f'{side}_base_fee'
    v = market.get(legacy_key)
    if v is not None:
        try: return float(v)
        except (TypeError, ValueError): pass
    return 0.0
```

Then `arb_server.py:1706` becomes:
```python
'maker_fee_bps': _read_poly_fee_bps(m, 'maker'),
'taker_fee_bps': _read_poly_fee_bps(m, 'taker'),
```

This handles all three shapes (legacy only, new only, both during transition) without behavior change for legacy-shape responses.

## Test approach

`tests/test_phase_audit3_poly_fee_schedule.py`:
- Legacy shape → returns the flat field value
- New shape with `feeSchedule.taker` → returns nested value
- Both present → new shape wins (forward-compat)
- Neither present → 0.0 with no exception
- Malformed (`feeSchedule: "not a dict"`) → 0.0 with no exception
- Numeric vs string types in either shape → coerce cleanly

## Why this is "verify before changing"

The default behavior (`or 0`) is silently insurance against any field rename, but at the cost of accepting too many arbs. Two outcomes from the probe:

1. **Legacy still works** (fields still flat): no urgent code change. Plan migration after seeing a Polymarket warning.
2. **Legacy is gone** (only feeSchedule): URGENT — every Polymarket arb since 31.03 has used `fee=0` and we've been over-flagging. Recalibrate `paper_results.jsonl` against actual fee data, then ship the fix.

## Verification — DONE 12.05.2026

Live probe `curl gamma-api.polymarket.com/events?closed=false&active=true&limit=1`:

```
Market keys containing fee: ['makerBaseFee', 'takerBaseFee', 'feesEnabled', 'feeType', 'feeSchedule']
maker_base_fee: None  ← snake_case GONE
taker_base_fee: None  ← snake_case GONE
feeSchedule:
  exponent: 1
  rate: 0.04         ← 4% — this is the NEW source of truth
  takerOnly: true    ← makers don't pay
  rebateRate: 0.25   ← 25% maker rebate
```

**Conclusions**:
1. Both legacy snake_case fields (`maker_base_fee`, `taker_base_fee`) are GONE.
2. CamelCase versions (`makerBaseFee`, `takerBaseFee`) still exist as numeric fields.
3. `feeSchedule` is the post-31.03 source with `rate: 0.04` (= 400 bps = 4%), `takerOnly: true`.
4. Our code reads snake_case → ALWAYS gets None → `or 0` fallback → `compute_poly_threshold(0)` returns `1.0 - SAFETY_BUFFER` (≈0.97). True threshold with 4% fee and 4 legs ≈ 0.93.

**Impact**: radar has been flagging Polymarket arbs in the `0.93 - 0.97` sum range as profitable since 31.03 (over a month). In dry-run this is hidden by TS executor stub (`realistic_pnl = simPnl` per SESSION_SNAPSHOT_2026-05-12.md:159). After `DRY_RUN=0` flip, true 4% taker fee deducts at match → win_rate drops sharply.

## Recommended fix (URGENT before DRY_RUN=0)

Use `feeSchedule.rate * 10000` as primary, fall back to camelCase, fall back to snake_case (defensive against future renames):

```python
def _read_poly_fee_bps(market, side):
    """Read fee bps for 'maker' or 'taker'.
    Priority: feeSchedule (post-31.03) → camelCase legacy → snake_case
    legacy → 0. Returns floats in basis points."""
    # feeSchedule (post-31.03.2026)
    fs = market.get('feeSchedule')
    if isinstance(fs, dict):
        rate = fs.get('rate')  # decimal, e.g. 0.04 for 4%
        if rate is not None:
            taker_only = bool(fs.get('takerOnly'))
            if side == 'maker' and taker_only:
                return 0.0
            try: return float(rate) * 10000.0
            except (TypeError, ValueError): pass
    # camelCase legacy (still present 12.05.2026)
    for key in (f'{side}BaseFee', f'{side}_base_fee'):
        v = market.get(key)
        if v is not None:
            try: return float(v)
            except (TypeError, ValueError): pass
    return 0.0
```

Add `exponent` handling if needed (current value `1` means linear; >1 would scale by stake notional — verify against docs before assuming).

Add `rebateRate` for maker mode: maker_fee_bps becomes negative when rebate applies (we PAY 0 fee + RECEIVE rebate). Currently our maker mode doesn't model rebates, so this just changes the threshold math symmetrically.

## Test approach

`tests/test_phase_audit3_poly_fee_schedule.py`:
- feeSchedule with rate=0.04, takerOnly=true → maker=0, taker=400
- feeSchedule with rate=0.02, takerOnly=false → maker=200, taker=200
- camelCase fallback when feeSchedule missing
- snake_case fallback when camelCase missing
- Malformed feeSchedule (string, list, missing rate) → 0
- Numeric edge cases (rate="0.04" string, rate=0) → coerce/zero correctly

## Test for downstream behavior

After the fix, `compute_poly_threshold(400, n_legs=2)` should return ≈ 0.92 (not 0.97). Add a test asserting this.

Then re-baseline paper_results: arbs in 0.93-0.97 range that were "wins" before should now be filtered out at detection — the count drops, but win_rate per fired arb should hold or improve.

## Sources
- [Polymarket Changelog 31.03.2026](https://docs.polymarket.com/changelog) — feeSchedule entry
- [Scripts/arb_server.py:490](Scripts/arb_server.py:490) `compute_poly_threshold` — downstream consumer
- [Scripts/executor/builders.py:311](Scripts/executor/builders.py:311) — comment refs Polymarket fee docs
