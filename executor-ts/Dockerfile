# executor-ts — Phase TS-3 Node.js executor service.
#
# Runs alongside the Python radar (which becomes the detector-only
# service). Python POSTs FireRequest objects to this container's
# `POST /fire`. See docs/TS_REWRITE_PLAN.md §3.
#
# Build from repo root:
#   docker build -t plan-kapkan-executor-ts -f executor-ts/Dockerfile executor-ts/
#
# Run standalone (dry-run, no keys):
#   docker run -p 5051:5051 -v $(pwd)/Executions:/app/Executions \
#     -e DRY_RUN=1 plan-kapkan-executor-ts
#
# Run via compose alongside radar — see docker-compose.yml.

FROM node:20.18-alpine AS builder

WORKDIR /app

# Install deps first (cached layer if package.json unchanged).
COPY package.json package-lock.json* ./
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund

# Copy source + build TypeScript → JavaScript.
COPY tsconfig.json ./
COPY src/ ./src/
RUN npx tsc

# ── Runtime stage ──────────────────────────────────────────────────
FROM node:20.18-alpine

# Phase 19v33 parity — bake build-time stamps so /version can prove
# which commit is actually running. Set via build-arg:
#   docker build --build-arg GIT_COMMIT=$(git rev-parse HEAD) ...
ARG GIT_COMMIT=unknown
ARG BUILD_TIME=unknown
ENV GIT_COMMIT=${GIT_COMMIT}
ENV BUILD_TIME=${BUILD_TIME}

WORKDIR /app

# Copy production deps (NODE_ENV=production strips devDependencies).
COPY package.json package-lock.json* ./
RUN npm ci --omit=dev --no-audit --no-fund || npm install --omit=dev --no-audit --no-fund

# Copy compiled JS from builder.
COPY --from=builder /app/dist ./dist/

# Bind-mounted at runtime — see docker-compose.yml volumes.
RUN mkdir -p /app/Executions

EXPOSE 5051

# Healthcheck — explicit 127.0.0.1 because `localhost` resolves to ::1 (IPv6)
# in Alpine containers, but the Node server binds IPv4 only — every probe
# silently failed with "connection refused" (FailingStreak crept to 40+).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD wget -q -O - http://127.0.0.1:5051/healthz > /dev/null || exit 1

# Non-root for safety.
# Phase audit-2 (11.05.2026) — use the built-in `node` user (UID 1000)
# instead of creating a parallel `exec` user. Two reasons:
#  1. `node:alpine` images ship with `node` already at UID 1000, so
#     `adduser -u 1000` fails with "uid '1000' in use" — exactly the
#     build error v1 of this fix hit.
#  2. We need UID 1000 to match the radar container so both can read/
#     write the shared `./Executions` bind mount. Previously executor-ts
#     ran UID 1001 → EACCES on every `dryrun.jsonl` append → 100%
#     errored TS fires after #165 fixed leg-identifier propagation.
RUN chown -R node:node /app
USER node

CMD ["node", "dist/server.js"]
