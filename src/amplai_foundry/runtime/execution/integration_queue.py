"""Host-side merge of same-base part patches (Work 033 S9, interfaces.md §3.6, §5.2 strategy 8).

The orchestrator strategy (M5 parts + integration) runs its part nodes one at a time from the same
base (the ``sandbox:<app>`` exclusive claim serializes them, design 07 §4) and then an integration
node. ``IntegrationQueue.merge`` builds that node's base without an agent: it materializes the base
in a scratch copy, applies each part's cumulative patch with ``git apply --3way`` in part order,
commits every clean part, rolls a conflicting part back, and admits the merged tree as a base
snapshot (``GitWorkspaceManager.snapshot``: base descriptor + the cumulative patch from the base
commit). A clean merge gives the integration node that base; on conflicts it is the base of the
clean parts, and the caller puts the conflicting parts' patches in the integration prompt.

The integration node is verified like any node (re-verification, design 07 §4); this module never
decides anything about verification and never writes outside the workspace manager's root.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...sandbox.git_workspace import GitWorkspaceManager
from ..contracts.identity import new_id
from ..errors import Hold
from ..storage.store import Scope

Ref = dict[str, Any]


@dataclass(frozen=True)
class MergeResult:
    snapshot: Ref  # base + merged parts (cumulative patch)
    applied: tuple[str, ...]  # node ids merged cleanly
    conflicts: tuple[dict[str, Any], ...]  # {"node_id", "paths": [str]}


def _env() -> dict[str, str]:
    """git plumbing without user/system config, hooks, pager or prompts (as
    ``sandbox/git_workspace.py`` ``_git_env``), with a fixed identity for scratch commits."""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": "/nonexistent",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
        "LC_ALL": "C",
        "GIT_AUTHOR_NAME": "AMPLAI",
        "GIT_AUTHOR_EMAIL": "amplai@localhost",
        "GIT_COMMITTER_NAME": "AMPLAI",
        "GIT_COMMITTER_EMAIL": "amplai@localhost",
    }


class IntegrationQueue:
    def __init__(self, workspaces: GitWorkspaceManager, scope: Scope) -> None:
        self.workspaces, self.scope = workspaces, scope

    def merge(self, base: Ref, parts: list[tuple[str, Ref]]) -> MergeResult:
        """Merge the parts' changes (``(node_id, change artifact ref)``, in part order) onto
        ``base`` (a base snapshot). Host only, no agent. Hold MERGE_BASE when a part's change was
        made on another base commit; GIT_COPY when the scratch copy cannot be committed."""
        artifacts = self.workspaces.artifacts
        patches: list[tuple[str, bytes]] = []
        base_value = self.workspaces._descriptor(self.scope, base)
        for node_id, change in parts:
            change_base, patch = self.workspaces.read_change(
                self.scope, artifacts.read(self.scope, change)
            )
            value = self.workspaces._descriptor(self.scope, change_base)
            if (value["repo"], value["commit"]) != (base_value["repo"], base_value["commit"]):
                raise Hold(
                    "MERGE_BASE", "A part was made on another base commit",
                    details={"node_id": node_id},
                )  # fmt: skip
            patches.append((node_id, patch))
        scratch = self.workspaces.materialize(self.scope, new_id("merge"), base)
        try:
            # a base snapshot may carry a patch (applied uncommitted): commit it so the index
            # matches the working tree, as `git apply --3way` (which implies --index) requires
            self._git(scratch, "add", "-A")
            self._git(scratch, "commit", "-q", "--no-verify", "--allow-empty", "-m", "base")
            applied: list[str] = []
            conflicts: list[dict[str, Any]] = []
            for node_id, patch in patches:
                if not patch.strip():
                    applied.append(node_id)  # nothing to merge
                    continue
                run = subprocess.run(
                    ["git", "-c", "core.hooksPath=/dev/null", "apply", "--3way", "--binary",
                     "--whitespace=nowarn", "-"],
                    input=patch, cwd=scratch, env=_env(), capture_output=True, timeout=300,
                    check=False,
                )  # fmt: skip
                if run.returncode == 0:
                    self._git(scratch, "add", "-A")
                    self._git(scratch, "commit", "-q", "--no-verify", "--allow-empty", "-m",
                              "part " + node_id)  # fmt: skip
                    applied.append(node_id)
                    continue
                unmerged = self._lines(scratch, "diff", "--name-only", "--diff-filter=U")
                paths = unmerged or self._patch_paths(scratch, patch)
                conflicts.append({"node_id": node_id, "paths": paths})
                # roll the conflicting part back to the last clean part
                self._git(scratch, "reset", "-q", "--hard", "HEAD")
                self._git(scratch, "clean", "-q", "-f", "-d")
            snapshot = self.workspaces.snapshot(self.scope, scratch)
        finally:
            self.workspaces.discard(scratch)
        return MergeResult(snapshot, tuple(applied), tuple(conflicts))

    def patch_text(self, change: Ref) -> str:
        """The cumulative patch of a part's change, as text (for the integration prompt)."""
        raw = self.workspaces.artifacts.read(self.scope, change)
        _base, patch = self.workspaces.read_change(self.scope, raw)
        return patch.decode(errors="replace")

    @staticmethod
    def _git(cwd: Path, *args: str) -> bytes:
        run = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "gc.auto=0",
             "-c", "maintenance.auto=false", *args],
            cwd=cwd, env=_env(), capture_output=True, timeout=300, check=False,
        )  # fmt: skip
        if run.returncode != 0:
            raise Hold(
                "GIT_COPY", "Could not merge in the scratch copy",
                details={"args": list(args[:1]), "stderr": run.stderr.decode()[-400:]},
            )  # fmt: skip
        return run.stdout

    def _lines(self, cwd: Path, *args: str) -> list[str]:
        out = self._git(cwd, *args).decode(errors="replace")
        return sorted({line.strip() for line in out.splitlines() if line.strip()})

    @staticmethod
    def _patch_paths(cwd: Path, patch: bytes) -> list[str]:
        """The paths a patch touches, as ``git apply --numstat`` reads them (not applied)."""
        run = subprocess.run(
            ["git", "apply", "--numstat", "-"], input=patch, cwd=cwd, env=_env(),
            capture_output=True, timeout=300, check=False,
        )  # fmt: skip
        paths = set()
        for line in run.stdout.decode(errors="replace").splitlines():
            fields = line.split("\t", 2)
            if len(fields) == 3 and fields[2]:
                paths.add(fields[2])
        return sorted(paths)
