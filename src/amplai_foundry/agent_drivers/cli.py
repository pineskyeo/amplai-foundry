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
from typing import TYPE_CHECKING, Any

from amplai_foundry.runtime.contracts.identity import digest
from amplai_foundry.runtime.errors import Conflict, Hold, RuntimeFault

from .protocol import EventNormalizer, JsonlDecoder, SessionJournal

if TYPE_CHECKING:
    from amplai_foundry.sandbox.container import ContainerSandbox

ACTIVE = frozenset({"starting", "running", "cancelling", "unknown"})
TERMINAL = frozenset({"completed", "failed", "cancelled", "paused"})


class CliDriver:
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

    def argv(
        self,
        prompt: str,
        *,
        session: str | None = None,
        output_schema: dict[str, Any] | None = None,
    ) -> list[str]:
        import json

        self.exact_session(session)
        if not isinstance(prompt, str) or len(prompt.encode()) > 1024 * 1024:
            raise Hold("PROMPT_LIMIT", "Driver prompt exceeds its configured budget")
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
                "--allowedTools",
                "Read,Edit,Write,Glob,Grep,Bash",
            ]
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
        args += [
            "--json", "--model", self.model, "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
        ]  # fmt: skip
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
    ) -> dict[str, Any]:
        if not self.qualified:
            raise Hold("DRIVER_UNQUALIFIED", "Exact-version environment qualification is required")
        self.exact_session(session)
        workspace = Path(workspace).absolute()
        if workspace.resolve() != workspace or not workspace.is_dir():
            raise Hold("WORKSPACE_PATH", "An isolated non-symlink workspace is required")
        home = self._home(dispatch["dispatch_id"], native_home)
        args = self.argv(prompt, session=session)
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
        record = self.journal.create(
            dispatch["dispatch_id"],
            {
                "dispatch_digest": digest(dispatch),
                "prompt_digest": digest(prompt),
                "prepared_digest": digest(prepared),
                "version": self.version,
                "model": self.model,
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

        def observe(event: dict[str, Any]) -> None:
            nonlocal seq
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
        if record["state"] in {"cancelled", "paused"} and record.get("process_stopped"):
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
        }

    def resume(
        self, new_dispatch: dict[str, Any], prompt: str, workspace: Path, checkpoint: dict[str, Any]
    ) -> str:
        prior = self.journal.read(checkpoint["dispatch_id"])
        if (
            digest(prior) != checkpoint["journal_digest"]
            or prior.get("native_home") != checkpoint.get("native_home")
            or prior["session_handle"] != checkpoint["session_handle"]
        ):
            raise Hold("CHECKPOINT_RECEIPT", "Checkpoint differs from the durable driver journal")
        if checkpoint["driver_version"] != self.version or checkpoint["model"] != self.model:
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
            )
        )

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
