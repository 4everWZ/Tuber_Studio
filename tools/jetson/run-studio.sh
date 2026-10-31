#!/usr/bin/env bash
# Repository convenience entry; all launches share one local-first image policy.
set -euo pipefail
studio_root=$(cd "$(dirname "$0")/../.." && pwd -P)
exec bash "$studio_root/tools/jetson/run-ready.sh" --source "$studio_root" --data "$studio_root/runtime" "$@"
