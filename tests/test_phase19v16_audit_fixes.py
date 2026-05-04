"""Phase 19v16 (05.05.2026) — fourth-pass audit fixes.

Eight high-confidence bugs from the third full-codebase audit (3 parallel
agents) verified by direct code-reading and fixed here. Each fix has a
focused regression test.

  1. arb_server._persist_scan_state — shallow copy → JSON race with mutation
  2. arb_server.api_deals — same shallow-copy race
  3. executor/dryrun_log — realistic_total skipped aborted legs → fake graduation
  4. dashboard.html — XSS via grade/risk/platform/source in createDealCard
  5. wallets/coordinator — reservation TTL too short for fire duration
  6. circuit_breaker — Telegram callback inside lock → starvation
  7. wallets/rebalance — local mutation without rollback (Phase 4 stub)
  8. arb_server.run_pause_scan — lost-update on parallel scan replacement
"""
import os
import sys
import threading

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


# ── Bug #1 + #2: deep-copy snapshot ─────────────────────────────

def test_persist_scan_state_serializes_inside_lock():
    """`_persist_scan_state` must serialize the snapshot WHILE holding
    `scan_lock` so concurrent mutation can't corrupt the JSON."""
    import inspect
    import arb_server
    src = inspect.getsource(arb_server._persist_scan_state)
    # Should call json.dumps inside the lock block
    assert 'json.dumps' in src
    pre_lock, _, post_lock = src.partition('with scan_lock:')
    # json.dumps must come AFTER `with scan_lock:` (inside its body)
    assert 'json.dumps' in post_lock


def test_api_deals_uses_deep_snapshot():
    """`api_deals` must JSON-roundtrip under the lock so the response
    payload is decoupled from live mutating state."""
    import inspect
    import arb_server
    src = inspect.getsource(arb_server.api_deals)
    assert 'json.loads(json.dumps' in src or 'copy.deepcopy' in src


# ── Bug #3: dryrun_log includes all legs ────────────────────────

def test_dryrun_log_realistic_total_includes_aborted():
    """`_evaluate_realistic_fill` must add cost for EVERY leg, not skip
    aborted/disabled rows that lack `expected_price`."""
    import inspect
    from executor import dryrun_log
    src = inspect.getsource(dryrun_log._evaluate_realistic_fill)
    # Old broken filter must be GONE
    assert "if r.get('realistic_fill') is not None" not in src \
        or '_row_cost' in src
    # Should fall back to leg.expected_price when row lacks it
    assert 'leg.expected_price' in src or 'leg_by_idx' in src


# ── Bug #4: dashboard XSS hardening ─────────────────────────────

def test_dashboard_deal_card_escapes_grade_risk_platform():
    """createDealCard must escape grade, risk, platform via escHtml/safeStr."""
    dash = os.path.join(os.path.dirname(HERE), 'Scripts', 'dashboard.html')
    with open(dash, 'r', encoding='utf-8') as f:
        html = f.read()
    # Look for the helper functions added in v16
    assert 'safeStr' in html or 'escHtml(String(d.grade' in html
    # Plain `${d.grade}` (without escape wrapper) must be gone in the badge
    # context. We can't run the JS, but check for the new helper usage.
    assert 'safeStr(d.grade)' in html or 'escHtml(String(d.grade' in html


def test_dashboard_uses_num_coercion():
    """Numeric fields use `num()` helper to default missing/NaN to 0."""
    dash = os.path.join(os.path.dirname(HERE), 'Scripts', 'dashboard.html')
    with open(dash, 'r', encoding='utf-8') as f:
        html = f.read()
    assert 'const num = ' in html
    # And it's applied to at least one common field
    assert 'num(d.net)' in html or 'num(d.total_cents)' in html


# ── Bug #5: coordinator reservation TTL ─────────────────────────

def test_coordinator_reservation_ttl_at_least_10s():
    """Phase 19v16 — TTL must comfortably exceed worst-case fire time."""
    from wallets import coordinator
    assert coordinator._RESERVATION_TTL_S >= 10.0


def test_coordinator_release_reservations_exists():
    """`release_reservations` API exists for explicit post-fire release."""
    from wallets import coordinator
    assert callable(getattr(coordinator, 'release_reservations', None))


def test_coordinator_release_clears_assignments():
    """release_reservations(wallets) must remove their bot_ids from the
    reservation cache."""
    from wallets.config import Wallet, WalletPool
    from wallets import coordinator
    coordinator._recently_assigned.clear()
    wallets = [Wallet(bot_id=f'bot{i}', eth_address=f'0x{i:040x}',
                      store_name='local', last_known_usdc=1000.0)
               for i in range(1, 4)]
    pool = WalletPool(wallets=wallets)
    chosen = coordinator.assign_legs(pool, legs_count=3, min_usdc_per_bot=50)
    assert chosen
    assert all(w.bot_id in coordinator._recently_assigned for w in chosen)
    coordinator.release_reservations(chosen)
    assert all(w.bot_id not in coordinator._recently_assigned for w in chosen)


# ── Bug #6: circuit_breaker callback outside lock ───────────────

def test_circuit_breaker_drains_callbacks_outside_lock():
    """`_transition` only mutates state under `_lock`; the callback
    dispatch is deferred via `_drain_pending_callbacks`."""
    import inspect
    from circuit_breaker import CircuitBreaker
    cb_src = inspect.getsource(CircuitBreaker._transition)
    assert '_pending_callbacks' in cb_src
    drain_src = inspect.getsource(CircuitBreaker._drain_pending_callbacks)
    assert '_on_state_change' in drain_src
    # allow / on_success / on_failure must invoke the drain after lock
    for fn in ('allow', 'on_success', 'on_failure'):
        src = inspect.getsource(getattr(CircuitBreaker, fn))
        assert '_drain_pending_callbacks' in src, \
            f"{fn} must call _drain_pending_callbacks after `with self._lock:`"


def test_circuit_breaker_callback_fires_outside_lock():
    """End-to-end: callback should not execute before the breaker lock
    is released."""
    from circuit_breaker import CircuitBreaker, CircuitState
    fired = []
    lock_held_during = []
    cb_obj = []  # captured later

    def cb(host, old, new, reason):
        # If the lock is still held when we fire, the breaker is broken
        if cb_obj:
            lock_held_during.append(cb_obj[0]._lock.locked())
        fired.append((host, old, new, reason))

    breaker = CircuitBreaker('test', failure_threshold=2, on_state_change=cb)
    cb_obj.append(breaker)
    breaker.on_failure('first')
    breaker.on_failure('second')   # should trip OPEN here
    assert fired, "callback must have fired on transition CLOSED→OPEN"
    # When callback ran, the lock should NOT have been held
    assert all(not h for h in lock_held_during), \
        "callback ran while breaker lock was held"


# ── Bug #7: rebalance.py shadow deltas ──────────────────────────

def test_rebalance_does_not_mutate_pool():
    """Phase 19v16 — `propose_rebalances` must NOT mutate
    `wallet.last_known_usdc` on the canonical pool."""
    from wallets.config import Wallet, WalletPool
    from wallets import rebalance
    rebalance._pair_last_rebalance.clear()  # reset cooldown
    wallets = [
        Wallet(bot_id='bot1', eth_address='0x1', store_name='local',
               last_known_usdc=1000.0),  # high
        Wallet(bot_id='bot2', eth_address='0x2', store_name='local',
               last_known_usdc=10.0),    # low
    ]
    pool = WalletPool(wallets=wallets)
    before = {w.bot_id: w.last_known_usdc for w in wallets}
    proposals = rebalance.propose_rebalances(
        pool, low_threshold=50, high_threshold=200, reserve=100)
    after = {w.bot_id: w.last_known_usdc for w in wallets}
    assert proposals  # something to rebalance
    assert before == after, \
        f"propose_rebalances mutated canonical pool: before={before} after={after}"


# ── Bug #8: pause_scan lost-update ──────────────────────────────

def test_pause_scan_uses_list_copy():
    """Source-level guard — `run_pause_scan` merge takes `list(scan_data['deals'])`
    not a live reference, preventing lost-update against a parallel run_scan."""
    import inspect
    import arb_server
    src = inspect.getsource(arb_server.run_pause_scan)
    # The fix: copy via list(...) so subsequent assignment doesn't blow
    # away a fresh full-scan replacement
    assert 'list(scan_data.get' in src or 'list(scan_data[' in src
    # And stats merge is safe via setdefault
    assert "setdefault('stats'" in src or 'stats = scan_data.get' in src
