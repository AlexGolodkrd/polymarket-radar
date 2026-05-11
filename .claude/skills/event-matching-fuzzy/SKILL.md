# Event Matching — Cross-Platform Fuzzy

**Created Phase 12 (01.05.2026)** — used by `cross_platform_matcher.py` (NOT YET implemented).

## Problem

Same real-world event has DIFFERENT titles across prediction markets:

| Platform | Title format |
|---|---|
| Polymarket | "Will the Los Angeles Lakers beat the Boston Celtics?" |
| Limitless | "Lakers vs Celtics — March 25" |
| SX Bet | "LAL @ BOS" (just team codes) |
| Kalshi | "NBA-LAL-BOS-MAR25" (structured ticker) |

Naive string match fails: 0% overlap between formats. We need fuzzy matching that handles:
- Synonym teams (LAL = Lakers = Los Angeles Lakers)
- Order swaps ("Lakers vs Celtics" = "Celtics vs Lakers" — but **outcomes are mirrored!**)
- Date suffixes
- Sport markers ("NBA", "Premier League")
- Question phrasing ("Will X win?" vs "X to win")

## Recommended approach

### Step 1: Normalize

```python
def normalize_title(s: str) -> str:
    s = s.lower()
    # Strip URL-style: kebab, underscores
    s = s.replace('-', ' ').replace('_', ' ')
    # Strip punctuation but KEEP digits (dates matter)
    s = re.sub(r'[^\w\s]', ' ', s)
    # Drop noise words
    s = re.sub(r'\b(will|to|win|wins|beat|vs|versus|the|a|an|and|or|of|on|in|at|@)\b',
               ' ', s)
    # Collapse whitespace
    s = re.sub(r'\s+', ' ', s).strip()
    return s
```

`"Will the Los Angeles Lakers beat Boston Celtics?"` → `"los angeles lakers boston celtics"`
`"LAL @ BOS"` → `"lal bos"`

### Step 2: Sport-specific tokenizer

For NBA/NFL/MLB, build team-name dictionary mapping nicknames + cities + codes:

```python
NBA_TEAMS = {
    'lakers': ['lal', 'la lakers', 'los angeles lakers'],
    'celtics': ['bos', 'boston celtics'],
    # ... 30 teams
}
```

When normalizing, replace any team variant with **canonical name**:
- `"lal bos"` → `"lakers celtics"`
- `"los angeles lakers boston celtics"` → `"lakers celtics"`

Now both forms collapse to the SAME normalized string.

### Step 3: Date extraction

Both events must be on the **same calendar day**:

```python
def extract_date(s: str, default_year: int) -> Optional[date]:
    """Look for MM/DD, MMM DD, YYYY-MM-DD patterns."""
    # ...
```

Cross-platform pair candidate iff:
- normalized titles match (>=85% similarity via difflib.SequenceMatcher)
- dates within ±1 day (timezone tolerance)

### Step 4: Outcome alignment

CRITICAL: when titles match, outcomes might be **swapped**:
- Polymarket: outcome 0 = "Lakers", outcome 1 = "Celtics"
- SX Bet: outcome 1 = "BOS", outcome 2 = "LAL"

So when building cross-platform arb leg pair, you must MAP outcome IDs across platforms by team name, not by index. Check via `team_name in outcome_label`.

### Step 5: Confidence score

```python
@dataclass
class MatchCandidate:
    confidence: float    # 0..1
    title_similarity: float
    date_match: bool
    outcomes_aligned: bool
    sport: str           # 'nba', 'nfl', 'soccer', 'unknown'

# Aggregate:
confidence = (
    title_similarity * 0.5 +
    (1.0 if date_match else 0.0) * 0.2 +
    (1.0 if outcomes_aligned else 0.0) * 0.3
)
```

Thresholds:
- `confidence >= 0.95` — auto-accept, fire arb
- `0.80 <= confidence < 0.95` — quarantine, operator review
- `confidence < 0.80` — drop silently

## Reference cases (test fixtures)

```python
# True positives
("Will the Lakers win on Mar 25?", "Lakers vs Celtics — Mar 25", 'nba')         → match
("LAL @ BOS Mar 25", "Will Boston beat the Lakers?", 'nba')                      → match (outcomes inverted)
("Manchester United vs Liverpool", "Man United v Liverpool — Mar 26", 'soccer') → match

# False positives we MUST reject
("Lakers vs Celtics Mar 25", "Lakers vs Celtics Mar 26", 'nba')                  → reject (different days)
("Lakers @ Celtics Mar 25", "Lakers @ Knicks Mar 25", 'nba')                     → reject (different opponent)
("Will Lakers win Western Conference?", "Will Lakers beat Celtics Mar 25?",'nba')→ reject (period scope)

# False negatives we MUST avoid
("nba lakers celtics", "los angeles boston basketball")                          → match (despite zero literal overlap)
```

## Risks

1. **Synonym dictionary maintenance** — new teams (expansion teams), team renames (Wizards → ?). Must update yearly.
2. **Multi-game tournaments** — "Knockout Stage Match 4" vs "Real Madrid vs Bayern" — needs context. Quarantine if confidence < 0.95.
3. **Cross-sport collision** — "Lakers" as boat race team in some niche league. Use `sport` field to namespace teams.
4. **Time-of-day events** — "Lakers FT score over 220" only matches if same scoring cutoff.

## Implementation plan

| Module | Lines (est) | Tests |
|---|---|---|
| `Scripts/event_matching.py::normalize_title` | 30 | 10 unit tests |
| `Scripts/event_matching.py::TEAM_DICTS` (NBA, NFL, MLB, soccer top leagues) | 200 | n/a (data) |
| `Scripts/event_matching.py::sport_canonicalize` | 50 | 15 unit tests |
| `Scripts/event_matching.py::match_score` | 40 | 20 fixtures |
| `Scripts/event_matching.py::find_cross_platform_pairs(pool_a, pool_b)` | 80 | 5 integration tests |

## Operator review UI (later)

When `confidence in [0.80, 0.95]`, show in dashboard `Карантин` tab with side-by-side comparison + Accept/Reject buttons. Operator decisions feed back into a learned dictionary.

## See also

- `cross-platform-arbs` skill — uses this module
- BUG_CATALOG.md §1.1 (Leeds-Burnley phantom — partial coverage demo of why fuzzy matching alone isn't enough)
