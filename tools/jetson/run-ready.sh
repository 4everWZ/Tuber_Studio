#!/usr/bin/env bash
# Standalone launch helper: Docker and an NVIDIA-enabled Jetson are sufficient.
set -euo pipefail
image=tuber-studio:ready-r36.4
data=./tuber-data
name=tuber-studio
port=9020
replace=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --image) image=${2:?}; shift 2 ;;
    --data) data=${2:?}; shift 2 ;;
    --name) name=${2:?}; shift 2 ;;
    --port) port=${2:?}; shift 2 ;;
    --replace) replace=true; shift ;;
    *) echo 'Usage: run-ready.sh [--image IMAGE] [--data DIR] [--name NAME] [--port PORT] [--replace]' >&2; exit 2 ;;
  esac
done
if [[ $(id -u) == 0 ]]; then echo 'Run as the data owner, not root.' >&2; exit 1; fi
[[ "$name" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]+$ && "$port" =~ ^[0-9]+$ ]] || exit 2
(( port > 0 && port < 65536 )) || exit 2
proposed=$(realpath -m -- "$data")
case "$proposed" in /|"$HOME"|/etc|/home|/tmp|/var|/usr|/opt|/run|/dev|/proc|/sys) echo 'Use a dedicated application data directory.' >&2; exit 1 ;; esac
mkdir -p -- "$data"
data=$(realpath -e -- "$data")
[[ -d "$data" && -w "$data" ]] || exit 2
if docker inspect "$name" >/dev/null 2>&1; then
  actual=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/workspace/tuber/runtime"}}{{.Source}}{{end}}{{end}}' "$name")
  [[ "$replace" == true && "$actual" == "$data" ]] || { echo 'Existing container must own this data and require --replace.' >&2; exit 1; }
fi
security=(--read-only --cap-drop ALL --security-opt no-new-privileges=true --pids-limit 256
  --ulimit core=0:0 --user "$(id -u):$(id -g)"
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=256m --tmpfs /run:rw,nosuid,nodev,noexec,size=16m)
# An offline, CPU-only initialization pass gives bind mounts and named-volume
# launches the same missing-only initialization; no model is loaded here.
docker run --rm --pull=never --network none "${security[@]}" \
  --mount "type=bind,src=$data,dst=/workspace/tuber/runtime" "$image" --initialize-only
mounts=(--mount "type=bind,src=$data,dst=/workspace/tuber/runtime")
for relative in models tts/weights tts/pretrained tts/ref tts/genie nltk_data; do
  target="$data/$relative"
  [[ $(realpath -e -- "$target") == "$target" ]] || { echo 'Model directories cannot traverse links.' >&2; exit 1; }
  mounts+=(--mount "type=bind,src=$target,dst=/workspace/tuber/runtime/$relative,readonly")
done
network="$name-private"
if docker network inspect "$network" >/dev/null 2>&1; then
  owner=$(docker network inspect --format '{{index .Labels "tuber-studio.service"}}' "$network")
  driver=$(docker network inspect --format '{{.Driver}}' "$network")
  [[ "$owner" == stack-sandbox && "$driver" == bridge ]] || { echo 'Network is not owned by this application.' >&2; exit 1; }
else
  docker network create --driver bridge --label tuber-studio.service=stack-sandbox "$network" >/dev/null
fi
if docker inspect "$name" >/dev/null 2>&1; then docker stop --time 45 "$name"; docker rm "$name"; fi
docker run -d --pull=never --name "$name" --runtime nvidia "${security[@]}" "${mounts[@]}" \
  --network "$network" \
  --publish "127.0.0.1:$port:9020" --restart unless-stopped --stop-timeout 45 \
  --label tuber-studio.service=stack "$image"
echo "Workbench: http://127.0.0.1:$port"
echo "Administrator token: $data/admin-token (private; generated per installation)"
