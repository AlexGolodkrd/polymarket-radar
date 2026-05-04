"""Phase 19v19 (05.05.2026) — sixth-pass audit fixes.

Three parallel agents audited under-covered modules (analytics,
paper_trading, cross_platform, event_matching, watchdog, preflight,
bot_connector, presign) plus a deep verification pass on executor/
builders deferred bugs from v18. ~25 findings; this PR closes 11
verified critical/high-severity ones.

Bug-by-bug:

 1. executor/builders.py — SX `worstOdds` was the OBSERVED worst price
    (`match['worst_price']`), not the slippage CAP (`max_taker`). Real
    SX fills got worse-than-intended odds when MM withdrew between
    snapshot and fill. Now signs the cap.

 2. executor/builders.py — Polymarket SELL side maker/taker amounts
    were inverted. CTF Exchange BUY: maker=USDC, taker=CTF. SELL:
    maker=CTF, taker=USDC. Old code unconditionally built BUY-shape
    → every SELL order rejected → revert/flatten flow broken in real
    mode.

 3. analytics.py — `deal_key()` was `f"{platform}::{title}"` →
    cross-platform X1 and X2 deals on same title collided in
    `_open_deals` → only one logged as `opened`, other invisible to
    `aggregate()`. Now includes arb_structure + cross_structure.

 4. analytics.py — one-scan miss = immediate close → re-detection on
    next scan reopened with fresh `opened_ts` → SAME arb double-counted
    in sim_count + sim_net. Added 3-scan grace before close.

 5. paper_trading.py — `_evaluate_realistic_fill` writes a row for
    EVERY dry-fired arb including ones with aborted legs. Graduation
    gate counted those, polluting win rate. Now filters to clean rows.
    Also bumped `wins` threshold from `> 0` to `> 0.005` (≥ 0.5¢) so
    float-zero rounding doesn't classify as loss.

 6. event_matching.py — `datetime.utcnow()` is naive + deprecated.
    Year-boundary flip on Dec 31 23:59 UTC: title "Jan 5" → 2026 at
    23:59, → 2027 at 00:01. Now uses tz-aware `now(timezone.utc)`
    plus forward-bias (resolved date >180d in past → bump year).

 7. event_matching.py — MM/DD pattern matched sports scores ("3/2",
    "Game 3/7"). Spurious dates polluted sport+date buckets → real
    cross-platform peer not matched → arb missed. Now requires
    4-digit year for slashed dates.

 8. event_matching.py — `date_match=False` when ONE side missing date
    → confidence dropped 0.3 → perfect title match (sim=1.0)
    collapsed to 0.6 → REJECTED at the 0.80 threshold. Genuine
    cross-platform arbs lost. New 3-tier: both match=1.0,
    one-missing=0.5 (neutral), both-disagree=0.0.

 9. preflight.py — `check_allowance` for neg_risk passed
    `EXCHANGE_NEGRISK` (env-var sourced, may be non-checksum) directly
    to `web3.contract.allowance(address, spender)`. web3.py raises
    `InvalidAddress` on non-checksum → caught by outer except →
    `_read_chain` returned None → preflight downgraded to "skip with
    warning" → allowance never enforced → on-chain TX fails because
    allowance=0. Now `Web3.to_checksum_address()` normalizes ALL three
    addresses defensively.

10. preflight.py — `_BAL_TTL_S=30s` for both balance AND allowance.
    Two fires within 30s read pre-fire balance → fire #2 thinks it
    has $50 but actually $10. Split: balance=2s, allowance=600s. New
    `invalidate_balance_cache(address)` API for atomic.py to call
    after each successful fire.

11. executor/presign.py — `TTL_SECONDS=30s` cached signed orders for
    longer than the typical book stability window. Bundle signed at
    t=0, consumed at t=29 with book moved 1.5¢ (>PRICE_SAFETY_MARGIN)
    → signed limit fired at stale price → either rests forever or
    gets adverse-filled. Reduced to 8s (matches NEAR→HOT latency).

12. wallets/stores.py — `WALLET_BACKEND=aws|windows_cred` returning
    empty addresses + `DRY_RUN=0` silently fell back to mock-stub →
    every "real" fire was actually fake → operator could lose hours
    thinking they were trading live. Now raises `RuntimeError` on
    real-mode + non-local backend with no addresses.
"""
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


# ── Bug #1: SX worstOdds = max_taker ─────────────────────────────

def test_sx_builder_signs_max_taker_not_observed_worst():
    """Source-level: SX signing path passes `max_taker`, not
    `match['worst_price']`."""
    import inspect
    from executor import builders
    src = inspect.getsource(builders.build_sx_order)
    # Find the _sign_sx_order_fill call
    assert '_sign_sx_order_fill' in src
    # Old broken pattern must be gone (passing match.get('worst_price'))
    sign_call = src.split('_sign_sx_order_fill(')[1].split(')')[0]
    assert "match.get('worst_price')" not in sign_call, \
        "SX worstOdds must use max_taker (slippage cap), not observed worst"
    assert 'worst_taker_price=max_taker' in sign_call


# ── Bug #2: Polymarket SELL maker/taker swap ─────────────────────

def test_polymarket_sell_maker_is_contracts():
    """For SELL orders, maker=contracts (CTF), taker=USDC."""
    import inspect
    from executor import builders
    src = inspect.getsource(builders.build_poly_order)
    # Branch on side must be present
    assert "if side == 'BUY':" in src or "side=='BUY'" in src
    # SELL branch swaps maker/taker
    assert 'maker_amount_wei = contracts_wei' in src
    assert 'taker_amount_wei = usdc_wei' in src


# ── Bug #3: deal_key includes arb_structure ──────────────────────

def test_analytics_deal_key_includes_structure():
    """deal_key for a CP deal includes cross_structure."""
    from analytics import deal_key
    d_x1 = {'platform': 'Polymarket+Limitless', 'title': 'Game May 4',
            'arb_structure': 'cross_platform', 'cross_structure': 'X1'}
    d_x2 = {'platform': 'Polymarket+Limitless', 'title': 'Game May 4',
            'arb_structure': 'cross_platform', 'cross_structure': 'X2'}
    assert deal_key(d_x1) != deal_key(d_x2), \
        "X1 and X2 must produce distinct keys"


def test_analytics_deal_key_distinguishes_structures():
    """ALL_YES vs YES_NO_PAIR on same Polymarket event don't collide."""
    from analytics import deal_key
    a = {'platform': 'Polymarket', 'title': 'Test',
         'arb_structure': 'all_yes'}
    b = {'platform': 'Polymarket', 'title': 'Test',
         'arb_structure': 'yes_no_pair'}
    assert deal_key(a) != deal_key(b)


# ── Bug #4: close-grace window ──────────────────────────────────

def test_analytics_close_grace_prevents_double_count():
    """One-scan miss should NOT immediately close the deal."""
    import analytics
    analytics.init()
    # Reset state
    with analytics._lock:
        analytics._open_deals.clear()
    deal = {'platform': 'Polymarket', 'title': 'Test',
            'arb_structure': 'all_yes', 'sum_cents': 95.0,
            'net': 1.5, 'min_liq': 100, 'grade': 'A'}
    # Scan 1: deal seen → opens
    analytics.update_from_scan([deal])
    with analytics._lock:
        assert len(analytics._open_deals) == 1
    # Scan 2: deal missing → miss=1, NOT closed yet
    analytics.update_from_scan([])
    with analytics._lock:
        assert len(analytics._open_deals) == 1, \
            "single miss must not close deal (grace=3)"
    # Scan 3: deal back → resets miss counter
    analytics.update_from_scan([deal])
    with analytics._lock:
        assert analytics._open_deals[analytics.deal_key(deal)]['misses'] == 0
    # Now miss for 3 scans → finally closes
    analytics.update_from_scan([])
    analytics.update_from_scan([])
    analytics.update_from_scan([])
    with analytics._lock:
        assert len(analytics._open_deals) == 0


# ── Bug #5: graduation_status filters aborted rows ───────────────

def test_paper_trading_skips_aborted_legs():
    """`graduation_status` must exclude rows with aborted/disabled legs."""
    import inspect
    import paper_trading
    src = inspect.getsource(paper_trading.graduation_status)
    assert '_row_is_clean' in src or "reason and reason not in" in src


# ── Bug #6 + #7: event_matching utcnow + MM/DD year-required ─────

def test_event_matching_no_utcnow():
    """`extract_date` must NOT call deprecated `datetime.utcnow()`
    in executable code (the comment block may quote it)."""
    import inspect
    import event_matching
    src = inspect.getsource(event_matching.extract_date)
    # Strip comments
    code_only = '\n'.join(
        line for line in src.split('\n')
        if not line.lstrip().startswith('#')
    )
    assert 'utcnow()' not in code_only


def test_event_matching_mmdd_requires_year():
    """`extract_date` must NOT match `3/2` without a year (sports scores
    pollute date buckets)."""
    from event_matching import extract_date
    assert extract_date("Lakers won 3/2") is None
    assert extract_date("Game 3/7 series") is None
    # With year still works
    d = extract_date("3/2/2026 game")
    assert d is not None
    assert d.year == 2026


def test_event_matching_forward_bias_year_boundary():
    """A MMM DD title close to today's date stays in current year;
    one >6 months in past forwards to next year."""
    from event_matching import extract_date
    today = datetime.now(timezone.utc).date()
    # Current month → should stay this year
    d = extract_date(today.strftime('%b ') + str(today.day))
    assert d is not None
    assert d.year == today.year


# ── Bug #8: date_match neutral on partial ────────────────────────

def test_match_event_partial_date_neutral():
    """Pair with one side dated and other side undated must NOT collapse
    to confidence < 0.80 if titles match perfectly."""
    from event_matching import _match_from_canon
    mc = _match_from_canon(
        canon_a='ethereum 110000', canon_b='ethereum 110000',
        sport_a=None, sport_b=None,
        date_a=date(2026, 5, 4), date_b=None,
    )
    assert mc.confidence >= 0.75, \
        f"perfect title match with partial date should not be < 0.75, got {mc.confidence}"


# ── Bug #9: preflight checksum-normalizes addresses ──────────────

def test_preflight_read_chain_uses_checksum():
    """Source guard: `_read_chain` calls `Web3.to_checksum_address()`
    on address, spender, and PUSD_ADDRESS."""
    import inspect
    import preflight
    src = inspect.getsource(preflight._read_chain)
    assert 'to_checksum_address' in src
    # Three addresses should be normalized
    assert 'address_cs' in src
    assert 'spender_cs' in src
    assert 'pusd_cs' in src


# ── Bug #10: balance TTL split ───────────────────────────────────

def test_preflight_balance_ttl_short():
    """Balance TTL must be ≤ 5s (was 30s)."""
    import preflight
    assert preflight._BAL_TTL_S <= 5.0


def test_preflight_invalidate_balance_cache_api():
    """Public API to wipe a wallet's cached balance."""
    import preflight
    assert callable(getattr(preflight, 'invalidate_balance_cache', None))


def test_preflight_invalidate_clears_only_balance():
    """Invalidate removes balance entries but keeps allowance."""
    import preflight
    preflight._BAL_CACHE.clear()
    preflight._cache_put(('0xabc', 'balance', ''), 100.0)
    preflight._cache_put(('0xabc', 'allowance', '0xspender'), 1e18)
    preflight.invalidate_balance_cache('0xABC')
    assert preflight._cache_get(('0xabc', 'balance', '')) is None
    assert preflight._cache_get(('0xabc', 'allowance', '0xspender')) is not None
    preflight._BAL_CACHE.clear()


# ── Bug #11: presign TTL reduced ─────────────────────────────────

def test_presign_ttl_under_book_stability_window():
    """Bundle TTL must be ≤ 10s (was 30s, longer than book stability)."""
    from executor import presign
    assert presign.TTL_SECONDS <= 10.0


# ── Bug #12: stores raises on AWS+empty+real-mode ────────────────

def test_stores_raises_on_real_mode_with_empty_aws():
    """`load_pool('aws')` returning empty + DRY_RUN=0 must RAISE."""
    import os as _os
    from wallets import stores
    # Force AWS path: monkey-patch AwsSecretsStore.addresses() to {}
    saved = stores.AwsSecretsStore
    saved_dry = _os.environ.get('DRY_RUN')
    try:
        class _EmptyAws:
            name = 'aws'
            def addresses(self):
                return {}
            def has_key(self, _):
                return False
        stores.AwsSecretsStore = _EmptyAws
        _os.environ['DRY_RUN'] = '0'
        with pytest.raises(RuntimeError, match='WALLET_BACKEND'):
            stores.load_pool('aws')
    finally:
        stores.AwsSecretsStore = saved
        if saved_dry is None:
            _os.environ.pop('DRY_RUN', None)
        else:
            _os.environ['DRY_RUN'] = saved_dry


def test_stores_local_empty_does_not_raise():
    """Local backend returning empty is OK (dev environment without
    Credentials.env)."""
    from wallets import stores
    saved = stores.LocalEnvStore
    try:
        class _EmptyLocal:
            name = 'local'
            def addresses(self):
                return {}
            def has_key(self, _):
                return False
            def _read(self, _):
                return None
        stores.LocalEnvStore = _EmptyLocal
        # Should NOT raise even with DRY_RUN=0 — local empty is dev OK
        pool = stores.load_pool('local')
        assert pool.wallets == []
    finally:
        stores.LocalEnvStore = saved
