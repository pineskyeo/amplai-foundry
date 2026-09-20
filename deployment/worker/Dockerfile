# AMPLAI V3 worker image: the CLI agents the ContainerSandbox launches.
# Built and pinned locally by scripts/sandbox_up.sh; nothing here is production qualified.
FROM node:22-bookworm-slim

ARG CLAUDE_CODE_VERSION=latest
ARG CODEX_VERSION=latest

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates python3 curl \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" "@openai/codex@${CODEX_VERSION}" \
 && npm cache clean --force

# The sandbox runs as an unprivileged uid (default 65534) with a tmpfs home; nothing is
# baked in that depends on a writable image layer.
RUN mkdir -p /workspace /home/agent && chmod 0777 /workspace /home/agent
WORKDIR /workspace
USER 65534:65534
ENTRYPOINT []
CMD ["claude", "--version"]
