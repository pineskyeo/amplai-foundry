"""Qualify a CLI driver with real turns inside the egress-controlled container sandbox.

Every probe is a measurement on the pinned worker image, on the qualified egress profile, with
the sandbox argv ``ContainerSandbox.command`` builds. The nine mandatory probes go through
``QualificationRunner`` with CAS-admitted artifacts, so the record has the same shape and the
same "fail unless all nine pass" rule as the host-side run in Work 015.

    set -a; . ~/.config/amplai/claude-oauth.env; set +a
    .venv/bin/python scripts/container_qualify.py --driver claude
    .venv/bin/python scripts/container_qualify.py --driver codex --codex-home DIR   # DIR from
        # scripts/sandbox_up.sh --codex-home DIR (the user copies the credential, not the agent)

Secret values never reach stdout, artifacts or the spec directory: the probes search for them
and the CAS refuses artifacts that match its secret patterns.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from amplai_foundry.agent_drivers.protocol import EventNormalizer, JsonlDecoder
from amplai_foundry.agent_drivers.qualification import MANDATORY, QualificationRunner
from amplai_foundry.runtime.reference import ReferenceDeployment
from amplai_foundry.sandbox.container import ContainerProfile, ContainerSandbox
from amplai_foundry.sandbox.egress import EgressProfile, load_qualification

REPO = Path(__file__).resolve().parents[1]
SPEC = REPO / "specs" / "016-container-egress-profile"
PROBE_ROOT = Path.home() / ".amplai-sandbox-probes" / "requalify"  # colima shares $HOME only
PROXY = "amplai-egress-proxy"
ISOLATION = [
    "--setting-sources",
    "",
    "--strict-mcp-config",
    "--disable-slash-commands",
    "--no-chrome",
]


def now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Probe:
    """One measured outcome plus the bytes that prove it."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.outcome = "inconclusive"
        self.reason = ""
        self.artifacts: list[tuple[str, bytes]] = []

    def record(self, outcome: str, reason: str = "", **files: bytes) -> Probe:
        self.outcome, self.reason = outcome, reason
        self.artifacts += [(f"{self.name}-{k}", v) for k, v in files.items()]
        return self


class ContainerTurns:
    def __init__(
        self, driver: str, model: str, codex_home: Path | None, container_profile: Path
    ) -> None:
        self.driver, self.model = driver, model
        container = json.loads(container_profile.read_text())
        self.egress = EgressProfile.load(REPO / "deployment" / "local-egress.json")
        ref = load_qualification(REPO / "deployment" / "local-egress-qualification.json")
        self.profile = ContainerProfile(
            container["image"],
            uid=container["uid"],
            gid=container["gid"],
            memory=container["memory"],
            cpus=container["cpus"],
            pids=container["pids"],
            network=self.egress.network,
            network_qualification_ref=ref,
            egress=self.egress,
        )
        self.sandbox = ContainerSandbox(self.profile)
        root = PROBE_ROOT / driver
        shutil.rmtree(root, ignore_errors=True)
        self.ws = root / "ws"
        self.ws.mkdir(parents=True)
        if driver == "codex":
            if codex_home is None or not (codex_home / ".codex" / "auth.json").is_file():
                raise SystemExit(
                    "codex needs --codex-home DIR (scripts/sandbox_up.sh --codex-home)"
                )
            real = (Path.home() / ".codex").resolve()
            if (codex_home / ".codex").resolve() == real or codex_home.resolve() == Path.home():
                # design 10_AUTHORITY_SECURITY: no home credential mounts; only a scoped copy
                raise SystemExit("--codex-home must be a scoped copy, never the real ~/.codex")
            self.home = codex_home
            self.secrets = [
                v.encode()
                for v in _json_strings(
                    json.loads((codex_home / ".codex" / "auth.json").read_text())
                )
                if len(v) >= 24
            ]
            self.env_names: list[str] = []
        else:
            self.home = root / "home"
            self.home.mkdir()
            token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
            if not token:
                raise SystemExit("claude needs CLAUDE_CODE_OAUTH_TOKEN in the environment")
            self.secrets = [token.encode()]
            self.env_names = ["CLAUDE_CODE_OAUTH_TOKEN"]
        self.runs = 0
        self.cost: list[dict[str, Any]] = []

    # -- argv -------------------------------------------------------------------------------
    def argv(self, prompt: str, *, session: str | None = None, tools: str = "Read") -> list[str]:
        if self.driver == "claude":
            args = ["claude", *ISOLATION, "-p", prompt, "--output-format", "stream-json"]
            args += [
                "--verbose",
                "--model",
                self.model,
                "--allowedTools",
                tools,
                "--max-turns",
                "3",
            ]
            return args + (["--resume", session] if session else [])
        args = ["codex", "--ask-for-approval", "never", "exec"]
        if session:
            args += ["resume", session]
        args += ["--json", "--model", self.model, "--skip-git-repo-check"]
        if not session:
            args += ["--sandbox", "workspace-write" if tools != "Read" else "read-only"]
        return [*args, prompt]

    def command(self, argv: list[str], name: str) -> list[str]:
        return self.sandbox.command(
            argv, self.ws, name, env_names=self.env_names, native_home=self.home
        )

    # -- execution ------------------------------------------------------------------------
    def turn(
        self, prompt: str, *, session: str | None = None, tools: str = "Read"
    ) -> dict[str, Any]:
        self.runs += 1
        name = f"amplai-qual-{self.driver}-{self.runs}"
        cmd = self.command(self.argv(prompt, session=session, tools=tools), name)
        started = time.time()
        try:  # F1: a timed-out attach must never leak the (non --rm) container
            run = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=600)
        finally:
            self._cleanup(name)
        return self._decode(run.stdout, run.stderr, run.returncode, started, session)

    def interrupted(self, prompt: str, *, signal: str) -> dict[str, Any]:
        """Start a long turn, wait for the first provider event, then stop (SIGTERM) or kill."""
        self.runs += 1
        name = f"amplai-qual-{self.driver}-{self.runs}"
        cmd = self.command(self.argv(prompt, tools="Bash"), name)
        proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        chunks: list[bytes] = []
        errs: list[bytes] = []
        first = threading.Event()

        def reader() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                chunks.append(line)
                first.set()

        def err_reader() -> None:  # F5: drain stderr too, so a chatty child never blocks
            assert proc.stderr is not None
            errs.append(proc.stderr.read())

        threading.Thread(target=reader, daemon=True).start()
        err_thread = threading.Thread(target=err_reader, daemon=True)
        err_thread.start()
        saw_event = first.wait(120)
        t0 = time.time()
        if signal == "SIGTERM":
            self.sandbox.stop(name)  # docker stop --time 5: SIGTERM, then SIGKILL after 5 s
        else:
            subprocess.run(
                ["docker", "kill", "--signal", "SIGKILL", name], capture_output=True, check=False
            )
        try:
            proc.wait(30)
        except subprocess.TimeoutExpired:
            proc.kill()
        stopped_in = round(time.time() - t0, 2)
        err_thread.join(10)
        inspect = subprocess.run(
            ["docker", "container", "inspect", name], capture_output=True, text=True, check=False
        )
        state = json.loads(inspect.stdout)[0]["State"] if inspect.returncode == 0 else {}
        self._cleanup(name)
        out = b"".join(chunks)
        completed = any(
            kind in out for kind in (b'"type":"result"', b'"type": "result"', b"turn.completed")
        )
        return {
            "saw_event": saw_event,
            "completed": completed,  # a completion event means the turn finished on its own
            "signal": signal,
            "stopped_in_s": stopped_in,
            "running_after": state.get("Running"),
            "exit_code": state.get("ExitCode"),
            "oom": state.get("OOMKilled"),
            "stdout": out,
            "stderr": b"".join(errs),
        }

    def _cleanup(self, name: str) -> None:
        # F4: wait only while the container exists and still runs; an absent container is
        # removed (no-op) at once instead of stalling the 20 s grace.
        deadline = time.time() + 20
        while time.time() < deadline:
            inspect = subprocess.run(
                ["docker", "container", "inspect", name],
                capture_output=True,
                text=True,
                check=False,
            )
            if inspect.returncode != 0 or self.sandbox.stopped(name):
                break
            time.sleep(0.5)
        subprocess.run(["docker", "container", "rm", "-f", name], capture_output=True, check=False)

    def _decode(
        self, out: bytes, err: bytes, rc: int, started: float, session: str | None
    ) -> dict[str, Any]:
        events = JsonlDecoder().feed(out, final=True)
        normalizer = EventNormalizer(self.driver, expected_session=session)
        normalized, fault = [], None
        try:
            for e in events:
                normalized.append(normalizer.accept(e))
        except Exception as exc:  # measured, not hidden
            fault = f"{type(exc).__name__}:{getattr(exc, 'code', '')}"
        if self.driver == "claude":
            result = next((e for e in reversed(events) if e.get("type") == "result"), {})
            text, usage = result.get("result"), result.get("usage")
            ok = bool(result) and not result.get("is_error") and rc == 0
        else:
            done = [e for e in events if e.get("type") == "turn.completed"]
            msgs = [e.get("item", {}) for e in events if e.get("type") == "item.completed"]
            text = next(
                (m.get("text") for m in reversed(msgs) if m.get("type") == "agent_message"), None
            )
            usage = done[-1].get("usage") if done else None
            ok = bool(done) and rc == 0
        return {
            "ok": ok,
            "rc": rc,
            "seconds": round(time.time() - started, 1),
            "events": len(events),
            "normalized": len(normalized),
            "fault": fault,
            "session": normalizer.session,
            "text": text,
            "usage": usage,
            "stdout": out,
            "stderr": err,
        }

    # -- side probes ------------------------------------------------------------------------
    def shell(self, script: str) -> subprocess.CompletedProcess[bytes]:
        self.runs += 1
        name = f"amplai-qual-{self.driver}-{self.runs}"
        cmd = self.command(["sh", "-c", script], name)
        run = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
        self._cleanup(name)
        return run

    def leaked(self, *blobs: bytes) -> list[str]:
        found = []
        for i, secret in enumerate(self.secrets):
            for j, blob in enumerate(blobs):
                if secret and secret in blob:
                    found.append(f"secret{i}-in-blob{j}")
        return found

    def tree_bytes(self, root: Path) -> tuple[bytes, list[str]]:
        names, data = [], b""
        for p in sorted(root.rglob("*")):
            if p.is_file() and not p.is_symlink():
                names.append(str(p.relative_to(root)))
                data += p.read_bytes()
        return data, names


def _const(value: dict[str, Any]) -> Callable[[], dict[str, Any]]:
    return lambda: value


def _json_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _json_strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _json_strings(v)]
    return []


def proxy_log_since(ts: float) -> list[dict[str, Any]]:
    run = subprocess.run(
        ["docker", "logs", "--since", str(int(ts)), PROXY],
        capture_output=True,
        text=True,
        check=False,
    )
    lines = []
    for line in run.stdout.splitlines():
        try:
            lines.append(json.loads(line))
        except ValueError:
            continue
    return lines


def measure(t: ContainerTurns, version: str) -> dict[str, Probe]:
    p = {name: Probe(name) for name in MANDATORY}
    ver = t.shell(f"{t.driver} --version")
    reported = ver.stdout.decode(errors="replace").strip()
    p["exact_version"].record(
        "pass" if ver.returncode == 0 and version and version in reported else "fail",
        f"container reports {reported!r}; pinned {version!r}",
        version=ver.stdout,
    )
    fs = t.shell(
        "touch /usr/local/bin/x 2>/dev/null; echo root_write_rc=$?;"
        " touch /workspace/.probe && echo ws_write_ok;"
        " for p in /Users /var/run/docker.sock; do"
        " [ -e $p ] && echo present=$p || echo absent=$p; done;"
        " [ -w /root ] && echo root_home=writable || echo root_home=readonly;"
        " echo root_home_entries=$(ls -A /root 2>/dev/null | wc -l); echo uid=$(id -u)"
    )
    fs_out = fs.stdout.decode(errors="replace")
    fs_ok = (
        "root_write_rc=1" in fs_out
        and "ws_write_ok" in fs_out
        and "present=" not in fs_out
        and "root_home=readonly" in fs_out
        and "root_home_entries=0" in fs_out.replace(" ", "")
        and f"uid={t.profile.uid}" in fs_out
    )
    egress_direct = t.shell(
        "env -u HTTPS_PROXY -u HTTP_PROXY -u https_proxy -u http_proxy"
        " curl -sS --max-time 5 https://api.anthropic.com/ >/dev/null 2>&1; echo direct_rc=$?;"
        " curl -sS --max-time 10 -o /dev/null -w 'denied_http=%{http_code}\\n'"
        " https://example.com/ 2>&1 | tail -1"
    )
    eg_out = egress_direct.stdout.decode(errors="replace") + egress_direct.stderr.decode(
        errors="replace"
    )

    since = time.time()
    first = t.turn("Reply with exactly the word PONG")
    t.cost.append({"turn": "pong", "usage": first["usage"], "seconds": first["seconds"]})
    log = proxy_log_since(since)
    allowed = sorted({x["target"] for x in log if x.get("decision") == "allow"})
    denied = sorted({x["target"] for x in log if x.get("decision") == "deny"})
    stream_ok = (
        first["ok"] and first["fault"] is None and first["normalized"] == first["events"] > 0
    )
    p["exact_session"].record(
        "pass" if stream_ok and first["session"] else "fail",
        f"session bound={bool(first['session'])} rc={first['rc']} text={first['text']!r}",
        stream=first["stdout"],
    )
    p["ordered_events"].record(
        "pass" if stream_ok else "fail",
        f"{first['normalized']}/{first['events']} events normalized, fault={first['fault']}",
        stream=first["stdout"],
    )
    p["budget_accounting"].record(
        "pass" if stream_ok and isinstance(first["usage"], dict) and first["usage"] else "fail",
        f"usage={'measured' if first['usage'] else 'absent'}",
        usage=json.dumps(first["usage"] or {}).encode(),
    )
    p["egress_containment"].record(
        "pass"
        if stream_ok and "direct_rc=0" not in eg_out and "denied_http=000" in eg_out and allowed
        else "fail",
        f"direct egress: {eg_out.strip().replace(chr(10), ' | ')};"
        f" turn used only allowlist targets {allowed}; sidecar denied {denied}",
        probe=egress_direct.stdout + egress_direct.stderr,
        proxy_log=("\n".join(json.dumps(x) for x in log)).encode(),
    )
    ws_bytes, ws_names = t.tree_bytes(t.ws)
    home_bytes, home_names = t.tree_bytes(t.home)
    proxy_bytes = json.dumps(log).encode()
    leaks = t.leaked(
        first["stdout"],
        first["stderr"],
        ws_bytes,
        proxy_bytes,
        json.dumps(t.command(t.argv("x"), "amplai-qual-argv")).encode(),
    )
    home_leak = t.leaked(home_bytes)
    p["secret_isolation"].record(
        "pass" if stream_ok and not leaks else "fail",
        "secret absent from stream, stderr, workspace, sidecar log and argv"
        + ("" if not leaks else f"; LEAK {leaks}")
        + (
            f"; present in worker-owned session home ({len(home_names)} files,"
            " not an agent-visible artifact)"
            if home_leak
            else "; absent from session home"
        ),
        scan=json.dumps(
            {
                "workspace_files": ws_names,
                "home_files": len(home_names),
                "leaks": leaks,
                "home_leak": bool(home_leak),
            }
        ).encode(),
    )
    p["filesystem_containment"].record(
        "pass" if fs_ok and stream_ok else "fail",
        f"probe: {fs_out.strip().replace(chr(10), ' | ')}; turn wrote only {ws_names}",
        probe=fs.stdout + fs.stderr,
    )

    long_prompt = "Run the shell command `sleep 120` with Bash and after it finishes reply DONE"
    cancel = t.interrupted(long_prompt, signal="SIGTERM")
    p["cancel_tree"].record(
        "pass"
        if cancel["saw_event"]
        and not cancel["completed"]
        and cancel["running_after"] is False
        and cancel["stopped_in_s"] < 10
        # SIGTERM honoured by the tree: claude exits 143, codex shuts down gracefully with 0.
        # A completion event would mean the turn ended on its own, not by the signal.
        and cancel["exit_code"] in (0, 143)
        else "fail",
        f"SIGTERM via docker stop after first event: stopped_in={cancel['stopped_in_s']}s"
        f" exit={cancel['exit_code']} ({'graceful' if cancel['exit_code'] == 0 else 'signal'})"
        f" completed={cancel['completed']} running_after={cancel['running_after']}",
        state=json.dumps(
            {k: v for k, v in cancel.items() if k not in {"stdout", "stderr"}}
        ).encode(),
        stream=cancel["stdout"],
    )
    crash = t.interrupted(long_prompt, signal="SIGKILL")
    resumed = t.turn(
        "Which single word did you reply with earlier in this session? Reply with just that word.",
        session=first["session"],
    )
    t.cost.append({"turn": "resume", "usage": resumed["usage"], "seconds": resumed["seconds"]})
    same = bool(first["session"]) and resumed["session"] == first["session"]
    p["crash_recovery"].record(
        "pass"
        if crash["saw_event"]
        and crash["running_after"] is False
        and resumed["ok"]
        and same
        and resumed["fault"] is None
        else "fail",
        f"SIGKILL mid-turn (exit={crash['exit_code']}), then exact-session resume:"
        f" same_session={same} rc={resumed['rc']} text={resumed['text']!r}",
        crash=json.dumps(
            {k: v for k, v in crash.items() if k not in {"stdout", "stderr"}}
        ).encode(),
        resume=resumed["stdout"],
    )
    return p


def record(
    t: ContainerTurns, probes: dict[str, Probe], version: str, out_dir: Path
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    with ReferenceDeployment(PROBE_ROOT / "deployment") as d:
        actor = replace(d.actor, permissions=d.actor.permissions | {"driver.qualify"})
        callables: dict[str, Callable[[], dict[str, Any]]] = {}
        for name, probe in probes.items():
            refs = []
            for label, data in probe.artifacts:
                if t.leaked(data):
                    raise SystemExit(f"refusing to store {label}: contains a secret value")
                # operator: this session measured and graded it; not an independent verifier
                refs.append(
                    d.artifacts.admit(d.scope, data, "application/octet-stream", trust="operator")
                )
                (out_dir / f"{t.driver}-{label}.bin").write_bytes(data)
            result = {"outcome": probe.outcome, "reason": probe.reason, "artifact_refs": refs}
            callables[name] = _const(result)
        env_ref = {
            "image": t.profile.image,
            "egress_profile": t.egress.wire(),
            "sandbox_argv": t.command(t.argv("<prompt>"), "amplai-qual-example"),
        }
        ref = QualificationRunner(d.store, d.artifacts).run(
            actor, f"{t.driver}-cli", version, env_ref, callables
        )
        report = d.store.get(d.scope, "qualification", ref)  # put() returns the ref, not the body
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--driver", choices=["claude", "codex"], required=True)
    ap.add_argument("--model")
    ap.add_argument("--codex-home", type=Path)
    ap.add_argument("--out", type=Path, default=SPEC / "driver-qualification.json")
    # A per-app image (D-072) is a new environment: qualify there before registering the driver.
    ap.add_argument(
        "--container-profile", type=Path, default=REPO / "deployment" / "local-container.json"
    )
    ap.add_argument("--artifacts", type=Path, default=SPEC / "artifacts")
    a = ap.parse_args()
    model = a.model or ("claude-sonnet-5" if a.driver == "claude" else "gpt-5.6-sol")
    host_version = subprocess.run(
        [a.driver, "--version"], capture_output=True, text=True, check=False
    ).stdout.strip()
    container = json.loads(a.container_profile.read_text())
    pinned = re.search(r"\d+\.\d+\.\d+", container["tools"][a.driver])
    version = pinned.group(0) if pinned else ""
    t = ContainerTurns(a.driver, model, a.codex_home, a.container_profile)
    probes = measure(t, version)
    report = record(t, probes, version, a.artifacts)
    checks = [
        {
            "name": c["name"],
            "outcome": c["outcome"],
            **({"reason": c["reason"]} if c.get("reason") else {}),
            "artifacts": len(c["artifact_refs"]),
        }
        for c in report["checks"]
    ]
    existing: dict[str, Any] = (
        json.loads(a.out.read_text())
        if a.out.is_file()
        else {
            "schema_version": "1.0",
            "kind": "driver_qualification_in_container",
            "reports": {},
            "cost": {},
            "findings": [],
        }
    )
    existing["checked_at"] = now()
    existing["method"] = (
        "scripts/container_qualify.py: real turns inside the pinned image on the qualified"
        " egress profile; QualificationRunner with CAS-admitted artifacts (operator trust:"
        " self-measured, not an independent verifier); status fail unless all nine pass"
    )
    existing["container_image"] = t.profile.image
    existing["egress_profile"] = t.egress.wire()
    existing["artifacts_dir"] = str(a.artifacts.absolute().relative_to(REPO))
    existing["reports"][f"{a.driver}-cli"] = {
        "status": report["status"],
        "driver_version": version,
        "host_version": host_version,
        "model": model,
        "auth": "oauth_token" if a.driver == "claude" else "chatgpt_account_sandbox_home",
        "native_delegation_qualified": report["native_delegation_qualified"],
        "qualification_id": report["qualification_id"],
        "checks": checks,
    }
    existing["cost"][f"{a.driver}-cli"] = t.cost
    a.out.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(existing, ensure_ascii=False, indent=2) + "\n"
    if t.leaked(body.encode()):
        raise SystemExit("refusing to write report: contains a secret value")
    a.out.write_text(body)
    print(
        json.dumps(
            {
                "driver": a.driver,
                "status": report["status"],
                "checks": {c["name"]: c["outcome"] for c in checks},
            },
            indent=1,
        )
    )
    for c in checks:
        print(f"  {c['name']:24} {c['outcome']:12} {c.get('reason', '')[:160]}")
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
