#!/usr/bin/env bash
set -euo pipefail
studio_root=$(cd "$(dirname "$0")/../.." && pwd -P)
[[ $(realpath -m -- "$studio_root/tmp") == "$studio_root/tmp" ]] || exit 1
mkdir -p -- "$studio_root/tmp"
context=$(mktemp -d "$studio_root/tmp/environment-image.XXXXXX")
python3 "$studio_root/tools/jetson/environment.py" --root "$studio_root" --output "$context/environment.json" >/dev/null
docker build --network host --target ready --build-context "environment_contract=$context" \
  -t tuber-studio:environment-r36.4 -f "$studio_root/tools/jetson/Dockerfile" "$studio_root"
echo 'Environment built. Launch: bash tools/jetson/run-ready.sh --image tuber-studio:environment-r36.4'
echo "Build context: $context (temporary; remove after retaining the image)"
