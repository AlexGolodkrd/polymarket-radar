"""Phase 19v33 (08.05.2026) — /api/version endpoint + deploy verification.

Operator's 08.05.2026 incident: v29-v32 PRs all merged into main but
Fulham × Bournemouth Exact Score deals still active on production.
Root cause: Dockerfile uses `COPY Scripts/`, so radar code lives INSIDE
the image. `docker restart` (without --build) kept serving the stale
image — git pull on the host updated files the running container never
read. v29 outcome guard, v30 slug threshold, v31 env tunable, v32
exact-score scope — all pre-merged silently never active for ~2 hours
of production paper trading.

To prevent this class of silent staleness from EVER recurring:
  1. Dockerfile ARG GIT_COMMIT + BUILD_TIME → ENV. The git sha is now
     baked into every image at build time.
  2. docker-compose.yml passes the args through compose's build:args.
  3. /api/version exposes the baked commit so external observers can
     detect a stale running container.
  4. deploy.yml passes GIT_COMMIT=$(git rev-parse HEAD) on `compose
     build`, then after the healthcheck calls /api/version and asserts
     the running commit equals the just-pulled commit. Mismatch → fail
     the run loudly.

This test covers the endpoint contract. Workflow-level assertion is
exercised by the next live deploy.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Scripts'))


@pytest.fixture
def app_client(monkeypatch):
    """Set known GIT_COMMIT env so the endpoint returns deterministic data.

    Phase audit-28d (27.05.2026) — no longer reloads `arb_server`. The
    new `radar.api.version` blueprint reads env vars at REQUEST time, so
    monkeypatching the env is enough. Reloading the module was leaking
    state across test files (it left `sys.modules['arb_server']`
    pointing at a new object while existing `import arb_server`
    bindings in OTHER test files kept pointing at the old one — broke
    fire-dedup tests downstream)."""
    monkeypatch.setenv('GIT_COMMIT', 'abc1234567890def')
    monkeypatch.setenv('BUILD_TIME', '2026-05-08T12:34:56Z')
    import arb_server
    return arb_server.app.test_client()


def test_version_endpoint_returns_200(app_client):
    resp = app_client.get('/api/version')
    assert resp.status_code == 200


def test_version_endpoint_returns_full_commit(app_client):
    resp = app_client.get('/api/version')
    body = resp.get_json()
    assert body['commit'] == 'abc1234567890def'


def test_version_endpoint_returns_short_commit(app_client):
    resp = app_client.get('/api/version')
    body = resp.get_json()
    assert body['commit_short'] == 'abc12345'


def test_version_endpoint_returns_build_time(app_client):
    resp = app_client.get('/api/version')
    body = resp.get_json()
    assert body['build_time'] == '2026-05-08T12:34:56Z'


def test_version_endpoint_falls_back_to_unknown(monkeypatch):
    """Without GIT_COMMIT in env (local dev), endpoint returns 'unknown'.
    Workflow accepts 'unknown' on the first post-v33 deploy as a soft
    bootstrap; subsequent deploys must return a real sha.

    Phase audit-28d — same fix as `app_client`: don't reload arb_server.
    The new blueprint reads env at request time, so monkeypatch.delenv
    is sufficient."""
    monkeypatch.delenv('GIT_COMMIT', raising=False)
    monkeypatch.delenv('BUILD_TIME', raising=False)
    import arb_server
    client = arb_server.app.test_client()
    resp = client.get('/api/version')
    body = resp.get_json()
    assert body['commit'] == 'unknown'
    assert body['build_time'] == 'unknown'


def test_version_endpoint_includes_phase(app_client):
    """Phase tag for human-readable signoff. Bumped each major code
    phase. We don't pin the exact value (it bumps every major refactor);
    just assert that it's a non-empty string."""
    resp = app_client.get('/api/version')
    body = resp.get_json()
    assert isinstance(body.get('phase'), str)
    assert len(body['phase']) > 0
