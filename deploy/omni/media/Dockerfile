FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir fastapi==0.136.1 uvicorn==0.44.0 websockets==16.0 Pillow==12.1.1
WORKDIR /app
COPY --chmod=0644 bridge.py publisher.py ./
USER 65532:65532
CMD ["uvicorn", "bridge:app", "--host", "0.0.0.0", "--port", "8080", "--ws-max-size", "2097152"]
