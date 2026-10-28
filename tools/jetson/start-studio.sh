#!/usr/bin/env bash
set -euo pipefail
studio_root=$(cd "$(dirname "$0")/../.." && pwd -P)
if docker container inspect tuber-studio >/dev/null 2>&1; then
  echo 'tuber-studio exists; use docker restart tuber-studio after a source update.' >&2
  exit 1
fi
mkdir -p "$studio_root/runtime"
if [[ ! -f "$studio_root/runtime/studio.json" ]]; then
  cp "$studio_root/config/studio.example.json" "$studio_root/runtime/studio.json"
fi
docker run -d --name tuber-studio --network host --restart unless-stopped \
  --label tuber-studio.service=backend --user "$(id -u):$(id -g)" \
  -v "$studio_root:/workspace/tuber" --entrypoint /bin/bash \
  dustynv/pytorch:2.7-r36.4.0-cu128-24.04 /workspace/tuber/tools/jetson/studio-entrypoint.sh
