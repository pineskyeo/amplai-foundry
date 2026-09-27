# AMPLAI V3 worker image + OpenCode server, layered on the pinned worker image so the claude/codex
# bytes (and their Work 016 qualification) stay identical. Built by scripts/opencode_container_up.sh.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

ARG OPENCODE_VERSION=1.17.13
USER root
RUN npm install -g "opencode-ai@${OPENCODE_VERSION}" && npm cache clean --force
USER 65534:65534
CMD ["opencode", "--version"]
