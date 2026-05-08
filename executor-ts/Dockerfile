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

# Healthcheck — same pattern as radar service.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD wget -q -O - http://localhost:5051/healthz > /dev/null || exit 1

# Non-root for safety.
RUN addgroup -S exec && adduser -S exec -G exec -u 1001 \
    && chown -R exec:exec /app
USER exec

CMD ["node", "dist/server.js"]
