"""Read-only structured turns on Codex CLI and Claude Code (Work 033 S4, interfaces.md §3.5).

The body of the read-only planner turns (D-069, D-079), extracted so the planner, reviewer,
investigators, judges and proposer share one implementation: the model reads a read-only
workspace in the qualified container and returns one JSON object that must match ``schema``.
The workspace is mounted read-only by docker (Codex's own sandbox cannot run in the container,
D-073); Claude gets read-only tools as well. A turn never writes contract refs, digests,
capabilities or budgets.

With ``effort=None`` the argv is byte-identical to the planners at ``c9f896a`` (golden G2, G4).

Trace (Work 033 S13, interfaces.md §9.1): ``run(..., capture_trace=True)`` also returns the turn's
decoded events sanitized by ``trace-sanitizer-v1`` (``meta_harness/traces.py``) as
``TurnResult.trace``; the default captures nothing and leaves argv, prompt and result unchanged.
Only a trial goal whose ``TrialContext.capture_trace`` is true (development-split trials,
``local_executor.py``) asks for it; every other caller, and every real goal, calls ``run``
without it. Where the snapshot goes (§9.1 carrier rule, clarifications after the S10/S13/S14 fix
wave):

- Plan-time turns are turns ``"planner"`` of one goal-level trace ``trace-<goal_id>.planner``,
  admitted once at the end of ``LocalExecutionService.plan`` (``TraceService.admit_turns``): the
  planner draft and the L1 ``replan_ask_first`` turn (``planner_codex.py``, a planner with
  ``TRACES``), then the strategy runner's orchestrator lead, workgraph_split parts and plan_execute
  steps turns (``PlanContext.snapshots``, ``strategy_runner.py``), in the order they ran.
- Run-time turns (a reviewer round as ``"reviewer"``, an investigator as ``"investigator-<k>"``)
  are named turns of the run's trace: the strategy runner keeps them per node while the goal runs
  and ``ExecutionLoop._execute`` passes ``StrategyRunner.trace_source`` as the ``aux_traces``
  argument of ``WorkCoordinator.execute``/``continue_resumed``, whose single admission
  (``_admit_trace``) appends them after the executor turns. No run trace carries a ``"planner"``
  turn.

A captured turn also checks its decoded events for the literals of its own sandbox credential (the
Claude OAuth token it passes into the container; every string value of the leased Codex
``auth.json``, as seeded and as left after the run, since the refresh token rotates). The §9.2
``scan_secrets`` patterns (``runtime/evidence/cas.py:17-23``) know neither form, and the container
uid can read the Claude token from its environment (``specs/032-credential-broker-probe/
baseline-exposure.json``: ``ENV_TOKEN_PRESENT``, ``PROC_ENV_TOKEN_PRESENT``). A hit anywhere in the
raw events (also in an event the sanitizer drops, or across a cut point) empties the snapshot and
counts a sanitizer error, so admission writes ``trace-drop`` ``sanitizer_error`` and stores nothing
(``traces.TraceService.admit``); a captured Codex turn whose credential cannot be read fails closed
the same way. A partial echo of a credential is not recognised.

Operator decision (C), 2026-10-08 (interfaces.md clarification "Answer Lookup And Workspace
Objects"), applies to these turns as to the executor's dispatches (``agent_drivers/cli.py``):

- A Codex turn's argv carries ``-c web_search="disabled"`` (``CODEX_WEB_SEARCH_OFF``) as its last
  config override, right before ``--skip-git-repo-check``; golden G4 is amended by exactly that
  pair, as G2 is for the executor.
- Every turn's decoded events go through ``answer_lookup.scan_turn`` (a search outside the
  workspace, reading git history beyond the base commit, a web search or fetch tool call). The
  turn's own mounts (Codex ``SCHEMA_MOUNT``, the ``mounts`` it was given, e.g. a multi-app
  planner's ``/amplai-input/apps/<app>``) are not outside. Evidence holding a secret pattern or
  the turn's own credential (the Claude token; the leased Codex ``auth.json`` values, as seeded
  and as left) is withheld. Findings are returned inside the turn's usage as ``answer_lookup``
  (``LOOKUP_KEY``), so they travel wherever the usage is stored without a caller change: the
  plan record's ``planner_usage`` (``product.py``) and each ``aux_usage`` entry's ``usage``
  (``strategy_runner.py``), where ``LocalTrialExecutor._counters`` reads them and fails the
  trial. A turn without a finding returns exactly the usage it returned before; a turn that
  reported no usage but has a finding returns ``{"input_tokens": None, "output_tokens": None,
  "answer_lookup": [...]}`` (its usage stays unknown). A turn that fails (``TURN_FAILED``,
  ``TURN_OUTPUT``, ``TURN_TIMEOUT``) returns nothing, so its findings are not kept.

Operator decision 2026-10-08 (supersedes IC-35; ``agent_drivers/offline.py``): a read-only turn of
a trial goal runs with every web tool off. ``run(..., offline=True)`` adds the same arguments as
the executor's trial argv: Claude ``--disallowedTools`` with the network tools (its argv already
loads no MCP server, ``--strict-mcp-config``) right after ``--allowedTools``; Codex the networked
features off and ``--ignore-user-config`` right after the web search pair. Each turn declares them
as ``offline_tools``; the trial callers (``strategy_runner._aux_turn``, ``product.plan`` through
the planner) require that declaration and hold DRIVER_WEB_UNDECLARED without it. Without
``offline`` the argv is unchanged (golden G4). Judges, proposer, dreaming and effort probes
always pass it after the same check (the operator's 2026-10-08 statement: every test turn).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Protocol

from jsonschema import Draft202012Validator

from ...agent_drivers import answer_lookup
from ...agent_drivers import offline as web_off
from ...agent_drivers.cli import CODEX_WEB_SEARCH_OFF
from ...agent_drivers.protocol import (
    CLAUDE_CACHE_INPUT,
    CLAUDE_CACHE_WRITE_SPLIT,
    JsonlDecoder,
    input_total,
)
from ...meta_harness.traces import holds_credential, read_credential_file, withhold
from ...sandbox.container import ContainerSandbox
from ..contracts.identity import digest, new_id
from ..errors import Hold
from .cells import LEGACY_EFFORT, check_effort_token
from .codex import AUTH, ScopedCredential

SCHEMA_MOUNT = "/amplai-input/plan-schema.json"  # planner_codex.py:224, :275 at c9f896a
READ_ONLY_TOOLS = "Read,Glob,Grep"  # planner_codex.py:323 at c9f896a
# decision (C): the key of a turn's usage that holds its answer-lookup findings
LOOKUP_KEY = "answer_lookup"


@dataclass(frozen=True)
class TurnResult:
    output: dict[str, Any]
    usage: dict[str, Any] | None
    seconds: float
    events_digest: str
    # S13: the sanitized events of the turn when it ran with capture_trace, else None
    trace: dict[str, Any] | None = None


def _claude_usage(reported: Any) -> dict[str, Any]:
    """A Claude turn's counts in the shape a Codex turn reports them (``turn.completed``):
    ``input_tokens`` is every input token, cache included (``protocol.input_total``), and the
    cache classes it contains are kept under Anthropic's names, with the cache-write lifetime
    split Anthropic reports under ``cache_creation``. Anthropic's own ``input_tokens`` excludes
    cache, so the uncached input is ``input_tokens`` minus both cache fields
    (``protocol.turn_detail`` rebuilds the D-094 detail for pricing)."""
    usage = reported if isinstance(reported, dict) else {}
    out: dict[str, Any] = {k: usage.get(k) for k in ("input_tokens", "output_tokens")}
    if type(out["input_tokens"]) is not int or out["input_tokens"] < 0:
        return out  # not reported: the turn's usage stays unknown
    out["input_tokens"] = input_total("claude", usage)
    for k in CLAUDE_CACHE_INPUT:
        if type(usage.get(k)) is int and usage[k] >= 0:
            out[k] = usage[k]
    creation = usage.get("cache_creation")
    if isinstance(creation, dict):
        for k in CLAUDE_CACHE_WRITE_SPLIT:
            if type(creation.get(k)) is int and creation[k] >= 0:
                out[k] = creation[k]
    return out


def _with_lookups(usage: dict[str, Any] | None, found: list[dict[str, str]]) -> Any:
    """Decision (C): ``usage`` with the turn's answer-lookup findings under ``LOOKUP_KEY``;
    ``usage`` itself when there is none. A turn that reported no usage keeps its counts unknown
    (None) beside the findings."""
    if not found:
        return usage
    base = dict(usage) if isinstance(usage, dict) else {"input_tokens": None, "output_tokens": None}
    return {**base, LOOKUP_KEY: [dict(f) for f in found]}


def _trace(
    provider: str, events: list[dict[str, Any]], capture: bool, credentials: set[str]
) -> dict[str, Any] | None:
    """The turn's events through the default-deny sanitizer (a buffer snapshot), or None.

    ``credentials``: the literals of the turn's own sandbox credential. A hit in the raw events,
    or no literal to look for, empties the snapshot and counts an error (admission then drops the
    whole trace as ``sanitizer_error``, ``traces.py``)."""
    if not capture:
        return None
    from ...meta_harness.traces import TraceBuffer

    buffer = TraceBuffer(provider)
    for event in events:
        buffer.add(event)
    snapshot = buffer.snapshot()
    if not credentials or holds_credential(events, credentials):
        withhold(snapshot)
    return snapshot


class ReadOnlyTurn(Protocol):
    cell_id: str

    def run(
        self,
        *,
        prompt: str,
        schema: dict[str, Any],
        workspace: Path,
        mounts: dict[str, Path] | None = None,
        capture_trace: bool = False,  # S13 (§9.1): also return the sanitized events
        offline: bool = False,  # a trial turn: web tools off (operator decision 2026-10-08)
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

    # operator decision 2026-10-08: what ``argv(..., offline=True)`` adds after web search off
    offline_tools: ClassVar[dict[str, list[str]]] = {
        "argv": [*web_off.CODEX_FEATURES_OFF, *web_off.CODEX_IGNORE_CONFIG]
    }

    def argv(self, prompt: str, *, offline: bool = False) -> list[str]:
        # effort as a config override of `codex exec` (cli-effort-facts.md, §14 Q1), beside
        # --json/--model as in the executor argv (agent_drivers/cli.py)
        effort = ["-c", "model_reasoning_effort=" + self.effort] if self.effort else []
        # a trial turn: the networked features off, no config.toml (as CliDriver.argv)
        off = [*web_off.CODEX_FEATURES_OFF, *web_off.CODEX_IGNORE_CONFIG] if offline else []
        # decision (C): web search off, the last config override (as CliDriver.argv)
        return [
            "codex", "--ask-for-approval", "never", "exec", "--json", "--model", self.model,
            *effort, *CODEX_WEB_SEARCH_OFF, *off, "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox", "--output-schema", SCHEMA_MOUNT, prompt,
        ]  # fmt: skip

    def run(
        self,
        *,
        prompt: str,
        schema: dict[str, Any],
        workspace: Path,
        mounts: dict[str, Path] | None = None,
        capture_trace: bool = False,
        offline: bool = False,
    ) -> TurnResult:
        run = self.runs_root / new_id("plan")
        home = run / "home"
        home.mkdir(parents=True, mode=0o700)
        schema_path = run / "plan-schema.json"
        schema_path.write_text(json.dumps(schema))
        name = "amplai-plan-" + run.name[-20:].replace("_", "-").lower()
        self.credential.seed(home)
        # S13: the leased credential's literals (as seeded), for a captured turn's trace and, by
        # decision (C), for withholding answer-lookup evidence that holds one
        literals = read_credential_file(home / AUTH)
        readonly_mounts = {SCHEMA_MOUNT: schema_path, **(mounts or {})}
        started = time.time()
        try:
            command = self.sandbox.command(
                self.argv(prompt, offline=offline),
                workspace,
                name,
                native_home=home,
                readonly_mounts=readonly_mounts,
                # read-only is the docker mount, not Codex's sandbox (D-073)
                workspace_readonly=True,
            )
            result = _run(command, name, self.timeout, None)
        finally:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
            # and as left by the run (a refresh rotates the tokens)
            literals |= read_credential_file(home / AUTH)
            self.credential.release(home)
        events = JsonlDecoder().feed(result.stdout, final=True)
        # decision (C): the turn's answer-lookup attempts; its own mounts are not outside
        found = answer_lookup.scan_turn("codex", events, literals, inside=readonly_mounts)
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
            _check(output, schema),
            _with_lookups(usage, found),
            round(time.time() - started, 1),
            digest(events),
            _trace("codex", events, capture_trace, literals),
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

    # operator decision 2026-10-08: what ``argv(..., offline=True)`` adds after --allowedTools
    # (this argv already loads no MCP server: --strict-mcp-config without --mcp-config)
    offline_tools: ClassVar[dict[str, list[str]]] = {"argv": list(web_off.CLAUDE_OFFLINE)}

    def argv(self, prompt: str, schema: dict[str, Any], *, offline: bool = False) -> list[str]:
        # `claude --help` 2.1.278: "--effort <level>" (cli-effort-facts.md, §14 Q15)
        effort = ["--effort", self.effort] if self.effort else []
        off = list(web_off.CLAUDE_OFFLINE) if offline else []
        return [
            "claude", "--setting-sources", "", "--strict-mcp-config", "--disable-slash-commands",
            "--no-chrome", "-p", prompt, "--output-format", "stream-json", "--verbose",
            "--model", self.model, *effort, "--allowedTools", self.tools, *off,
            "--json-schema", json.dumps(schema, separators=(",", ":")),
        ]  # fmt: skip

    def run(
        self,
        *,
        prompt: str,
        schema: dict[str, Any],
        workspace: Path,
        mounts: dict[str, Path] | None = None,
        capture_trace: bool = False,
        offline: bool = False,
    ) -> TurnResult:
        run = self.runs_root / new_id("plan")
        home = run / "home"
        home.mkdir(parents=True, mode=0o700)
        name = "amplai-plan-" + run.name[-20:].replace("_", "-").lower()
        started = time.time()
        env = {**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": self.token}
        try:
            command = self.sandbox.command(
                self.argv(prompt, schema, offline=offline),
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
        # decision (C): the turn's answer-lookup attempts; its own mounts are not outside
        found = answer_lookup.scan_turn("claude", events, {self.token}, inside=mounts or {})
        return TurnResult(
            output,
            _with_lookups(_claude_usage(final.get("usage")), found),
            round(time.time() - started, 1),
            digest(events),
            _trace("claude", events, capture_trace, {self.token}),
        )
