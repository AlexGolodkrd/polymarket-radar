"""Event matching for cross-platform arbitrage (Phase 12, 01.05.2026).

Same real-world event has DIFFERENT titles across markets:
  Polymarket: "Will the Los Angeles Lakers beat the Boston Celtics?"
  Limitless:  "Lakers vs Celtics — March 25"
  SX Bet:     "LAL @ BOS"

This module normalizes + fuzzy-matches titles + filters by date so
cross-platform arb pairs can be detected reliably.

NOT YET WIRED into the radar — standalone module ready for Phase 13
cross_platform_matcher.py to import.

See `.claude/skills/event-matching-fuzzy/SKILL.md` for design rationale.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, List, Optional, Tuple

# ── Team aliases (extend as needed) ─────────────────────────────────
# Map all known variants to canonical name. Lookup is by lowercase.
NBA_TEAMS = {
    # canonical : list of variants
    'lakers':       ['lal', 'la lakers', 'los angeles lakers'],
    'celtics':      ['bos', 'boston celtics', 'boston'],
    'warriors':     ['gsw', 'golden state', 'golden state warriors'],
    'heat':         ['mia', 'miami heat', 'miami'],
    'nets':         ['bkn', 'brooklyn nets', 'brooklyn'],
    'knicks':       ['nyk', 'new york knicks', 'new york'],
    'clippers':     ['lac', 'la clippers', 'los angeles clippers'],
    'mavericks':    ['dal', 'dallas mavericks', 'dallas mavs', 'mavs'],
    'rockets':      ['hou', 'houston rockets', 'houston'],
    'spurs':        ['sas', 'san antonio spurs', 'san antonio'],
    'thunder':      ['okc', 'oklahoma city thunder', 'oklahoma city'],
    'nuggets':      ['den', 'denver nuggets', 'denver'],
    'suns':         ['phx', 'phoenix suns', 'phoenix'],
    'jazz':         ['uta', 'utah jazz', 'utah'],
    'kings':        ['sac', 'sacramento kings', 'sacramento'],
    'blazers':      ['por', 'portland trail blazers', 'portland'],
    'bucks':        ['mil', 'milwaukee bucks', 'milwaukee'],
    'bulls':        ['chi', 'chicago bulls', 'chicago'],
    'cavaliers':    ['cle', 'cleveland cavaliers', 'cleveland', 'cavs'],
    'pistons':      ['det', 'detroit pistons', 'detroit'],
    'pacers':       ['ind', 'indiana pacers', 'indiana'],
    'hawks':        ['atl', 'atlanta hawks', 'atlanta'],
    'hornets':      ['cha', 'charlotte hornets', 'charlotte'],
    'magic':        ['orl', 'orlando magic', 'orlando'],
    'wizards':      ['was', 'washington wizards', 'washington'],
    '76ers':        ['phi', 'philadelphia 76ers', 'philadelphia', 'sixers'],
    'raptors':      ['tor', 'toronto raptors', 'toronto'],
    'grizzlies':    ['mem', 'memphis grizzlies', 'memphis'],
    'pelicans':     ['nop', 'new orleans pelicans', 'new orleans'],
    'timberwolves': ['min', 'minnesota timberwolves', 'minnesota', 'wolves'],
}

# NFL — minimal subset for now
NFL_TEAMS = {
    'chiefs':   ['kc', 'kansas city chiefs', 'kansas city'],
    'eagles':   ['phi', 'philadelphia eagles'],
    'bills':    ['buf', 'buffalo bills'],
    'cowboys':  ['dal', 'dallas cowboys'],
    '49ers':    ['sf', 'san francisco 49ers', 'sf 49ers', 'niners'],
    'ravens':   ['bal', 'baltimore ravens'],
    'patriots': ['ne', 'new england patriots', 'pats'],
    # extend as needed
}

# Soccer — top leagues
SOCCER_TEAMS = {
    'man united':   ['manchester united', 'man utd', 'utd', 'mufc'],
    'man city':     ['manchester city', 'mcfc'],
    'liverpool':    ['lfc'],
    'arsenal':      ['afc'],
    'chelsea':      ['cfc'],
    'tottenham':    ['spurs', 'thfc'],
    'real madrid':  ['real', 'rmcf'],
    'barcelona':    ['barca', 'fcb'],
    'bayern':       ['bayern munich', 'fc bayern'],
    'psg':          ['paris', 'paris saint germain', 'paris saint-germain'],
    # extend as needed
}

# Combined lookup: variant → (canonical, sport)
_VARIANT_TO_CANONICAL = {}
for sport, dct in (('nba', NBA_TEAMS), ('nfl', NFL_TEAMS), ('soccer', SOCCER_TEAMS)):
    for canonical, variants in dct.items():
        _VARIANT_TO_CANONICAL[canonical] = (canonical, sport)
        for v in variants:
            _VARIANT_TO_CANONICAL[v] = (canonical, sport)


# Noise words to strip during normalization
_NOISE_RE = re.compile(
    r'\b(will|to|win|wins|won|beat|beats|defeat|defeats|cover|covers|score|'
    r'scores|over|under|total|game|match|matchup|vs|versus|the|a|an|and|or|'
    r'of|on|in|at)\b',
    re.IGNORECASE,
)


def normalize_title(title: str) -> str:
    """Lowercase, strip punctuation, drop noise words, collapse whitespace.
    KEEPS digits (dates and counts often matter for matching)."""
    if not title:
        return ''
    s = title.lower()
    s = s.replace('@', ' ').replace('-', ' ').replace('_', ' ')
    s = re.sub(r"[^\w\s]", ' ', s)        # strip remaining punct
    s = _NOISE_RE.sub(' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def canonicalize_teams(normalized: str) -> Tuple[str, Optional[str]]:
    """Replace any team variant with canonical name. Returns
    (canonicalized_string, detected_sport_or_None).

    Walks longest-variants-first to avoid 'la' matching before 'la lakers'.
    """
    sorted_variants = sorted(
        _VARIANT_TO_CANONICAL.keys(),
        key=lambda v: -len(v),                  # longest first
    )
    sport_votes = {}
    out = ' ' + normalized + ' '            # padding for word-boundary match
    for variant in sorted_variants:
        pattern = r'\b' + re.escape(variant) + r'\b'
        canonical, sport = _VARIANT_TO_CANONICAL[variant]
        new_out, n = re.subn(pattern, canonical, out)
        if n > 0:
            out = new_out
            sport_votes[sport] = sport_votes.get(sport, 0) + n
    out = out.strip()
    out = re.sub(r'\s+', ' ', out)
    sport = max(sport_votes, key=sport_votes.get) if sport_votes else None
    return out, sport


# Date extraction — handles MMM DD, MM/DD, YYYY-MM-DD
_MONTHS = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
    'january': 1, 'february': 2, 'march': 3, 'april': 4, 'may': 5,
    'june': 6, 'july': 7, 'august': 8, 'september': 9, 'october': 10,
    'november': 11, 'december': 12,
}


def extract_date(title: str, default_year: Optional[int] = None) -> Optional[date]:
    """Try to extract a calendar date from title. Returns None if no match.

    Patterns tried in order:
      YYYY-MM-DD     → ISO-style
      MMM DD[, YYYY] → "Mar 25" or "March 25, 2026"
      MM/DD[/YYYY]   → "3/25" or "3/25/26"
      DD MMM YYYY    → "25 Mar 2026" (European)
    """
    if default_year is None:
        default_year = datetime.utcnow().year
    s = title.lower()
    # YYYY-MM-DD
    m = re.search(r'\b(20\d\d)\s*[-/]\s*(\d{1,2})\s*[-/]\s*(\d{1,2})\b', s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    # MMM DD[, YYYY]
    m = re.search(r'\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec'
                   r'|january|february|march|april|june|july|august'
                   r'|september|october|november|december)'
                   r'\.?\s+(\d{1,2})(?:[,\s]+(20\d\d))?\b', s)
    if m:
        mon = _MONTHS[m.group(1)]
        d = int(m.group(2))
        y = int(m.group(3)) if m.group(3) else default_year
        try:
            return date(y, mon, d)
        except ValueError:
            pass
    # MM/DD[/YYYY] or MM/DD/YY
    m = re.search(r'\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b', s)
    if m:
        mon = int(m.group(1))
        d = int(m.group(2))
        y = m.group(3)
        if y:
            y = int(y)
            if y < 100: y = 2000 + y
        else:
            y = default_year
        try:
            return date(y, mon, d)
        except ValueError:
            pass
    return None


def title_similarity(a: str, b: str) -> float:
    """Fuzzy similarity 0..1 using SequenceMatcher.
    Inputs should already be normalized + canonicalized."""
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


@dataclass
class MatchCandidate:
    """Result of matching two titles across platforms."""
    confidence: float       # 0..1
    title_similarity: float
    date_match: bool
    sport: Optional[str]
    norm_a: str
    norm_b: str
    date_a: Optional[date]
    date_b: Optional[date]


def match_event(title_a: str, title_b: str,
                end_date_a: Optional[date] = None,
                end_date_b: Optional[date] = None,
                date_tolerance_days: int = 1) -> MatchCandidate:
    """Compare two event titles + optional resolution dates. Returns a
    MatchCandidate with confidence score.

    Use confidence thresholds:
      >= 0.95   → auto-accept
      0.80-0.95 → quarantine (operator review)
      < 0.80    → drop
    """
    norm_a = normalize_title(title_a)
    norm_b = normalize_title(title_b)
    canon_a, sport_a = canonicalize_teams(norm_a)
    canon_b, sport_b = canonicalize_teams(norm_b)
    sim = title_similarity(canon_a, canon_b)

    # Use end_date if provided, else extract from title
    da = end_date_a or extract_date(title_a)
    db = end_date_b or extract_date(title_b)
    if da and db:
        date_match = abs((da - db).days) <= date_tolerance_days
    else:
        # No date available on at least one side → neutral
        date_match = False if (da or db) else True   # both missing = neutral OK

    sport = sport_a if sport_a == sport_b and sport_a else None

    confidence = (
        sim * 0.6 +
        (1.0 if date_match else 0.0) * 0.3 +
        (0.1 if sport else 0.0)                  # bonus for cross-sport agreement
    )
    confidence = min(1.0, confidence)
    return MatchCandidate(
        confidence=confidence,
        title_similarity=sim,
        date_match=date_match,
        sport=sport,
        norm_a=canon_a,
        norm_b=canon_b,
        date_a=da,
        date_b=db,
    )


def find_pairs(events_a: Iterable[dict], events_b: Iterable[dict], *,
                title_key: str = 'title',
                end_date_key: str = 'end_date',
                min_confidence: float = 0.80) -> List[Tuple[dict, dict, MatchCandidate]]:
    """Greedy pairwise matching of two event lists. For each event in a,
    find the best match in b; if confidence >= min_confidence, add to pairs.

    Each event_b is paired AT MOST ONCE with the highest-confidence event_a.
    """
    list_b = list(events_b)
    used_b_ids = set()
    pairs = []
    for ea in events_a:
        best = None
        best_idx = None
        for i, eb in enumerate(list_b):
            if i in used_b_ids:
                continue
            mc = match_event(
                ea.get(title_key, ''), eb.get(title_key, ''),
                end_date_a=_parse_date(ea.get(end_date_key)),
                end_date_b=_parse_date(eb.get(end_date_key)),
            )
            if mc.confidence >= min_confidence:
                if best is None or mc.confidence > best.confidence:
                    best = mc
                    best_idx = i
        if best is not None:
            pairs.append((ea, list_b[best_idx], best))
            used_b_ids.add(best_idx)
    return pairs


def _parse_date(val) -> Optional[date]:
    """Parse various date inputs. Accepts date, ISO string, datetime."""
    if val is None:
        return None
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, str):
        try:
            return datetime.fromisoformat(val.replace('Z', '+00:00')).date()
        except Exception:
            try:
                return datetime.strptime(val[:10], '%Y-%m-%d').date()
            except Exception:
                return None
    return None
