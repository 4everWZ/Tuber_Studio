ARG LLAMA_IMAGE=ghcr.io/4everwz/jetson-llama-cpp:r36.4-cu128-u24.04-sm87-d749821
ARG CUDA_IMAGE=dustynv/pytorch:2.7-r36.4.0-cu128-24.04
FROM ${LLAMA_IMAGE} AS llama
FROM ${CUDA_IMAGE} AS tts-source
ARG TTS_TAG=20240821v2
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && git clone --depth 1 --branch "${TTS_TAG}" https://github.com/RVC-Boss/GPT-SoVITS.git /opt/GPT-SoVITS-v2 \
    && rm -rf /opt/GPT-SoVITS-v2/.git
FROM ${CUDA_IMAGE}

# Reuse the original CUDA/PyTorch image; do not copy a second CUDA stack.
COPY --from=llama /opt/llama.cpp/bin/llama-server /opt/llama/bin/llama-server
COPY --from=tts-source /opt/GPT-SoVITS-v2 /opt/GPT-SoVITS-v2
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace/tuber
COPY pyproject.toml uv.lock ./
COPY tools/jetson/tts/requirements.txt /tmp/tts-requirements.txt
ARG PYPI_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
ENV UV_CACHE_DIR=/tmp/uv-cache \
    PIP_CONFIG_FILE=/dev/null \
    PIP_EXTRA_INDEX_URL=
RUN /opt/venv/bin/uv venv --seed --system-site-packages --python /usr/bin/python3.12 /opt/studio-venv \
    && base_site=$(/opt/venv/bin/python -c 'import sysconfig; print(sysconfig.get_path("purelib"))') \
    && target_site=$(/opt/studio-venv/bin/python -c 'import sysconfig; print(sysconfig.get_path("purelib"))') \
    && printf '%s\n' "$base_site" > "$target_site/jetson-base.pth" \
    && /opt/venv/bin/python -c 'from importlib.metadata import version; print("torch=="+version("torch")); print("torchaudio=="+version("torchaudio")); print("torchvision=="+version("torchvision"))' > /tmp/native-torch.txt \
    && /opt/venv/bin/uv export --locked --no-dev --no-emit-project --no-hashes --format requirements-txt -o /tmp/studio-app.txt \
    && /opt/studio-venv/bin/python -m pip install --index-url "${PYPI_INDEX}" \
       -c /tmp/native-torch.txt -r /tmp/studio-app.txt -r /tmp/tts-requirements.txt \
    && /opt/studio-venv/bin/python -c 'from importlib.metadata import distribution; assert str(distribution("torch").locate_file("")).startswith("/opt/venv/"), "Original CUDA torch must be reused"' \
    && rm -rf /tmp/uv-cache /tmp/studio-app.txt /tmp/native-torch.txt /tmp/tts-requirements.txt

COPY src ./src
COPY config ./config
COPY assets ./assets
COPY third_party ./third_party
COPY evaluations/semantic-cases.json ./evaluations/semantic-cases.json
COPY evaluations/speech-cases.json ./evaluations/speech-cases.json
COPY tools/evaluate_semantics.py ./tools/evaluate_semantics.py
COPY tools/evaluate_context.py ./tools/evaluate_context.py
COPY tools/evaluate_tts_memory.py ./tools/evaluate_tts_memory.py
COPY tools/evaluate_tts_candidates.py ./tools/evaluate_tts_candidates.py
COPY tools/evaluate_tts_parity.py ./tools/evaluate_tts_parity.py
COPY tools/evaluate_genie_tts.py ./tools/evaluate_genie_tts.py
COPY tools/evaluate_live.py ./tools/evaluate_live.py
COPY tools/jetson/tts/serve.py tools/jetson/tts/server.py tools/jetson/tts/compact.py tools/jetson/tts/inference_only.py tools/jetson/tts/prepare.py tools/jetson/tts/requirements.txt tools/jetson/tts/tts_infer.yaml ./tools/jetson/tts/
COPY tools/jetson/initialize-data.py ./tools/jetson/initialize-data.py
COPY tools/jetson/tts/install-candidates.py ./tools/jetson/tts/install-candidates.py
# Dependencies were already installed from the locked export. This image owns a
# read-only source tree; preserve the same import path without rebuilding an
# editable wheel (and downloading build backends) on every source update.
RUN /opt/studio-venv/bin/python -c "from pathlib import Path; import sysconfig; Path(sysconfig.get_path('purelib'), 'tuber-studio-source.pth').write_text('/workspace/tuber/src' + chr(10))" \
    && /opt/studio-venv/bin/python -c "from pathlib import Path; from tuber_studio.config import ROOT; assert ROOT == Path('/workspace/tuber')" \
    && /opt/studio-venv/bin/python tools/evaluate_tts_candidates.py --help > /dev/null \
    && /opt/studio-venv/bin/python tools/evaluate_tts_parity.py --help > /dev/null \
    && /opt/studio-venv/bin/python tools/jetson/tts/install-candidates.py --help > /dev/null \
    && rm -rf /tmp/uv-cache /root/.cache/pip

ENV PATH=/opt/studio-venv/bin:/opt/llama/bin:$PATH \
    TUBER_ROOT=/workspace/tuber \
    TUBER_GPT_SOVITS_ROOT=/opt/GPT-SoVITS-v2 \
    NLTK_DATA=/workspace/tuber/runtime/nltk_data \
    HOME=/workspace/tuber/runtime \
    USER=tuber \
    LOGNAME=tuber \
    XDG_CACHE_HOME=/workspace/tuber/runtime/cache \
    NUMBA_CACHE_DIR=/workspace/tuber/runtime/cache/numba \
    MPLCONFIGDIR=/workspace/tuber/runtime/cache/matplotlib \
    TORCH_HOME=/workspace/tuber/runtime/cache/torch \
    GRADIO_TEMP_DIR=/workspace/tuber/runtime/tmp \
    GRADIO_ANALYTICS_ENABLED=False \
    DO_NOT_TRACK=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    OMP_NUM_THREADS=2 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
EXPOSE 9020
# Normal deployment overrides this with the host project owner's numeric UID/GID.
# A direct image launch must not silently inherit the CUDA base image's root user.
USER 65532:65532
ENTRYPOINT ["/opt/studio-venv/bin/python", "-m", "tuber_studio.stack"]
