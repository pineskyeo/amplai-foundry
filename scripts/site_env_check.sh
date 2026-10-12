#!/bin/sh
# AMPLAI site environment check (read-only; no install, no sudo).
# Usage: sh site_env_check.sh [IMAGE]   -> writes amplai-env-<host>-<date>.txt in the current directory
#   IMAGE: a container image already present on this host (e.g. ubuntu:22.04, registry.access.redhat.com/ubi8/ubi).
#          Used only for the bind-mount write test. If omitted, the test is skipped.
OUT="amplai-env-$(hostname)-$(date +%Y%m%d-%H%M).txt"
IMAGE="$1"
exec > "$OUT" 2>&1

section() { printf '\n== %s ==\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }

section "OS"
cat /etc/os-release 2>/dev/null | grep -E '^(NAME|VERSION|ID)='
uname -srm
if grep -qi microsoft /proc/version 2>/dev/null; then echo "WSL: yes ($(cat /proc/version))"; else echo "WSL: no"; fi
ldd --version 2>/dev/null | head -1

section "Resources"
echo "cpus: $(nproc 2>/dev/null)"
free -m 2>/dev/null | head -2
df -h "$HOME" 2>/dev/null | tail -1

section "Python (need >= 3.11)"
for p in python3.13 python3.12 python3.11 python3; do
  if have "$p"; then echo "$p: $($p --version 2>&1) at $(command -v $p)"; fi
done
have uv && echo "uv: $(uv --version)" || echo "uv: not found"

section "git"
have git && git --version || echo "git: not found"

section "SELinux (RHEL bind mounts need relabel when enforcing)"
have getenforce && getenforce || echo "getenforce: not found"

section "Container engines"
for e in docker podman; do
  if have "$e"; then
    echo "-- $e: $($e --version 2>&1)"
    $e info 2>&1 | grep -Ei 'server version|version:|cgroup|rootless|security|storage driver|selinux' | head -12
  else
    echo "-- $e: not found"
  fi
done
id

section "Bind mount write test (uid 65534, read-only root, cap-drop ALL)"
ENGINE=""
have docker && docker info >/dev/null 2>&1 && ENGINE=docker
[ -z "$ENGINE" ] && have podman && ENGINE=podman
if [ -z "$IMAGE" ]; then
  echo "skipped: no IMAGE argument (run: sh site_env_check.sh <local image>)"
elif [ -z "$ENGINE" ]; then
  echo "skipped: no working docker/podman"
else
  for mode in 700 755 777; do
    D=$(mktemp -d "$HOME/.amplai-envcheck.XXXXXX")
    chmod "$mode" "$D"
    MOUNT="$D:/workspace"
    [ "$(getenforce 2>/dev/null)" = "Enforcing" ] && MOUNT="$MOUNT:Z"
    if $ENGINE run --rm --init --read-only --cap-drop=ALL --user 65534:65534 \
         --tmpfs /tmp --network none -v "$MOUNT" "$IMAGE" \
         sh -c 'echo ok > /workspace/probe && cat /workspace/probe' >/dev/null 2>&1; then
      echo "dir mode $mode: container uid 65534 CAN write (file owner on host: $(stat -c '%u' "$D/probe" 2>/dev/null))"
    else
      echo "dir mode $mode: container uid 65534 CANNOT write"
    fi
    rm -rf "$D" 2>/dev/null || echo "note: could not remove $D (owned by container uid); remove it manually"
  done
fi

section "Network reachability (HTTPS HEAD, no credentials sent)"
if have curl; then
  for u in https://api.anthropic.com https://api.openai.com https://chatgpt.com https://hooks.slack.com \
           https://slack.com https://github.com https://pypi.org https://files.pythonhosted.org \
           https://registry-1.docker.io https://ghcr.io; do
    code=$(curl -sS -o /dev/null -m 8 -I -w '%{http_code}' "$u" 2>/dev/null)
    echo "$u -> ${code:-fail}"
  done
  echo "proxy env: HTTPS_PROXY=${HTTPS_PROXY:-${https_proxy:-unset}}"
else
  echo "curl: not found"
fi

section "done"
echo "report: $OUT"
