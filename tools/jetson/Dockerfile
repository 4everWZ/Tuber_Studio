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
COPY tools/evaluate_semantics.py ./tools/evaluate_semantics.py
COPY tools/jetson/tts/serve.py tools/jetson/tts/prepare.py tools/jetson/tts/requirements.txt tools/jetson/tts/tts_infer.yaml ./tools/jetson/tts/
COPY tools/jetson/initialize-data.py ./tools/jetson/initialize-data.py
RUN /opt/studio-venv/bin/python -m pip install --index-url "${PYPI_INDEX}" --no-deps -e . \
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
    TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    OMP_NUM_THREADS=2 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
EXPOSE 9020
ENTRYPOINT ["/opt/studio-venv/bin/python", "-m", "tuber_studio.stack"]
