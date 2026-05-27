/* plan-kapkan radar dashboard JS.
 *
 * Extracted from Scripts/dashboard.html in audit-28-dashboard-split
 * (27.05.2026). Same JS content; served as a static file so the
 * browser can cache it independently of HTML changes.
 *
 * Referenced by the HTML via:
 *     <script src="/static/dashboard.js"></script>
 *
 * Module-scoped (no IIFE wrap) — the legacy code uses many globals
 * that interact across the file; wrapping would change behavior.
 * If you ever modernise this to ES-modules: each `function fooBar()`
 * at top level becomes an `export function`; `renderXyz()` calls
 * across the file become imports. Today, all globals live on
 * window.
 */
// Same-origin: requests go to whatever host serves dashboard.html.
// Works locally (Flask :5050), on the VPS (https://kapkan.4frdm.live
// proxied to 127.0.0.1:5050 via nginx), and behind any future CDN.
// Hardcoded http://localhost:5050 used to break mixed-content + cross-origin
// when opened over HTTPS.
const API = '';
let pollTimer = null;
let lastScan = null;
const expandedSet = new Set(); // tracks expanded titles
let seenAlerts = new Set();
let currentTab = 'deals';
let alertsOpen = false;

function toggleAlerts() {
  const dd = document.getElementById('alertsDropdown');
  alertsOpen = !alertsOpen;
  dd.style.display = alertsOpen ? 'block' : 'none';
  if(alertsOpen) {
    document.getElementById('bellBadge').style.display = 'none';
  }
}

function switchTab(tab) {
  document.getElementById('tab-deals').style.display = tab === 'deals' ? 'block' : 'none';
  document.getElementById('tab-near').style.display = tab === 'near' ? 'block' : 'none';
  document.getElementById('tab-analytics').style.display = tab === 'analytics' ? 'block' : 'none';

  document.querySelectorAll('.nav-link').forEach(el => el.classList.remove('active'));
  document.getElementById('nav-' + tab).classList.add('active');

  currentTab = tab;
  if(tab === 'analytics') {
    const active = document.querySelector('#periodSwitch .period-btn.active');
    loadAnalytics(active ? active.dataset.period : 'day');
  }
  if(tab === 'near') {
    loadNear();
  }
}

let nearTimer = null;
// Wrapper for the manual "Обновить" button — shows a spinner +
// disabled state so the user knows the fetch is in flight.
async function loadNearWithSpinner() {
  const btn = document.getElementById('nearRefreshBtn');
  const icon = document.getElementById('nearRefreshIcon');
  if (btn && icon) {
    btn.disabled = true;
    btn.style.opacity = '0.6';
    icon.textContent = '⏳';
    icon.style.display = 'inline-block';
    icon.style.animation = 'nearSpin 0.9s linear infinite';
  }
  try {
    await loadNear();
  } finally {
    if (btn && icon) {
      btn.disabled = false;
      btn.style.opacity = '1';
      icon.textContent = '🔄';
      icon.style.animation = 'none';
    }
  }
}

// Inject keyframes once
(function injectNearSpin(){
  if (document.getElementById('nearSpinKeyframes')) return;
  const s = document.createElement('style');
  s.id = 'nearSpinKeyframes';
  s.textContent = '@keyframes nearSpin { from {transform:rotate(0deg)} to {transform:rotate(360deg)} }';
  document.head.appendChild(s);
})();

async function loadNear() {
  try {
    const r = await fetch(`${API}/api/near`);
    const d = await r.json();
    // nearBufferLabel was removed from the DOM but the JS still referenced
    // it — that threw TypeError on null.textContent and aborted loadNear()
    // before any rows were rendered. Defensive null-check.
    const nbl = document.getElementById('nearBufferLabel');
    if (nbl) nbl.textContent = `[порог; +${d.buffer_cents}¢)`;
    const tb = document.querySelector('#nearTable tbody');
    tb.innerHTML = '';
    if (!d.items || !d.items.length) {
      tb.innerHTML = '<tr><td colspan="11" style="color:var(--text3);text-align:center;padding:20px">Сейчас в NEAR пусто — ни один кандидат не близок к порогу.</td></tr>';
    } else {
      const nearStructMap = {
        'all_yes':     {label:'A',  bg:'var(--green-bg)', color:'var(--green)', tip:'ALL YES — Σ YES asks < threshold'},
        'all_no':      {label:'B',  bg:'var(--gold-bg)',  color:'var(--gold)',  tip:'ALL NO — Σ NO asks < (N-1) · threshold'},
        'yes_no_pair': {label:'C',  bg:'var(--bg4)',      color:'var(--text)',  tip:'YES+NO per market — yes+no < threshold'},
        'binary':      {label:'◑',  bg:'var(--bg4)',      color:'var(--text2)', tip:'Binary market'},
      };
      // Format end_date as compact "29 Apr 23:30" with relative-day color hint.
      // Returns {text, color, title}.
      function fmtEnd(iso) {
        if (!iso) return {text:'—', color:'var(--text3)', title:''};
        const dt = new Date(iso);
        if (isNaN(dt.getTime())) return {text:'—', color:'var(--text3)', title:''};
        const now = new Date();
        const ms = dt - now;
        const hr = ms / 3600000;
        const days = ms / 86400000;
        let color = 'var(--text2)';
        if (ms < 0) color = 'var(--text3)';                   // already past
        else if (hr < 24) color = 'var(--green)';             // <1 day
        else if (days < 3) color = 'var(--gold)';             // <3 days
        const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
        const text = `${dt.getUTCDate()} ${months[dt.getUTCMonth()]} ${String(dt.getUTCHours()).padStart(2,'0')}:${String(dt.getUTCMinutes()).padStart(2,'0')}`;
        const title = dt.toISOString().replace('T',' ').slice(0,19) + ' UTC';
        return {text, color, title};
      }
      // Phase 19v20 (05.05.2026) — defensive numeric coercion + escape
      // for ALL server-controlled fields in NEAR table. Earlier v16 fix
      // applied the same hardening to the deal cards but missed this
      // path. If backend sends non-numeric `distance_cents` or
      // `min_liquidity` (server bug or schema drift), `<` comparisons
      // and `.toLocaleString()` either return NaN or attempt method on
      // string — operator sees garbage values without errors.
      const num = (v) => Number(v) || 0;
      d.items.forEach((it, i) => {
        const distC = num(it.distance_cents);
        const distColor = distC < 1 ? 'var(--green)' : distC < 3 ? 'var(--gold)' : 'var(--text2)';
        const sInfo = nearStructMap[it.arb_structure] || {label:'—', bg:'var(--bg4)', color:'var(--text3)', tip:''};
        const ed = fmtEnd(it.end_date);
        // Phase 9kkk (30.04.2026) — operator request: search_query copy button.
        // search_query = clean parent title without parent==child duplication
        // (set in arb_server.py near_summary). Falls back to plain title.
        const sq = it.search_query || it.title;
        const platStr = String(it.platform || '');
        const platClass = (platStr === 'SX Bet' ? 'SX' : platStr.split('+')[0])
                            .replace(/[^a-zA-Z0-9]/g, '');
        const tr = document.createElement('tr');
        tr.style.borderTop = '1px solid var(--border)';
        tr.innerHTML = `
          <td style="color:var(--text3);padding:8px 12px">${i+1}</td>
          <td><span class="badge platform platform-${platClass}">${escHtml(platStr)}</span></td>
          <td><span class="badge" style="background:${sInfo.bg};color:${sInfo.color}" title="${escHtml(sInfo.tip)}">${escHtml(sInfo.label)}</span></td>
          <td style="max-width:380px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escHtml(it.title)}">
            <button class="copy-btn" data-q="${escHtml(sq)}" title="Скопировать название для поиска на сайте платформы" style="background:none;border:1px solid var(--border);color:var(--text3);cursor:pointer;padding:1px 5px;font-size:10px;border-radius:3px;margin-right:6px;vertical-align:middle">📋</button>
            ${escHtml(it.title)}
          </td>
          <td style="text-align:right;font-weight:bold">${num(it.sum_cents)}¢</td>
          <td style="text-align:right;font-weight:bold;color:${distColor}">+${distC}¢</td>
          <td style="text-align:right;color:var(--text3)">${num(it.threshold_cents)}¢</td>
          <td style="text-align:right">${num(it.outcomes_count)}</td>
          <td style="text-align:right;color:var(--text2)">${num(it.min_price_cents)}¢</td>
          <td style="text-align:right;color:var(--text2)" title="Min liquidity (USDC) — потолок размера ставки на лучшем аске самой узкой ноги">$${fmtCompact(num(it.min_liquidity))}</td>
          <td style="text-align:right;color:${ed.color};padding:8px 12px;font-family:ui-monospace,monospace;font-size:11px" title="${escHtml(ed.title)}">${escHtml(ed.text)}</td>
        `;
        tb.appendChild(tr);
      });
      // Phase 9kkk — wire copy buttons (delegated handler for the whole table)
      tb.querySelectorAll('.copy-btn').forEach(btn => {
        btn.addEventListener('click', async (e) => {
          e.stopPropagation();
          const q = btn.dataset.q || '';
          try {
            await navigator.clipboard.writeText(q);
            const orig = btn.textContent;
            btn.textContent = '✓';
            btn.style.color = 'var(--green)';
            setTimeout(() => {
              btn.textContent = orig;
              btn.style.color = 'var(--text3)';
            }, 1200);
          } catch (err) {
            // Fallback for older browsers / non-https
            const ta = document.createElement('textarea');
            ta.value = q;
            ta.style.position = 'fixed';
            ta.style.opacity = '0';
            document.body.appendChild(ta);
            ta.select();
            try { document.execCommand('copy'); } catch (e) {}
            document.body.removeChild(ta);
            btn.textContent = '✓';
            setTimeout(() => { btn.textContent = '📋'; }, 1200);
          }
        });
      });
    }
    // Update badge counter on the nav link
    const badge = document.getElementById('nearBadge');
    if (d.count > 0) {
      badge.textContent = d.count;
      badge.style.display = 'inline-block';
    } else {
      badge.style.display = 'none';
    }
  } catch(e) { console.error('near load failed', e); }
  if (nearTimer) clearTimeout(nearTimer);
  if (currentTab === 'near') {
    nearTimer = setTimeout(loadNear, 5000);  // refresh every 5s while open
  }
}

let analyticsTimer = null;
async function loadAnalytics(period) {
  document.querySelectorAll('#periodSwitch .period-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.period === period));
  try {
    const r = await fetch(`${API}/api/analytics?period=${period}`);
    const d = await r.json();
    renderAnalytics(d);
  } catch(e) {
    console.error('analytics load failed', e);
  }
  // Auto-refresh while user stays on Analytics tab
  if (analyticsTimer) clearTimeout(analyticsTimer);
  if (currentTab === 'analytics') {
    analyticsTimer = setTimeout(() => loadAnalytics(period), 15000);
  }
}

// Structure label dictionary used in multiple Analytics tables
const _STRUCT_LABEL = {
  'all_yes':       'A · ALL YES',
  'all_no':        'B · ALL NO',
  'yes_no_pair':   'C · YES+NO',
  'binary':        '◑ binary',
  'cross_platform':'⇆ Cross-Platform',
};

async function renderAnalytics(d) {
  // Phase 19v20 (05.05.2026) — defensive Number() coercion before
  // .toFixed(). Old `(sim.net_total ?? 0).toFixed(2)` crashed with
  // `TypeError: Cannot read .toFixed of null` if backend returned
  // `sim_net: null` (or string). Outer try/catch in loadAnalytics
  // swallowed it → analytics tab silently froze on stale data.
  const num = (v) => Number(v) || 0;
  const sim = d.sim || {}, open = d.currently_open || {}, filled = d.filled || {};
  // Phase audit-15 (15.05.2026) — REAL entered-trade counter.
  const aFilledEl = document.getElementById('aFilledCount');
  if (aFilledEl) {
    const f = num(filled.count);
    const u = num(filled.unique_count);
    aFilledEl.textContent = f + (u && u !== f ? ' (' + u + ' уник)' : '');
  }
  document.getElementById('aSimNet').textContent  = '$' + num(sim.net_total).toFixed(2);
  document.getElementById('aSeenCount').textContent = num(sim.count);
  // Phase audit-2 (12.05.2026) — distinct-fixtures stat. Format
  // "<unique> / <ratio>" e.g. "2 / 0.013" tells operator at a glance
  // that 229 opens were really just ~2 fixtures cycling.
  const uniqEl = document.getElementById('aUniqueCount');
  if (uniqEl) {
    const u = num(sim.unique_count);
    const r = sim.unique_ratio;
    uniqEl.textContent = u + (r !== null && r !== undefined
      ? ' (' + (Number(r) * 100).toFixed(1) + '%)' : '');
  }
  document.getElementById('aAvgNet').textContent = '$' + num(sim.avg_net).toFixed(2);
  document.getElementById('aClosedCount').textContent = num(d.closed_count);
  // Phase audit-4 (15.05.2026 PM) — "Сейчас открыто" now reads from
  // /api/portfolio_positions.open.count (real filled positions with
  // end_date in the future), not from sim _open_deals. Old text was
  // "0 (sim $0.00)" while operator held 5 fired positions — wrong source.
  // Displayed value updated below inside loadPositionsPanel().
  document.getElementById('aOpen').textContent = '…';

  // Phase audit-18 (15.05.2026) + audit-4 (PM) — current+resolved positions.
  // Backend split into {open, resolved}. Open table shows live positions;
  // resolved table folds below with per-row Real P&L computed in
  // updateRealPnl() from platform resolution lookups.
  try {
    const pr = await fetch(`${API}/api/portfolio_positions`, { credentials: 'include' });
    if (pr.ok) {
      const pdata = await pr.json();
      const openData     = pdata.open     || { count: 0, positions: [], total_cost_usdc: 0 };
      const resolvedData = pdata.resolved || { count: 0, positions: [], total_cost_usdc: 0 };

      // ── "Сейчас открыто" top-stat ────────────────────────────
      document.getElementById('aOpen').textContent =
        num(openData.count) + ' ($' + num(openData.total_cost_usdc).toFixed(2) + ')';

      // ── Open positions table ──────────────────────────────────
      const summaryEl = document.getElementById('aPositionsSummary');
      const tbody = document.querySelector('#aPositions tbody');
      if (summaryEl) {
        summaryEl.textContent = `${num(openData.count)} позиций · общий stake $${num(openData.total_cost_usdc).toFixed(2)}`;
      }
      if (tbody) {
        tbody.innerHTML = '';
        for (const p of (openData.positions || [])) {
          const tr = document.createElement('tr');
          tr.style.borderTop = '1px solid var(--border)';
          tr.innerHTML = `
            <td style="padding:4px 8px;max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escHtml(p.title||'')}">${escHtml(p.title||'')}</td>
            <td style="padding:4px 8px"><span class="badge platform platform-${escHtml(p.platform||'')}" style="font-size:10px">${escHtml(p.platform||'?')}</span></td>
            <td style="padding:4px 8px;color:var(--text2)">${escHtml(p.side||'?')}</td>
            <td style="padding:4px 8px;text-align:right">${p.contracts !== null ? Number(p.contracts).toFixed(2) : '—'}</td>
            <td style="padding:4px 8px;text-align:right">${p.avg_fill_price !== null ? (Number(p.avg_fill_price) * 100).toFixed(1) + '¢' : '—'}</td>
            <td style="padding:4px 8px;text-align:right;color:var(--green)">$${Number(p.total_size_usdc).toFixed(2)}</td>
            <td style="padding:4px 8px;text-align:right;color:var(--text3)">${num(p.fire_count)}</td>
          `;
          tbody.appendChild(tr);
        }
        if ((openData.positions || []).length === 0) {
          tbody.innerHTML = '<tr><td colspan="7" style="padding:12px;color:var(--text3);text-align:center">Нет открытых позиций</td></tr>';
        }
      }

      // ── Resolved positions table + Real P&L lookup ────────────
      const resolvedSumEl = document.getElementById('aResolvedSummary');
      const resolvedTbody = document.querySelector('#aResolved tbody');
      if (resolvedSumEl) {
        resolvedSumEl.textContent = `${num(resolvedData.count)} позиций · общий stake $${num(resolvedData.total_cost_usdc).toFixed(2)}`;
      }
      if (resolvedTbody) {
        resolvedTbody.innerHTML = '';
        for (const p of (resolvedData.positions || [])) {
          const tr = document.createElement('tr');
          tr.style.borderTop = '1px solid var(--border)';
          tr.dataset.posKey = `${p.platform}::${p.title}::${p.side}`;
          tr.innerHTML = `
            <td style="padding:4px 8px;max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escHtml(p.title||'')}">${escHtml(p.title||'')}</td>
            <td style="padding:4px 8px"><span class="badge platform platform-${escHtml(p.platform||'')}" style="font-size:10px">${escHtml(p.platform||'?')}</span></td>
            <td style="padding:4px 8px;color:var(--text2)">${escHtml(p.side||'?')}</td>
            <td style="padding:4px 8px;text-align:right">${p.contracts !== null ? Number(p.contracts).toFixed(2) : '—'}</td>
            <td style="padding:4px 8px;text-align:right">${p.avg_fill_price !== null ? (Number(p.avg_fill_price) * 100).toFixed(1) + '¢' : '—'}</td>
            <td style="padding:4px 8px;text-align:right;color:var(--green)">$${Number(p.total_size_usdc).toFixed(2)}</td>
            <td class="real-pnl-cell" style="padding:4px 8px;text-align:right;color:var(--text3)">…</td>
          `;
          resolvedTbody.appendChild(tr);
        }
        if ((resolvedData.positions || []).length === 0) {
          resolvedTbody.innerHTML = '<tr><td colspan="7" style="padding:12px;color:var(--text3);text-align:center">Нет закрытых позиций</td></tr>';
        }
      }
      // Kick off Real P&L lookups (async, doesn't block UI render).
      updateRealPnl(resolvedData.positions || []).catch(e => console.warn('real-pnl failed', e));
    }
  } catch (e) {
    console.warn('positions panel load failed', e);
  }

  // Phase audit-2 (12.05.2026) — per-platform/per-structure tables get
  // a "Sim count / unique" column so operator can see "Limitless+SX: 222
  // opens / 2 unique" instead of mentally subtracting.
  const tbP = document.querySelector('#aByPlatform tbody');
  tbP.innerHTML = '';
  Object.entries(d.by_platform || {}).sort((a,b)=>num(b[1].sim_net) - num(a[1].sim_net)).forEach(([p, s]) => {
    const sn = num(s.sim_net);
    const cnt = num(s.sim_count);
    const uq = num(s.unique_count);
    const countCell = uq && uq !== cnt
      ? `${cnt} <span style="color:var(--text3)">/ ${uq} uniq</span>`
      : `${cnt}`;
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${escHtml(p)}</td><td style="text-align:right">${countCell}</td><td style="text-align:right;color:${sn>=0?'var(--green)':'var(--red)'}">$${sn.toFixed(2)}</td>`;
    tbP.appendChild(tr);
  });
  if (!Object.keys(d.by_platform || {}).length) {
    tbP.innerHTML = '<tr><td colspan="3" style="color:var(--text3);text-align:center;padding:12px">Нет данных за период</td></tr>';
  }

  const tbS = document.querySelector('#aByStructure tbody');
  tbS.innerHTML = '';
  Object.entries(d.by_structure || {}).sort((a,b)=>num(b[1].sim_net) - num(a[1].sim_net)).forEach(([s, st]) => {
    const sn = num(st.sim_net);
    const cnt = num(st.sim_count);
    const uq = num(st.unique_count);
    const countCell = uq && uq !== cnt
      ? `${cnt} <span style="color:var(--text3)">/ ${uq} uniq</span>`
      : `${cnt}`;
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${escHtml(_STRUCT_LABEL[s] || s)}</td><td style="text-align:right">${countCell}</td><td style="text-align:right;color:${sn>=0?'var(--green)':'var(--red)'}">$${sn.toFixed(2)}</td>`;
    tbS.appendChild(tr);
  });
  if (!Object.keys(d.by_structure || {}).length) {
    tbS.innerHTML = '<tr><td colspan="3" style="color:var(--text3);text-align:center;padding:12px">Нет данных</td></tr>';
  }

  const tbT = document.querySelector('#aTop5 tbody');
  tbT.innerHTML = '';
  (d.top5_by_sim_net || []).forEach((row, i) => {
    const tr = document.createElement('tr');
    const sLabel = _STRUCT_LABEL[row.arb_structure] || row.arb_structure || '—';
    tr.innerHTML = `<td style="color:var(--text3)">${i+1}</td><td>${escHtml(row.platform || '?')}</td><td>${sLabel}</td><td style="max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escHtml(row.title || '')}">${escHtml((row.title||'').slice(0,55))}</td><td style="text-align:right;color:${row.net>=0?'var(--green)':'var(--red)'}">$${row.net.toFixed(2)}</td>`;
    tbT.appendChild(tr);
  });
  if (!(d.top5_by_sim_net || []).length) {
    tbT.innerHTML = '<tr><td colspan="5" style="color:var(--text3);text-align:center;padding:12px">Нет сделок</td></tr>';
  }

  // Trigger history reload (uses same period as aggregate)
  loadHistory(0);
}

// ── Phase audit-4 (15.05.2026 PM) — client-side Real P&L lookup ────
//
// For each resolved position, fetch the source market's resolution
// state from the originating platform's public API. The fetch goes
// FROM THE BROWSER (operator's IP), not from the VPS, to dodge the
// Cloudflare bans the VPS picks up. Results are cached in localStorage
// for 1h so a dashboard reload doesn't hammer the APIs.
//
// Why client-side: Limitless/SX/Polymarket gateway endpoints don't
// universally support CORS for unauthenticated read. Most do (we test
// at runtime); ones that don't show '—' Real P&L gracefully. No backend
// proxy required; the operator wanted this in PR plan.
//
// Each platform has its own resolver — see _resolveLimitless,
// _resolveSX, _resolvePolymarket below.

const _RESOLUTION_TTL_MS = 60 * 60 * 1000; // 1h
const _RESOLUTION_CACHE_KEY = 'kapkan_resolutions_v1';

function _readResolutionCache() {
  try {
    const raw = localStorage.getItem(_RESOLUTION_CACHE_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch (e) { return {}; }
}
function _writeResolutionCache(cache) {
  try { localStorage.setItem(_RESOLUTION_CACHE_KEY, JSON.stringify(cache)); }
  catch (e) { /* quota; ignore */ }
}
function _cacheGet(cache, key) {
  const e = cache[key];
  if (!e) return null;
  if (Date.now() - (e.ts || 0) > _RESOLUTION_TTL_MS) return null;
  return e.value;
}
function _cacheSet(cache, key, value) {
  cache[key] = { ts: Date.now(), value };
}

// ── Per-platform resolvers ─────────────────────────────────────────
// Each returns { resolved: bool, winning_side: <string or null> }.
// `winning_side` is normalised to UPPERCASE ('YES'/'NO'/'OUTCOME_1'/
// 'OUTCOME_2') so the PnL calc can compare to position.side directly.

async function _resolveLimitless(slug) {
  if (!slug) return { resolved: false, winning_side: null };
  try {
    const r = await fetch(`https://api.limitless.exchange/markets/${encodeURIComponent(slug)}`,
                          { headers: { 'Accept': 'application/json' } });
    if (!r.ok) return { resolved: false, winning_side: null };
    const m = await r.json();
    // Limitless market response shape (verified 15.05.2026 probe):
    //   status: 'FUNDED' | 'RESOLVED' | ...
    //   resolution: { winningOutcome: 'YES'|'NO' } when resolved
    // Older shape uses `winningOutcomeIndex` (0=YES, 1=NO).
    const status = (m.status || '').toUpperCase();
    if (status !== 'RESOLVED' && !m.resolved) {
      return { resolved: false, winning_side: null };
    }
    let side = null;
    if (m.resolution && m.resolution.winningOutcome) {
      side = String(m.resolution.winningOutcome).toUpperCase();
    } else if (m.winningOutcomeIndex === 0) side = 'YES';
    else if (m.winningOutcomeIndex === 1) side = 'NO';
    return { resolved: true, winning_side: side };
  } catch (e) {
    return { resolved: false, winning_side: null, error: String(e) };
  }
}

async function _resolveSX(marketHash) {
  if (!marketHash) return { resolved: false, winning_side: null };
  try {
    const r = await fetch(`https://api.sx.bet/markets/find?marketHashes=${encodeURIComponent(marketHash)}`,
                          { headers: { 'Accept': 'application/json' } });
    if (!r.ok) return { resolved: false, winning_side: null };
    const data = await r.json();
    const arr = (data && data.data) || [];
    if (!arr.length) return { resolved: false, winning_side: null };
    const m = arr[0];
    // SX market.status: 1=open, 2=closed (game ended, not yet settled),
    // 3=settled, 4=resolved (cancelled/refunded). Settlement winner
    // exposed as `outcome` (1 or 2) when status>=3.
    const status = Number(m.status || 0);
    if (status < 3 || !m.outcome) return { resolved: false, winning_side: null };
    return { resolved: true, winning_side: 'OUTCOME_' + Number(m.outcome) };
  } catch (e) {
    return { resolved: false, winning_side: null, error: String(e) };
  }
}

async function _resolvePolymarket(conditionId) {
  if (!conditionId) return { resolved: false, winning_side: null };
  try {
    const r = await fetch(`https://clob.polymarket.com/markets/${encodeURIComponent(conditionId)}`,
                          { headers: { 'Accept': 'application/json' } });
    if (!r.ok) return { resolved: false, winning_side: null };
    const m = await r.json();
    if (!m.closed) return { resolved: false, winning_side: null };
    // Polymarket V2: tokens[].winner === true on the winning side
    // (verified post-31.03.2026 spec). YES/NO label in tokens[].outcome.
    const tokens = m.tokens || [];
    const winning = tokens.find(t => t.winner === true);
    if (!winning) return { resolved: false, winning_side: null };
    return { resolved: true, winning_side: String(winning.outcome || '').toUpperCase() };
  } catch (e) {
    return { resolved: false, winning_side: null, error: String(e) };
  }
}

async function _resolvePosition(position, cache) {
  const platform = (position.platform || '').toLowerCase();
  const ids = position.ids || {};
  let cacheKey, fetcher;
  if (platform === 'limitless' && ids.slug) {
    cacheKey = `limitless::${ids.slug}`;
    fetcher = () => _resolveLimitless(ids.slug);
  } else if ((platform === 'sx_bet' || platform === 'sx') && ids.market_hash) {
    cacheKey = `sx::${ids.market_hash}`;
    fetcher = () => _resolveSX(ids.market_hash);
  } else if (platform === 'polymarket' && ids.condition_id) {
    cacheKey = `polymarket::${ids.condition_id}`;
    fetcher = () => _resolvePolymarket(ids.condition_id);
  } else {
    return { resolved: false, winning_side: null, source: 'no_identifier' };
  }
  const cached = _cacheGet(cache, cacheKey);
  if (cached) return cached;
  const res = await fetcher();
  if (res && res.resolved) _cacheSet(cache, cacheKey, res);
  return res;
}

// Compute Real P&L for ONE position. Returns a number (USD) or null if
// resolution couldn't be determined yet (unresolved / lookup failed).
//
// Formula:
//   WIN  → payout = contracts * 1 - winning_fee_estimate
//          pnl = payout - stake
//   LOSS → pnl = -stake
//
// Winning_fee_estimate per platform:
//   SX Bet: 2% taken from winnings (= 0.02 * contracts)
//   Limitless: effectiveFeeBps observed live ≈ 0% (promo); use 0
//   Polymarket: 0 on payout (fee already in fill price)
function _computeRealPnl(position, resolution) {
  if (!resolution || !resolution.resolved) return null;
  const stake = Number(position.total_size_usdc || 0);
  const contracts = Number(position.contracts || 0);
  if (!stake || !contracts) return null;
  const winning = (resolution.winning_side || '').toUpperCase();
  const ourSide = (position.side || '').toUpperCase();
  // Tolerate slight label variation: 'OUTCOME_1' vs '1' etc.
  const won = winning && (
    winning === ourSide
    || winning === ourSide.replace('OUTCOME_', '')
    || ('OUTCOME_' + winning) === ourSide
  );
  if (!won) return -stake;
  let winning_fee = 0;
  const platform = (position.platform || '').toLowerCase();
  if (platform === 'sx_bet' || platform === 'sx') {
    winning_fee = 0.02 * contracts; // 2% of $1-payout per contract
  }
  // Limitless/Polymarket — already accounted for via fill price.
  const payout = contracts - winning_fee;
  return payout - stake;
}

async function updateRealPnl(resolvedPositions) {
  const cache = _readResolutionCache();
  let total = 0;
  let resolved_count = 0;
  let pending = false;
  // Iterate sequentially to keep total fetch rate friendly to platform
  // APIs (and to make any single CORS failure visible per row rather
  // than burst-fail all at once).
  for (const pos of resolvedPositions) {
    const tr = document.querySelector(`tr[data-pos-key="${pos.platform}::${pos.title}::${pos.side}"]`);
    const cell = tr && tr.querySelector('.real-pnl-cell');
    try {
      const res = await _resolvePosition(pos, cache);
      const pnl = _computeRealPnl(pos, res);
      if (pnl === null) {
        if (cell) cell.textContent = '—';
        pending = true;
        continue;
      }
      total += pnl;
      resolved_count++;
      if (cell) {
        cell.textContent = (pnl >= 0 ? '+$' : '-$') + Math.abs(pnl).toFixed(2);
        cell.style.color = pnl >= 0 ? 'var(--green)' : 'var(--red)';
      }
    } catch (e) {
      if (cell) cell.textContent = '?';
    }
  }
  _writeResolutionCache(cache);
  const realPnlEl = document.getElementById('aRealPnl');
  if (realPnlEl) {
    const sign = total >= 0 ? '+' : '−';
    const txt = sign + '$' + Math.abs(total).toFixed(2);
    realPnlEl.textContent = txt + (pending ? ' (частично)' : '');
    realPnlEl.style.color = total >= 0 ? 'var(--green)' : 'var(--red)';
  }
}

// История сделок — все 'opened' events с фильтрами и пагинацией
async function loadHistory(offset) {
  window._histOffset = offset || 0;
  const period = document.querySelector('#periodSwitch .period-btn.active')?.dataset?.period || 'all';
  const platform = document.getElementById('aHistPlatform').value;
  const structure = document.getElementById('aHistStructure').value;
  const minNet = document.getElementById('aHistMinNet').value || '0';
  const params = new URLSearchParams({period, limit: '100', offset: String(offset || 0), min_net: minNet});
  if (platform) params.set('platform', platform);
  if (structure) params.set('structure', structure);
  try {
    const r = await fetch(`${API}/api/analytics/history?` + params);
    const d = await r.json();
    renderHistory(d);
  } catch(e) { console.error('history load failed', e); }
}

function renderHistory(d) {
  document.getElementById('aHistoryCount').textContent =
    `— показано ${d.shown}/${d.total} (offset ${d.offset})`;
  const tb = document.querySelector('#aHistory tbody');
  tb.innerHTML = '';
  if (!d.rows || !d.rows.length) {
    tb.innerHTML = '<tr><td colspan="13" style="color:var(--text3);text-align:center;padding:20px">Нет сделок под фильтр</td></tr>';
    return;
  }
  const now = Date.now();
  d.rows.forEach(row => {
    const tr = document.createElement('tr');
    tr.style.borderTop = '1px solid var(--border)';
    const dt = new Date(row.ts * 1000);
    const tsStr = dt.toISOString().replace('T', ' ').slice(0, 19);
    const sLabel = _STRUCT_LABEL[row.arb_structure] || row.arb_structure || '—';
    const dur = row.duration_sec != null
      ? (row.duration_sec >= 60 ? (row.duration_sec/60).toFixed(1) + 'm' : row.duration_sec.toFixed(0) + 's')
      : '—';
    const status = row.status === 'open' ? '<span style="color:var(--green)">● open</span>' : '<span style="color:var(--text3)">closed</span>';
    const netColor = row.net >= 5 ? 'var(--green)' : row.net >= 1 ? 'var(--gold)' : row.net > 0 ? 'var(--text2)' : 'var(--red)';

    // Резолв: дата + сколько дней до. Зелёный ≤3 дня, gold ≤7, text2 >7.
    // Legacy events без end_date показывают '—'.
    let endCell = '<span style="color:var(--text3)">—</span>';
    if (row.end_date) {
      const ed = new Date(row.end_date);
      if (!isNaN(ed.getTime())) {
        const days = (ed.getTime() - now) / 86400000;
        const dateOnly = ed.toISOString().slice(0, 10);
        const daysStr = days >= 0
          ? '+' + (days < 1 ? days.toFixed(1) : days.toFixed(0)) + 'д'
          : 'past ' + Math.abs(days).toFixed(0) + 'д';
        const color = days < 0 ? 'var(--text3)'
                    : days <= 3 ? 'var(--green)'
                    : days <= 7 ? 'var(--gold)'
                    : 'var(--text2)';
        endCell = `<div style="color:${color};font-family:ui-monospace,monospace;font-size:11px">${dateOnly}<br><span style="font-size:10px;opacity:0.8">${daysStr}</span></div>`;
      }
    }

    tr.innerHTML = `
      <td style="padding:5px 8px;color:var(--text3);font-family:ui-monospace,monospace">${tsStr}</td>
      <td style="white-space:nowrap">${endCell}</td>
      <td>${escHtml(row.platform || '?')}</td>
      <td style="white-space:nowrap">${sLabel}</td>
      <td style="max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escHtml(row.title || '')}">${escHtml((row.title||'').slice(0,60))}</td>
      <td style="text-align:right;font-family:ui-monospace,monospace">${row.sum_cents != null ? row.sum_cents + 'c' : '—'}</td>
      <td style="text-align:right;color:var(--text2);font-family:ui-monospace,monospace" title="Стейк: сколько USDC бот зарезервировал на сделку (capped по min_liq + per-trade limit $55)">${row.balance_used != null ? '$' + Number(row.balance_used).toFixed(2) : '—'}</td>
      <td style="text-align:right;font-weight:bold;color:${netColor};font-family:ui-monospace,monospace">$${(row.net || 0).toFixed(2)}</td>
      <td style="text-align:right;color:var(--text2)">${row.roi != null ? row.roi + '%' : '—'}</td>
      <td>${row.grade || '—'}</td>
      <td style="text-align:right;color:var(--text3)" title="Min liquidity (USDC) — наименьшая глубина стакана на лучшем аске среди ног арба. Это потолок: больше этого размера на этой цене купить нельзя. Stake обычно меньше из-за per-trade cap $55 и доли каждой ноги в общем стейке">${row.min_liq != null ? '$' + fmtCompact(row.min_liq) : '—'}</td>
      <td style="text-align:right;color:var(--text3);font-family:ui-monospace,monospace">${dur}</td>
      <td>${status}</td>`;
    tb.appendChild(tr);
  });
}

// Removed 28.04.2026: manual decision flow (decideDeal / Took / Skipped buttons +
// /api/analytics/decision endpoint) replaced by full automation:
//   Phase 2 dry-run executor — auto-fires every HOT deal
//   Phase 5 paper trading — graduation gate on rolling 100 trades
//   Live execution (post-graduation) — bot decides, risk gate disciplines
// Manual track measured human discipline; the bot doesn't need it.

function showToast(msg) {
  const container = document.getElementById('toastContainer');
  const toast = document.createElement('div');
  toast.className = 'toast';
  // Phase 19v20 (05.05.2026) — XSS fix in toast path. `msg` is a
  // server-controlled quarantine title (from `q.title`); old code
  // assigned it via innerHTML after a string concat → a malicious
  // market title containing HTML/JS injected into the toast DOM.
  // Build via DOM nodes + textContent for the user-controlled part.
  const cleaned = String(msg || '').replace("Уязвимость 'Other' (скрыт): ", "");
  const prefix = document.createElement('span');
  prefix.innerHTML = '⚠️ <strong>Фильтр:</strong><br>';  // static, safe
  const body = document.createElement('span');
  body.textContent = cleaned;  // server-controlled — escape via DOM
  toast.appendChild(prefix);
  toast.appendChild(body);
  container.appendChild(toast);
  setTimeout(() => toast.remove(), 5000);
}

// Phase 19v20 (05.05.2026) — single-flight + abortable fetch.
// Old `setInterval(fetchDeals, 3000)` could pile up 2-3 in-flight
// requests when /api/deals took >3s on a heavy scan. Whichever
// finished LAST won, sometimes overwriting fresher data with older.
// Now: previous request is aborted before the new one fires; a
// `_inflight` flag also short-circuits redundant entries when a
// long fetch is already running.
let _dealsAbort = null;
let _dealsInflight = false;
async function fetchDeals() {
  // Skip if tab is hidden — no point in polling for a backgrounded
  // dashboard. visibilitychange listener below triggers an immediate
  // refresh when the user returns.
  if (typeof document !== 'undefined' && document.hidden) return;
  if (_dealsInflight) {
    if (_dealsAbort) {
      try { _dealsAbort.abort(); } catch (_) {}
    }
  }
  _dealsAbort = (typeof AbortController !== 'undefined') ? new AbortController() : null;
  _dealsInflight = true;
  try {
    const opts = _dealsAbort ? { signal: _dealsAbort.signal } : {};
    const r = await fetch(`${API}/api/deals`, opts);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    try {
      updateUI(data);
    } catch (renderErr) {
      console.error('updateUI failed (render bug, network OK):', renderErr);
      setStatus('error', 'UI render error — open DevTools console');
    }
  } catch(e) {
    if (e.name === 'AbortError') return;   // expected on supersede
    console.error('fetch /api/deals failed:', e);
    setStatus('error', 'Сервер недоступен');
  } finally {
    _dealsInflight = false;
    _dealsAbort = null;
  }
}

// Event delegation
document.addEventListener('click', function(e) {
  const header = e.target.closest('.deal-header');
  if (header && !e.target.closest('button')) {
    const card = header.parentElement;
    const key = card.dataset.title;
    if (!key) return;
    card.classList.toggle('expanded');
    if (card.classList.contains('expanded')) {
      expandedSet.add(key);
    } else {
      expandedSet.delete(key);
    }
  }
});

// Phase 9kkk hotfix #3: actionDeal() removed — was a no-op since PR #23
// dropped /api/approve and /api/reject. Quarantine is read-only now;
// is_quarantine flag prevents executor from firing those deals.

// Phase 2: trigger dry-run executor for a single deal (logs to dryrun.jsonl
// + schedules realistic-fill eval). UI is purely informational — the actual
// auto-fire runs after every scan from the backend.
async function dryFireDeal(title, btn) {
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '⏳ firing…';
  try {
    const r = await fetch(`${API}/api/dryfire`, {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({title})
    });
    const j = await r.json();
    if (j.status === 'ok' && j.fired && j.fired.length) {
      const f = j.fired[0];
      btn.innerHTML = f.aborted ? `⚠ ${f.aborted.slice(0,20)}` : `✓ ${f.structure} (${f.leg_count} legs)`;
      btn.style.color = 'var(--green)';
    } else {
      btn.innerHTML = `✗ ${j.reason || 'failed'}`;
      btn.style.color = 'var(--red)';
    }
    setTimeout(() => { btn.innerHTML = orig; btn.style.color=''; btn.disabled=false; }, 3500);
  } catch(e) {
    btn.innerHTML = '✗ network';
    btn.style.color = 'var(--red)';
    setTimeout(() => { btn.innerHTML = orig; btn.style.color=''; btn.disabled=false; }, 3500);
  }
}

// Phase 2: poll paper-trade stats from /api/paper_stats every 10s (graduation
// gate input). Updates the small panel in the header so the user sees rolling
// win rate + drift without opening the Analytics tab.
async function refreshPaperStats() {
  try {
    const r = await fetch(`${API}/api/paper_stats?window=100`);
    const j = await r.json();
    const el = document.getElementById('paperStatsPanel');
    if (!el) return;
    if (!j || j.count === 0) {
      el.innerHTML = '<span style="color:var(--text3)">paper: 0/100 trades</span>';
      return;
    }
    const winColor = (j.win_rate_pct >= 70) ? 'var(--green)' : (j.win_rate_pct >= 50) ? 'var(--gold)' : 'var(--red)';
    const driftPct = j.mean_drift !== null ? (j.mean_drift * 100).toFixed(1) : '—';
    const grad = j.graduation_ready ? '<span style="color:var(--green);font-weight:bold" title="Phase 5 graduation gate ready — flip DRY_RUN=0 to enable real-mode">🎓 ready</span>' : '';
    el.innerHTML = `<span style="color:var(--text3)">paper</span> <span style="color:${winColor};font-weight:bold">${j.win_rate_pct}%</span> <span style="color:var(--text3)">win</span> · <span style="color:var(--text2)">drift ${driftPct}%</span> · <span style="color:var(--text3)">${j.count}/100</span> ${grad}`;
  } catch(e) {}
}
setInterval(refreshPaperStats, 10000);
window.addEventListener('load', refreshPaperStats);

// Phase TS-5d.2 (14.05.2026) — residential proxy state panel.
// Polls /api/ts_metrics (which proxies TS executor's /metrics) every
// 10s and renders the proxy block.
//
// IMPORTANT: residential proxy is configured for ORDER PLACEMENT in
// real-deposit mode (DRY_RUN=0). In dry-run the only traffic through
// the proxy is the keepalive ticker (the actual /order POSTs are
// stubbed to dry-fire logs, not sent over the network). Panel
// stays hidden when no proxy is configured at all.
let _lastProxyState = null;
async function refreshProxyState() {
  try {
    const r = await fetch(`${API}/api/ts_metrics`);
    if (!r.ok) return;
    const j = await r.json();
    const el = document.getElementById('proxyPanel');
    if (!el) return;
    const p = j && j.proxy;
    _lastProxyState = p;
    if (!p || !p.enabled) {
      // No PROXY_URL_* env set on executor — hide panel completely.
      el.style.display = 'none';
      return;
    }
    el.style.display = '';
    const isDryRun = j.dry_run === true;
    const nAgents = (p.agents || []).length;
    const ka = p.keepalive_active;
    const kaSec = p.keepalive_interval_s;

    // Color logic (phase TS-5g.1 — refined to reflect proxy_pool's
    // lazy agent creation). Keepalive ticker starts ONLY when first
    // agent is built via getDispatcher, which happens only on a real
    // order POST. In dry-run, no real POSTs happen, so keepalive
    // stays inactive by design — not an error.
    //  - red:    enabled, NOT dry-run, but keepalive off → real bug
    //  - amber:  dry-run path (proxy ready, just waiting for fires)
    //  - blue:   keepalive standby (real-mode but no agents yet)
    //  - green:  real-mode + keepalive actively pinging agents
    let color, label;
    if (isDryRun) {
      color = 'var(--gold)';
      label = ka
        ? `🛜 ${nAgents}ag · ka ${kaSec}s · dry-run`
        : `🛜 standby · ka ${kaSec}s · dry-run`;
    } else if (!ka) {
      // Real-mode, no keepalive — either first fire pending OR all
      // agents got closed unexpectedly. Surface as warn but blue-ish
      // since "0 agents in real-mode" is the normal startup state too.
      color = nAgents === 0 ? 'var(--blue, #4aa)' : 'var(--red)';
      label = nAgents === 0
        ? `🛜 standby · REAL · awaiting first fire`
        : `⚠️ keepalive OFF · ${nAgents}ag REAL`;
    } else {
      color = 'var(--green)';
      label = `🛜 ${nAgents}ag · ka ${kaSec}s · REAL`;
    }
    el.innerHTML = `<span style="color:${color};font-weight:bold">${label}</span>`;
  } catch(e) {
    // Don't spam console for transient network errors — TS executor
    // may be restarting (deploy in progress). Panel just stops
    // updating until next poll succeeds.
  }
}

function toggleProxyModal() {
  // Lightweight modal: show the per-agent table inline as alert
  // (no full modal infra needed for this rare interaction).
  const p = _lastProxyState;
  if (!p || !p.enabled) {
    alert('Residential proxy NOT configured.\n\nSet PROXY_URL_DEFAULT in executor env (Credentials.env), then restart executor-ts.');
    return;
  }
  const agentList = (p.agents || []).map(a => `  • ${a.key} → ${a.host}`).join('\n')
    || '  (no active agents yet — first order POST will create one per (platform, bot) pair)';
  const note = (p.fallback_to_direct)
    ? '\n\n⚠️ PROXY_FALLBACK_TO_DIRECT=1 — testing mode, NOT for real money!'
    : '\n\nFallback to direct VPS IP: DISABLED (safe default).';
  alert(
    `Residential proxy status\n\n` +
    `Enabled: ${p.enabled}\n` +
    `Keepalive: ${p.keepalive_active ? 'ACTIVE' : 'INACTIVE'} (${p.keepalive_interval_s}s interval)\n` +
    `Active agents: ${(p.agents || []).length}\n\n` +
    `Per-(platform, bot) agents:\n${agentList}` +
    note +
    `\n\nReminder: residential proxy is for ORDER PLACEMENT in real-deposit mode (DRY_RUN=0). ` +
    `In dry-run only the keepalive ticker uses it.`
  );
}

setInterval(refreshProxyState, 10000);
window.addEventListener('load', refreshProxyState);

// Phase audit-2 (11.05.2026) — pipelinePanel removed from UI per
// operator feedback ("убери пайп лайн с ui панели, мне он нужен
// только в разговоре с тобой"). The endpoints (`/api/pipeline_timings`,
// `/api/scan_health` with scan_tick_ms) stay public so the maintenance
// agent can keep probing them during diagnostic sessions. Just no
// header chip cluttering the operator's dashboard.

// ── Phase 3: risk panel ─────────────────────────────────────────
async function refreshRiskStatus() {
  try {
    const r = await fetch(`${API}/api/risk_status`);
    const j = await r.json();
    const el = document.getElementById('riskPanel');
    const killBtn = document.getElementById('killBtn');
    if (!el || !killBtn) return;

    if (j.killed) {
      el.innerHTML = `<span style="color:var(--red);font-weight:bold">🛑 KILLED</span>`;
      killBtn.innerHTML = '↺ RESUME';
      killBtn.style.background = 'var(--green-bg)';
      killBtn.style.color = 'var(--green)';
      killBtn.style.borderColor = 'var(--green)';
      killBtn.onclick = confirmResume;
      return;
    }

    killBtn.innerHTML = '🛑 STOP';
    killBtn.style.background = 'var(--red-bg)';
    killBtn.style.color = 'var(--red)';
    killBtn.style.borderColor = 'var(--red)';
    killBtn.onclick = confirmKillSwitch;

    const pnl = j.daily_pnl_usd;
    const lim = j.daily_loss_limit_usd;
    const pnlColor = pnl <= -lim*0.7 ? 'var(--red)' : pnl < 0 ? 'var(--gold)' : 'var(--green)';
    const losing = j.losing_trades_last_hour;
    const losingColor = losing >= j.losing_trades_per_hour_limit-1 ? 'var(--red)' :
                        losing >= 3 ? 'var(--gold)' : 'var(--text3)';
    let pauseHtml = '';
    if (j.paused) {
      const min = (j.paused_remaining_s/60).toFixed(0);
      pauseHtml = ` · <span style="color:var(--gold);font-weight:bold" title="${j.paused_reason}">⏸ ${min}m</span>`;
    }
    el.innerHTML = `<span style="color:var(--text3)">risk</span> ` +
                   `<span style="color:${pnlColor};font-weight:bold">$${pnl.toFixed(2)}</span>` +
                   `<span style="color:var(--text3)">/-$${lim.toFixed(0)}</span> · ` +
                   `<span style="color:${losingColor}">L${losing}/${j.losing_trades_per_hour_limit}</span>` +
                   pauseHtml;
  } catch(e) {}
}
setInterval(refreshRiskStatus, 5000);
window.addEventListener('load', refreshRiskStatus);

// Double-confirm kill switch — first click pops the modal, second-click
// "Yes, stop" inside the modal actually trips it. The modal also lets the
// operator type an optional reason (defaults to "manual_dashboard").
function confirmKillSwitch() {
  const reason = window.prompt(
    '⚠️ KILL SWITCH\n\n' +
    'This will:\n' +
    '  • Block ALL new fires (auto + manual dry-fires)\n' +
    '  • Cancel pending orders (Phase 4 wires this up; Phase 3 logs intent)\n' +
    '  • NOT close existing positions\n\n' +
    'Type a reason (or leave blank for "manual_dashboard") then OK to confirm,\n' +
    'or Cancel to abort.',
    ''
  );
  if (reason === null) return; // user clicked Cancel
  // Second confirm — explicit yes/no to avoid accidental Enter on prompt
  if (!window.confirm('Are you sure? This stops the bot.')) return;
  fetch(`${API}/api/kill`, {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({confirm:'YES', reason: reason || 'manual_dashboard'})
  }).then(r => r.json()).then(j => {
    refreshRiskStatus();
  }).catch(e => alert('Kill failed: '+e));
}

function confirmResume() {
  if (!window.confirm('Clear kill switch + active pause? Trading will resume.')) return;
  fetch(`${API}/api/risk_resume`, {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({confirm:'YES', reason:'manual_resume'})
  }).then(r => r.json()).then(j => refreshRiskStatus())
    .catch(e => alert('Resume failed: '+e));
}

// ── Phase 4: wallets panel ──────────────────────────────────────
async function refreshWalletsPanel() {
  try {
    const r = await fetch(`${API}/api/wallets`);
    const j = await r.json();
    const el = document.getElementById('walletsPanel');
    if (!el) return;
    if (j.count === 0) {
      el.innerHTML = `<span style="color:var(--text3)">wallets: 0/6 — not configured</span>`;
      return;
    }
    const sig = j.bots.filter(b => b.can_sign).length;
    const total = j.bots.reduce((s, b) => s + (b.usdc || 0), 0);
    const sigColor = sig === j.count ? 'var(--green)' : sig > 0 ? 'var(--gold)' : 'var(--text3)';
    el.innerHTML = `<span style="color:var(--text3)">wallets</span> ` +
                   `<span style="font-weight:bold">${j.count}/6</span>` +
                   ` <span style="color:${sigColor}">(${sig} can sign)</span>` +
                   ` · <span style="color:var(--text2)">$${total.toFixed(0)} pool</span>`;
  } catch(e) {}
}
setInterval(refreshWalletsPanel, 10000);
window.addEventListener('load', refreshWalletsPanel);

// ── Phase 5: graduation details modal ───────────────────────────
async function showGraduationDetails() {
  try {
    const r = await fetch(`${API}/api/graduation`);
    const j = await r.json();
    const dist = await fetch(`${API}/api/paper_distribution`).then(r=>r.json());
    const hist = await fetch(`${API}/api/graduation_history?days=14`).then(r=>r.json());

    let header = j.graduation_ready
      ? `🎓 GRADUATION READY — flip DRY_RUN=0 for first ${j.first_real_count} trades at $${j.first_real_size_usdc}/leg`
      : `In progress: ${j.next_threshold_hint}`;
    let body = `\nWindow: ${j.count}/${j.min_trades_required} paper trades`;
    if (j.win_rate_pct !== null) body += `\nWin rate: ${j.win_rate_pct}% (need ≥${j.min_win_rate_pct}%)`;
    if (j.mean_drift !== null) body += `\nMean drift: ${(j.mean_drift*100).toFixed(2)}% (need ≤${j.max_drift_pct}%)`;
    if (j.median_pnl !== null) body += `\nMedian P&L: $${j.median_pnl.toFixed(3)}`;
    if (j.mean_slippage_cents !== null) body += `\nMean slippage: ${j.mean_slippage_cents}¢/leg`;
    if (j.blockers.length) body += '\n\nBlockers:\n  • ' + j.blockers.join('\n  • ');

    let distStr = '\n\nP&L distribution (last 500 trades):';
    if (dist.total > 0) {
      const maxC = Math.max(...dist.counts);
      dist.bins.forEach((b, i) => {
        const bar = '█'.repeat(Math.round(20 * dist.counts[i] / maxC)) || '·';
        distStr += `\n  ${b.padEnd(15)} ${bar} ${dist.counts[i]}`;
      });
    } else {
      distStr += ' (no data)';
    }

    let histStr = '\n\nDaily win rate (last 14 days):';
    if (hist.days.length > 0) {
      hist.days.forEach(d => {
        histStr += `\n  ${d.date}  ${('' + d.win_rate_pct).padStart(5)}%  (n=${d.count}, drift ${d.mean_drift_pct ?? '—'}%)`;
      });
    } else {
      histStr += ' (no data)';
    }

    alert(header + body + distStr + histStr);
  } catch(e) { alert('Failed to load graduation details: ' + e); }
}

function toggleWalletsModal() {
  // Quick alert-based wallet detail — can be upgraded to a real modal later
  fetch(`${API}/api/wallets`).then(r => r.json()).then(j => {
    if (j.count === 0) {
      alert('Wallet pool is empty.\n\n' +
            'To enable: configure BOT1..BOT6 ETH addresses in the server-side environment.\n' +
            'Private keys can stay blank until Phase 5 graduation gate passes.');
      return;
    }
    const lines = j.bots.map(b => {
      const addr = b.eth_address.slice(0, 10) + '…' + b.eth_address.slice(-4);
      return `${b.bot_id}: ${addr}  $${b.usdc.toFixed(2)}  ${b.can_sign ? '🔑 can sign' : '🚫 no key'}`;
    });
    fetch(`${API}/api/rebalance/proposals`).then(r => r.json()).then(rb => {
      const pStr = rb.proposals.length === 0
        ? '\nRebalance: pool balanced — no proposals.'
        : '\nRebalance proposals:\n' + rb.proposals.map(p =>
            `  ${p.from} → ${p.to}: $${p.amount_usdc}  (${p.reason})`).join('\n');
      alert(`Wallet pool — backend=${j.backend}, ${j.count}/6 bots\n\n` +
            lines.join('\n') + '\n' + pStr);
    });
  });
}

function updateUI(data) {
  // Status. Phase 9vv (29.04.2026): UX cleanup — show short "Live"/"Сканирование"
  // only, drop the "polymarket N/N pages" technical label from the header
  // (still visible in the empty Deals/NEAR placeholder per operator request).
  if (data.scanning) {
    setStatus('scanning', 'Сканирование…');
    document.getElementById('scanBtn').disabled = true;
  } else if (data.error) {
    setStatus('error', 'Ошибка: ' + data.error);
    document.getElementById('scanBtn').disabled = false;
  } else if (data.last_scan) {
    setStatus('live', 'Live');
    document.getElementById('scanBtn').disabled = false;
    lastScan = new Date(data.last_scan);
  }

  // NEAR badge in nav (visible from any tab)
  const nb = document.getElementById('nearBadge');
  if (nb) {
    const nc = data.near_count || 0;
    if (nc > 0) { nb.textContent = nc; nb.style.display = 'inline-block'; }
    else { nb.style.display = 'none'; }
  }

  // WS widgets — Polymarket (data.ws) + Limitless (data.ws_limitless).
  // Same render logic for both; factored as a tiny helper.
  function renderWsState(elId, m, defaultMax) {
    const el = document.getElementById(elId);
    if (!el) return;
    if (!m || (!m.subs_active && !m.subs_desired)) {
      el.textContent = 'off';
      el.style.color = 'var(--text3)';
      return;
    }
    const subs = `${m.subs_active||0}/${m.subs_max||defaultMax}`;
    const rate = (m.msg_per_sec ?? 0) + ' msg/s';
    const age = m.last_msg_age_sec;
    const ageStr = age == null ? '—' : (age < 60 ? `${Math.round(age)}s` : `${Math.round(age/60)}m`);
    const rec = m.reconnects || 0;
    el.textContent = `${subs} · ${rate} · age ${ageStr}` + (rec ? ` · rec ${rec}` : '');
    const ok = m.connected && age != null && age < 30;
    el.style.color = ok ? 'var(--green)' : 'var(--gold)';
  }
  renderWsState('wsState', data.ws, 200);
  renderWsState('wsLimState', data.ws_limitless, 250);

  // Phase 19v6 (03.05.2026) — Platform Status Panel cards.
  // Same data sources as the WS widgets above, but laid out per-platform
  // with prominent health dots and pool counts. SX uses REST stats since
  // it has no WebSocket (REST poll every SX_MICRO_INTERVAL=3s).
  function renderPlatCard(prefix, m, pool_hot, pool_near, extras) {
    const dot = document.getElementById('platDot' + prefix);
    if (!dot) return;
    let healthOk = false;
    if (m && (m.subs_active || m.subs_desired || extras.fresh)) {
      const age = m.last_msg_age_sec ?? extras.age_seconds;
      healthOk = (m.connected || extras.fresh) && (age == null || age < 30);
    }
    dot.style.background = healthOk ? 'var(--green)' :
                           (m && (m.subs_active || extras.markets) ? 'var(--gold)' : 'var(--text3)');

    if (m) {
      const subsEl = document.getElementById('plat' + prefix + 'Subs');
      const rateEl = document.getElementById('plat' + prefix + 'Rate');
      const ageEl  = document.getElementById('plat' + prefix + 'Age');
      if (subsEl) subsEl.textContent = `${m.subs_active||0}/${m.subs_max||extras.maxSubs||'?'}`;
      if (rateEl) rateEl.textContent = `${m.msg_per_sec ?? 0} m/s`;
      const age = m.last_msg_age_sec;
      if (ageEl) ageEl.textContent = age == null ? '—' : (age < 60 ? `${Math.round(age)}s` : `${Math.round(age/60)}m`);
    }
    const hotEl = document.getElementById('plat' + prefix + 'Hot');
    if (hotEl) hotEl.textContent = pool_hot;
    const nearEl = document.getElementById('plat' + prefix + 'Near');
    if (nearEl) nearEl.textContent = pool_near;
    if (extras.passEl && extras.passVal != null) {
      const el = document.getElementById(extras.passEl);
      if (el) el.textContent = extras.passVal;
    }
  }
  const _stats = data.stats || {};
  renderPlatCard('Poly', data.ws,
    _stats.pool_poly_hot || 0, _stats.pool_poly_near || 0,
    {maxSubs: 1000, passEl: 'platPolyPass', passVal: _stats.poly_pass || 0});
  renderPlatCard('Lim', data.ws_limitless,
    _stats.pool_lim_hot || 0, _stats.pool_lim_near || 0,
    {maxSubs: 250, passEl: 'platLimEvents', passVal: _stats.lim_events || 0});

  // SX is REST-only — synthesize a "metrics-like" object from scan_stats.
  const sxMarketsCount = _stats.sx_markets || 0;
  const sxBinaryCount  = _stats.sx_binary_count || 0;
  const sxHttp = _stats.sx_http_status || '—';
  const sxMoneyline = _stats.sx_moneyline_count || 0;
  const sxHealthFresh = sxMarketsCount > 0 && sxHttp === 200;
  // Reuse renderPlatCard but with no WS metrics — pass null and use 'fresh' flag
  const sxDot = document.getElementById('platDotSx');
  if (sxDot) {
    sxDot.style.background = sxHealthFresh ? 'var(--green)' :
                              (sxMarketsCount > 0 ? 'var(--gold)' : 'var(--red)');
  }
  const sxMarketsEl = document.getElementById('platSxMarkets');
  if (sxMarketsEl) sxMarketsEl.textContent = sxMarketsCount;
  const sxBinEl = document.getElementById('platSxBinary');
  if (sxBinEl) sxBinEl.textContent = sxBinaryCount;
  const sxHttpEl = document.getElementById('platSxHttp');
  if (sxHttpEl) {
    sxHttpEl.textContent = sxHttp;
    sxHttpEl.style.color = (sxHttp === 200) ? 'var(--green)' :
                            (sxHttp ? 'var(--red)' : 'var(--text3)');
  }
  const sxHotEl = document.getElementById('platSxHot');
  if (sxHotEl) sxHotEl.textContent = _stats.pool_sx_hot || 0;
  const sxNearEl = document.getElementById('platSxNear');
  if (sxNearEl) sxNearEl.textContent = _stats.pool_sx_near || 0;
  const sxMlEl = document.getElementById('platSxMoneyline');
  if (sxMlEl) sxMlEl.textContent = sxMoneyline;

  // Phase 9ww (29.04.2026) — Deals badge in nav, mirrors NEAR/Карантин.
  // Phase 9eee.2 — was `const deals = ...` here AND in Stats section
  // → SyntaxError: Identifier 'deals' has already been declared → entire
  // script halts → all tabs unresponsive. Now we don't bind a name here;
  // the badge update reads from data.deals directly.
  {
    const dealsBadge = document.getElementById('dealsBadge');
    const _dealsCount = (data.deals || []).length;
    if (dealsBadge) {
      if (_dealsCount > 0) {
        dealsBadge.textContent = _dealsCount;
        dealsBadge.style.display = 'inline-block';
      } else {
        dealsBadge.style.display = 'none';
      }
    }
  }

  // Phase clean-quarantine (11.05.2026) — quarantine tab removed. Empty
  // the legacy alerts dropdown so a stale list doesn't linger on screen.
  const listEl = document.getElementById('alertsList');
  if (listEl) listEl.innerHTML = '<div class="alerts-empty">Нет новых сделок</div>';

  // Stats
  const deals = data.deals || [];
  const stats = data.stats || {};
  const polyDeals = deals.filter(d => d.platform === 'Polymarket');
  const kalshiDeals = deals.filter(d => d.platform === 'Kalshi');
  const sxDeals = deals.filter(d => d.platform === 'SX Bet');
  const limDeals = deals.filter(d => d.platform === 'Limitless');
  const totalProfit = deals.reduce((s,d) => s + d.net, 0);
  const avgRoi = deals.length > 0 ? deals.reduce((s,d) => s + d.roi, 0) / deals.length : 0;

  // Phase 9eee.1 — defensive null-checks via helper (rule #1).
  function _setText(id, val) {
    const el = document.getElementById(id);
    if (el) el.textContent = val;
  }
  _setText('sDeals', deals.length);
  _setText('sProfit', '$' + totalProfit.toFixed(0));
  _setText('sAvg', avgRoi.toFixed(1) + '%');
  _setText('sPoly', polyDeals.length);
  _setText('sLim', limDeals.length);
  _setText('sKalshi', kalshiDeals.length);
  _setText('sSx', sxDeals.length);
  _setText('sScanned', (stats.poly_events||0) + (stats.kalshi_events||0) + (stats.sx_markets||0) + (stats.lim_events||0));

  // Deals
  const grid = document.getElementById('dealsGrid');

  // Phase 9iii (30.04.2026) — operator wants ONLY "Арбитражных окон не
  // найдено" placeholder. The previous "Идёт сканирование — polymarket
  // 4/4 pages" tech-info was useful for debugging but added noise to
  // production UI. Status indicator in the header (Live/Сканирование)
  // already conveys scan state; no need to duplicate in the deals grid.
  if (deals.length === 0) {
    grid.innerHTML = '';
    const el = document.createElement('div');
    el.className = 'empty';
    el.innerHTML = '<h2>😐 Арбитражных окон не найдено</h2><p>Попробуйте обновить через 2 минуты</p>';
    grid.appendChild(el);
    return;
  }

  grid.innerHTML = '';
  deals.forEach((d, i) => {
    grid.appendChild(createDealCard(d, i, false));
  });
}

function createDealCard(d, idx, isQuarantine) {
  const card = document.createElement('div');
  card.className = 'deal-card' + (expandedSet.has(d.title) ? ' expanded' : '');
  card.dataset.title = d.title;
  
  let qButtons = '';
  if(isQuarantine) {
    // Phase 9kkk hotfix #3 (30.04.2026) — operator-found bug:
    // "Одобрить / Удалить" buttons were dead since PR #23 (manual decision
    // flow removal — replaced by full automation + risk gate + graduation
    // gate). The /api/approve and /api/reject endpoints don't exist on the
    // server, so clicks did nothing. Quarantine tab is now read-only:
    // events with detected Other-outcome / threshold-series / etc. live
    // there for transparency, the executor refuses to fire them
    // automatically (is_quarantine=True flag in the deal dict).
    // We keep the visual indication via the "OTHER RISK" badge on the
    // deal card itself; no operator action is needed.
    qButtons = `
      <div style="margin-left:16px;display:flex;gap:8px;align-items:center;color:var(--text3);font-size:11px;font-style:italic">
        Авто-блок: executor не файрит карантинные сделки
      </div>
    `;
  } else {
    // Phase audit-14 (15.05.2026) — manual Dry-fire button removed in
    // live-trading mode. Auto-fire is the only path now; the button
    // was operator-confusing (visible even in LIVE) and not needed for
    // any production workflow. The dryfire endpoint stays for ad-hoc
    // CLI testing via curl, but the dashboard no longer surfaces it.
    qButtons = '';
  }

  // Phase 1 + 13: arb_structure badge — A/B/C + binary (SX Bet) + X cross-platform.
  const structMap = {
    'all_yes':         {label:'A · ALL YES',  bg:'var(--green-bg)', color:'var(--green)', tip:'Σ YES asks < threshold'},
    'all_no':          {label:'B · ALL NO',   bg:'var(--gold-bg)',  color:'var(--gold)',  tip:'Σ NO asks < (N-1) · threshold — multi-outcome events only'},
    'yes_no_pair':     {label:'C · YES+NO',   bg:'var(--bg4)',      color:'var(--text)',  tip:'Per-market YES + NO < threshold'},
    'binary':          {label:'◑ binary',     bg:'var(--bg4)',      color:'var(--text2)', tip:'Two-outcome market — A/B/C collapse to one structure'},
    // Phase 13 (01.05.2026) — cross-platform X1/X2 — same event, 2 platforms
    'cross_platform':  {label:'⇆ CROSS',      bg:'#3b1d52',         color:'#c08aff',      tip:'Cross-platform: YES on platform A + NO on platform B (or symmetric)'},
  };
  const sInfo = structMap[d.arb_structure] || null;
  const structBadge = sInfo
    ? `<span class="badge" style="background:${sInfo.bg};color:${sInfo.color}" title="${sInfo.tip}">${sInfo.label}</span>`
    : '';

  // Phase 16 (01.05.2026) — fire_mode badge (maker / taker / hybrid).
  const fireMode = d.fire_mode || (d.fire_result && d.fire_result.fire_mode);
  const fireModeBadge = fireMode && fireMode !== 'taker'
    ? `<span class="badge" style="background:#1a3850;color:#7ec6ff" title="Last fire used ${fireMode} mode">⚡ ${fireMode}</span>`
    : '';
  // Phase 13 (01.05.2026) — cross-platform: show "Poly+Lim" pair instead of single platform
  const isCrossPlatform = d.arb_structure === 'cross_platform';
  const crossSubtype = d.cross_structure || ''; // X1 / X2
  const crossSubBadge = isCrossPlatform && crossSubtype
    ? `<span class="badge" style="background:#5a3870;color:#fff;font-size:10px" title="X1 = YES_a + NO_b; X2 = NO_a + YES_b">${crossSubtype}</span>`
    : '';

  // Phase 19v16 (05.05.2026) — defensive escaping for ALL server-controlled
  // strings interpolated into the deal card. Earlier Phase 19v14 fixed
  // the dryFire button only; the rest of the card still rendered
  // `${d.platform}`, `${d.grade}`, `${d.risk}`, `${e.source}`, `${e.name}`
  // (in title attr) directly. A malicious market title or a rogue
  // upstream API value could inject HTML/JS. Numerics are coerced via
  // Number()||0 so missing fields render as 0 instead of "undefined" or
  // throwing on `.toFixed`/`.toLocaleString`.
  const num = (v) => Number(v) || 0;
  const safeStr = (v) => escHtml(String(v ?? ''));
  // Sanitize platform → CSS class fragment must be alphanumeric only
  const plat = String(d.platform || '');
  const platClass = (plat === 'SX Bet' ? 'SX' : plat.split('+')[0])
                       .replace(/[^a-zA-Z0-9]/g, '');
  const balance = num(d.balance_used);
  card.innerHTML = `
    <div class="deal-header">
      <div class="deal-title">${escHtml(d.title)}</div>
      <div class="deal-badges">
        ${isQuarantine ? '<span class="badge" style="background:var(--red-bg);color:var(--red)">⚠️ OTHER RISK</span>' : ''}
        ${balance > 0 && balance < 100 ? `<span class="badge" style="background:var(--bg4);color:var(--text2)">$${balance.toFixed(0)} STAKE</span>` : ''}
        <span class="badge platform platform-${platClass}">${safeStr(plat)}</span>
        ${structBadge}
        ${crossSubBadge}
        ${fireModeBadge}
        <span class="badge ${gradeClass(d.grade)}">${safeStr(d.grade)}</span>
        <span class="badge" style="background:var(--bg4)">${num(d.outcomes) || (d.entries ? d.entries.length : 0)} исх.</span>
        <span style="font-size:18px;font-weight:800;color:${num(d.net)>0?'var(--green)':'var(--red)'}">$${num(d.net).toFixed(2)}</span>
        ${qButtons}
      </div>
    </div>
    <div class="deal-metrics">
      <div class="metric"><div class="metric-val">${num(d.total_cents)}¢</div><div class="metric-lbl">Сумма</div></div>
      <div class="metric"><div class="metric-val">${num(d.spread_cents)}¢</div><div class="metric-lbl">Спред</div></div>
      <div class="metric"><div class="metric-val profit">${num(d.gross_pct)}%</div><div class="metric-lbl">Gross</div></div>
      <div class="metric"><div class="metric-val" style="color:var(--red)">${num(d.fee_pct)}%</div><div class="metric-lbl">Fee</div></div>
      <div class="metric"><div class="metric-val ${num(d.roi)>0?'profit':'loss'}">${num(d.roi)}%</div><div class="metric-lbl">ROI</div></div>
      <div class="metric"><div class="metric-val ${num(d.adj_roi)>0?'profit':'loss'}">${num(d.adj_roi)}%</div><div class="metric-lbl">ROI adj</div></div>
    </div>
    <div class="deal-body">
      <table class="outcomes-table">
        <tr><th>#</th><th>Исход</th><th>Цена</th><th>Источник</th><th>Коэф</th><th>Ставка</th><th>Контракты</th><th>Fee</th><th>Ликвидность</th><th>Доля</th></tr>
        ${d.entries.map((e, ei) => `
          <tr>
            <td style="color:var(--text3)">${ei+1}</td>
            <td style="max-width:520px;white-space:normal;word-break:break-word" title="${escHtml(e.name)}">${escHtml(e.name)}</td>
            <td><strong>${num(e.price_cents)}¢</strong></td>
            <td><span class="badge" style="font-size:9px;padding:2px 6px;${e.source==='clob_ask'||e.source==='kalshi_ob'||e.source==='sx_ob'||e.source==='lim_clob'?'background:var(--green-bg);color:var(--green)':e.source==='ws'||e.source==='lim_ws'?'background:var(--gold-bg);color:var(--gold)':'background:var(--red-bg);color:var(--red)'}" title="src=${safeStr(e.source||'?')}">${e.source==='clob_ask'?'CLOB':e.source==='kalshi_ob'?'KALSHI':e.source==='sx_ob'?'SX':e.source==='lim_clob'?'LIM':e.source==='ws'?'WS':e.source==='lim_ws'?'LIM-WS':e.source==='implied'?'⚠ MID':safeStr(String(e.source||'?').toUpperCase())}</span></td>
            <td style="color:var(--text2)">${num(e.coeff)}x</td>
            <td style="color:var(--green)" title="Размер ставки в долларах. Для cross-platform арбов одинаков на всех ногах (= balance_used = min(min_leg_depth, $55))">${fmtMoney(e.stake)}</td>
            <td title="Количество контрактов = stake / price">${num(e.contracts).toLocaleString('en-US', {maximumFractionDigits:2})}</td>
            <td style="color:var(--red)">${fmtMoney(e.fee)}</td>
            <td title="Депт стакана на этой цене в долларах USDC">${fmtMoney(e.liquidity)}</td>
            <td>
              ${num(e.share_pct)}%
              <div class="price-bar"><div class="price-fill" style="width:${Math.min(num(e.share_pct),100)}%"></div></div>
            </td>
          </tr>
        `).join('')}
      </table>
      <div class="deal-footer">
        <span>💰 Грязный: <strong style="color:var(--green)">${fmtMoney(d.gross)}</strong></span>
        <span>📉 Slippage: <strong>~${num(d.slip_pct)}%</strong> (-${fmtMoney(d.slip_cost)})</span>
        <span title="Min liquidity — наименьшая глубина стакана на лучшем аске среди ног. Это потолок размера ставки на этой цене.">🏦 Мин. стакан: <strong>$${fmtCompact(num(d.min_liq))}</strong></span>
        <span>📊 Порог: <strong>${num(d.threshold)}¢</strong></span>
        <span>⚙️ Theta: <strong>${num(d.theta)}</strong></span>
      </div>
    </div>
  `;
  return card;
}

function gradeClass(g) {
  if (g === 'A+' || g === 'A') return 'grade-a';
  if (g === 'B') return 'grade-b';
  if (g === 'C' || g === 'D') return 'grade-c';
  return 'grade-f';
}

function escHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

// fmtCompact($29559) → '29.5k'; fmtCompact($999) → '999'; fmtCompact($1500000) → '1.5M'.
// Used for min_liq / market cap-style numbers where space is tight and
// exact dollars don't matter — operator wants order-of-magnitude, not
// $29,559.00 precision. Operator feedback (11.05.2026): "не понятно, я
// думал всё это время, что это тысячи" — fmtCompact makes K/M explicit.
function fmtCompact(n) {
  const x = Number(n) || 0;
  if (x >= 1e9) return (x / 1e9).toFixed(1).replace(/\.0$/, '') + 'B';
  if (x >= 1e6) return (x / 1e6).toFixed(1).replace(/\.0$/, '') + 'M';
  if (x >= 1e3) return (x / 1e3).toFixed(1).replace(/\.0$/, '') + 'k';
  return Math.round(x).toString();
}

// Phase audit-2 (11.05.2026) — BUG-E4: locale inconsistency fix.
// Operator screenshot 11.05.2026 showed same number rendered as both
// '$23.091' (period) and '$23,091' (comma) in adjacent table columns —
// stake column used `num(x)` (toString → en-US-style period as decimal)
// while liquidity column used `num(x).toLocaleString()` (uses BROWSER
// locale — Russian/German would use comma as decimal). For a value
// like 23.091, this rendered the SAME amount in two visually-different
// formats and operator thought stake and liquidity were different
// numbers.
//
// fmtMoney always renders in en-US locale (period decimal, comma
// thousand separator), regardless of browser locale. < $100 → 2 decimal
// places; $100-$10k → integer with thousand sep; ≥$10k → fmtCompact.
function fmtMoney(n) {
  const x = Number(n) || 0;
  if (x >= 10000) return '$' + fmtCompact(x);
  if (x >= 100) return '$' + Math.round(x).toLocaleString('en-US');
  return '$' + x.toFixed(2);
}

function setStatus(type, text) {
  const dot = document.getElementById('statusDot');
  const txt = document.getElementById('statusText');
  dot.className = 'status-dot ' + type;
  txt.textContent = text;
}

async function triggerScan() {
  document.getElementById('scanBtn').disabled = true;
  setStatus('scanning', 'Запуск...');
  try {
    await fetch(`${API}/api/scan`, {method:'POST'});
  } catch(e) {}
  setTimeout(fetchDeals, 2000);
}

// Timer update
function updateTimer() {
  if (!lastScan) return;
  const diff = Math.floor((Date.now() - lastScan.getTime()) / 1000);
  const m = Math.floor(diff / 60);
  const s = diff % 60;
  document.getElementById('timer').textContent = `Обновлено ${m}:${String(s).padStart(2,'0')} назад`;
}

// Init
fetchDeals();
// Phase 19 (02.05.2026): poll /api/deals каждые 3с вместо 10с.
// run_scan() обновляет scan_data per-chunk через _push_partial → near
// pool, deals, last_scan свежие в течение секунд.
//
// Phase audit-2 (11.05.2026) — operator hit the case "появились и я
// гадаю 15 секунд есть ли они еще". 3s polling means a deal that
// disappeared between scan-ticks could linger on the UI for up to 3s
// after it actually vanished from scan_data. Drop to 1s so the deals
// table mirrors live state. /api/deals is a cheap memory read (no
// recompute), so 1 RPS per tab is negligible load on gunicorn.
setInterval(fetchDeals, 1000);
setInterval(updateTimer, 1000);

// Phase 19v20 — visibility-aware polling. When the user switches back
// to a hidden dashboard tab, fire an immediate fetch instead of waiting
// up to 3s for the next interval tick.
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) {
    fetchDeals();
  }
});

// Phase 19v20 — bounded growth for ever-accumulating sets so a long-
// running tab doesn't leak memory. `expandedSet` and `seenAlerts` were
// only ever grown, never pruned. With ~thousands of titles per day,
// after 24-48h the operator sees noticeable slowdown on `Set.has`
// lookups + ever-growing heap.
function _capSet(s, maxSize) {
  if (!(s instanceof Set) || s.size <= maxSize) return;
  const overflow = s.size - maxSize;
  let i = 0;
  for (const k of s) {
    if (i++ >= overflow) break;
    s.delete(k);
  }
}
setInterval(() => {
  if (typeof expandedSet !== 'undefined') _capSet(expandedSet, 500);
  if (typeof seenAlerts !== 'undefined') _capSet(seenAlerts, 1000);
}, 60000);
