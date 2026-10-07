#!/usr/bin/env bash
# Bring up the local container runtime, build the worker image and pin it by registry digest.
#
#   scripts/sandbox_up.sh            # start colima, local registry, build+push, write profile
#   scripts/sandbox_up.sh --status   # show what is running and the pinned image
#   scripts/sandbox_up.sh --down     # stop the registry and colima (image and profile stay)
#   scripts/sandbox_up.sh --egress   # start the allowlist egress sidecar and qualify it
#   scripts/sandbox_up.sh --egress-down
#   scripts/sandbox_up.sh --codex-home DIR   # copy ~/.codex/auth.json into a sandbox home (user-run)
#   scripts/sandbox_up.sh --opencode-home DIR [provider]  # copy one OpenCode login (user-run)
#
# Egress: the agent container joins only an --internal network (no route, no DNS out). The
# sidecar amplai-egress-proxy is the sole member with a bridge leg and it tunnels CONNECT
# only to deployment/local-egress.json allowlist entries. scripts/egress_qualify.py measures
# it and writes deployment/local-egress-qualification.json, the ref ContainerSandbox needs.
#
# ContainerSandbox (src/amplai_foundry/sandbox/container.py) accepts only
# <name>@sha256:<digest>, so the image goes through a local registry to obtain a repo digest.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
REGISTRY_NAME=amplai-registry
REGISTRY_PORT=5000
IMAGE=localhost:${REGISTRY_PORT}/amplai-worker
PROFILE="$REPO/deployment/local-container.json"
EGRESS_PROFILE="$REPO/deployment/local-egress.json"
INTERNAL_NET=amplai-internal
EGRESS_NET=amplai-egress
PROXY_NAME=amplai-egress-proxy
PROXY_PORT=3128
# exact host:port; deny wins. Add entries deliberately and re-run --egress to requalify.
ALLOW="api.anthropic.com:443 chatgpt.com:443 api.openai.com:443 auth.openai.com:443 opencode.ai:443"

status() {
  echo "colima : $(colima status 2>&1 | head -1)"
  echo "docker : $(docker info --format '{{.ServerVersion}} {{.OperatingSystem}}' 2>/dev/null || echo 'not reachable')"
  echo "registry: $(docker ps --filter name=$REGISTRY_NAME --format '{{.Status}}' 2>/dev/null || echo 'not running')"
  [ -f "$PROFILE" ] && echo "profile : $(python3 -c "import json;print(json.load(open('$PROFILE'))['image'])")" || echo "profile : none"
  echo "egress  : $(docker ps --filter name=$PROXY_NAME --format '{{.Status}}' 2>/dev/null || echo 'not running')"
  [ -f "$REPO/deployment/local-egress-qualification.json" ] && echo "egress qualification: $(python3 -c "import json;print(json.load(open('$REPO/deployment/local-egress-qualification.json'))['outcome'])")" || echo "egress qualification: none"
}

egress_up() {
  [ -f "$PROFILE" ] || { echo "run scripts/sandbox_up.sh first (no $PROFILE)"; exit 2; }
  IMAGE_PINNED="$(python3 -c "import json;print(json.load(open('$PROFILE'))['image'])")"
  if docker network inspect "$INTERNAL_NET" >/dev/null 2>&1; then
    # an existing network must really be internal (no route out); never trust the name alone
    [ "$(docker network inspect --format '{{.Internal}}' "$INTERNAL_NET")" = "true" ] \
      || { echo "$INTERNAL_NET exists but is not --internal; remove it first"; exit 2; }
  else
    docker network create --internal "$INTERNAL_NET" >/dev/null
  fi
  docker network inspect "$EGRESS_NET" >/dev/null 2>&1 || docker network create "$EGRESS_NET" >/dev/null
  docker rm -f "$PROXY_NAME" >/dev/null 2>&1 || true
  ALLOW_ARGS=""; for a in $ALLOW; do ALLOW_ARGS="$ALLOW_ARGS --allow $a"; done
  # the sidecar is as confined as the agent: read-only, no caps, unprivileged, pinned image
  # shellcheck disable=SC2086
  # --restart: the sidecar comes back with docker after a reboot (colima as a login service)
  docker run -d --name "$PROXY_NAME" --network "$EGRESS_NET" --read-only --cap-drop=ALL \
    --security-opt=no-new-privileges --user 65534:65534 --pids-limit 256 --memory 256m \
    --restart unless-stopped \
    --mount "type=bind,src=$REPO/deployment/egress,dst=/amplai-input/egress,readonly" \
    "$IMAGE_PINNED" python3 /amplai-input/egress/egress_proxy.py --listen 0.0.0.0:$PROXY_PORT $ALLOW_ARGS >/dev/null
  docker network connect "$INTERNAL_NET" "$PROXY_NAME"
  python3 - "$EGRESS_PROFILE" "$INTERNAL_NET" "$PROXY_NAME:$PROXY_PORT" $ALLOW <<'PY'
import json, sys
out, network, proxy, *allow = sys.argv[1:]
json.dump({"schema_version": "1.0", "name": "local-colima", "network": network, "proxy": proxy,
           "allow": sorted(allow), "sidecar": "amplai-egress-proxy (deployment/egress/egress_proxy.py)",
           "qualification": "local development egress profile; not a production qualification"},
          open(out, "w"), indent=2)
print(open(out).read())
PY
  for _ in $(seq 1 30); do  # wait until the sidecar actually listens, not a fixed sleep
    docker logs "$PROXY_NAME" 2>/dev/null | grep -q '"event": "listening"' && break
    sleep 0.5
  done
  "$REPO/.venv/bin/python" "$REPO/scripts/egress_qualify.py"
}

egress_down() {
  docker rm -f "$PROXY_NAME" >/dev/null 2>&1 || true
  docker network rm "$INTERNAL_NET" "$EGRESS_NET" >/dev/null 2>&1 || true
}

codex_home() {
  # A scoped copy of the ChatGPT-account credential for the sandbox HOME. Never the real
  # ~/.codex (design: no home credential mounts). The user runs this; the agent does not.
  dest="$1"; [ -n "$dest" ] || { echo "usage: --codex-home DIR"; exit 2; }
  [ -f "$HOME/.codex/auth.json" ] || { echo "no ~/.codex/auth.json (run: codex login)"; exit 2; }
  mkdir -p "$dest/.codex" && chmod 700 "$dest"
  # 644 because the container runs as uid 65534 and colima maps the bind owner to the host user;
  # the 700 parent keeps other host users out. Remove DIR when the qualification run is done.
  cp "$HOME/.codex/auth.json" "$dest/.codex/auth.json" && chmod 644 "$dest/.codex/auth.json"
  echo "sandbox codex home: $dest (auth.json copied; uid 65534 must read it inside the container)"
}

opencode_home() {
  # A scoped copy of ONE OpenCode provider login for the sandbox data dir. Never the real
  # ~/.local/share/opencode (design: no home credential mounts). The user runs this; the agent
  # does not. Only the named provider entry is copied (default opencode-go), not every login.
  dest="$1"; provider="${2:-opencode-go}"
  [ -n "$dest" ] || { echo "usage: --opencode-home DIR [provider]"; exit 2; }
  src="${XDG_DATA_HOME:-$HOME/.local/share}/opencode/auth.json"
  [ -f "$src" ] || { echo "no $src (run: opencode auth login)"; exit 2; }
  mkdir -p "$dest/.local/share/opencode" && chmod 700 "$dest"
  python3 - "$src" "$dest/.local/share/opencode/auth.json" "$provider" <<'PY'
import json, os, sys
src, out, provider = sys.argv[1:]
logins = json.load(open(src))
if provider not in logins:
    sys.exit(f"provider {provider!r} not logged in; available: {sorted(logins)}")
fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
with os.fdopen(fd, "w") as f:
    json.dump({provider: logins[provider]}, f)
PY
  # 644 because the container runs as uid 65534 and colima maps the bind owner to the host user;
  # the 700 parent keeps other host users out. Remove DIR when the qualification run is done.
  chmod 644 "$dest/.local/share/opencode/auth.json"
  echo "sandbox opencode data dir: $dest (only '$provider' copied; mounted as the sandbox HOME)"
}

case "${1:-up}" in
  --status) status; exit 0 ;;
  --down)
    egress_down
    docker rm -f "$REGISTRY_NAME" >/dev/null 2>&1 || true
    colima stop
    exit 0 ;;
  --egress) egress_up; exit $? ;;
  --egress-down) egress_down; exit 0 ;;
  --codex-home) codex_home "${2:-}"; exit 0 ;;
  --opencode-home) opencode_home "${2:-}" "${3:-}"; exit 0 ;;
esac

colima status >/dev/null 2>&1 || colima start --cpu 2 --memory 4 --disk 20
docker info >/dev/null
if ! docker ps --filter name="$REGISTRY_NAME" --format '{{.Names}}' | grep -q "$REGISTRY_NAME"; then
  docker rm -f "$REGISTRY_NAME" >/dev/null 2>&1 || true
  docker run -d --name "$REGISTRY_NAME" --restart unless-stopped \
    -p 127.0.0.1:${REGISTRY_PORT}:5000 registry:2 >/dev/null
fi
# CLAUDE_CODE_VERSION / CODEX_VERSION pin the CLIs (an unpinned "latest" layer stays cached)
docker build --build-arg "CLAUDE_CODE_VERSION=${CLAUDE_CODE_VERSION:-latest}" \
  --build-arg "CODEX_VERSION=${CODEX_VERSION:-latest}" -t "$IMAGE:local" "$REPO/deployment/worker"
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
