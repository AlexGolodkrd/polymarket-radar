# Docker Management

**Source**: NousResearch/hermes-agent/optional-skills/devops/docker-management

## Overview

Manage Docker containers, images, volumes, networks, and Compose stacks using standard Docker CLI. No deps beyond Docker itself.

## Prerequisites

- Docker Engine running
- User in `docker` group (or use `sudo`)
- Docker Compose v2 (`docker compose`, not `docker-compose`)

## Quick reference

| Action | Command |
|---|---|
| Run container | `docker run -d --name foo image:tag` |
| Stop container | `docker stop foo` |
| Logs (follow) | `docker logs -f foo` |
| Exec inside | `docker exec -it foo bash` |
| Build image | `docker build -t myimg:tag .` |
| Compose up | `docker compose up -d --build` |
| Compose restart | `docker compose restart svc` |
| Disk usage | `docker system df` |
| Clean unused | `docker system prune -af` |

## Procedures

### 1. Container operations

```bash
docker run -d \
  --name plan-kapkan-radar \
  --restart unless-stopped \
  -p 127.0.0.1:5050:5050 \
  --env-file Credentials.env \
  plan-kapkan-radar:latest
```

### 2. Image management

- Pull: `docker pull image:tag`
- Build with cache bust: `docker build --no-cache -t img .`
- Push: `docker push registry/img:tag`
- Cleanup dangling: `docker image prune`

### 3. Docker Compose workflows

```yaml
services:
  radar:
    build: .
    image: plan-kapkan-radar
    ports: ["127.0.0.1:5050:5050"]
    env_file: Credentials.env
    restart: unless-stopped
```

`docker compose up -d --build` — rebuild + restart.

### 4. Volumes and networks

```bash
docker volume create myvol
docker volume prune
docker network create mynet
```

## Common pitfalls

| Symptom | Cause | Fix |
|---|---|---|
| `port already in use` | Another container or host process bound to same port | `docker ps -a`, `lsof -i :PORT` |
| `permission denied` on volume | uid/gid mismatch between container and host | `chown -R 1000:1000 /host/path` or set `user: "1000:1000"` in compose |
| Build cache stale | Code change after FROM but cache hits unrelated layer | `docker build --no-cache` |
| Image too big | All layers in one stage | Use **multi-stage build** with `FROM ... AS builder` and `FROM ... AS runner` |

## Verification after deploy

```bash
docker ps --format '{{.Names}} {{.Status}}'   # all containers up?
docker logs --tail=50 plan-kapkan-radar       # recent logs
ss -tlnp | grep 5050                           # port listening?
docker exec plan-kapkan-radar python -c 'import requests'  # deps OK?
```

## Dockerfile optimization tips

1. **Multi-stage**: separate build deps from runtime
2. **Layer ordering**: COPY requirements.txt → pip install → COPY rest. Source changes don't bust pip cache.
3. **`.dockerignore`**: exclude `.git`, `__pycache__`, tests when not needed
4. **Pin base image versions**: `python:3.11-slim` not `python:slim`
5. **Run as non-root**: `USER 1000:1000` in final stage
6. **Slim base images**: alpine for static binaries, slim for Python

## Application to plan-kapkan

Already done:
- ✅ docker-compose with override (port bind 127.0.0.1, uid 1000)
- ✅ python:3.11-slim base
- ✅ `restart: unless-stopped`

Could improve:
- ❌ Multi-stage build (current single-stage installs full pip toolchain in image)
- ❌ `.dockerignore` audit (likely includes `Executions/`, `tests/`)
- ❌ Pin Python version with patch level (`python:3.11.10-slim`)
- ❌ Add `HEALTHCHECK` directive in Dockerfile

## Quick wins for our project

```bash
# Show container resource usage
docker stats plan-kapkan-radar

# Stream logs filtered by phase markers
docker logs -f plan-kapkan-radar | grep -E '\[MAIN\]|\[POLY\]|\[LIM\]|chunk'

# Force rebuild without cache (when scratching head why code change isn't picked up)
cd /home/arb/plan-kapkan && docker compose down && docker compose up -d --build --no-cache

# Inspect what's inside an image
docker exec plan-kapkan-radar ls -la /app/Scripts
```
