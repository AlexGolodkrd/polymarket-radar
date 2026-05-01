"""Pytest conftest — autouse fixtures.

Phase 14a (01.05.2026): reset circuit_breaker state between tests so SX
tests don't fail when CB was tripped by an earlier test (singleton state).
"""
import os
import sys
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), 'Scripts')
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)


@pytest.fixture(autouse=True)
def _reset_circuit_breakers():
    """Before every test, reset all known circuit breakers to CLOSED.
    Prevents test pollution where CB tripped in one test blocks the next.
    """
    try:
        import circuit_breaker
        from circuit_breaker import CircuitState
        # Reset every breaker in the registry
        try:
            breakers = circuit_breaker.all_breakers()
        except Exception:
            breakers = {}
        for name, cb in (breakers or {}).items():
            try:
                cb._state = CircuitState.CLOSED
                cb._failure_count = 0
                cb._opened_at = None
            except Exception:
                pass
    except ImportError:
        pass
    yield
