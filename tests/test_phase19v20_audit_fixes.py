"""Phase 19v20 (05.05.2026) — seventh-pass audit fixes.

Three parallel agents audited (1) eval data flow + classify_pools,
(2) dashboard.html in full, (3) fills.py + atomic edge cases. ~25
findings; this PR closes 10 verified critical/high-severity ones.

Notably: agent caught a REGRESSION on the stacked branch — Phase 19v13
fix (arb_id uuid suffix) was merged to main as PR #85, but the stacked
v14→…→v19 chain branched off main BEFORE that merge, so the fix never
made it onto our working tree. Re-applied here.

Bug-by-bug:

 1. executor/atomic.py — RESTORE v13 arb_id uuid suffix. Without it,
    two paper-fire threads firing the same-titled deal in the same ms
    collide; dryrun_log keys collapse, fills.registry can route a
    fill to the wrong arb.

 2. executor/atomic.py revert_filled_legs — only included
    `status == 'filled'`, missing `'filled_with_slippage'`. Phase 19v15
    widened detection to include both, but revert was never updated;
    slippage-filled legs flagged broken-arb but excluded from revert
    SELL → directional exposure left open.

 3. executor/atomic.py revert dispatch — used `deal['platform']`
    (group-level, e.g. 'Polymarket+Limitless') for ALL legs. For
    cross-platform deals this routed a Limitless leg through the
    Polymarket SELL path with a token_id that doesn't exist on
    Polymarket → 4xx → leg stays unflattened. Now uses
    `entry['platform']` per leg.

 4. arb_server.py classify_pools — `has_real = any(yes_src in REAL)`
    only checked YES side. `_best_near_structure` requires both
    yes AND no in REAL_OB_SOURCES (or no_src=None). Events with real
    YES but `no_src='implied'` (no real NO orderbook AND no synthetic
    from yes-bid) inflated `pool_poly_near` but never appeared in the
    visible NEAR list — exact "fairy-tale stats" symptom Phase 19v12
    was supposed to kill. Phase 19v12 fix was incomplete; now matches
    `_best_near_structure`'s per-leg gate.

 5. dashboard.html NEAR table — XSS hardening parity with v16
    `createDealCard`. Numeric fields (`distance_cents`, `min_liquidity`,
    `sum_cents`) were inlined raw into `tr.innerHTML`. Now coerced via
    `num()` helper + escaped via `escHtml()`; CSS class fragments
    sanitized.

 6. dashboard.html — `setInterval(fetchDeals, 3000)` had no
    AbortController. Slow scans (>3s on `/api/deals`) → 2-3 in-flight
    fetches → newer response could land BEFORE older → operator saw
    stale data flicker. Now: previous request aborted before new one
    fires; `_dealsInflight` flag short-circuits redundant entries.

 7. dashboard.html — polling continued on hidden tabs. Multi-tab
    operators saw 4×N RPS for nothing. Added visibilitychange
    listener: skip on hidden, immediate refresh on return.

 8. dashboard.html — `expandedSet` and `seenAlerts` grew unbounded
    (no eviction). After 24-48h on a single tab: thousands of stale
    keys, ever-growing heap. Added 60s caps via `_capSet(s, max)`.

 9. dashboard.html — `showToast(msg)` did
    `toast.innerHTML = ... + msg.replace(...)`. `msg` is a
    server-controlled quarantine title → XSS via market name. Now
    builds via DOM nodes + textContent for the user-controlled part.

10. dashboard.html — `renderAnalytics` did `.toFixed(2)` on raw
    `sim.net_total` etc. If backend returned `null` → TypeError →
    outer try/catch swallowed → analytics tab silently froze on
    stale numbers. Now `Number(...)||0` coercion before `.toFixed`.

11. executor/atomic.py — `_write_position_row` read `arb_id` from
    `leg_result.extra` (the WS fill payload), but fill payloads
    don't carry arb_id. Result: positions.jsonl always had
    `arb_id: null`, breaking reconcile / analytics join to dryrun.jsonl.
    Fix: pass arb_id explicitly from caller.

12. executor/fills.py — `expire_stale` purge counter dead code:
    `del self._by_slug[key]; purged += len(self._by_slug.get(key, []))`
    ran AFTER the del → `.get(...)` returned `[]` → +0. Empty-bucket
    prune AND partial-trim path both under-counted. Operator's GC
    visibility metric was always lying.
"""
import os
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


# ── Bug #1: arb_id uuid suffix restored ───────────────────────────

def test_atomic_imports_uuid():
    """`import uuid` must be present (Phase 19v13 regression check)."""
    import inspect
    from executor import atomic
    src = inspect.getsource(atomic)
    assert '\nimport uuid\n' in src or 'import uuid' in src.split('\n')[20:30]


def test_arb_id_includes_uuid_suffix():
    """fire_arb constructs arb_id with uuid4 suffix."""
    import inspect
    from executor import atomic
    src = inspect.getsource(atomic.fire_arb)
    assert 'uuid.uuid4().hex' in src
    assert '_suffix' in src or 'arb_id = (f' in src


# ── Bug #2: revert includes filled_with_slippage ──────────────────

def test_revert_includes_slippage_filled():
    """revert_filled_legs must SELL legs that were filled at slippage
    (not just status=='filled')."""
    import inspect
    from executor import atomic
    src = inspect.getsource(atomic.revert_filled_legs)
    assert "'filled_with_slippage'" in src
    # Old strict-only filter must be gone
    assert "if l.status == 'filled']" not in src.replace(' ', '')


# ── Bug #3: per-leg platform dispatch ─────────────────────────────

def test_revert_uses_entry_platform():
    """revert_filled_legs must read platform from `entry`, not `deal`."""
    import inspect
    from executor import atomic
    src = inspect.getsource(atomic.revert_filled_legs)
    assert "entry.get('platform')" in src


# ── Bug #4: classify_pools has_real symmetric ─────────────────────

def test_classify_pools_has_real_checks_both_sides():
    """Source guard: classify_pools `has_real` checks BOTH yes_src and
    no_src to match `_best_near_structure`'s filter."""
    import inspect
    import arb_server
    src = inspect.getsource(arb_server.classify_pools)
    # Find the has_real definition
    has_real_idx = src.find('has_real = ')
    assert has_real_idx > 0
    snippet = src[has_real_idx:has_real_idx + 400]
    # Must check BOTH yes_src and no_src
    assert 'yes_src' in snippet
    assert 'no_src' in snippet


# ── Bug #5: dashboard NEAR table coerces numerics ─────────────────

def test_dashboard_near_uses_num_coercion():
    """NEAR table renderer uses `num()` helper for numeric fields."""
    dash = os.path.join(os.path.dirname(HERE), 'Scripts', 'dashboard.html')
    with open(dash, 'r', encoding='utf-8') as f:
        html = f.read()
    # Check the NEAR-renderer area near `d.items.forEach`
    idx = html.find('d.items.forEach')
    assert idx > 0
    # Include 200 chars BEFORE the forEach (where `num` is declared)
    snippet = html[max(0, idx-300):idx + 3000]
    assert 'const num = ' in snippet or '(v) => Number(v)' in snippet
    assert 'num(it.distance_cents)' in snippet or 'distC = num(' in snippet
    assert 'num(it.min_liquidity)' in snippet or 'num(it.sum_cents)' in snippet


# ── Bug #6 + #7 + #8: dashboard polling hardening ─────────────────

def test_dashboard_fetchdeals_uses_abortcontroller():
    """fetchDeals must use AbortController to cancel stale requests."""
    dash = os.path.join(os.path.dirname(HERE), 'Scripts', 'dashboard.html')
    with open(dash, 'r', encoding='utf-8') as f:
        html = f.read()
    assert 'AbortController' in html
    assert '_dealsInflight' in html
    assert '_dealsAbort' in html


def test_dashboard_visibility_handler():
    """Polling pauses when document hidden, resumes on focus."""
    dash = os.path.join(os.path.dirname(HERE), 'Scripts', 'dashboard.html')
    with open(dash, 'r', encoding='utf-8') as f:
        html = f.read()
    assert 'visibilitychange' in html
    assert 'document.hidden' in html


def test_dashboard_set_capping():
    """Memory leak fix: expandedSet / seenAlerts get capped periodically."""
    dash = os.path.join(os.path.dirname(HERE), 'Scripts', 'dashboard.html')
    with open(dash, 'r', encoding='utf-8') as f:
        html = f.read()
    assert '_capSet' in html
    assert 'expandedSet' in html
    assert 'seenAlerts' in html


# ── Bug #9: toast XSS ─────────────────────────────────────────────

def test_dashboard_toast_uses_textcontent():
    """showToast must NOT inject server text via innerHTML."""
    dash = os.path.join(os.path.dirname(HERE), 'Scripts', 'dashboard.html')
    with open(dash, 'r', encoding='utf-8') as f:
        html = f.read()
    # Find showToast body
    idx = html.find('function showToast(')
    assert idx > 0
    body = html[idx:idx + 800]
    # New pattern: textContent on user portion
    assert 'textContent' in body
    # Old vulnerable pattern absent
    assert "innerHTML = '⚠️ <strong>Фильтр:</strong><br>' + msg" not in body


# ── Bug #10: analytics .toFixed coercion ──────────────────────────

def test_dashboard_analytics_coerces_numbers():
    """renderAnalytics must Number()-coerce before .toFixed."""
    dash = os.path.join(os.path.dirname(HERE), 'Scripts', 'dashboard.html')
    with open(dash, 'r', encoding='utf-8') as f:
        html = f.read()
    idx = html.find('function renderAnalytics(')
    assert idx > 0
    body = html[idx:idx + 2000]
    assert 'num(sim.net_total)' in body or 'Number(sim.net_total)' in body


# ── Bug #11: positions.jsonl arb_id pass-through ──────────────────

def test_write_position_row_takes_arb_id_param():
    """`_write_position_row` accepts `arb_id` keyword argument."""
    import inspect
    from executor import atomic
    sig = inspect.signature(atomic._write_position_row)
    assert 'arb_id' in sig.parameters


def test_fire_paths_pass_arb_id_to_position_log():
    """Both `_fire_one_leg_live` and `_fire_one_leg_maker` pass arb_id
    when writing the position row."""
    import inspect
    from executor import atomic
    for fn_name in ('_fire_one_leg_live', '_fire_one_leg_maker'):
        fn = getattr(atomic, fn_name)
        src = inspect.getsource(fn)
        if '_write_position_row' not in src:
            continue
        # Find the call site
        call_idx = src.find('_write_position_row(')
        assert call_idx > 0
        call = src[call_idx:call_idx + 200]
        assert 'arb_id=arb_id' in call, \
            f"{fn_name} should pass arb_id=arb_id to _write_position_row"


# ── Bug #12: expire_stale purge counter ───────────────────────────

def test_fills_expire_stale_counts_correctly():
    """Source-level: `purged += len(bucket)` BEFORE del, not after."""
    import inspect
    from executor import fills
    src = inspect.getsource(fills.FillRegistry.expire_stale)
    # Strip docstring + comments — they may quote the broken pattern
    code_lines = []
    in_docstring = False
    quote = None
    for line in src.split('\n'):
        ls = line.lstrip()
        if not in_docstring and (ls.startswith('"""') or ls.startswith("'''")):
            quote = ls[:3]
            if ls.count(quote) >= 2:
                continue  # single-line docstring
            in_docstring = True
            continue
        if in_docstring:
            if quote in line:
                in_docstring = False
            continue
        if ls.startswith('#'):
            continue
        code_lines.append(line)
    code_only = '\n'.join(code_lines)
    assert 'purged += len(bucket)' in code_only
    # Old broken pattern absent
    assert 'purged += len(self._by_slug.get(key, []))' not in code_only
