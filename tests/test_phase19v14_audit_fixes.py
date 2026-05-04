"""Phase 19v14 (05.05.2026) — second-pass audit: 10 critical bugs.

Five parallel agents audited the entire codebase from line 0; ten
high-confidence bugs were verified and fixed in this PR. One regression
test per fix.

  1. arb_server.py — `log` undefined → NameError on every error path
  2. poly_ws.py — `price_change` ignored cancellations of current best_ask
  3. limitless_ws.py — `books` written without `_lock` (mutation race)
  4. dryrun_log.py — `entry['contracts']` KeyError for cross-platform deals
  5. wallets/stores.py — env-file parser preserved inline `#`-comments
  6. wallets/coordinator.py — `assign_legs` had no lock (parallel-fire collision)
  7. executor/atomic.py — `pool.map(timeout=5)` swallowed by outer except
  8. executor/atomic.py — Polymarket revert POST sent without auth headers
  9. dashboard.html — XSS via market title in `onclick` attribute
 10. limitless_ws.py / poly_ws.py — books not cleaned on unsubscribe
"""
import os
import sys
import threading
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


# ── Bug #1: arb_server log defined ───────────────────────────────

def test_arb_server_has_log():
    """Module-level `log` must be defined as a real Logger."""
    import logging
    import arb_server
    assert hasattr(arb_server, 'log')
    assert isinstance(arb_server.log, logging.Logger)


def test_arb_server_log_calls_dont_raise():
    """Calls to log.debug / log.warning at the module's known sites
    must succeed without NameError."""
    import arb_server
    # Direct call — would NameError if `log` undefined
    arb_server.log.debug("test from phase19v14")
    arb_server.log.warning("test from phase19v14")


# ── Bug #2: poly_ws price_change handles cancellations ────────────

def test_poly_ws_price_change_invalidates_on_cancel():
    """price_change with size=0 at current best_ask wipes the book so
    consumers don't see stale lower price."""
    import poly_ws
    cli = poly_ws.PolyMarketWS()
    # Seed a book
    cli.books['t1'] = {'best_ask': 0.55, 'depth': 100.0, 'ts': time.time()}
    # Simulate cancel of best ask
    ev = {
        'event_type': 'price_change',
        'asset_id': 't1',
        'changes': [{'price': '0.55', 'size': '0', 'side': 'SELL'}],
    }
    cli._handle_event(ev)
    # Book should be invalidated
    book = cli.get_book('t1')
    assert book is not None
    assert book.get('best_ask') is None, \
        f"expected best_ask=None after cancel, got {book.get('best_ask')}"


def test_poly_ws_price_change_lower_ask_still_applies():
    """A delta with a lower live ask continues to update best_ask."""
    import poly_ws
    cli = poly_ws.PolyMarketWS()
    cli.books['t1'] = {'best_ask': 0.55, 'depth': 100.0, 'ts': time.time()}
    ev = {
        'event_type': 'price_change',
        'asset_id': 't1',
        'changes': [{'price': '0.50', 'size': '50', 'side': 'SELL'}],
    }
    cli._handle_event(ev)
    book = cli.get_book('t1')
    assert book['best_ask'] == 0.50


# ── Bug #3: limitless_ws books mutation under lock ───────────────

def test_limitless_ws_books_write_under_lock():
    """`_handle_orderbook` must take `_lock` before writing `self.books`."""
    import inspect
    import limitless_ws
    src = inspect.getsource(limitless_ws.LimitlessWS._handle_orderbook)
    # Lock acquisition before book mutation
    assert 'with self._lock:' in src
    # The write line should be inside the lock body
    lock_pos = src.find('with self._lock:')
    write_pos = src.find('self.books[slug] = book')
    assert 0 < lock_pos < write_pos, \
        "self.books mutation must follow `with self._lock:`"


# ── Bug #4: dryrun_log handles missing 'contracts' key ───────────

def test_dryrun_log_falls_back_when_no_contracts_key():
    """`_evaluate_realistic_fill` must not crash when entry lacks 'contracts'.
    CP deals only have stake + price."""
    import inspect
    from executor import dryrun_log
    src = inspect.getsource(dryrun_log._evaluate_realistic_fill)
    # Fallback must be present
    assert "_contracts_of" in src or "entry.get('contracts')" in src \
        or 'entry.get("contracts")' in src
    # Must NOT use bare `entry['contracts']` lookup
    code_only = '\n'.join(
        line for line in src.split('\n')
        if not line.lstrip().startswith('#')
    )
    assert "entries[r['leg_idx']]['contracts']" not in code_only.replace(' ', '')


# ── Bug #5: stores env parser strips inline comments ─────────────

def test_stores_env_parser_strips_inline_comment():
    """`BOT1_PRIVATE_KEY=0xabc # rotated 2026-04-30` must yield only the
    hex value, not the comment suffix."""
    import tempfile
    from wallets.stores import LocalEnvStore
    with tempfile.NamedTemporaryFile(
        mode='w', suffix='.env', delete=False, encoding='utf-8'
    ) as f:
        f.write('BOT1_ETH_ADDRESS=0xc0ffee254729296a45a3885639ac7e10f9d54979 # main bot\n')
        f.write('BOT1_PRIVATE_KEY=0xabc123def456789012345678901234567890123456789012345678901234abcd # rotated\n')
        f.write('BOT2_PASSPHRASE=password#with#hashes\n')  # quoted scenario
        path = f.name
    try:
        store = LocalEnvStore.__new__(LocalEnvStore)
        store._env_path = path
        store._lock = threading.RLock()
        store._cache_addresses = None
        store._cache_keys = {}
        env = store._load_env_file()
        assert env['BOT1_ETH_ADDRESS'] == '0xc0ffee254729296a45a3885639ac7e10f9d54979'
        assert env['BOT1_PRIVATE_KEY'] == '0xabc123def456789012345678901234567890123456789012345678901234abcd'
        # Unquoted with # not preceded by space → preserved (legitimate hash in token)
        assert env['BOT2_PASSPHRASE'] == 'password#with#hashes'
    finally:
        os.unlink(path)


# ── Bug #6: coordinator assign_legs serialized ──────────────────

def test_coordinator_assign_legs_uses_lock():
    """`assign_legs` must serialize via `_assign_lock` so two parallel
    fires don't both pick the same wallets."""
    import inspect
    from wallets import coordinator
    src = inspect.getsource(coordinator.assign_legs)
    assert 'with _assign_lock' in src or '_assign_lock.acquire' in src
    assert '_recently_assigned' in src


def test_coordinator_parallel_assign_no_collision():
    """Two back-to-back assign_legs calls return DISJOINT wallet sets."""
    from wallets.config import Wallet, WalletPool
    from wallets import coordinator
    # Fresh state
    coordinator._recently_assigned.clear()
    wallets = [Wallet(bot_id=f'bot{i}', eth_address=f'0x{i:040x}',
                      store_name='local',
                      last_known_usdc=1000.0)
               for i in range(1, 7)]
    pool = WalletPool(wallets=wallets)
    a = coordinator.assign_legs(pool, legs_count=3, min_usdc_per_bot=50)
    b = coordinator.assign_legs(pool, legs_count=3, min_usdc_per_bot=50)
    a_ids = {w.bot_id for w in a}
    b_ids = {w.bot_id for w in b}
    assert a_ids.isdisjoint(b_ids), \
        f"parallel assigns collided: a={a_ids} b={b_ids}"
    coordinator._recently_assigned.clear()


# ── Bug #7: atomic recheck timeout caught ────────────────────────

def test_atomic_recheck_catches_overall_timeout():
    """`_last_ms_depth_recheck` must catch FutureTimeoutError and report
    it as a failure instead of raising up to fire_arb."""
    import inspect
    from executor import atomic
    src = inspect.getsource(atomic._last_ms_depth_recheck)
    # Must catch FutureTimeoutError
    assert 'FutureTimeoutError' in src
    assert 'recheck_overall_timeout' in src


# ── Bug #8: atomic PM revert sends headers ───────────────────────

def test_atomic_polymarket_revert_sends_headers():
    """Polymarket revert POST must send at least Content-Type, with
    HMAC headers when wallet has poly_api_key."""
    import inspect
    from executor import atomic
    src = inspect.getsource(atomic.revert_filled_legs) \
        if hasattr(atomic, 'revert_filled_legs') \
        else inspect.getsource(atomic)
    # Find the Polymarket revert block — must reference a headers dict
    # or build_poly_hmac_headers
    assert ('build_poly_hmac_headers' in src
            and 'Content-Type' in src), \
        "PM revert must send Content-Type + optional HMAC headers"


# ── Bug #9: dashboard XSS-safe dryfire button ────────────────────

def test_dashboard_dryfire_uses_dataset_attr():
    """The Dry-fire button must pull title from `data-fire-title` (HTML-
    escaped) rather than embedding it in the onclick string."""
    dash = os.path.join(
        os.path.dirname(HERE), 'Scripts', 'dashboard.html'
    )
    with open(dash, 'r', encoding='utf-8') as f:
        html = f.read()
    # New pattern present
    assert 'data-fire-title="${escHtml(d.title)}"' in html
    # Old vulnerable pattern absent
    assert "dryFireDeal('${titleEsc}'" not in html


# ── Bug #10: ws update_subscriptions cleans removed slugs ────────

def test_limitless_ws_drops_removed_slugs():
    """`update_subscriptions` must delete books for slugs no longer in
    desired set."""
    import inspect
    import limitless_ws
    src = inspect.getsource(limitless_ws.LimitlessWS.update_subscriptions)
    assert 'self.books.pop' in src
    assert 'removed' in src or 'self._desired - new_set' in src


def test_poly_ws_drops_removed_tokens():
    """Same cleanup for Polymarket WS."""
    import inspect
    import poly_ws
    src = inspect.getsource(poly_ws.PolyMarketWS.update_subscriptions)
    assert 'self.books.pop' in src
    assert 'removed' in src or 'self._desired - new_set' in src
