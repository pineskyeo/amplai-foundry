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
from typing import Any

from ...runtime.contracts.identity import new_id
from ...runtime.errors import Hold, RuntimeFault
from ...runtime.storage.store import Scope
from ...sandbox.container import ContainerSandbox
from ...sandbox.git_workspace import GitWorkspaceManager
from .service import VerificationObservation

TAIL = 4000


class PatchCommandVerifier:
    def __init__(
        self,
        command_id: str,
        argv: list[str],
        *,
        workspaces: GitWorkspaceManager,
        scope: Scope,
        sandbox: ContainerSandbox,
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
