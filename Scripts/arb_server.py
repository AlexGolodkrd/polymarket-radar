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

app = Flask(__name__)

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
MAX_WORKERS = 80
TIMEOUT = 5
NEAR_BUFFER = 0.03             # 3c — sum in [threshold, threshold+NEAR_BUFFER) goes to NEAR pool
MAX_WS_SUBS = 200              # Polymarket WS subscription cap (rate-limit guard)
SX_PAGE_SIZE = 100             # SX Bet API rejects pageSize > 100 (HTTP 400)
SX_MAX_PAGES_MAIN = 10         # 10 * 100 = up to 1000 markets in main scan
SX_MAX_PAGES_PAUSE = 5         # 5 * 100 = up to 500 markets in pause scan

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
poly_clob_cache = {}
poly_clob_cache_lock = threading.Lock()

# Polymarket WS client (initialized in __main__).
ws_client = None

# ── Helpers ─────────────────────────────────────────────────────
def calc_fee(price, contracts, theta):
    p = max(0.001, min(0.999, price))
    return theta * contracts * p * (1 - p)

def is_deadline(names):
    if len(names) < 2: return False
    return sum(1 for n in names if DEADLINE_RE.search(n)) >= len(names) * 0.5

def is_within_10_days(date_str=None, timestamp=None):
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
        # Разрешаем события, которые завершаются в течение 10 дней
        # Или которые уже завершились, но еще активны (разрешение в процессе) (до -2 дней)
        return -86400*2 <= diff <= 10 * 86400
    except Exception as e: 
        return False

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
    try:
        r = requests.get(f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}/orderbook",
                         timeout=TIMEOUT, headers=HEADERS)
        lvls = r.json().get('orderbook_fp', {}).get('yes_dollars', [])
        if not lvls: return ticker, None, 0
        return ticker, float(lvls[0][0]), sum(float(l[1]) for l in lvls)
    except: return ticker, None, 0

def _fetch_sx_orders(market_hash):
    try:
        r = requests.get(f"https://api.sx.bet/orders?marketHashes={market_hash}&maker=true", timeout=TIMEOUT)
        data = r.json()
        orders = data.get('data', {}).get('orders', []) if data.get('status') == 'success' else []
        best1, best2, depth1, depth2 = None, None, 0, 0
        for o in orders:
            price = float(o.get('percentageOdds', '0')) / 1e20
            size = float(o.get('orderSizeFillable', '0')) / 1e6
            if price <= 0 or price >= 1: continue
            is_buy = o.get('isMakerBettingOutcomeOne', True)
            if is_buy:
                if best1 is None or price > best1: best1 = price
                depth1 += size * price
            else:
                if best2 is None or price > best2: best2 = price
                depth2 += size * price
        return market_hash, best1, depth1, best2, depth2
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
def build_deal(title, platform, outcomes, total_price, theta, threshold):
    min_liq = float('inf')
    for o in outcomes:
        liq = o.get('liquidity', 0)
        if liq > 0 and liq < min_liq: min_liq = liq
    if min_liq == float('inf'): min_liq = 0

    max_share = max(o['price']/total_price for o in outcomes) if total_price > 0 else 0
    max_theoretical_stake = BALANCE * max_share
    
    scale_factor = 1.0
    if min_liq > 0 and max_theoretical_stake > min_liq:
        scale_factor = min_liq / max_theoretical_stake
    elif min_liq == 0:
        scale_factor = 0.1 # safety
        
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
def eval_poly(cands, clob_res):
    deals = []
    for ev, rough, is_q in cands:
        outcomes = []
        for o in rough:
            m = o['m']
            name = m.get('question', m.get('groupItemTitle', '?'))
            price = o['implied']; liq = float(m.get('liquidity',0) or 0); source = 'implied'
            tid = o.get('token_id')
            if tid and tid in clob_res:
                ask_p, depth = clob_res[tid]
                if ask_p and 0 < ask_p < 1:
                    price = ask_p; liq = depth if depth>0 else liq; source = 'clob_ask'
            outcomes.append({'name': name, 'price': price, 'liquidity': liq, 'source': source,
                           'volume': float(m.get('volume',0) or 0)})
        if len(outcomes) < 2: continue
        total = sum(o['price'] for o in outcomes)
        if total >= THRESH_POLY: continue
        deal = build_deal(ev.get('title','?'), 'Polymarket', outcomes, total, THETA_POLY, THRESH_POLY)
        if deal:
            deal['is_quarantine'] = is_q
            if deal['total_cents'] >= 95.0:
                if deal['min_liq'] < 1000 or deal['slip_pct'] >= 0.3: continue
            deals.append(deal)
    return deals

def eval_kalshi(cands, kalshi_res):
    deals = []
    for ev, tickers in cands:
        outcomes = []
        for m in ev.get('markets', []):
            t = m.get('ticker','')
            if t not in kalshi_res: continue
            price, depth = kalshi_res[t]
            if price is None or price < 0.05 or price >= 1: continue
            outcomes.append({'name': m.get('title',t), 'price': price,
                           'liquidity': depth, 'source': 'kalshi_ob'})
        if len(outcomes) < 2: continue
        total = sum(o['price'] for o in outcomes)
        if total < 0.50 or total >= THRESH_KALSHI: continue
        if not any(o['price'] > 0.20 for o in outcomes): continue
        deal = build_deal(ev.get('title','?'), 'Kalshi', outcomes, total, THETA_KALSHI, THRESH_KALSHI)
        if deal: deals.append(deal)
    return deals

def eval_sx(sx_markets, sx_orders):
    deals = []
    by_event = {}
    for m in sx_markets:
        eid = m.get('sportXeventId', '')
        if eid: by_event.setdefault(eid, []).append(m)
    for eid, markets in by_event.items():
        moneyline = [m for m in markets if m.get('type') == 226]
        if not moneyline: continue
        m = moneyline[0]
        
        # 10-day filter
        if not is_within_10_days(timestamp=m.get('gameTime')): continue

        mh = m.get('marketHash', '')
        if mh not in sx_orders: continue
        best1, depth1, best2, depth2 = sx_orders[mh]
        if best1 is None or best2 is None: continue
        if best1 <= 0 or best2 <= 0: continue
        total = best1 + best2
        if total >= THRESH_SX: continue
        outcomes = [
            {'name': m.get('outcomeOneName','Team 1'), 'price': best1, 'liquidity': depth1, 'source': 'sx_ob'},
            {'name': m.get('outcomeTwoName','Team 2'), 'price': best2, 'liquidity': depth2, 'source': 'sx_ob'}
        ]
        title = f"{m.get('teamOneName','?')} vs {m.get('teamTwoName','?')} ({m.get('leagueLabel','')})"
        deal = build_deal(title, 'SX Bet', outcomes, total, THETA_SX, THRESH_SX)
        if deal: deals.append(deal)
    return deals

# ── Single-candidate re-eval (used by WS callback + classification) ──
def _poly_outcomes_from_cand(cand, clob_res, ws_books):
    """Reconstruct the `outcomes` list for a Polymarket candidate using the
    freshest price source available per token: WS book → REST clob → implied."""
    ev, rough, _is_q = cand
    outcomes = []
    for o in rough:
        m = o['m']
        name = m.get('question', m.get('groupItemTitle', '?'))
        tid = o.get('token_id')
        price = o['implied']
        liq = float(m.get('liquidity', 0) or 0)
        source = 'implied'
        if tid:
            book = ws_books.get(tid) if ws_books else None
            if book and book.get('best_ask') and 0 < book['best_ask'] < 1:
                price = book['best_ask']
                liq = book.get('depth') or liq
                source = 'ws'
            elif clob_res and tid in clob_res:
                ask, depth = clob_res[tid]
                if ask and 0 < ask < 1:
                    price = ask; liq = depth or liq; source = 'clob_ask'
        outcomes.append({'name': name, 'price': price, 'liquidity': liq, 'source': source,
                        'volume': float(m.get('volume', 0) or 0)})
    return outcomes

def _eval_poly_one(cand, clob_res=None, ws_books=None):
    """Build a deal for ONE Polymarket candidate, or None if it fails any guard.
    Pure function — no globals touched. Used by both eval_poly (batch) and the
    WS callback (single-token push)."""
    outcomes = _poly_outcomes_from_cand(cand, clob_res or {}, ws_books or {})
    if len(outcomes) < 2: return None
    total = sum(o['price'] for o in outcomes)
    if total >= THRESH_POLY: return None
    ev, _rough, is_q = cand
    deal = build_deal(ev.get('title', '?'), 'Polymarket', outcomes, total, THETA_POLY, THRESH_POLY)
    if not deal: return None
    deal['is_quarantine'] = is_q
    if deal['total_cents'] >= 95.0:
        if deal['min_liq'] < 1000 or deal['slip_pct'] >= 0.3: return None
    return deal

# ── Pool classification (HOT / NEAR / COLD) ─────────────────────
def _sum_poly_cand(cand, clob_res, ws_books):
    outcomes = _poly_outcomes_from_cand(cand, clob_res, ws_books)
    if len(outcomes) < 2: return None
    return sum(o['price'] for o in outcomes)

def _sum_kalshi_cand(ev_tickers_pair, kalshi_res):
    ev, _tickers = ev_tickers_pair
    prices = []
    for m in ev.get('markets', []):
        t = m.get('ticker', '')
        if t not in kalshi_res: continue
        price, _depth = kalshi_res[t]
        if price is None or price < 0.05 or price >= 1: continue
        prices.append(price)
    if len(prices) < 2: return None
    s = sum(prices)
    return s if 0.50 <= s else None

def _sum_sx_market(m, sx_orders):
    mh = m.get('marketHash', '')
    if mh not in sx_orders: return None
    best1, _d1, best2, _d2 = sx_orders[mh]
    if not best1 or not best2 or best1 <= 0 or best2 <= 0: return None
    return best1 + best2

def classify_pools(pc, kc, sx_markets, clob_res, kalshi_res, sx_res, ws_books=None):
    """Split candidates into HOT (sum<thresh) and NEAR ([thresh, thresh+buffer))."""
    poly_hot, poly_near = [], []
    for cand in pc:
        s = _sum_poly_cand(cand, clob_res, ws_books or {})
        if s is None: continue
        if s < THRESH_POLY: poly_hot.append(cand)
        elif s < THRESH_POLY + NEAR_BUFFER: poly_near.append(cand)

    kalshi_hot, kalshi_near = [], []
    for cand in kc:
        s = _sum_kalshi_cand(cand, kalshi_res)
        if s is None: continue
        if s < THRESH_KALSHI: kalshi_hot.append(cand)
        elif s < THRESH_KALSHI + NEAR_BUFFER: kalshi_near.append(cand)

    # SX: by event (moneyline only)
    sx_hot, sx_near = [], []
    seen_events = set()
    for m in sx_markets:
        if m.get('type') != 226: continue
        eid = m.get('sportXeventId', '')
        if not eid or eid in seen_events: continue
        seen_events.add(eid)
        s = _sum_sx_market(m, sx_res)
        if s is None: continue
        if s < THRESH_SX: sx_hot.append(m)
        elif s < THRESH_SX + NEAR_BUFFER: sx_near.append(m)

    return {
        'poly':   {'hot': poly_hot,   'near': poly_near},
        'kalshi': {'hot': kalshi_hot, 'near': kalshi_near},
        'sx':     {'hot': sx_hot,     'near': sx_near},
    }

def collect_poly_tokens(poly_pool):
    """Flatten HOT+NEAR poly candidates into a list of token_ids for WS subs."""
    out = []
    for cand in poly_pool['hot'] + poly_pool['near']:
        _ev, rough, _ = cand
        for o in rough:
            tid = o.get('token_id')
            if tid: out.append(tid)
    return out

def rebuild_poly_token_index(poly_pool):
    """token_id -> candidate, for WS callback reverse lookup."""
    idx = {}
    for cand in poly_pool['hot'] + poly_pool['near']:
        _ev, rough, _ = cand
        for o in rough:
            tid = o.get('token_id')
            if tid: idx[tid] = cand
    return idx

# ── WS push callback ────────────────────────────────────────────
def on_ws_update(token_id):
    """Polymarket WS pushed an orderbook update for `token_id`. Re-evaluate
    that candidate and inject/replace the deal in scan_data['deals']."""
    if ws_client is None: return
    with poly_token_index_lock:
        cand = poly_token_index.get(token_id)
    if cand is None: return
    with poly_clob_cache_lock:
        clob_snapshot = dict(poly_clob_cache)
    ws_books = {}
    # Pull books only for tokens of THIS candidate to keep the snapshot tight
    _ev, rough, _ = cand
    for o in rough:
        tid = o.get('token_id')
        if tid:
            b = ws_client.get_book(tid)
            if b: ws_books[tid] = b
    deal = _eval_poly_one(cand, clob_res=clob_snapshot, ws_books=ws_books)
    title = cand[0].get('title', '?')
    with scan_lock:
        deals = list(scan_data.get('deals', []))
        # Remove existing entry for this title (if any), then insert new if profitable
        deals = [d for d in deals if d['title'] != title]
        if deal and not deal.get('is_quarantine'):
            deals.append(deal)
        deals.sort(key=lambda d: d['net'], reverse=True)
        scan_data['deals'] = deals
        if 'stats' in scan_data and isinstance(scan_data['stats'], dict):
            scan_data['stats']['arb_found'] = len(deals)

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
                    if tids: o['token_id'] = tids[0]; token_ids.append(tids[0])
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

        # Phase 1: Fetch up to 1000 events per platform
        t_poly = time.time()
        poly_events = []
        for offset in [0, 500]:
            try:
                r = requests.get(f"https://gamma-api.polymarket.com/events?closed=false&limit=500&active=true&offset={offset}", timeout=15)
                poly_events.extend(r.json())
            except Exception as e: print(f"[POLY] {e}")
        t_poly = time.time() - t_poly

        t_kalshi = time.time()
        kalshi_events = []
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

        t_sx = time.time()
        sx_markets = []
        sx_fetch_error = None
        sx_http_status = None
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
        sx_ml_hashes = [m['marketHash'] for m in sx_markets if m.get('type') == 226]
        stats['sx_moneyline_count'] = len(sx_ml_hashes)
        stats['poly_neg_risk'] = len(pc)

        # Phase 3: Batch fetch orderbooks
        clob_res = batch_fetch(_fetch_clob, poly_tids)
        kalshi_res = batch_fetch(_fetch_kalshi_ob, kalshi_tks)
        sx_res = batch_fetch(_fetch_sx_orders, sx_ml_hashes)
        
        stats['clob_fetched'] = sum(1 for v in clob_res.values() if v[0] is not None)
        stats['kalshi_ob_fetched'] = sum(1 for v in kalshi_res.values() if v[0] is not None)

        # Phase 4: Evaluate
        all_deals = eval_poly(pc, clob_res) + eval_kalshi(kc, kalshi_res) + eval_sx(sx_markets, sx_res)
        
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
        # Cache REST clob snapshot for WS-driven re-eval fallback
        with poly_clob_cache_lock:
            poly_clob_cache.clear(); poly_clob_cache.update(clob_res)
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
            ml_hashes = [m['marketHash'] for m in batch if m.get('type') == 226]
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
                ml_hashes = [m['marketHash'] for m in pool if m.get('type') == 226]
                sx_res = batch_fetch(_fetch_sx_orders, ml_hashes)
                _merge_platform_deals(eval_sx(pool, sx_res), 'SX Bet')
        except Exception as e:
            print(f"[SX MICRO] Error: {e}")
        time.sleep(SX_MICRO_INTERVAL)

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

if __name__ == '__main__':
    # Start Polymarket WS client (idle until first scan populates pools)
    ws_client = PolyMarketWS(on_update=on_ws_update, max_subs=MAX_WS_SUBS, verbose=True)
    ws_client.start()

    threading.Thread(target=scan_loop, daemon=True).start()
    threading.Thread(target=kalshi_micro_loop, daemon=True).start()
    threading.Thread(target=sx_micro_loop, daemon=True).start()
    threading.Thread(target=poly_micro_fallback_loop, daemon=True).start()

    print("=" * 60)
    print("  ARBITRAGE RADAR v7 — http://localhost:5050")
    print("  Poly (300) + Kalshi (200) + SX Bet (200) = 700 events (REST main)")
    print(f"  HOT/NEAR pools (buffer={NEAR_BUFFER:.2f})")
    print(f"  Polymarket WS: max {MAX_WS_SUBS} subs, ping every 10s")
    print(f"  Kalshi REST micro: every {KALSHI_MICRO_INTERVAL}s on HOT+NEAR")
    print(f"  SX Bet REST micro: every {SX_MICRO_INTERVAL}s on HOT+NEAR (live sport)")
    print("============================================================")
    app.run(host='0.0.0.0', port=5050, debug=False)
