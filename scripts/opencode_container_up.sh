#!/usr/bin/env bash
# Build the OpenCode worker image on top of the pinned worker image and pin it by registry digest.
#
#   scripts/opencode_container_up.sh     # needs scripts/sandbox_up.sh to have run (registry up)
#
# The base is the digest in deployment/local-container.json, so claude/codex bytes do not move.
# Writes deployment/local-container-opencode.json, the profile scripts/opencode_qualify.py
# --container reads.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE=localhost:5000/amplai-worker-opencode
OPENCODE_VERSION="${OPENCODE_VERSION:-1.17.13}"
BASE="$(python3 -c "import json;print(json.load(open('$REPO/deployment/local-container.json'))['image'])")"
docker build --build-arg "BASE_IMAGE=$BASE" --build-arg "OPENCODE_VERSION=$OPENCODE_VERSION" \
  -t "$IMAGE:local" "$REPO/deployment/worker-opencode"
docker push "$IMAGE:local" >/dev/null
DIGEST="$(docker inspect --format '{{index .RepoDigests 0}}' "$IMAGE:local" | sed 's/.*@//')"
OPENCODE_V="$(docker run --rm --network none -e HOME=/home/agent "$IMAGE:local" opencode --version 2>/dev/null | head -1)"
python3 - "$IMAGE@$DIGEST" "$BASE" "$OPENCODE_V" "$REPO/deployment/local-container-opencode.json" <<'PY'
import datetime, json, sys
image, base, opencode_v, out = sys.argv[1:]
json.dump({
  "schema_version": "1.0",
  "engine": "docker",
  "runtime": "colima (lima VM) on macOS",
  "image": image,
  "base_image": base,
  "uid": 65534, "gid": 65534, "memory": "1g", "cpus": 1.0, "pids": 128, "network": "none",
  "tools": {"opencode": opencode_v},
  "built_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
  "qualification": "local development profile; not a production qualification",
}, open(out, "w"), indent=2)
print(open(out).read())
PY
