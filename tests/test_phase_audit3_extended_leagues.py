"""Phase audit-3 (15.05.2026) — extended league coverage.

Operator request: bot was scanning only EPL / La Liga / Bundesliga /
Serie A / Ligue 1 / MLS plus major US leagues. Many active prediction-
market fixtures (Eredivisie, J-League, Brasileirao, Liga MX, etc.) were
falling into the '_nosport' bucket in find_pairs because their league
markers weren't recognized → cross-platform matches missed.

This adds league patterns + sport mappings for:
  * Soccer 2nd tiers: Bundesliga 2, La Liga 2, Ligue 2, Serie B
  * Other top European: Eredivisie, Primeira, Super Lig, Scottish, Belgian
  * Non-European top: J-League, A-League, Brasileirao, Liga MX,
                       Argentine Primera, Saudi Pro
  * Basketball: Euroleague, WNBA
  * Cricket (T20): IPL, BBL

No new team aliases — find_pairs' sport-bucket fallback uses
extract_league when team alias is absent, so league markers alone are
sufficient to route a fixture to the right bucket. Team fuzzy-match
within the bucket still relies on title similarity across platforms.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


# ── Soccer second tiers ──────────────────────────────────────────

def test_detect_bundesliga2():
    from event_matching import extract_league
    assert extract_league('2. Bundesliga, Schalke vs Hannover, May 17') == 'bundesliga2'
    assert extract_league('Bundesliga 2 — Hamburg vs Cologne') == 'bundesliga2'
    assert extract_league('German Bundesliga 2 final round') == 'bundesliga2'


def test_detect_laliga2():
    from event_matching import extract_league
    assert extract_league('La Liga 2: Levante vs Mirandés') == 'laliga2'
    assert extract_league('Segunda División matchday 38') == 'laliga2'
    assert extract_league('Spanish Segunda final') == 'laliga2'


def test_detect_ligue2():
    from event_matching import extract_league
    assert extract_league('Ligue 2 promotion playoff') == 'ligue2'
    assert extract_league('French Ligue 2: Saint Etienne vs Rodez') == 'ligue2'


def test_detect_serieb():
    from event_matching import extract_league
    assert extract_league('Italian Serie B: Parma vs Genoa') == 'serieb'
    assert extract_league('Serie B matchday 20') == 'serieb'


# ── Other top European ───────────────────────────────────────────

def test_detect_eredivisie():
    from event_matching import extract_league
    assert extract_league('Eredivisie: Ajax vs PSV') == 'eredivisie'
    assert extract_league('Dutch Eredivisie final round') == 'eredivisie'


def test_detect_primeira():
    from event_matching import extract_league
    assert extract_league('Primeira Liga: Benfica vs Porto') == 'primeira'
    assert extract_league('Liga Portugal title decider') == 'primeira'


def test_detect_super_lig():
    from event_matching import extract_league
    assert extract_league('Süper Lig: Galatasaray vs Fenerbahçe') == 'super_lig'
    assert extract_league('Turkish Super Lig matchday') == 'super_lig'


def test_detect_scottish_premier():
    from event_matching import extract_league
    assert extract_league('Scottish Premiership: Celtic vs Rangers') == 'scottish_premier'


def test_detect_belgian_pro():
    from event_matching import extract_league
    assert extract_league('Belgian Pro League playoff') == 'belgian_pro'
    assert extract_league('Jupiler Pro: Anderlecht vs Club Brugge') == 'belgian_pro'


# ── Non-European top leagues ─────────────────────────────────────

def test_detect_jleague():
    from event_matching import extract_league
    assert extract_league('J-League: Yokohama vs Kawasaki') == 'jleague'
    assert extract_league('J1 League final round') == 'jleague'
    assert extract_league('Japanese J League title race') == 'jleague'


def test_detect_aleague():
    from event_matching import extract_league
    assert extract_league('A-League grand final') == 'aleague'
    assert extract_league('Australian A League playoff') == 'aleague'


def test_detect_brasileirao():
    from event_matching import extract_league
    assert extract_league('Brasileirão: Flamengo vs Palmeiras') == 'brasileirao'
    assert extract_league('Brazilian Serie A title race') == 'brasileirao'


def test_detect_liga_mx():
    from event_matching import extract_league
    assert extract_league('Liga MX: Club América vs Pachuca') == 'liga_mx'
    assert extract_league('Mexican Liga MX final') == 'liga_mx'


def test_detect_argentine_primera():
    from event_matching import extract_league
    assert extract_league('Argentine Primera: River vs Boca') == 'argentine_primera'
    assert extract_league('Liga Profesional Argentina matchday') == 'argentine_primera'


def test_detect_saudi_pro():
    from event_matching import extract_league
    assert extract_league('Saudi Pro League: Al-Hilal vs Al-Nassr') == 'saudi_pro'
    assert extract_league('Roshn Saudi League final') == 'saudi_pro'


# ── Basketball additions ─────────────────────────────────────────

def test_detect_euroleague():
    from event_matching import extract_league
    assert extract_league('Euroleague final four: Real Madrid vs Olympiakos') == 'euroleague'
    assert extract_league('Turkish Airlines Euroleague playoff') == 'euroleague'


def test_detect_wnba():
    from event_matching import extract_league
    assert extract_league('WNBA finals: Aces vs Liberty') == 'wnba'


# ── Cricket additions ────────────────────────────────────────────

def test_detect_ipl():
    from event_matching import extract_league
    assert extract_league('IPL: Chennai vs Mumbai') == 'ipl'
    assert extract_league('Indian Premier League final') == 'ipl'


def test_detect_bbl():
    from event_matching import extract_league
    assert extract_league('BBL: Sydney Sixers vs Perth Scorchers') == 'bbl'
    assert extract_league('Big Bash League final') == 'bbl'


# ── Sport bucketing ──────────────────────────────────────────────

def test_new_leagues_have_sport_mapping():
    """Every league code added must map to a sport in _LEAGUE_TO_SPORT
    so find_pairs' fallback bucketing works."""
    from event_matching import _LEAGUE_PATTERNS, _LEAGUE_TO_SPORT
    new_codes = (
        'bundesliga2', 'laliga2', 'ligue2', 'serieb',
        'eredivisie', 'primeira', 'super_lig', 'scottish_premier', 'belgian_pro',
        'jleague', 'aleague', 'brasileirao', 'liga_mx', 'argentine_primera',
        'saudi_pro',
        'euroleague', 'wnba',
        'ipl', 'bbl',
    )
    for code in new_codes:
        assert code in _LEAGUE_TO_SPORT, f"{code} missing sport mapping"
    # Sports themselves should be in the standard set
    assert _LEAGUE_TO_SPORT['ipl'] == 'cricket'
    assert _LEAGUE_TO_SPORT['euroleague'] == 'basketball'
    assert _LEAGUE_TO_SPORT['jleague'] == 'soccer'


# ── Existing leagues NOT regressed ───────────────────────────────

def test_existing_leagues_still_work():
    """Sanity: original patterns still match exactly as before."""
    from event_matching import extract_league
    assert extract_league('EPL: Brentford vs Crystal Palace') == 'epl'
    assert extract_league('Serie A: Inter vs Hellas Verona') == 'seriea'
    assert extract_league('NBA Finals Game 7') == 'nba'
    assert extract_league('NHL Stanley Cup Final') == 'nhl'


# ── No false positives ───────────────────────────────────────────

def test_no_false_positive_for_unrelated_titles():
    """Common phrasings that shouldn't trigger any new league marker."""
    from event_matching import extract_league
    assert extract_league('BTC Up or Down — 1 day') is None
    assert extract_league('Lakers vs Celtics') is None  # no league marker
    # 'BBL' as part of a word/team name shouldn't match — word boundary required
    assert extract_league('AbBlock test') is None
