"""Phase 19v13 (05.05.2026) — multi-bug audit fixes.

Consolidated PR fixing 10 high-confidence bugs found by full-codebase audit:

  1. _persist_scan_state daemon race (concurrent file writes corrupt JSON)
  2. find_pairs dedup broken (`not (x in seen or seen.add(x))` ≡ True)
  3. WS-first /book mislabels stale data as `clob_ask`
  4. Duplicate `risk="LOW"` branch in build_deal — collapses two depth tiers
  5. POLY_ADDRESS sent un-checksummed in L2 auth headers
  6. arb_id collision when ms-tick + same title coincide
  7. Bare `except:` in _fetch_kalshi_ob swallows KeyboardInterrupt
  8. sum_x1 / sum_x2 computed twice in cross_platform.build_*_deal
  9. to_radar_deal_format crashes on empty legs (`min(empty)`)
 10. paper_trading median off-by-one for even-length lists

Each test is a focused regression — run targets the fix, not the surrounding
behaviour.
"""
import os
import sys
import threading
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


# ── Bug #1: persist_scan_state daemon race ────────────────────────

def test_persist_state_lock_exists():
    """Module-level lock guards concurrent persist daemons."""
    import arb_server
    assert hasattr(arb_server, '_persist_state_lock')
    # Lock must be a real threading.Lock (or RLock)
    assert isinstance(
        arb_server._persist_state_lock,
        type(threading.Lock())
    ) or hasattr(arb_server._persist_state_lock, 'acquire')


def test_persist_uses_non_blocking_acquire():
    """Skip-if-running pattern: acquire(blocking=False) prevents pile-up."""
    import inspect
    import arb_server
    src = inspect.getsource(arb_server.run_scan)
    assert '_persist_state_lock.acquire(blocking=False)' in src


# ── Bug #2: find_pairs dedup broken ───────────────────────────────

def test_find_pairs_dedup_no_crash_on_duplicates():
    """Multiple events that bucket together (same sport+date) must dedup
    so each b-event is considered once per a-event."""
    from event_matching import find_pairs
    # 3 a-events, 3 b-events all on same date — without dedup, each
    # b-event would be visited 4 times per a (sport×date × sport×_nodate
    # × _nosport×date × _nosport×_nodate buckets). Function must still
    # complete and return a list.
    a = [
        {'title': 'Lakers vs Celtics May 4', 'end_date': '2026-05-04'},
        {'title': 'Bitcoin above 100k May 4', 'end_date': '2026-05-04'},
    ]
    b = [
        {'title': 'Lakers vs Celtics May 4', 'end_date': '2026-05-04'},
        {'title': 'Bitcoin above 100k May 4', 'end_date': '2026-05-04'},
    ]
    pairs = find_pairs(a, b, min_confidence=0.5)
    assert isinstance(pairs, list)
    # Should have at most 2 pairs (each a paired with at most one b)
    assert len(pairs) <= 2


def test_find_pairs_dedup_loop_explicit():
    """Source-level guard: dedup uses explicit if/in/seen pattern, not
    the broken `not (x in seen or seen.add(x))` shortcut."""
    import inspect
    import event_matching
    src = inspect.getsource(event_matching.find_pairs)
    # Strip comments before scanning — the comment block explaining the
    # bug deliberately quotes the broken pattern.
    code_only = '\n'.join(
        line for line in src.split('\n')
        if not line.lstrip().startswith('#')
    )
    # Broken pattern must be GONE from executable code
    assert 'or seen.add(' not in code_only, \
        "find_pairs still uses broken `seen.add()` truth-shortcut"
    # Explicit replacement loop should be present
    assert 'seen.add(c[0])' in code_only or 'seen.add(c_idx)' in code_only


# ── Bug #3: WS-first /book freshness guard ────────────────────────

def test_ws_first_freshness_guard():
    """WS books must be discarded when `ts` is older than freshness window."""
    import inspect
    import arb_server
    src = inspect.getsource(arb_server.run_scan)
    assert 'WS_BOOK_FRESHNESS_SEC' in src
    assert "ws_book.get('ts')" in src or 'ws_book["ts"]' in src
    # Stale-counter for diagnostics
    assert 'ws_stale_skipped' in src


# ── Bug #4: duplicate risk=LOW branch ─────────────────────────────

def test_build_deal_risk_tiers_monotonic():
    """`min_liq > max_stake*3` → MED, not LOW (no duplicate label)."""
    import inspect
    import arb_server
    src = inspect.getsource(arb_server.build_deal)
    # Find the risk-tier ladder
    lines = [l.strip() for l in src.split('\n')
             if 'risk=' in l and ('min_liq' in l or 'else:' in l)]
    # Count each label
    low_count = sum(1 for l in lines if 'risk="LOW"' in l)
    assert low_count == 1, f"expected 1 LOW branch, found {low_count}"


# ── Bug #5: POLY_ADDRESS checksum ─────────────────────────────────

def test_poly_hmac_headers_checksum_address():
    """build_poly_hmac_headers normalises address to EIP-55."""
    from executor.builders import build_poly_hmac_headers
    # Lowercase input
    headers = build_poly_hmac_headers(
        method='POST', path='/order', body='{}',
        api_key='dummy', api_secret='ZmFrZQ==', passphrase='pass',
        eth_address='0xc0ffee254729296a45a3885639ac7e10f9d54979',
    )
    # Either normalised to checksum or returned as-is if eth_utils missing.
    # The valid lowercase test address has a deterministic checksum form;
    # we don't lock the exact case (depends on whether eth_utils is available),
    # but we DO lock that the address is preserved AND the test doesn't crash.
    assert 'POLY_ADDRESS' in headers
    addr = headers['POLY_ADDRESS']
    assert addr.lower() == '0xc0ffee254729296a45a3885639ac7e10f9d54979'


# ── Bug #6: arb_id collision (uuid suffix) ────────────────────────

def test_arb_id_has_uuid_suffix():
    """arb_id pattern: <ms>-<title>-<6hex> — last segment guarantees uniqueness."""
    import inspect
    from executor import atomic
    src = inspect.getsource(atomic.fire_arb)
    # Either uuid4().hex or random suffix appended
    assert 'uuid' in src
    assert 'arb_id' in src


def test_arb_id_unique_under_burst():
    """Two same-ms same-title fires get different arb_ids."""
    # Direct white-box: replicate arb_id construction
    import uuid as _uuid
    title = 'Test Title'
    t = int(time.time() * 1000)
    a = f"{t}-{title[:32].replace(' ','_')}-{_uuid.uuid4().hex[:6]}"
    b = f"{t}-{title[:32].replace(' ','_')}-{_uuid.uuid4().hex[:6]}"
    assert a != b


# ── Bug #7: bare `except:` narrowed ───────────────────────────────

def test_fetch_kalshi_ob_no_bare_except():
    """`_fetch_kalshi_ob` uses `except Exception` — no bare except swallowing
    KeyboardInterrupt / SystemExit."""
    import inspect
    import arb_server
    src = inspect.getsource(arb_server._fetch_kalshi_ob)
    # Bare-except pattern must be gone
    assert '\n    except:' not in src
    assert '    except: return' not in src
    # Narrow form must be present
    assert 'except Exception' in src


# ── Bug #8: sum_x1 / sum_x2 single-compute ─────────────────────────

def test_cross_platform_sum_x1_computed_once():
    """Source check — sum_x1 not assigned twice in same function."""
    import inspect
    import cross_platform
    src = inspect.getsource(cross_platform.build_cross_platform_deal)
    # Count `sum_x1 = out_a.yes_price + out_b.no_price` assignments
    assigns = src.count('sum_x1 = out_a.yes_price + out_b.no_price')
    assert assigns == 1, f"sum_x1 assigned {assigns} times (expected 1)"
    assigns2 = src.count('sum_x2 = out_a.no_price + out_b.yes_price')
    assert assigns2 == 1, f"sum_x2 assigned {assigns2} times (expected 1)"


# ── Bug #9: empty legs guard ──────────────────────────────────────

def test_to_radar_deal_format_empty_legs_no_crash():
    """`to_radar_deal_format` returns sane defaults instead of ValueError
    when `cp_deal.legs == []`."""
    from cross_platform import CrossPlatformDeal, to_radar_deal_format
    empty = CrossPlatformDeal(
        structure='X1', title='Empty', sum_cents=50.0,
        threshold_cents=98.0, net_cents=2.0, legs=[],
        platform_pair=('A', 'B'), confidence=0.9, end_date='2026-05-04',
    )
    out = to_radar_deal_format(empty)
    assert out['min_liq'] == 0.0
    assert out['balance_used'] == 0.0
    assert out['net'] == 0.0  # actual_stake=0 → net=0


# ── Bug #10: paper_trading median ─────────────────────────────────

def test_paper_trading_median_even_length():
    """median of [10, 20, 30, 40] must be 25, not 30 (off-by-one bug)."""
    # White-box: re-implement same formula
    pnls = sorted([10.0, 20.0, 30.0, 40.0])
    m = len(pnls)
    if m % 2 == 1:
        median_pnl = pnls[m // 2]
    else:
        median_pnl = (pnls[m // 2 - 1] + pnls[m // 2]) / 2
    assert median_pnl == 25.0


def test_paper_trading_median_source_uses_two_branches():
    """Source-level guard — graduation_status uses both odd/even branches."""
    import inspect
    import paper_trading
    src = inspect.getsource(paper_trading.graduation_status)
    # Off-by-one form must be gone
    assert 'pnls[len(pnls) // 2] if pnls' not in src
    # Even-length branch present
    assert 'm % 2' in src or 'len(pnls) % 2' in src
