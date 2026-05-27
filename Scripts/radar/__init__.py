"""plan-kapkan radar package.

This package is the **target home** for the radar logic that currently
lives in the `Scripts/arb_server.py` monolith. The migration is
phased — see `docs/ARCHITECTURE.md` audit-28a→e for the plan.

Each module here exports a small, focused API. Tests import via the
package path (e.g. `from radar.dedup import FireDedup`); legacy tests
that touch `arb_server._fired_arb_keys` keep working because
`arb_server.py` re-exports the symbols.

Phase audit-28a (27.05.2026):
    + `radar.dedup` — fire-deduplication TTL store.
"""
