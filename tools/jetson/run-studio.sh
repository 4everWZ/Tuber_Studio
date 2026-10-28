#!/usr/bin/env bash
# Deploy one application container; models/data remain outside the image.
set -euo pipefail
studio_root=$(cd "$(dirname "$0")/../.." && pwd -P)
replace=false
existing=false
if [[ "${1:-}" == --replace ]]; then replace=true; shift; fi
if [[ $# -ne 0 ]]; then echo 'Usage: run-studio.sh [--replace]' >&2; exit 2; fi
if docker container inspect tuber-studio >/dev/null 2>&1; then
  existing=true
  mounted_root=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/workspace/tuber/runtime"}}{{.Source}}{{end}}{{end}}' tuber-studio)
  legacy_root=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/workspace/tuber"}}{{.Source}}{{end}}{{end}}' tuber-studio)
  if [[ "$replace" != true || ( "$mounted_root" != "$studio_root/runtime" && "$legacy_root" != "$studio_root" ) ]]; then
    echo 'Existing container must belong to this runtime and --replace must be explicit.' >&2; exit 1
  fi
fi
owned_gpu=()
for name in tuber-llm tuber-llm2b tuber-tts; do
  if ! docker container inspect "$name" >/dev/null 2>&1; then continue; fi
  model_mount=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/models"}}{{.Source}}{{end}}{{end}}' "$name")
  project_mount=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/workspace/tuber"}}{{.Source}}{{end}}{{end}}' "$name")
  if [[ "$model_mount" != "$studio_root/runtime/models" && "$project_mount" != "$studio_root" ]]; then
    echo "Refusing migration: $name does not belong to this project." >&2; exit 1
  fi
  owned_gpu+=("$name")
done
mkdir -p "$studio_root/runtime"
if [[ ! -f "$studio_root/runtime/studio.json" ]]; then
  cp "$studio_root/config/studio.example.json" "$studio_root/runtime/studio.json"
fi
# All downloads and checks use the exact final image/environment, without loading GPU tensors.
docker run --rm --network host --user "$(id -u):$(id -g)" \
  -v "$studio_root/runtime:/workspace/tuber/runtime" \
  --entrypoint /opt/studio-venv/bin/python tuber-studio:r36.4 \
  /workspace/tuber/tools/jetson/initialize-data.py
for name in "${owned_gpu[@]}"; do
  docker update --restart=no "$name" >/dev/null
  docker stop --timeout 15 "$name" >/dev/null
done
if [[ "$existing" == true ]]; then docker stop --timeout 45 tuber-studio; docker rm tuber-studio; fi
docker run -d --name tuber-studio --runtime nvidia --network host \
  --restart unless-stopped --stop-timeout 45 --user "$(id -u):$(id -g)" \
  --label tuber-studio.service=stack \
  -v "$studio_root/runtime:/workspace/tuber/runtime" tuber-studio:r36.4
