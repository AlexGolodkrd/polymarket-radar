"""Phase 19v30 (06.05.2026) — slug-based threshold-series detection.

Operator screenshot 06.05.2026 — paper-trading dashboard showed a
Limitless deal "SOL price on May 6, 14:00 UTC?" with structure `all_yes`
and Net $156.77 / 1076% ROI. Verification through dryrun.jsonl revealed:

  - leg0: tokenId 24599861... at expected_price 0.056
  - leg1: a different token at expected_price 0.029
  - sum = 0.085 → if ALL_YES were valid, this would be a real arb
    (gross payout $1, cost $0.085, net 91.5%). But the legs are NOT
    mutually exclusive — both are "SOL above $X" / "SOL above $Y"
    binary markets. SOL > max(X, Y) → both YES win. SOL < min(X, Y)
    → no leg wins → loss of full stake.

The radar's existing `is_threshold_series` guard (Phase 9o) was supposed
to catch this. It missed because:

  - The PARENT title is "SOL price on May 6, 14:00 UTC?" — no comparator
    word → THRESHOLD_SERIES_RE didn't match.
  - The secondary signal (every child title starts with "above"/"below")
    failed because Limitless's child *titles* for SOL events apparently
    show just the threshold value ("$887.53") without a comparator word.
  - The SLUGS, however, do encode it: `sol-above-dollar88753-...`. v30
    adds slug-based detection as a tertiary signal: if every child slug
    contains the same `*-above-*` / `*-below-*` / `*-over-*` /
    `*-under-*` segment, it's a threshold series and ALL_YES / ALL_NO
    are invalid (YES_NO_PAIR per child remains valid).
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


# ── New tertiary signal: slug-based detection ──────────────────────

def test_slug_above_pattern_detected_as_threshold_series():
    """Operator's exact reproduction. Parent title is generic, child
    slugs all share `-above-`, no child titles → must be flagged."""
    from arb_server import is_threshold_series
    parent = 'SOL price on May 6, 14:00 UTC?'
    slugs = [
        'sol-above-dollar88753-on-may-6-1400-utc-1778072400184',
        'sol-above-dollar90000-on-may-6-1400-utc-1778072400185',
    ]
    assert is_threshold_series(parent, child_titles=None,
                                child_slugs=slugs) is True


def test_slug_below_pattern_detected():
    from arb_server import is_threshold_series
    parent = 'BTC drop check'
    slugs = [
        'btc-below-100k-may-6',
        'btc-below-95k-may-6',
        'btc-below-90k-may-6',
    ]
    assert is_threshold_series(parent, child_slugs=slugs) is True


def test_slug_over_under_pattern_detected():
    from arb_server import is_threshold_series
    assert is_threshold_series('foo', child_slugs=['eth-over-3000', 'eth-over-3500']) is True
    assert is_threshold_series('foo', child_slugs=['eth-under-2000', 'eth-under-1500', 'eth-under-1000']) is True


def test_slug_mixed_comparators_not_flagged():
    """Mixed above/below across children → may be a balanced binary
    (one above + one below) or genuine multi-outcome → DO NOT auto-flag."""
    from arb_server import is_threshold_series
    slugs = [
        'sol-above-dollar88753',
        'sol-below-dollar88753',   # opposite side
    ]
    # Parent has no threshold pattern, so without same-comparator slugs
    # this should NOT flag.
    assert is_threshold_series('SOL price snapshot',
                                child_slugs=slugs) is False


def test_slug_without_comparator_segment_not_flagged():
    """Slugs that don't contain `-above-`/`-below-`/etc. → not a
    threshold series. Normal team-name slugs must pass through."""
    from arb_server import is_threshold_series
    slugs = [
        'lakers-vs-celtics-2026-05-07',
        'lakers-vs-celtics-2026-05-07-tie',
    ]
    assert is_threshold_series('Lakers vs Celtics',
                                child_slugs=slugs) is False


def test_slug_empty_or_missing_not_flagged():
    from arb_server import is_threshold_series
    assert is_threshold_series('foo', child_slugs=[]) is False
    assert is_threshold_series('foo', child_slugs=None) is False
    assert is_threshold_series('foo', child_slugs=[None, None]) is False


def test_slug_partial_coverage_not_flagged():
    """If only SOME children have the comparator segment, don't flag —
    might be a heterogeneous parent group, not a threshold series."""
    from arb_server import is_threshold_series
    slugs = [
        'sol-above-dollar88753',
        'random-other-slug',
    ]
    assert is_threshold_series('SOL price', child_slugs=slugs) is False


# ── Backward compatibility: pre-existing signals still work ────────

def test_parent_title_above_still_caught():
    """Phase 9o behavior preserved — parent title with explicit
    threshold pattern is still flagged regardless of slugs/titles."""
    from arb_server import is_threshold_series
    assert is_threshold_series('Reddit DAUq above ___') is True
    assert is_threshold_series('BTC above $100,000') is True


def test_child_title_secondary_signal_still_works():
    """All child titles starting with 'Above' → still flagged."""
    from arb_server import is_threshold_series
    titles = ['Above 65M', 'Above 70M', 'Above 75M']
    assert is_threshold_series('Reddit Q1 DAUs',
                                child_titles=titles) is True


def test_normal_1x2_event_passes_through():
    """Standard soccer 1X2 event must NOT be flagged: child titles
    don't share comparator, slugs don't have `-above-`/`-below-`."""
    from arb_server import is_threshold_series
    parent = 'Independiente Santa Fe vs SC Corinthians'
    titles = ['Santa Fe', 'Tie', 'Corinthians']
    slugs = [
        'independiente-santa-fe-vs-sc-corinthians-2026-05-07',
        'independiente-santa-fe-vs-sc-corinthians-tie-2026-05-07',
        'independiente-santa-fe-vs-sc-corinthians-corinthians-2026-05-07',
    ]
    assert is_threshold_series(parent, titles, slugs) is False


# ── End-to-end: SOL phantom must not produce a deal ────────────────

def test_sol_phantom_eliminated_via_slug_signal():
    """Operator's 06.05.2026 SOL deal regression. With v30 slug-based
    detection, eval_limitless's threshold_series guard should fire and
    suppress the ALL_YES deal even when parent title and child titles
    miss the comparator word."""
    from arb_server import is_threshold_series
    # Reproduces the live data shape:
    parent = 'SOL price on May 6, 14:00 UTC?'
    # Limitless apparently puts just the threshold value in child name
    titles = ['$887.53', '$900.00']    # no 'above' word
    slugs = [
        'sol-above-dollar88753-on-may-6-1400-utc-1778072400184',
        'sol-above-dollar90000-on-may-6-1400-utc-1778072400184',
    ]
    assert is_threshold_series(parent, titles, slugs) is True
