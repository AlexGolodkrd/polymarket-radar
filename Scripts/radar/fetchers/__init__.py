"""Per-platform REST fetchers (orderbook + market metadata).

Extracted from arb_server.py in audit-28b cont 9 (29.05.2026). Each
sub-module exposes `_fetch_*` functions and any per-platform metadata
helpers (fee schedule parsing, deadline normalisation, etc.).

Cache + session state stays on arb_server.py module level (shared with
WS clients + scan_loop) and is read via lazy imports from inside the
fetcher functions.
"""
