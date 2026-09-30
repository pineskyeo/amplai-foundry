"""Work 032 probe: can a credential broker keep the provider credential out of the container?

    .venv/bin/python scripts/broker_probe.py claude --token-file ~/.config/amplai/claude-oauth.env
    .venv/bin/python scripts/broker_probe.py codex --codex-home ~/.amplai-sandbox-probes/codex-home

A probe only: nothing in the product changes (specs/032-credential-broker-probe/spec.md).

- A broker container (mitmproxy, pinned by digest) joins the agent's --internal network and the
  outside bridge, like the egress proxy. It terminates TLS for the allowlisted provider hosts and
  replaces a placeholder in request headers with the real value it reads from the operator's own
  credential file, mounted read-only (specs/032-credential-broker-probe/broker_addon.py).
- The agent container runs the pinned app image with the sandbox flags, the broker as its proxy,
  the broker's CA as an extra trust root, and only the placeholder.
- One model turn, then a marker-only side probe as the container uid (no model): is the real
  value anywhere the agent's processes can read? The real value is searched for in every output
  in this process and never printed; a report that contains it is refused.
"""

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

REPO = Path(__file__).resolve().parents[1]
SPEC = REPO / "specs" / "032-credential-broker-probe"
MITM = "mitmproxy/mitmproxy@sha256:e0deb0df7edf9f909053f274a067cd1cacb90f5c17d74459e1693179c0b98d8f"
BROKER = "amplai-broker-probe"
INTERNAL, OUTSIDE = "amplai-internal", "amplai-egress"
ROOT = Path.home() / ".amplai-sandbox-probes" / "broker"  # colima shares $HOME only
ALLOW = {
    "claude": ["api.anthropic.com:443"],
    "codex": ["api.openai.com:443", "auth.openai.com:443", "chatgpt.com:443"],
}
ISOLATION = ["--setting-sources", "", "--strict-mcp-config", "--disable-slash-commands",
             "--no-chrome"]  # fmt: skip


def sh(args: list[str], **kw: Any) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(args, capture_output=True, check=False, **kw)


def env_secret(path: Path, name: str) -> str:
    for line in path.read_text().splitlines():
        key, _, value = line.partition("=")
        if key.strip() == name and value.strip():
            return value.strip()
    raise SystemExit(f"{path} has no {name}")


def start_broker(credential: Path, placeholder: str, fmt: str, allow: list[str]) -> Path:
    sh(["docker", "rm", "-f", BROKER])
    conf = ROOT / "mitm-conf"
    shutil.rmtree(conf, ignore_errors=True)
    conf.mkdir(parents=True)
    conf.chmod(0o777)  # the image's mitmproxy user writes its CA here
    addon = SPEC / "broker_addon.py"
    # the image's entrypoint changes file ownership, which --cap-drop=ALL refuses; run
    # mitmdump directly as an unprivileged uid with its config dir on the bind mount
    run = sh([
        "docker", "run", "-d", "--name", BROKER, "--network", OUTSIDE,
        "--cap-drop=ALL", "--security-opt=no-new-privileges", "--user", "65534:65534",
        "--entrypoint", "mitmdump",
        "--mount", f"type=bind,src={addon},dst=/broker/addon.py,readonly",
        "--mount", f"type=bind,src={credential},dst=/broker/credential,readonly",
        "--mount", f"type=bind,src={conf},dst=/broker/conf",
        MITM, "--listen-port", "8080", "-s", "/broker/addon.py",
        "--set", "confdir=/broker/conf",
        "--set", "allow=" + ",".join(allow), "--set", "placeholder=" + placeholder,
        "--set", "secret_file=/broker/credential", "--set", "secret_format=" + fmt,
        "--set", "flow_detail=0", "--set", "termlog_verbosity=warn",
    ])  # fmt: skip
    if run.returncode != 0:
        raise SystemExit("broker did not start: " + run.stderr.decode()[-300:])
    sh(["docker", "network", "connect", INTERNAL, BROKER])
    ca = conf / "mitmproxy-ca-cert.pem"
    for _ in range(60):
        if ca.is_file():
            return ca
        time.sleep(1)
    logs = sh(["docker", "logs", BROKER]).stdout.decode(errors="replace")[-400:]
    raise SystemExit("the broker wrote no CA certificate: " + logs)


def agent_argv(image: str, home: Path, ca: Path, env: dict[str, str], cmd: list[str]) -> list[str]:
    args = [
        "docker", "run", "--rm", "--init", "--read-only", "--cap-drop=ALL",
        "--security-opt=no-new-privileges", "--user", "65534:65534", "--network", INTERNAL,
        "--memory", "2g", "--pids-limit", "256", "--tmpfs", "/tmp:rw,size=256m",
        "--mount", f"type=bind,src={home},dst=/home/agent",
        "--mount", f"type=bind,src={ca},dst=/broker-ca/ca.pem,readonly",
        "-e", "HOME=/home/agent", "-e", f"HTTPS_PROXY=http://{BROKER}:8080",
        "-e", f"HTTP_PROXY=http://{BROKER}:8080", "-e", "NO_PROXY=localhost,127.0.0.1",
        "-e", "NODE_EXTRA_CA_CERTS=/broker-ca/ca.pem",
    ]  # fmt: skip
    for key, value in env.items():
        args += ["-e", f"{key}={value}"]
    return [*args, image, *cmd]


MARKERS = r"""
echo "UID $(id -u)"
t="${CLAUDE_CODE_OAUTH_TOKEN:-}"
if [ -n "$t" ]; then echo "ENV_TOKEN_SET len=${#t}"; fi
ls -a "$HOME" "$HOME/.codex" 2>/dev/null | tr '\n' ' '; echo
echo PROBE_END
"""


def broker_log() -> list[dict[str, Any]]:
    out = sh(["docker", "logs", BROKER]).stdout.decode(errors="replace")
    rows = []
    for line in out.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and "event" in value:
            rows.append(value)
    return rows


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("driver", choices=["claude", "codex"])
    ap.add_argument("--token-file", type=Path)
    ap.add_argument("--codex-home", type=Path)
    ap.add_argument("--model")
    ap.add_argument(
        "--container-profile", type=Path,
        default=REPO / "deployment" / "local-container-app-amplai-foundry.json",
    )  # fmt: skip
    ap.add_argument("--out", type=Path, default=SPEC / "broker-probe.json")
    a = ap.parse_args(argv)
    image = json.loads(a.container_profile.read_text())["image"]
    ROOT.mkdir(parents=True, exist_ok=True)
    home = ROOT / f"home-{a.driver}"
    shutil.rmtree(home, ignore_errors=True)
    home.mkdir()
    home.chmod(0o777)
    if a.driver == "claude":
        if a.token_file is None:
            raise SystemExit("claude needs --token-file")
        credential = a.token_file.expanduser().absolute()
        secret = env_secret(credential, "CLAUDE_CODE_OAUTH_TOKEN")
        placeholder = "sk-ant-oat01-AMPLAI-BROKER-PLACEHOLDER-" + secrets.token_hex(24)
        fmt = "env:CLAUDE_CODE_OAUTH_TOKEN"
        env = {"CLAUDE_CODE_OAUTH_TOKEN": placeholder}
        model = a.model or "claude-sonnet-5"
        cmd = ["claude", *ISOLATION, "-p", "Reply with exactly: PONG", "--output-format",
               "json", "--model", model, "--max-turns", "1"]  # fmt: skip
    else:
        if a.codex_home is None:
            raise SystemExit("codex needs --codex-home (the operator's scoped copy)")
        credential = (a.codex_home.expanduser() / ".codex" / "auth.json").absolute()
        real = json.loads(credential.read_text())
        secret = real["tokens"]["access_token"]
        # the same claims, no signature and no refresh token: nothing here can authenticate
        fake = json.loads(json.dumps(real))
        tag = secrets.token_hex(16)
        for key in ("access_token", "id_token"):
            head, payload, _signature = real["tokens"][key].split(".")
            fake["tokens"][key] = f"{head}.{payload}.AMPLAIBROKERPLACEHOLDER{key[:2]}{tag}"
        fake["tokens"]["refresh_token"] = "AMPLAIBROKERPLACEHOLDERrefresh" + tag
        placeholder = fake["tokens"]["access_token"]
        fmt = "json:tokens.access_token"
        (home / ".codex").mkdir()
        (home / ".codex").chmod(0o777)
        (home / ".codex" / "auth.json").write_text(json.dumps(fake))
        (home / ".codex" / "auth.json").chmod(0o666)
        env = {"SSL_CERT_FILE": "/tmp/bundle.pem"}
        model = a.model or "gpt-5.6-sol"
        codex = ("codex --ask-for-approval never exec --json --model " + model
                 + " --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox"
                 + " 'Reply with exactly: PONG'")  # fmt: skip
        # rustls reads SSL_CERT_FILE as the whole trust store: the system bundle plus the CA
        cmd = ["sh", "-c", "cat /etc/ssl/certs/ca-certificates.crt /broker-ca/ca.pem"
               " > /tmp/bundle.pem && cd /tmp && " + codex]  # fmt: skip
    ca = start_broker(credential, placeholder, fmt, ALLOW[a.driver])
    try:
        began = time.monotonic()
        turn = sh(agent_argv(image, home, ca, env, cmd), timeout=600)
        seconds = round(time.monotonic() - began, 1)
        side = sh(agent_argv(image, home, ca, env, ["sh", "-c", MARKERS]), timeout=120)
        log = broker_log()
    finally:
        sh(["docker", "rm", "-f", BROKER])
    blobs = [turn.stdout, turn.stderr, side.stdout, side.stderr, json.dumps(log).encode()]
    after = home / ".codex" / "auth.json"
    if after.is_file():  # did a refresh write real tokens back into the container's home?
        blobs.append(after.read_bytes())
    leaked = [i for i, blob in enumerate(blobs) if secret.encode() in blob]
    if leaked:
        print(json.dumps({"driver": a.driver, "error": "SECRET_SURFACED", "blobs": leaked}))
        return 2
    try:
        answer = json.loads(turn.stdout.decode())
    except ValueError:
        answer = {}
    if a.driver == "codex":  # a JSON event stream: the agent message and the turn end
        events = []
        for line in turn.stdout.decode(errors="replace").splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        texts = [
            e["item"].get("text", "")
            for e in events
            if isinstance(e.get("item"), dict) and e["item"].get("type") == "agent_message"
        ]
        failed = [e for e in events if e.get("type") in {"turn.failed", "error"}]
        answer = {"result": texts[-1] if texts else "", "is_error": bool(failed) or not texts,
                  "events": sorted({str(e.get("type")) for e in events})}  # fmt: skip
    result = {
        "driver": a.driver,
        "image": image,
        "broker_image": MITM,
        "turn_rc": turn.returncode,
        "turn_seconds": seconds,
        "turn_result": str(answer.get("result", ""))[:80] if isinstance(answer, dict) else "",
        "turn_is_error": answer.get("is_error") if isinstance(answer, dict) else None,
        "turn_events": answer.get("events") if isinstance(answer, dict) else None,
        "turn_stderr_tail": turn.stderr.decode(errors="replace")[-300:].replace(
            placeholder, "<placeholder>"
        ),
        "broker_requests": [r for r in log if r.get("event") == "request"],
        "broker_responses": [r for r in log if r.get("event") == "response"],
        "broker_denied": [r for r in log if r.get("event") == "deny"],
        "container_markers": side.stdout.decode(errors="replace")
        .replace(placeholder, "<placeholder>")
        .split("\n"),
        "placeholder_length": len(placeholder),
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    report = (
        json.loads(a.out.read_text()) if a.out.is_file() else {"schema_version": "1.0", "runs": {}}
    )
    report["runs"][a.driver] = result
    body = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if secret in body:
        raise SystemExit("refusing to write a report that contains the secret")
    a.out.write_text(body)
    print(
        json.dumps(
            {k: result[k] for k in ("turn_rc", "turn_result", "turn_is_error", "turn_seconds")}
        )
    )
    print(
        json.dumps(
            [
                {k: r.get(k) for k in ("target", "path", "replaced_headers")}
                for r in result["broker_requests"]
            ]
        )[:1500]
    )
    print(json.dumps(result["broker_responses"])[:800])
    print(result["container_markers"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
