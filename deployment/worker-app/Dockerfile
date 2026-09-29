# Per-app worker image (D-072): the pinned worker base plus the target app's Python dependencies,
# so the agent and the verifier can run the app's tests with no network at run time.
# Built by scripts/app_image_up.sh; the build context holds only the app's pyproject.toml and
# install_deps.py (no heredoc: the local engine uses the classic builder).
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3-venv python3-pip \
 && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml /opt/app/pyproject.toml
COPY install_deps.py /opt/app/install_deps.py
ARG EXTRAS=dev
RUN python3 -m venv /opt/app-venv \
 && /opt/app-venv/bin/python /opt/app/install_deps.py "${EXTRAS}" \
 && /opt/app-venv/bin/python -m pytest --version
ENV PATH=/opt/app-venv/bin:$PATH
# Codex runs tools through `bash -lc`; a login shell rebuilds PATH from /etc/profile, which
# dropped the venv (measured 2026-09-28: `python: command not found`).
RUN printf 'export PATH=/opt/app-venv/bin:$PATH\n' > /etc/profile.d/amplai-venv.sh
# OpenCode in the app image (Work 031 D3, operator 2026-09-29): execution and verification
# share one pinned environment. Same install as deployment/worker-opencode/Dockerfile.
ARG OPENCODE_VERSION=1.17.13
RUN npm install -g "opencode-ai@${OPENCODE_VERSION}" && npm cache clean --force
USER 65534:65534
CMD ["python", "--version"]
