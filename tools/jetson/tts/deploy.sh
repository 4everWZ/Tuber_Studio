#!/bin/bash
# Run on Jetson. Explicit --replace only replaces this project's TTS container.
set -euo pipefail
root=$(cd "$(dirname "$0")/../../.." && pwd -P)
image=tuber-studio-tts:r36.4
name=tuber-tts
offline=false
replace=false
for argument in "$@"; do
  case "$argument" in
    --offline) offline=true ;;
    --replace) replace=true ;;
    *) echo "Usage: deploy.sh [--offline] [--replace]" >&2; exit 2 ;;
  esac
done
if docker container inspect "$name" >/dev/null 2>&1; then
  if [[ "$replace" == false ]]; then
    echo "Container $name exists; use --replace after current work has stopped." >&2; exit 1
  fi
  mounted_root=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/workspace/tuber"}}{{.Source}}{{end}}{{end}}' "$name")
  if [[ "$mounted_root" != "$root" ]]; then
    echo "Refusing to replace $name: its project mount differs from $root" >&2; exit 1
  fi
fi
if ! docker image inspect "$image" >/dev/null 2>&1; then
  if [[ "$offline" == true ]]; then
    echo "Offline prerequisite missing: $image (build once online)." >&2; exit 1
  fi
  docker build --network host -t "$image" -f "$root/tools/jetson/tts/Dockerfile" "$root/tools/jetson/tts"
fi
arguments=()
if [[ "$offline" == true ]]; then arguments+=(--offline); fi
# Existing API keeps running until source, dependencies and models are ready.
docker run --rm --network host -v "$root:/workspace/tuber" \
  -e TTS_HF_BASE="${TTS_HF_BASE:-https://hf-mirror.com}" \
  -e TTS_PIP_INDEX_URL="${TTS_PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}" \
  --entrypoint /bin/bash "$image" \
  /workspace/tuber/tools/jetson/tts/initialize.sh "${arguments[@]}"
if docker container inspect "$name" >/dev/null 2>&1; then
  docker stop --timeout 15 "$name"
  docker rm "$name"
fi
docker run -d --name "$name" --runtime nvidia --network host \
  --restart unless-stopped --memory 4g \
  --label tuber-studio.service=tts -v "$root:/workspace/tuber" "$image"
