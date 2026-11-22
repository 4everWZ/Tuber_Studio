# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.11.8 AS uv
FROM node:24-bookworm-slim AS web-build
WORKDIR /build/frontend
RUN corepack enable && corepack prepare pnpm@10.28.1 --activate
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile --ignore-scripts
COPY frontend ./
RUN pnpm build

FROM node:24-bookworm-slim AS songs
WORKDIR /songs
COPY components/song_requests/package.json components/song_requests/package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund

FROM python:3.12-slim-bookworm AS core
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && install -d -o 65532 -g 65532 -m 0700 /data

WORKDIR /opt/tuber
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --no-cache
COPY LICENSE ./
COPY src ./src
COPY --from=web-build /build/src/tuber_studio/web_dist ./src/tuber_studio/web_dist
COPY config ./config
COPY assets ./assets
COPY components/song_requests/helper.cjs components/song_requests/matching.cjs ./components/song_requests/
COPY --from=songs /usr/local/bin/node /usr/local/bin/node
COPY --from=songs /songs/node_modules ./components/song_requests/node_modules
COPY deploy/omni/core_entrypoint.py deploy/omni/check_batch.py ./

ENV PATH="/opt/tuber/.venv/bin:$PATH" \
    PYTHONPATH="/opt/tuber/src" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TUBER_RUNTIME=/data \
    HOME=/data
EXPOSE 9020
USER 65532:65532
CMD ["python", "/opt/tuber/core_entrypoint.py"]

FROM core AS reading-worker
USER root
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
    TUBER_READING_BROWSER_CHANNEL=chromium-headless-shell \
    HOME=/tmp
RUN python -m playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/* \
    && install -d -o 65532 -g 65532 -m 0700 /run/tuber-reading
COPY deploy/omni/reading-worker/server.py /opt/tuber/reading_server.py
USER 65532:65532
CMD ["python", "/opt/tuber/reading_server.py"]

# The default target remains the workbench used by app-compose.yml.
FROM core AS workbench
