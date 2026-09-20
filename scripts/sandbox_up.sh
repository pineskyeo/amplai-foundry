#!/usr/bin/env bash
# Bring up the local container runtime, build the worker image and pin it by registry digest.
#
#   scripts/sandbox_up.sh            # start colima, local registry, build+push, write profile
#   scripts/sandbox_up.sh --status   # show what is running and the pinned image
#   scripts/sandbox_up.sh --down     # stop the registry and colima (image and profile stay)
#
# ContainerSandbox (src/amplai_foundry/sandbox/container.py) accepts only
# <name>@sha256:<digest>, so the image goes through a local registry to obtain a repo digest.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
REGISTRY_NAME=amplai-registry
REGISTRY_PORT=5000
IMAGE=localhost:${REGISTRY_PORT}/amplai-worker
PROFILE="$REPO/deployment/local-container.json"

status() {
  echo "colima : $(colima status 2>&1 | head -1)"
  echo "docker : $(docker info --format '{{.ServerVersion}} {{.OperatingSystem}}' 2>/dev/null || echo 'not reachable')"
  echo "registry: $(docker ps --filter name=$REGISTRY_NAME --format '{{.Status}}' 2>/dev/null || echo 'not running')"
  [ -f "$PROFILE" ] && echo "profile : $(python3 -c "import json;print(json.load(open('$PROFILE'))['image'])")" || echo "profile : none"
}

case "${1:-up}" in
  --status) status; exit 0 ;;
  --down)
    docker rm -f "$REGISTRY_NAME" >/dev/null 2>&1 || true
    colima stop
    exit 0 ;;
esac

colima status >/dev/null 2>&1 || colima start --cpu 2 --memory 4 --disk 20
docker info >/dev/null
if ! docker ps --filter name="$REGISTRY_NAME" --format '{{.Names}}' | grep -q "$REGISTRY_NAME"; then
  docker rm -f "$REGISTRY_NAME" >/dev/null 2>&1 || true
  docker run -d --name "$REGISTRY_NAME" -p 127.0.0.1:${REGISTRY_PORT}:5000 registry:2 >/dev/null
fi
docker build -t "$IMAGE:local" "$REPO/deployment/worker"
docker push "$IMAGE:local" >/dev/null
DIGEST="$(docker inspect --format '{{index .RepoDigests 0}}' "$IMAGE:local" | sed 's/.*@//')"
CLAUDE_V="$(docker run --rm --network none "$IMAGE:local" claude --version 2>/dev/null | head -1)"
CODEX_V="$(docker run --rm --network none "$IMAGE:local" codex --version 2>/dev/null | head -1)"
python3 - "$IMAGE@$DIGEST" "$CLAUDE_V" "$CODEX_V" "$PROFILE" <<'PY'
import json, sys, datetime
image, claude_v, codex_v, out = sys.argv[1:]
json.dump({
  "schema_version": "1.0",
  "engine": "docker",
  "runtime": "colima (lima VM) on macOS",
  "image": image,
  "uid": 65534, "gid": 65534, "memory": "1g", "cpus": 1.0, "pids": 128, "network": "none",
  "tools": {"claude": claude_v, "codex": codex_v},
  "built_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
  "qualification": "local development profile; not a production qualification",
}, open(out, "w"), indent=2)
print(json.dumps(json.load(open(out)), indent=2))
PY
