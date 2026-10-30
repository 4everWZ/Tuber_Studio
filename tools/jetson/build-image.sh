#!/usr/bin/env bash
set -euo pipefail
studio_root=$(cd "$(dirname "$0")/../.." && pwd -P)
docker build --network host --target program-base -t tuber-studio:r36.4 \
  -f "$studio_root/tools/jetson/Dockerfile" "$studio_root"
