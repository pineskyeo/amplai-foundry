"""Verify a design-mode change: design documents only, complete, and grounded (Work 019 F).

mode=design produces design artifacts and review evidence and never implementation
(design-reference/design/03_INVARIANT_REGISTRY.md:59, 05_GOAL_RESOLVER_CONTRACT.md:48). The
runner reads the host-computed patch and the base+patch copy; it never executes anything from
the change. A change passes only if

- every changed path is under ``specs/design/<one directory>/`` (no source, test or config);
- that directory has ``design.md`` with every required section;
- the document cites at least ``min_sources`` repository locations as ``path:line`` and every
  citation resolves on base + patch (the file exists outside the design directory and has that
  line).
"""

from __future__ import annotations

import re
from typing import Any

from ...runtime.contracts.identity import new_id
from ...runtime.errors import Hold
from ...runtime.storage.store import Scope
from ...sandbox.git_workspace import GitWorkspaceManager
from .service import VerificationObservation

DESIGN_ROOT = "specs/design/"
SECTIONS = (
    "Goal",
    "Current State",
    "Options",
    "Decision",
    "Risks",
    "Implementation Plan",
    "Sources",
)
CITE = re.compile(r"(?<![\w/.-])([A-Za-z0-9_][A-Za-z0-9_./-]*\.[A-Za-z0-9]+):(\d+)(?:-\d+)?")
DIFF_PATH = re.compile(rb"^diff --git a/(\S+) b/(\S+)$", re.M)


def changed_paths(patch: bytes) -> list[str]:
    paths: set[str] = set()
    for old, new in DIFF_PATH.findall(patch):
        paths.update({old.decode(errors="replace"), new.decode(errors="replace")})
    return sorted(paths)


class DesignDocumentCheck:
    """Deterministic acceptance runner for design goals (never runs code from the change)."""

    def __init__(
        self, workspaces: GitWorkspaceManager, scope: Scope, *, min_sources: int = 3
    ) -> None:
        self.workspaces, self.scope, self.min_sources = workspaces, scope, min_sources

    def __call__(self, raw: bytes) -> VerificationObservation:
        _base, patch = self.workspaces.read_change(self.scope, raw)
        paths = changed_paths(patch)
        details: dict[str, Any] = {"changed_paths": paths[:200]}
        if not paths:
            return VerificationObservation("fail", "The change is empty", details, 1)
        outside = [p for p in paths if not p.startswith(DESIGN_ROOT)]
        if outside:
            details["outside"] = outside[:50]
            return VerificationObservation(
                "fail", "Design mode may change only files under " + DESIGN_ROOT, details, 1
            )
        dirs = {p[len(DESIGN_ROOT) :].split("/", 1)[0] for p in paths}
        if len(dirs) != 1:
            return VerificationObservation(
                "fail", "A design goal writes exactly one design directory", details, 1
            )
        (slug,) = dirs
        try:
            workspace, _ = self.workspaces.materialize_change(self.scope, new_id("design"), raw)
        except Hold as exc:
            if exc.code == "PATCH_APPLY":
                return VerificationObservation(
                    "fail", "Patch does not apply to its base commit", details, 1
                )
            raise
        try:
            doc = workspace / DESIGN_ROOT / slug / "design.md"
            if not doc.is_file():
                return VerificationObservation(
                    "fail", f"{DESIGN_ROOT}{slug}/design.md is missing", details, 1
                )
            text = doc.read_text(errors="replace")
            headings = {
                line[3:].strip().lower() for line in text.splitlines() if line.startswith("## ")
            }
            missing = [s for s in SECTIONS if s.lower() not in headings]
            resolved: list[str] = []
            unresolved: list[str] = []
            for path, line in sorted(set(CITE.findall(text))):
                target = workspace / path
                ok = (
                    not path.startswith(DESIGN_ROOT)
                    and ".." not in path.split("/")
                    and target.is_file()
                    and 1 <= int(line) <= len(target.read_bytes().splitlines())
                )
                (resolved if ok else unresolved).append(f"{path}:{line}")
            details.update(
                design=f"{DESIGN_ROOT}{slug}/design.md",
                missing_sections=missing,
                sources=resolved[:100],
                unresolved_sources=unresolved[:50],
            )
            reasons = []
            if missing:
                reasons.append("missing sections: " + ", ".join(missing))
            if unresolved:
                reasons.append(f"{len(unresolved)} citation(s) do not resolve")
            if len(resolved) < self.min_sources:
                reasons.append(f"{len(resolved)} resolved source(s), need {self.min_sources}")
            if reasons:
                return VerificationObservation("fail", "; ".join(reasons), details, 1)
            return VerificationObservation(
                "pass", f"design.md complete with {len(resolved)} resolved sources", details, 0
            )
        finally:
            self.workspaces.discard(workspace)
