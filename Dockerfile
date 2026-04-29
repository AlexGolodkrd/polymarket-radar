# plan-kapkan arbitrage radar — Phase 6 VPS deployment image.
#
# Build from repo root:
#   docker build -t plan-kapkan-radar .
#
# Run (dry-run mode, no keys needed):
#   docker run -p 5050:5050 -v $(pwd)/Executions:/app/Executions plan-kapkan-radar
#
# Run with wallet keys (after Phase 5 graduation):
#   docker run -p 5050:5050 \
#     -v $(pwd)/Executions:/app/Executions \
#     --env-file Credentials.env \
#     -e DRY_RUN=0 \
#     plan-kapkan-radar
#
# Two services run inside this image:
#   - arb_server.py  (the radar + Flask UI on :5050)
#   - watchdog.py    (polls .killed flag, cancels pending orders if radar dies)
# docker-compose.yml runs them as separate containers sharing Executions/.

FROM python:3.11-slim

# System deps:
#   - curl for healthcheck
#   - gcc + libc-dev for any wheel that doesn't ship a manylinux build
#     (eth-account pulls in a few)
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl gcc libc-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (this layer is cached as long as
# requirements.txt doesn't change — much faster rebuilds during dev)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the actual code. Order matches the layer-cache hierarchy:
# rarely-changed templates first, frequently-changed Python last.
COPY idea.md .
COPY Scripts/ ./Scripts/
COPY tests/ ./tests/

# Executions/ is created at runtime — bind-mounted from the host (see
# docker-compose.yml volumes block). Explicit mkdir keeps the path
# valid for fresh containers.
RUN mkdir -p /app/Executions

# Default port (Flask)
EXPOSE 5050

# Healthcheck — Docker restarts the container if /api/risk_status
# starts returning errors. 30s interval, 3 retries to settle on cold start.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsSL http://localhost:5050/api/risk_status > /dev/null || exit 1

# Non-root for safety. The Executions volume must be writable by uid 1000
# (default mount on Linux hosts is fine; on Windows/Mac compose will work
# without explicit chown).
RUN useradd -m -u 1000 radar && chown -R radar:radar /app
USER radar

# Phase 9ccc (29.04.2026) — gunicorn production WSGI replaces Flask dev
# server. Reasons (from flask-best-practices skill):
#   - dev server prints WARNING in production logs (looks unprofessional,
#     and IS a real concern: dev server has no graceful shutdown, no
#     proper signal handling, no worker recycling on memory leaks)
#   - gunicorn handles SIGTERM cleanly for docker stop / restart
#   - --threads supports our threaded scan_loop + WS callbacks
#   - --preload starts ws/scan threads ONCE in master process; workers
#     fork() with state already initialized → faster reload
#   - --max-requests 10000 + jitter recycles workers periodically →
#     drops accumulated cache fragmentation, prevents slow memory growth
#
# Single worker (-w 1) because:
#   - All radar state (scan_data, pools, caches) is in-process globals.
#     Multiple workers would each have their OWN scan loop → 4x API rate.
#   - threading inside one worker handles concurrency just fine.
#
# Falls back to dev server if RADAR_DEV=1 (for local debugging):
CMD ["sh", "-c", "if [ \"$RADAR_DEV\" = \"1\" ]; then python Scripts/arb_server.py; else cd Scripts && exec gunicorn -w 1 --threads 50 -b 0.0.0.0:5050 --timeout 300 --graceful-timeout 30 --max-requests 10000 --max-requests-jitter 1000 --access-logfile - --error-logfile - 'arb_server:app'; fi"]
