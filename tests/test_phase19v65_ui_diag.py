"""Phase 19v6.5 (03.05.2026) — UI platform panel + NEAR rejection diagnostics.

Two changes:
1. **Platform Status Panel** in dashboard.html — Poly/Lim/SX cards with
   pool counts + health dot. Adds SX widget that was missing.
2. **NEAR rejection counters** — `_last_near_rejection_stats` tracks why
   raw NEAR pool (e.g. 376) doesn't match visible NEAR (e.g. 0).
   Surfaced via `payload['near_diag']` in /api/deals.
"""
import os, sys, pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


def test_dashboard_has_platform_panel():
    """dashboard.html contains Phase 19v6 platform-status cards."""
    here = os.path.dirname(os.path.abspath(__file__))
    dash_path = os.path.join(os.path.dirname(here), 'Scripts', 'dashboard.html')
    with open(dash_path, 'r', encoding='utf-8') as f:
        text = f.read()
    # All 3 platform cards present
    assert 'data-platform="poly"' in text
    assert 'data-platform="lim"' in text
    assert 'data-platform="sx"' in text
    # SX-specific status display ids
    assert 'platSxMarkets' in text
    assert 'platSxBinary' in text
    assert 'platSxHttp' in text
    # Health dots
    assert 'platDotPoly' in text
    assert 'platDotLim' in text
    assert 'platDotSx' in text


def test_dashboard_has_sx_widget_in_header():
    """Phase 19v6 — SX widget added to header (was missing before)."""
    here = os.path.dirname(os.path.abspath(__file__))
    dash_path = os.path.join(os.path.dirname(here), 'Scripts', 'dashboard.html')
    with open(dash_path, 'r', encoding='utf-8') as f:
        text = f.read()
    assert 'sxWidget' in text
    assert 'sxState' in text


def test_near_rejection_stats_exists():
    """`_last_near_rejection_stats` is a module-level global."""
    import arb_server
    assert hasattr(arb_server, '_last_near_rejection_stats')
    assert isinstance(arb_server._last_near_rejection_stats, dict)


def test_near_rejection_stats_keys():
    """near_summary populates expected diagnostic keys."""
    import arb_server
    # Force a near_summary call with empty pools (avoid network) — should
    # still set _last_near_rejection_stats with all keys at 0.
    arb_server.near_summary(clob_res={}, kalshi_res={}, sx_res={}, lim_res={})
    diag = arb_server._last_near_rejection_stats
    assert isinstance(diag, dict)
    expected_keys = {
        'poly_raw', 'poly_visible', 'poly_rejected_quarantine',
        'poly_rejected_zombie', 'poly_rejected_strict',
        'lim_raw', 'lim_visible', 'lim_rejected_strict',
        'sx_raw', 'sx_visible', 'sx_rejected_strict',
        'total_visible',
    }
    for k in expected_keys:
        assert k in diag, f'missing diag key: {k}'


def test_api_deals_includes_near_diag():
    """api_deals payload includes `near_diag` when stats populated."""
    import arb_server
    # Trigger near_summary to populate diag
    arb_server.near_summary(clob_res={}, kalshi_res={}, sx_res={}, lim_res={})
    # Call api_deals and check payload contains near_diag
    with arb_server.app.test_client() as client:
        resp = client.get('/api/deals')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'near_diag' in data, "api/deals must include near_diag for UI"
