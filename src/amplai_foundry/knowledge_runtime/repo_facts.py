"""Repo facts resolver and source provenance (design/05 §1-§2, design/12 §4, T-020/073/074).

Facts are observations: scoped, versioned, digest-bound and never instructions.
Nothing here grants authority; a repository document that says "disable verifiers"
is data with ``untrusted_as_instructions`` set, not a policy.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.runtime.storage.store import Scope

if TYPE_CHECKING:
    from amplai_foundry.knowledge_runtime.service import KnowledgeService

Ref = dict[str, Any]

_SYMBOL = re.compile(r"^\s*(?P<kind>def|class|async def)\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\b")


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class RepoFactsResolver:
    EXCLUDED = frozenset({".git", ".venv", "node_modules", "__pycache__", ".env"})
    TEST_CONFIGS = frozenset({"pyproject.toml", "package.json", "Makefile", "go.mod", "Cargo.toml"})

    def __init__(
        self,
        root: str | Path,
        *,
        max_files: int = 4096,
        max_hits: int = 256,
        max_file_bytes: int = 1024 * 1024,
    ) -> None:
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise RuntimeFault("REPO_ROOT", "Repository root must be an existing directory")
        self.max_files, self.max_hits, self.max_file_bytes = max_files, max_hits, max_file_bytes

    # -- boundary ----------------------------------------------------------------
    def _inside(self, path: Path) -> Path:
        candidate = (self.root / path) if not path.is_absolute() else path
        if candidate.is_symlink() or not candidate.resolve().is_relative_to(self.root):
            raise RuntimeFault("REPO_PATH_ESCAPE", "Repository path escapes its allowed root")
        return candidate

    def _files(self, paths: list[str] | None = None) -> list[Path]:
        roots = [self._inside(Path(p)) for p in paths] if paths else [self.root]
        for r in roots:
            if ".." in Path(str(r)).parts:
                raise RuntimeFault("REPO_PATH_ESCAPE", "Repository path escapes its allowed root")
        files: list[Path] = []
        for start in roots:
            if start.is_file():
                files.append(start)
                continue
            for directory, subdirs, names in os.walk(start):
                subdirs[:] = sorted(
                    d
                    for d in subdirs
                    if d not in self.EXCLUDED and not (Path(directory) / d).is_symlink()
                )
                for name in sorted(names):
                    path = Path(directory) / name
                    if name in self.EXCLUDED or path.is_symlink():
                        continue
                    if not path.resolve().is_relative_to(self.root):
                        raise RuntimeFault(
                            "REPO_PATH_ESCAPE", "Repository path escapes its allowed root"
                        )
                    files.append(path)
                    if len(files) > self.max_files:
                        raise Hold(
                            "REPO_FACT_LIMIT",
                            "Repository inventory needs a narrower approved scope",
                        )
        return files

    # -- inventory ---------------------------------------------------------------
    def inspect(self) -> dict[str, Any]:
        files: list[dict[str, Any]] = [
            {"path": str(p.relative_to(self.root)), "size": p.stat().st_size} for p in self._files()
        ]
        return {
            "root": str(self.root),
            "files": files,
            "test_configs": [f["path"] for f in files if Path(f["path"]).name in self.TEST_CONFIGS],
            "source_version": self.source_version(),
        }

    def read(self, relative: str) -> bytes:
        path = self._inside(Path(relative))
        if not path.is_file():
            raise RuntimeFault("NOT_FOUND", "Repository file not found")
        if path.stat().st_size > self.max_file_bytes:
            raise Hold("REPO_FILE_SIZE", "File exceeds the bounded read size")
        return path.read_bytes()

    # -- versioning --------------------------------------------------------------
    def source_version(self) -> dict[str, Any]:
        hasher = hashlib.sha256()
        for path in self._files():
            hasher.update(str(path.relative_to(self.root)).encode())
            hasher.update(b"\0")
            hasher.update(_sha256(path.read_bytes()).encode())
            hasher.update(b"\n")
        version: dict[str, Any] = {"vcs": "none", "tree_digest": "sha256:" + hasher.hexdigest()}
        if (self.root / ".git").exists():
            try:
                head = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=self.root,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=True,
                ).stdout.strip()
                status = subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=self.root,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=True,
                ).stdout
            except (OSError, subprocess.SubprocessError):
                return version
            if re.fullmatch(r"[0-9a-f]{40}", head):
                version.update({"vcs": "git", "head": head, "dirty": bool(status.strip())})
        return version

    # -- search ------------------------------------------------------------------
    def search(
        self,
        pattern: str,
        *,
        kind: str = "text",
        paths: list[str] | None = None,
        max_hits: int | None = None,
    ) -> list[dict[str, Any]]:
        if kind not in {"text", "symbol"}:
            raise RuntimeFault("SEARCH_KIND", "Search kind must be text or symbol")
        if not pattern or len(pattern) > 512:
            raise RuntimeFault("SEARCH_PATTERN", "A bounded, non-empty pattern is required")
        limit = max_hits or self.max_hits
        needle = re.compile(re.escape(pattern))
        hits: list[dict[str, Any]] = []
        for path in self._files(paths):
            if path.stat().st_size > self.max_file_bytes:
                continue
            data = path.read_bytes()
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            file_digest = _sha256(data)
            relative = str(path.relative_to(self.root))
            for number, line in enumerate(text.splitlines(), start=1):
                if kind == "text":
                    if not needle.search(line):
                        continue
                    hit: dict[str, Any] = {}
                else:
                    match = _SYMBOL.match(line)
                    if not match or match.group("name") != pattern:
                        continue
                    hit = {
                        "symbol": match.group("name"),
                        "symbol_kind": "class" if match.group("kind") == "class" else "function",
                    }
                hit.update(
                    {
                        "path": relative,
                        "line": number,
                        "excerpt_range": [number, number],
                        "excerpt": line[:512],
                        "digest": file_digest,
                        "kind": kind,
                    }
                )
                hits.append(hit)
                if len(hits) > limit:
                    raise Hold(
                        "REPO_FACT_LIMIT", "Search needs a narrower scope or a smaller bound"
                    )
        return sorted(hits, key=lambda h: (h["path"], h["line"]))

    # -- provenance --------------------------------------------------------------
    def record(self, knowledge: KnowledgeService, scope: Scope, hit: dict[str, Any]) -> Ref:
        start, end = hit["excerpt_range"]
        locator = f"{hit['path']}:{hit['line']}:{start}-{end}@{hit['digest']}"
        return knowledge.record_observation(
            scope,
            locator,
            str(hit["excerpt"]),
            kind="repo_fact",
            trust="observed",
            metadata={"source_version": self.source_version(), "search_kind": hit["kind"]},
        )


class ExternalSourcePins:
    """External material is ingested as a digest-pinned snapshot or held as unverified."""

    def __init__(self, knowledge: KnowledgeService) -> None:
        self.knowledge = knowledge

    def pin(self, scope: Scope, url: str, data: bytes, *, fetched_at: str) -> Ref:
        if not url.startswith(("http://", "https://")) or len(url) > 2048:
            raise RuntimeFault("EXTERNAL_URL", "A bounded http(s) URL is required")
        excerpt = data[:16384].decode("utf-8", errors="replace")
        return self.knowledge.record_observation(
            scope,
            url,
            excerpt,
            kind="external_snapshot",
            trust="untrusted",
            metadata={
                "content_digest": _sha256(data),
                "fetched_at": fetched_at,
                "bytes": len(data),
            },
        )

    def resolve(self, scope: Scope, ref: Ref, *, current_bytes: bytes | None) -> dict[str, Any]:
        value = self.knowledge.store.get(scope, "knowledge-observation", ref)
        if value.get("kind") != "external_snapshot":
            raise RuntimeFault("EXTERNAL_KIND", "Reference is not an external snapshot")
        if current_bytes is None:
            raise Hold(
                "EXTERNAL_UNVERIFIED",
                "Current external content is unknown; the pinned digest is not assumed current",
                details={"url": value["locator"], "pinned_digest": value["content_digest"]},
            )
        if _sha256(current_bytes) != value["content_digest"]:
            raise Hold(
                "EXTERNAL_STALE",
                "External content drifted from the pinned snapshot",
                details={"url": value["locator"], "pinned_digest": value["content_digest"]},
            )
        return {"status": "pinned", "ref": ref, "content_digest": value["content_digest"]}
