#!/usr/bin/env bash
set -euo pipefail
cd /workspace/tuber
export UV_CACHE_DIR=/workspace/tuber/runtime/uv-cache
export UV_PROJECT_ENVIRONMENT=/workspace/tuber/runtime/backend-venv
# Reconcile partial first installs and changed lockfiles on every container start.
/opt/venv/bin/uv sync --locked --no-dev --python /usr/bin/python3.12
exec "$UV_PROJECT_ENVIRONMENT/bin/python" -m tuber_studio --host 127.0.0.1 --port 9020
