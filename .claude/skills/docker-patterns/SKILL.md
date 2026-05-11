# Docker Patterns

**Source**: affaan-m/everything-claude-code/.kiro/skills/docker-patterns

## Production-ready Compose patterns

### Local dev vs prod via override

```yaml
# docker-compose.yml (committed)
services:
  radar:
    image: plan-kapkan-radar
    build: .
    restart: unless-stopped
```

```yaml
# docker-compose.override.yml (gitignored, env-specific)
services:
  radar:
    ports: !override
      - "127.0.0.1:5050:5050"
    user: "1000:1000"
    environment:
      PYTHONUNBUFFERED: "1"
```

`docker compose up` automatically merges `override.yml` if present. Same pattern we use.

### Multi-stage Dockerfile

```dockerfile
# Stage 1: build deps
FROM python:3.11-slim AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

# Stage 2: runtime
FROM python:3.11-slim AS runner
WORKDIR /app
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH
COPY Scripts/ Scripts/
USER 1000:1000
CMD ["python", "Scripts/arb_server.py"]
```

**Win**: image size drops 30-50% (no pip toolchain in final).

## Networking

### Internal only (no public port)
```yaml
services:
  redis:
    image: redis:7-alpine
    expose: ["6379"]   # not `ports:` — only inside compose network
```

### Reverse proxy + backend
```yaml
services:
  nginx:
    ports: ["80:80", "443:443"]
    depends_on: [radar]
  radar:
    expose: ["5050"]   # accessed via nginx, not exposed to host
```

## Security hardening

| Concern | Pattern |
|---|---|
| Don't run as root | `USER 1000:1000` in Dockerfile |
| Drop unnecessary caps | `cap_drop: [ALL]` then `cap_add: [NET_BIND_SERVICE]` if needed |
| Read-only filesystem | `read_only: true` + named tmpfs for writable paths |
| No privilege escalation | `security_opt: [no-new-privileges:true]` |
| Resource limits | `mem_limit: 512m`, `cpus: 1.0` |

### Example secure compose

```yaml
services:
  radar:
    image: plan-kapkan-radar
    user: "1000:1000"
    read_only: true
    tmpfs:
      - /tmp
      - /app/Executions  # if writable needed
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    mem_limit: 1g
    cpus: 2.0
    restart: unless-stopped
```

## Debugging

### Container won't start

```bash
docker compose up        # foreground, shows errors
docker logs --tail=100 svc  # if up'd in background
docker compose config    # validates merged compose
```

### Code changes not picked up

Most common: `docker compose restart` only restarts existing container — doesn't rebuild image. After code changes:

```bash
docker compose down
docker compose up -d --build    # forces rebuild
```

OR for dev iteration: bind-mount `Scripts/` so changes are live:

```yaml
services:
  radar:
    volumes:
      - ./Scripts:/app/Scripts:ro
```

(But this defeats image immutability — only for dev.)

### Network issues

```bash
docker network ls
docker network inspect bridge
docker exec svc1 ping svc2     # test connectivity
docker exec svc1 nslookup svc2 # DNS resolution
```

## Anti-patterns

❌ **Latest tag in production** — `image:latest` mutates without notice. Use `image:1.2.3` or git SHA.

❌ **Secrets in `docker run -e`** — visible in `ps`/`docker inspect`. Use `--env-file` or Docker secrets.

❌ **Running stateful services in single-host Compose** — no failover. For HA, Kubernetes/Swarm.

❌ **`docker exec` for routine tasks** — should be in entrypoint or healthcheck.

## Application to plan-kapkan

### Already good
- ✅ `restart: unless-stopped`
- ✅ Port binding `127.0.0.1:5050:5050` (not 0.0.0.0)
- ✅ uid 1000 user
- ✅ `--env-file` not raw `-e` for secrets

### Should fix
- ❌ **No multi-stage Dockerfile** — image is bigger than needed
- ❌ **No image tag versioning** — running `latest` semantically
- ❌ **No memory/CPU limits** — a runaway scan could exhaust VPS
- ❌ **No cap_drop** — container has full Linux caps by default
- ❌ **Code-change → restart confusion** (we hit this today!) — restart doesn't rebuild

### Recommended addition to override.yml

```yaml
services:
  radar:
    mem_limit: 1g
    cpus: 2.0
    cap_drop: [ALL]
    cap_add: [NET_BIND_SERVICE]
    security_opt: [no-new-privileges:true]
```
