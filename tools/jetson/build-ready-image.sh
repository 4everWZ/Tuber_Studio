#!/usr/bin/env bash
set -euo pipefail
studio_root=$(cd "$(dirname "$0")/../.." && pwd -P)
[[ $(realpath -m -- "$studio_root/tmp") == "$studio_root/tmp" ]] || exit 1
mkdir -p -- "$studio_root/tmp"
context=$(mktemp -d "$studio_root/tmp/ready-image.XXXXXX")
# packager owns a new output tree, so reserve the name then release the empty directory.
rmdir -- "$context"
python3 "$studio_root/tools/jetson/package-ready.py" --root "$studio_root" --output "$context"
docker build --network host --target ready --build-context "ready_assets=$context" \
  -t tuber-studio:ready-r36.4 -f "$studio_root/tools/jetson/Dockerfile" "$studio_root"
echo "Ready image built. Launch: bash tools/jetson/run-ready.sh"
echo "Build context: $context (temporary; remove after retaining the image)"
