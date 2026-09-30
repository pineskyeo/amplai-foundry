# ruff: noqa
# mypy: ignore-errors
# Copied from src/amplai_foundry/agent_drivers/cli.py at c9f896a (exact_session :83-93,
# argv :95-151) and src/amplai_foundry/runtime/execution/planner_codex.py at c9f896a
# (argv :220-225, claude_argv :343-349); do not edit. Work 033 S0 golden oracle G2, adapted
# only to take plain arguments instead of self.

import json
from typing import Any

from amplai_foundry.runtime.errors import Hold


def exact_session(session: str | None) -> str | None:
    if session is not None and (
        not isinstance(session, str)
        or not session
        or session.startswith("-")
        or session in {"latest", "continue"}
        or len(session) > 512
        or any(ord(c) < 32 for c in session)
    ):
        raise Hold("SESSION_UNPINNED", "Only an exact native session ID is accepted")
    return session


def argv(
    provider: str,
    binary: str,
    model: str,
    auth: str,
    prompt: str,
    *,
    session: str | None = None,
    output_schema: dict[str, Any] | None = None,
) -> list[str]:
    import json

    exact_session(session)
    if not isinstance(prompt, str) or len(prompt.encode()) > 1024 * 1024:
        raise Hold("PROMPT_LIMIT", "Driver prompt exceeds its configured budget")
    if provider == "claude":
        isolation = (
            ["--bare"]
            if auth == "api_key"
            else [
                "--setting-sources",
                "",
                "--strict-mcp-config",
                "--disable-slash-commands",
                "--no-chrome",
            ]
        )
        args = [
            binary,
            *isolation,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            model,
            "--allowedTools",
            "Read,Edit,Write,Glob,Grep,Bash",
        ]
        if session:
            args += ["--resume", session]
        if output_schema:
            args += ["--json-schema", json.dumps(output_schema, separators=(",", ":"))]
        return args
    args = [binary, "--ask-for-approval", "never", "exec"]
    if session:
        args += ["resume", session]
    # --skip-git-repo-check: workspaces are commit copies without .git. This is exactly the
    # argv scripts/container_qualify.py qualifies in the container.
    # Codex's own bwrap sandbox cannot create namespaces in the unprivileged container
    # (measured 2026-09-28: shell and file writes fail), so the qualified container is the
    # sandbox and Codex's is bypassed (D-073).
    args += [
        "--json", "--model", model, "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
    ]  # fmt: skip
    if output_schema is not None:
        raise Hold("SCHEMA_FILE_REQUIRED", "Codex needs a pinned read-only schema file")
    return [*args, prompt]


def codex_planner_argv(model: str, prompt: str) -> list[str]:
    return [
        "codex", "--ask-for-approval", "never", "exec", "--json", "--model", model,
        "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox",
        "--output-schema", "/amplai-input/plan-schema.json", prompt,
    ]  # fmt: skip


# copied from planner_codex.py:323 at c9f896a; frozen so a widened allowlist fails G2
READ_ONLY_TOOLS = "Read,Glob,Grep"


def claude_planner_argv(model: str, prompt: str, schema: dict[str, Any]) -> list[str]:
    return [
        "claude", "--setting-sources", "", "--strict-mcp-config", "--disable-slash-commands",
        "--no-chrome", "-p", prompt, "--output-format", "stream-json", "--verbose",
        "--model", model, "--allowedTools", READ_ONLY_TOOLS,
        "--json-schema", json.dumps(schema, separators=(",", ":")),
    ]  # fmt: skip
