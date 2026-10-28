#!/bin/bash
# Run inside the project image, with the repository at /workspace/tuber.
set -euo pipefail
cd /workspace/tuber
offline=false
if [[ "${1:-}" == "--offline" ]]; then offline=true; shift; fi
if [[ $# -ne 0 ]]; then echo "Usage: initialize.sh [--offline]" >&2; exit 2; fi
if [[ "$offline" == true && ! -x runtime/tts/venv/bin/python ]]; then
  echo "Offline prerequisite missing: runtime/tts/venv" >&2; exit 1
fi
environment_ready=false
if [[ -x runtime/tts/venv/bin/python ]] && runtime/tts/venv/bin/python -c \
  'import sys; from pathlib import Path; sys.path.insert(0, "tools/jetson/tts"); from prepare import verify_dependencies; verify_dependencies(Path("tools/jetson/tts/requirements.txt"))' >/dev/null 2>&1; then
  environment_ready=true
fi
if [[ "$offline" == false && "$environment_ready" == false ]]; then
  mkdir -p runtime/tts
  python3 -m venv --system-site-packages runtime/tts/venv
  # A nested venv does not inherit its parent venv's site-packages automatically.
  base_site=$(/opt/venv/bin/python -c 'import sysconfig; print(sysconfig.get_path("purelib"))')
  target_site=$(runtime/tts/venv/bin/python -c 'import sysconfig; print(sysconfig.get_path("purelib"))')
  printf '%s\n' "$base_site" > "$target_site/jetson-base.pth"
  export PIP_CONFIG_FILE=/dev/null
  export PIP_INDEX_URL="${TTS_PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
  export PIP_EXTRA_INDEX_URL=
  runtime/tts/venv/bin/python -m pip install --retries 1 --timeout 30 \
    -r tools/jetson/tts/requirements.txt
fi
args=()
if [[ "$offline" == true ]]; then args+=(--offline); fi
runtime/tts/venv/bin/python tools/jetson/tts/prepare.py "${args[@]}"
