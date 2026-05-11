---
name: auto-deploy-gotchas
description: Lessons learned from real production deploy failures on the plan-kapkan radar VPS — silent container crash, nginx workflow editing backups, tsconfig rootDir bullshit, Drone-SSH script injection. Use when writing/debugging GitHub Actions deploy workflows, dockerfile changes, or nginx config workflows targeting a remote VPS via SSH.
---

# Auto-deploy gotchas — hard-won lessons

These traps cost real production downtime on this project. Every entry has a specific date and PR so you can `git show` for context.

## 1. "Container Started" ≠ healthy

**Trap:** `docker compose up -d --build` logs `Container plan-kapkan-executor-ts Started` and the workflow exits success. But the container's `CMD` can crash within milliseconds, leaving the container in `Restarting (1)` state. The deploy workflow looks green; the service is dead.

**Symptom on this project:** TS executor crash-looped for weeks with `Error: Cannot find module '/app/dist/server.js'`. Every TS-3+ deploy reported success. Radar's `_fire_arb_via_ts` dispatcher caught the connection error and silently fell back to in-process Python. Nobody noticed until we added `/api/ts_metrics` proxy.

**Fix:** After `docker compose up`, sleep + `docker ps | grep "Up [0-9]"` to confirm container is actually staying up. Or query a container-internal health endpoint. Don't trust `Started` log lines.

**See:** PR #141 (the rootDir fix), and the `/api/ts_metrics` endpoint added in PR #138.

## 2. `tsc` rootDir resolves to project root if you `include` tests

**Trap:** `tsconfig.json` with `"rootDir": "."` and `"include": ["src/**/*", "tests/**/*"]`. Even when Docker context only copies `src/`, tsc emits to `dist/src/server.js` instead of `dist/server.js` — because the explicit rootDir overrides longest-common-prefix inference.

**Symptom:** Dockerfile's `CMD ["node", "dist/server.js"]` fails with `MODULE_NOT_FOUND`. Container crash-loops.

**Fix:** Split tsconfigs:
- `tsconfig.json`: `rootDir: "./src"`, `include: ["src/**/*"]` — used by `npx tsc` to build
- `tsconfig.test.json` extends with `rootDir: "."`, `noEmit: true`, includes src+tests — used by `npm run typecheck`

Vitest doesn't care about tsconfig.json's include because it transpiles in-memory via vite.

**See:** PR #141.

## 3. nginx workflow editing `.bak` instead of live config

**Trap:** Auto-detecting nginx config path with `grep -rl 'kapkan' /etc/nginx/sites-available/` returns ALL files matching — including timestamped backups from previous workflow runs. Directory iteration order is filesystem-dependent (not alphabetical); on this VPS the backup came first.

**Symptom:** Workflow logs `inserted exception block`, `nginx -t` passes, reload succeeds — but nginx still serves 401. Because we edited `*-radar.bak.20260510T172318Z`, not the live config.

**Fix:** Filter `.bak` files from candidates:
```bash
CANDIDATES=$(sudo grep -rl 'kapkan' /etc/nginx/... | grep -v '\.bak')
```

**See:** PR #141 fix in `.github/workflows/apply-nginx-ts-metrics.yml`.

## 4. nginx `location =` block in WRONG `server { }`

**Trap:** Naive `re.search(r'server\s*\{')` matches the FIRST server block in nginx config. On certbot-managed sites, the first is the HTTP→HTTPS redirect block. Inserting an `auth_basic off; location` there has no effect on HTTPS traffic — the second (HTTPS) server block still applies its own auth_basic.

**Symptom:** `location = /api/foo` shows up in `nginx -T`, in the HTTP block. HTTPS probes still 401.

**Fix:** Walk server blocks via brace-depth counter, find the FIRST one containing `listen 443` or `ssl_certificate`:
```python
for m in re.finditer(r'server\s*\{', content):
    # Track brace depth to find matching close
    start = m.end(); depth = 1; j = start
    while j < len(content) and depth > 0:
        if content[j] == '{': depth += 1
        elif content[j] == '}': depth -= 1
        j += 1
    block = content[start:j-1]
    if re.search(r'listen\s+443|ssl_certificate', block):
        # Insert here
```

**See:** PR #141.

## 5. Drone-SSH `script_stop: true` injects shell between every line

**Trap:** `appleboy/ssh-action@v1` with `script_stop: true` (default) inserts `DRONE_SSH_PREV_COMMAND_EXIT_CODE=$? ; if [ ... ]; fi;` between every line of your script. Breaks heredocs, breaks `python -c`, breaks any multi-line command.

**Symptom:** Script that works in interactive ssh fails in workflow with bizarre syntax errors deep in heredoc bodies.

**Fix:** Set `script_stop: false`. Use `set -euo pipefail` at top of script for fail-fast behavior.

**See:** Phase deploy-fix-1 history (PRs #112-#114). Now standard in all SSH workflows on this repo.

## 6. docker-compose `environment:` silently overrides `env_file:`

**Trap:** Compose merges `env_file:` then `environment:`. Writing `EXECUTOR_URL: ${EXECUTOR_URL:-}` in `environment:` with no shell-side value passes `EXECUTOR_URL=` (empty string), MASKING the value the operator put in `Credentials.env`.

**Symptom:** `Credentials.env` has `EXECUTOR_URL=http://executor-ts:5051`, `docker exec radar env` shows `EXECUTOR_URL=` empty.

**Fix:** Don't list a variable in `environment:` if its only purpose is to flow from `env_file:`. Comment loudly why.

**See:** docker-compose.yml comment block (Phase v36-fix, 09.05.2026).

## 7. Flask relative paths break under gunicorn workers

**Trap:** `os.path.join('Executions', 'analytics_events.jsonl')` in a Flask route reads relative to the gunicorn worker's CWD. Under different launch configs (`python -m Scripts.arb_server` vs `gunicorn -w 4 arb_server:app`), CWD can be project root OR Scripts/ subdir.

**Symptom:** Endpoint returns `count: 0` while a sibling endpoint (using absolute path) returns hundreds of rows from the same file.

**Fix:** Always use `os.path.abspath(__file__)`-derived paths. Centralize in one module (`analytics.py` here) and import the constant.

**See:** PR #143, the `/api/recent_deals` path fix.

## 8. Build cache mismatch: `docker restart` vs `docker compose up --build`

**Trap:** `docker restart` reuses the existing image — code changes from `git pull` are NOT picked up. Workflow shows "Restarted" successfully; running code is from the previous image.

**Symptom:** PR is merged to main, version-sha matches, but new behavior doesn't manifest. (Phase 19v32 hit this exactly.)

**Fix:** Always `docker compose up -d --build` after `git pull`. Pass `--build-arg GIT_COMMIT=$(git rev-parse HEAD)` so the image bakes the sha. Then verify post-deploy via `/api/version` matching expected sha.

**See:** Phase 19v33 (PR family with `/api/version` + workflow version-gate).

## Process: always add a version-gate to deploys

Every deploy workflow on this repo ends with:

```python
import urllib.request, json, sys
expected = sys.argv[1].strip()
r = json.loads(urllib.request.urlopen('http://localhost:5050/api/version', timeout=5).read())
running = r.get('commit')
if running == expected:
    print('  OK running commit matches expected')
    sys.exit(0)
print(f'::error::Running commit ({running}) != expected ({expected}). Aborting.')
sys.exit(1)
```

Combined with `ARG GIT_COMMIT` baked into the Dockerfile, this catches the "merged but not running" silent-staleness class of bugs.
