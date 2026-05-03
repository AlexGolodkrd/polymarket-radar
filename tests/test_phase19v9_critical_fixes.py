"""Phase 19v9 (03.05.2026) — CRITICAL hotfix tests.

Two production bugs found via stats analysis at active hours:

1. **SX status field is STRING not INT.** SX API returns
   `'ACTIVE'`/`'SETTLED'`/`'CANCELLED'`, not numeric `1`. Old check
   `status != 1` rejected ALL 934 markets (sx_pass=0, sx_skip_status=934).

2. **Limitless NEAR loop missed `yes_src`/`no_src` fields.**
   `_best_near_structure`'s REAL_OB_SOURCES filter rejected ALL Lim NEAR
   candidates because `p.get('yes_src')` was None. pool_lim_near=17 raw
   yet UI shows 0.
"""
import os, sys, pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


def test_filter_sx_accepts_string_active_status():
    """SX returns status='ACTIVE' (string). Old code required int 1."""
    from arb_server import filter_sx
    markets = [
        {'marketHash': '0x1', 'status': 'ACTIVE', 'gameTime': int(__import__('time').time()) + 7200,
         'type': 52, 'outcome': None, 'outcomeOneName': 'Lakers',
         'outcomeTwoName': 'Celtics'},
        {'marketHash': '0x2', 'status': 'SETTLED', 'gameTime': int(__import__('time').time()) + 7200,
         'type': 52, 'outcome': 1, 'outcomeOneName': 'A', 'outcomeTwoName': 'B'},
        {'marketHash': '0x3', 'status': 1, 'gameTime': int(__import__('time').time()) + 7200,
         'type': 52, 'outcome': None, 'outcomeOneName': 'C', 'outcomeTwoName': 'D'},
    ]
    diag = {}
    out = filter_sx(markets, diag=diag)
    # Active string + legacy int 1 should pass; SETTLED + outcome=1 reject
    assert len(out) == 2
    assert diag['sx_pass'] == 2
    assert diag['sx_skip_status'] == 1   # only the SETTLED one


def test_filter_sx_lowercase_active_also_works():
    """Defensive: lowercase 'active' also accepted."""
    from arb_server import filter_sx
    markets = [
        {'marketHash': '0x1', 'status': 'active', 'gameTime': int(__import__('time').time()) + 7200,
         'type': 52, 'outcome': None, 'outcomeOneName': 'A',
         'outcomeTwoName': 'B'},
    ]
    diag = {}
    out = filter_sx(markets, diag=diag)
    assert len(out) == 1


def test_filter_sx_rejects_unknown_status():
    """Unknown / paused / cancelled statuses still rejected."""
    from arb_server import filter_sx
    markets = [
        {'marketHash': '0x1', 'status': 'PAUSED', 'gameTime': int(__import__('time').time()) + 7200,
         'type': 52, 'outcome': None, 'outcomeOneName': 'X', 'outcomeTwoName': 'Y'},
        {'marketHash': '0x2', 'status': None, 'gameTime': int(__import__('time').time()) + 7200,
         'type': 52, 'outcome': None, 'outcomeOneName': 'X', 'outcomeTwoName': 'Y'},
    ]
    diag = {}
    out = filter_sx(markets, diag=diag)
    assert len(out) == 0
    assert diag['sx_skip_status'] == 2


def test_eval_sx_accepts_active_string():
    """eval_sx (belt-and-suspenders status check) also accepts 'ACTIVE'."""
    import inspect
    import arb_server
    src = inspect.getsource(arb_server.eval_sx)
    # Must NOT have bare `if status != 1:` (string-incompat)
    # Should accept tuple of valid statuses
    assert "(1, 'ACTIVE', 'active')" in src or \
           "{1, 'ACTIVE', 'active'}" in src or \
           "in (1, 'ACTIVE'" in src


def test_lim_near_summary_sets_source_fields():
    """Phase 19v9 fix: Limitless near_summary `pm` dicts now have yes_src
    and no_src='lim_clob' so _best_near_structure REAL_OB_SOURCES passes."""
    import inspect
    import arb_server
    src = inspect.getsource(arb_server.near_summary)
    # Source field assignment after lim_res unpacking
    assert "'yes_src': 'lim_clob'" in src
    assert "'no_src': 'lim_clob'" in src or "'no_src': 'lim_clob' if" in src
