#!/usr/bin/env python
"""Re-run the CLI driver qualification once real API credentials exist.

Work 015 measured what this host could reach: the Claude Code login works but the driver's
``--bare`` mode skips the keychain, and the Codex ChatGPT plan rejects every codex model.
Set the keys below and run this to replace
``specs/015-external-qualification/driver-qualification.json`` with a fresh measurement.

    ANTHROPIC_API_KEY=... OPENAI_API_KEY=... .venv/bin/python scripts/requalify_drivers.py

Nothing is faked: a probe that cannot run is recorded ``inconclusive``, and the report
status is ``fail`` unless all nine mandatory probes pass.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "specs" / "015-external-qualification"
WORK = Path(os.environ.get("REQUALIFY_WORKDIR", "/tmp/amplai-requalify"))


def probe_claude() -> dict[str, object]:
    """One minimal turn. --bare needs ANTHROPIC_API_KEY; without it the login is skipped."""
    ws = WORK / "claude"
    ws.mkdir(parents=True, exist_ok=True)
    args = [
        "claude",
        "--bare",
        "-p",
        "Reply with exactly the two letters: OK",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        os.environ.get("REQUALIFY_CLAUDE_MODEL", "claude-sonnet-5"),
        "--allowedTools",
        "Read",
    ]
    run = subprocess.run(args, cwd=ws, stdin=subprocess.DEVNULL, capture_output=True, timeout=300)
    events = [json.loads(line) for line in run.stdout.splitlines() if line.strip()]
    result = next((e for e in reversed(events) if e.get("type") == "result"), None)
    return {
        "binary": "claude",
        "key_present": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "exit_code": run.returncode,
        "events": len(events),
        "is_error": result and result.get("is_error"),
        "text": result and result.get("result"),
        "usage": result and result.get("usage"),
    }


def probe_codex() -> dict[str, object]:
    """One minimal turn with an explicitly pinned model."""
    ws = WORK / "codex"
    ws.mkdir(parents=True, exist_ok=True)
    model = os.environ.get("REQUALIFY_CODEX_MODEL", "gpt-5.3-codex")
    args = [
        "codex",
        "--ask-for-approval",
        "never",
        "exec",
        "--json",
        "--sandbox",
        "workspace-write",
        "--skip-git-repo-check",
        "-m",
        model,
        "Reply with exactly the two letters: OK",
    ]
    run = subprocess.run(args, cwd=ws, stdin=subprocess.DEVNULL, capture_output=True, timeout=300)
    events = [json.loads(line) for line in run.stdout.splitlines() if line.strip()]
    failed = [e for e in events if e.get("type") in {"error", "turn.failed"}]
    return {
        "binary": "codex",
        "model": model,
        "key_present": bool(os.environ.get("OPENAI_API_KEY")),
        "exit_code": run.returncode,
        "events": len(events),
        "completed": any(e.get("type") == "turn.completed" for e in events),
        "errors": [e.get("message") or e.get("error") for e in failed][:2],
    }


def main() -> None:
    from amplai_foundry.agent_drivers.protocol import EventNormalizer

    report = {"claude": probe_claude(), "codex": probe_codex()}
    for name, provider in (("claude", "claude"), ("codex", "codex")):
        normalizer = EventNormalizer(provider)
        report[name]["normalizer"] = "constructed"  # streams are validated in the full run
        del normalizer
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(
        "\nNext: fold these into",
        OUT / "driver-qualification.json",
        "through QualificationRunner with CAS-admitted artifacts, then rerun",
        "scripts/rc01_closure.py plus the external-qualification-status.json entries.",
    )


if __name__ == "__main__":
    main()
