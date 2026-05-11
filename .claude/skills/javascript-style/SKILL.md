# JavaScript Style & Defensive Coding

**Source**: hoelzro/dotfiles/claude/skills/js-style + lessons from our radar bugs

## When to use

Writing or modifying any JS in `dashboard.html`. Goal: prevent the
"полная поломка UI" pattern we hit twice this week (`nearBufferLabel`
null crash + cache-busting issue).

## Iron rules for our dashboard.html

### 1. NEVER assume `getElementById` returns non-null

```js
// ❌ BAD — crashes whole script if element missing
document.getElementById('foo').textContent = 'bar';

// ✅ GOOD — defensive null-check
const el = document.getElementById('foo');
if (el) el.textContent = 'bar';

// ✅ ALSO GOOD — optional chaining (modern JS)
document.getElementById('foo')?.setAttribute('value', 'bar');
```

**Why**: a single null exception in `updateUI()` halts ALL subsequent JS,
making the whole dashboard non-interactive (nav tabs stop working).

### 2. NEVER throw out of forEach / map callbacks

```js
// ❌ BAD — errors caught silently OR halt iteration
items.forEach(it => {
  doStuff(it.field.subfield);  // throws if field is undefined
});

// ✅ GOOD — wrap each iteration
items.forEach(it => {
  try {
    doStuff(it?.field?.subfield);
  } catch (e) {
    console.error('item failed:', e, it);
  }
});
```

### 3. ALWAYS guard server-data shape

```js
// ❌ BAD — assumes shape, breaks on missing field
const deals = data.deals.length;

// ✅ GOOD
const deals = (data.deals || []).length;

// ✅ GOOD with defaults
const stats = data.stats || {};
const arbCount = stats.arb_found || 0;
```

### 4. Wrap fetch handlers — always

```js
async function fetchSomething() {
  try {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    updateUI(data);  // ← if THIS throws, status stays "Connecting..."
  } catch (e) {
    console.error('fetch failed:', e);
    setStatus('error', 'Сервер недоступен');
  }
}
```

### 5. Cache-bust HTML in production

Browsers aggressively cache HTML. After every deploy, users see OLD JS
unless we send `Cache-Control: no-cache`. Server-side at Flask:

```python
@app.route('/')
def index():
    resp = send_file('dashboard.html')
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return resp
```

(This is what Phase 9eee.1 added.)

### 6. Test path — manually after every dashboard.html change

Before commit:
1. Open DevTools → Console — should have ZERO red errors on first paint
2. Click each nav tab — all 4 (Deals/Карантин/NEAR/Analytics) must respond
3. Hard reload (Ctrl+Shift+R) — page should still work without cache
4. Check Network tab — `/api/deals` returns 200, no 4xx/5xx

## What broke this week (lessons)

| Bug | Cause | Fix | Prevention |
|---|---|---|---|
| NEAR table empty при count>0 | `getElementById('nearBufferLabel').textContent` — element removed but JS kept reference | null-check added | rule #1 above |
| All tabs дед при cached JS | Flask отдавал dashboard.html без cache headers | added `Cache-Control: no-cache` | rule #5 above |
| Status stuck at "Connecting..." | One JS exception halted entire script | wrap callbacks + null checks | rules #1+#2 above |

## Repository

https://github.com/hoelzro/dotfiles
