# Optional environment variant; helper source is part of the mounted program.
ARG TUBER_IMAGE=tuber-studio:environment-r36.4
FROM node:24-bookworm-slim AS component
WORKDIR /component
COPY components/song_requests/package.json components/song_requests/package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund

FROM ${TUBER_IMAGE}
USER root
COPY --from=component /usr/local/bin/node /usr/local/bin/node
COPY --from=component /component/node_modules /opt/tuber-song-node/node_modules
COPY --from=component /component/package-lock.json /opt/tuber-song-node/package-lock.json
ENV NODE_PATH=/opt/tuber-song-node/node_modules
USER 65532:65532
