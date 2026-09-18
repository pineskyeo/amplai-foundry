"""Portable content-addressed workspace snapshots; not an OS security boundary.

Generated code may execute only via a qualified SandboxDriver. This manager
materializes ordinary files, rejects links/devices/credential files, pins source
bytes, and collects actual outputs after a confirmed process boundary.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path, PurePosixPath

from ..runtime.contracts.identity import canonical, new_id
from ..runtime.contracts.registry import strict_json_loads
from ..runtime.errors import Hold, RuntimeFault
from .local import DataSandbox


class WorkspaceManager:
    def __init__(self, root: Path, artifacts, *, max_bytes=64 * 1024 * 1024, max_files=4096):
        self.root = Path(root).absolute()
        if self.root.resolve() != self.root:
            raise Hold("WORKSPACE_ROOT", "Workspace storage cannot traverse links")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.artifacts, self.max_bytes, self.max_files = artifacts, max_bytes, max_files

    @staticmethod
    def path(name: str) -> str:
        if not isinstance(name, str):
            raise RuntimeFault("WORKSPACE_PATH", "Path must be text")
        parts = PurePosixPath(name).parts
        if (
            not name
            or name.startswith("/")
            or ".." in parts
            or "\\" in name
            or "\x00" in name
            or not parts
        ):
            raise Hold("WORKSPACE_PATH", "Only normalized relative paths are allowed")
        if PurePosixPath(name).as_posix() != name:
            raise Hold("WORKSPACE_PATH", "Ambiguous path syntax is not accepted")
        if any(x.startswith(".") for x in parts) or parts[-1].lower().endswith(
            (".pem", ".key", ".p12", ".pfx")
        ):
            raise Hold(
                "WORKSPACE_SECRET_PATH",
                "Hidden control and credential paths are not portable workspace data",
            )
        return name

    def snapshot(self, scope, directory: Path) -> dict:
        root = Path(directory).absolute()
        if root.resolve() != root or not root.is_dir():
            raise Hold("WORKSPACE_SOURCE", "Expected a real directory")
        files, size = {}, 0
        # No followlinks; each opened file is separately checked against link races.
        for current, dirs, names in os.walk(root, followlinks=False):
            for name in sorted(dirs):
                child = Path(current) / name
                if child.is_symlink():
                    raise Hold("WORKSPACE_LINK", "Symlink directory cannot be snapshotted")
            for name in sorted(names):
                child = Path(current) / name
                relative = self.path(child.relative_to(root).as_posix())
                fd = os.open(child, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as source:
                    before = os.fstat(source.fileno())
                    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                        raise Hold(
                            "WORKSPACE_SPECIAL",
                            "Only ordinary, non-hardlinked files may enter a snapshot",
                        )
                    data = source.read(self.max_bytes + 1)
                    after = os.fstat(source.fileno())
                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                        raise Hold("WORKSPACE_CHANGED", "Source changed while being snapshotted")
                size += len(data)
                if size > self.max_bytes or len(files) >= self.max_files:
                    raise Hold("WORKSPACE_QUOTA", "Snapshot exceeds file or byte budget")
                files[relative] = {
                    "artifact": self.artifacts.admit(scope, data, "application/octet-stream"),
                    "executable": bool(before.st_mode & 0o111),
                }
        value = {"format": "amplai.workspace.v1", "files": files, "total_bytes": size}
        return self.artifacts.admit(
            scope, canonical(value), "application/vnd.amplai.workspace+json"
        )

    def empty_snapshot(self, scope):
        return self.artifacts.admit(
            scope,
            canonical({"format": "amplai.workspace.v1", "files": {}, "total_bytes": 0}),
            "application/vnd.amplai.workspace+json",
        )

    def _contents(self, scope, snapshot):
        value = strict_json_loads(self.artifacts.read(scope, snapshot))
        if (
            set(value) != {"format", "files", "total_bytes"}
            or value["format"] != "amplai.workspace.v1"
            or not isinstance(value["files"], dict)
        ):
            raise Hold("WORKSPACE_FORMAT", "Unknown workspace snapshot format")
        if len(value["files"]) > self.max_files:
            raise Hold("WORKSPACE_QUOTA", "Too many files")
        content = {}
        total = 0
        for name, entry in value["files"].items():
            self.path(name)
            if set(entry) != {"artifact", "executable"} or type(entry["executable"]) is not bool:
                raise Hold("WORKSPACE_FORMAT", "Malformed snapshot entry")
            data = self.artifacts.read(scope, entry["artifact"])
            total += len(data)
            if total > self.max_bytes:
                raise Hold("WORKSPACE_QUOTA", "Snapshot exceeds byte budget")
            content[name] = (data, entry["executable"])
        if total != value["total_bytes"]:
            raise Hold("WORKSPACE_SIZE", "Snapshot total differs from bytes")
        return content

    def materialize(self, scope, run_id, snapshot):
        from ..runtime.contracts.identity import ID

        if not ID.fullmatch(run_id):
            raise Hold("WORKSPACE_RUN_ID", "Invalid run identifier")
        content = self._contents(scope, snapshot)
        target = self.root / run_id
        if target.exists():
            raise Hold("WORKSPACE_EXISTS", "Existing mutable workspace is not automatically reused")
        temporary = self.root / new_id("workspace-stage")
        temporary.mkdir(mode=0o700)
        try:
            box = DataSandbox(temporary, max_bytes=self.max_bytes, protected=())
            for name, (data, executable) in content.items():
                box.write(name, data)
                if executable:
                    (temporary / name).chmod(0o700)
            os.rename(temporary, target)
            return target
        except BaseException:
            import shutil

            shutil.rmtree(temporary)
            raise

    def collect(self, scope, workspace, bindings, node, *, process_stopped):
        if process_stopped is not True:
            raise Hold("COLLECT_RUNNING", "Outputs cannot be trusted before process stop")
        root = Path(workspace).absolute()
        if root.parent != self.root or root.resolve() != root:
            raise Hold("COLLECT_SCOPE", "Output root is not a managed isolated workspace")
        ports = {p["name"]: p for p in node["produces"]}
        if set(bindings) - set(ports) or any(
            p["required"] and n not in bindings for n, p in ports.items()
        ):
            raise Hold(
                "OUTPUT_BINDINGS", "Output file mapping must cover required graph ports only"
            )
        box = DataSandbox(root, max_bytes=self.max_bytes, protected=())
        result = {}
        for port, name in bindings.items():
            self.path(name)
            data = box.read(name)
            result[port] = self.artifacts.admit(scope, data, ports[port]["media_type"])
        return result

    def assert_matches(self, scope, directory, snapshot):
        """Compare actual bytes/modes to a frozen snapshot, without re-admission IDs."""
        root = Path(directory).absolute()
        if root.resolve() != root or root.parent != self.root:
            raise Hold("WORKSPACE_SCOPE", "Cannot resume outside the managed workspace root")
        expected = self._contents(scope, snapshot)
        seen = set()
        box = DataSandbox(root, max_bytes=self.max_bytes, protected=())
        for current, dirs, names in os.walk(root, followlinks=False):
            for name in dirs:
                if (Path(current) / name).is_symlink():
                    raise Hold("WORKSPACE_LINK", "Resume tree contains a link")
            for name in names:
                path = Path(current) / name
                rel = self.path(path.relative_to(root).as_posix())
                if rel not in expected:
                    raise Hold("WORKSPACE_DRIFT", "New file appeared after checkpoint")
                content, executable = expected[rel]
                if box.read(rel) != content or bool(path.stat().st_mode & 0o111) != executable:
                    raise Hold(
                        "WORKSPACE_DRIFT", "Workspace differs from checkpoint bytes or modes"
                    )
                seen.add(rel)
        if seen != set(expected):
            raise Hold("WORKSPACE_DRIFT", "Checkpoint files are missing")
        return True
