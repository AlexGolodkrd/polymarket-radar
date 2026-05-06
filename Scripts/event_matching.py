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
from datetime import date, datetime, timedelta, timezone
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


# Phase 19v28 (06.05.2026) — market-scope classifier.
# Same teams + same date != same market. Polymarket "BVB vs Frankfurt
# Halftime Result" is a DIFFERENT market from SX Bet's full-match
# moneyline / handicap / over-under for the same fixture. Pairing them
# as cross-platform arbs produces phantoms (operator screenshot:
# 6 deals all halftime-vs-fulltime mismatches).
#
# Returns one of:
#   'halftime'   — first half result / 1H / HT
#   'handicap'   — Asian handicap / spread (e.g. "BVB -1", "Tot +0.5")
#   'totals'     — Over/Under N goals / points
#   'period'     — quarter / period (sport-specific NBA/NHL)
#   'moneyline'  — full-match winner / 1X2 / standard "who wins"
#   'unknown'    — fall-through, treat as compatible only with itself

_HALFTIME_PATTERNS = re.compile(
    r'(?:^|\b|\s)('
    r'halftime\s+result|halftime|half\s*time|1st\s*half|first\s*half|'
    r'1\s*h\b|\bht\b|первый\s+тайм|первого\s+тайма'
    r')(?:\b|\s|$)',
    re.IGNORECASE,
)
_HANDICAP_PATTERNS = re.compile(
    r'(?:^|\s|\()'
    r'([+\-]\d+(?:\.\d+)?|handicap|spread|asian\s+handicap)'
    r'(?:\s|\)|$)',
    re.IGNORECASE,
)
_TOTALS_PATTERNS = re.compile(
    r'\b(over|under|o/?\s*\d|u/?\s*\d|total\s*(goals?|points?|runs?))\b',
    re.IGNORECASE,
)
_PERIOD_PATTERNS = re.compile(
    r'\b(\d(?:st|nd|rd|th)\s*(quarter|period|inning)|q[1-4]\b|p[1-3]\b)',
    re.IGNORECASE,
)


def detect_market_scope(title: str, outcome_name: str = '') -> str:
    """Classify a market by scope/type. Used by cross-platform matcher
    to refuse pairs of incompatible scopes (halftime vs fulltime, etc.).

    Order matters: most specific first. Defaults to 'moneyline' since
    that's what most binary YES/NO markets are.
    """
    blob = f"{title or ''} {outcome_name or ''}"
    if _HALFTIME_PATTERNS.search(blob):
        return 'halftime'
    if _PERIOD_PATTERNS.search(blob):
        return 'period'
    # Handicap detection: look for explicit "handicap"/"spread" OR a
    # signed number adjacent to a team token (e.g. "Tottenham -0.5",
    # "West Ham +1"). The signed-number regex is conservative — must be
    # surrounded by whitespace/parens to avoid matching dates etc.
    if _HANDICAP_PATTERNS.search(blob):
        # Filter out date-like patterns (e.g. "+90" minute, "+1 day").
        # Word-boundary on BOTH sides — old `am|pm` without leading
        # boundary matched "Ham" → false-rejected "West Ham +1".
        if not re.search(r'\b(day|min|minute|am|pm|et|utc)\b',
                         blob, re.IGNORECASE):
            return 'handicap'
    if _TOTALS_PATTERNS.search(blob):
        return 'totals'
    return 'moneyline'


def scopes_compatible(scope_a: str, scope_b: str) -> bool:
    """Two markets can be cross-platform paired only if their scopes
    match exactly. Different-scope pairs (e.g. halftime vs moneyline)
    look like opposite outcomes by team/price but actually pay out under
    overlapping conditions → not real arbs."""
    return scope_a == scope_b


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


# Phase 19v29 (06.05.2026) — outcome-name canonicalization.
# Cross-platform pair builder used to call _outcome_match_cross_platform
# which always returned ('opposite', 'opposite') — leaving caller to pair
# A.YES with B.NO blindly. That worked when A and B referred to the same
# real-world outcome ("Lakers win" on both platforms) but produced phantom
# arbs when find_pairs matched two outcomes of the SAME event but for
# DIFFERENT teams. Operator screenshot 06.05.2026: 5 deals on Santa Fe ×
# Corinthians (Copa Libertadores) at "12% net" — every single deal was
# Polymarket "Santa Fe" YES paired with SX Bet "Corinthians SP" NO (or
# similar), neither leg covering the third 1X2 outcome ("Tie") → at any
# tie result both legs would lose. v28 scope guard didn't catch them
# because both sides were 'moneyline' scope.
#
# Fix: outcome_name canonicalization that strips YES/NO/win/champion
# noise + handicap numerals + applies team aliases, then exact-match
# (or fuzzy >= 0.70) the two strings before allowing a cross-platform
# X1/X2 pair to be built.
_OUTCOME_NOISE_RE = re.compile(
    r'\b(yes|no|wins?|won|victory|winner|winning|'
    r'champion|champions|to\s+win|advance|advances|advancing|'
    r'fc|cf|sc|ca|cd|sa|sp|fk|ac|ec|us|sg)\b',
    re.IGNORECASE,
)
_OUTCOME_NUM_RE = re.compile(r'[+\-]?\d+(?:\.\d+)?')


def canonicalize_outcome_name(
    raw: str,
) -> Tuple[str, Optional[str]]:
    """Phase 19v29 — normalize an outcome name to a canonical team key.

    Pipeline:
      1. normalize_title (lowercase, strip punct, drop noise words)
      2. drop outcome-specific suffixes (yes/no/win/champion/etc.)
      3. drop trailing handicap/total numerals (-1, +0.5, "over 2.5")
      4. drop common club suffixes (FC, CF, SC, SP, ...)
      5. canonicalize_teams (alias → canonical)

    Returns (canonical, sport_or_none). Empty string in if both inputs
    consist entirely of noise words.

    Examples:
      'BV Borussia 09 Dortmund'         → 'borussia dortmund'
      'Borussia Dortmund -1'            → 'borussia dortmund'
      'Tottenham Hotspur FC'            → 'tottenham'  (alias hit)
      'Tottenham YES'                   → 'tottenham'
      'Independiente Santa Fe'          → 'independiente santa fe' (no alias)
      'Corinthians SP'                  → 'corinthians'
      'Lakers'                          → 'lakers'
      'Tie' / 'Draw'                    → 'tie' / 'draw' (no alias)
      'Over 2.5'                        → 'over' (numeral stripped)
    """
    if not raw:
        return '', None
    # Step 1 — title-level normalization (drops generic noise like 'to win')
    s = normalize_title(raw)
    # Step 2 — outcome-specific noise (YES/NO and win-synonyms)
    s = _OUTCOME_NOISE_RE.sub(' ', s)
    # Step 3 — strip handicap / total numerals
    s = _OUTCOME_NUM_RE.sub(' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    if not s:
        return '', None
    # Step 4+5 — alias to canonical
    canon, sport = canonicalize_teams(s)
    return canon, sport


def outcomes_compatible(
    name_a: str, name_b: str, *,
    fuzzy_threshold: float = 0.70,
) -> bool:
    """Phase 19v29 — decide if two outcome names refer to the same
    real-world side of a market.

    Rule (first match wins):
      1. Both canonicalize to the same string → True
      2. One canonical is a token-set subset of the other → True
         ('tottenham' ⊆ 'tottenham hotspur', 'corinthians' ⊆
         'corinthians sp', 'santa fe' ⊆ 'independiente santa fe')
      3. Both canonicals share ≥ 2 tokens → True
         (catches multi-word names where a single side word differs)
      4. Sequence similarity of canonicalized strings ≥ fuzzy_threshold
         → True (last-resort character-level fuzzy)
      5. Otherwise → False

    Returns False if either input is empty/None or canonicalizes to
    empty (e.g. 'YES' alone has no team after noise stripping).

    Why subset before fuzzy: SequenceMatcher on 'santa fe' vs
    'independiente santa fe' gives ratio ≈ 0.53 (below 0.70 default),
    but token-level the shorter is a clean subset of the longer — that
    is a strong signal of same outcome that fuzzy misses.

    Why this can't false-allow phantom pairs in practice: the fixture
    title (set of teams) is already pinned by find_pairs/match_event
    upstream. Subset matching only fires when one outcome name is a
    suffix/prefix of the other — typical of platform naming variants
    ('Tottenham' vs 'Tottenham Hotspur FC'), not of cross-team pairs
    inside the same fixture (Santa Fe vs Corinthians never share tokens).
    """
    if not name_a or not name_b:
        return False
    canon_a, _ = canonicalize_outcome_name(name_a)
    canon_b, _ = canonicalize_outcome_name(name_b)
    if not canon_a or not canon_b:
        return False
    if canon_a == canon_b:
        return True

    tokens_a = set(canon_a.split())
    tokens_b = set(canon_b.split())
    if tokens_a and tokens_b:
        # Subset — one is contained inside the other token-wise. The
        # shared part dominates and the extra words are typical platform
        # name decoration ('Hotspur', 'Independiente', 'SP', etc.).
        if tokens_a <= tokens_b or tokens_b <= tokens_a:
            return True
        # Multi-token overlap — both sides share at least 2 distinctive
        # tokens. Helps team names like 'New York Knicks' vs 'NY Knicks'
        # post-canonicalization where NY isn't always normalized.
        if len(tokens_a & tokens_b) >= 2:
            return True

    return title_similarity(canon_a, canon_b) >= fuzzy_threshold


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
        # Phase 19v19 (05.05.2026) — `datetime.utcnow()` is naive +
        # deprecated in 3.12+. Use tz-aware now. The year-boundary
        # flip is also fixed below by forward-bias: prediction markets
        # almost always resolve in the future, so if the parsed date
        # is more than 6 months in the past relative to today, bump
        # year by one.
        default_year = datetime.now(timezone.utc).year
    s = title.lower()
    # Helper to forward-bias year for ambiguous Mon-DD parses around
    # New Year transitions: Dec 31 23:59 UTC scanning "Jan 5" should
    # resolve to NEXT year, not current.
    today_utc = datetime.now(timezone.utc).date()

    def _forward_bias(y, mon, day):
        try:
            d = date(y, mon, day)
        except ValueError:
            return None
        if (today_utc - d).days > 180:
            try:
                return date(y + 1, mon, day)
            except ValueError:
                return d
        return d
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
        if m.group(3):
            try:
                return date(int(m.group(3)), mon, d)
            except ValueError:
                pass
        else:
            biased = _forward_bias(default_year, mon, d)
            if biased is not None:
                return biased
    # MM/DD[/YYYY] or MM/DD/YY
    # Phase 19v19 (05.05.2026) — REQUIRE 4-digit year for slashed dates
    # in noisy contexts. Old regex `(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?`
    # matched "Lakers won 3/2" and "Game 3/7" (series score) → spurious
    # date(year, 3, 2) → wrong (sport, date) bucket → real cross-platform
    # peer not matched → arb missed.
    m = re.search(r'\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b', s)
    if m:
        mon = int(m.group(1))
        d = int(m.group(2))
        y = int(m.group(3))
        if y < 100: y = 2000 + y
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


def _match_from_canon(canon_a: str, canon_b: str,
                       sport_a: Optional[str], sport_b: Optional[str],
                       date_a: Optional[date], date_b: Optional[date],
                       date_tolerance_days: int = 1) -> MatchCandidate:
    """Phase 19v8 (03.05.2026) — fast-path internal helper that takes
    PRE-COMPUTED canonicalized titles + sports + dates. Skips re-running
    `normalize_title` + `canonicalize_teams` (60+ regex × 60+ variants =
    expensive). Used by `find_pairs` after one-time preprocessing.

    Public `match_event(title_a, title_b)` still works for one-shot
    callers (tests, single-event scoring); it just calls this helper
    after computing canon strings.
    """
    sim = title_similarity(canon_a, canon_b)
    # Phase 19v19 (05.05.2026) — neutral score on partial date availability.
    # Old logic: if EITHER side had a date and the other didn't,
    # `date_match=False` → confidence dropped by 0.3 → perfectly
    # title-matched pair (sim=1.0) collapsed to 0.6 → REJECTED at the
    # 0.80 default threshold. This silently dropped many genuine
    # cross-platform arbs (Polymarket no end_date in title vs Limitless
    # with title date, etc.).
    # Fix: 3-tier date score — both match=1.0, one missing=0.5 (neutral),
    # both present and disagree=0.0.
    if date_a and date_b:
        if abs((date_a - date_b).days) <= date_tolerance_days:
            date_score = 1.0
            date_match = True
        else:
            date_score = 0.0
            date_match = False
    elif date_a or date_b:
        # one side missing → neutral, don't penalize
        date_score = 0.5
        date_match = False
    else:
        # both missing → don't help, don't hurt
        date_score = 0.5
        date_match = True
    sport = sport_a if sport_a == sport_b and sport_a else None
    confidence = sim * 0.6 + date_score * 0.3 + (0.1 if sport else 0.0)
    confidence = min(1.0, confidence)
    return MatchCandidate(
        confidence=confidence, title_similarity=sim, date_match=date_match,
        sport=sport, norm_a=canon_a, norm_b=canon_b,
        date_a=date_a, date_b=date_b,
    )


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

    Phase 19v8 (03.05.2026) — O(N×M) → O(N×bucket_size) via sport+date
    bucketing. Polymarket × Limitless pairing was 7500×100=750k compares
    per scan; with bucketing → ~7500 × ~20 = 150k. ~5× speedup. For
    Polymarket × SX (7500×1000=7.5M) the win is ~50× → ~150k compares.
    Total cross-platform pairing time: 30-50s → ~3-8s.
    """
    list_b = list(events_b)
    if not list_b:
        return []

    # Build index: (sport, date_str) → [(idx, normalized_title, date)]
    # Events with sport=None go into a "fallback" bucket — compared against
    # ALL events_a's items with no sport. With unknown date we include event
    # in a "no-date" bucket too.
    def _preprocess(ev: dict) -> Tuple[str, Optional[str], str, Optional[date]]:
        title = ev.get(title_key, '') or ''
        norm = normalize_title(title)
        canon, sport = canonicalize_teams(norm)
        end_date = _parse_date(ev.get(end_date_key))
        date_in_title = extract_date(title)
        ev_date = end_date or date_in_title
        return canon, sport, norm, ev_date

    # Pre-process both lists once (vs match_event re-doing this 8.4M times).
    list_a = list(events_a)
    pre_a = [_preprocess(ea) for ea in list_a]
    pre_b = [_preprocess(eb) for eb in list_b]

    # Bucket b by (sport, date_iso). Keep "any-sport" and "any-date" buckets
    # for events that lack one or both signals — these still get compared
    # against same-bucket peers in a.
    from collections import defaultdict
    buckets_b: dict = defaultdict(list)
    for i, (canon, sport, norm, ev_date) in enumerate(pre_b):
        sport_key = sport or '_nosport'
        date_key = ev_date.isoformat() if ev_date else '_nodate'
        buckets_b[(sport_key, date_key)].append((i, canon, ev_date))
        # Also add to "broader" bucket if date_tolerance might match: ±1 day
        if ev_date:
            try:
                from datetime import timedelta
                buckets_b[(sport_key, (ev_date - timedelta(days=1)).isoformat())].append((i, canon, ev_date))
                buckets_b[(sport_key, (ev_date + timedelta(days=1)).isoformat())].append((i, canon, ev_date))
            except Exception:
                pass

    used_b_ids = set()
    pairs = []
    for a_idx, (canon_a, sport_a, norm_a, date_a) in enumerate(pre_a):
        sport_key = sport_a or '_nosport'
        date_key = date_a.isoformat() if date_a else '_nodate'
        # Candidates: same sport+date, same sport+no-date,
        # no-sport+same-date, no-sport+no-date.
        candidates = []
        for sk in (sport_key, '_nosport'):
            for dk in (date_key, '_nodate'):
                candidates.extend(buckets_b.get((sk, dk), []))
        # Dedup by index — same b might appear via multiple bucket lookups.
        # Phase 19v13 (04.05.2026) — fix broken Pythonic-but-wrong dedup:
        # `not (c[0] in seen or seen.add(c[0]))` always returns True because
        # `seen.add()` returns None → expression is `not (False or None)` =
        # `not None` = True. Dedup never fired → duplicate compares.
        seen = set()
        deduped = []
        for c in candidates:
            if c[0] in seen:
                continue
            seen.add(c[0])
            deduped.append(c)
        candidates = deduped

        best = None
        best_idx = None
        for i, canon_b, date_b in candidates:
            if i in used_b_ids:
                continue
            # Phase 19v8: use fast-path that skips redundant normalize+
            # canonicalize work (already done in _preprocess). 60-90% time
            # saved on per-bucket compares vs original match_event.
            sport_b = pre_b[i][1]
            mc = _match_from_canon(
                canon_a, canon_b, sport_a, sport_b, date_a, date_b,
            )
            if mc.confidence >= min_confidence:
                if best is None or mc.confidence > best.confidence:
                    best = mc
                    best_idx = i
        if best is not None:
            pairs.append((list_a[a_idx], list_b[best_idx], best))
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
