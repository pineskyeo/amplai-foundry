"""Work 031 M1/M2: can the agent's tools read the OpenCode server password? (measurement)

Starts ``opencode serve`` in the pinned image on the qualified egress profile (the same
ContainerSandbox argv as ``opencode_qualify.py --container``) but WITH a random
``OPENCODE_SERVER_PASSWORD`` and the B+C mitigation from specs/031-opencode-driver/design.md:

- B: a ``shell.env`` plugin from a read-only config dir that blanks the variables (and sets a
  marker so the plugin's effect is observable),
- C: ``SHELL`` points at a read-only wrapper that unsets the variables before ``exec bash``.

Measured: (c) unauthenticated requests, (a) what one real bash-tool turn can see, /proc
readability as the agent's uid, (b) what can and cannot be said about formatter/git children,
(d) real SSE event order, heartbeat and whether a reconnect replays. One model turn.
The password is a throwaway random value; nothing is printed except redacted booleans/names.

    .venv/bin/python scripts/opencode_auth_probe.py \
        --opencode-home ~/.amplai-sandbox-probes/opencode-home
"""

# ruff: noqa: E501, E741  (a one-off measurement script with embedded shell)
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from opencode_qualify import ContainerServer, DockerExecTransport, Turns, _curl_quote

REPO = Path(__file__).resolve().parents[1]
NAME = "amplai-m1-opencode-server"
MODEL = "opencode-go/glm-5.3-flash"
VARS = ("OPENCODE_SERVER_PASSWORD", "OPENCODE_SERVER_USERNAME")

PLUGIN = """\
export default async () => ({
  "shell.env": async (_input, output) => {
    output.env.OPENCODE_SERVER_PASSWORD = "";
    output.env.OPENCODE_SERVER_USERNAME = "";
    output.env.AMPLAI_PLUGIN_LOADED = "1";
  },
});
"""
WRAPPER = """\
#!/bin/sh
unset OPENCODE_SERVER_PASSWORD OPENCODE_SERVER_USERNAME
exec /bin/bash "$@"
"""
# Prints variable names and 0/1 flags only, never a value.
PROBE_SH = r"""
echo "== tool SHELL=$SHELL marker=${AMPLAI_PLUGIN_LOADED:-unset}"
echo "== tool env names"; env | cut -d= -f1 | sort | tr '\n' ' '; echo
echo "== tool env OPENCODE_SERVER_* lines: $(env | grep -c '^OPENCODE_SERVER_')"
filled=$(env | grep -c '^OPENCODE_SERVER_PASSWORD=.')
echo "== tool env OPENCODE_SERVER_PASSWORD non-empty: $filled"
pid=$$
while [ "$pid" -gt 1 ] 2>/dev/null; do
  cmd=$(tr '\0' ' ' < /proc/$pid/cmdline 2>/dev/null | cut -c1-60)
  if [ -r /proc/$pid/environ ]; then
    readable=yes
    named=$(tr '\0' '\n' < /proc/$pid/environ 2>/dev/null | grep -c '^OPENCODE_SERVER_PASSWORD=')
    filled=$(tr '\0' '\n' < /proc/$pid/environ 2>/dev/null | grep -c '^OPENCODE_SERVER_PASSWORD=.')
  else
    readable=no; named=-; filled=-
  fi
  echo "== ancestor pid=$pid cmd=[$cmd] environ_readable=$readable has_var=$named nonempty=$filled"
  pid=$(awk '{print $4}' /proc/$pid/stat 2>/dev/null)
done
fmts=$(for f in prettier black ruff gofmt rustfmt clang-format; do command -v $f; done)
echo "== formatters on PATH: $(echo $fmts)"
"""


class MitigatedServer(ContainerServer):
    NAME = NAME

    def __init__(self, root: Path, home: Path, cfg: Path, profile: Path) -> None:
        super().__init__(root, "AMPLAI_M1_CANARY", "canary-" + secrets.token_hex(8), home, profile)
        self.cfg = cfg
        self.password = secrets.token_urlsafe(24)
        self.transport = DockerExecTransport(self.NAME, self.PORT, auth=("opencode", self.password))

    def argv(self) -> list[str]:
        cmd = self.sandbox.command(
            ["opencode", "serve", "--port", str(self.PORT), "--hostname", "127.0.0.1"],
            self.ws,
            self.NAME,
            env_names=list(VARS),
            native_home=self.home,
            readonly_mounts={"/amplai-input/oc-config": self.cfg},
        )
        at = cmd.index(self.profile.image)
        extra = [
            "--env", "OPENCODE_CONFIG_DIR=/amplai-input/oc-config",
            "--env", "SHELL=/amplai-input/oc-config/shell",
        ]  # fmt: skip
        return [cmd[0], cmd[1], "-d", *cmd[2:at], *extra, *cmd[at:]]

    def start(self) -> None:
        os.environ["OPENCODE_SERVER_PASSWORD"] = self.password
        os.environ["OPENCODE_SERVER_USERNAME"] = "opencode"
        try:
            super().start()
        finally:
            os.environ.pop("OPENCODE_SERVER_PASSWORD", None)
            os.environ.pop("OPENCODE_SERVER_USERNAME", None)


class Sse:
    """One SSE connection via ``docker exec curl -N``; every line is kept with a timestamp."""

    def __init__(self, server: MitigatedServer, path: str, seconds: int) -> None:
        cfg = f"url = {_curl_quote(f'http://127.0.0.1:{server.PORT}{path}')}\n"
        cfg += f"user = {_curl_quote('opencode:' + server.password)}\n"
        self.proc = subprocess.Popen(
            ["docker", "exec", "-i", server.NAME, "curl", "-sN", "-i", "--max-time", str(seconds),
             "-K", "-"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )  # fmt: skip
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(cfg.encode())
        self.proc.stdin.close()
        self.t0 = time.time()
        self.lines: list[tuple[float, str]] = []
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self) -> None:
        assert self.proc.stdout
        for raw in self.proc.stdout:
            self.lines.append(
                (round(time.time() - self.t0, 2), raw.decode(errors="replace").rstrip())
            )

    def close(self) -> list[tuple[float, str]]:
        self.proc.terminate()
        self.thread.join(5)
        return self.lines


def events(lines: list[tuple[float, str]]) -> list[dict[str, Any]]:
    out = []
    for at, line in lines:
        if line.startswith("data:"):
            try:
                ev = json.loads(line[5:])
            except ValueError:
                continue
            out.append({"t": at, "type": ev.get("type"), "id": ev.get("id"),
                        "has_sse_id_line": False, "props": sorted(ev.get("properties") or {})})  # fmt: skip
    return out


def sse_id_lines(lines: list[tuple[float, str]]) -> int:
    return sum(1 for _, line in lines if line.startswith("id:"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--opencode-home", type=Path, required=True)
    ap.add_argument("--container-profile", type=Path,
                    default=REPO / "deployment" / "local-container-opencode.json")  # fmt: skip
    ap.add_argument("--out", type=Path, default=Path("/tmp/oc031/m1.json"))
    a = ap.parse_args()
    root = Path("/tmp/oc031/m1root").resolve()
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    # colima shares $HOME only, so the read-only config dir lives under it (not ~/.amplai)
    cfg = Path.home() / ".amplai-sandbox-probes" / "opencode-m1-config"
    shutil.rmtree(cfg, ignore_errors=True)
    (cfg / "plugin").mkdir(parents=True)
    (cfg / "plugin" / "blank.js").write_text(PLUGIN)
    # the server writes this file into a config dir when it is missing (EROFS -> HTTP 500)
    (cfg / ".gitignore").write_text(
        "node_modules\npackage.json\npackage-lock.json\nbun.lock\n.gitignore"
    )
    (cfg / "shell").write_text(WRAPPER)
    (cfg / "shell").chmod(0o755)

    server = MitigatedServer(root, a.opencode_home.absolute(), cfg, a.container_profile)
    (server.ws / "probe.sh").write_text(PROBE_SH)
    result: dict[str, Any] = {"model": MODEL, "turns": 1}
    secrets_ = [server.password, *server.leak_values()]
    server.start()
    try:
        # (c) authentication
        noauth = server.client(auth=False)
        bad = DockerExecTransport(server.NAME, server.PORT, auth=("opencode", "wrong"))
        result["auth"] = {
            "health_with_password": server.client().get("/global/health").status_code,
            "health_no_password": noauth.get("/global/health").status_code,
            "session_list_no_password": noauth.get("/session").status_code,
            "event_no_password": noauth.get("/event").status_code,
            "health_wrong_password": httpx.Client(
                base_url=server.url, transport=bad, timeout=30).get("/global/health").status_code,
            "listeners": server.listeners(),
        }  # fmt: skip
        # /proc as the agent's uid, no model involved
        uid = server._exec("id -u").strip()
        rows = server._exec(
            "for d in /proc/[0-9]*; do p=${d#/proc/}; c=$(tr '\\000' ' ' < $d/cmdline 2>/dev/null"
            " | cut -c1-40); if [ -r $d/environ ]; then r=yes;"
            " f=$(tr '\\000' '\\n' < $d/environ 2>/dev/null | grep -c '^OPENCODE_SERVER_PASSWORD=.');"
            ' else r=no; f=-; fi; echo "$p|$r|$f|$c"; done'
        ).splitlines()
        result["proc_as_agent_uid"] = {"uid": uid, "rows": rows}
        # does the readable value equal the real password? (compared here, never printed)
        pid = next((r.split("|")[0] for r in rows if "opencode" in r and "serve" in r), None)
        if pid:
            env = server._exec(f"tr '\\000' '\\n' < /proc/{pid}/environ")
            result["server_pid_environ_equals_password"] = (
                f"OPENCODE_SERVER_PASSWORD={server.password}" in env
            )
            result["server_pid_environ_names"] = sorted(
                l.split("=", 1)[0] for l in env.splitlines() if "=" in l
            )
        made = server.client().post("/session", json={"title": "m1-diagnostic"})
        result["session_create"] = {"status": made.status_code, "body": made.text[:600]}
        result["server_log_tail"] = server._exec(
            "tail -n 25 $(ls -t /home/agent/.local/share/opencode/log/*.log 2>/dev/null | head -1)"
            " 2>&1 | cut -c1-300"
        )
        if made.status_code != 200:
            raise SystemExit("session creation failed; see the recorded diagnostics")
        # (a)+(d) one real turn while an SSE connection records the events
        turns = Turns(server, MODEL, "1.17.13")
        live = Sse(server, "/event", 400)
        time.sleep(1.5)
        turn = turns.run(
            "Use the bash tool to run exactly: sh /workspace/probe.sh"
            " and then reply with that command's output verbatim."
        )
        time.sleep(1)
        live_lines = live.close()
        msgs = turns.messages(turn["session"])
        blob = json.dumps(msgs)
        outputs = [
            (p.get("state") or {}).get("output", "")
            for m in msgs for p in m.get("parts", []) if p.get("type") == "tool"
        ]  # fmt: skip
        result["turn"] = {
            "state": turn["state"]["state"],
            "seconds": turn["seconds"],
            "bash_ran": '"tool": "bash"' in blob,
            "password_value_in_messages": server.password in blob,
            "bash_output": outputs,
        }
        result["events_turn"] = events(live_lines)
        result["sse_id_lines_turn"] = sse_id_lines(live_lines)
        result["header"] = [l for _, l in live_lines[:8] if not l.startswith("data")]
        # (d) reconnect after the turn: any replay of the finished turn? heartbeat spacing?
        again = Sse(server, "/event", 40)
        time.sleep(32)
        again_lines = again.close()
        result["events_reconnect"] = events(again_lines)
        result["sse_id_lines_reconnect"] = sse_id_lines(again_lines)
        # (b) what the image offers a formatter/git child
        result["git_on_path"] = server._exec("command -v git || true").strip()
        turns.cleanup()
    except BaseException as exc:
        result["error"] = repr(exc) + " " + repr(getattr(exc, "details", None))
        result["logs"] = subprocess.run(
            ["docker", "logs", "--tail", "40", server.NAME],
            capture_output=True,
            text=True,
            check=False,
        ).stdout[-3000:]
    finally:
        server.kill()
        shutil.rmtree(cfg, ignore_errors=True)
    body = json.dumps(result, indent=2)
    redacted = 0
    for value in secrets_:
        if value and value in body:
            redacted += 1
            body = body.replace(value, "<REDACTED>")
    body = body.rstrip("}").rstrip() + f',\n  "values_redacted_on_write": {redacted}\n}}'
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(body)
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
