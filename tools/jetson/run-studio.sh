#!/usr/bin/env bash
# Deploy one application container; models/data remain outside the image.
set -euo pipefail
studio_root=$(cd "$(dirname "$0")/../.." && pwd -P)
network_name=tuber-studio-private
if [[ $(id -u) == 0 ]]; then echo 'Run as the project owner with Docker access, not root.' >&2; exit 1; fi
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
runtime_root=$(realpath -e -- "$studio_root/runtime")
if [[ "$runtime_root" != "$studio_root/runtime" ]]; then
  echo 'Runtime must be the real directory inside this project.' >&2; exit 1
fi
readonly_data=(models tts/weights tts/pretrained tts/ref nltk_data)
for relative in "${readonly_data[@]}"; do
  target="$runtime_root/$relative"
  # Reject escaping/symlinked parents before creating any directory.
  if [[ $(realpath -m -- "$target") != "$target" ]]; then
    echo 'Inference data directories must not traverse links.' >&2; exit 1
  fi
  mkdir -p "$target"
done
if docker network inspect "$network_name" >/dev/null 2>&1; then
  network_owner=$(docker network inspect --format '{{index .Labels "tuber-studio.service"}}' "$network_name")
  network_driver=$(docker network inspect --format '{{.Driver}}' "$network_name")
  if [[ "$network_owner" != stack-sandbox || "$network_driver" != bridge ]]; then
    echo 'Refusing an existing network not owned by this project sandbox.' >&2; exit 1
  fi
else
  docker network create --driver bridge --label tuber-studio.service=stack-sandbox "$network_name" >/dev/null
fi
if [[ ! -f "$studio_root/runtime/studio.json" ]]; then
  cp "$studio_root/config/studio.example.json" "$studio_root/runtime/studio.json"
fi
# All downloads and checks use the exact final image/environment, without loading GPU tensors.
common_security=(--read-only --cap-drop ALL --security-opt no-new-privileges=true
  --pids-limit 256 --ulimit core=0:0 --network "$network_name" --user "$(id -u):$(id -g)"
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=256m --tmpfs /run:rw,nosuid,nodev,noexec,size=16m)
docker run --rm "${common_security[@]}" \
  -v "$studio_root/runtime:/workspace/tuber/runtime" \
  --entrypoint /opt/studio-venv/bin/python tuber-studio:r36.4 \
  /workspace/tuber/tools/jetson/initialize-data.py
for name in "${owned_gpu[@]}"; do
  docker update --restart=no "$name" >/dev/null
  docker stop --timeout 15 "$name" >/dev/null
done
if [[ "$existing" == true ]]; then docker stop --timeout 45 tuber-studio; docker rm tuber-studio; fi
readonly_mounts=()
for relative in "${readonly_data[@]}"; do
  readonly_mounts+=(--mount "type=bind,src=$runtime_root/$relative,dst=/workspace/tuber/runtime/$relative,readonly")
done
docker run -d --name tuber-studio --runtime nvidia "${common_security[@]}" \
  --publish 127.0.0.1:9020:9020 --restart unless-stopped --stop-timeout 45 \
  --label tuber-studio.service=stack \
  --mount "type=bind,src=$runtime_root,dst=/workspace/tuber/runtime" \
  "${readonly_mounts[@]}" tuber-studio:r36.4
