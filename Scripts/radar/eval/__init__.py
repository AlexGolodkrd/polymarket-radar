"""Per-platform deal evaluators.

Extracted from arb_server.py in audit-28b cont 3 (PR #251) +
audit-28b cont 6 (28.05.2026). Each module exposes one or more
top-level `eval_*` functions that turn `(candidates, orderbook_cache)`
into a list of arb-deal dicts.
"""
