#!/bin/bash
set -euo pipefail
cd /workspace/tuber/runtime/tts/GPT-SoVITS-v2
export GRADIO_ANALYTICS_ENABLED=False
export TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD=1
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-2}"
exec /workspace/tuber/runtime/tts/venv/bin/python \
  /workspace/tuber/tools/jetson/tts/serve.py \
  -c /workspace/tuber/runtime/tts/tts_infer.yaml -a 127.0.0.1 -p 9011
