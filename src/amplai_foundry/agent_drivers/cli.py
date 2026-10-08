"""Version-pinned CLI transport with durable dispatch and exact native sessions.

The transport reports observations, never verifies a goal. `qualified=True` only
unlocks the adapter; scheduler admission still needs the exact signed profile and
sandbox qualification. No host-shell fallback or automatic orphan replay exists.
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from amplai_foundry.runtime.contracts.identity import digest
from amplai_foundry.runtime.errors import Conflict, Hold, RuntimeFault

from . import answer_lookup, offline
from .protocol import EventNormalizer, JsonlDecoder, SessionJournal

if TYPE_CHECKING:
    from amplai_foundry.runtime.execution.cells import DispatchOptions
    from amplai_foundry.sandbox.container import ContainerSandbox

ACTIVE = frozenset({"starting", "running", "cancelling", "unknown"})
TERMINAL = frozenset({"completed", "failed", "cancelled", "paused"})
# the credential a SeededCodexPort leases into a dispatch home (runtime/execution/codex.py AUTH)
CODEX_AUTH = Path(".codex") / "auth.json"
# Operator decision (C), 2026-10-08: Codex hosted web search is off for every dispatch. Codex CLI
# 0.155.1 (the pinned app image) reads the config key ``web_search``: `codex -c
# web_search=bogus features list` fails with "unknown variant `bogus`, expected one of `disabled`,
# `cached`, `indexed`, `live`", and `codex --help` says "--search  Enable live web search". The
# override is an argv `-c` (`codex exec --help` and `codex exec resume --help`: "-c, --config
# <key=value>  Override a configuration value that would otherwise be loaded from
# ~/.codex/config.toml"), so a config.toml the agent can write in its home never re-enables it.
CODEX_WEB_SEARCH_OFF = ("-c", 'web_search="disabled"')
# config keys that would turn web search (or, on a trial, a networked feature) back on; never
# accepted through DispatchOptions
CODEX_WEB_KEYS = frozenset({
    "web_search", "tools.web_search", "features.web_search_request", "features.web_search_cached",
    "features.standalone_web_search",
    *(f"features.{name}" for name in offline.CODEX_NETWORK_FEATURES),
})  # fmt: skip


def _holds(value: Any, literals: frozenset[str] | set[str]) -> bool:
    """IC-28: whether a string anywhere in ``value`` contains one of ``literals``
    (``meta_harness/traces.py``, imported lazily: drivers depend on it only when capturing)."""
    from amplai_foundry.meta_harness.traces import holds_credential

    return holds_credential([value], literals)


class CliDriver:
    # Work 033 S4 (interfaces.md §3.4): argv/prepare/checkpoint/resume take DispatchOptions
    accepts_options: ClassVar[bool] = True
    CLAUDE_DEFAULT_TOOLS: ClassVar[str] = "Read,Edit,Write,Glob,Grep,Bash"

    def __init__(
        self,
        provider: str,
        binary: str,
        version: str,
        sandbox: ContainerSandbox,
        journal: SessionJournal,
        *,
        model: str,
        qualified: bool = False,
        environment: dict[str, str] | None = None,
        max_seconds: float = 3600,
        auth: str = "api_key",
    ) -> None:
        if provider not in {"claude", "codex"}:
            raise RuntimeFault("DRIVER_KIND", "Unknown CLI provider")
        if auth not in {"api_key", "oauth_token"}:
            raise RuntimeFault("DRIVER_AUTH", "auth must be api_key or oauth_token")
        # Claude Code's --bare never reads OAuth; a subscription token (claude setup-token,
        # CLAUDE_CODE_OAUTH_TOKEN) therefore needs the non-bare argv with explicit isolation
        # flags. The sandbox (tmpfs HOME, clean workspace) supplies the rest of bare's intent.
        if auth == "oauth_token" and "CLAUDE_CODE_OAUTH_TOKEN" not in (environment or {}):
            raise Hold("AUTH_TOKEN_REQUIRED", "oauth_token auth needs CLAUDE_CODE_OAUTH_TOKEN")
        self.auth = auth
        if not model or model in {"latest", "default", "auto"}:
            raise Hold("MODEL_UNPINNED", "An explicit model profile is required")
        if not version or not 0 < max_seconds <= 86400:
            raise RuntimeFault("DRIVER_PROFILE", "Version and bounded runtime required")
        self.provider, self.binary, self.version = provider, binary, version
        self.sandbox, self.journal, self.model = sandbox, journal, model
        self.qualified, self.environment = qualified, dict(environment or {})
        blocked = {
            "HOME",
            "PATH",
            "LD_PRELOAD",
            "LD_LIBRARY_PATH",
            "PYTHONPATH",
            "NODE_OPTIONS",
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
        }
        if blocked & self.environment.keys():
            raise Hold(
                "ENV_AUTHORITY", "Credential injection cannot replace process/runtime settings"
            )
        self.max_seconds = max_seconds
        self.processes: dict[str, subprocess.Popen[bytes]] = {}
        self.threads: dict[str, threading.Thread] = {}
        # Work 033 S13 (§9.1): the sanitized trace of each dispatch prepared with
        # ``options.capture_trace`` (trial goals only), kept in memory until ``destroy``
        self._traces: dict[str, Any] = {}
        # IC-28 (provisional): a captured dispatch's own credential literals (its injected
        # environment values; for Codex the leased auth.json as seeded and as left by the run),
        # the Codex home they are read from, and the dispatches whose raw events held one
        self._credentials: dict[str, set[str]] = {}
        self._credential_homes: dict[str, Path] = {}
        self._credential_hits: set[str] = set()
        # operator decision (C): each dispatch's answer-lookup evidence (``answer_lookup.py``)
        self._lookups: dict[str, list[dict[str, str]]] = {}
        self.native_root = journal.root / "native"
        self.native_root.mkdir(mode=0o700, exist_ok=True)

    @staticmethod
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

    @property
    def offline_tools(self) -> dict[str, Any]:
        """How a trial dispatch turns the web tools off (``agent_drivers/offline.py``): the
        arguments ``argv`` adds when ``options.offline`` is set."""
        if self.provider == "claude":
            no_mcp = list(offline.CLAUDE_NO_MCP) if self.auth == "api_key" else []
            return {"argv": [*offline.CLAUDE_OFFLINE, *no_mcp]}
        return {
            "argv": [
                *CODEX_WEB_SEARCH_OFF,
                *offline.CODEX_FEATURES_OFF,
                *offline.CODEX_IGNORE_CONFIG,
            ]
        }

    def _options(self, options: DispatchOptions | None) -> DispatchOptions | None:
        """None for no options or default options (today's argv, golden G2)."""
        if options is None:
            return None
        if options.model != self.model:
            # a port is built and qualified for one model (codex.py build_*_port)
            raise Hold(
                "MODEL_NOT_QUALIFIED_FOR_PORT",
                "The options name a model this port is not built for",
                details={"port": self.model, "options": options.model},
            )
        if self.provider == "codex" and (
            options.max_turns is not None
            or options.append_system_prompt is not None
            or options.allowed_tools is not None
        ):
            raise Hold("DRIVER_OPTIONS_UNSUPPORTED", "Claude options on the Codex CLI")
        if self.provider == "claude" and options.codex_config:
            raise Hold("DRIVER_OPTIONS_UNSUPPORTED", "Codex config overrides on the Claude CLI")
        if any(key in CODEX_WEB_KEYS for key, _ in options.codex_config):
            raise Hold("DRIVER_OPTIONS_UNSUPPORTED", "Codex web search stays off (decision (C))")
        return None if options.is_default() else options

    def argv(
        self,
        prompt: str,
        *,
        session: str | None = None,
        output_schema: dict[str, Any] | None = None,
        options: DispatchOptions | None = None,
    ) -> list[str]:
        import json

        self.exact_session(session)
        if not isinstance(prompt, str) or len(prompt.encode()) > 1024 * 1024:
            raise Hold("PROMPT_LIMIT", "Driver prompt exceeds its configured budget")
        opts = self._options(options)
        if self.provider == "claude":
            isolation = (
                ["--bare"]
                if self.auth == "api_key"
                else [
                    "--setting-sources",
                    "",
                    "--strict-mcp-config",
                    "--disable-slash-commands",
                    "--no-chrome",
                ]
            )
            args = [
                self.binary,
                *isolation,
                "-p",
                prompt,
                "--output-format",
                "stream-json",
                "--verbose",
                "--model",
                self.model,
            ]
            # §3.4 argv contract: effort, then max-turns and system prompt, then the tools
            # (`claude --help` of 2.1.278 quotes --effort, --append-system-prompt and
            # --allowedTools; --max-turns is not listed, §14 Q13, specs/033-.../cli-effort-facts.md)
            if opts is not None and opts.effort is not None:
                args += ["--effort", opts.effort]
            if opts is not None and opts.max_turns is not None:
                args += ["--max-turns", str(opts.max_turns)]
            if opts is not None and opts.append_system_prompt is not None:
                args += ["--append-system-prompt", opts.append_system_prompt]
            tools = (
                ",".join(opts.allowed_tools)
                if opts is not None and opts.allowed_tools is not None
                else self.CLAUDE_DEFAULT_TOOLS
            )
            args += ["--allowedTools", tools]
            if opts is not None and opts.offline:
                # operator decision 2026-10-08: a trial denies the network tools; the API-key
                # (--bare) argv also loads no MCP server (the OAuth argv already says so above)
                args += [*offline.CLAUDE_OFFLINE]
                if self.auth == "api_key":
                    args += [*offline.CLAUDE_NO_MCP]
            if session:
                args += ["--resume", session]
            if output_schema:
                args += ["--json-schema", json.dumps(output_schema, separators=(",", ":"))]
            return args
        args = [self.binary, "--ask-for-approval", "never", "exec"]
        if session:
            args += ["resume", session]
        # --skip-git-repo-check: workspaces are commit copies without .git. This is exactly the
        # argv scripts/container_qualify.py qualifies in the container.
        # Codex's own bwrap sandbox cannot create namespaces in the unprivileged container
        # (measured 2026-09-28: shell and file writes fail), so the qualified container is the
        # sandbox and Codex's is bypassed (D-073).
        args += ["--json", "--model", self.model]
        # `-c key=value` is an option of `codex exec` and of `codex exec resume` (Codex CLI
        # 0.155.1 help, specs/033-harness-taxonomy/runs/cli-effort-facts.md, §14 Q1); it sits
        # with --json/--model, which the qualified argv already passes after the session id
        if opts is not None and opts.effort is not None:
            args += ["-c", "model_reasoning_effort=" + opts.effort]
        for key, value in opts.codex_config if opts is not None else ():
            args += ["-c", key + "=" + value]
        # decision (C): last of the overrides, on every dispatch (first turn, resume, follow-up)
        args += [*CODEX_WEB_SEARCH_OFF]
        if opts is not None and opts.offline:
            # operator decision 2026-10-08: a trial also turns the networked features off and
            # never loads $CODEX_HOME/config.toml (MCP servers), on every turn of the session
            args += [*offline.CODEX_FEATURES_OFF, *offline.CODEX_IGNORE_CONFIG]
        args += ["--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox"]
        if output_schema is not None:
            raise Hold("SCHEMA_FILE_REQUIRED", "Codex needs a pinned read-only schema file")
        return [*args, prompt]

    def probe(self) -> dict[str, Any]:
        return {
            "driver_id": self.provider + "-cli",
            "configured_version": self.version,
            "auth": self.auth,
            "maturity": "qualified" if self.qualified else "disabled",
            "transport": "cli",
            "exact_sessions": True,
            "native_steering": False,
            "native_delegation": False,
            "observed_version": None,
            "qualification_required": [
                "binary_version",
                "container_escape",
                "egress",
                "auth",
                "jsonl",
                "child_budget",
                "cancel_tree",
                "resume_exact",
            ],
        }

    def _home(self, dispatch_id: str, native_home: Path | None) -> Path:
        self.journal._path(dispatch_id)  # Validate ID before joining a path.
        home = Path(native_home).absolute() if native_home else self.native_root / dispatch_id
        if home.resolve() != home or home.parent != self.native_root:
            raise Hold("SESSION_HOME", "Native state must be under the worker-owned session root")
        home.mkdir(mode=0o700, exist_ok=True)
        # The dedicated container UID must be able to write its private home.
        profile = getattr(self.sandbox, "profile", None)
        if profile and os.geteuid() == 0:
            os.chown(home, profile.uid, profile.gid)
        return home

    def prepare(
        self,
        dispatch: dict[str, Any],
        prompt: str,
        workspace: Path,
        *,
        session: str | None = None,
        native_home: Path | None = None,
        options: DispatchOptions | None = None,
    ) -> dict[str, Any]:
        if not self.qualified:
            raise Hold("DRIVER_UNQUALIFIED", "Exact-version environment qualification is required")
        self.exact_session(session)
        workspace = Path(workspace).absolute()
        if workspace.resolve() != workspace or not workspace.is_dir():
            raise Hold("WORKSPACE_PATH", "An isolated non-symlink workspace is required")
        opts = self._options(options)
        home = self._home(dispatch["dispatch_id"], native_home)
        if opts is not None and opts.capture_trace:
            self._trace_buffer(dispatch["dispatch_id"])
            self._seeded_credentials(dispatch["dispatch_id"], home)
        args = self.argv(prompt, session=session, options=opts)
        command = self.sandbox.command(
            args,
            workspace,
            dispatch["dispatch_id"],
            env_names=list(self.environment),
            native_home=home,
        )
        prepared = {
            "dispatch_id": dispatch["dispatch_id"],
            "command": command,
            "workspace": str(workspace),
            "native_home": str(home),
            "session": session,
        }
        # effort and options digest only for non-default options: a default dispatch keeps
        # today's journal bytes (checkpoint/resume compare them, §3.4). They enter the request
        # digest here and are stored with the immutable metadata below, since
        # SessionJournal.create keeps only the digest of its payload.
        bound = {"effort": opts.effort, "options_digest": opts.digest()} if opts is not None else {}
        record = self.journal.create(
            dispatch["dispatch_id"],
            {
                "dispatch_digest": digest(dispatch),
                "prompt_digest": digest(prompt),
                "prepared_digest": digest(prepared),
                "version": self.version,
                "model": self.model,
                **bound,
            },
        )
        # Immutable metadata is written before spawning. Duplicate prepare is safe.
        if record.get("prepared_digest") not in {None, digest(prepared)}:
            raise Conflict("PREPARED_CHANGED", "Dispatch command differs from durable preparation")
        if record.get("prepared_digest") is None:
            self.journal.update(
                dispatch["dispatch_id"],
                prepared_digest=digest(prepared),
                native_home=str(home),
                workspace=str(workspace),
                driver_version=self.version,
                model=self.model,
                **bound,
            )
        return prepared

    def start(self, prepared: dict[str, Any]) -> str:
        did: str = prepared["dispatch_id"]
        record = self.journal.read(did)
        if record.get("prepared_digest") != digest(prepared):
            raise Conflict(
                "PREPARED_CHANGED", "Do not modify argv, workspace or session after preparation"
            )
        if record["state"] != "prepared":
            if did not in self.processes and record["state"] in ACTIVE:
                raise Hold("ORPHAN_SESSION", "Reconcile the named container; never spawn twice")
            return did
        # A persisted transition makes duplicate concurrent starts fail closed.
        self.journal.transition(
            did, {"prepared"}, "starting", expected_version=record["row_version"]
        )
        env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL") if k in os.environ}
        env.update(self.environment)
        try:
            process = subprocess.Popen(
                prepared["command"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            # The host exec did not start: unlike transport loss this is established.
            self.journal.transition(
                did, {"starting"}, "failed", failure="spawn_failure", process_stopped=True
            )
            raise Hold("DRIVER_SPAWN", "Container launcher could not start") from exc
        self.processes[did] = process
        self.journal.transition(
            did, {"starting"}, "running", pid=process.pid, process_stopped=False
        )
        thread = threading.Thread(
            target=self._collect, args=(did, process, prepared["session"]), daemon=True
        )
        self.threads[did] = thread
        thread.start()
        threading.Thread(target=self._deadline, args=(did, process), daemon=True).start()
        return did

    def _deadline(self, did: str, process: subprocess.Popen[bytes]) -> None:
        try:
            process.wait(timeout=self.max_seconds)
        except subprocess.TimeoutExpired:
            try:
                self.cancel(did, reason="deadline")
            except Exception:
                self.journal.update(
                    did, state="unknown", failure="deadline_stop_unconfirmed", process_stopped=False
                )

    def _collect(
        self, did: str, process: subprocess.Popen[bytes], expected_session: str | None
    ) -> None:
        decoder = JsonlDecoder()
        normalizer = EventNormalizer(self.provider, expected_session=expected_session)
        stderr_overflow = threading.Event()
        stdout, stderr = process.stdout, process.stderr
        assert isinstance(stdout, io.BufferedReader) and stderr is not None

        def drain() -> None:
            size = 0
            while chunk := stderr.read(4096):
                size += len(chunk)
                if size > 4 * 1024 * 1024:
                    stderr_overflow.set()
                    with contextlib.suppress(Exception):
                        self.sandbox.stop(did)
                    break

        stderr_thread = threading.Thread(target=drain, daemon=True)
        stderr_thread.start()
        seq = 0

        buffer = self._traces.get(did)
        literals = frozenset(self._credentials.get(did) or ())

        def observe(event: dict[str, Any]) -> None:
            nonlocal seq
            if buffer is not None:
                # §9.1: the raw event, before the normalizer; the sanitizer never raises here
                buffer.add(event)
                # IC-28: the raw event (also one the sanitizer drops, or text it cuts) checked
                # for the dispatch's own credential literals
                if literals and did not in self._credential_hits and _holds(event, literals):
                    self._credential_hits.add(did)
            # decision (C): recorded before the normalizer, which may refuse the event
            self._note_lookups(did, event)
            normalized = normalizer.accept(event)
            seq += 1
            self.journal.append(did, f"provider-{seq}", normalized)
            self.journal.update(
                did,
                session_handle=normalizer.session,
                usage=normalizer.usage,
                usage_detail=normalizer.usage_detail,
            )

        try:
            while chunk := stdout.read1(16384):
                for event in decoder.feed(chunk):
                    observe(event)
            for event in decoder.feed(b"", final=True):
                observe(event)
            code = process.wait()
            self._left_credentials(did)  # IC-28: before the turn can be collected
            stderr_thread.join(timeout=1)
            stopped = self.sandbox.stopped(did) is True
            complete = (
                code == 0
                and normalizer.completed
                and not normalizer.failed
                and normalizer.session
                and not stderr_overflow.is_set()
            )
            current = self.journal.read(did)
            if current["state"] in {"cancelled", "paused", "cancelling"}:
                return  # Late completion can never erase a cancellation.
            self.journal.transition(
                did,
                {"running", "starting"},
                "completed" if complete and stopped else "failed" if stopped else "unknown",
                exit_code=code,
                session_handle=normalizer.session,
                usage=normalizer.usage,
                usage_detail=normalizer.usage_detail,
                process_stopped=stopped,
                failure=None if complete and stopped else "provider_completion_not_established",
            )
        except Exception as exc:
            stopped = False
            try:
                self.sandbox.stop(did)
                stopped = self.sandbox.stopped(did) is True
            except Exception:
                pass
            # Stop the launcher as well; containment confirmation still comes from sandbox.
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            current = self.journal.read(did)
            if current["state"] not in {"cancelled", "paused", "cancelling"}:
                self.journal.update(
                    did,
                    state="failed" if stopped else "unknown",
                    failure=getattr(exc, "code", type(exc).__name__),
                    process_stopped=stopped,
                )
        finally:
            for stream in (process.stdout, process.stderr):
                if stream:
                    stream.close()

    def poll(self, handle: str) -> dict[str, Any]:
        result = self.journal.read(handle)
        if result["state"] in ACTIVE and handle not in self.processes:
            raise Hold(
                "ORPHAN_SESSION", "Restarted worker must reconcile the exact named container"
            )
        return result

    def steer(self, handle: str, event: dict[str, Any]) -> dict[str, Any]:
        self.journal.read(handle)
        return {
            "status": "checkpoint_required",
            "native_applied": False,
            "reason": "No qualified mid-turn control channel in this CLI profile",
        }

    def pause(self, handle: str) -> dict[str, Any]:
        return self.cancel(handle, reason="checkpoint_pause")

    def cancel(self, handle: str, *, reason: str = "cancel") -> dict[str, Any]:
        record = self.journal.read(handle)
        # A turn that already failed with its process confirmed stopped keeps its record: a
        # cancel after the fact (the worker's ``_fail`` on DRIVER_BOUNDARY) must not overwrite
        # why it ended (``failure``, e.g. the stream fault ``_collect`` recorded) with "cancel".
        if record["state"] in {"cancelled", "paused", "failed"} and record.get("process_stopped"):
            return {
                "process_stopped": True,
                "session_handle": record["session_handle"],
                "state": record["state"],
            }
        if record["state"] == "prepared":
            state = "paused" if reason == "checkpoint_pause" else "cancelled"
            self.journal.transition(
                handle, {"prepared"}, state, process_stopped=True, failure=reason
            )
            return {"process_stopped": True, "session_handle": None, "state": state}
        self.journal.update(handle, state="cancelling", failure=reason)
        try:
            self.sandbox.stop(handle)
            if self.sandbox.stopped(handle) is not True:
                raise Hold("CANCEL_UNCONFIRMED", "Process tree is not positively stopped")
            process = self.processes.get(handle)
            if process:
                process.wait(timeout=15)
        except (OSError, subprocess.TimeoutExpired, RuntimeFault) as exc:
            self.journal.update(
                handle, state="unknown", process_stopped=False, failure="cancel_unconfirmed"
            )
            raise Hold(
                "CANCEL_UNCONFIRMED", "Stop acknowledgement is not termination evidence"
            ) from exc
        state = "paused" if reason == "checkpoint_pause" else "cancelled"
        record = self.journal.update(handle, state=state, process_stopped=True, failure=reason)
        return {"process_stopped": True, "session_handle": record["session_handle"], "state": state}

    def checkpoint(self, handle: str) -> dict[str, Any]:
        thread = self.threads.get(handle)
        if thread and thread is not threading.current_thread():
            thread.join(timeout=5)
        record = self.journal.read(handle)
        if record["state"] not in TERMINAL or record.get("process_stopped") is not True:
            raise Hold("CHECKPOINT_UNCONFIRMED", "Checkpoint needs confirmed process termination")
        return {
            "dispatch_id": handle,
            "session_handle": record["session_handle"],
            "journal_digest": digest(record),
            "driver_version": self.version,
            "model": self.model,
            "native_home": record.get("native_home"),
            "workspace": record.get("workspace"),
            "effort": record.get("effort"),
            "options_digest": record.get("options_digest"),
        }

    def resume(
        self,
        new_dispatch: dict[str, Any],
        prompt: str,
        workspace: Path,
        checkpoint: dict[str, Any],
        *,
        options: DispatchOptions | None = None,
    ) -> str:
        prior = self.journal.read(checkpoint["dispatch_id"])
        if (
            digest(prior) != checkpoint["journal_digest"]
            or prior.get("native_home") != checkpoint.get("native_home")
            or prior["session_handle"] != checkpoint["session_handle"]
        ):
            raise Hold("CHECKPOINT_RECEIPT", "Checkpoint differs from the durable driver journal")
        opts = self._options(options)
        if (
            checkpoint["driver_version"] != self.version
            or checkpoint["model"] != self.model
            # the resumed turn runs under the same effort and options (§3.4)
            or checkpoint.get("effort") != (opts.effort if opts is not None else None)
            or checkpoint.get("options_digest") != (opts.digest() if opts is not None else None)
        ):
            raise Hold("RESUME_PROFILE", "Native state cannot resume under a different profile")
        self.exact_session(checkpoint["session_handle"])
        if not checkpoint["session_handle"] or not checkpoint.get("native_home"):
            raise Hold("RESUME_SESSION", "Provider did not establish durable native state")
        if str(Path(workspace).resolve()) != checkpoint["workspace"]:
            raise Hold(
                "RESUME_WORKSPACE", "Native resume requires the same verified workspace path"
            )
        return self.start(
            self.prepare(
                new_dispatch,
                prompt,
                workspace,
                session=checkpoint["session_handle"],
                native_home=Path(checkpoint["native_home"]),
                options=opts,
            )
        )

    def _note_lookups(self, did: str, event: dict[str, Any]) -> None:
        """Operator decision (C), 2026-10-08: answer-lookup attempts in a raw event
        (``answer_lookup.scan_event``: a search outside the workspace, reading history beyond the
        base commit, a web search tool call) are kept in the dispatch's driver journal as
        ``answer_lookup`` (at most ``DISPATCH_MAX`` entries), where the worker reads them for the
        run. Evidence holding a secret pattern or one of the dispatch's credential values is
        withheld. A dispatch without a finding keeps its journal bytes."""
        found = answer_lookup.scan_event(self.provider, event)
        if not found:
            return
        literals = {v for v in self.environment.values() if len(v) >= 16}
        literals |= self._credentials.get(did) or set()
        kept = self._lookups.setdefault(did, [])
        if answer_lookup.merge(kept, answer_lookup.withheld(found, literals)):
            self.journal.update(did, answer_lookup=[dict(entry) for entry in kept])

    def answer_lookup(self, handle: str) -> list[dict[str, Any]]:
        """Decision (C): the answer-lookup evidence the dispatch's journal holds
        (``_note_lookups``); empty when it holds none (``worker.driver_lookups`` reads it)."""
        entries = self.journal.read(handle).get("answer_lookup")
        if not isinstance(entries, list):
            return []
        return [dict(entry) for entry in entries if isinstance(entry, dict)]

    def _trace_buffer(self, dispatch_id: str) -> Any:
        """The dispatch's trace buffer (``meta_harness/traces.py``, imported lazily: drivers do
        not depend on the meta-harness unless a trial captures). A duplicate prepare keeps it."""
        from amplai_foundry.meta_harness.traces import TraceBuffer

        return self._traces.setdefault(dispatch_id, TraceBuffer(self.provider))

    def _seeded_credentials(self, dispatch_id: str, home: Path) -> None:
        """IC-28: the captured dispatch's own credential literals at prepare: its injected
        environment values (the Claude OAuth token or API key) of at least ``CREDENTIAL_MIN``
        characters and, for Codex, the string values of the auth.json the port leased into its
        home (``SeededCodexPort.prepare`` seeds before ``prepare``)."""
        from amplai_foundry.meta_harness.traces import CREDENTIAL_MIN, read_credential_file

        literals = {v for v in self.environment.values() if len(v) >= CREDENTIAL_MIN}
        if self.provider == "codex":
            self._credential_homes[dispatch_id] = home
            literals |= read_credential_file(home / CODEX_AUTH)
        self._credentials[dispatch_id] = self._credentials.get(dispatch_id, set()) | literals

    def _left_credentials(self, did: str) -> None:
        """IC-28: a Codex dispatch's auth.json as the run left it (a refresh rotates the
        tokens), read after the process ended and before the port releases the lease."""
        home = self._credential_homes.get(did)
        if home is None:
            return
        from amplai_foundry.meta_harness.traces import read_credential_file

        with contextlib.suppress(Exception):
            left = read_credential_file(home / CODEX_AUTH)
            self._credentials[did] = self._credentials.get(did, set()) | left

    def trace(self, handle: str) -> dict[str, Any] | None:
        """The sanitized trace of a dispatch (Work 033 S13, §9.1): the snapshot of its buffer
        (``trace-sanitizer-v1``), or None when it was not prepared with ``capture_trace``.

        IC-28 (provisional, the read-only turn's rule, ``readonly_turn._trace``): when the raw
        events held one of the dispatch's own credential literals as seeded, or a kept text holds
        one of them as seeded or as left by the run, the snapshot keeps no item and counts a
        sanitizer error, so admission writes ``trace-drop`` ``sanitizer_error`` and stores
        nothing. A dispatch without any credential literal of at least ``CREDENTIAL_MIN``
        characters is not checked (the executor is not failed closed, unlike a read-only Codex
        turn)."""
        self.journal.read(handle)
        buffer = self._traces.get(handle)
        if buffer is None:
            return None
        snapshot: dict[str, Any] = buffer.snapshot()
        literals = self._credentials.get(handle) or set()
        kept = [snapshot.get("items"), snapshot.get("result")]
        if handle in self._credential_hits or (literals and _holds(kept, literals)):
            from amplai_foundry.meta_harness.traces import withhold

            withhold(snapshot)
        return snapshot

    def collect(self, handle: str) -> dict[str, Any]:
        record = self.poll(handle)
        if record["state"] != "completed" or record.get("process_stopped") is not True:
            raise Hold("DRIVER_NOT_COMPLETE", "Provider output is not a confirmed completed turn")
        return {
            "provider_completed": True,
            "goal_verified": False,
            "session_handle": record["session_handle"],
            "usage": record.get("usage"),
            "usage_detail": record.get("usage_detail"),
            "process_stopped": True,
            "event_count": record.get("cursor", 0),
        }

    def destroy(self, handle: str) -> None:
        record = self.journal.read(handle)
        if record["state"] not in TERMINAL or record.get("process_stopped") is not True:
            raise Hold(
                "DESTROY_RUNNING", "Confirm termination and reconcile effects before destruction"
            )
        if record.get("pid") is not None:
            self.sandbox.destroy(handle)
        self.processes.pop(handle, None)
        self.threads.pop(handle, None)
        self._traces.pop(handle, None)
        self._credentials.pop(handle, None)
        self._credential_homes.pop(handle, None)
        self._credential_hits.discard(handle)
        self._lookups.pop(handle, None)
        # Native home is retained for explicit operator-governed retention, never put in a ZIP.


class ClaudeCodeDriver(CliDriver):
    def __init__(
        self, version: str, sandbox: ContainerSandbox, journal: SessionJournal, **kwargs: Any
    ) -> None:
        super().__init__("claude", "claude", version, sandbox, journal, **kwargs)


class CodexCliDriver(CliDriver):
    def __init__(
        self, version: str, sandbox: ContainerSandbox, journal: SessionJournal, **kwargs: Any
    ) -> None:
        super().__init__("codex", "codex", version, sandbox, journal, **kwargs)
