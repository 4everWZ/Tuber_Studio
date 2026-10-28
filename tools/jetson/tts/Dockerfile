FROM dustynv/pytorch:2.7-r36.4.0-cu128-24.04

# Python dependencies and models live in the project's mounted runtime/tts.
# Only this project image receives the native audio decoder.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace/tuber
ENV GRADIO_ANALYTICS_ENABLED=False \
    TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    OMP_NUM_THREADS=2
ENTRYPOINT ["/bin/bash", "/workspace/tuber/tools/jetson/tts/entrypoint.sh"]
