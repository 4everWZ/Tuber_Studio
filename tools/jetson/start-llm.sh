#!/usr/bin/env bash
# Run from a Jetson checkout. Downloads only the project's selected model.
set -euo pipefail
studio_root=$(cd "$(dirname "$0")/../.." && pwd -P)
model_dir="$studio_root/runtime/models"
model=MiniCPM5-1B-Q4_K_M.gguf
image=ghcr.io/4everwz/jetson-llama-cpp:r36.4-cu128-u24.04-sm87-d749821
mkdir -p "$model_dir"
if docker container inspect tuber-llm >/dev/null 2>&1; then
  echo 'tuber-llm already exists. Inspect its /props; this script will not replace it.' >&2
  exit 1
fi
if [[ ! -f "$model_dir/$model" ]]; then
  # An explicit mirror may be supplied where the official host is unreachable.
  model_base=${TUBER_HF_BASE:-https://huggingface.co}
  curl --fail --location --retry 2 \
    "$model_base/openbmb/MiniCPM5-1B-GGUF/resolve/main/$model" -o "$model_dir/$model.part"
  mv "$model_dir/$model.part" "$model_dir/$model"
fi
docker run -d --name tuber-llm --runtime nvidia --network host --restart unless-stopped \
  --label tuber-studio.service=llm -v "$model_dir:/models:ro" "$image" \
  llama-server -m "/models/$model" --alias MiniCPM5-1B --host 127.0.0.1 --port 9010 \
  --jinja --ctx-size 131072 --parallel 1 --fit off --n-gpu-layers 99 \
  --flash-attn on --cache-type-k q8_0 --cache-type-v q8_0 \
  --reasoning off --chat-template-kwargs '{"enable_thinking":false}' \
  --temp 0.7 --top-p 0.95 --min-p 0.0 --batch-size 256 --ubatch-size 64 --cache-ram 0
