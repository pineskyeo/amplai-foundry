"""Work 031 M7: facts for tool-uid separation (no model turn unless --symlink-turn).

Disposable containers of the pinned OpenCode image with the qualified sandbox flags
(``ContainerSandbox.command``: --read-only, --cap-drop=ALL, no-new-privileges, uid 65534).
The operator's scoped OpenCode home is mounted READ-ONLY at /home/agent for the credential
check; no value is read or printed, only ``test -r`` and ``stat`` metadata.

Checks (results as JSON, no secrets):
  a  provider credential readability as the agent uid (today's layout)
  b  ``docker exec -u <other uid>`` into a cap-dropped, no-new-privileges container
  c  /proc/<pid>/environ of another uid's process (a random fake secret, never printed)
  d  file permissions between two uids on tmpfs and on the colima bind-mounted workspace
  e  user namespaces (unshare) inside the qualified profile
  f  a unix socket handoff between the two uids in one container, and across two containers
     through a bind-mounted directory
  g  whether git run as uid A executes a hook-like setting (core.fsmonitor) from a repository
     owned by uid B (safe.directory ownership check)
  h  two containers sharing only the workspace: process visibility across them
  --symlink-turn: ONE real model turn; does OpenCode's read tool follow a workspace symlink to a
     canary file outside the workspace without an external_directory permission?

    .venv/bin/python scripts/opencode_uid_probe.py --opencode-home ~/.amplai-sandbox-probes/opencode-home
"""

# ruff: noqa: E501, E402
from __future__ import annotations

import argparse
import json
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from amplai_foundry.sandbox.container import ContainerProfile, ContainerSandbox

PROFILE = json.loads((REPO / "deployment" / "local-container-opencode.json").read_text())
IMAGE = PROFILE["image"]
A_UID, B_UID = 65534, 65533  # A: the server's uid today; B: a separate tool uid
PROBES = Path.home() / ".amplai-sandbox-probes" / "opencode-uid"  # colima shares $HOME only


def sh(*argv: str, stdin: str | None = None, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv), input=stdin, capture_output=True, text=True, timeout=timeout, check=False
    )


def box() -> ContainerSandbox:
    return ContainerSandbox(
        ContainerProfile(
            IMAGE, uid=A_UID, gid=A_UID, memory="512m", cpus=1.0, pids=128, network="none"
        )
    )


def start(name: str, ws: Path, *, home: Path | None, env: dict[str, str] | None = None,
          extra_mounts: list[str] | None = None) -> None:  # fmt: skip
    sh("docker", "rm", "-f", name)
    cmd = box().command(["sleep", "600"], ws, name, env_names=list(env or {}), native_home=home)
    if home is not None:  # the scoped copy is mounted read-only for this check
        i = cmd.index(f"type=bind,src={home},dst=/home/agent")
        cmd[i] += ",readonly"
    at = cmd.index(IMAGE)
    mounts = [x for m in (extra_mounts or []) for x in ("--mount", m)]
    run = subprocess.run(
        [cmd[0], cmd[1], "-d", *cmd[2:at], *mounts, *cmd[at:]],
        env={**dict(__import__("os").environ), **(env or {})},
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    if run.returncode != 0:
        raise SystemExit(f"start {name}: {run.stderr[-300:]}")


def ex(name: str, script: str, *, user: str | None = None) -> str:
    argv = ["docker", "exec", "-i"] + (["-u", user] if user else []) + [name, "sh", "-c", script]
    run = sh(*argv)
    return (
        run.stdout + (("\n[stderr] " + run.stderr.strip()) if run.stderr.strip() else "")
    ).strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--opencode-home", type=Path, required=True)
    ap.add_argument("--symlink-turn", action="store_true")
    ap.add_argument("--out", type=Path, default=Path("/tmp/oc031/m7.json"))
    a = ap.parse_args()
    home = a.opencode_home.expanduser().absolute()
    shutil.rmtree(PROBES, ignore_errors=True)
    ws, sockdir = PROBES / "workspace", PROBES / "sock"
    ws.mkdir(parents=True)
    sockdir.mkdir()
    ws.chmod(0o777)
    sockdir.chmod(0o777)
    fake = "fake-secret-" + secrets.token_hex(12)
    r: dict[str, Any] = {"image": IMAGE, "a_uid": A_UID, "b_uid": B_UID}
    x, y = "amplai-m7-server", "amplai-m7-tools"
    try:
        start(x, ws, home=home, env={"AMPLAI_M7_FAKE_SECRET": fake},
              extra_mounts=[f"type=bind,src={sockdir},dst=/amplai-sock"])  # fmt: skip
        r["tools"] = ex(
            x,
            "for t in python3 node git unshare setpriv su-exec gosu sudo stat; do printf '%s=%s ' $t $(command -v $t || echo -); done",
        )
        r["security"] = sh("docker", "inspect", "-f",
                           "{{.HostConfig.ReadonlyRootfs}} {{.HostConfig.CapDrop}} {{.HostConfig.SecurityOpt}} {{.Config.User}}", x).stdout.strip()  # fmt: skip
        # a: provider credential as the agent uid, today (scoped home mounted read-only)
        cred = "/home/agent/.local/share/opencode/auth.json"
        r["a_credential_as_agent"] = ex(
            x,
            f"id -u; stat -c 'owner=%u:%g mode=%a' {cred}; test -r {cred} && echo readable=yes || echo readable=no; stat -c 'home owner=%u:%g mode=%a' /home/agent",
        )
        r["a_credential_as_other_uid"] = ex(
            x,
            f"id -u; test -r {cred} && echo readable=yes || echo readable=no",
            user=f"{B_UID}:{B_UID}",
        )
        # b: docker exec as another uid into the cap-dropped container
        r["b_exec_other_uid"] = ex(
            x, "id; grep -E '^(CapEff|NoNewPrivs)' /proc/self/status", user=f"{B_UID}:{A_UID}"
        )
        # c: /proc environ of the uid-A process (sleep, pid 1 is docker-init) as uid B and as uid A
        probe_c = ("for p in /proc/[0-9]*; do n=${p#/proc/}; c=$(tr '\\0' ' ' < $p/cmdline 2>/dev/null | cut -c1-20);"
                   " u=$(awk '/^Uid:/{print $2}' $p/status 2>/dev/null);"
                   " if tr '\\0' '\\n' < $p/environ 2>/dev/null | grep -q '^AMPLAI_M7_FAKE_SECRET=.'; then v=has-secret;"
                   " elif [ -r $p/environ ]; then v=readable-no-secret; else v=denied; fi; echo \"pid=$n uid=$u $v [$c]\"; done")  # fmt: skip
        r["c_environ_as_B"] = ex(x, probe_c, user=f"{B_UID}:{A_UID}")
        r["c_environ_as_A"] = ex(x, probe_c)
        # d: file modes between the uids (tmpfs /tmp and the bind-mounted workspace)
        ex(x, "umask 077; mkdir -p /tmp/a-home && echo s > /tmp/a-home/cred && ls -ld /tmp/a-home")
        r["d_tmpfs_private_as_B"] = ex(
            x,
            "cat /tmp/a-home/cred >/dev/null 2>&1 && echo read=yes || echo read=no",
            user=f"{B_UID}:{A_UID}",
        )
        ex(
            x,
            "umask 002; echo a > /workspace/by_a.txt; umask 077; echo a > /workspace/by_a_private.txt",
        )
        ex(x, "umask 002; echo b > /workspace/by_b.txt", user=f"{B_UID}:{A_UID}")
        r["d_workspace_stat"] = ex(
            x,
            "stat -c '%n owner=%u:%g mode=%a' /workspace/by_a.txt /workspace/by_a_private.txt /workspace/by_b.txt /workspace",
        )
        r["d_workspace_B_reads_A_private"] = ex(
            x,
            "cat /workspace/by_a_private.txt >/dev/null 2>&1 && echo read=yes || echo read=no",
            user=f"{B_UID}:{A_UID}",
        )
        r["d_workspace_A_edits_B_file"] = ex(
            x, "echo more >> /workspace/by_b.txt && echo write=yes || echo write=no"
        )
        r["d_workspace_B_edits_A_file"] = ex(
            x,
            "echo more >> /workspace/by_a.txt && echo write=yes || echo write=no",
            user=f"{B_UID}:{A_UID}",
        )
        r["d_host_view"] = sorted(
            f"{p.name} uid={p.stat().st_uid} mode={oct(p.stat().st_mode & 0o777)}"
            for p in ws.iterdir()
        )
        # e: user namespaces inside the qualified profile
        r["e_unshare"] = ex(
            x, "unshare -U -r id 2>&1; echo rc=$?; cat /proc/sys/user/max_user_namespaces 2>&1"
        )
        # f: unix socket handoff between uids (same container, /tmp) and across containers (bind dir)
        server_js = ("const net=require('net');const fs=require('fs');const p=process.argv[1];try{fs.unlinkSync(p)}catch{};"
                     "net.createServer(c=>{c.end('uid='+process.getuid()+'\\n')}).listen(p,()=>{fs.chmodSync(p,0o770)});"
                     "setTimeout(()=>process.exit(0),20000)")  # fmt: skip
        client_js = ("const net=require('net');const c=net.connect(process.argv[1]);let d='';c.on('data',x=>d+=x);"
                     "c.on('end',()=>{console.log('reply '+d.trim())});c.on('error',e=>console.log('error '+e.code))")  # fmt: skip
        subprocess.Popen(["docker", "exec", "-u", f"{B_UID}:{A_UID}", x, "node", "-e", server_js, "/tmp/runner.sock"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # fmt: skip
        time.sleep(2)
        r["f_socket_same_container_A_to_B"] = ex(x, f'node -e "{client_js}" /tmp/runner.sock')
        # h: a second container (tool uid B) sharing only the workspace and the socket dir
        sh("docker", "rm", "-f", y)
        cmd = ContainerSandbox(ContainerProfile(IMAGE, uid=B_UID, gid=A_UID, memory="512m", cpus=1.0, pids=128,
                               network="none")).command(["sleep", "600"], ws, y)  # fmt: skip
        at = cmd.index(IMAGE)
        run = sh(
            cmd[0],
            cmd[1],
            "-d",
            *cmd[2:at],
            "--mount",
            f"type=bind,src={sockdir},dst=/amplai-sock",
            *cmd[at:],
        )
        r["h_second_container_started"] = run.returncode == 0
        if run.returncode == 0:
            r["h_processes_visible_in_tool_container"] = ex(
                y,
                "ls /proc | grep -E '^[0-9]+$' | wc -l; ps -eo pid,user,args 2>/dev/null | head -5 || true",
            )
            r["h_tool_container_reads_server_home"] = ex(
                y, f"test -e {cred} && echo exists=yes || echo exists=no"
            )
            r["h_workspace_shared"] = ex(y, "ls /workspace | sort | tr '\\n' ' '")
            subprocess.Popen(["docker", "exec", y, "node", "-e", server_js, "/amplai-sock/runner.sock"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # fmt: skip
            time.sleep(2)
            r["f_socket_across_containers_bind_dir"] = ex(
                x, f'node -e "{client_js}" /amplai-sock/runner.sock'
            )
        # g: git run as uid A in a repository owned by uid B with core.fsmonitor set
        r["g_git_version"] = ex(x, "git --version")
        ex(x, "rm -rf /tmp/repo && mkdir /tmp/repo && cd /tmp/repo && git init -q && git config core.fsmonitor 'touch /tmp/fsmonitor-ran; false' && chmod -R a+rX /tmp/repo",
           user=f"{B_UID}:{A_UID}")  # fmt: skip
        r["g_git_as_A_in_B_repo"] = ex(
            x,
            "cd /tmp/repo && git status --short >/dev/null 2>/tmp/git.err; echo rc=$?; head -c 200 /tmp/git.err; test -e /tmp/fsmonitor-ran && echo fsmonitor_ran=yes || echo fsmonitor_ran=no",
        )
        ex(
            x,
            "rm -rf /tmp/repo2 && mkdir /tmp/repo2 && cd /tmp/repo2 && git init -q && git config core.fsmonitor 'touch /tmp/fsmonitor2-ran; false'",
        )
        r["g_git_as_A_in_A_repo"] = ex(
            x,
            "cd /tmp/repo2 && git status --short >/dev/null 2>&1; echo rc=$?; test -e /tmp/fsmonitor2-ran && echo fsmonitor_ran=yes || echo fsmonitor_ran=no",
        )
        if a.symlink_turn:
            r["symlink_turn"] = symlink_turn(home)
    finally:
        sh("docker", "rm", "-f", x)
        sh("docker", "rm", "-f", y)
    body = json.dumps(r, indent=2)
    if fake in body:
        raise SystemExit("refusing to write: the fake secret value is in the output")
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(body)
    print(f"wrote {a.out}")
    return 0


def symlink_turn(home: Path) -> dict[str, Any]:
    """One real turn: the read tool on a workspace symlink to a canary outside the workspace."""
    from opencode_auth_probe import MitigatedServer
    from opencode_qualify import Turns

    root = Path("/tmp/oc031/m7root").resolve()
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    cfg = Path.home() / ".amplai-sandbox-probes" / "opencode-m7-config"
    shutil.rmtree(cfg, ignore_errors=True)
    (cfg / "plugin").mkdir(parents=True)
    (cfg / ".gitignore").write_text(
        "node_modules\npackage.json\npackage-lock.json\nbun.lock\n.gitignore"
    )
    (cfg / "plugin" / "blank.js").write_text(
        'export default async () => ({"shell.env": async (_i, o) => { o.env.OPENCODE_SERVER_PASSWORD = ""; }});\n'
    )
    (cfg / "shell").write_text(
        '#!/bin/sh\nunset OPENCODE_SERVER_PASSWORD OPENCODE_SERVER_USERNAME\nexec /bin/bash "$@"\n'
    )
    (cfg / "shell").chmod(0o755)
    server = MitigatedServer(root, home, cfg, REPO / "deployment" / "local-container-opencode.json")
    token = "SYMCANARY-" + secrets.token_hex(8)
    out: dict[str, Any] = {}
    server.start()
    try:
        server._exec(
            f"mkdir -p /tmp/outside && echo {token} > /tmp/outside/canary.txt && ln -s /tmp/outside/canary.txt /workspace/notes.txt && ls -l /workspace/notes.txt"
        )
        out["symlink"] = server._exec("ls -l /workspace/notes.txt").strip()
        t = Turns(server, "opencode-go/glm-5.3-flash", "1.17.13")
        turn = t.run(
            "Use the read tool (not bash) to read the file /workspace/notes.txt and reply with its exact contents.",
            until=lambda s: (
                bool(s.get("provider_completed")) or s["state"] in {"completed", "failed"}
            ),
        )
        msgs = t.messages(turn["session"])
        blob = json.dumps(msgs)
        pending = t.http.get("/permission").json()
        tools = [
            {"tool": p.get("tool"), "status": (p.get("state") or {}).get("status"),
             "error": str((p.get("state") or {}).get("error", ""))[:200]}
            for m in msgs for p in m.get("parts", []) if p.get("type") == "tool"
        ]  # fmt: skip
        out.update({
            "state": turn["state"]["state"], "seconds": turn["seconds"],
            "canary_in_messages": token in blob, "tools": tools,
            "pending_permissions": [
                {"permission": q.get("permission"), "patterns": q.get("patterns")} for q in pending
            ] if isinstance(pending, list) else pending,
        })  # fmt: skip
        if pending:
            turn["driver"].cancel(turn["dispatch"])
        t.cleanup()
    finally:
        server.kill()
        shutil.rmtree(cfg, ignore_errors=True)
    return out


if __name__ == "__main__":
    sys.exit(main())
