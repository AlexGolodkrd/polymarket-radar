"""
Arbitrage Radar v7 — 3 platforms (Poly, Kalshi, SX Bet) + Polymarket WebSocket.

Main scan: 300 Poly + 200 Kalshi + 200 SX Bet (fast ~35s) — REST.
Pause scan: extra pages — REST background.
HOT/NEAR pool architecture:
    HOT  = sum < threshold              (already an arb)
    NEAR = threshold <= sum < +NEAR_BUFFER  (one tick away from arb)
    COLD = sum >= threshold + NEAR_BUFFER   (ignored until next main scan)
Polymarket HOT+NEAR    → WebSocket push (instant)
Kalshi    HOT+NEAR    → REST micro-scan every KALSHI_MICRO_INTERVAL s
SX Bet    HOT+NEAR    → REST micro-scan every SX_MICRO_INTERVAL s (live sport)

Rate-limit safeguards:
    - WS subs capped at MAX_WS_SUBS (default 200)
    - WS reconnect backoff 1->2->4->8->30s
    - WS heartbeat: PING every 10s, watchdog drops conn after 30s silence
    - REST micro-scan only the HOT+NEAR pool, never the full universe
"""
import sys, io, os, json, re, time, threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from flask import Flask, jsonify, send_file
import requests

# Make Scripts/ importable when run from project root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from poly_ws import PolyMarketWS
from poly_user_ws import PolyUserWS
from limitless_ws import LimitlessWS
import analytics
from executor import fire_arb, paper_stats
from executor.builders import WalletStub
import risk as risk_mod
import wallets as wallets_mod
import paper_trading

app = Flask(__name__)

# ── Phase 2: dry-run executor — auto-fire deals when they enter HOT ─
# Tracks arb_ids already dry-fired so we don't spam logs on every scan.
# Cleared when the deal title/structure leaves the deals list.
_fired_arb_keys: set = set()
_fired_arb_keys_lock = threading.Lock()

def _arb_fire_key(deal: dict) -> str:
    return f"{deal.get('arb_structure','?')}::{deal.get('platform','?')}::{deal.get('title','?')}"

# Phase 4: load wallet pool from configured backend at startup.
# If Credentials.env has no BOT*_ETH_ADDRESS entries, the pool stays empty
# and atomic.py falls back to a single mock stub (still dry-run safe).
# When the user fills in addresses, the real 6-bot pool is used.
_wallet_pool = wallets_mod.load_pool()
_DRY_RUN_WALLETS = [
    WalletStub(bot_id=w.bot_id, eth_address=w.eth_address,
               private_key=None)  # Phase 5+ flips this when graduation passes
    for w in _wallet_pool.wallets
]

def _maybe_dry_fire(deals):
    """Auto-fire (dry-run) any deal not previously fired this session.
    Called after every main scan and after every WS-driven re-eval.

    Phase 9i (28.04.2026): two-phase commit fixes a TOCTOU race +
    serialization issue. Old code held _fired_arb_keys_lock across the
    fire_arb call which:
      (a) blocked any other thread (scan_loop, WS callbacks) for the
          full 5s dead-man timeout in real mode
      (b) had a check-then-add gap if anyone snuck a release into the
          inner fire_arb path
    New approach: reserve all keys atomically under lock, fire without
    lock, parallel calls now safe."""
    if not deals:
        return
    to_fire = []
    with _fired_arb_keys_lock:
        for d in deals:
            if d.get('is_quarantine'): continue
            key = _arb_fire_key(d)
            if key in _fired_arb_keys: continue
            _fired_arb_keys.add(key)   # reserve first — no double-fire window
            to_fire.append((key, d))
    # Fire outside the lock — slow path doesn't block other threads.
    for key, d in to_fire:
        try:
            fire_arb(d, wallets=_DRY_RUN_WALLETS, dry_run=True)
        except Exception as e:
            print(f"[DRYFIRE] error firing {key}: {e}")

@app.after_request
def add_cors(resp):
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp

# ── Config ──────────────────────────────────────────────────────
BALANCE = 100.0
THETA_POLY      = 0.025   # Polymarket taker fee ~2.5%
THETA_KALSHI    = 0.07    # Kalshi taker fee ~7%
THETA_SX        = 0.02    # SX Bet taker fee ~2%
# Limitless Exchange (Base L2): NO platform fee, only gas (~$0.01 per leg).
# We model 0.5% as conservative buffer covering gas + slippage on $50 trade
# (gas $0.04 across 4 legs = 0.08% on $50, so 0.5% leaves room for spread).
THETA_LIMITLESS = 0.005
THRESH_POLY      = 0.97   # legacy fallback when no per-market info available;
                          # actual threshold is now DYNAMIC per arb (see
                          # compute_poly_threshold) — Polymarket V2 has
                          # per-market dynamic taker_fee_bps, so a single
                          # static threshold over-counts on 0-fee markets and
                          # under-counts on 2.5-3% markets.
THRESH_KALSHI    = 0.93   # 93c — covers ~7% taker fee with margin
THRESH_SX        = 0.97   # 97c — covers ~2% taker fee with margin
# Limitless: no platform fee → can be much tighter than Polymarket.
# Phase 9l (28.04.2026): bumped from 0.99 → 0.988 for extra cushion
# (matches the +0.002 safety buffer we added to dynamic Poly thresholds).
# 0.988 = 1.2¢ minimum margin per $1 = covers ~$0.005 gas + slippage
# safely + 0.5¢ buffer against drift between scan and fire.
THRESH_LIMITLESS = 0.988

# ── Dynamic Polymarket threshold (Phase 9k) ─────────────────────────
# Break-even THRESH per (theta, N_legs) so we don't reject valid arbs on
# 0%-fee markets (V2 promo) nor accept loss-making arbs on 2.5%+ markets.
#
# Cost components on $1 of capital:
#   fee_total      = theta × 1            (Polymarket charges taker fee on
#                                          every leg's filled stake;
#                                          stakes sum to capital)
#   slippage_total ≈ 0.003 × 1            (per-leg slip, conservative cap)
#   safety_buffer  = 0.005 × 1            (drift between scan and fire)
#
# Required margin to break even = fee_total + slippage_total + safety
#                              = theta + 0.008
# Therefore THRESH = 1 - (theta + 0.008)
#
# Floor 0.95 (never below — even hard-capped if API misreports fee).
# Cap   0.995 (never above — spread that tight is noise, not arb).
POLY_DYNAMIC_THRESH_FLOOR  = 0.948    # Phase 9l: -0.002 from 0.95 for extra cushion
POLY_DYNAMIC_THRESH_CAP    = 0.993    # Phase 9l: -0.002 from 0.995
POLY_SLIPPAGE_RESERVE      = 0.003    # per arb (not per leg — conservative)
# Phase 9l (28.04.2026): bumped safety buffer from 0.005 → 0.007 at user
# request. Effect: every dynamic threshold is now 0.002 lower, giving us
# an extra 0.2% margin cushion against unexpected drift / cache-stale
# fee / liquidity drop between scan and fire. Examples:
#   0%   fee: 0.992 → 0.990
#   1%   fee: 0.982 → 0.980
#   2.5% fee: 0.967 → 0.965
#   4%   fee: 0.952 → 0.950
# Trade-off: ~0.2% fewer arbs accepted, but every accepted one has bigger
# safety margin against the 5-10% promah scenarios we identified.
POLY_SAFETY_BUFFER         = 0.007


def compute_poly_threshold(taker_fee_bps: float, n_legs: int = None) -> float:
    """Return the break-even threshold for a Polymarket arb at this
    market's actual taker fee. n_legs reserved for future tuning (more
    legs = more individual slippage paths) but currently unused —
    POLY_SLIPPAGE_RESERVE is already a conservative arb-level number.

    Examples:
        0% fee (0 bps)   → 1 - 0.008 = 0.992
        1% fee (100 bps) → 1 - 0.018 = 0.982
        2.5% fee (250)   → 1 - 0.033 = 0.967
        4% fee (400)     → 1 - 0.048 = 0.952
        6% fee (600)     → 1 - 0.068 = 0.95 (clipped to floor)
    """
    theta = (taker_fee_bps or 0) / 10000.0
    raw = 1.0 - (theta + POLY_SLIPPAGE_RESERVE + POLY_SAFETY_BUFFER)
    if raw < POLY_DYNAMIC_THRESH_FLOOR: return POLY_DYNAMIC_THRESH_FLOOR
    if raw > POLY_DYNAMIC_THRESH_CAP:   return POLY_DYNAMIC_THRESH_CAP
    return raw
SCAN_INTERVAL = 90
MICRO_INTERVAL = 5             # legacy — kept as fallback only
KALSHI_MICRO_INTERVAL = 5      # REST poll for Kalshi HOT+NEAR pool
SX_MICRO_INTERVAL = 3          # REST poll for SX Bet HOT+NEAR pool (live sport)

# Per-platform enable toggles. Set ENABLE_KALSHI=0 / ENABLE_SX=0 in env to
# skip those platforms entirely — no fetches, no eval, no micro-loop.
# Useful when focusing capacity on one platform (e.g. Polymarket-only mode
# while Kalshi/SX are inaccessible from current jurisdiction).
ENABLE_KALSHI = os.environ.get('ENABLE_KALSHI', '1') != '0'
ENABLE_SX = os.environ.get('ENABLE_SX', '1') != '0'
# Limitless Exchange (Base L2 prediction market) — added 28.04.2026.
# Same CLOB/EIP-712 architecture as Polymarket, no KYC, no platform fee.
ENABLE_LIMITLESS = os.environ.get('ENABLE_LIMITLESS', '1') != '0'
LIMITLESS_API_KEY = os.environ.get('LIMITLESS_API_KEY', '').strip()  # for trade-side ops; reads work without key

# Polymarket main-scan pages. Each page = 500 events. 4 pages = 2000 events
# per scan. Default was 2 pages; bumped because skipping Kalshi/SX frees
# ~25s of fetch budget per scan that we can spend on more Poly coverage.
POLY_MAIN_PAGES = int(os.environ.get('POLY_MAIN_PAGES', '4'))
# Limitless main-scan pages. The API caps `limit` at 25 (verified 28.04.2026
# — server returns HTTP 400 for limit>25). To cover ~1000 markets we need
# 40 pages of 25. With 100ms polite gap → full fetch ~4s, well under our
# scan budget. Bumped from 10×100 to 40×25 after the cap was discovered.
LIMITLESS_MAIN_PAGES = int(os.environ.get('LIMITLESS_MAIN_PAGES', '40'))
LIMITLESS_PAGE_SIZE = int(os.environ.get('LIMITLESS_PAGE_SIZE', '25'))   # API max
LIMITLESS_PAGE_DELAY_S = float(os.environ.get('LIMITLESS_PAGE_DELAY_S', '0.1'))
LIMITLESS_MICRO_INTERVAL = int(os.environ.get('LIMITLESS_MICRO_INTERVAL', '5'))
LIMITLESS_API_BASE = 'https://api.limitless.exchange'
MAX_WORKERS = 80
TIMEOUT = 5
NEAR_BUFFER = 0.07             # 7c — wider net for "almost arb" candidates (was 3c)
MAX_WS_SUBS = 1000             # Polymarket WS cap. Doubled from 500 to fit YES+NO
                               # tokens both subscribed (Phase 1 — ALL_NO / YES_NO_PAIR).
SX_PAGE_SIZE = 100             # SX Bet API rejects pageSize > 100 (HTTP 400)
SX_MAX_PAGES_MAIN = 10         # 10 * 100 = up to 1000 markets in main scan
SX_MAX_PAGES_PAUSE = 5         # 5 * 100 = up to 500 markets in pause scan

# SX Bet market types that are *binary and exhaustive* (outcomeOne+outcomeTwo
# cover all possible outcomes — perfect for arbitrage). Discovered live via
# `GET /markets/active`. Excludes type=1 (soccer 'Team X wins' Yes/No which
# does NOT cover draw) — that needs the 3-way pipeline (separate PR).
SX_BINARY_TYPES = {
    2,   # Soccer Total Over/Under
    3,   # Soccer Spread/Handicap
    21,  # Basketball 1st Period Total
    28,  # Hockey Total
    29,  # MMA Total
    45,  # Basketball 2nd Period Total
    46,  # Basketball 3rd Period Total
    52,  # Soccer Draw No Bet (W/L only)
    53,  # Basketball 1st Half Spread
    63,  # Basketball 1st Half Moneyline
    64,  # Basketball 1st Period Spread
    65,  # Basketball 2nd Period Spread
    66,  # Basketball 3rd Period Spread
    77,  # Basketball 1st Half Total
    165, # Tennis Sets Total
    166, # Tennis Games Total
    201, # Tennis Period Spread
    202, # Tennis 1st Set Moneyline
    203, # Tennis 2nd Set Moneyline
    204, # Basketball 3rd Period Moneyline
    226, # Hockey Moneyline (the original, kept)
    236, # Baseball 1st 5 Innings Total
    342, # Hockey Spread
    866, # Tennis Sets Spread
    1536,# E-Sports Total
    1618,# Baseball 1st 5 Innings Moneyline
}
WINDOW_DAYS = 10               # accept events ending within this many days (reverted
                               # 28.04.2026 from 30 → 10: 30-day events lock capital
                               # for a month for $5-30 profit = poor turnover.
                               # 10-day window = 3x better capital efficiency.)
WINDOW_PAST_DAYS = 2           # also keep events that ended up to this many days ago

DEADLINE_RE = re.compile(
    r'\b(by|before)\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|'
    r'january|february|march|april|june|july|august|september|october|november|december|'
    r'20\d{2}|end of|q[1-4])', re.IGNORECASE)

# "Other" outcome detector. Multi-outcome events with a hidden "Other" /
# "None of the above" option are vulnerable arbs: if we buy YES on A,B,C
# only and "Other" actually wins, every leg loses. We quarantine such deals
# (still show in UI for analysis, but block the executor from firing them).
# Pattern covers EN + RU phrasing seen across Polymarket / Limitless titles.
OTHER_RE = re.compile(
    r'\b(other|any other|none of the above|other team|other candidate|other player|'
    r'прочее|другое|неопределен|любой другой)\b',
    re.IGNORECASE)


def has_other_outcome(names):
    """True if any name matches the 'Other' pattern — see OTHER_RE comment.
    Used by both filter_poly and eval_limitless to flag deals as quarantine."""
    return any(OTHER_RE.search(n or '') for n in names)

HEADERS = {"Accept": "application/json"}

# ── State ───────────────────────────────────────────────────────
scan_data = {"last_scan": None, "scanning": False, "deals": [], "quarantine": [], "stats": {}, "error": None, "ws": {}}
whitelist = set()
blacklist = set()
scan_lock = threading.Lock()
candidates_global = {"poly": [], "kalshi": [], "sx": []}
cand_lock = threading.Lock()

# HOT / NEAR pools per platform.
# Each pool item is the same shape we already use in eval_*: a candidate tuple/list.
pools = {
    'poly':   {'hot': [], 'near': []},
    'kalshi': {'hot': [], 'near': []},
    'sx':     {'hot': [], 'near': []},
    'lim':    {'hot': [], 'near': []},
}
pools_lock = threading.Lock()

# Reverse index: Polymarket token_id -> candidate, used by WS callback to know
# which event to re-evaluate when a price_change arrives.
poly_token_index = {}
poly_token_index_lock = threading.Lock()

# Limitless reverse slug-index. Phase 9d (28.04.2026) — same role as
# poly_token_index: when WS pushes an orderbookUpdate on a slug, we
# look up the parent event in O(1) and re-evaluate immediately instead
# of waiting for the 5s micro-loop tick. Critical for Limitless's
# 30-min crypto-oracle markets where prices move fast in the last
# minutes before resolution. Maps BOTH child slugs (negRisk groups)
# and event-level slugs (standalone binary) to the parent event dict.
lim_slug_index = {}
lim_slug_index_lock = threading.Lock()


# Limitless per-slug metadata cache. Phase 9c (28.04.2026) — without this,
# atomic._build_leg cannot construct a real Limitless order (`tokenId`
# uint256 is required by the EIP-712 Order type; `verifyingContract` is
# required by the EIP-712 domain and varies per market venue).
#
# Shape: {slug: {'yes_token': str, 'no_token': str, 'verifying_contract':
#                str, 'volume': float, 'is_other': bool, 'fetched_at': float}}
#
# Tokens + venue.exchange are stable for a given slug (CTF condition is
# immutable once deployed) so we cache forever within a process. Volume
# changes — refresh every 5 min so HOT-pool sorting can prefer liquid markets.
lim_meta_cache = {}
lim_meta_lock = threading.Lock()
LIM_META_REFRESH_S = 300   # 5 min — only volume needs refresh; tokens stay

# Polymarket V2 per-market info cache. V2 migration moved fee/tick/min-size
# from hardcoded constants to dynamic per-market values queryable via the
# REST API. Without this, our deal builder used THETA_POLY=2.5% across the
# board — but in V2 many markets charge 0% taker fee, so we were
# REJECTING valid arbs (overpessimistic net) and signing orders with the
# wrong tick size (server would reject).
#
# Shape: {condition_id: {tick_size, min_order_size,
#                        maker_fee_bps, taker_fee_bps, neg_risk,
#                        accepting_orders, fetched_at}}
poly_market_info_cache = {}
poly_market_info_lock = threading.Lock()
POLY_MARKET_INFO_REFRESH_S = 600    # 10 min — fee changes are rare

# Last full REST clob_res cached so WS-driven re-eval can fall back to old asks
# for tokens of the same event that haven't been pushed yet.
# Also reused by /api/near to render NEAR snapshot without re-fetching.
poly_clob_cache = {}
poly_clob_cache_lock = threading.Lock()
kalshi_res_cache = {}
sx_res_cache = {}
lim_res_cache = {}
res_cache_lock = threading.Lock()

# Polymarket WS client (initialized in __main__).
ws_client = None

# Polymarket user-channel WS clients — Phase 9f. One per bot wallet that
# has poly L2 creds. Maintained as a list so iteration in update_markets /
# kill / reconcile is straightforward.
poly_user_ws_clients: list = []

# Limitless WS client (initialized in __main__ when ENABLE_LIMITLESS=1).
# Same pattern as ws_client: idle until first scan classifies a HOT/NEAR pool,
# then `update_subscriptions(slugs)` triggers connect + subscribe.
lim_ws_client = None
LIMITLESS_MAX_WS_SUBS = int(os.environ.get('LIMITLESS_MAX_WS_SUBS', '250'))

# ── Helpers ─────────────────────────────────────────────────────
def calc_fee(price, contracts, theta):
    p = max(0.001, min(0.999, price))
    return theta * contracts * p * (1 - p)

def is_deadline(names):
    if len(names) < 2: return False
    return sum(1 for n in names if DEADLINE_RE.search(n)) >= len(names) * 0.5

def is_within_window(date_str=None, timestamp=None, max_days=None, past_days=None):
    """True if the event ends within `max_days` ahead OR ended within
    `past_days` behind (still resolving). Defaults read from module config
    (WINDOW_DAYS / WINDOW_PAST_DAYS), so call sites stay short."""
    if max_days is None: max_days = WINDOW_DAYS
    if past_days is None: past_days = WINDOW_PAST_DAYS
    now = datetime.now(timezone.utc)
    try:
        if timestamp:
            dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        elif date_str:
            if date_str.endswith('Z'): date_str = date_str[:-1] + '+00:00'
            elif len(date_str) == 10: date_str += 'T00:00:00+00:00'
            dt = datetime.fromisoformat(date_str)
            if not dt.tzinfo: dt = dt.replace(tzinfo=timezone.utc)
        else: return False

        diff = (dt - now).total_seconds()
        return -86400 * past_days <= diff <= 86400 * max_days
    except Exception:
        return False

# Back-compat shim — older code paths and external callers may still use this name
def is_within_10_days(date_str=None, timestamp=None):
    return is_within_window(date_str=date_str, timestamp=timestamp)

# ── Fetchers ────────────────────────────────────────────────────
def _fetch_clob(token_id):
    try:
        r = requests.get(f"https://clob.polymarket.com/book?token_id={token_id}", timeout=TIMEOUT)
        asks = r.json().get('asks', [])
        if not asks: return token_id, None, 0
        best = min(asks, key=lambda a: float(a.get('price', 999)))
        depth = sum(float(a.get('size',0))*float(a.get('price',0)) for a in asks)
        return token_id, float(best['price']), depth
    except: return token_id, None, 0

def _fetch_kalshi_ob(ticker):
    """Fetch Kalshi orderbook for both YES and NO sides.
    Returns: ticker, yes_ask, yes_depth, no_ask, no_depth.
    NO side enables ALL_NO and YES_NO_PAIR arb structures (Phase 1).
    """
    try:
        r = requests.get(f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}/orderbook",
                         timeout=TIMEOUT, headers=HEADERS)
        ob = r.json().get('orderbook_fp', {})
        yes_lvls = ob.get('yes_dollars', [])
        no_lvls = ob.get('no_dollars', [])
        yes_ask = float(yes_lvls[0][0]) if yes_lvls else None
        yes_depth = sum(float(l[1]) for l in yes_lvls) if yes_lvls else 0
        no_ask = float(no_lvls[0][0]) if no_lvls else None
        no_depth = sum(float(l[1]) for l in no_lvls) if no_lvls else 0
        return ticker, yes_ask, yes_depth, no_ask, no_depth
    except: return ticker, None, 0, None, 0

def _fetch_sx_orders(market_hash):
    """Convert SX Bet maker orderbook into taker-side best ask prices.

    SX Bet API: an order with `isMakerBettingOutcomeOne=True` and
    `percentageOdds=p` means a market-maker is bidding for outcomeOne at
    implied probability `p`. A taker filling that order takes the OPPOSITE
    side (outcomeTwo) at price `1 - p`. So:
        taker_ask_outcomeTwo = 1 - max(maker_bid where maker is on outcomeOne)
        taker_ask_outcomeOne = 1 - max(maker_bid where maker is on outcomeTwo)
    Returns best ask (lowest taker cost) and total taker-side liquidity per outcome.
    """
    try:
        r = requests.get(f"https://api.sx.bet/orders?marketHashes={market_hash}&maker=true", timeout=TIMEOUT)
        data = r.json()
        orders = data.get('data', {}).get('orders', []) if data.get('status') == 'success' else []
        max_maker_bid_one, max_maker_bid_two = None, None
        depth_taker_one, depth_taker_two = 0, 0
        for o in orders:
            price = float(o.get('percentageOdds', '0')) / 1e20  # maker's implied prob
            size = float(o.get('orderSizeFillable', '0')) / 1e6  # USDC
            if price <= 0 or price >= 1 or size <= 0: continue
            taker_price = 1 - price  # what taker pays for the OPPOSITE outcome
            if o.get('isMakerBettingOutcomeOne', True):
                # maker bids outcomeOne -> taker can buy outcomeTwo at (1-price)
                if max_maker_bid_one is None or price > max_maker_bid_one:
                    max_maker_bid_one = price
                depth_taker_two += size * taker_price
            else:
                # maker bids outcomeTwo -> taker can buy outcomeOne at (1-price)
                if max_maker_bid_two is None or price > max_maker_bid_two:
                    max_maker_bid_two = price
                depth_taker_one += size * taker_price
        # Best ask for taker on each outcome = 1 - best maker bid on the OTHER side
        best1 = (1 - max_maker_bid_two) if max_maker_bid_two is not None else None
        best2 = (1 - max_maker_bid_one) if max_maker_bid_one is not None else None
        return market_hash, best1, depth_taker_one, best2, depth_taker_two
    except:
        return market_hash, None, 0, None, 0


# ── Limitless Exchange (Phase 9, 28.04.2026) ────────────────────────
# CLOB-based prediction market on Base L2 (api.limitless.exchange).
# Architecture mirrors Polymarket: YES/NO shares, $1 collateral, EIP-712
# signed orders, negRisk-style multi-outcome groups. We fetch markets +
# orderbook via REST and treat the data the same way as Polymarket
# downstream (filter → classify_pools → eval → fire). Key differences:
#   - No platform fee (only Base gas ~$0.01) → tighter THRESH_LIMITLESS=0.99
#   - Smaller volume than Polymarket (~$3M vs $110M daily) but proportionally
#     less competition, so spreads stay wider.
def _fetch_limitless_orderbook(slug):
    """GET /markets/{slug}/orderbook → returns asks/bids per token.
    Limitless orderbook returns a single token's book (per-outcome).
    Unlike Polymarket it doesn't have explicit YES/NO token ids in the
    list response — we ask per slug and the response includes its tokenId.

    Returns (slug, best_ask_yes, depth_yes, best_ask_no, depth_no).
    For binary markets the slug usually has one orderbook for YES; the NO
    side is the inverse (1 - best_bid). For multi-outcome (negRisk) groups
    each child slug has its own orderbook.

    Performance: when Limitless WS is connected and has a fresh book for
    this slug (≤2s old), we serve from the WS cache and skip the REST call.
    Saves ~50-200ms per slug per scan, lets us run more pages without bumping
    rate limits.
    """
    # Prefer WS cache for hot tokens — falls back to REST if stale/missing.
    if lim_ws_client is not None:
        cached = lim_ws_client.get_book(slug)
        if cached and (time.time() - cached.get('ts', 0)) < 2.0:
            yes_ask = cached.get('best_yes_ask')
            yes_bid = cached.get('best_yes_bid')
            no_ask = (1 - yes_bid) if (yes_bid is not None and 0 < yes_bid < 1) else None
            return (slug, yes_ask, cached.get('depth_yes', 0),
                    no_ask, cached.get('depth_no', 0))
    try:
        r = requests.get(f"{LIMITLESS_API_BASE}/markets/{slug}/orderbook", timeout=TIMEOUT)
        if r.status_code != 200:
            return slug, None, 0, None, 0
        ob = r.json()
        asks = ob.get('asks') or []
        bids = ob.get('bids') or []
        # YES-side ask = lowest sell price (what taker pays to BUY YES)
        best_yes_ask, depth_yes = None, 0
        if asks:
            try:
                ask_prices = sorted(float(a.get('price', 999)) for a in asks)
                best_yes_ask = ask_prices[0]
                depth_yes = sum(float(a.get('price', 0)) * float(a.get('size', 0)) for a in asks)
            except Exception:
                pass
        # NO-side ask = 1 - best YES bid (no-arbitrage: yes_ask + no_ask >= 1).
        # If best YES bid = 0.55, taker can effectively buy NO at 0.45 by
        # selling YES at 0.55 against an existing buyer = same trade.
        # On Limitless, NO is also tradable directly via separate orderbook
        # (each outcome has its own slug in negRisk groups), but for binary
        # we synthesise NO-ask from YES-bid.
        best_no_ask, depth_no = None, 0
        if bids:
            try:
                bid_prices = sorted((float(b.get('price', 0)) for b in bids), reverse=True)
                best_yes_bid = bid_prices[0]
                if 0 < best_yes_bid < 1:
                    best_no_ask = 1 - best_yes_bid
                    depth_no = sum(float(b.get('price', 0)) * float(b.get('size', 0)) for b in bids)
            except Exception:
                pass
        return slug, best_yes_ask, depth_yes, best_no_ask, depth_no
    except Exception:
        return slug, None, 0, None, 0


def _fetch_limitless_market_meta(slug):
    """GET /markets/{slug} → tokens.{yes,no}, venue.exchange, isOther, volume.

    Cached per-process: tokens and venue are immutable for a deployed CTF
    condition, so we never re-fetch them. Volume changes — we refresh the
    whole record every LIM_META_REFRESH_S so HOT-pool ordering can react.
    Returns the cached dict, or None if both fetch and cache miss.

    Why this exists: atomic._build_leg cannot construct a real Limitless
    order without `tokenId` (uint256 in EIP-712 Order) and a per-market
    `verifyingContract` (in EIP-712 domain). Without these, every dry-run
    leg posts `tokenId='0'` which the server rejects.
    """
    now = time.time()
    with lim_meta_lock:
        cached = lim_meta_cache.get(slug)
    if cached and (now - cached.get('fetched_at', 0)) < LIM_META_REFRESH_S:
        return cached
    try:
        r = requests.get(f"{LIMITLESS_API_BASE}/markets/{slug}", timeout=TIMEOUT)
        if r.status_code != 200:
            return cached  # stale better than None
        m = r.json()
        toks = m.get('tokens') or {}
        venue = m.get('venue') or {}
        rec = {
            'yes_token': str(toks.get('yes')) if toks.get('yes') is not None else None,
            'no_token': str(toks.get('no')) if toks.get('no') is not None else None,
            'verifying_contract': venue.get('exchange'),
            'volume': float(m.get('volume') or 0),
            'is_other': bool(m.get('isOther')),
            'fetched_at': now,
        }
        with lim_meta_lock:
            lim_meta_cache[slug] = rec
        return rec
    except Exception:
        return cached


def _fetch_poly_market_info(condition_id: str):
    """GET /markets/{condition_id} → tick / min-size / fees / neg_risk.

    Phase 9j (28.04.2026) — V2 migration polish. V2 made `feeRateBps` a
    per-market dynamic value queryable via this endpoint instead of the
    hardcoded ~2.5% we used. Real V2 fees vary 0-2.5% per market.

    Returns dict or None on error. Cached POLY_MARKET_INFO_REFRESH_S.
    """
    if not condition_id:
        return None
    now = time.time()
    with poly_market_info_lock:
        cached = poly_market_info_cache.get(condition_id)
    if cached and (now - cached.get('fetched_at', 0)) < POLY_MARKET_INFO_REFRESH_S:
        return cached
    try:
        r = requests.get(
            f"https://clob.polymarket.com/markets/{condition_id}",
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return cached   # stale better than None — keep last known
        m = r.json() or {}
        rec = {
            'condition_id': condition_id,
            # API returns floats/ints; tick is already decimal (0.01),
            # min_order_size is in USDC (e.g. 5), fees are in bps (e.g. 250 = 2.5%)
            'tick_size': float(m.get('minimum_tick_size') or 0.01),
            'min_order_size': float(m.get('minimum_order_size') or 1),
            'maker_fee_bps': float(m.get('maker_base_fee') or 0),
            'taker_fee_bps': float(m.get('taker_base_fee') or 0),
            'neg_risk': bool(m.get('neg_risk')),
            'accepting_orders': bool(m.get('accepting_orders')),
            'enable_order_book': bool(m.get('enable_order_book')),
            'closed': bool(m.get('closed')),
            'archived': bool(m.get('archived')),
            'active': bool(m.get('active')) if m.get('active') is not None else True,
            # Phase 9m additions (research 28.04.2026):
            # - accepting_order_timestamp: UNIX seconds when book opens
            #   for orders. Pre-market events have this in the future.
            # - seconds_delay: server-side matchmaking delay (commonly 3
            #   for sport books). Influences cancel TTL / drift budget.
            # - neg_risk_market_id / neg_risk_request_id: needed when
            #   constructing negRisk-specific signed payloads.
            'accepting_order_timestamp': int(m.get('accepting_order_timestamp') or 0),
            'seconds_delay': int(m.get('seconds_delay') or 0),
            'neg_risk_market_id': m.get('neg_risk_market_id'),
            'neg_risk_request_id': m.get('neg_risk_request_id'),
            # rewards.{rates,min_size,max_spread} — relevant only for
            # maker strategy. We're a taker; preserve raw for analytics.
            'rewards': m.get('rewards') or {},
            'fetched_at': now,
        }
        with poly_market_info_lock:
            poly_market_info_cache[condition_id] = rec
        return rec
    except Exception:
        return cached


def batch_fetch(fn, ids):
    results = {}
    if not ids: return results
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futs = [pool.submit(fn, i) for i in ids]
        for f in as_completed(futs):
            try:
                res = f.result()
                results[res[0]] = res[1:]
            except: pass
    return results

# ── Deal Builder ────────────────────────────────────────────────
# Risk-aware sizing — cap deal stake by both min_liquidity AND the per-trade
# risk limit (MAX_PER_TRADE_USD from feedback memory, default $55). Without
# this cap, the executor's risk gate would block every Polymarket arb because
# default BALANCE ($100) > MAX_PER_TRADE_USD ($55), and paper_results.jsonl
# would never accumulate. (Found 28.04.2026 after first 32h dry-run.)
try:
    from risk import MAX_PER_TRADE_USD as _RISK_PER_TRADE_CAP
except Exception:
    _RISK_PER_TRADE_CAP = 55.0   # safe default matching feedback memory

def build_deal(title, platform, outcomes, total_price, theta, threshold,
               payout_target: float = 1.0):
    """Build a deal record (sized stakes + grade + economics).

    `payout_target`: $ guaranteed payout per $1 of contracts purchased.
      - ALL_YES (one outcome wins, gets $1): payout_target = 1.0
      - YES_NO_PAIR per market (always pays $1): 1.0
      - ALL_NO with N outcomes (N-1 of them pay $1 each): payout_target = N-1
      - SX Bet binary: 1.0
    Phase 9i (28.04.2026) fix — without this, ALL_NO gross was computed
    as (1 - sum_no) which goes hugely negative for N>=3 (e.g. N=3, sum=1.95
    → gross = -52.25 on $55 stake → net<=0 filter dropped EVERY ALL_NO arb).
    Now: gross = (payout_target - total_price) * actual_balance.
    """
    min_liq = float('inf')
    for o in outcomes:
        liq = o.get('liquidity', 0)
        if liq > 0 and liq < min_liq: min_liq = liq
    if min_liq == float('inf'): min_liq = 0

    max_share = max(o['price']/total_price for o in outcomes) if total_price > 0 else 0
    max_theoretical_stake = BALANCE * max_share

    scale_factor = 1.0
    # Liquidity scale — never put a leg larger than min_liq
    if min_liq > 0 and max_theoretical_stake > min_liq:
        scale_factor = min_liq / max_theoretical_stake
    elif min_liq == 0:
        scale_factor = 0.1 # safety

    # Per-trade risk-cap scale — Phase 9i: cap is per-LEG, so what matters
    # is `max_leg_stake = actual_balance * max_share`. Solve so that
    # max_leg_stake <= _RISK_PER_TRADE_CAP.
    target_max_leg = _RISK_PER_TRADE_CAP
    if max_share > 0 and BALANCE * scale_factor * max_share > target_max_leg:
        scale_factor = target_max_leg / (BALANCE * max_share)

    actual_balance = BALANCE * scale_factor
    # Gross = guaranteed payout − cost. payout_target=1 for ALL_YES /
    # binary, =N-1 for ALL_NO so the same formula works for both.
    gross = actual_balance * (payout_target - total_price)
    
    total_fee = 0; entries = []
    for o in outcomes:
        stake = actual_balance * (o['price'] / total_price) if total_price > 0 else 0
        contracts = stake / o['price'] if o['price'] > 0 else 0
        fee = calc_fee(o['price'], contracts, theta)
        total_fee += fee
        entries.append({
            'name': o['name'], 'price_cents': round(o['price']*100,1),
            'coeff': round(1/o['price'],1) if o['price']>0 else 0,
            'stake': round(stake,2), 'contracts': round(contracts,1),
            'fee': round(fee,4), 'liquidity': round(o.get('liquidity',0),0),
            'share_pct': round(o['price']/total_price*100,1) if total_price>0 else 0,
            'source': o.get('source','?')
        })
    
    net = gross - total_fee
    if net <= 0: return None # Filter out non-profitable immediately

    roi = net / actual_balance * 100 if actual_balance > 0 else 0
    max_stake = max(e['stake'] for e in entries) if entries else 0
    slip_pct = min(5.0, (max_stake/min_liq)*100) if min_liq>0 and max_stake>0 else 5.0
    slip_cost = actual_balance * slip_pct / 100
    adj = net - slip_cost
    liq_ok = all(e['liquidity']>=50 for e in entries if e['liquidity']>0)
    
    if adj>20 and liq_ok: grade="A+"
    elif adj>10: grade="A"
    elif adj>5: grade="B"
    elif adj>2: grade="C"
    elif adj>0: grade="D"
    else: grade="F"
    
    if min_liq>max_stake*10: risk="LOW"
    elif min_liq>max_stake*3: risk="LOW"
    elif min_liq>max_stake: risk="MED"
    elif min_liq>0: risk="HIGH"
    else: risk="CRIT"
    
    return {
        'title': title, 'platform': platform, 'outcomes': len(outcomes),
        'total_cents': round(total_price*100,1), 'threshold': round(threshold*100,0),
        'spread_cents': round((threshold-total_price)*100,1),
        'gross': round(gross,2), 'gross_pct': round((1-total_price)*100,1),
        'fee': round(total_fee,3), 'fee_pct': round(total_fee/actual_balance*100,2) if actual_balance else 0,
        'net': round(net,2), 'roi': round(roi,1),
        'slip_pct': round(slip_pct,2), 'slip_cost': round(slip_cost,2),
        'adj': round(adj,2), 'adj_roi': round(adj/actual_balance*100,1) if actual_balance else 0,
        'min_liq': round(min_liq,0), 'max_stake': round(max_stake,2),
        'balance_used': round(actual_balance,2),
        'liq_ok': liq_ok, 'grade': grade, 'risk': risk, 'theta': theta,
        'entries': entries, 'scan_time': datetime.now(timezone.utc).isoformat()
    }

# ── Evaluate Candidates ────────────────────────────────────────
def _poly_per_market(rough, clob_res, ws_books=None):
    """Per-market YES/NO price+liquidity snapshot. Used by 3-structure evaluator
    and by NEAR-pool classification. Source priority: WS book → REST clob → implied."""
    ws_books = ws_books or {}
    clob_res = clob_res or {}
    out = []
    for o in rough:
        m = o['m']
        name = m.get('question', m.get('groupItemTitle', '?'))
        yes_tid = o.get('token_id_yes') or o.get('token_id')
        no_tid = o.get('token_id_no')
        # YES side
        yes_price = o['implied']; yes_liq = float(m.get('liquidity',0) or 0); yes_src = 'implied'
        if yes_tid:
            b = ws_books.get(yes_tid)
            if b and b.get('best_ask') and 0 < b['best_ask'] < 1:
                yes_price = b['best_ask']; yes_liq = b.get('depth') or yes_liq; yes_src = 'ws'
            elif yes_tid in clob_res:
                ask, depth = clob_res[yes_tid]
                if ask and 0 < ask < 1:
                    yes_price = ask; yes_liq = depth or yes_liq; yes_src = 'clob_ask'
        # NO side — fall back to (1 - yes_implied) when no real book is available
        no_price = (1 - o['implied']) if 0 < o['implied'] < 1 else None
        no_liq = 0; no_src = 'implied'
        if no_tid:
            b = ws_books.get(no_tid)
            if b and b.get('best_ask') and 0 < b['best_ask'] < 1:
                no_price = b['best_ask']; no_liq = b.get('depth') or no_liq; no_src = 'ws'
            elif no_tid in clob_res:
                ask, depth = clob_res[no_tid]
                if ask and 0 < ask < 1:
                    no_price = ask; no_liq = depth or no_liq; no_src = 'clob_ask'
        out.append({
            'name': name, 'volume': float(m.get('volume',0) or 0),
            'yes_price': yes_price, 'yes_liq': yes_liq, 'yes_src': yes_src,
            'no_price': no_price, 'no_liq': no_liq, 'no_src': no_src,
        })
    return out

def _attach_poly_v2_meta(deal: dict, rough: list, no_only: bool = False):
    """Attach V2 per-market metadata (tick_size, min_order_size, neg_risk,
    condition_id) to each leg's entry. Used by atomic.build_poly_order to
    validate price tick alignment + min order size + select correct
    EIP-712 domain (negRisk vs standard).

    `rough` is the list of market candidates parsed by filter_poly. We
    match leg index → rough[i] in the order they appear (build_deal
    preserves outcome order).
    """
    entries = deal.get('entries') or []
    for i, e in enumerate(entries):
        # For YES_NO_PAIR each entry maps to ONE market (rough[0] usually);
        # for ALL_YES/ALL_NO entries map 1:1 to rough.
        idx = 0 if len(rough) == 1 else min(i, len(rough) - 1)
        if no_only and len(rough) > 1:
            # ALL_NO sometimes has fewer NOs than rough length (filtered);
            # we still attach the closest-by-name market info.
            pass
        m = rough[idx]['m'] if idx < len(rough) else None
        if not m:
            continue
        cid = m.get('conditionId') or m.get('condition_id')
        info = _fetch_poly_market_info(cid) if cid else None
        if info:
            e['condition_id'] = cid
            e['tick_size'] = info['tick_size']
            e['min_order_size'] = info['min_order_size']
            e['neg_risk'] = info['neg_risk']
            e['taker_fee_bps'] = info['taker_fee_bps']
            # Phase 9m: attach status flags for pre-fire gate. atomic
            # checks these RIGHT before POST and aborts the leg if the
            # market closed/disabled between scan and fire.
            e['accepting_orders'] = info.get('accepting_orders', True)
            e['enable_order_book'] = info.get('enable_order_book', True)
            e['accepting_order_timestamp'] = info.get('accepting_order_timestamp', 0)
            e['seconds_delay'] = info.get('seconds_delay', 0)
            # neg_risk_market_id needed for the signed payload's market
            # reference field on negRisk markets. Stored for downstream
            # builder use; current build_poly_order doesn't yet require it.
            e['neg_risk_market_id'] = info.get('neg_risk_market_id')
        # token_id_yes/no already attached during filter_poly — leave alone


def _eval_poly_structures(cand, clob_res=None, ws_books=None):
    """Returns a list of deals — one per arb structure (A/B/C) that crosses
    its threshold. Empty list if none. Used by both batch eval_poly and the
    WS push callback (single-candidate refresh).

    Phase 9g (28.04.2026) — coverage rule: ALL_YES and ALL_NO must price
    EVERY outcome of the event. If even one outcome was dropped during
    filter (no outcomePrices, no clob token, etc.) we silently
    over-counted before — see Limitless EPL Leeds-vs-Burnley case.
    Standalone YES_NO_PAIR is still safe per-market.
    """
    ev, rough, is_q = cand
    per_market = _poly_per_market(rough, clob_res, ws_books)
    if len(per_market) < 2: return []
    # Total outcomes the event actually has on the book — comes from
    # the gamma payload's `markets` list, NOT from our filtered `rough`.
    # If filter dropped any (missing outcomePrices, parse fail, etc.) the
    # count differs and we must reject ALL_YES / ALL_NO.
    total_outcomes_on_event = len(ev.get('markets') or []) or len(per_market)
    full_coverage = (len(per_market) == total_outcomes_on_event)

    # Phase 9j: pull V2 dynamic per-market fee/tick/min_size. We use the
    # WORST (highest) taker fee across this event's markets — pessimistic
    # ranking, so net is never overestimated. Tick/min_size attached to
    # each leg so atomic._build_leg can validate before signing.
    market_infos = []
    for o in rough:
        cid = o['m'].get('conditionId') or o['m'].get('condition_id')
        if cid:
            info = _fetch_poly_market_info(cid)
            if info:
                market_infos.append(info)
    if market_infos:
        max_taker_fee_bps = max(i['taker_fee_bps'] for i in market_infos)
        # Convert bps → fraction. theta is the "per-$1-of-stake fee" multiplier
        # so taker_fee_bps=250 (2.5%) → theta=0.025.
        effective_theta = max_taker_fee_bps / 10000.0
    else:
        # No info available (cache miss + fetch fail) — fall back to old
        # conservative default. Better safe than over-firing.
        effective_theta = THETA_POLY
        max_taker_fee_bps = THETA_POLY * 10000

    # Phase 9k: dynamic threshold based on actual fee. On 0%-fee markets
    # we now accept arbs up to 0.992 (vs old static 0.97 — caught nothing
    # tighter than 3¢ margin); on 3%+ fee we tighten to 0.962 (vs old
    # 0.97 which would let through losers). See compute_poly_threshold.
    dyn_threshold = compute_poly_threshold(max_taker_fee_bps)

    title = ev.get('title', '?')
    end_date = ev.get('endDate')   # ISO 8601, e.g. "2026-05-24T23:59:59Z"
    deals = []

    def _quality_ok(d):
        if d['total_cents'] >= 95.0:
            if d['min_liq'] < 1000 or d['slip_pct'] >= 0.3: return False
        return True

    def _attach(d):
        """Common per-deal metadata: end_date so analytics history can show
        when capital becomes free, is_quarantine flag, etc."""
        if d:
            d['end_date'] = end_date
        return d

    # ── A. ALL_YES ──────────────────────────────────────────────────
    yes_out = [{'name': p['name'], 'price': p['yes_price'],
                'liquidity': p['yes_liq'], 'source': p['yes_src'],
                'volume': p['volume']} for p in per_market]
    total_yes = sum(o['price'] for o in yes_out)
    if full_coverage and total_yes < dyn_threshold:
        d = build_deal(title, 'Polymarket', yes_out, total_yes, effective_theta, dyn_threshold)
        if d:
            d['is_quarantine'] = is_q; d['arb_structure'] = 'all_yes'
            _attach(d)
            # Phase 9j: attach per-market V2 metadata to each leg (tick / min /
            # neg_risk) so atomic.build_poly_order can validate before signing.
            _attach_poly_v2_meta(d, rough)
            if _quality_ok(d): deals.append(d)

    # ── B. ALL_NO (N>=3, multi-outcome) ─────────────────────────────
    # Same coverage rule — drop if any outcome lacks a NO price OR if
    # filter dropped some outcomes upstream.
    no_raw = [p for p in per_market if p['no_price'] is not None and 0 < p['no_price'] < 1]
    N = len(no_raw)
    if N >= 3 and N == total_outcomes_on_event:
        no_out = [{'name': f"NO {p['name']}", 'price': p['no_price'],
                   'liquidity': p['no_liq'], 'source': p['no_src'],
                   'volume': p['volume']} for p in no_raw]
        total_no = sum(o['price'] for o in no_out)
        no_threshold = (N - 1) * dyn_threshold
        if total_no < no_threshold:
            # Phase 9i: pass payout_target=N-1 so build_deal computes gross
            # correctly. Old code mistakenly used (1 - total_no) which goes
            # huge negative for N≥3 → net<=0 filter killed all ALL_NO arbs.
            d = build_deal(title + ' (ALL_NO)', 'Polymarket', no_out,
                           total_no, effective_theta, no_threshold,
                           payout_target=float(N - 1))
            if d:
                d['is_quarantine'] = is_q; d['arb_structure'] = 'all_no'
                d['payout_target'] = N - 1
                _attach(d)
                _attach_poly_v2_meta(d, rough, no_only=True)
                deals.append(d)

    # ── C. YES_NO_PAIR (per-market) ─────────────────────────────────
    # Per-market: each market has its OWN fee/threshold (other markets
    # in the event don't constrain it). Re-fetch per leg if available.
    for idx, p in enumerate(per_market):
        if p['no_price'] is None or not (0 < p['no_price'] < 1): continue
        if not (0 < p['yes_price'] < 1): continue
        # Pick this leg's actual market info if available
        leg_theta = effective_theta
        leg_threshold = dyn_threshold
        if idx < len(market_infos):
            leg_fee_bps = market_infos[idx]['taker_fee_bps']
            leg_theta = leg_fee_bps / 10000.0
            leg_threshold = compute_poly_threshold(leg_fee_bps)
        pair_total = p['yes_price'] + p['no_price']
        if pair_total >= leg_threshold: continue
        pair_out = [
            {'name': f"YES {p['name']}", 'price': p['yes_price'],
             'liquidity': p['yes_liq'], 'source': p['yes_src'], 'volume': p['volume']},
            {'name': f"NO {p['name']}", 'price': p['no_price'],
             'liquidity': p['no_liq'], 'source': p['no_src'], 'volume': p['volume']},
        ]
        d = build_deal(f"{title} — {p['name']}", 'Polymarket', pair_out,
                       pair_total, leg_theta, leg_threshold)
        if d:
            d['is_quarantine'] = is_q; d['arb_structure'] = 'yes_no_pair'
            _attach(d)
            _attach_poly_v2_meta(d, [next(r for r in rough
                                           if r['m'].get('question') == p['name']
                                           or r['m'].get('groupItemTitle') == p['name'])]
                                  if any(r['m'].get('question') == p['name']
                                         or r['m'].get('groupItemTitle') == p['name']
                                         for r in rough) else rough)
            if _quality_ok(d): deals.append(d)
    return deals

def eval_poly(cands, clob_res):
    """Batch evaluator. Returns deals across all 3 arb structures (A/B/C)."""
    deals = []
    for cand in cands:
        deals.extend(_eval_poly_structures(cand, clob_res=clob_res))
    return deals

def eval_kalshi(cands, kalshi_res):
    """Evaluate all three arb structures for Kalshi events:
        A. ALL_YES — sum(yes_ask) < THRESH_KALSHI
        B. ALL_NO  — sum(no_ask)  < (N-1) * THRESH_KALSHI  (multi-outcome only)
        C. YES_NO_PAIR (per market): yes_ask + no_ask < THRESH_KALSHI
    """
    deals = []
    for cand in cands:
        ev, tickers = cand
        # Kalshi event-level close_time, fallback to per-market field below
        end_date = ev.get('close_time') or ev.get('expected_expiration_time')
        # Phase 9g coverage gate — track total outcomes vs priced
        total_outcomes_on_event = len(ev.get('markets') or [])
        per_market = []
        for m in ev.get('markets', []):
            t = m.get('ticker','')
            if t not in kalshi_res: continue
            yes_ask, yes_depth, no_ask, no_depth = kalshi_res[t]
            if yes_ask is None or yes_ask < 0.05 or yes_ask >= 1: continue
            per_market.append({
                'name': m.get('title', t), 'ticker': t,
                'yes_price': yes_ask, 'yes_liq': yes_depth,
                'no_price': no_ask if (no_ask and 0 < no_ask < 1) else None,
                'no_liq': no_depth or 0,
                # Per-market close_time (Kalshi sometimes has both)
                'end_date': m.get('close_time') or end_date,
            })
        if len(per_market) < 2: continue
        full_coverage = (len(per_market) == total_outcomes_on_event)

        # ── A. ALL_YES ──────────────────────────────────────────────
        # Coverage required — uncovered outcome winning kills the arb.
        yes_outcomes = [{'name': p['name'], 'price': p['yes_price'],
                         'liquidity': p['yes_liq'], 'source': 'kalshi_ob'}
                        for p in per_market]
        total_yes = sum(o['price'] for o in yes_outcomes)
        if (full_coverage and 0.50 <= total_yes < THRESH_KALSHI
                and any(o['price'] > 0.20 for o in yes_outcomes)):
            d = build_deal(ev.get('title','?'), 'Kalshi', yes_outcomes,
                           total_yes, THETA_KALSHI, THRESH_KALSHI)
            if d:
                d['arb_structure'] = 'all_yes'; d['end_date'] = end_date
                deals.append(d)

        # ── B. ALL_NO (N>=3) — coverage required ────────────────────
        no_raw = [p for p in per_market if p['no_price'] is not None]
        N = len(no_raw)
        if N >= 3 and N == total_outcomes_on_event:
            no_outcomes = [{'name': f"NO {p['name']}", 'price': p['no_price'],
                            'liquidity': p['no_liq'], 'source': 'kalshi_ob'}
                           for p in no_raw]
            total_no = sum(o['price'] for o in no_outcomes)
            no_threshold = (N - 1) * THRESH_KALSHI
            if total_no < no_threshold:
                # Phase 9i: payout_target=N-1 for ALL_NO (see build_deal docstring)
                d = build_deal(ev.get('title','?') + ' (ALL_NO)', 'Kalshi',
                               no_outcomes, total_no, THETA_KALSHI, no_threshold,
                               payout_target=float(N - 1))
                if d:
                    d['arb_structure'] = 'all_no'; d['payout_target'] = N - 1
                    d['end_date'] = end_date
                    deals.append(d)

        # ── C. YES_NO_PAIR ──────────────────────────────────────────
        for p in per_market:
            if p['no_price'] is None: continue
            pair_total = p['yes_price'] + p['no_price']
            if pair_total >= THRESH_KALSHI: continue
            pair_out = [
                {'name': f"YES {p['name']}", 'price': p['yes_price'],
                 'liquidity': p['yes_liq'], 'source': 'kalshi_ob'},
                {'name': f"NO {p['name']}", 'price': p['no_price'],
                 'liquidity': p['no_liq'], 'source': 'kalshi_ob'},
            ]
            d = build_deal(p['name'], 'Kalshi', pair_out, pair_total,
                           THETA_KALSHI, THRESH_KALSHI)
            if d:
                d['arb_structure'] = 'yes_no_pair'
                d['end_date'] = p.get('end_date')
                deals.append(d)
    return deals

def _sx_market_title(m: dict) -> str:
    """Pretty title that disambiguates Moneyline vs Total vs Spread for the
    same matchup. Uses outcomeOneName/outcomeTwoName which already carry
    Over/Under and ±line annotations."""
    league = m.get('leagueLabel', '')
    o1 = m.get('outcomeOneName', m.get('teamOneName', 'Team 1'))
    o2 = m.get('outcomeTwoName', m.get('teamTwoName', 'Team 2'))
    return f"{o1} vs {o2} ({league})" if league else f"{o1} vs {o2}"

def eval_sx(sx_markets, sx_orders):
    """One deal per market (by marketHash), not per event. A single match
    can have Moneyline + Total + Spread + Period markets — each is an
    independent binary arb opportunity, so we evaluate them separately."""
    deals = []
    seen_hashes = set()
    for m in sx_markets:
        if m.get('type') not in SX_BINARY_TYPES: continue
        mh = m.get('marketHash', '')
        if not mh or mh in seen_hashes: continue
        seen_hashes.add(mh)

        # 30-day filter on gameTime
        if not is_within_10_days(timestamp=m.get('gameTime')): continue

        if mh not in sx_orders: continue
        best1, depth1, best2, depth2 = sx_orders[mh]
        if best1 is None or best2 is None: continue
        if best1 <= 0 or best2 <= 0: continue
        total = best1 + best2
        if total >= THRESH_SX: continue
        outcomes = [
            {'name': m.get('outcomeOneName', 'Team 1'), 'price': best1, 'liquidity': depth1, 'source': 'sx_ob'},
            {'name': m.get('outcomeTwoName', 'Team 2'), 'price': best2, 'liquidity': depth2, 'source': 'sx_ob'},
        ]
        deal = build_deal(_sx_market_title(m), 'SX Bet', outcomes, total, THETA_SX, THRESH_SX)
        if deal:
            # SX Bet markets are inherently binary (outcomeOne vs outcomeTwo).
            # All three arb structures collapse to the same shape here.
            deal['arb_structure'] = 'binary'
            # SX gameTime is unix-seconds; normalise to ISO-8601 for analytics
            game_ts = m.get('gameTime')
            if isinstance(game_ts, (int, float)) and game_ts > 0:
                deal['end_date'] = datetime.fromtimestamp(game_ts, tz=timezone.utc).isoformat()
            deals.append(deal)
    return deals


def _lim_quality_ok(d, per_market):
    """Drop ultra-tight Limitless deals that look attractive on paper but
    fall apart in execution. Same intent as Polymarket's _quality_ok but
    tuned to Limitless economics:
      - When sum is ≥ 95¢ (margin <5¢), require min_liq ≥ $200 — cheap-margin
        arbs need depth, otherwise slippage eats it. (Polymarket uses $1000
        because Polymarket has 2.5% taker fee already baked in, and Polymarket
        markets are 30x bigger.)
      - Slippage cap kept at 0.3% same as Polymarket — same orderbook math.
      - Block deals where ALL legs have $0 reported volume — most likely a
        ghost market or stale price; we'd happily fire and not get filled.
    """
    if d['total_cents'] >= 95.0:
        if d.get('min_liq', 0) < 200 or d.get('slip_pct', 0) >= 0.3:
            return False
    if per_market:
        all_dead = all((p.get('volume', 0) or 0) <= 0 for p in per_market)
        if all_dead:
            return False
    return True


def filter_limitless(events, diag=None):
    """Apply parity filters to Limitless events before evaluation.

    Same gate set as filter_poly so analytics and quarantine logic behave
    identically across platforms:
      - 10-day window (`deadline` / `expirationTimestamp`)
      - blacklist by event title (operator-curated)
      - is_deadline() text-pattern reject — events whose title pattern is
        "By March 31" / "Before Q4 2026" tend to resolve ambiguously and
        produce phantom arbs
      - quarantine: hidden "Other" outcome → keep deal but mark
        is_quarantine=True so the executor refuses to fire it

    Returns list of (event, is_quarantine) tuples — callers iterate and
    propagate `is_quarantine` to deals via build_deal extras.
    """
    if diag is None: diag = {}
    diag['lim_in'] = len(events)
    for k in ('lim_skip_blacklist', 'lim_skip_no_window', 'lim_skip_deadline_text',
              'lim_pass', 'lim_quarantine', 'lim_skip_outcome_closed'):
        diag.setdefault(k, 0)

    out = []
    for ev in events:
        title = ev.get('title') or ev.get('proxyTitle') or '?'
        if title in blacklist:
            diag['lim_skip_blacklist'] += 1; continue

        # Phase 9h (28.04.2026): per-outcome status gate. If ANY child market
        # is closed/expired/hidden, ALL_YES + ALL_NO arbs are dangerous —
        # a closed outcome can still win at resolution but we can't buy YES
        # on it (orderbook gone), so the bookkeeping looks like an arb in
        # the priced subset but reality leaves us uncovered.
        # See discussion 28.04: "leeds 67.5 / draw 20.6 / burnley 13, draw
        # closes mid-event — what if Draw still wins?"
        # Rule: drop the whole event from consideration. PR #26 catches
        # missing prices at eval time; this catch is at FILTER level so
        # the event never even enters HOT/NEAR pools or analytics.
        ev_status = (ev.get('status') or '').upper()
        ev_closed = (ev.get('expired') or ev.get('hidden')
                     or ev_status in ('CLOSED', 'RESOLVED', 'PAUSED', 'SUSPENDED'))
        if ev_closed:
            diag['lim_skip_outcome_closed'] += 1; continue

        deadline = ev.get('deadline') or ev.get('expirationTimestamp')
        if isinstance(deadline, (int, float)):
            ts = deadline / 1000 if deadline > 1e12 else deadline
            if not is_within_10_days(timestamp=ts):
                diag['lim_skip_no_window'] += 1; continue
        elif isinstance(deadline, str):
            if not is_within_10_days(date_str=deadline):
                diag['lim_skip_no_window'] += 1; continue
        else:
            diag['lim_skip_no_window'] += 1; continue

        # Title-based deadline reject (events about "By Mar 31" type questions)
        # — applies to standalone events and to groups via child titles.
        children = ev.get('markets') or []
        names = [c.get('title') or c.get('proxyTitle') or '' for c in children]
        if not names: names = [title]
        if is_deadline(names):
            diag['lim_skip_deadline_text'] += 1; continue

        # Per-child status gate. Drops the whole multi-outcome event if even
        # ONE child is closed/expired — see comment block above.
        if children:
            child_closed = False
            for c in children:
                cs = (c.get('status') or '').upper()
                if (c.get('expired') or c.get('hidden')
                        or cs in ('CLOSED', 'RESOLVED', 'PAUSED', 'SUSPENDED')):
                    child_closed = True
                    break
            if child_closed:
                diag['lim_skip_outcome_closed'] += 1; continue

        # Quarantine — hidden "Other" outcome. Two signals:
        #  (1) Limitless API exposes a per-market boolean `isOther` directly
        #      (verified 28.04.2026 — present on every /markets/{slug} response)
        #  (2) heuristic title match via has_other_outcome — covers Polymarket-
        #      style events imported into Limitless that don't set isOther.
        api_other = bool(ev.get('isOther')) or any(
            bool((c or {}).get('isOther')) for c in children)
        is_quarantine = api_other or has_other_outcome(names + [title])
        if is_quarantine:
            diag['lim_quarantine'] += 1
        out.append((ev, is_quarantine))
        diag['lim_pass'] += 1
    return out


def eval_limitless(events, lim_res, diag=None):
    """Evaluate Limitless Exchange events for arb structures A/B/C.

    `events` is the raw list returned by /markets/active (each event is a
    market or a negRisk group). `lim_res` maps slug → (best_yes_ask,
    depth_yes, best_no_ask, depth_no) from _fetch_limitless_orderbook.

    Limitless event shape (from openapi.json):
        - Binary market: {slug, title, deadline, prices:[yes,no], liquidity, ...}
        - NegRisk group: {slug, title, markets:[{slug, title, prices, ...}]}
    For groups we treat each child market as a YES outcome of the umbrella
    event and apply the same A/ALL_YES, B/ALL_NO, C/YES_NO_PAIR logic as
    Polymarket. For standalone binary markets we run only structure C.

    Phase 9b (28.04.2026): events run through filter_limitless first so we
    apply blacklist + 10-day window + is_deadline text reject + Other
    quarantine — parity with filter_poly.
    """
    deals = []
    filtered = filter_limitless(events, diag=diag)
    for ev, is_quarantine in filtered:
        deadline = ev.get('deadline') or ev.get('expirationTimestamp')
        title = ev.get('title') or ev.get('proxyTitle') or '?'
        end_date_iso = None
        if isinstance(deadline, (int, float)):
            end_date_iso = datetime.fromtimestamp(
                deadline / 1000 if deadline > 1e12 else deadline,
                tz=timezone.utc,
            ).isoformat()
        elif isinstance(deadline, str):
            end_date_iso = deadline

        # Two shapes: negRisk group with `markets[]`, or single binary market
        children = ev.get('markets') or []
        if children:
            # NegRisk group — treat as multi-outcome A/B/C event
            #
            # CRITICAL coverage rule (Phase 9g, fix 28.04.2026): we MUST track
            # how many outcomes the event actually has vs how many we managed
            # to price. If even ONE outcome is missing an ask price, ALL_YES
            # and ALL_NO are NOT real arbs — that uncovered outcome can win
            # and we lose every leg.
            #
            # Real-world example that triggered this fix: EPL Leeds vs Burnley
            # had 3 outcomes (Leeds, Draw, Burnley); Draw had volume=0 so its
            # orderbook was empty → yes_ask=None. Old code silently dropped
            # Draw and reported sum(Leeds + Burnley) = 80.5¢ as an "arb".
            # Real sum across all 3 was 101.1¢ — a guaranteed loss if Draw won.
            total_outcomes = len(children)
            per_market = []
            outcomes_missing_yes = 0
            outcomes_missing_no = 0
            for child in children:
                slug = child.get('slug') or child.get('address')
                if not slug or slug not in lim_res:
                    outcomes_missing_yes += 1
                    outcomes_missing_no += 1
                    continue
                yes_ask, yes_depth, no_ask, no_depth = lim_res[slug]
                if yes_ask is None or not (0 < yes_ask < 1):
                    outcomes_missing_yes += 1
                    if no_ask is None or not (0 < no_ask < 1):
                        outcomes_missing_no += 1
                    continue
                if no_ask is None or not (0 < no_ask < 1):
                    outcomes_missing_no += 1
                # Pull token IDs + venue.exchange so atomic._build_leg can
                # construct a real EIP-712 order. Cached forever per slug.
                meta = _fetch_limitless_market_meta(slug) or {}
                per_market.append({
                    'name': child.get('title') or child.get('proxyTitle') or '?',
                    'slug': slug,
                    'yes_price': yes_ask, 'yes_liq': yes_depth or 0,
                    'no_price': no_ask if (no_ask and 0 < no_ask < 1) else None,
                    'no_liq': no_depth or 0,
                    'yes_token': meta.get('yes_token'),
                    'no_token': meta.get('no_token'),
                    'verifying_contract': meta.get('verifying_contract'),
                    'volume': meta.get('volume', 0),
                })
            if len(per_market) < 2:
                continue
            full_yes_coverage = (outcomes_missing_yes == 0)
            full_no_coverage = (outcomes_missing_no == 0)

            # Structure A: ALL_YES
            # Gated on full_yes_coverage — if even one outcome lacks an ask,
            # we can't actually buy YES on every winning path → not an arb.
            yes_outcomes = [{'name': p['name'], 'price': p['yes_price'],
                             'liquidity': p['yes_liq'], 'source': 'lim_clob',
                             'volume': p.get('volume', 0)}
                            for p in per_market]
            total_yes = sum(o['price'] for o in yes_outcomes)
            if full_yes_coverage and total_yes < THRESH_LIMITLESS:
                d = build_deal(title, 'Limitless', yes_outcomes, total_yes,
                               THETA_LIMITLESS, THRESH_LIMITLESS)
                if d:
                    d['arb_structure'] = 'all_yes'
                    d['is_quarantine'] = is_quarantine
                    d['end_date'] = end_date_iso
                    # Attach slug + token + verifying_contract per leg so
                    # atomic._build_leg can build a signed EIP-712 order.
                    for i, e in enumerate(d.get('entries', [])):
                        if i < len(per_market):
                            p = per_market[i]
                            e['slug'] = p['slug']
                            e['side'] = 'YES'
                            e['token_id'] = p['yes_token']
                            e['verifying_contract'] = p['verifying_contract']
                    if _lim_quality_ok(d, per_market):
                        deals.append(d)

            # Structure B: ALL_NO (N≥3) — ALSO gated on full coverage.
            # If outcome X has no NO ask, we can't buy NO_X, and X winning
            # would lose us all our purchased NO legs (we don't get paid
            # because our NOs only pay on the OTHER outcomes winning).
            no_raw = [p for p in per_market if p['no_price'] is not None]
            N = len(no_raw)
            # Require full NO coverage AND that the NO-coverage matches the
            # original outcome count, not just per_market — same rationale.
            if full_no_coverage and N == total_outcomes and N >= 3:
                no_outcomes = [{'name': f"NO {p['name']}", 'price': p['no_price'],
                                'liquidity': p['no_liq'], 'source': 'lim_clob',
                                'volume': p.get('volume', 0)}
                               for p in no_raw]
                total_no = sum(o['price'] for o in no_outcomes)
                no_threshold = (N - 1) * THRESH_LIMITLESS
                if total_no < no_threshold:
                    # Phase 9i: payout_target=N-1 for ALL_NO
                    d = build_deal(title + ' (ALL_NO)', 'Limitless',
                                   no_outcomes, total_no, THETA_LIMITLESS,
                                   no_threshold, payout_target=float(N - 1))
                    if d:
                        d['arb_structure'] = 'all_no'
                        d['is_quarantine'] = is_quarantine
                        d['payout_target'] = N - 1
                        d['end_date'] = end_date_iso
                        for i, e in enumerate(d.get('entries', [])):
                            if i < len(no_raw):
                                p = no_raw[i]
                                e['slug'] = p['slug']
                                e['side'] = 'NO'
                                e['token_id'] = p['no_token']
                                e['verifying_contract'] = p['verifying_contract']
                        if _lim_quality_ok(d, no_raw):
                            deals.append(d)

            # Structure C: YES_NO_PAIR per market
            for p in per_market:
                if p['no_price'] is None: continue
                pair_total = p['yes_price'] + p['no_price']
                if pair_total >= THRESH_LIMITLESS: continue
                pair_out = [
                    {'name': f"YES {p['name']}", 'price': p['yes_price'],
                     'liquidity': p['yes_liq'], 'source': 'lim_clob',
                     'volume': p.get('volume', 0)},
                    {'name': f"NO {p['name']}", 'price': p['no_price'],
                     'liquidity': p['no_liq'], 'source': 'lim_clob',
                     'volume': p.get('volume', 0)},
                ]
                d = build_deal(f"{title} — {p['name']}", 'Limitless', pair_out,
                               pair_total, THETA_LIMITLESS, THRESH_LIMITLESS)
                if d:
                    d['arb_structure'] = 'yes_no_pair'
                    d['is_quarantine'] = is_quarantine
                    d['end_date'] = end_date_iso
                    for e in d.get('entries', []):
                        is_yes = e['name'].startswith('YES ')
                        e['slug'] = p['slug']
                        e['side'] = 'YES' if is_yes else 'NO'
                        e['token_id'] = p['yes_token'] if is_yes else p['no_token']
                        e['verifying_contract'] = p['verifying_contract']
                    if _lim_quality_ok(d, [p]):
                        deals.append(d)
        else:
            # Standalone binary market — only structure C applies
            slug = ev.get('slug') or ev.get('address')
            if not slug or slug not in lim_res:
                continue
            yes_ask, yes_depth, no_ask, no_depth = lim_res[slug]
            if yes_ask is None or no_ask is None: continue
            if not (0 < yes_ask < 1) or not (0 < no_ask < 1): continue
            pair_total = yes_ask + no_ask
            if pair_total >= THRESH_LIMITLESS: continue
            meta = _fetch_limitless_market_meta(slug) or {}
            volume = meta.get('volume', 0)
            pair_out = [
                {'name': f"YES {title}", 'price': yes_ask,
                 'liquidity': yes_depth or 0, 'source': 'lim_clob',
                 'volume': volume},
                {'name': f"NO {title}", 'price': no_ask,
                 'liquidity': no_depth or 0, 'source': 'lim_clob',
                 'volume': volume},
            ]
            d = build_deal(title, 'Limitless', pair_out, pair_total,
                           THETA_LIMITLESS, THRESH_LIMITLESS)
            if d:
                d['arb_structure'] = 'binary'
                d['is_quarantine'] = is_quarantine
                d['end_date'] = end_date_iso
                d['slug'] = slug
                for e in d.get('entries', []):
                    is_yes = e['name'].startswith('YES ')
                    e['slug'] = slug
                    e['side'] = 'YES' if is_yes else 'NO'
                    e['token_id'] = meta.get('yes_token') if is_yes else meta.get('no_token')
                    e['verifying_contract'] = meta.get('verifying_contract')
                # Pseudo per_market for quality check on standalone binary
                pseudo_pm = [{
                    'yes_liq': yes_depth or 0,
                    'no_liq': no_depth or 0,
                    'volume': volume,
                }]
                if _lim_quality_ok(d, pseudo_pm):
                    deals.append(d)
    return deals


def _sum_limitless_cand(ev, lim_res):
    """For NEAR-pool classification — return min normalized sum across A/B/C
    structures (matches _sum_poly_cand semantics).

    Phase 9g: same incomplete-coverage rule as eval_limitless. ALL_YES /
    ALL_NO sums are only valid if EVERY outcome has a price; otherwise
    don't pollute NEAR pool with fake-tight events that look promising
    but aren't real arbs.
    """
    children = ev.get('markets') or []
    pm = []
    total_outcomes = 0
    yes_missing = 0
    no_missing = 0
    if children:
        total_outcomes = len(children)
        for child in children:
            slug = child.get('slug') or child.get('address')
            if not slug or slug not in lim_res:
                yes_missing += 1; no_missing += 1; continue
            yes_ask, _yd, no_ask, _nd = lim_res[slug]
            if yes_ask is None or not (0 < yes_ask < 1):
                yes_missing += 1
                if no_ask is None or not (0 < no_ask < 1): no_missing += 1
                continue
            if no_ask is None or not (0 < no_ask < 1):
                no_missing += 1
            pm.append({
                'yes': yes_ask,
                'no': no_ask if (no_ask and 0 < no_ask < 1) else None,
            })
    else:
        total_outcomes = 1
        slug = ev.get('slug') or ev.get('address')
        if slug and slug in lim_res:
            yes_ask, _yd, no_ask, _nd = lim_res[slug]
            if yes_ask is not None and no_ask is not None and 0 < yes_ask < 1 and 0 < no_ask < 1:
                pm.append({'yes': yes_ask, 'no': no_ask})

    if not pm: return None
    candidates = []
    # ALL_YES — only when we have a price for every outcome
    if children and yes_missing == 0:
        candidates.append(sum(p['yes'] for p in pm))
    elif not children:
        # Standalone binary — yes-only sum doesn't apply
        pass
    # ALL_NO (N >= 3) — only with full NO coverage across original outcomes
    no_raw = [p for p in pm if p['no'] is not None]
    N = len(no_raw)
    if children and N == total_outcomes and N >= 3:
        candidates.append(sum(p['no'] for p in no_raw) / (N - 1))
    # YES_NO_PAIR per market — single-market arb, coverage doesn't matter
    pair_min = None
    for p in pm:
        if p['no'] is None: continue
        s = p['yes'] + p['no']
        pair_min = s if pair_min is None or s < pair_min else pair_min
    if pair_min is not None: candidates.append(pair_min)
    return min(candidates) if candidates else None


# ── Single-candidate re-eval (used by WS callback + classification) ──
def _poly_outcomes_from_cand(cand, clob_res, ws_books):
    """[Legacy YES-only] Reconstruct YES-side outcomes list. Kept for
    backwards-compat (NEAR summary, _sum_poly_cand). New code paths go
    through _poly_per_market which returns both YES and NO."""
    _ev, rough, _is_q = cand
    pm = _poly_per_market(rough, clob_res, ws_books)
    return [{'name': p['name'], 'price': p['yes_price'],
             'liquidity': p['yes_liq'], 'source': p['yes_src'],
             'volume': p['volume']} for p in pm]

def _eval_poly_one(cand, clob_res=None, ws_books=None):
    """Returns list of deals across all 3 arb structures (A/B/C). Empty list if
    none cross threshold. Pure function — no globals touched. Used by both
    eval_poly (batch) and the WS callback (single-token push)."""
    return _eval_poly_structures(cand, clob_res=clob_res, ws_books=ws_books)

# ── Pool classification (HOT / NEAR / COLD) ─────────────────────
def _sum_poly_cand(cand, clob_res, ws_books):
    """Best (smallest) sum across all 3 arb structures, normalized to [0..1]
    so the same NEAR_BUFFER threshold works for A/B/C uniformly. Used for
    NEAR-pool classification — a candidate enters NEAR if ANY structure is
    close to its threshold.

    Normalization:
        A. ALL_YES   → sum(yes) directly                   (target <1.0)
        B. ALL_NO    → sum(no)/(N-1)                       (target <1.0)
        C. YES_NO_PAIR → min over markets of (yes+no)      (target <1.0)
    """
    ev, rough, _is_q = cand
    pm = _poly_per_market(rough, clob_res, ws_books)
    if len(pm) < 2: return None
    # Phase 9g: incomplete-coverage gate — if filter dropped any outcomes,
    # ALL_YES / ALL_NO sums are unsafe (uncovered outcome can win → loss).
    total_outcomes_on_event = len(ev.get('markets') or []) or len(pm)
    full_coverage = (len(pm) == total_outcomes_on_event)
    candidates = []
    # A. ALL_YES — only when we priced every outcome
    if full_coverage:
        s_yes = sum(p['yes_price'] for p in pm if 0 < p['yes_price'] < 1)
        if s_yes > 0: candidates.append(s_yes)
    # B. ALL_NO — same rule, AND need NO price on every outcome
    no_raw = [p for p in pm if p['no_price'] is not None and 0 < p['no_price'] < 1]
    N = len(no_raw)
    if N >= 3 and N == total_outcomes_on_event:
        s_no = sum(p['no_price'] for p in no_raw)
        candidates.append(s_no / (N - 1))
    # C. YES_NO_PAIR — single-market arb, coverage doesn't apply
    pair_min = None
    for p in pm:
        if p['no_price'] is None or not (0 < p['no_price'] < 1): continue
        if not (0 < p['yes_price'] < 1): continue
        s = p['yes_price'] + p['no_price']
        pair_min = s if pair_min is None or s < pair_min else pair_min
    if pair_min is not None: candidates.append(pair_min)
    return min(candidates) if candidates else None

def _sum_kalshi_cand(ev_tickers_pair, kalshi_res):
    """Best (smallest) normalized sum across arb structures A/B/C, for NEAR-pool
    classification. A candidate enters NEAR if ANY structure is close to its
    threshold. Same normalization scheme as _sum_poly_cand.

    Phase 9g: incomplete-coverage gate — same rationale as Polymarket /
    Limitless. ALL_YES / ALL_NO need every outcome priced.
    """
    ev, _tickers = ev_tickers_pair
    total_outcomes = len(ev.get('markets') or [])
    pm = []
    for m in ev.get('markets', []):
        t = m.get('ticker', '')
        if t not in kalshi_res: continue
        yes_ask, _yd, no_ask, _nd = kalshi_res[t]
        if yes_ask is None or yes_ask < 0.05 or yes_ask >= 1: continue
        pm.append({'yes': yes_ask, 'no': no_ask if (no_ask and 0 < no_ask < 1) else None})
    if len(pm) < 2: return None
    full_coverage = (len(pm) == total_outcomes)
    candidates = []
    # A. ALL_YES — only when we priced every outcome
    if full_coverage:
        s_yes = sum(p['yes'] for p in pm)
        if 0.50 <= s_yes: candidates.append(s_yes)
    # B. ALL_NO — same rule, AND every outcome has a NO ask
    no_raw = [p for p in pm if p['no'] is not None]
    N = len(no_raw)
    if N >= 3 and N == total_outcomes:
        candidates.append(sum(p['no'] for p in no_raw) / (N - 1))
    # C. YES_NO_PAIR — single-market arb, coverage doesn't apply
    pair_min = None
    for p in pm:
        if p['no'] is None: continue
        s = p['yes'] + p['no']
        pair_min = s if pair_min is None or s < pair_min else pair_min
    if pair_min is not None: candidates.append(pair_min)
    return min(candidates) if candidates else None

def _sum_sx_market(m, sx_orders):
    mh = m.get('marketHash', '')
    if mh not in sx_orders: return None
    best1, _d1, best2, _d2 = sx_orders[mh]
    if not best1 or not best2 or best1 <= 0 or best2 <= 0: return None
    return best1 + best2

def classify_pools(pc, kc, sx_markets, clob_res, kalshi_res, sx_res,
                   lim_events=None, lim_res=None, ws_books=None):
    """Split candidates into HOT (sum<thresh) and NEAR ([thresh, thresh+buffer)).
    NEAR lists are sorted by `sum` ascending so the closest-to-arb candidates
    win when the WS subscription set is capped at MAX_WS_SUBS."""
    poly_hot, poly_near = [], []
    for cand in pc:
        s = _sum_poly_cand(cand, clob_res, ws_books or {})
        if s is None: continue
        # Phase 9k: per-cand dynamic threshold based on its actual market fee.
        # On 0-fee markets we let through up to 0.992 sum (vs old 0.97);
        # on 3%+ markets we tighten to 0.962. Same break-even math as
        # _eval_poly_structures uses.
        _ev, _rough, _is_q = cand
        cand_max_fee_bps = 0
        for o in _rough:
            cid = o['m'].get('conditionId') or o['m'].get('condition_id')
            if cid:
                info = _fetch_poly_market_info(cid)
                if info:
                    if info['taker_fee_bps'] > cand_max_fee_bps:
                        cand_max_fee_bps = info['taker_fee_bps']
        cand_threshold = compute_poly_threshold(cand_max_fee_bps)
        if s < cand_threshold: poly_hot.append((s, cand))
        elif s < cand_threshold + NEAR_BUFFER: poly_near.append((s, cand))
    poly_hot.sort(key=lambda x: x[0])      # tighter sum first (most profitable)
    poly_near.sort(key=lambda x: x[0])     # closest to arb first
    poly_hot  = [c for _, c in poly_hot]
    poly_near = [c for _, c in poly_near]

    kalshi_hot, kalshi_near = [], []
    for cand in kc:
        s = _sum_kalshi_cand(cand, kalshi_res)
        if s is None: continue
        if s < THRESH_KALSHI: kalshi_hot.append((s, cand))
        elif s < THRESH_KALSHI + NEAR_BUFFER: kalshi_near.append((s, cand))
    kalshi_hot.sort(key=lambda x: x[0])
    kalshi_near.sort(key=lambda x: x[0])
    kalshi_hot  = [c for _, c in kalshi_hot]
    kalshi_near = [c for _, c in kalshi_near]

    # SX: per-market (each binary type is a separate arb opportunity)
    sx_hot_sorted, sx_near_sorted = [], []
    seen_hashes = set()
    for m in sx_markets:
        if m.get('type') not in SX_BINARY_TYPES: continue
        mh = m.get('marketHash', '')
        if not mh or mh in seen_hashes: continue
        seen_hashes.add(mh)
        s = _sum_sx_market(m, sx_res)
        if s is None: continue
        if s < THRESH_SX: sx_hot_sorted.append((s, m))
        elif s < THRESH_SX + NEAR_BUFFER: sx_near_sorted.append((s, m))
    sx_hot_sorted.sort(key=lambda x: x[0])
    sx_near_sorted.sort(key=lambda x: x[0])
    sx_hot  = [m for _, m in sx_hot_sorted]
    sx_near = [m for _, m in sx_near_sorted]

    # Limitless: per-event (negRisk group OR standalone binary). Sort by
    # (sum_asks, -event_volume) — at equal arbitrage tightness, prefer
    # markets with more reported volume, since those are more likely to
    # actually fill at quoted prices. Volume comes from event payload
    # (`volumeFormatted` / `volume` aggregated across child markets).
    def _ev_volume(ev):
        v = ev.get('volume') or ev.get('volumeFormatted') or 0
        try: v = float(v)
        except Exception: v = 0
        for c in (ev.get('markets') or []):
            try: v += float(c.get('volume') or 0)
            except Exception: pass
        return v

    lim_hot_sorted, lim_near_sorted = [], []
    for ev in (lim_events or []):
        s = _sum_limitless_cand(ev, lim_res or {})
        if s is None: continue
        sort_key = (s, -_ev_volume(ev))
        if s < THRESH_LIMITLESS: lim_hot_sorted.append((sort_key, ev))
        elif s < THRESH_LIMITLESS + NEAR_BUFFER: lim_near_sorted.append((sort_key, ev))
    lim_hot_sorted.sort(key=lambda x: x[0])
    lim_near_sorted.sort(key=lambda x: x[0])
    lim_hot  = [ev for _, ev in lim_hot_sorted]
    lim_near = [ev for _, ev in lim_near_sorted]

    return {
        'poly':   {'hot': poly_hot,   'near': poly_near},
        'kalshi': {'hot': kalshi_hot, 'near': kalshi_near},
        'sx':     {'hot': sx_hot,     'near': sx_near},
        'lim':    {'hot': lim_hot,    'near': lim_near},
    }

def collect_poly_tokens(poly_pool):
    """Flatten HOT+NEAR poly candidates into a list of token_ids for WS subs.
    Order: HOT YES first (already an arb), then HOT NO, then NEAR YES, then NEAR NO.
    YES gets priority because structure A (ALL_YES) is the most common arb;
    NO is needed for structure B (ALL_NO) and C (YES_NO_PAIR) — Phase 1."""
    yes_hot, no_hot, yes_near, no_near = [], [], [], []
    for cand in poly_pool['hot']:
        _ev, rough, _ = cand
        for o in rough:
            if o.get('token_id_yes'): yes_hot.append(o['token_id_yes'])
            if o.get('token_id_no'): no_hot.append(o['token_id_no'])
    for cand in poly_pool['near']:
        _ev, rough, _ = cand
        for o in rough:
            if o.get('token_id_yes'): yes_near.append(o['token_id_yes'])
            if o.get('token_id_no'): no_near.append(o['token_id_no'])
    return yes_hot + no_hot + yes_near + no_near

def _best_near_structure(pm, threshold):
    """Pick the arb structure closest to crossing its threshold.
    Returns dict with structure, sum, threshold, distance, outcomes_count, prices, liqs.
    `pm` is a list of per-market dicts with yes_price/yes_liq/no_price/no_liq."""
    options = []
    if not pm: return None
    # A. ALL_YES
    yes_prices = [p['yes_price'] for p in pm if 0 < p['yes_price'] < 1]
    yes_liqs = [p['yes_liq'] for p in pm if 0 < p['yes_price'] < 1]
    if len(yes_prices) >= 2:
        s = sum(yes_prices)
        options.append({'structure':'all_yes','sum':s,'threshold':threshold,
                        'outcomes_count':len(yes_prices),
                        'prices':yes_prices,'liqs':yes_liqs})
    # B. ALL_NO (N>=3)
    no_pm = [p for p in pm if p['no_price'] is not None and 0 < p['no_price'] < 1]
    N = len(no_pm)
    if N >= 3:
        no_prices = [p['no_price'] for p in no_pm]
        s = sum(no_prices)
        options.append({'structure':'all_no','sum':s,'threshold':(N-1)*threshold,
                        'outcomes_count':N,
                        'prices':no_prices,'liqs':[p['no_liq'] for p in no_pm]})
    # C. YES_NO_PAIR (best market)
    pair_best = None
    for p in pm:
        if p['no_price'] is None or not (0 < p['no_price'] < 1): continue
        if not (0 < p['yes_price'] < 1): continue
        s = p['yes_price'] + p['no_price']
        if pair_best is None or s < pair_best['sum']:
            pair_best = {'structure':'yes_no_pair','sum':s,'threshold':threshold,
                         'outcomes_count':2,
                         'prices':[p['yes_price'], p['no_price']],
                         'liqs':[p['yes_liq'], p['no_liq']]}
    if pair_best is not None: options.append(pair_best)
    if not options: return None
    # Pick option with smallest (sum - threshold) — closest to arb (most negative is best)
    options.sort(key=lambda o: o['sum'] - o['threshold'])
    return options[0]

def near_summary(clob_res=None, kalshi_res=None, sx_res=None, lim_res=None, ws_books=None):
    """Build a UI-friendly snapshot of NEAR candidates across all platforms.
    Each entry includes `arb_structure` so the dashboard can render A/B/C/binary
    badges. The structure shown is whichever is closest to its threshold."""
    out = []
    with pools_lock:
        poly_near = list(pools['poly']['near'])
        kalshi_near = list(pools['kalshi']['near'])
        sx_near = list(pools['sx']['near'])
        lim_near = list(pools['lim']['near'])

    for cand in poly_near:
        ev, rough, _ = cand
        pm = _poly_per_market(rough, clob_res or poly_clob_cache, ws_books or {})
        best = _best_near_structure(pm, THRESH_POLY)
        if best is None: continue
        out.append({
            'platform': 'Polymarket',
            'arb_structure': best['structure'],
            'title': ev.get('title', '?'),
            'sum_cents': round(best['sum'] * 100, 1),
            'distance_cents': round((best['sum'] - best['threshold']) * 100, 1),
            'threshold_cents': round(best['threshold'] * 100, 0),
            'outcomes_count': best['outcomes_count'],
            'min_price_cents': round(min(best['prices']) * 100, 1),
            'max_price_cents': round(max(best['prices']) * 100, 1),
            'min_liquidity':   round(min(best['liqs']) if best['liqs'] else 0, 0),
        })

    for cand in kalshi_near:
        ev, _tickers = cand
        pm = []
        for m in ev.get('markets', []):
            t = m.get('ticker', '')
            if not kalshi_res or t not in kalshi_res: continue
            yes_ask, yes_depth, no_ask, no_depth = kalshi_res[t]
            if yes_ask is None or yes_ask < 0.05 or yes_ask >= 1: continue
            pm.append({'name': m.get('title', t),
                       'yes_price': yes_ask, 'yes_liq': yes_depth or 0,
                       'no_price': no_ask if (no_ask and 0 < no_ask < 1) else None,
                       'no_liq': no_depth or 0})
        best = _best_near_structure(pm, THRESH_KALSHI)
        if best is None: continue
        out.append({
            'platform': 'Kalshi',
            'arb_structure': best['structure'],
            'title': ev.get('title', '?'),
            'sum_cents': round(best['sum'] * 100, 1),
            'distance_cents': round((best['sum'] - best['threshold']) * 100, 1),
            'threshold_cents': round(best['threshold'] * 100, 0),
            'outcomes_count': best['outcomes_count'],
            'min_price_cents': round(min(best['prices']) * 100, 1),
            'max_price_cents': round(max(best['prices']) * 100, 1),
            'min_liquidity':   round(min(best['liqs']) if best['liqs'] else 0, 0),
        })

    for m in sx_near:
        if not sx_res: continue
        mh = m.get('marketHash', '')
        if mh not in sx_res: continue
        best1, depth1, best2, depth2 = sx_res[mh]
        if not best1 or not best2: continue
        s = best1 + best2
        out.append({
            'platform': 'SX Bet',
            'arb_structure': 'binary',
            'title': _sx_market_title(m),
            'sum_cents': round(s * 100, 1),
            'distance_cents': round((s - THRESH_SX) * 100, 1),
            'threshold_cents': round(THRESH_SX * 100, 0),
            'outcomes_count': 2,
            'min_price_cents': round(min(best1, best2) * 100, 1),
            'max_price_cents': round(max(best1, best2) * 100, 1),
            'min_liquidity':   round(min(depth1 or 0, depth2 or 0), 0),
        })

    # Limitless: per-event aggregate (single market or negRisk group)
    for ev in lim_near:
        if not lim_res: continue
        children = ev.get('markets') or []
        pm = []
        if children:
            for child in children:
                slug = child.get('slug') or child.get('address')
                if not slug or slug not in lim_res: continue
                yes_ask, yes_depth, no_ask, no_depth = lim_res[slug]
                if yes_ask is None or not (0 < yes_ask < 1): continue
                pm.append({'name': child.get('title', '?'),
                           'yes_price': yes_ask, 'yes_liq': yes_depth or 0,
                           'no_price': no_ask if (no_ask and 0 < no_ask < 1) else None,
                           'no_liq': no_depth or 0})
        else:
            slug = ev.get('slug') or ev.get('address')
            if slug and slug in lim_res:
                yes_ask, yes_depth, no_ask, no_depth = lim_res[slug]
                if yes_ask is not None and 0 < yes_ask < 1:
                    pm.append({'name': ev.get('title', '?'),
                               'yes_price': yes_ask, 'yes_liq': yes_depth or 0,
                               'no_price': no_ask if (no_ask and 0 < no_ask < 1) else None,
                               'no_liq': no_depth or 0})
        best = _best_near_structure(pm, THRESH_LIMITLESS)
        if best is None: continue
        out.append({
            'platform': 'Limitless',
            'arb_structure': best['structure'],
            'title': ev.get('title', '?'),
            'sum_cents': round(best['sum'] * 100, 1),
            'distance_cents': round((best['sum'] - best['threshold']) * 100, 1),
            'threshold_cents': round(best['threshold'] * 100, 0),
            'outcomes_count': best['outcomes_count'],
            'min_price_cents': round(min(best['prices']) * 100, 1),
            'max_price_cents': round(max(best['prices']) * 100, 1),
            'min_liquidity':   round(min(best['liqs']) if best['liqs'] else 0, 0),
        })

    out.sort(key=lambda x: x['distance_cents'])
    return out

def rebuild_poly_token_index(poly_pool):
    """token_id -> candidate, for WS callback reverse lookup.
    Maps both YES and NO tokens so a price update on either side triggers
    re-evaluation of all 3 arb structures (A/B/C)."""
    idx = {}
    for cand in poly_pool['hot'] + poly_pool['near']:
        _ev, rough, _ = cand
        for o in rough:
            yes_tid = o.get('token_id_yes') or o.get('token_id')
            no_tid = o.get('token_id_no')
            if yes_tid: idx[yes_tid] = cand
            if no_tid: idx[no_tid] = cand
    return idx


def rebuild_lim_slug_index(lim_pool):
    """slug -> parent event, for WS callback reverse lookup. Maps:
      - For negRisk groups: each child slug → parent event dict
      - For standalone binary: event slug → same event dict
    So a single WS push on any slug surfaces the right event for re-eval.
    """
    idx = {}
    for ev in (lim_pool.get('hot', []) + lim_pool.get('near', [])):
        children = ev.get('markets') or []
        if children:
            for c in children:
                s = c.get('slug') or c.get('address')
                if s: idx[s] = ev
        else:
            s = ev.get('slug') or ev.get('address')
            if s: idx[s] = ev
    return idx

# ── WS push callback ────────────────────────────────────────────
def on_ws_update(token_id):
    """Polymarket WS pushed an orderbook update for `token_id`. Re-evaluate
    that candidate (across all 3 arb structures) and inject/replace deals."""
    if ws_client is None: return
    with poly_token_index_lock:
        cand = poly_token_index.get(token_id)
    if cand is None: return
    with poly_clob_cache_lock:
        clob_snapshot = dict(poly_clob_cache)
    ws_books = {}
    # Pull books for BOTH YES and NO tokens of this candidate
    _ev, rough, _ = cand
    for o in rough:
        for tid in (o.get('token_id_yes') or o.get('token_id'), o.get('token_id_no')):
            if not tid: continue
            b = ws_client.get_book(tid)
            if b: ws_books[tid] = b
    new_deals = _eval_poly_one(cand, clob_res=clob_snapshot, ws_books=ws_books)
    base_title = cand[0].get('title', '?')
    with scan_lock:
        deals = list(scan_data.get('deals', []))
        # Drop any existing deals for this event (any structure) — match by
        # title prefix since structure suffixes may differ.
        deals = [d for d in deals
                 if not (d['title'] == base_title
                         or d['title'].startswith(base_title + ' (')
                         or d['title'].startswith(base_title + ' — '))]
        for d in new_deals:
            if not d.get('is_quarantine'):
                deals.append(d)
        deals.sort(key=lambda d: d['net'], reverse=True)
        scan_data['deals'] = deals
        if 'stats' in scan_data and isinstance(scan_data['stats'], dict):
            scan_data['stats']['arb_found'] = len(deals)
    # WS path produced new deals — auto-dry-fire any newcomers (Phase 2).
    _maybe_dry_fire(new_deals)

# ── Filter Candidates ──────────────────────────────
def filter_poly(events, diag=None):
    """Returns (candidates, token_ids). If `diag` dict is passed, fills in
    per-step skip counters under keys 'poly_in' and 'poly_skip_*' / 'poly_pass'.
    Counters help understand WHY the radar shows 0 deals on quiet markets."""
    if diag is None: diag = {}
    diag['poly_in'] = len(events)
    for k in ('poly_skip_blacklist','poly_skip_no_window','poly_skip_lt2_markets',
              'poly_skip_no_negrisk','poly_skip_lt2_rough','poly_skip_sum_high',
              'poly_skip_deadline_text','poly_pass'):
        diag.setdefault(k, 0)

    candidates = []; token_ids = []
    for ev in events:
        title = ev.get('title', '?')
        if title in blacklist:
            diag['poly_skip_blacklist'] += 1; continue

        # 10-day filter
        end_date = ev.get('endDateIso') or ev.get('endDate')
        if not is_within_10_days(date_str=end_date):
            diag['poly_skip_no_window'] += 1; continue

        markets = ev.get('markets', [])
        if len(markets) < 2:
            diag['poly_skip_lt2_markets'] += 1; continue

        # Phase 9h: per-market closed/archived/restricted gate. Polymarket
        # exposes `closed`, `archived`, `restricted`, `enableOrderBook`,
        # `acceptingOrders` per market. If ANY child fails these checks,
        # ALL_YES + ALL_NO are unsafe (closed outcome can still resolve as
        # winner but we can't buy YES on it). Reject whole event.
        if (ev.get('closed') is True or ev.get('archived') is True
                or ev.get('restricted') is True):
            diag.setdefault('poly_skip_outcome_closed', 0)
            diag['poly_skip_outcome_closed'] += 1; continue
        any_child_closed = any(
            (m.get('closed') is True or m.get('archived') is True
             or m.get('restricted') is True
             or m.get('enableOrderBook') is False
             or m.get('acceptingOrders') is False)
            for m in markets
        )
        if any_child_closed:
            diag.setdefault('poly_skip_outcome_closed', 0)
            diag['poly_skip_outcome_closed'] += 1; continue

        # Polymarket exposes negRisk on the EVENT (canonical location); the
        # field on each market is almost always False even when the event is
        # mutually-exclusive. Earlier code only looked at market.negRisk and
        # rejected ~100% of valid candidates. Accept either signal.
        if not (ev.get('negRisk') is True or
                (markets and all(m.get('negRisk') is True for m in markets))):
            diag['poly_skip_no_negrisk'] += 1; continue
        # Quarantine: detect events with hidden "Other" outcome. If Other wins
        # and we hold YES on A,B,C only, every leg loses. Such deals stay in
        # scan_data['quarantine'] for analysis but the executor refuses them.
        # (Earlier this branch had `is_quarantine = False` hard-coded — bug,
        # fixed 28.04.2026 so the quarantine pipeline actually works.)
        market_names = [m.get('question') or m.get('groupItemTitle') or '' for m in markets]
        is_quarantine = has_other_outcome(market_names)
        rough = []
        for m in markets:
            ps = m.get('outcomePrices')
            if not ps: continue
            try: p = float(json.loads(ps)[0])
            except: continue
            if p <= 0 or p >= 1: continue
            rough.append({'m': m, 'implied': p})
        if len(rough) < 2:
            diag['poly_skip_lt2_rough'] += 1; continue
        if sum(o['implied'] for o in rough) >= 0.99:
            diag['poly_skip_sum_high'] += 1; continue
        names = [o['m'].get('question', o['m'].get('groupItemTitle','?')) for o in rough]
        if is_deadline(names):
            diag['poly_skip_deadline_text'] += 1; continue
        for o in rough:
            tids_str = o['m'].get('clobTokenIds')
            if tids_str:
                try:
                    tids = json.loads(tids_str)
                    if tids:
                        # tids[0] = YES side, tids[1] = NO side (Polymarket convention).
                        # Keep both. `token_id` stays as YES for backwards compat with
                        # WS reverse-index and existing callers; `token_id_no` enables
                        # ALL_NO and YES_NO_PAIR arb structures (Phase 1).
                        o['token_id'] = tids[0]
                        o['token_id_yes'] = tids[0]
                        token_ids.append(tids[0])
                        if len(tids) > 1 and tids[1]:
                            o['token_id_no'] = tids[1]
                            token_ids.append(tids[1])
                        else:
                            o['token_id_no'] = None
                except: pass
        candidates.append((ev, rough, is_quarantine))
        diag['poly_pass'] += 1
    return candidates, token_ids

def filter_kalshi(events, diag=None):
    """Returns (candidates, tickers). If `diag` is passed, fills counters
    under 'kalshi_in' / 'kalshi_skip_*' / 'kalshi_pass'."""
    if diag is None: diag = {}
    diag['kalshi_in'] = len(events)
    for k in ('kalshi_skip_lt2_markets','kalshi_skip_no_window',
              'kalshi_skip_deadline_text','kalshi_skip_no_tickers','kalshi_pass'):
        diag.setdefault(k, 0)

    candidates = []; tickers = []
    for ev in events:
        markets = ev.get('markets', [])
        if len(markets) < 2:
            diag['kalshi_skip_lt2_markets'] += 1; continue

        # 10-day filter
        close_time = markets[0].get('close_time') or markets[0].get('expected_expiration_time')
        if not is_within_10_days(date_str=close_time):
            diag['kalshi_skip_no_window'] += 1; continue

        names = [m.get('title', m.get('ticker','?')) for m in markets]
        if is_deadline(names):
            diag['kalshi_skip_deadline_text'] += 1; continue
        ev_tickers = []
        for m in markets:
            t = m.get('ticker')
            if t: ev_tickers.append(t); tickers.append(t)
        if len(ev_tickers) >= 2:
            candidates.append((ev, ev_tickers))
            diag['kalshi_pass'] += 1
        else:
            diag['kalshi_skip_no_tickers'] += 1
    return candidates, tickers

# ═══════════════════════════════════════════════════════════════
# MAIN SCAN — 300 Poly + 200 Kalshi + 200 SX Bet = 700 events
# ═══════════════════════════════════════════════════════════════
def run_scan():
    with scan_lock:
        scan_data['scanning'] = True; scan_data['error'] = None
    stats = {'poly_events':0, 'kalshi_events':0, 'sx_markets':0,
             'poly_neg_risk':0, 'clob_fetched':0, 'kalshi_ob_fetched':0,
             'arb_found':0, 'scan_type': 'MAIN'}
    t0 = time.time()
    try:
        print(f"\n{'='*50}")
        print(f"[MAIN] Start {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC")

        # Phase 1: Fetch events from enabled platforms.
        # Polymarket: paginate POLY_MAIN_PAGES × 500 events (default 4 = 2000)
        t_poly = time.time()
        poly_events = []
        offsets = [i * 500 for i in range(POLY_MAIN_PAGES)]
        for offset in offsets:
            try:
                r = requests.get(f"https://gamma-api.polymarket.com/events?closed=false&limit=500&active=true&offset={offset}", timeout=15)
                page = r.json()
                if not page: break  # no more events at this offset
                poly_events.extend(page)
            except Exception as e: print(f"[POLY] {e}")
        t_poly = time.time() - t_poly

        # Kalshi — skipped entirely if ENABLE_KALSHI=0
        t_kalshi = time.time()
        kalshi_events = []
        if ENABLE_KALSHI:
            try:
                r = requests.get("https://api.elections.kalshi.com/trade-api/v2/events?status=open&limit=200&with_nested_markets=true", timeout=15, headers=HEADERS)
                data = r.json()
                kalshi_events.extend(data.get('events', []))
                cursor = data.get('cursor')
                for _ in range(4):
                    if not cursor: break
                    r = requests.get(f"https://api.elections.kalshi.com/trade-api/v2/events?status=open&limit=200&with_nested_markets=true&cursor={cursor}", timeout=15, headers=HEADERS)
                    data = r.json()
                    kalshi_events.extend(data.get('events', []))
                    cursor = data.get('cursor')
            except Exception as e: print(f"[KALSHI] {e}")
        t_kalshi = time.time() - t_kalshi

        # SX Bet — skipped entirely if ENABLE_SX=0
        t_sx = time.time()
        sx_markets = []
        sx_fetch_error = None
        sx_http_status = None
        if ENABLE_SX:
            try:
                r = requests.get(f"https://api.sx.bet/markets/active?onlyMainLine=true&pageSize={SX_PAGE_SIZE}", timeout=15)
                sx_http_status = r.status_code
                data = r.json()
                if data.get('status') == 'success':
                    sx_markets.extend(data.get('data', {}).get('markets', []))
                    next_key = data.get('data', {}).get('nextKey')
                    for _ in range(SX_MAX_PAGES_MAIN - 1):
                        if not next_key: break
                        r = requests.get(f"https://api.sx.bet/markets/active?onlyMainLine=true&pageSize={SX_PAGE_SIZE}&paginationKey={next_key}", timeout=15)
                        data = r.json()
                        if data.get('status') == 'success':
                            sx_markets.extend(data.get('data', {}).get('markets', []))
                            next_key = data.get('data', {}).get('nextKey')
                else:
                    # Surface non-success status for diagnostics
                    sx_fetch_error = f"status={data.get('status')} msg={str(data)[:120]}"
            except Exception as e:
                sx_fetch_error = f"{type(e).__name__}: {e}"
                print(f"[SX] {e}")
        t_sx = time.time() - t_sx

        # Limitless Exchange — skipped if ENABLE_LIMITLESS=0.
        # Paginated fetch with a small inter-page delay to stay polite under
        # any undocumented rate limits. 10 pages × 100 = up to 1000 markets.
        t_lim = time.time()
        lim_events = []
        if ENABLE_LIMITLESS:
            try:
                for page in range(1, LIMITLESS_MAIN_PAGES + 1):
                    r = requests.get(
                        f"{LIMITLESS_API_BASE}/markets/active?page={page}&limit={LIMITLESS_PAGE_SIZE}",
                        timeout=15,
                    )
                    if r.status_code != 200: break
                    data = r.json()
                    items = data if isinstance(data, list) else data.get('data') or data.get('markets') or []
                    if not items: break
                    lim_events.extend(items)
                    if len(items) < LIMITLESS_PAGE_SIZE: break  # last page
                    if LIMITLESS_PAGE_DELAY_S > 0 and page < LIMITLESS_MAIN_PAGES:
                        time.sleep(LIMITLESS_PAGE_DELAY_S)
            except Exception as e:
                print(f"[LIMITLESS] {e}")
        t_lim = time.time() - t_lim

        stats['poly_events'] = len(poly_events)
        stats['kalshi_events'] = len(kalshi_events)
        stats['sx_markets'] = len(sx_markets)
        stats['lim_events'] = len(lim_events)
        stats['sx_http_status'] = sx_http_status
        stats['sx_fetch_error'] = sx_fetch_error
        print(f"[FETCH] Poly={len(poly_events)} ({t_poly:.1f}s) Kalshi={len(kalshi_events)} ({t_kalshi:.1f}s) SX={len(sx_markets)} ({t_sx:.1f}s) Lim={len(lim_events)} ({t_lim:.1f}s) sx_http={sx_http_status}")

        # Phase 2: Filter (with diagnostic counters)
        pc, poly_tids = filter_poly(poly_events, diag=stats)
        kc, kalshi_tks = filter_kalshi(kalshi_events, diag=stats)
        sx_ml_hashes = [m['marketHash'] for m in sx_markets if m.get('type') in SX_BINARY_TYPES]
        stats['sx_binary_count'] = len(sx_ml_hashes)
        stats['sx_moneyline_count'] = sum(1 for m in sx_markets if m.get('type') == 226)  # subset, kept for back-compat
        stats['poly_neg_risk'] = len(pc)

        # Limitless: collect all child slugs (negRisk groups) + standalone
        # market slugs for batch orderbook fetch
        lim_slugs = []
        for ev in lim_events:
            children = ev.get('markets') or []
            if children:
                for c in children:
                    s = c.get('slug') or c.get('address')
                    if s: lim_slugs.append(s)
            else:
                s = ev.get('slug') or ev.get('address')
                if s: lim_slugs.append(s)
        stats['lim_slugs'] = len(lim_slugs)

        # Phase 3: Batch fetch orderbooks
        clob_res = batch_fetch(_fetch_clob, poly_tids)
        kalshi_res = batch_fetch(_fetch_kalshi_ob, kalshi_tks)
        sx_res = batch_fetch(_fetch_sx_orders, sx_ml_hashes)
        lim_res = batch_fetch(_fetch_limitless_orderbook, lim_slugs) if ENABLE_LIMITLESS else {}

        stats['clob_fetched'] = sum(1 for v in clob_res.values() if v[0] is not None)
        stats['kalshi_ob_fetched'] = sum(1 for v in kalshi_res.values() if v[0] is not None)
        stats['lim_ob_fetched'] = sum(1 for v in lim_res.values() if v[0] is not None)

        # Phase 4: Evaluate. Disabled platforms are skipped — kc/sx_markets
        # will be empty anyway because we didn't fetch them, but explicit
        # guards make the intent clear and let us short-circuit eval.
        all_deals = eval_poly(pc, clob_res)
        if ENABLE_KALSHI:
            all_deals += eval_kalshi(kc, kalshi_res)
        if ENABLE_SX:
            all_deals += eval_sx(sx_markets, sx_res)
        if ENABLE_LIMITLESS:
            all_deals += eval_limitless(lim_events, lim_res)
        
        deals = [d for d in all_deals if not d.get('is_quarantine')]
        deals.sort(key=lambda d: d['net'], reverse=True)
        
        quarantine = [d for d in all_deals if d.get('is_quarantine')]
        quarantine.sort(key=lambda d: d['net'], reverse=True)
        
        stats['arb_found'] = len(deals)
        stats['quarantine_count'] = len(quarantine)

        # Save candidates for micro-scan (legacy path, kept for safety)
        with cand_lock:
            candidates_global['poly'] = pc
            candidates_global['kalshi'] = kc
            candidates_global['sx'] = sx_markets

        # ── Classify into HOT/NEAR pools, update WS subscription set ──
        ws_books = {tid: ws_client.get_book(tid) for tid in (clob_res.keys() if ws_client else [])}
        ws_books = {k: v for k, v in ws_books.items() if v}
        new_pools = classify_pools(pc, kc, sx_markets, clob_res, kalshi_res, sx_res,
                                    lim_events=lim_events, lim_res=lim_res, ws_books=ws_books)
        with pools_lock:
            pools.update(new_pools)
        # Cache REST clob snapshot for WS-driven re-eval fallback + NEAR snapshot
        with poly_clob_cache_lock:
            poly_clob_cache.clear(); poly_clob_cache.update(clob_res)
        with res_cache_lock:
            kalshi_res_cache.clear(); kalshi_res_cache.update(kalshi_res)
            sx_res_cache.clear(); sx_res_cache.update(sx_res)
            lim_res_cache.clear(); lim_res_cache.update(lim_res)
        # Push token list to WS — capped at MAX_WS_SUBS, HOT first
        if ws_client is not None:
            poly_pool = new_pools['poly']
            tokens = collect_poly_tokens({'hot': poly_pool['hot'], 'near': poly_pool['near']})
            ws_client.update_subscriptions(tokens[:MAX_WS_SUBS])
            new_idx = rebuild_poly_token_index(poly_pool)
            with poly_token_index_lock:
                poly_token_index.clear(); poly_token_index.update(new_idx)
        # Push Limitless slug list to its WS — same idea, separate budget.
        # We collect every child slug (per-outcome) plus the event-level slug
        # for standalone binaries; both are pushed to subscribe_market_prices.
        # Phase 9d: also rebuild the reverse slug→event index so on_lim_ws_update
        # callbacks can locate the parent event in O(1) for push-driven re-eval.
        lim_pool = new_pools.get('lim') or {'hot': [], 'near': []}
        new_lim_idx = rebuild_lim_slug_index(lim_pool)
        with lim_slug_index_lock:
            lim_slug_index.clear(); lim_slug_index.update(new_lim_idx)

        if lim_ws_client is not None:
            lim_slugs_set = list(new_lim_idx.keys())
            lim_ws_client.update_subscriptions(lim_slugs_set[:LIMITLESS_MAX_WS_SUBS])
        # Phase 9f: push HOT+NEAR Polymarket condition_ids to every per-wallet
        # user-channel WS so they can latch on `trade` events for our orders.
        if poly_user_ws_clients:
            poly_pool_now = new_pools['poly']
            condition_ids = []
            for cand in poly_pool_now['hot'] + poly_pool_now['near']:
                ev_obj = cand[0] if isinstance(cand, tuple) else cand.get('ev')
                if not isinstance(ev_obj, dict):
                    continue
                # Polymarket exposes either `condition_id` on each market
                # or a top-level event `id`. Collect from markets[].
                for m in (ev_obj.get('markets') or []):
                    cid = m.get('conditionId') or m.get('condition_id')
                    if cid: condition_ids.append(cid)
            for client in poly_user_ws_clients:
                client.update_markets(condition_ids[:MAX_WS_SUBS])
        stats['pool_poly_hot']    = len(new_pools['poly']['hot'])
        stats['pool_poly_near']   = len(new_pools['poly']['near'])
        stats['pool_kalshi_hot']  = len(new_pools['kalshi']['hot'])
        stats['pool_kalshi_near'] = len(new_pools['kalshi']['near'])
        stats['pool_sx_hot']      = len(new_pools['sx']['hot'])
        stats['pool_sx_near']     = len(new_pools['sx']['near'])
        stats['pool_lim_hot']     = len(new_pools['lim']['hot'])
        stats['pool_lim_near']    = len(new_pools['lim']['near'])

        elapsed = time.time() - t0
        print(f"[MAIN] Done in {elapsed:.1f}s — {stats['arb_found']} arb found, {stats['quarantine_count']} in quarantine "
              f"| pools: poly H{stats['pool_poly_hot']}/N{stats['pool_poly_near']} "
              f"kalshi H{stats['pool_kalshi_hot']}/N{stats['pool_kalshi_near']} "
              f"sx H{stats['pool_sx_hot']}/N{stats['pool_sx_near']} "
              f"lim H{stats['pool_lim_hot']}/N{stats['pool_lim_near']}")
        if deals: save_history(deals)

    except Exception as e:
        print(f"[MAIN] Error: {e}")
        import traceback; traceback.print_exc()
        with scan_lock:
            scan_data['error'] = str(e)
            scan_data['scanning'] = False
            return

    with scan_lock:
        scan_data['deals'] = deals
        scan_data['quarantine'] = quarantine
        scan_data['stats'] = stats
        scan_data['last_scan'] = datetime.now(timezone.utc).isoformat()
        scan_data['scanning'] = False
        # First fresh scan after a restore — clear the "stale" flags
        # so the UI knows the snapshot is now live.
        scan_data.pop('restored_from_disk', None)
        scan_data.pop('restored_age_s', None)
    # Persist after every completed MAIN scan so a container restart
    # serves the last-known good snapshot to the UI immediately.
    _persist_scan_state()
    # Auto-dry-fire new arbs from this main scan (Phase 2). Idempotent —
    # tracks already-fired keys, so the same deal isn't logged every 90s.
    _maybe_dry_fire(deals)

# ═══════════════════════════════════════════════════════════════
# PAUSE SCAN — Extra pages 
# ═══════════════════════════════════════════════════════════════
def run_pause_scan():
    """Fetch additional Poly/Kalshi/SX pages during pause."""
    t0 = time.time()
    extra_deals = []

    # Extra Polymarket pages
    for offset in [300, 800, 1300]:
        try:
            r = requests.get(f"https://gamma-api.polymarket.com/events?closed=false&limit=500&active=true&offset={offset}", timeout=10)
            data = r.json()
            if not data: break
            pc, tids = filter_poly(data)
            if pc:
                clob = batch_fetch(_fetch_clob, tids)
                extra_deals.extend(eval_poly(pc, clob))
            if len(data) < 500: break
        except Exception as e: break

    # Extra SX Bet pages
    try:
        r = requests.get(f"https://api.sx.bet/markets/active?onlyMainLine=true&pageSize={SX_PAGE_SIZE}", timeout=10)
        data = r.json()
        next_key = data.get('data', {}).get('nextKey') if data.get('status') == 'success' else None
        pages = 0
        while next_key and pages < (SX_MAX_PAGES_PAUSE - 1):
            r = requests.get(f"https://api.sx.bet/markets/active?onlyMainLine=true&pageSize={SX_PAGE_SIZE}&paginationKey={next_key}", timeout=10)
            data = r.json()
            if data.get('status') != 'success': break
            batch = data.get('data', {}).get('markets', [])
            ml_hashes = [m['marketHash'] for m in batch if m.get('type') in SX_BINARY_TYPES]
            if ml_hashes:
                sx_res = batch_fetch(_fetch_sx_orders, ml_hashes)
                extra_deals.extend(eval_sx(batch, sx_res))
            next_key = data.get('data', {}).get('nextKey')
            pages += 1
    except: pass

    # Merge
    if extra_deals:
        extra_deals.sort(key=lambda d: d['net'], reverse=True)
        with scan_lock:
            existing = scan_data.get('deals', [])
            existing_titles = {d['title'] for d in existing}
            for d in extra_deals:
                if d['title'] not in existing_titles:
                    existing.append(d)
            existing.sort(key=lambda d: d['net'], reverse=True)
            scan_data['deals'] = existing
            scan_data['stats']['arb_found'] = len(existing)
        save_history(extra_deals, micro=True)

# ── Micro Scanners (per-platform, pool-scoped) ──────────────────
def _merge_platform_deals(new_deals, platform):
    """Replace this platform's deals/quarantine in scan_data with the new list,
    keeping deals from other platforms intact."""
    new_deals_clean = [d for d in new_deals if not d.get('is_quarantine')]
    new_quar       = [d for d in new_deals if d.get('is_quarantine')]
    with scan_lock:
        deals = [d for d in scan_data.get('deals', []) if d.get('platform') != platform]
        deals.extend(new_deals_clean)
        deals.sort(key=lambda d: d['net'], reverse=True)
        quar = [d for d in scan_data.get('quarantine', []) if d.get('platform') != platform]
        quar.extend(new_quar)
        quar.sort(key=lambda d: d['net'], reverse=True)
        scan_data['deals'] = deals
        scan_data['quarantine'] = quar
        if isinstance(scan_data.get('stats'), dict):
            scan_data['stats']['arb_found'] = len(deals)
            scan_data['stats']['quarantine_count'] = len(quar)

def kalshi_micro_loop():
    """Refresh Kalshi HOT+NEAR pool every KALSHI_MICRO_INTERVAL seconds."""
    time.sleep(15)
    while True:
        try:
            with scan_lock:
                if scan_data['scanning']:
                    time.sleep(KALSHI_MICRO_INTERVAL); continue
            with pools_lock:
                pool = list(pools['kalshi']['hot']) + list(pools['kalshi']['near'])
            if pool:
                tks = [t for _, tickers in pool for t in tickers]
                k_res = batch_fetch(_fetch_kalshi_ob, tks)
                _merge_platform_deals(eval_kalshi(pool, k_res), 'Kalshi')
        except Exception as e:
            print(f"[KALSHI MICRO] Error: {e}")
        time.sleep(KALSHI_MICRO_INTERVAL)

def sx_micro_loop():
    """Refresh SX Bet HOT+NEAR pool every SX_MICRO_INTERVAL seconds (live sport)."""
    time.sleep(15)
    while True:
        try:
            with scan_lock:
                if scan_data['scanning']:
                    time.sleep(SX_MICRO_INTERVAL); continue
            with pools_lock:
                pool = list(pools['sx']['hot']) + list(pools['sx']['near'])
            if pool:
                ml_hashes = [m['marketHash'] for m in pool if m.get('type') in SX_BINARY_TYPES]
                sx_res = batch_fetch(_fetch_sx_orders, ml_hashes)
                _merge_platform_deals(eval_sx(pool, sx_res), 'SX Bet')
        except Exception as e:
            print(f"[SX MICRO] Error: {e}")
        time.sleep(SX_MICRO_INTERVAL)


def limitless_micro_loop():
    """Refresh Limitless HOT+NEAR pool every LIMITLESS_MICRO_INTERVAL seconds.
    Same pattern as kalshi_micro_loop / sx_micro_loop — re-fetches orderbooks
    of the in-pool slugs and re-evaluates, so a price flick into arb territory
    surfaces in <5s without waiting for the 90s main scan. WebSocket would
    cut this to <100ms but is left as Phase 2 of the Limitless integration."""
    time.sleep(20)
    while True:
        try:
            with scan_lock:
                if scan_data['scanning']:
                    time.sleep(LIMITLESS_MICRO_INTERVAL); continue
            with pools_lock:
                pool = list(pools['lim']['hot']) + list(pools['lim']['near'])
            if pool:
                slugs = []
                for ev in pool:
                    children = ev.get('markets') or []
                    if children:
                        for c in children:
                            s = c.get('slug') or c.get('address')
                            if s: slugs.append(s)
                    else:
                        s = ev.get('slug') or ev.get('address')
                        if s: slugs.append(s)
                lim_res = batch_fetch(_fetch_limitless_orderbook, slugs)
                _merge_platform_deals(eval_limitless(pool, lim_res), 'Limitless')
        except Exception as e:
            print(f"[LIM MICRO] Error: {e}")
        time.sleep(LIMITLESS_MICRO_INTERVAL)

def analytics_loop():
    """Periodically snapshot scan_data['deals'] into analytics so we get
    open/close lifecycle events without instrumenting every write site."""
    time.sleep(10)
    while True:
        try:
            with scan_lock:
                deals_snapshot = list(scan_data.get('deals') or [])
            analytics.update_from_scan(deals_snapshot)
        except Exception as e:
            print(f"[ANALYTICS] Error: {e}")
        time.sleep(10)

def poly_micro_fallback_loop():
    """Fallback REST poll for Polymarket HOT+NEAR pool — runs ONLY when WS is
    disconnected (no msgs in last 30s). Keeps Polymarket fresh during outages."""
    time.sleep(20)
    while True:
        try:
            ws_dead = True
            if ws_client is not None:
                m = ws_client.get_metrics()
                age = m.get('last_msg_age_sec')
                if age is not None and age < 30:
                    ws_dead = False
            if ws_dead:
                with pools_lock:
                    pool = list(pools['poly']['hot']) + list(pools['poly']['near'])
                if pool:
                    tids = [o.get('token_id') for _, rough, _ in pool for o in rough if o.get('token_id')]
                    clob = batch_fetch(_fetch_clob, tids)
                    _merge_platform_deals(eval_poly(pool, clob), 'Polymarket')
        except Exception as e:
            print(f"[POLY FALLBACK] Error: {e}")
        time.sleep(MICRO_INTERVAL)

def save_history(deals, micro=False):
    try:
        hdir = os.path.join(os.path.dirname(__file__), '..', 'Executions')
        os.makedirs(hdir, exist_ok=True)
        with open(os.path.join(hdir, 'price_history.jsonl'), 'a', encoding='utf-8') as f:
            f.write(json.dumps({
                "time": datetime.now(timezone.utc).isoformat(), "micro": micro,
                "deals": [{"title":d["title"],"platform":d["platform"],"sum":d["total_cents"],"net":d["net"]} for d in deals[:10]]
            }) + "\n")
    except: pass


# ── scan_data warm-cache ───────────────────────────────────────────
# The MAIN scan can take 30-60s on a cold container start. Until it
# finishes, scan_data is empty and the UI shows a "Запуск сканирования…"
# spinner that visually looks like a hang. Persist the last-completed
# scan_data to disk so a restarted container immediately serves the
# previous snapshot — the loop then overwrites it on the first fresh
# pass. Stale (>24h) state is dropped.
SCAN_STATE_PATH = os.path.join(os.path.dirname(__file__), '..',
                               'Executions', 'scan_state.json')
SCAN_STATE_MAX_AGE_S = 24 * 3600


def _persist_scan_state():
    """Atomically write the current scan_data snapshot to disk.
    Best-effort — failures are logged but never raise."""
    try:
        os.makedirs(os.path.dirname(SCAN_STATE_PATH), exist_ok=True)
        with scan_lock:
            payload = dict(scan_data)
        # Strip volatile / runtime-only fields. WS metrics are reattached
        # live by /api/deals on every request.
        for k in ('scanning', 'error', 'ws', 'ws_limitless', 'near_count'):
            payload.pop(k, None)
        tmp = SCAN_STATE_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, default=str)
        os.replace(tmp, SCAN_STATE_PATH)
    except Exception as e:
        print(f"[persist] {e}")


def _restore_scan_state():
    """On startup, repopulate scan_data from the persisted snapshot if it
    exists and is recent enough — so /api/deals does not return an empty
    payload while the first run_scan() is still in flight."""
    try:
        if not os.path.exists(SCAN_STATE_PATH):
            return
        age = time.time() - os.path.getmtime(SCAN_STATE_PATH)
        if age > SCAN_STATE_MAX_AGE_S:
            print(f"[restore] scan_state {age/3600:.1f}h old — skipping")
            return
        with open(SCAN_STATE_PATH, 'r', encoding='utf-8') as f:
            payload = json.load(f)
        # Mark as restored so the UI/operators know this is not live.
        # First successful run_scan() overwrites it (re-persists without
        # this flag).
        payload['restored_from_disk'] = True
        payload['restored_age_s'] = round(age, 1)
        with scan_lock:
            scan_data.update(payload)
        n_deals = len(payload.get('deals') or [])
        print(f"[restore] loaded {n_deals} deals from cache "
              f"(age {age:.0f}s) — UI will show last snapshot until "
              f"first scan completes")
    except Exception as e:
        print(f"[restore] {e}")


def scan_loop():
    time.sleep(2)
    while True:
        run_scan()
        threading.Thread(target=run_pause_scan, daemon=True).start()
        time.sleep(SCAN_INTERVAL)

# ── Routes ──────────────────────────────────────────────────────
@app.route('/')
def index():
    return send_file(os.path.join(os.path.dirname(__file__), 'dashboard.html'))

@app.route('/api/deals')
def api_deals():
    with scan_lock:
        payload = dict(scan_data)
    # Inject fresh WS metrics on each request (cheap, no extra thread)
    if ws_client is not None:
        payload['ws'] = ws_client.get_metrics()
    if lim_ws_client is not None:
        payload['ws_limitless'] = lim_ws_client.get_metrics()
    # Inject NEAR pool size so the nav badge can light up even from other tabs
    with pools_lock:
        payload['near_count'] = (len(pools['poly']['near'])
                                 + len(pools['kalshi']['near'])
                                 + len(pools['sx']['near']))
    return jsonify(payload)

from flask import request

@app.route('/api/scan', methods=['POST'])
def api_scan():
    threading.Thread(target=run_scan, daemon=True).start()
    return jsonify({"status": "scan_started"})

@app.route('/api/approve', methods=['POST'])
def api_approve():
    title = request.json.get('title')
    if title:
        with scan_lock:
            whitelist.add(title)
            # Re-evaluate in next cycle, or just let micro-scan handle it
    return jsonify({"status": "approved"})

@app.route('/api/reject', methods=['POST'])
def api_reject():
    title = request.json.get('title')
    if title:
        with scan_lock:
            blacklist.add(title)
    return jsonify({"status": "rejected"})

# ── NEAR pool snapshot (UI tab) ─────────────────────────────
@app.route('/api/near')
def api_near():
    with poly_clob_cache_lock:
        clob = dict(poly_clob_cache)
    with res_cache_lock:
        ka = dict(kalshi_res_cache)
        sx = dict(sx_res_cache)
    ws_books = {}
    if ws_client is not None:
        for tid in clob.keys():
            b = ws_client.get_book(tid)
            if b: ws_books[tid] = b
    items = near_summary(clob_res=clob, kalshi_res=ka, sx_res=sx, ws_books=ws_books)
    return jsonify({
        'count': len(items),
        'buffer_cents': round(NEAR_BUFFER * 100, 1),
        'items': items,
    })

# ── Analytics ────────────────────────────────────────────────
@app.route('/api/analytics')
def api_analytics():
    period = (request.args.get('period') or 'month').lower()
    if period not in ('day', 'week', 'month', 'all'):
        period = 'month'
    return jsonify(analytics.aggregate(period))


@app.route('/api/analytics/history')
def api_analytics_history():
    """Per-trade history — every 'opened' event in the period, paginated.
    Filters: platform, structure, min_net. Newest first.
    Query: period=day|week|month|all, limit, offset, platform, structure, min_net"""
    period = (request.args.get('period') or 'all').lower()
    if period not in ('day', 'week', 'month', 'all'):
        period = 'all'
    try:
        limit = max(1, min(int(request.args.get('limit', '100')), 1000))
    except (TypeError, ValueError):
        limit = 100
    try:
        offset = max(0, int(request.args.get('offset', '0')))
    except (TypeError, ValueError):
        offset = 0
    try:
        min_net = float(request.args.get('min_net', '0'))
    except (TypeError, ValueError):
        min_net = 0.0
    platform = request.args.get('platform') or None
    structure = request.args.get('structure') or None
    return jsonify(analytics.history(period=period, limit=limit, offset=offset,
                                     platform=platform, structure=structure,
                                     min_net=min_net))

# ── Phase 2: paper trading dashboard endpoints ───────────────────
@app.route('/api/paper_stats')
def api_paper_stats():
    """Rolling stats from Executions/paper_results.jsonl. Used by the
    dashboard's paper-trade panel and the Phase 5 graduation gate."""
    n = int(request.args.get('window', '100'))
    return jsonify(paper_stats(window_n=n))

# ── Phase 5: paper trading + graduation gate endpoints ───────────
@app.route('/api/graduation')
def api_graduation():
    """Graduation gate status — count, win rate, drift, blockers,
    ready flag. The dashboard uses this to render the 🎓 ready banner
    and the blocker list."""
    return jsonify(paper_trading.graduation_status().to_dict())


@app.route('/api/paper_distribution')
def api_paper_distribution():
    """P&L histogram bins for the Analytics tab chart."""
    n = int(request.args.get('window', '500'))
    return jsonify(paper_trading.paper_distribution(window_n=n))


@app.route('/api/graduation_history')
def api_graduation_history():
    """Daily rolling win rate / drift for the last N days — time-series
    so the operator sees the trajectory toward graduation."""
    days = int(request.args.get('days', '14'))
    return jsonify({'days': paper_trading.graduation_history(days=days)})


# ── Phase 4: wallet pool endpoints ───────────────────────────────
@app.route('/api/wallets')
def api_wallets():
    """Snapshot of wallet pool — bots, balances, signing capability,
    pool backend. Polled by the dashboard's wallets panel."""
    return jsonify({
        'backend': _wallet_pool.backend,
        'cold_address': _wallet_pool.cold_address,
        'count': len(_wallet_pool.wallets),
        'bots': [{
            'bot_id': w.bot_id,
            'eth_address': w.eth_address,
            'store_name': w.store_name,
            'can_sign': w.can_sign,
            'usdc': round(w.last_known_usdc, 2),
            'last_balance_unix': w.last_balance_check_unix,
        } for w in _wallet_pool.wallets],
    })


@app.route('/api/rebalance/proposals')
def api_rebalance_proposals():
    """Compute rebalance proposals against the current pool. Read-only —
    nothing transferred. The auto loop runs separately and logs results
    to Executions/rebalance.jsonl."""
    proposals = wallets_mod.propose_rebalances(_wallet_pool)
    return jsonify({
        'count': len(proposals),
        'proposals': [{
            'from': p.from_bot, 'to': p.to_bot,
            'amount_usdc': p.amount_usdc, 'reason': p.reason,
        } for p in proposals],
        'history': wallets_mod.rebalance_history(limit=20),
    })


# ── Phase 3: risk management endpoints ───────────────────────────
@app.route('/api/risk_status')
def api_risk_status():
    """Live risk snapshot — daily P&L, pause state, kill flag, last reconcile.
    Polled by the dashboard every few seconds for the risk panel."""
    return jsonify(risk_mod.snapshot())


@app.route('/api/network_status')
def api_network_status():
    """Network safety (Layer 3) — current outbound IP + country + whether
    it's in ALLOWED_COUNTRIES. Used to verify VPS is on the right network
    before flipping DRY_RUN=0. force=1 query param bypasses cache."""
    force = request.args.get('force') == '1'
    if force:
        risk_mod.get_current_ip_country(force_refresh=True)
    return jsonify(risk_mod.network_status())


@app.route('/api/kill', methods=['POST'])
def api_kill():
    """Trip the kill switch. Body MUST include {confirm: 'YES'} —
    server-side double-confirm enforcement (UI also has a modal, this is
    belt-and-suspenders so a misclicked dev curl doesn't kill prod)."""
    body = request.get_json(silent=True) or {}
    if body.get('confirm') != 'YES':
        return jsonify({'status': 'error',
                        'reason': 'must POST {"confirm": "YES", "reason": "..."} '
                                  'to confirm kill — guards against accidental clicks'}), 400
    reason = body.get('reason') or 'manual_dashboard'
    info = risk_mod.kill(reason=reason)
    return jsonify({'status': 'killed', 'flag': info})


@app.route('/api/risk_resume', methods=['POST'])
def api_risk_resume():
    """Clear the kill switch and any active pause. Body needs
    {confirm: 'YES'}. Operator-only — typically used after investigating
    a reconcile mismatch or daily-limit pause."""
    body = request.get_json(silent=True) or {}
    if body.get('confirm') != 'YES':
        return jsonify({'status': 'error',
                        'reason': 'must POST {"confirm": "YES"} to confirm resume'}), 400
    was_killed = risk_mod.unkill(reason=body.get('reason') or 'manual_resume')
    s = risk_mod.get_state()
    s.paused_until_unix = None
    s.paused_reason = None
    risk_mod.save_state(s)
    return jsonify({'status': 'resumed', 'was_killed': was_killed})


@app.route('/api/dryfire', methods=['POST'])
def api_dryfire():
    """Manually trigger a dry-fire on a specific deal by title (matches the
    one shown on a card). Useful for ad-hoc testing — auto-fire already
    handles new arbs, but a manual trigger lets the user re-fire to
    re-evaluate realistic slippage on demand."""
    body = request.get_json(silent=True) or {}
    title = body.get('title')
    if not title:
        return jsonify({'status': 'error', 'reason': 'title required'}), 400
    with scan_lock:
        deals = list(scan_data.get('deals') or [])
    matches = [d for d in deals if d.get('title') == title]
    if not matches:
        return jsonify({'status': 'error', 'reason': f'no deal matches title {title!r}'}), 404
    fired = []
    for d in matches:
        try:
            r = fire_arb(d, wallets=_DRY_RUN_WALLETS, dry_run=True)
            fired.append({'arb_id': r.arb_id, 'structure': r.deal_structure,
                          'leg_count': len(r.legs), 'aborted': r.aborted_reason})
        except Exception as e:
            fired.append({'error': str(e)})
    return jsonify({'status': 'ok', 'fired': fired})

def on_lim_ws_update(slug):
    """Limitless WS pushed an orderbook update for `slug`.

    Phase 9d (28.04.2026): real push-driven re-evaluation. Look the parent
    event up via lim_slug_index (O(1)), pull the current WS orderbook for
    every slug under that event, run eval_limitless on the single event,
    and merge new deals into scan_data immediately — same pattern as
    Polymarket's on_ws_update.

    Why this matters: Limitless markets are mostly 30-minute crypto
    oracles where prices move fast in the last minutes before resolution.
    The 5s micro-loop polling we relied on before would miss most of
    those arb windows. Push-driven re-eval brings reaction latency to
    ~250ms (coalesce tick) — parity with Polymarket.
    """
    if lim_ws_client is None:
        return
    with lim_slug_index_lock:
        ev = lim_slug_index.get(slug)
    if ev is None:
        return

    # Build a fresh per-slug orderbook map for this single event from the
    # WS cache. Falls back to the last REST snapshot in lim_res_cache for
    # any slugs the WS hasn't pushed yet (newly added negRisk children).
    children = ev.get('markets') or []
    slugs = []
    if children:
        for c in children:
            s = c.get('slug') or c.get('address')
            if s: slugs.append(s)
    else:
        s = ev.get('slug') or ev.get('address')
        if s: slugs.append(s)

    fresh_lim_res = {}
    with res_cache_lock:
        cached_snapshot = dict(lim_res_cache)
    for s in slugs:
        cached = lim_ws_client.get_book(s) if lim_ws_client else None
        if cached and (time.time() - cached.get('ts', 0)) < 5.0:
            yes_ask = cached.get('best_yes_ask')
            yes_bid = cached.get('best_yes_bid')
            no_ask = (1 - yes_bid) if (yes_bid is not None and 0 < yes_bid < 1) else None
            fresh_lim_res[s] = (
                yes_ask, cached.get('depth_yes', 0),
                no_ask, cached.get('depth_no', 0),
            )
        elif s in cached_snapshot:
            fresh_lim_res[s] = cached_snapshot[s]

    if not fresh_lim_res:
        return  # nothing to evaluate yet

    new_deals = eval_limitless([ev], fresh_lim_res)
    if not new_deals:
        # Push made no arbs visible — but if this event WAS in scan_data
        # before, it might have crossed back above threshold and should be
        # dropped. Use the same merge path as the success case.
        pass

    base_title = ev.get('title') or ev.get('proxyTitle') or '?'
    with scan_lock:
        deals = list(scan_data.get('deals', []))
        # Drop any existing Limitless deals matching this event's title
        # (parent or per-market suffix). Same pattern as on_ws_update.
        deals = [
            d for d in deals
            if not (d.get('platform') == 'Limitless'
                    and (d['title'] == base_title
                         or d['title'].startswith(base_title + ' (')
                         or d['title'].startswith(base_title + ' — ')))
        ]
        for d in new_deals:
            if not d.get('is_quarantine'):
                deals.append(d)
        deals.sort(key=lambda d: d['net'], reverse=True)
        scan_data['deals'] = deals
        if 'stats' in scan_data and isinstance(scan_data['stats'], dict):
            scan_data['stats']['arb_found'] = len(deals)
    # Auto-dry-fire any newcomer arbs (Phase 2 — same gate as Polymarket).
    _maybe_dry_fire(new_deals)


def on_poly_fill(event):
    """Polymarket user-channel `trade` event → fills.registry.consume.

    Phase 9f bridge — same role as on_lim_fill but for Polymarket. Polymarket
    pushes `trade` lifecycle events (MATCHED → MINED → CONFIRMED). We latch
    on MATCHED (status='MATCHED' or 'matched') because that's when the
    on-chain match exists; CONFIRMED comes later when the tx is mined and
    only useful for reconcile, not for atomic-wake.

    Event shape per Polymarket docs:
      {event_type:'trade', id, taker_order_id, maker_orders[],
       market, asset_id, side, size, price, status, timestamp, ...}
    `taker_order_id` is the order_id WE sent in our POST. Match by that.
    """
    if not event:
        return
    typ = (event.get('event_type') or event.get('type') or '').lower()
    if typ != 'trade':
        return
    status = (event.get('status') or '').upper()
    # Latch on MATCHED only — CONFIRMED arrives later (mined). Both have the
    # same fill price/size, so consuming on MATCHED is the right speed/safety.
    if status not in ('MATCHED', 'CONFIRMED'):
        return

    order_id = (event.get('taker_order_id') or event.get('order_id')
                or event.get('orderId'))
    market = event.get('market')        # condition_id
    try:
        fill_price = float(event.get('price', 0) or 0) or None
    except Exception:
        fill_price = None
    try:
        fill_size = float(event.get('size', 0) or 0)
    except Exception:
        fill_size = None

    result = {
        'fill_price': fill_price,
        'fill_size_usdc': fill_size,
        'status': status,
        'asset_id': event.get('asset_id'),
        'condition_id': market,
        'raw': event,
    }
    try:
        from executor import fills as _fills_mod
    except Exception:
        return

    consumed = None
    if order_id:
        consumed = _fills_mod.registry.consume_by_order_id(
            'polymarket', str(order_id), result)
    if consumed is None and market:
        # Fallback: SETTLEMENT events that don't carry our orderId, key by
        # condition_id (we register slug=condition_id for poly legs).
        consumed = _fills_mod.registry.consume_by_slug(
            'polymarket', market, result)

    if consumed:
        print(f"[POLY FILL] {status} → arb {consumed.arb_id} leg {consumed.leg_idx} "
              f"(price={fill_price})")


def on_lim_fill(event):
    """Authenticated `orderEvent` push from Limitless WS.

    Phase 9e (28.04.2026): the bridge from Limitless WS to executor's
    fills.registry. atomic._fire_one_leg_live registers a future on
    (platform='limitless', order_id=...) and waits on its Event. When
    this callback fires, we look up the registration and set the Event
    so atomic wakes immediately instead of waiting for the 5s dead-man.

    Two event shapes per docs:
      - source='OME': matching-engine fill, has takerOrderId / orderId,
        marketId/slug, price, remainingSize.
      - source='SETTLEMENT': on-chain settlement, has takerOrderId,
        marketSlug, txHash, makerMatches[].

    We try by orderId first, fall back to slug (FIFO across same-slug
    legs of one arb). Either way fills.registry pops the registration
    and sets its Event.
    """
    if not event:
        return
    src = event.get('source', '?')
    typ = event.get('type', '?')

    # Build a normalised result dict for atomic to consume
    result = {
        'fill_price': float(event.get('price', 0) or 0) or None,
        'fill_size_usdc': None,
        'remaining_size': event.get('remainingSize'),
        'source': src,
        'type': typ,
        'tx_hash': event.get('txHash'),
        'raw': event,
    }
    # remainingSize > 0 means partial fill — still wake atomic but keep
    # status info in `result` so it can decide reversal later.
    try:
        from executor import fills as _fills_mod
    except Exception:
        return

    order_id = (event.get('orderId') or event.get('takerOrderId')
                or event.get('order_id'))
    slug = event.get('marketSlug') or event.get('slug')

    consumed = None
    if order_id:
        consumed = _fills_mod.registry.consume_by_order_id('limitless', str(order_id), result)
    if consumed is None and slug:
        consumed = _fills_mod.registry.consume_by_slug('limitless', slug, result)

    if consumed:
        print(f"[LIM FILL] {src}/{typ} → arb {consumed.arb_id} leg {consumed.leg_idx} "
              f"(price={result['fill_price']})")
    else:
        # Not our fill — every market push lands here too. Quiet at INFO level.
        pass


if __name__ == '__main__':
    # Start Polymarket WS client (idle until first scan populates pools)
    ws_client = PolyMarketWS(on_update=on_ws_update, max_subs=MAX_WS_SUBS, verbose=True)
    ws_client.start()
    # Start Limitless WS client (idle until first scan populates pools).
    # Doing it here so the start banner can print its status.
    if ENABLE_LIMITLESS:
        # API key is optional for public market data, REQUIRED for authenticated
        # channels (orderEvent / positions). LimitlessWS gracefully skips
        # auth-only subscriptions if api_key is empty — public stream still works.
        lim_ws_client = LimitlessWS(
            on_update=on_lim_ws_update,
            max_subs=LIMITLESS_MAX_WS_SUBS,
            verbose=False,
            api_key=LIMITLESS_API_KEY or None,
            on_fill=on_lim_fill,
        )
        lim_ws_client.start()

    # Phase 9f: Polymarket user-channel WS, one per wallet that has L2 creds.
    # Skips wallets without poly_api_key/secret/passphrase silently.
    for w in _wallet_pool.wallets:
        if getattr(w, 'has_poly_creds', False):
            client = PolyUserWS(wallet=w, on_fill=on_poly_fill, verbose=False)
            client.start()
            poly_user_ws_clients.append(client)

    # Initialize analytics (loads persisted state, if any)
    analytics.init()

    # Warm cache — load the previous scan_data snapshot so /api/deals is
    # not empty while the first cold run_scan() is still in flight.
    _restore_scan_state()

    threading.Thread(target=scan_loop, daemon=True).start()
    if ENABLE_KALSHI:
        threading.Thread(target=kalshi_micro_loop, daemon=True).start()
    if ENABLE_SX:
        threading.Thread(target=sx_micro_loop, daemon=True).start()
    if ENABLE_LIMITLESS:
        threading.Thread(target=limitless_micro_loop, daemon=True).start()
    threading.Thread(target=poly_micro_fallback_loop, daemon=True).start()
    threading.Thread(target=analytics_loop, daemon=True).start()

    # Phase 3: position reconciliation runs every 60s, halts on mismatch.
    # Phase 9f: register the Polymarket fetcher (GET /data/positions with
    # L2 HMAC auth). Each wallet that has poly creds adds its own fetcher;
    # the loop merges them into a single remote view. Without creds we
    # silently skip — local-only reconcile is still safe for paper-trade.
    from risk import reconcile as _reconcile

    def _make_poly_fetcher(w):
        from executor.builders import build_poly_hmac_headers, POLY_POSITIONS_URL
        def fetch():
            try:
                path = '/data/positions'
                ts = int(time.time())
                headers = build_poly_hmac_headers(
                    method='GET', path=path, body='',
                    api_key=w.poly_api_key,
                    api_secret=w.poly_secret,
                    passphrase=w.poly_passphrase,
                    eth_address=w.eth_address,
                    ts=ts,
                )
                r = requests.get(POLY_POSITIONS_URL, headers=headers, timeout=10)
                if r.status_code != 200:
                    return {}
                data = r.json() or []
                out = {}
                for p in (data if isinstance(data, list)
                          else data.get('positions') or []):
                    market = (p.get('conditionId') or p.get('condition_id')
                              or p.get('market'))
                    outcome = p.get('outcome') or p.get('outcomeIndex')
                    size = p.get('size') or p.get('amount') or 0
                    if market and outcome is not None:
                        try:
                            out[('Polymarket', market, outcome)] = float(size)
                        except Exception: pass
                return out
            except Exception as e:
                print(f"[RECONCILE poly fetcher {w.bot_id}] {e}")
                return {}
        return fetch

    poly_fetchers_registered = 0
    for w in _wallet_pool.wallets:
        if getattr(w, 'has_poly_creds', False):
            _reconcile.register_exchange_fetcher(_make_poly_fetcher(w))
            poly_fetchers_registered += 1
    if poly_fetchers_registered > 0:
        print(f"  Reconcile: Polymarket fetcher registered ({poly_fetchers_registered} wallet(s))")

    # Phase 9e: register the Limitless fetcher (WS-positions-cache primary,
    # REST /portfolio fallback). This is the FIRST live exchange fetcher
    # since cancel/positions on Limitless authenticate via X-API-Key, no
    # private key required.
    if ENABLE_LIMITLESS and lim_ws_client is not None:
        from risk import reconcile as _reconcile

        def _fetch_limitless_positions_for_reconcile():
            """Use the WS-pushed positions cache when fresh (<60s old),
            otherwise fall back to REST. Returns the same shape every
            other reconcile fetcher does: dict keyed by
            (platform, market_id, outcome) → size_usdc."""
            age = lim_ws_client.positions_age_s() if lim_ws_client else None
            if age is not None and age < 60:
                return lim_ws_client.get_positions_snapshot()
            # REST fallback. Only attempt when api_key present (auth).
            if not LIMITLESS_API_KEY:
                return {}
            try:
                # Try /portfolio/{eth_address}; on shape change, return {}
                # rather than raising — reconcile treats fetcher errors
                # as "remote unknown" and we don't want to halt the loop
                # over a docs-changed endpoint.
                addr = next(
                    (w.eth_address for w in _wallet_pool.wallets if w.eth_address),
                    None,
                )
                if not addr:
                    return {}
                r = requests.get(
                    f"{LIMITLESS_API_BASE}/portfolio/{addr}",
                    headers={'X-API-Key': LIMITLESS_API_KEY},
                    timeout=10,
                )
                if r.status_code != 200:
                    return {}
                positions = r.json() or []
                out = {}
                for p in (positions if isinstance(positions, list)
                          else positions.get('positions') or []):
                    slug = p.get('marketSlug') or p.get('slug')
                    outcome = p.get('outcome') or p.get('outcomeIndex')
                    size = p.get('size') or p.get('amount') or 0
                    if slug and outcome is not None:
                        try:
                            out[('Limitless', slug, outcome)] = float(size)
                        except Exception: pass
                return out
            except Exception as e:
                print(f"[RECONCILE limitless fetcher] {e}")
                return {}

        _reconcile.register_exchange_fetcher(
            _fetch_limitless_positions_for_reconcile)
        print("  Reconcile: Limitless fetcher registered (WS cache + REST fallback)")

    risk_mod.start_reconcile_loop()

    print("=" * 60)
    print("  ARBITRAGE RADAR v7 — http://localhost:5050")
    poly_total = POLY_MAIN_PAGES * 500
    kalshi_str = "1000 events" if ENABLE_KALSHI else "DISABLED"
    sx_str = "up to 1000 markets" if ENABLE_SX else "DISABLED"
    lim_total = LIMITLESS_MAIN_PAGES * 100
    lim_str = f"up to {lim_total} markets" if ENABLE_LIMITLESS else "DISABLED"
    print(f"  Poly ({poly_total}) + Kalshi ({kalshi_str}) + SX Bet ({sx_str}) + Limitless ({lim_str})")
    print(f"  HOT/NEAR pools (buffer={NEAR_BUFFER:.2f})")
    print(f"  Polymarket WS: max {MAX_WS_SUBS} subs, ping every 10s")
    if ENABLE_LIMITLESS:
        print(f"  Limitless WS:  max {LIMITLESS_MAX_WS_SUBS} subs, ping every 15s")
    if ENABLE_KALSHI:
        print(f"  Kalshi REST micro: every {KALSHI_MICRO_INTERVAL}s on HOT+NEAR")
    if ENABLE_SX:
        print(f"  SX Bet REST micro: every {SX_MICRO_INTERVAL}s on HOT+NEAR (live sport)")
    print(f"  Risk: max ${risk_mod.MAX_PER_TRADE_USD:.0f}/trade, "
          f"daily loss limit -${risk_mod.DAILY_LOSS_LIMIT_USD:.0f}, "
          f"{risk_mod.LOSING_TRADES_PER_HOUR} losing/h → 1h pause")
    if risk_mod.is_killed():
        print("  ⚠ KILL SWITCH ACTIVE (Executions/.killed exists) — fires blocked")
    # Phase 4: wallet pool status
    n = len(_wallet_pool.wallets)
    sig = sum(1 for w in _wallet_pool.wallets if w.can_sign)
    print(f"  Wallets: {n} bot(s) loaded ({sig} can sign), backend={_wallet_pool.backend}"
          + (" — empty pool, executor falls back to mock stub" if n == 0 else ""))
    # Network safety (Layer 3) — fetch own IP/country at startup so the
    # operator sees immediately if VPS is on the wrong network.
    if risk_mod.ALLOWED_COUNTRIES:
        ip, country, err = risk_mod.get_current_ip_country(force_refresh=True)
        if err:
            print(f"  ⚠ Network: ALLOWED={','.join(sorted(risk_mod.ALLOWED_COUNTRIES))} "
                  f"— check FAILED ({err[:60]}). Fires will be blocked until network recovers.")
        elif country in risk_mod.ALLOWED_COUNTRIES:
            print(f"  Network: ALLOWED={','.join(sorted(risk_mod.ALLOWED_COUNTRIES))} "
                  f"| current IP {ip} ({country}) → ✓ allowed")
        else:
            print(f"  ⚠ Network: ALLOWED={','.join(sorted(risk_mod.ALLOWED_COUNTRIES))} "
                  f"| current IP {ip} ({country}) → ✗ DISALLOWED. Fires WILL be blocked.")
    else:
        print(f"  Network: ALLOWED_COUNTRIES not set — geo check DISABLED "
              f"(safe for local dev; set on VPS, e.g. ALLOWED_COUNTRIES=GE)")
    print("============================================================")

    # Phase 8: notify operator on startup so they know radar booted (esp.
    # after a crash/restart). Telegram envvars optional; no-op if unset.
    try:
        import notify
        if notify.is_configured():
            sig_count = sum(1 for w in _wallet_pool.wallets if w.can_sign)
            startup_msg = (
                f'*Radar started*\n'
                f'Mode: `{"DRY_RUN" if executor_atomic_dry_run() else "LIVE"}`\n'
                f'Platforms: Poly={poly_total}'
                + (f' Kalshi+Sx ON' if (ENABLE_KALSHI or ENABLE_SX) else ' (Kalshi/SX disabled)')
                + f'\nWallets: {len(_wallet_pool.wallets)}/6 ({sig_count} can sign)'
                + (f'\nNetwork: {",".join(sorted(risk_mod.ALLOWED_COUNTRIES))}'
                   if risk_mod.ALLOWED_COUNTRIES else '\nNetwork check: DISABLED')
            )
            notify.send(startup_msg, level='success', dedupe_key='radar_startup')
    except Exception as _e:
        print(f"  (telegram startup notify skipped: {_e})")

    # threaded=True is critical: the dev WSGI handler serializes requests
    # by default. With background scan_loop fetching for 30-90s, /api/deals
    # would queue behind any in-flight handler and the dashboard would show
    # "Сервер недоступен" while a cold scan is running. With threading on,
    # endpoints respond from the live scan_data snapshot instantly.
    app.run(host='0.0.0.0', port=5050, debug=False, threaded=True)


def executor_atomic_dry_run():
    """Helper for startup banner — returns True if executor is in dry-run mode."""
    try:
        from executor.atomic import DRY_RUN
        return DRY_RUN
    except Exception:
        return True
