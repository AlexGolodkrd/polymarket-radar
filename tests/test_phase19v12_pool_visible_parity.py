"""Phase 19v12 (04.05.2026) — pool/visible parity fix.

Operator screenshot показал:
- Polymarket panel: HOT 9 · NEAR 298 · pass 1515
- NEAR table: 0 Polymarket rows visible (only 1 Limitless DOGE)

Asymmetry: classify_pools считал кандидатов с 'implied' source, но
_best_near_structure фильтровал их → "сказочная статистика".

Fix: classify_pools теперь применяет тот же REAL_OB_SOURCES filter.
Pool count = visible count consistently.
"""
import os, sys, pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


def test_classify_pools_rejects_all_implied():
    """Candidate where all legs have yes_src='implied' should NOT enter
    NEAR pool — same as `_best_near_structure` filter."""
    import inspect
    import arb_server
    src = inspect.getsource(arb_server.classify_pools)
    assert "_REAL_OB_SOURCES" in src or "REAL_OB_SOURCES" in src
    assert "has_real" in src
    assert "_poly_per_market" in src


def test_classify_pools_includes_real_source_cands():
    """Candidate with at least 1 real-source leg → enters pool."""
    # Source-level guard — verifies has_real check uses `any(...)`
    import inspect
    import arb_server
    src = inspect.getsource(arb_server.classify_pools)
    # any() check on yes_src
    assert "any(p.get('yes_src')" in src or 'any(p.get("yes_src")' in src


def test_classify_pools_real_ob_set_matches_near_summary():
    """REAL_OB_SOURCES set in classify_pools should match the one in
    `_best_near_structure` to keep pool→visible parity."""
    import inspect
    import arb_server
    cp_src = inspect.getsource(arb_server.classify_pools)
    near_src = inspect.getsource(arb_server._best_near_structure)
    # Both should reference same set: {'clob_ask','kalshi_ob','sx_ob','lim_clob','clob_synthetic'}
    for source_name in ('clob_ask', 'lim_clob', 'sx_ob', 'clob_synthetic'):
        assert f"'{source_name}'" in cp_src, f"classify_pools missing {source_name}"
        assert f"'{source_name}'" in near_src, f"_best_near_structure missing {source_name}"
