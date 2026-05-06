"""Phase 19v31 (06.05.2026) — env overrides for `_quality_ok` thresholds.

Operator's stated open-item: the post-9gg defaults
    Polymarket tight arb (sum ≥ 95¢) → min_liq ≥ $600, slip_pct < 0.3
    Limitless tight arb (sum ≥ 95¢)  → min_liq ≥ $130, slip_pct < 0.3
were hardcoded. With paper_stats stuck at count=0, operator may want to
relax the gate temporarily to surface more candidates for evaluation
(or tighten further if too many wash deals come through).

v31 lifts those four numbers into env vars (`QUALITY_TIGHT_*`) that the
operator can flip in `Credentials.env` without a code redeploy. Defaults
match prior behavior, so a missing env var is a strict no-op.

Test strategy: import arb_server once at module level (defaults loaded),
then monkeypatch the constants per test. This avoids the Windows-specific
pytest issue where reloading `arb_server` retriggers `_bootstrap_radar()`
prints into a closed pytest capture buffer (ValueError I/O closed file).
The env→constant binding itself is verified by a separate subprocess
test below.
"""
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


# ── Defaults exist on the module (post-9gg parity) ─────────────────

def test_constants_exist_with_defaults():
    import arb_server as m
    assert hasattr(m, 'QUALITY_TIGHT_CUTOFF_CENTS')
    assert hasattr(m, 'QUALITY_TIGHT_MIN_LIQ')
    assert hasattr(m, 'QUALITY_LIM_TIGHT_MIN_LIQ')
    assert hasattr(m, 'QUALITY_TIGHT_MAX_SLIP')
    # Defaults should match the post-9gg behavior. If a future release
    # changes the default, this test is the canary that surfaces it.
    assert m.QUALITY_TIGHT_CUTOFF_CENTS == 95.0
    assert m.QUALITY_TIGHT_MIN_LIQ == 600.0
    assert m.QUALITY_LIM_TIGHT_MIN_LIQ == 130.0
    assert m.QUALITY_TIGHT_MAX_SLIP == 0.3


# ── _lim_quality_ok respects the constants (gate behavior) ─────────

def test_lim_quality_blocks_at_default(monkeypatch):
    """Tight Limitless deal with min_liq=$50 blocked at default $130."""
    import arb_server as m
    deal = {'total_cents': 96.0, 'min_liq': 50, 'slip_pct': 0.1}
    pseudo_pm = [{'volume': 100}]
    assert not m._lim_quality_ok(deal, pseudo_pm)


def test_lim_quality_passes_after_relax(monkeypatch):
    """Same deal passes if QUALITY_LIM_TIGHT_MIN_LIQ relaxed to $40."""
    import arb_server as m
    monkeypatch.setattr(m, 'QUALITY_LIM_TIGHT_MIN_LIQ', 40.0)
    deal = {'total_cents': 96.0, 'min_liq': 50, 'slip_pct': 0.1}
    pseudo_pm = [{'volume': 100}]
    assert m._lim_quality_ok(deal, pseudo_pm)


def test_lim_quality_loose_arb_unaffected(monkeypatch):
    """Loose arb (sum<cutoff) is NOT gated regardless of min_liq."""
    import arb_server as m
    deal = {'total_cents': 80.0, 'min_liq': 1, 'slip_pct': 0.1}
    pseudo_pm = [{'volume': 100}]
    assert m._lim_quality_ok(deal, pseudo_pm)


def test_lim_quality_blocks_high_slippage(monkeypatch):
    """Tight deal with slip_pct ≥ default 0.3 → blocked even at fat depth."""
    import arb_server as m
    deal = {'total_cents': 97.0, 'min_liq': 5000, 'slip_pct': 0.35}
    pseudo_pm = [{'volume': 100}]
    assert not m._lim_quality_ok(deal, pseudo_pm)


def test_lim_quality_blocks_high_slippage_relaxed(monkeypatch):
    """Same deal passes when QUALITY_TIGHT_MAX_SLIP raised to 0.5."""
    import arb_server as m
    monkeypatch.setattr(m, 'QUALITY_TIGHT_MAX_SLIP', 0.5)
    deal = {'total_cents': 97.0, 'min_liq': 5000, 'slip_pct': 0.35}
    pseudo_pm = [{'volume': 100}]
    assert m._lim_quality_ok(deal, pseudo_pm)


def test_lim_quality_cutoff_change_widens_gate(monkeypatch):
    """Lowering cutoff to 80 makes a previously-loose 85¢ arb gated."""
    import arb_server as m
    monkeypatch.setattr(m, 'QUALITY_TIGHT_CUTOFF_CENTS', 80.0)
    # 85 ≥ 80 → tight gate engages → min_liq=50 < default 130 → block
    deal = {'total_cents': 85.0, 'min_liq': 50, 'slip_pct': 0.1}
    pseudo_pm = [{'volume': 100}]
    assert not m._lim_quality_ok(deal, pseudo_pm)


def test_lim_quality_all_dead_volume_still_blocks(monkeypatch):
    """Phase 9gg's all-dead-volume kill is independent of env."""
    import arb_server as m
    monkeypatch.setattr(m, 'QUALITY_LIM_TIGHT_MIN_LIQ', 0.0)
    deal = {'total_cents': 80.0, 'min_liq': 1000, 'slip_pct': 0.05}
    pseudo_pm = [{'volume': 0}, {'volume': 0}]
    assert not m._lim_quality_ok(deal, pseudo_pm)


# ── Env→constant binding (subprocess to avoid stdout teardown bug) ─

def _run_env_check(env_overrides):
    """Spawn a fresh Python that imports arb_server and prints the four
    QUALITY_* constants. Returns dict {name: float}."""
    env = os.environ.copy()
    for k in ('QUALITY_TIGHT_CUTOFF_CENTS', 'QUALITY_TIGHT_MIN_LIQ',
              'QUALITY_LIM_TIGHT_MIN_LIQ', 'QUALITY_TIGHT_MAX_SLIP'):
        env.pop(k, None)
    env.update(env_overrides)
    repo_root = os.path.dirname(HERE)
    out = subprocess.check_output(
        [sys.executable, '-c',
         "import sys; sys.path.insert(0, 'Scripts'); "
         "import arb_server as m; "
         "print(m.QUALITY_TIGHT_CUTOFF_CENTS, m.QUALITY_TIGHT_MIN_LIQ, "
         "m.QUALITY_LIM_TIGHT_MIN_LIQ, m.QUALITY_TIGHT_MAX_SLIP)"],
        cwd=repo_root, env=env, stderr=subprocess.STDOUT, timeout=30,
    ).decode()
    last = out.strip().splitlines()[-1].split()
    return {
        'QUALITY_TIGHT_CUTOFF_CENTS': float(last[0]),
        'QUALITY_TIGHT_MIN_LIQ': float(last[1]),
        'QUALITY_LIM_TIGHT_MIN_LIQ': float(last[2]),
        'QUALITY_TIGHT_MAX_SLIP': float(last[3]),
    }


def test_env_default_no_overrides():
    out = _run_env_check({})
    assert out == {
        'QUALITY_TIGHT_CUTOFF_CENTS': 95.0,
        'QUALITY_TIGHT_MIN_LIQ': 600.0,
        'QUALITY_LIM_TIGHT_MIN_LIQ': 130.0,
        'QUALITY_TIGHT_MAX_SLIP': 0.3,
    }


def test_env_min_liq_override():
    out = _run_env_check({'QUALITY_TIGHT_MIN_LIQ': '200'})
    assert out['QUALITY_TIGHT_MIN_LIQ'] == 200.0
    # Other defaults preserved
    assert out['QUALITY_LIM_TIGHT_MIN_LIQ'] == 130.0


def test_env_lim_min_liq_independent():
    out = _run_env_check({'QUALITY_LIM_TIGHT_MIN_LIQ': '50'})
    assert out['QUALITY_LIM_TIGHT_MIN_LIQ'] == 50.0
    assert out['QUALITY_TIGHT_MIN_LIQ'] == 600.0


def test_env_cutoff_and_slip_override():
    out = _run_env_check({
        'QUALITY_TIGHT_CUTOFF_CENTS': '90',
        'QUALITY_TIGHT_MAX_SLIP': '0.5',
    })
    assert out['QUALITY_TIGHT_CUTOFF_CENTS'] == 90.0
    assert out['QUALITY_TIGHT_MAX_SLIP'] == 0.5
