"""Read-only structured turns on Codex CLI and Claude Code (Work 033 S4, interfaces.md §3.5).

The body of the read-only planner turns (D-069, D-079), extracted so the planner, reviewer,
investigators, judges and proposer share one implementation: the model reads a read-only
workspace in the qualified container and returns one JSON object that must match ``schema``.
The workspace is mounted read-only by docker (Codex's own sandbox cannot run in the container,
D-073); Claude gets read-only tools as well. A turn never writes contract refs, digests,
capabilities or budgets.

With ``effort=None`` the argv is byte-identical to the planners at ``c9f896a`` (golden G2, G4).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from jsonschema import Draft202012Validator

from ...agent_drivers.protocol import JsonlDecoder
from ...sandbox.container import ContainerSandbox
from ..contracts.identity import digest, new_id
from ..errors import Hold
from .cells import LEGACY_EFFORT, check_effort_token
from .codex import ScopedCredential

SCHEMA_MOUNT = "/amplai-input/plan-schema.json"  # planner_codex.py:224, :275 at c9f896a
READ_ONLY_TOOLS = "Read,Glob,Grep"  # planner_codex.py:323 at c9f896a


@dataclass(frozen=True)
class TurnResult:
    output: dict[str, Any]
    usage: dict[str, Any] | None
    seconds: float
    events_digest: str


class ReadOnlyTurn(Protocol):
    cell_id: str

    def run(
        self,
        *,
        prompt: str,
        schema: dict[str, Any],
        workspace: Path,
        mounts: dict[str, Path] | None = None,
    ) -> TurnResult: ...


def _effort(effort: str | None) -> str | None:
    """None or a documented-shape token; "provider-default" means no flag (§2.4)."""
    if effort is None or effort == LEGACY_EFFORT:
        return None
    check_effort_token(effort)
    return effort


def _runs_root(runs_root: Path) -> Path:
    root = Path(runs_root).absolute()
    if root.resolve() != root:
        raise Hold("TURN_FAILED", "Read-only turn storage cannot traverse links")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def _run(command: list[str], name: str, timeout: int, env: dict[str, str] | None) -> Any:
    try:
        return subprocess.run(command, capture_output=True, timeout=timeout, check=False, env=env)
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "kill", name], capture_output=True, check=False)
        raise Hold("TURN_TIMEOUT", "Read-only turn exceeded its time budget") from None


def _check(output: Any, schema: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(output, dict):
        raise Hold("TURN_OUTPUT", "Read-only turn reply has no structured output")
    if list(Draft202012Validator(schema).iter_errors(output)):
        raise Hold("TURN_OUTPUT", "Read-only turn reply does not match its schema")
    return output


class CodexReadOnlyTurn:
    """``codex exec --output-schema`` on a read-only workspace mount (planner_codex.py:260-312)."""

    def __init__(
        self,
        sandbox: ContainerSandbox,
        credential: ScopedCredential,
        runs_root: Path,
        *,
        model: str,
        effort: str | None,
        cell_id: str,
        timeout_seconds: int = 900,
    ) -> None:
        self.sandbox, self.credential, self.model = sandbox, credential, model
        self.effort, self.cell_id = _effort(effort), cell_id
        self.runs_root = _runs_root(runs_root)
        self.timeout = timeout_seconds

    def argv(self, prompt: str) -> list[str]:
        # effort as a config override of `codex exec` (cli-effort-facts.md, §14 Q1), beside
        # --json/--model as in the executor argv (agent_drivers/cli.py)
        effort = ["-c", "model_reasoning_effort=" + self.effort] if self.effort else []
        return [
            "codex", "--ask-for-approval", "never", "exec", "--json", "--model", self.model,
            *effort, "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox",
            "--output-schema", SCHEMA_MOUNT, prompt,
        ]  # fmt: skip

    def run(
        self,
        *,
        prompt: str,
        schema: dict[str, Any],
        workspace: Path,
        mounts: dict[str, Path] | None = None,
    ) -> TurnResult:
        run = self.runs_root / new_id("plan")
        home = run / "home"
        home.mkdir(parents=True, mode=0o700)
        schema_path = run / "plan-schema.json"
        schema_path.write_text(json.dumps(schema))
        name = "amplai-plan-" + run.name[-20:].replace("_", "-").lower()
        self.credential.seed(home)
        started = time.time()
        try:
            command = self.sandbox.command(
                self.argv(prompt),
                workspace,
                name,
                native_home=home,
                readonly_mounts={SCHEMA_MOUNT: schema_path, **(mounts or {})},
                # read-only is the docker mount, not Codex's sandbox (D-073)
                workspace_readonly=True,
            )
            result = _run(command, name, self.timeout, None)
        finally:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
            self.credential.release(home)
        events = JsonlDecoder().feed(result.stdout, final=True)
        messages = [
            e["item"].get("text", "")
            for e in events
            if e.get("type") == "item.completed"
            and e.get("item", {}).get("type") == "agent_message"
        ]
        usage = next(
            (e.get("usage") for e in reversed(events) if e.get("type") == "turn.completed"), None
        )
        if result.returncode != 0 or not messages:
            raise Hold(
                "TURN_FAILED",
                "Read-only turn did not complete",
                details={"rc": result.returncode, "stderr": result.stderr.decode()[-400:]},
            )
        try:
            output = json.loads(messages[-1])
        except ValueError:
            raise Hold("TURN_OUTPUT", "Read-only turn reply is not JSON") from None
        return TurnResult(
            _check(output, schema), usage, round(time.time() - started, 1), digest(events)
        )


class ClaudeReadOnlyTurn:
    """Claude Code ``--json-schema`` with read-only tools (planner_codex.py:363-408).

    Structured output is the final ``result`` event's ``structured_output`` (measured in the app
    image, 2026-09-28); the OAuth token is passed by environment name.
    """

    def __init__(
        self,
        sandbox: ContainerSandbox,
        token: str,
        runs_root: Path,
        *,
        model: str,
        effort: str | None,
        cell_id: str,
        tools: str = READ_ONLY_TOOLS,
        timeout_seconds: int = 900,
    ) -> None:
        if not token:
            raise Hold("AUTH_TOKEN_REQUIRED", "Claude turns need CLAUDE_CODE_OAUTH_TOKEN")
        self.sandbox, self.token, self.model = sandbox, token, model
        self.effort, self.cell_id, self.tools = _effort(effort), cell_id, tools
        self.runs_root = _runs_root(runs_root)
        self.timeout = timeout_seconds

    def argv(self, prompt: str, schema: dict[str, Any]) -> list[str]:
        # `claude --help` 2.1.278: "--effort <level>" (cli-effort-facts.md, §14 Q15)
        effort = ["--effort", self.effort] if self.effort else []
        return [
            "claude", "--setting-sources", "", "--strict-mcp-config", "--disable-slash-commands",
            "--no-chrome", "-p", prompt, "--output-format", "stream-json", "--verbose",
            "--model", self.model, *effort, "--allowedTools", self.tools,
            "--json-schema", json.dumps(schema, separators=(",", ":")),
        ]  # fmt: skip

    def run(
        self,
        *,
        prompt: str,
        schema: dict[str, Any],
        workspace: Path,
        mounts: dict[str, Path] | None = None,
    ) -> TurnResult:
        run = self.runs_root / new_id("plan")
        home = run / "home"
        home.mkdir(parents=True, mode=0o700)
        name = "amplai-plan-" + run.name[-20:].replace("_", "-").lower()
        started = time.time()
        env = {**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": self.token}
        try:
            command = self.sandbox.command(
                self.argv(prompt, schema),
                workspace,
                name,
                env_names=["CLAUDE_CODE_OAUTH_TOKEN"],
                native_home=home,
                workspace_readonly=True,
                readonly_mounts=mounts or None,
            )
            result = _run(command, name, self.timeout, env)
        finally:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
        events = JsonlDecoder().feed(result.stdout, final=True)
        final = next((e for e in reversed(events) if e.get("type") == "result"), None)
        if result.returncode != 0 or not final or final.get("is_error"):
            raise Hold(
                "TURN_FAILED",
                "Read-only turn did not complete",
                details={"rc": result.returncode, "stderr": result.stderr.decode()[-400:]},
            )
        output = _check(final.get("structured_output"), schema)
        usage = final.get("usage") or {}
        return TurnResult(
            output,
            {k: usage.get(k) for k in ("input_tokens", "output_tokens")},
            round(time.time() - started, 1),
            digest(events),
        )
