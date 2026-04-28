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
    Called after every main scan and after every WS-driven re-eval."""
    if not deals:
        return
    with _fired_arb_keys_lock:
        for d in deals:
            if d.get('is_quarantine'): continue
            key = _arb_fire_key(d)
            if key in _fired_arb_keys: continue
            try:
                fire_arb(d, wallets=_DRY_RUN_WALLETS, dry_run=True)
                _fired_arb_keys.add(key)
            except Exception as e:
                print(f"[DRYFIRE] error firing {key}: {e}")

@app.after_request
def add_cors(resp):
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp

# ── Config ──────────────────────────────────────────────────────
BALANCE = 100.0
THETA_POLY   = 0.025   # Polymarket taker fee ~2.5%
THETA_KALSHI = 0.07    # Kalshi taker fee ~7%
THETA_SX     = 0.02    # SX Bet taker fee ~2%
THRESH_POLY   = 0.97   # 97c — covers ~2.5% taker fee with margin (idea.md)
THRESH_KALSHI = 0.93   # 93c — covers ~7% taker fee with margin (idea.md)
THRESH_SX     = 0.97   # 97c — covers ~2% taker fee with margin (idea.md)
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

# Polymarket main-scan pages. Each page = 500 events. 4 pages = 2000 events
# per scan. Default was 2 pages; bumped because skipping Kalshi/SX frees
# ~25s of fetch budget per scan that we can spend on more Poly coverage.
POLY_MAIN_PAGES = int(os.environ.get('POLY_MAIN_PAGES', '4'))
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
WINDOW_DAYS = 30               # accept events ending within this many days (was 10)
WINDOW_PAST_DAYS = 2           # also keep events that ended up to this many days ago

DEADLINE_RE = re.compile(
    r'\b(by|before)\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|'
    r'january|february|march|april|june|july|august|september|october|november|december|'
    r'20\d{2}|end of|q[1-4])', re.IGNORECASE)

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
}
pools_lock = threading.Lock()

# Reverse index: Polymarket token_id -> candidate, used by WS callback to know
# which event to re-evaluate when a price_change arrives.
poly_token_index = {}
poly_token_index_lock = threading.Lock()

# Last full REST clob_res cached so WS-driven re-eval can fall back to old asks
# for tokens of the same event that haven't been pushed yet.
# Also reused by /api/near to render NEAR snapshot without re-fetching.
poly_clob_cache = {}
poly_clob_cache_lock = threading.Lock()
kalshi_res_cache = {}
sx_res_cache = {}
res_cache_lock = threading.Lock()

# Polymarket WS client (initialized in __main__).
ws_client = None

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

def build_deal(title, platform, outcomes, total_price, theta, threshold):
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

    # Per-trade risk-cap scale — total deal cost (= actual_balance) must stay
    # within MAX_PER_TRADE_USD so the executor's risk gate doesn't block it.
    if BALANCE * scale_factor > _RISK_PER_TRADE_CAP:
        scale_factor = _RISK_PER_TRADE_CAP / BALANCE

    actual_balance = BALANCE * scale_factor
    gross = actual_balance * (1 - total_price)
    
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

def _eval_poly_structures(cand, clob_res=None, ws_books=None):
    """Returns a list of deals — one per arb structure (A/B/C) that crosses
    its threshold. Empty list if none. Used by both batch eval_poly and the
    WS push callback (single-candidate refresh)."""
    ev, rough, is_q = cand
    per_market = _poly_per_market(rough, clob_res, ws_books)
    if len(per_market) < 2: return []
    title = ev.get('title', '?')
    deals = []

    def _quality_ok(d):
        if d['total_cents'] >= 95.0:
            if d['min_liq'] < 1000 or d['slip_pct'] >= 0.3: return False
        return True

    # ── A. ALL_YES ──────────────────────────────────────────────────
    yes_out = [{'name': p['name'], 'price': p['yes_price'],
                'liquidity': p['yes_liq'], 'source': p['yes_src'],
                'volume': p['volume']} for p in per_market]
    total_yes = sum(o['price'] for o in yes_out)
    if total_yes < THRESH_POLY:
        d = build_deal(title, 'Polymarket', yes_out, total_yes, THETA_POLY, THRESH_POLY)
        if d:
            d['is_quarantine'] = is_q; d['arb_structure'] = 'all_yes'
            if _quality_ok(d): deals.append(d)

    # ── B. ALL_NO (N>=3, multi-outcome) ─────────────────────────────
    no_raw = [p for p in per_market if p['no_price'] is not None and 0 < p['no_price'] < 1]
    N = len(no_raw)
    if N >= 3:
        no_out = [{'name': f"NO {p['name']}", 'price': p['no_price'],
                   'liquidity': p['no_liq'], 'source': p['no_src'],
                   'volume': p['volume']} for p in no_raw]
        total_no = sum(o['price'] for o in no_out)
        no_threshold = (N - 1) * THRESH_POLY
        if total_no < no_threshold:
            d = build_deal(title + ' (ALL_NO)', 'Polymarket', no_out,
                           total_no, THETA_POLY, no_threshold)
            if d:
                d['is_quarantine'] = is_q; d['arb_structure'] = 'all_no'
                d['payout_target'] = N - 1
                deals.append(d)

    # ── C. YES_NO_PAIR (per-market) ─────────────────────────────────
    for p in per_market:
        if p['no_price'] is None or not (0 < p['no_price'] < 1): continue
        if not (0 < p['yes_price'] < 1): continue
        pair_total = p['yes_price'] + p['no_price']
        if pair_total >= THRESH_POLY: continue
        pair_out = [
            {'name': f"YES {p['name']}", 'price': p['yes_price'],
             'liquidity': p['yes_liq'], 'source': p['yes_src'], 'volume': p['volume']},
            {'name': f"NO {p['name']}", 'price': p['no_price'],
             'liquidity': p['no_liq'], 'source': p['no_src'], 'volume': p['volume']},
        ]
        d = build_deal(f"{title} — {p['name']}", 'Polymarket', pair_out,
                       pair_total, THETA_POLY, THRESH_POLY)
        if d:
            d['is_quarantine'] = is_q; d['arb_structure'] = 'yes_no_pair'
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
            })
        if len(per_market) < 2: continue

        # ── A. ALL_YES ──────────────────────────────────────────────
        yes_outcomes = [{'name': p['name'], 'price': p['yes_price'],
                         'liquidity': p['yes_liq'], 'source': 'kalshi_ob'}
                        for p in per_market]
        total_yes = sum(o['price'] for o in yes_outcomes)
        if (0.50 <= total_yes < THRESH_KALSHI
                and any(o['price'] > 0.20 for o in yes_outcomes)):
            d = build_deal(ev.get('title','?'), 'Kalshi', yes_outcomes,
                           total_yes, THETA_KALSHI, THRESH_KALSHI)
            if d: d['arb_structure'] = 'all_yes'; deals.append(d)

        # ── B. ALL_NO (N>=3) ────────────────────────────────────────
        no_raw = [p for p in per_market if p['no_price'] is not None]
        N = len(no_raw)
        if N >= 3:
            no_outcomes = [{'name': f"NO {p['name']}", 'price': p['no_price'],
                            'liquidity': p['no_liq'], 'source': 'kalshi_ob'}
                           for p in no_raw]
            total_no = sum(o['price'] for o in no_outcomes)
            no_threshold = (N - 1) * THRESH_KALSHI
            if total_no < no_threshold:
                d = build_deal(ev.get('title','?') + ' (ALL_NO)', 'Kalshi',
                               no_outcomes, total_no, THETA_KALSHI, no_threshold)
                if d:
                    d['arb_structure'] = 'all_no'; d['payout_target'] = N - 1
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
            if d: d['arb_structure'] = 'yes_no_pair'; deals.append(d)
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
            deals.append(deal)
    return deals

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
    _ev, rough, _is_q = cand
    pm = _poly_per_market(rough, clob_res, ws_books)
    if len(pm) < 2: return None
    candidates = []
    # A
    s_yes = sum(p['yes_price'] for p in pm if 0 < p['yes_price'] < 1)
    if s_yes > 0: candidates.append(s_yes)
    # B
    no_raw = [p for p in pm if p['no_price'] is not None and 0 < p['no_price'] < 1]
    N = len(no_raw)
    if N >= 3:
        s_no = sum(p['no_price'] for p in no_raw)
        candidates.append(s_no / (N - 1))
    # C
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
    threshold. Same normalization scheme as _sum_poly_cand."""
    ev, _tickers = ev_tickers_pair
    pm = []
    for m in ev.get('markets', []):
        t = m.get('ticker', '')
        if t not in kalshi_res: continue
        yes_ask, _yd, no_ask, _nd = kalshi_res[t]
        if yes_ask is None or yes_ask < 0.05 or yes_ask >= 1: continue
        pm.append({'yes': yes_ask, 'no': no_ask if (no_ask and 0 < no_ask < 1) else None})
    if len(pm) < 2: return None
    candidates = []
    # A. ALL_YES — keep the existing 0.50 floor to drop garbage events
    s_yes = sum(p['yes'] for p in pm)
    if 0.50 <= s_yes: candidates.append(s_yes)
    # B. ALL_NO
    no_raw = [p for p in pm if p['no'] is not None]
    N = len(no_raw)
    if N >= 3:
        candidates.append(sum(p['no'] for p in no_raw) / (N - 1))
    # C. YES_NO_PAIR — best (smallest) per-market pair sum
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

def classify_pools(pc, kc, sx_markets, clob_res, kalshi_res, sx_res, ws_books=None):
    """Split candidates into HOT (sum<thresh) and NEAR ([thresh, thresh+buffer)).
    NEAR lists are sorted by `sum` ascending so the closest-to-arb candidates
    win when the WS subscription set is capped at MAX_WS_SUBS."""
    poly_hot, poly_near = [], []
    for cand in pc:
        s = _sum_poly_cand(cand, clob_res, ws_books or {})
        if s is None: continue
        if s < THRESH_POLY: poly_hot.append((s, cand))
        elif s < THRESH_POLY + NEAR_BUFFER: poly_near.append((s, cand))
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

    return {
        'poly':   {'hot': poly_hot,   'near': poly_near},
        'kalshi': {'hot': kalshi_hot, 'near': kalshi_near},
        'sx':     {'hot': sx_hot,     'near': sx_near},
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

def near_summary(clob_res=None, kalshi_res=None, sx_res=None, ws_books=None):
    """Build a UI-friendly snapshot of NEAR candidates across all platforms.
    Each entry includes `arb_structure` so the dashboard can render A/B/C/binary
    badges. The structure shown is whichever is closest to its threshold."""
    out = []
    with pools_lock:
        poly_near = list(pools['poly']['near'])
        kalshi_near = list(pools['kalshi']['near'])
        sx_near = list(pools['sx']['near'])

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

        is_quarantine = False

        markets = ev.get('markets', [])
        if len(markets) < 2:
            diag['poly_skip_lt2_markets'] += 1; continue
        # Polymarket exposes negRisk on the EVENT (canonical location); the
        # field on each market is almost always False even when the event is
        # mutually-exclusive. Earlier code only looked at market.negRisk and
        # rejected ~100% of valid candidates. Accept either signal.
        if not (ev.get('negRisk') is True or
                (markets and all(m.get('negRisk') is True for m in markets))):
            diag['poly_skip_no_negrisk'] += 1; continue
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

        stats['poly_events'] = len(poly_events)
        stats['kalshi_events'] = len(kalshi_events)
        stats['sx_markets'] = len(sx_markets)
        stats['sx_http_status'] = sx_http_status
        stats['sx_fetch_error'] = sx_fetch_error
        print(f"[FETCH] Poly={len(poly_events)} ({t_poly:.1f}s) Kalshi={len(kalshi_events)} ({t_kalshi:.1f}s) SX={len(sx_markets)} ({t_sx:.1f}s) sx_http={sx_http_status}")

        # Phase 2: Filter (with diagnostic counters)
        pc, poly_tids = filter_poly(poly_events, diag=stats)
        kc, kalshi_tks = filter_kalshi(kalshi_events, diag=stats)
        sx_ml_hashes = [m['marketHash'] for m in sx_markets if m.get('type') in SX_BINARY_TYPES]
        stats['sx_binary_count'] = len(sx_ml_hashes)
        stats['sx_moneyline_count'] = sum(1 for m in sx_markets if m.get('type') == 226)  # subset, kept for back-compat
        stats['poly_neg_risk'] = len(pc)

        # Phase 3: Batch fetch orderbooks
        clob_res = batch_fetch(_fetch_clob, poly_tids)
        kalshi_res = batch_fetch(_fetch_kalshi_ob, kalshi_tks)
        sx_res = batch_fetch(_fetch_sx_orders, sx_ml_hashes)
        
        stats['clob_fetched'] = sum(1 for v in clob_res.values() if v[0] is not None)
        stats['kalshi_ob_fetched'] = sum(1 for v in kalshi_res.values() if v[0] is not None)

        # Phase 4: Evaluate. Disabled platforms are skipped — kc/sx_markets
        # will be empty anyway because we didn't fetch them, but explicit
        # guards make the intent clear and let us short-circuit eval.
        all_deals = eval_poly(pc, clob_res)
        if ENABLE_KALSHI:
            all_deals += eval_kalshi(kc, kalshi_res)
        if ENABLE_SX:
            all_deals += eval_sx(sx_markets, sx_res)
        
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
        new_pools = classify_pools(pc, kc, sx_markets, clob_res, kalshi_res, sx_res, ws_books)
        with pools_lock:
            pools.update(new_pools)
        # Cache REST clob snapshot for WS-driven re-eval fallback + NEAR snapshot
        with poly_clob_cache_lock:
            poly_clob_cache.clear(); poly_clob_cache.update(clob_res)
        with res_cache_lock:
            kalshi_res_cache.clear(); kalshi_res_cache.update(kalshi_res)
            sx_res_cache.clear(); sx_res_cache.update(sx_res)
        # Push token list to WS — capped at MAX_WS_SUBS, HOT first
        if ws_client is not None:
            poly_pool = new_pools['poly']
            tokens = collect_poly_tokens({'hot': poly_pool['hot'], 'near': poly_pool['near']})
            ws_client.update_subscriptions(tokens[:MAX_WS_SUBS])
            new_idx = rebuild_poly_token_index(poly_pool)
            with poly_token_index_lock:
                poly_token_index.clear(); poly_token_index.update(new_idx)
        stats['pool_poly_hot']    = len(new_pools['poly']['hot'])
        stats['pool_poly_near']   = len(new_pools['poly']['near'])
        stats['pool_kalshi_hot']  = len(new_pools['kalshi']['hot'])
        stats['pool_kalshi_near'] = len(new_pools['kalshi']['near'])
        stats['pool_sx_hot']      = len(new_pools['sx']['hot'])
        stats['pool_sx_near']     = len(new_pools['sx']['near'])

        elapsed = time.time() - t0
        print(f"[MAIN] Done in {elapsed:.1f}s — {stats['arb_found']} arb found, {stats['quarantine_count']} in quarantine "
              f"| pools: poly H{stats['pool_poly_hot']}/N{stats['pool_poly_near']} "
              f"kalshi H{stats['pool_kalshi_hot']}/N{stats['pool_kalshi_near']} "
              f"sx H{stats['pool_sx_hot']}/N{stats['pool_sx_near']}")
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

if __name__ == '__main__':
    # Start Polymarket WS client (idle until first scan populates pools)
    ws_client = PolyMarketWS(on_update=on_ws_update, max_subs=MAX_WS_SUBS, verbose=True)
    ws_client.start()

    # Initialize analytics (loads persisted state, if any)
    analytics.init()

    threading.Thread(target=scan_loop, daemon=True).start()
    if ENABLE_KALSHI:
        threading.Thread(target=kalshi_micro_loop, daemon=True).start()
    if ENABLE_SX:
        threading.Thread(target=sx_micro_loop, daemon=True).start()
    threading.Thread(target=poly_micro_fallback_loop, daemon=True).start()
    threading.Thread(target=analytics_loop, daemon=True).start()

    # Phase 3: position reconciliation runs every 60s, halts on mismatch.
    # In Phase 3 there are no exchange fetchers registered yet, so it just
    # logs heartbeats — Phase 4 plugs in real fetchers per wallet.
    risk_mod.start_reconcile_loop()

    print("=" * 60)
    print("  ARBITRAGE RADAR v7 — http://localhost:5050")
    poly_total = POLY_MAIN_PAGES * 500
    kalshi_str = "1000 events" if ENABLE_KALSHI else "DISABLED"
    sx_str = "up to 1000 markets" if ENABLE_SX else "DISABLED"
    print(f"  Poly ({poly_total}) + Kalshi ({kalshi_str}) + SX Bet ({sx_str})")
    print(f"  HOT/NEAR pools (buffer={NEAR_BUFFER:.2f})")
    print(f"  Polymarket WS: max {MAX_WS_SUBS} subs, ping every 10s")
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

    app.run(host='0.0.0.0', port=5050, debug=False)


def executor_atomic_dry_run():
    """Helper for startup banner — returns True if executor is in dry-run mode."""
    try:
        from executor.atomic import DRY_RUN
        return DRY_RUN
    except Exception:
        return True
