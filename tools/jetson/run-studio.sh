#!/usr/bin/env bash
# Repository convenience entry; all launches share one local-first image policy.
set -euo pipefail
studio_root=$(cd "$(dirname "$0")/../.." && pwd -P)
local_image=()
if docker image inspect tuber-studio:environment-r36.4 >/dev/null 2>&1; then
  local_image=(--image tuber-studio:environment-r36.4)
fi
exec bash "$studio_root/tools/jetson/run-ready.sh" --source "$studio_root" --data "$studio_root/runtime" "${local_image[@]}" "$@"
