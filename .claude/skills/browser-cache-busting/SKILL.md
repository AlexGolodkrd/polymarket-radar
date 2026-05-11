---
name: browser-cache-busting
description: |
  Prevent stale-JS / stale-CSS browser caching after dashboard.html deploy.
  Operator's recurring pain: deploy lands on VPS, but he opens kapkan.4frdm.live
  and sees yesterday's UI because Chrome served from cache. This SKILL covers
  Cache-Control headers, querystring versioning, and ETag strategies.
---

# browser-cache-busting — свежий UI после деплоя

## Проблема (как мы её ловим)

1. Деплой на VPS прошёл (новый `dashboard.html` + новый JS)
2. Оператор открывает `https://kapkan.4frdm.live`
3. Chrome видит что `dashboard.html` ещё в кэше → отдаёт старую версию
4. Оператор видит старый UI без новых фич / со старыми багами
5. Час разбираемся "почему фича не работает" — на самом деле просто кэш

## 3 уровня решения (от самого простого до самого надёжного)

### 1. Hard-coded Cache-Control headers (минимум)

В Flask:
```python
@app.route('/')
def dashboard():
    response = send_file('dashboard.html')
    # Force re-validation on every request
    response.headers['Cache-Control'] = 'no-cache, must-revalidate'
    response.headers['Pragma'] = 'no-cache'  # legacy HTTP/1.0
    response.headers['Expires'] = '0'
    return response
```

Эффект: браузер каждый раз делает GET и проверяет `Last-Modified` / `ETag`. Если файл не изменился — 304 (быстро, мало байт). Если изменился — 200 + новое содержимое.

**Это уже у нас в `arb_server.py`** (Phase 9eee — добавили после жалобы оператора). Проверка:
```bash
curl -I https://kapkan.4frdm.live/ | grep -i "cache-control"
# Ожидаем: Cache-Control: no-cache, must-revalidate
```

### 2. Querystring versioning для ассетов

Если в `dashboard.html` есть external scripts:
```html
<!-- ❌ Плохо: -->
<script src="/static/app.js"></script>

<!-- ✅ Хорошо: -->
<script src="/static/app.js?v={{ version }}"></script>
```

Где `{{ version }}` — git commit hash или timestamp deploy'а. Каждый deploy → новый querystring → браузер считает это новым ресурсом → грузит с нуля.

```python
import subprocess
APP_VERSION = subprocess.check_output(
    ['git', 'rev-parse', '--short', 'HEAD'], stderr=subprocess.DEVNULL
).decode().strip()

@app.context_processor
def inject_version():
    return dict(version=APP_VERSION)
```

### 3. ETag-based (самый правильный)

Flask делает это автоматически если `send_file()` или `send_from_directory()`:
```python
@app.route('/')
def dashboard():
    return send_file('dashboard.html', conditional=True)
    # conditional=True добавляет ETag + Last-Modified, отвечает 304 если не изменилось
```

Браузер сам решает: cache hit → 304, cache miss → 200.

## Для нашего проекта (рекомендация)

`dashboard.html` всё ещё inline (один файл, JS внутри). Тогда:

```python
# arb_server.py
import hashlib
_DASHBOARD_HASH = None

def _compute_dashboard_hash():
    global _DASHBOARD_HASH
    with open('Scripts/dashboard.html', 'rb') as f:
        _DASHBOARD_HASH = hashlib.md5(f.read()).hexdigest()[:8]
    return _DASHBOARD_HASH

_compute_dashboard_hash()  # at startup

@app.route('/')
def dashboard():
    """Phase 9X — proper cache validation via ETag."""
    response = send_file('Scripts/dashboard.html', conditional=True)
    response.headers['ETag'] = f'"{_DASHBOARD_HASH}"'
    # Allow cache for 60s, then re-validate
    response.headers['Cache-Control'] = 'public, max-age=60, must-revalidate'
    return response

# Recompute hash on SIGHUP (after `docker compose up` restart)
# или просто при каждом restart контейнера — новый процесс, новый hash
```

Альтернативно — убрать кэш совсем для HTML, держать кэш для статики:
```python
@app.route('/')
def dashboard():
    response = send_file('Scripts/dashboard.html')
    response.headers['Cache-Control'] = 'no-cache, must-revalidate'
    return response

@app.route('/static/<path:p>')
def static(p):
    response = send_from_directory('Scripts/static', p)
    response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    return response
```

## CDN / nginx / Cloudflare layer

Если у нас впереди nginx (на VPS — да):
```nginx
# nginx-config/...
location / {
    proxy_pass http://radar:5050;
    proxy_set_header Host $host;
    add_header Cache-Control "no-cache, must-revalidate" always;
}
```

`always` — ВАЖНО. Без него nginx не добавит header если backend дал 304.

## Smoke test после deploy

```bash
# 1. Проверь что headers правильные
curl -I https://kapkan.4frdm.live/ | grep -iE "cache-control|etag|last-modified"
# Ожидаем: Cache-Control: no-cache..., ETag: "..."

# 2. Симулируй cache hit (браузер с ETag)
curl -I -H "If-None-Match: \"old-etag\"" https://kapkan.4frdm.live/
# Ожидаем: HTTP/1.1 200 (новый etag)

curl -I -H "If-None-Match: \"$(curl -sI https://kapkan.4frdm.live/ | grep -i etag | awk '{print $2}' | tr -d '\r')\"" https://kapkan.4frdm.live/
# Ожидаем: HTTP/1.1 304 Not Modified
```

## Что советовать оператору

```
Если после deploy UI не обновляется:

1. Hard reload: Ctrl+Shift+R (Windows/Linux) / Cmd+Shift+R (Mac)
2. DevTools → Network tab → "Disable cache" чекбокс при открытом DevTools
3. Incognito window (приватный браузинг минует кэш)
4. Полностью очистить кэш: Settings → Privacy → Clear browsing data
```

## Обнаружение проблемы автоматически

```js
// В dashboard.html — самопроверка версии
const BUILD_HASH = '__BUILD_HASH__';  // подставляется при build/deploy

// Каждые 5 минут проверяем актуальность
setInterval(async () => {
    const r = await fetch('/api/build_info');
    const d = await r.json();
    if (d.hash !== BUILD_HASH) {
        const banner = document.getElementById('staleBanner');
        banner.style.display = 'block';
        banner.innerHTML = '⚠ Установлена новая версия. <a href="javascript:location.reload(true)">Обновить страницу</a>';
    }
}, 5 * 60 * 1000);
```

```python
@app.route('/api/build_info')
def build_info():
    return jsonify({'hash': APP_VERSION, 'built_at': APP_BUILT_AT})
```

## Anti-pattern: НЕ делать

```python
# ❌ Не использовать max-age=0 — браузеры некоторых версий игнорируют
response.headers['Cache-Control'] = 'max-age=0'

# ❌ Не комбинировать ETag + max-age=0 — становится 2 источника truth
response.headers['ETag'] = '...'
response.headers['Cache-Control'] = 'max-age=0'  # игнорируется

# ❌ Не отключать кэш для всего — статика должна кэшироваться
@app.after_request
def disable_cache(r):
    r.headers['Cache-Control'] = 'no-store'  # ← убивает performance
    return r
```

## Refs

- `deploy-pipeline/SKILL.md` — где cache-busting вписан в общий процесс
- `flask-best-practices/SKILL.md` — общие Flask паттерны
