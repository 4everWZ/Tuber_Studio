# Build an optional variant from the matching Tuber Studio program image.
# The regular program image remains free of Node and these npm dependencies.
ARG TUBER_IMAGE=tuber-studio:ready
FROM node:24-bookworm-slim AS component
WORKDIR /component
COPY components/song_requests/package.json components/song_requests/package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund
COPY components/song_requests/*.cjs ./
COPY components/song_requests/README.md ./

FROM ${TUBER_IMAGE}
USER root
COPY --from=component /usr/local/bin/node /usr/local/bin/node
COPY --from=component /component /workspace/tuber/components/song_requests
USER 65532:65532
