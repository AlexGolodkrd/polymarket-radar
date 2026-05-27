"""/api/version blueprint.

Extracted from arb_server.py in audit-28d (27.05.2026). The endpoint
returns the git commit + build timestamp baked into the running Docker
image, so the operator can confirm prod is on the expected code.

Image build sets these via build-args (see Dockerfile + deploy.yml):
    docker compose build \\
        --build-arg GIT_COMMIT=$(git rev-parse HEAD) \\
        --build-arg BUILD_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)

Fallback to 'unknown' for local dev (no build-args).
"""
from __future__ import annotations

import os
from typing import Any

from flask import Blueprint, jsonify

bp = Blueprint('radar_version', __name__)


# Bump `_PHASE_TAG` when introducing a new code-level phase so the
# `/api/version` payload distinguishes deploys that don't change git
# commit (e.g. config-only rollouts via env updates + restart).
_PHASE_TAG: str = 'audit-28'


@bp.route('/api/version')
def api_version() -> Any:
    """GET /api/version → {commit, commit_short, build_time, phase}.

    No auth required — the response carries no secrets, just build
    metadata. Operator + monitoring poll this to detect:
      1. Image drift (commit ≠ expected).
      2. Stale containers (build_time too old).
      3. Phase tag for human-readable changelog correlation.
    """
    commit = os.environ.get('GIT_COMMIT', 'unknown')
    return jsonify({
        'commit': commit,
        'commit_short': (commit or '')[:8],
        'build_time': os.environ.get('BUILD_TIME', 'unknown'),
        'phase': _PHASE_TAG,
    })
