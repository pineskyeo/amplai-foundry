"""Git commit workspaces and host-computed patches (D-068); not an OS security boundary.

A run's workspace is a ``git archive`` copy of one base commit of a registered repository:
dotfiles included, ``.git`` never. The agent only ever sees that copy through the container's
bind mount. After the process is confirmed stopped, the host computes the change as a binary
patch against the base commit with a temporary index, so the result is what the tree actually
contains, never a file the agent claims to have written.

The repository path comes from trusted configuration (``repos``), never from an artifact, so a
snapshot descriptor cannot point the host at another directory.
"""

from __future__ import annotations

import io
import os
import posixpath
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from ..runtime.contracts.identity import canonical, new_id
from ..runtime.contracts.registry import strict_json_loads
from ..runtime.errors import Hold, RuntimeFault
from ..runtime.evidence.cas import ArtifactStore
from ..runtime.storage.store import Scope

Ref = dict[str, Any]
BASE_MEDIA = "application/vnd.amplai.git-base+json"
PATCH_MEDIA = "text/x-diff"
CHANGE_MEDIA = "application/vnd.amplai.change+json"
PATCH_BINDING = "amplai:patch"
_SHA = re.compile(r"[0-9a-f]{40}")
_REPO_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")


def _git_env() -> dict[str, str]:
    # No user/system config, no hooks, no pager, no prompts: plumbing only.
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": "/nonexistent",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
        "LC_ALL": "C",
    }


def _safe_members(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
    """Regular files, directories and in-tree symlinks only; no absolute or parent paths.

    Checked here rather than relying on ``extractall(filter=...)``, which Python 3.11 only has
    from 3.11.4 (the worker image ships 3.11.2).
    """
    members = []
    for m in tar.getmembers():
        path = PurePosixPath(m.name)
        if path.is_absolute() or ".." in path.parts:
            raise Hold("ARCHIVE_PATH", "Archive member escapes the workspace")
        if m.issym():
            resolved = posixpath.normpath(posixpath.join(str(path.parent), m.linkname))
            if PurePosixPath(m.linkname).is_absolute() or resolved.split("/")[0] == "..":
                raise Hold("ARCHIVE_LINK", "Archive symlink points outside the workspace")
        elif not (m.isreg() or m.isdir()):
            raise Hold("ARCHIVE_SPECIAL", "Archive holds a link, device or special file")
        members.append(m)
    return members


class GitWorkspaceManager:
    """Duck-typed replacement for ``WorkspaceManager`` used by ``WorkCoordinator``."""

    def __init__(
        self,
        root: Path,
        artifacts: ArtifactStore,
        repos: dict[str, Path],
        *,
        max_tree_bytes: int = 512 * 1024 * 1024,
        max_patch_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        self.root = Path(root).absolute()
        if self.root.resolve() != self.root:
            raise Hold("WORKSPACE_ROOT", "Workspace storage cannot traverse links")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.repos: dict[str, Path] = {}
        for repo_id, path in repos.items():
            if not _REPO_ID.fullmatch(repo_id):
                raise RuntimeFault("GIT_REPO_ID", "Invalid repository id")
            real = Path(path).absolute()
            if real.resolve() != real or not (real / ".git").exists():
                raise Hold("GIT_REPO", "Registered repository must be a real git checkout")
            self.repos[repo_id] = real
        self.artifacts = artifacts
        self.max_tree_bytes, self.max_patch_bytes = max_tree_bytes, max_patch_bytes
        # The coordinator calls collect() without the base; remember it per materialized run.
        self._bases: dict[Path, Ref] = {}

    # -- git plumbing --------------------------------------------------------------------------
    def _git(
        self,
        repo: Path,
        *args: str,
        work_tree: Path | None = None,
        index: Path | None = None,
        input: bytes | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        env = _git_env()
        if index is not None:
            env["GIT_INDEX_FILE"] = str(index)
        cmd = ["git", "-c", "core.hooksPath=/dev/null", "--git-dir", str(repo / ".git")]
        if work_tree is not None:
            cmd += ["--work-tree", str(work_tree)]
        run = subprocess.run(
            [*cmd, *args], input=input, capture_output=True, env=env, timeout=300, check=False
        )
        if check and run.returncode != 0:
            raise Hold(
                "GIT_FAILED",
                "git plumbing failed",
                details={"args": list(args[:2]), "stderr": run.stderr.decode()[-400:]},
            )
        return run

    def _repo(self, repo_id: str) -> Path:
        try:
            return self.repos[repo_id]
        except KeyError:
            raise Hold("GIT_REPO_UNKNOWN", "Repository is not registered") from None

    def _descriptor(self, scope: Scope, snapshot: Ref) -> dict[str, Any]:
        value = strict_json_loads(self.artifacts.read(scope, snapshot))
        if (
            not isinstance(value, dict)
            or value.get("format") != "amplai.git-base.v1"
            or set(value) - {"format", "repo", "commit", "tree", "patch"}
            or not {"format", "repo", "commit", "tree"} <= set(value)
            or not _SHA.fullmatch(str(value["commit"]))
            or not _SHA.fullmatch(str(value["tree"]))
        ):
            raise Hold("WORKSPACE_FORMAT", "Unknown git workspace descriptor")
        repo = self._repo(value["repo"])
        tree = self._git(repo, "rev-parse", value["commit"] + "^{tree}").stdout.decode().strip()
        if tree != value["tree"]:
            raise Hold("GIT_BASE_DRIFT", "Base commit does not have the recorded tree")
        return value

    # -- WorkspaceManager surface ------------------------------------------------------------
    def base_snapshot(self, scope: Scope, repo_id: str, revision: str = "HEAD") -> Ref:
        repo = self._repo(repo_id)
        commit = self._git(repo, "rev-parse", "--verify", revision + "^{commit}")
        sha = commit.stdout.decode().strip()
        tree = self._git(repo, "rev-parse", sha + "^{tree}").stdout.decode().strip()
        value = {"format": "amplai.git-base.v1", "repo": repo_id, "commit": sha, "tree": tree}
        return self.artifacts.admit(scope, canonical(value), BASE_MEDIA)

    def materialize(self, scope: Scope, run_id: str, snapshot: Ref) -> Path:
        from ..runtime.contracts.identity import ID

        if not ID.fullmatch(run_id):
            raise Hold("WORKSPACE_RUN_ID", "Invalid run identifier")
        value = self._descriptor(scope, snapshot)
        target = self.root / run_id
        if target.exists():
            raise Hold("WORKSPACE_EXISTS", "Existing mutable workspace is not automatically reused")
        stage = self.root / new_id("workspace-stage")
        stage.mkdir(mode=0o700)
        try:
            self._extract(self._repo(value["repo"]), value["commit"], stage)
            # A fresh local repository with the base as its only commit: repo tests may read
            # committed files through git (found by the first real run). No remote, no history.
            self._commit(stage, "amplai base " + value["commit"])
            if value.get("patch"):
                # a repair copy: the previous attempt stays visible as uncommitted changes
                self._apply(stage, self.artifacts.read(scope, value["patch"]))
            self._open(stage)
            os.rename(stage, target)
            base = {k: v for k, v in value.items() if k != "patch"}
            self._bases[target] = self.artifacts.admit(scope, canonical(base), BASE_MEDIA)
            return target
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise

    def _extract(self, repo: Path, commit: str, target: Path) -> None:
        archive = self._git(repo, "archive", "--format=tar", commit).stdout
        if len(archive) > self.max_tree_bytes:
            raise Hold("WORKSPACE_QUOTA", "Base tree exceeds the workspace byte budget")
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            members = _safe_members(tar)
            if hasattr(tarfile, "data_filter"):
                # PEP 706 data filter as well where available (Python >= 3.11.4).
                tar.extractall(target, members=members, filter="data")
            else:
                tar.extractall(target, members=members)

    @staticmethod
    def _open(target: Path) -> None:
        """The container runs as an unprivileged uid; the copy (and its .git) must be writable."""
        target.chmod(0o777)
        for current, dirs, names in os.walk(target):
            for name in dirs:
                path = Path(current) / name
                if not path.is_symlink():
                    path.chmod(0o777)
            for name in names:
                path = Path(current) / name
                if not path.is_symlink():
                    path.chmod(path.stat().st_mode | 0o666)

    @staticmethod
    def _commit(target: Path, message: str) -> None:
        env = {
            **_git_env(),
            "GIT_AUTHOR_NAME": "AMPLAI",
            "GIT_AUTHOR_EMAIL": "amplai@localhost",
            "GIT_COMMITTER_NAME": "AMPLAI",
            "GIT_COMMITTER_EMAIL": "amplai@localhost",
        }
        steps = [["init", "-q", "-b", "amplai"]] if not (target / ".git").exists() else []
        steps += [["add", "-A"], ["commit", "-q", "--no-verify", "--allow-empty", "-m", message]]
        for args in steps:
            run = subprocess.run(
                ["git", "-c", "core.hooksPath=/dev/null", *args],
                cwd=target,
                env=env,
                capture_output=True,
                timeout=300,
                check=False,
            )
            if run.returncode != 0:
                raise Hold(
                    "GIT_COPY",
                    "Could not make the workspace a git checkout",
                    details={"args": args[:1], "stderr": run.stderr.decode()[-400:]},
                )

    @staticmethod
    def _apply(target: Path, patch: bytes) -> None:
        if not patch:
            return
        run = subprocess.run(
            ["git", "apply", "--binary", "--whitespace=nowarn", "-"],
            input=patch,
            cwd=target,
            capture_output=True,
            env=_git_env(),
            timeout=300,
            check=False,
        )
        if run.returncode != 0:
            raise Hold(
                "PATCH_APPLY",
                "Patch does not apply to the base commit",
                details={"stderr": run.stderr.decode()[-400:]},
            )

    def diff(self, scope: Scope, workspace: Path, snapshot: Ref) -> bytes:
        """The binary patch from the base commit to ``workspace`` (host plumbing only)."""
        value = self._descriptor(scope, snapshot)
        repo = self._repo(value["repo"])
        root = Path(workspace).absolute()
        if root.resolve() != root or root.parent != self.root:
            raise Hold("COLLECT_SCOPE", "Output root is not a managed isolated workspace")
        with tempfile.TemporaryDirectory(dir=self.root) as tmp:
            index = Path(tmp) / "index"
            self._git(repo, "read-tree", value["commit"], index=index)
            self._git(repo, "add", "-A", "--", ".", work_tree=root, index=index)
            patch = self._git(
                repo,
                "diff",
                "--cached",
                "--binary",
                "--no-color",
                "--no-ext-diff",
                "--no-renames",
                value["commit"],
                work_tree=root,
                index=index,
            ).stdout
        if len(patch) > self.max_patch_bytes:
            raise Hold("PATCH_QUOTA", "Change exceeds the patch byte budget")
        return patch

    def collect(
        self,
        scope: Scope,
        workspace: str | Path,
        bindings: dict[str, str],
        node: dict[str, Any],
        *,
        process_stopped: bool,
        base_snapshot: Ref | None = None,
    ) -> dict[str, Ref]:
        if process_stopped is not True:
            raise Hold("COLLECT_RUNNING", "Outputs cannot be trusted before process stop")
        ports = {p["name"]: p for p in node["produces"]}
        if (
            set(bindings) - set(ports)
            or any(p["required"] and n not in bindings for n, p in ports.items())
            or any(v != PATCH_BINDING for v in bindings.values())
            or any(ports[n]["media_type"] != CHANGE_MEDIA for n in bindings)
        ):
            raise Hold("OUTPUT_BINDINGS", "A git workspace produces only its host-computed change")
        snapshot = base_snapshot or self._base_of(Path(workspace))
        base = self._descriptor(scope, snapshot)
        patch = self.diff(scope, Path(workspace), snapshot)
        # The patch is its own artifact so the CAS secret scan sees its raw bytes.
        change = {
            "format": "amplai.change.v1",
            "base": {k: base[k] for k in ("repo", "commit", "tree")},
            "patch": self.artifacts.admit(scope, patch, PATCH_MEDIA),
            "patch_bytes": len(patch),
        }
        ref = self.artifacts.admit(scope, canonical(change), CHANGE_MEDIA)
        return {port: ref for port in bindings}

    def read_change(self, scope: Scope, raw: bytes) -> tuple[Ref, bytes]:
        """(base descriptor ref, patch bytes) of a collected change, re-checked against git."""
        value = strict_json_loads(raw)
        if (
            not isinstance(value, dict)
            or set(value) != {"format", "base", "patch", "patch_bytes"}
            or value["format"] != "amplai.change.v1"
            or not isinstance(value["base"], dict)
            or set(value["base"]) != {"repo", "commit", "tree"}
        ):
            raise Hold("CHANGE_FORMAT", "Unknown change artifact")
        patch = self.artifacts.read(scope, value["patch"])
        if len(patch) != value["patch_bytes"]:
            raise Hold("CHANGE_FORMAT", "Change patch size differs from its record")
        base = {"format": "amplai.git-base.v1", **value["base"]}
        ref = self.artifacts.admit(scope, canonical(base), BASE_MEDIA)
        self._descriptor(scope, ref)  # repo registered, commit has the recorded tree
        return ref, patch

    def materialize_change(self, scope: Scope, run_id: str, raw: bytes) -> tuple[Path, bytes]:
        """A fresh copy of the change's base with its patch applied (verifier/publish input)."""
        base, patch = self.read_change(scope, raw)
        target = self.materialize(scope, run_id, base)
        try:
            self._apply(target, patch)
            self._commit(target, "amplai change")
            self._open(target)
        except BaseException:
            self.discard(target)
            raise
        return target, patch

    def _base_of(self, workspace: Path) -> Ref:
        base = self._bases.get(Path(workspace).absolute())
        if base is None:
            raise Hold("WORKSPACE_BASE_UNKNOWN", "Workspace was not materialized by this manager")
        return base

    def discard(self, workspace: Path) -> None:
        """Remove a finished run's copy (outputs are already in the CAS)."""
        root = Path(workspace).absolute()
        if root.parent != self.root or root.resolve() != root:
            raise Hold("WORKSPACE_SCOPE", "Not a managed isolated workspace")
        self._bases.pop(root, None)
        shutil.rmtree(root, ignore_errors=True)

    def snapshot(self, scope: Scope, directory: Path) -> Ref:
        """Base + patch descriptor of a stopped workspace (steering checkpoints)."""
        base = self._base_of(Path(directory))
        value = self._descriptor(scope, base)
        patch = self.diff(scope, Path(directory), base)
        value = {**value, "patch": self.artifacts.admit(scope, patch, PATCH_MEDIA)}
        return self.artifacts.admit(scope, canonical(value), BASE_MEDIA)

    def assert_matches(self, scope: Scope, directory: str | Path, snapshot: Ref) -> bool:
        value = self._descriptor(scope, snapshot)
        base = {k: v for k, v in value.items() if k != "patch"}
        base_ref = self.artifacts.admit(scope, canonical(base), BASE_MEDIA)
        expected = self.artifacts.read(scope, value["patch"]) if value.get("patch") else b""
        if self.diff(scope, Path(directory), base_ref) != expected:
            raise Hold("WORKSPACE_DRIFT", "Workspace differs from its checkpoint")
        return True
