"""Goal-level check of a multi-app change (Work 019 D-081).

Every node's change is first required to be non-empty (``NonEmptyChangeCheck``). Then each
operator-configured integration command whose apps are all part of the goal runs once with
every app's base + patch copy mounted read-only at ``/amplai-input/apps/<app>`` and an empty
writable ``/workspace``, in a ``network=none`` container. Exit 0 is the pass rule. The runner
receives only stored change artifacts; nothing the agents left in their workspaces is reused.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Protocol

from ...runtime.contracts.identity import new_id
from ...runtime.errors import Hold, RuntimeFault
from ...runtime.storage.store import Scope
from ...sandbox.git_workspace import GitWorkspaceManager
from .patch_commands import TAIL, NonEmptyChangeCheck
from .service import VerificationObservation


class MountingSandbox(Protocol):
    """ContainerSandbox as the integration check uses it: read-only app mounts."""

    @property
    def profile(self) -> Any: ...

    def command(
        self,
        argv: list[str],
        workspace: Path,
        run_name: str,
        *,
        readonly_mounts: dict[str, Path] | None = None,
    ) -> list[str]: ...


class IntegrationCheck:
    def __init__(
        self,
        commands: list[tuple[str, list[str], int, list[str]]],  # (id, argv, timeout, apps)
        *,
        workspaces: GitWorkspaceManager,
        scope: Scope,
        sandbox: MountingSandbox,
    ) -> None:
        if getattr(getattr(sandbox, "profile", None), "network", None) != "none":
            raise RuntimeFault("VERIFIER_NETWORK", "An integration check runs with network=none")
        self.commands, self.workspaces, self.scope, self.sandbox = (
            commands, workspaces, scope, sandbox,
        )  # fmt: skip
        self.nonempty = NonEmptyChangeCheck(workspaces, scope)

    def __call__(
        self, contract: dict[str, Any], graph: dict[str, Any], outputs: dict[str, Any]
    ) -> VerificationObservation:
        first = self.nonempty(contract, graph, outputs)
        if first.outcome != "pass":
            return first
        apps = {
            node["node_id"][len("node-") :]: node["node_id"]
            for node in graph["nodes"]
            if node["node_id"].startswith("node-")
        }
        wanted = [c for c in self.commands if set(c[3]) <= set(apps)]
        if not wanted:
            return VerificationObservation(
                "pass", "Every node produced a change; no integration command applies", {}, 0
            )
        copies = {}
        scratch = self.workspaces.root / new_id("integration")
        try:
            for app, node_id in apps.items():
                ports = outputs.get(node_id) or {}
                if len(ports) != 1:
                    raise Hold("INTEGRATION_INPUT", "Each node has exactly one change output")
                (ref,) = ports.values()
                raw = self.workspaces.artifacts.read(self.scope, ref)
                try:
                    copies[app], _ = self.workspaces.materialize_change(
                        self.scope, new_id("integration-" + app), raw
                    )
                except Hold as exc:
                    if exc.code == "PATCH_APPLY":
                        return VerificationObservation(
                            "fail", f"{app} patch does not apply to its base", {}, 1
                        )
                    raise
            scratch.mkdir(mode=0o700)
            mounts = {f"/amplai-input/apps/{app}": path for app, path in copies.items()}
            results: list[dict[str, Any]] = []
            for command_id, argv, timeout, _apps in wanted:
                name = "amplai-integration-" + new_id("i")[-20:].replace("_", "-").lower()
                started = time.time()
                try:
                    command = self.sandbox.command(
                        list(argv), scratch, name, readonly_mounts=mounts
                    )
                    try:
                        run = subprocess.run(
                            command, capture_output=True, timeout=timeout, check=False
                        )
                    except subprocess.TimeoutExpired:
                        subprocess.run(["docker", "kill", name], capture_output=True, check=False)
                        return VerificationObservation(
                            "inconclusive", f"{command_id} exceeded its timeout",
                            {"commands": results}, None,
                        )  # fmt: skip
                finally:
                    subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
                entry = {
                    "command_id": command_id,
                    "argv": list(argv),
                    "exit_code": run.returncode,
                    "seconds": round(time.time() - started, 1),
                    "stdout_tail": run.stdout.decode(errors="replace")[-TAIL:],
                    "stderr_tail": run.stderr.decode(errors="replace")[-TAIL:],
                }
                results.append(entry)
                if run.returncode != 0:
                    return VerificationObservation(
                        "fail", f"integration {command_id} exited {run.returncode}",
                        {"commands": results}, run.returncode,
                    )  # fmt: skip
            return VerificationObservation(
                "pass",
                "Every node produced a change; integration commands passed",
                {"commands": results},
                0,
            )
        finally:
            for path in copies.values():
                self.workspaces.discard(path)
            shutil.rmtree(scratch, ignore_errors=True)
