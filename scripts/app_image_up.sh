#!/usr/bin/env bash
# Build the per-app worker image (D-072) and pin it by registry digest.
#
#   scripts/app_image_up.sh APP_ID REPO_PATH [EXTRAS]     # EXTRAS default: dev
#
# The base is the digest in deployment/local-container.json (claude/codex bytes unchanged).
# The build context is a temporary directory holding only REPO_PATH/pyproject.toml.
# Writes deployment/local-container-app-APP_ID.json. A new digest means the driver must be
# requalified in that image before it is registered (scripts/container_qualify.py --image).
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
APP="${1:?usage: app_image_up.sh APP_ID REPO_PATH [EXTRAS]}"
SRC="${2:?usage: app_image_up.sh APP_ID REPO_PATH [EXTRAS]}"
EXTRAS="${3:-dev}"
[[ "$APP" =~ ^[a-z0-9][a-z0-9-]{0,40}$ ]] || { echo "invalid APP_ID"; exit 2; }
[ -f "$SRC/pyproject.toml" ] || { echo "no $SRC/pyproject.toml"; exit 2; }
IMAGE="localhost:5000/amplai-worker-app-$APP"
BASE="$(python3 -c "import json;print(json.load(open('$REPO/deployment/local-container.json'))['image'])")"
CTX="$(mktemp -d)"
trap 'rm -rf "$CTX"' EXIT
cp "$REPO/deployment/worker-app/Dockerfile" "$REPO/deployment/worker-app/install_deps.py" "$CTX/"
cp "$SRC/pyproject.toml" "$CTX/pyproject.toml"
docker build --build-arg "BASE_IMAGE=$BASE" --build-arg "EXTRAS=$EXTRAS" -t "$IMAGE:local" "$CTX"
docker push "$IMAGE:local" >/dev/null
DIGEST="$(docker inspect --format '{{index .RepoDigests 0}}' "$IMAGE:local" | sed 's/.*@//')"
run() { docker run --rm --network none -e HOME=/home/agent "$IMAGE:local" "$@" 2>/dev/null | head -1; }
CODEX_V="$(run codex --version)"
PY_V="$(run python --version)"
PYPROJECT_SHA="$(shasum -a 256 "$SRC/pyproject.toml" | cut -d' ' -f1)"
python3 - "$IMAGE@$DIGEST" "$BASE" "$CODEX_V" "$PY_V" "$APP" "$EXTRAS" "$PYPROJECT_SHA" \
  "$REPO/deployment/local-container-app-$APP.json" <<'PY'
import datetime, json, sys
image, base, codex_v, py_v, app, extras, pyproject_sha, out = sys.argv[1:]
json.dump({
  "schema_version": "1.0",
  "engine": "docker",
  "runtime": "colima (lima VM) on macOS",
  "image": image,
  "base_image": base,
  "app": app,
  "extras": extras,
  "pyproject_sha256": pyproject_sha,
  "uid": 65534, "gid": 65534, "memory": "2g", "cpus": 2.0, "pids": 256, "network": "none",
  "tools": {"codex": codex_v, "python": py_v},
  "built_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
  "qualification": "local development profile; requalify the driver in this image before use",
}, open(out, "w"), indent=2)
print(open(out).read())
PY
