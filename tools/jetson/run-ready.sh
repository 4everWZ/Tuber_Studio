#!/usr/bin/env bash
# Program is mounted read-only; the image supplies only the reusable environment.
set -euo pipefail
image=
script_dir=$(cd "$(dirname "$0")" && pwd -P)
source_root=
for candidate in "$script_dir/program" "$script_dir/../.."; do
  if [[ -f "$candidate/src/tuber_studio/portable.py" ]]; then source_root=$(realpath -e -- "$candidate"); break; fi
done
data=./tuber-data
name=tuber-studio
port=9020
replace=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --image) image=${2:?}; shift 2 ;;
    --source) source_root=${2:?}; shift 2 ;;
    --data) data=${2:?}; shift 2 ;;
    --name) name=${2:?}; shift 2 ;;
    --port) port=${2:?}; shift 2 ;;
    --replace) replace=true; shift ;;
    *) echo 'Usage: run-ready.sh [--source PROGRAM_DIR] [--image IMAGE] [--data DIR] [--name NAME] [--port PORT] [--replace]' >&2; exit 2 ;;
  esac
done
[[ -n "$source_root" && -f "$source_root/src/tuber_studio/portable.py" ]] || { echo 'Extract program.tar.gz beside this launcher, or provide --source PROGRAM_DIR.' >&2; exit 2; }
source_root=$(realpath -e -- "$source_root")
if [[ -z "$image" ]]; then
  if [[ -f "$source_root/environment-image.txt" ]]; then image=$(cat "$source_root/environment-image.txt");
  else image=ghcr.io/4everwz/tuber_studio:environment-r36.4; fi
fi
[[ -n "$image" && "$image" != -* && "$image" != *[[:space:]]* ]] || exit 2
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
if ! docker image inspect "$image" >/dev/null 2>&1; then docker pull "$image"; fi
# Both preparation and service use the exact same local image, even if an
# operator pulls a newer mutable tag during initialization.
image=$(docker image inspect --format '{{.Id}}' "$image")
security=(--read-only --cap-drop ALL --security-opt no-new-privileges=true --pids-limit 256
  --ulimit core=0:0 --user "$(id -u):$(id -g)"
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=256m --tmpfs /run:rw,nosuid,nodev,noexec,size=16m)
program_mounts=()
# Mount only public application paths; never expose host .git/.env or its runtime.
for relative in src tools config assets third_party components pyproject.toml uv.lock evaluations/semantic-cases.json evaluations/speech-cases.json; do
  target="$source_root/$relative"
  [[ -e "$target" && $(realpath -e -- "$target") == "$target" ]] || { echo "Invalid program path: $relative" >&2; exit 1; }
  program_mounts+=(--mount "type=bind,src=$target,dst=/workspace/tuber/$relative,readonly")
done
metadata_key=$(docker run --rm --pull=never --network none "${security[@]}" "${program_mounts[@]}" \
  --mount "type=bind,src=$data,dst=/workspace/tuber/runtime" \
  --entrypoint /opt/studio-venv/bin/python "$image" /workspace/tuber/tools/jetson/prepare-launch.py)
[[ "$metadata_key" =~ ^[a-f0-9]{64}$ ]] || { echo 'Environment preflight did not return valid launch metadata.' >&2; exit 1; }
program_mounts+=(--mount "type=bind,src=$data/.portable-launch/$metadata_key,dst=/opt/tuber-builtin,readonly")
# First install downloads pinned assets and prepares the fixed voice before
# starting resident inference. Repeated launches reuse verified local files.
initial_network=(--network bridge)
mirror=()
if [[ -n "${TUBER_HF_BASE:-}" ]]; then mirror=(-e "TUBER_HF_BASE=$TUBER_HF_BASE"); fi
if [[ -n "${TUBER_DOWNLOAD_PROXY:-}" ]]; then mirror+=(-e "TUBER_DOWNLOAD_PROXY=$TUBER_DOWNLOAD_PROXY"); fi
docker run --rm --pull=never --runtime nvidia "${initial_network[@]}" "${mirror[@]}" "${security[@]}" "${program_mounts[@]}" \
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
docker run -d --pull=never --name "$name" --runtime nvidia "${security[@]}" "${program_mounts[@]}" "${mounts[@]}" \
  --network "$network" \
  --publish "127.0.0.1:$port:9020" --restart unless-stopped --stop-timeout 45 \
  --label tuber-studio.service=stack "$image"
echo "Workbench: http://127.0.0.1:$port"
echo "Administrator token: $data/admin-token (private; generated per installation)"
