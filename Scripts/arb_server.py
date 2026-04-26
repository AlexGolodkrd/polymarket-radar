"""
Arbitrage Radar v6.1 — 3 platforms (Poly, Kalshi, SX Bet).
Main scan: 300 Poly + 200 Kalshi + 200 SX Bet (fast ~35s)
Pause scan: extra pages
Micro-scan: re-check top candidates every 5s
"""
import sys, io, os, json, re, time, threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from flask import Flask, jsonify, send_file
import requests

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
THRESH_POLY  = 0.985   # 98.5c threshold
THRESH_KALSHI = 0.985
THRESH_SX    = 0.985
SCAN_INTERVAL = 90
MICRO_INTERVAL = 5
MAX_WORKERS = 80
TIMEOUT = 5

DEADLINE_RE = re.compile(
    r'\b(by|before)\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|'
    r'january|february|march|april|june|july|august|september|october|november|december|'
    r'20\d{2}|end of|q[1-4])', re.IGNORECASE)

HEADERS = {"Accept": "application/json"}

# ── State ───────────────────────────────────────────────────────
scan_data = {"last_scan": None, "scanning": False, "deals": [], "quarantine": [], "stats": {}, "error": None}
whitelist = set()
blacklist = set()
scan_lock = threading.Lock()
candidates_global = {"poly": [], "kalshi": [], "sx": []}
cand_lock = threading.Lock()

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

# ── Filter Candidates ──────────────────────────────
def filter_poly(events):
    candidates = []; token_ids = []
    for ev in events:
        title = ev.get('title', '?')
        if title in blacklist: continue

        # 10-day filter
        end_date = ev.get('endDateIso') or ev.get('endDate')
        if not is_within_10_days(date_str=end_date): continue
        
        is_quarantine = False

        markets = ev.get('markets', [])
        if len(markets) < 2: continue
        if not all(m.get('negRisk') is True for m in markets): continue
        rough = []
        for m in markets:
            ps = m.get('outcomePrices')
            if not ps: continue
            try: p = float(json.loads(ps)[0])
            except: continue
            if p <= 0 or p >= 1: continue
            rough.append({'m': m, 'implied': p})
        if len(rough) < 2: continue
        if sum(o['implied'] for o in rough) >= 0.99: continue
        names = [o['m'].get('question', o['m'].get('groupItemTitle','?')) for o in rough]
        if is_deadline(names): continue
        for o in rough:
            tids_str = o['m'].get('clobTokenIds')
            if tids_str:
                try:
                    tids = json.loads(tids_str)
                    if tids: o['token_id'] = tids[0]; token_ids.append(tids[0])
                except: pass
        candidates.append((ev, rough, is_quarantine))
    return candidates, token_ids

def filter_kalshi(events):
    candidates = []; tickers = []
    for ev in events:
        markets = ev.get('markets', [])
        if len(markets) < 2: continue
        
        # 10-day filter
        close_time = markets[0].get('close_time') or markets[0].get('expected_expiration_time')
        if not is_within_10_days(date_str=close_time): continue

        names = [m.get('title', m.get('ticker','?')) for m in markets]
        if is_deadline(names): continue
        ev_tickers = []
        for m in markets:
            t = m.get('ticker')
            if t: ev_tickers.append(t); tickers.append(t)
        if len(ev_tickers) >= 2:
            candidates.append((ev, ev_tickers))
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
        try:
            r = requests.get("https://api.sx.bet/markets/active?onlyMainLine=true&pageSize=200", timeout=15)
            data = r.json()
            if data.get('status') == 'success':
                sx_markets.extend(data.get('data', {}).get('markets', []))
                next_key = data.get('data', {}).get('nextKey')
                for _ in range(4):
                    if not next_key: break
                    r = requests.get(f"https://api.sx.bet/markets/active?onlyMainLine=true&pageSize=200&paginationKey={next_key}", timeout=15)
                    data = r.json()
                    if data.get('status') == 'success':
                        sx_markets.extend(data.get('data', {}).get('markets', []))
                        next_key = data.get('data', {}).get('nextKey')
        except Exception as e: print(f"[SX] {e}")
        t_sx = time.time() - t_sx

        stats['poly_events'] = len(poly_events)
        stats['kalshi_events'] = len(kalshi_events)
        stats['sx_markets'] = len(sx_markets)
        print(f"[FETCH] Poly={len(poly_events)} ({t_poly:.1f}s) Kalshi={len(kalshi_events)} ({t_kalshi:.1f}s) SX={len(sx_markets)} ({t_sx:.1f}s)")

        # Phase 2: Filter
        pc, poly_tids = filter_poly(poly_events)
        kc, kalshi_tks = filter_kalshi(kalshi_events)
        sx_ml_hashes = [m['marketHash'] for m in sx_markets if m.get('type') == 226]
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

        # Save candidates for micro-scan
        with cand_lock:
            candidates_global['poly'] = pc
            candidates_global['kalshi'] = kc
            candidates_global['sx'] = sx_markets

        elapsed = time.time() - t0
        print(f"[MAIN] Done in {elapsed:.1f}s — {stats['arb_found']} arb found, {stats['quarantine_count']} in quarantine")
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
        r = requests.get("https://api.sx.bet/markets/active?onlyMainLine=true&pageSize=200", timeout=10)
        data = r.json()
        next_key = data.get('data', {}).get('nextKey') if data.get('status') == 'success' else None
        pages = 0
        while next_key and pages < 3:
            r = requests.get(f"https://api.sx.bet/markets/active?onlyMainLine=true&pageSize=200&paginationKey={next_key}", timeout=10)
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

# ── Micro Scanner ───────────────────────────────────────────────
def micro_scan_loop():
    time.sleep(15)
    while True:
        try:
            with scan_lock:
                if scan_data['scanning']:
                    time.sleep(MICRO_INTERVAL); continue
            with cand_lock:
                pc = candidates_global['poly']
                kc = candidates_global['kalshi']
                sx = candidates_global['sx']
            
            tids = [o.get('token_id') for _,rough in pc for o in rough if o.get('token_id')]
            tks = [t for _,tickers in kc for t in tickers]
            ml_hashes = [m['marketHash'] for m in sx if m.get('type') == 226]

            clob = batch_fetch(_fetch_clob, tids)
            k_res = batch_fetch(_fetch_kalshi_ob, tks)
            sx_res = batch_fetch(_fetch_sx_orders, ml_hashes)

            all_deals = eval_poly(pc, clob) + eval_kalshi(kc, k_res) + eval_sx(sx, sx_res)
            
            deals = [d for d in all_deals if not d.get('is_quarantine')]
            deals.sort(key=lambda d: d['net'], reverse=True)
            
            quarantine = [d for d in all_deals if d.get('is_quarantine')]
            quarantine.sort(key=lambda d: d['net'], reverse=True)

            with scan_lock:
                scan_data['deals'] = deals
                scan_data['quarantine'] = quarantine
                scan_data['stats']['arb_found'] = len(deals)
                scan_data['stats']['quarantine_count'] = len(quarantine)
        except Exception as e:
            print(f"[MICRO] Error: {e}")
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
    with scan_lock: return jsonify(scan_data)

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
    threading.Thread(target=scan_loop, daemon=True).start()
    threading.Thread(target=micro_scan_loop, daemon=True).start()
    print("=" * 60)
    print("  ARBITRAGE RADAR v6.1 — http://localhost:5050")
    print("  Poly (300) + Kalshi (200) + SX Bet (200) = 700 events")
    print("============================================================")
    app.run(host='0.0.0.0', port=5050, debug=False)
