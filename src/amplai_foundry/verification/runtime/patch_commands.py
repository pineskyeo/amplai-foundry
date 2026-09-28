"""Verify a change by running a pinned command on a fresh base copy with the patch applied (R6).

The runner receives only the change artifact bytes (``VerificationService.verify``). It rebuilds
the base commit from trusted repository configuration, applies the host-computed patch, and runs
one allowlisted argv inside a ContainerSandbox with ``network=none``. The worker's own workspace
is never reused, so nothing the agent left behind (caches, altered tests outside the patch, a
running process) can influence the verdict. Exit code 0 is the pass rule; pytest's "no tests
collected" (5) and every other code fail.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any, Protocol

from ...runtime.contracts.identity import new_id
from ...runtime.errors import Hold, RuntimeFault
from ...runtime.storage.store import Scope
from ...sandbox.git_workspace import GitWorkspaceManager
from .service import VerificationObservation

TAIL = 4000


class CommandSandbox(Protocol):
    """What the verifier needs: a profile with ``network`` and an argv builder."""

    @property
    def profile(self) -> Any: ...

    def command(self, argv: list[str], workspace: Path, run_name: str) -> list[str]: ...


class PatchCommandVerifier:
    def __init__(
        self,
        command_id: str,
        argv: list[str],
        *,
        workspaces: GitWorkspaceManager,
        scope: Scope,
        sandbox: CommandSandbox,
        timeout_seconds: int = 900,
    ) -> None:
        if not command_id or not argv or not 1 <= timeout_seconds <= 3600:
            raise RuntimeFault("VERIFIER_COMMAND", "Verifier command profile is incomplete")
        if getattr(getattr(sandbox, "profile", None), "network", None) != "none":
            raise RuntimeFault("VERIFIER_NETWORK", "A patch verifier runs with network=none")
        self.id, self.argv, self.timeout = command_id, tuple(argv), timeout_seconds
        self.workspaces, self.scope, self.sandbox = workspaces, scope, sandbox

    def __call__(self, raw: bytes) -> VerificationObservation:
        name = "amplai-verify-" + new_id("v")[-20:].replace("_", "-").lower()
        run_id = new_id("verify")
        started = time.time()
        try:
            workspace, patch = self.workspaces.materialize_change(self.scope, run_id, raw)
        except Hold as exc:
            # A change that does not apply to its base is a failed change, not a hold.
            if exc.code == "PATCH_APPLY":
                info = exc.details if isinstance(exc.details, dict) else {}
                return VerificationObservation(
                    "fail", "Patch does not apply to its base commit", dict(info), 1
                )
            raise
        details: dict[str, Any] = {
            "command_id": self.id,
            "argv": list(self.argv),
            "patch_bytes": len(patch),
        }
        try:
            command = self.sandbox.command(list(self.argv), workspace, name)
            try:
                result = subprocess.run(
                    command, capture_output=True, timeout=self.timeout, check=False
                )
            except subprocess.TimeoutExpired:
                subprocess.run(["docker", "kill", name], capture_output=True, check=False)
                details["seconds"] = round(time.time() - started, 1)
                return VerificationObservation(
                    "inconclusive", "Verifier exceeded its frozen timeout", details, None
                )
            details.update(
                seconds=round(time.time() - started, 1),
                stdout_tail=result.stdout.decode(errors="replace")[-TAIL:],
                stderr_tail=result.stderr.decode(errors="replace")[-TAIL:],
            )
            if result.returncode == 0:
                return VerificationObservation("pass", "Command succeeded", details, 0)
            return VerificationObservation(
                "fail", f"Command exited {result.returncode}", details, result.returncode
            )
        finally:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
            self.workspaces.discard(workspace)


class SuiteVerifier:
    """The app's acceptance suite: every installed command on one base + patch copy.

    A work node carries exactly one verifier profile (``Runtime.save_graph``: NODE_VERIFIER),
    so each app installs one suite and every acceptance binds to it. A change passes only if
    all commands exit 0, which also catches regressions outside the claimed acceptance. The
    last observation is reused for the same change bytes, so N acceptances run the suite once.
    """

    def __init__(
        self,
        commands: list[tuple[str, list[str], int]],
        *,
        workspaces: GitWorkspaceManager,
        scope: Scope,
        sandbox: CommandSandbox,
    ) -> None:
        if not commands:
            raise RuntimeFault("VERIFIER_COMMAND", "A suite needs at least one command")
        if getattr(getattr(sandbox, "profile", None), "network", None) != "none":
            raise RuntimeFault("VERIFIER_NETWORK", "A patch verifier runs with network=none")
        self.commands, self.workspaces, self.scope, self.sandbox = (
            commands, workspaces, scope, sandbox,
        )  # fmt: skip
        self._last: tuple[str, VerificationObservation] | None = None

    def __call__(self, raw: bytes) -> VerificationObservation:
        import hashlib

        key = hashlib.sha256(raw).hexdigest()
        if self._last and self._last[0] == key:
            return self._last[1]
        observation = self._run(raw)
        self._last = (key, observation)
        return observation

    def _run(self, raw: bytes) -> VerificationObservation:
        run_id = new_id("verify")
        try:
            workspace, patch = self.workspaces.materialize_change(self.scope, run_id, raw)
        except Hold as exc:
            if exc.code == "PATCH_APPLY":
                info = exc.details if isinstance(exc.details, dict) else {}
                return VerificationObservation(
                    "fail", "Patch does not apply to its base commit", dict(info), 1
                )
            raise
        results: list[dict[str, Any]] = []
        try:
            for command_id, argv, timeout in self.commands:
                name = "amplai-verify-" + new_id("v")[-20:].replace("_", "-").lower()
                started = time.time()
                try:
                    command = self.sandbox.command(list(argv), workspace, name)
                    try:
                        result = subprocess.run(
                            command, capture_output=True, timeout=timeout, check=False
                        )
                    except subprocess.TimeoutExpired:
                        subprocess.run(["docker", "kill", name], capture_output=True, check=False)
                        results.append(
                            {"command_id": command_id, "exit_code": None, "timed_out": True,
                             "seconds": round(time.time() - started, 1)}
                        )  # fmt: skip
                        return VerificationObservation(
                            "inconclusive",
                            f"{command_id} exceeded its timeout",
                            {"patch_bytes": len(patch), "commands": results},
                            None,
                        )
                finally:
                    subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
                entry = {
                    "command_id": command_id,
                    "argv": list(argv),
                    "exit_code": result.returncode,
                    "seconds": round(time.time() - started, 1),
                    "stdout_tail": result.stdout.decode(errors="replace")[-TAIL:],
                    "stderr_tail": result.stderr.decode(errors="replace")[-TAIL:],
                }
                results.append(entry)
                if result.returncode != 0:
                    return VerificationObservation(
                        "fail",
                        f"{command_id} exited {result.returncode}",
                        {"patch_bytes": len(patch), "commands": results,
                         "stdout_tail": entry["stdout_tail"], "stderr_tail": entry["stderr_tail"]},
                        result.returncode,
                    )  # fmt: skip
            return VerificationObservation(
                "pass",
                "All suite commands succeeded",
                {"patch_bytes": len(patch), "commands": results},
                0,
            )
        finally:
            self.workspaces.discard(workspace)


class NonEmptyChangeCheck:
    """Global check: every node produced a change that is not empty (a no-op is not done)."""

    def __init__(self, workspaces: GitWorkspaceManager, scope: Scope) -> None:
        self.workspaces, self.scope = workspaces, scope

    def __call__(
        self, contract: dict[str, Any], graph: dict[str, Any], outputs: dict[str, Any]
    ) -> VerificationObservation:
        empty = []
        for work_id, ports in outputs.items():
            for port, ref in ports.items():
                raw = self.workspaces.artifacts.read(self.scope, ref)
                _base, patch = self.workspaces.read_change(self.scope, raw)
                if not patch.strip():
                    empty.append(f"{work_id}:{port}")
        if empty:
            return VerificationObservation("fail", "No change was produced", {"empty": empty}, 1)
        return VerificationObservation("pass", "Every node produced a change", {}, 0)
