#!/usr/bin/env bash
set -euo pipefail
studio_root=$(cd "$(dirname "$0")/../.." && pwd -P)
[[ $(realpath -m -- "$studio_root/tmp") == "$studio_root/tmp" ]] || exit 1
mkdir -p -- "$studio_root/tmp"
context=$(mktemp -d "$studio_root/tmp/ready-image.XXXXXX")
# packager owns a new output tree, so reserve the name then release the empty directory.
rmdir -- "$context"
python3 "$studio_root/tools/jetson/package-ready.py" --root "$studio_root" --output "$context"
bash "$studio_root/tools/jetson/build-image.sh"
docker build --network none --build-arg BASE_IMAGE=tuber-studio:r36.4 \
  -t tuber-studio:ready-r36.4 -f "$studio_root/tools/jetson/Dockerfile.ready" "$context"
echo "Ready image built. Launch: bash tools/jetson/run-ready.sh"
echo "Build context: $context (temporary; remove after retaining the image)"
