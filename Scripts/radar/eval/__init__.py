"""Per-platform arb evaluators.

Each evaluator takes filtered candidates + orderbook fetch results and
emits deal dicts (matching the radar's UI/analytics shape). All deals
go through `radar.build_deal.build_deal` for sizing + grade + economics.

Status:
    audit-28b cont 3 (28.05.2026):
        + radar.eval.sx — SX Bet binary evaluator + 3way stub

Pending:
    radar.eval.polymarket — eval_poly (~250 lines, depends on
        _poly_per_market + _attach_poly_v2_meta + _quality_ok)
    radar.eval.limitless — eval_limitless (~270 lines, depends on
        _lim_quality_ok + _resolve_lim_end_date)
"""
