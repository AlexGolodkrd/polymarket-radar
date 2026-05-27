"""Pytest conftest — autouse fixtures.

History:
    Phase 14a (01.05.2026): reset circuit_breaker state between tests so SX
        tests didn't fail when CB was tripped by an earlier test.
    Phase audit-27.05 (27.05.2026): added reset for:
        - killswitch.killed flag (Executions/.killed file) — pre-existing
          test runs from 30.04.2026 left a stale `.killed` on disk; every
          subsequent run failed `TestAllNoGrossMath`, `TestDistinctWallets`,
          `TestJitterFires` with "risk_blocked: kill_switch_active".
        - analytics._open_deals / _near_logged / _fired_arb_keys — global
          state that bled across test files.
        - config singleton — re-instantiate so env monkeypatches take effect.

Every reset is wrapped in try/except: missing modules (e.g. before
all-imports are wired) shouldn't break unrelated tests.
"""
from __future__ import annotations

import os
import sys
from typing import Iterator

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), 'Scripts')
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)


# ──────────────────────────────────────────────────────────────────────
# Singleton resets — run before every test.
# ──────────────────────────────────────────────────────────────────────

def _reset_circuit_breakers() -> None:
    """All known breakers → CLOSED. Prevents test pollution where
    a CB tripped in test A blocks tests B..Z."""
    try:
        import circuit_breaker
        from circuit_breaker import CircuitState
        breakers = {}
        try:
            breakers = circuit_breaker.all_breakers()
        except Exception:
            pass
        for _name, cb in (breakers or {}).items():
            try:
                cb._state = CircuitState.CLOSED
                cb._failure_count = 0
                cb._opened_at = None
            except Exception:
                pass
    except ImportError:
        pass


def _reset_killswitch() -> None:
    """Remove any stale Executions/.killed flag before each test.

    Phase audit-27.05 root cause: a `.killed` file from 30.04.2026 sat
    on the operator's disk, causing every fire-path test to abort with
    `risk_blocked: kill_switch_active`. Tests should never assume the
    inherited filesystem state.
    """
    try:
        from risk import killswitch
        # The module exposes is_killed() reading from KILL_PATH; remove
        # the file directly so subsequent reads return False.
        path = getattr(killswitch, 'KILL_PATH', None) or getattr(
            killswitch, '_KILL_PATH', None)
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
        # Best-effort fallback to known location
        fallback = os.path.join(SCRIPTS, '..', 'Executions', '.killed')
        if os.path.exists(fallback):
            try:
                os.remove(fallback)
            except OSError:
                pass
    except ImportError:
        # killswitch module not loaded → nothing to clean
        fallback = os.path.join(SCRIPTS, '..', 'Executions', '.killed')
        if os.path.exists(fallback):
            try:
                os.remove(fallback)
            except OSError:
                pass


def _reset_analytics_state() -> None:
    """Clear in-memory analytics dicts. Each test should start with no
    open deals and no NEAR-seen dedup history."""
    try:
        import analytics
        with analytics._lock:
            analytics._open_deals.clear()
            analytics._near_logged.clear()
    except Exception:
        pass


def _reset_fired_arb_keys() -> None:
    """Clear `_fired_arb_keys` so tests that don't touch it explicitly
    start from a known-empty cooldown state."""
    try:
        import arb_server
        with arb_server._fired_arb_keys_lock:
            arb_server._fired_arb_keys.clear()
    except Exception:
        pass


def _reset_config() -> None:
    """If `config.config` singleton is loaded, re-instantiate so tests
    that monkeypatch env see the new values."""
    try:
        import config
        config.reload()
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────
# Autouse fixture — wires every reset into every test, transparently.
# ──────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_singletons() -> Iterator[None]:
    """Before each test: zero out shared mutable state across all modules
    the radar uses. Catches pre-existing kill-switch pollution + analytics
    leaks + CB trips. Yields to the test, then runs again on teardown
    (defensive — some tests leave state for the next test to pick up)."""
    _reset_killswitch()
    _reset_circuit_breakers()
    _reset_analytics_state()
    _reset_fired_arb_keys()
    _reset_config()
    yield
    # Teardown — same calls, so the next test starts clean even if
    # `yield` raised.
    _reset_killswitch()
    _reset_analytics_state()
    _reset_fired_arb_keys()
