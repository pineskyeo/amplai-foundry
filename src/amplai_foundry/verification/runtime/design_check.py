"""Verify a design-mode change: design documents only, complete, and grounded (Work 019 F).

mode=design produces design artifacts and review evidence and never implementation
(design-reference/design/03_INVARIANT_REGISTRY.md:59, 05_GOAL_RESOLVER_CONTRACT.md:48). The
runner reads the host-computed patch and the base+patch copy; it never executes anything from
the change. A change passes only if

- every path git applied is under ``specs/design/<one directory>/`` (no source, test or
  config);
- that directory has ``design.md`` with every required section;
- the document cites at least ``min_sources`` repository locations as ``path:line`` and every
  citation resolves: a regular file of the base commit, named exactly, outside the design
  directory, with that line (no symlink and no case variant).
"""

from __future__ import annotations

import re
from pathlib import Path
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


class DesignDocumentCheck:
    """Deterministic acceptance runner for design goals (never runs code from the change)."""

    def __init__(
        self, workspaces: GitWorkspaceManager, scope: Scope, *, min_sources: int = 3
    ) -> None:
        self.workspaces, self.scope, self.min_sources = workspaces, scope, min_sources

    def __call__(self, raw: bytes) -> VerificationObservation:
        self.workspaces.read_change(self.scope, raw)  # format, size and base re-checked
        try:
            workspace, _ = self.workspaces.materialize_change(self.scope, new_id("design"), raw)
        except Hold as exc:
            if exc.code == "PATCH_APPLY":
                return VerificationObservation(
                    "fail", "Patch does not apply to its base commit", {}, 1
                )
            raise
        try:
            return self._check(workspace)
        finally:
            self.workspaces.discard(workspace)

    def _check(self, workspace: Path) -> VerificationObservation:
        # the paths git applied, not a parse of the patch text: a header the parser skips
        # (spaces, quoting, a traditional diff) cannot hide a source change (review F4)
        paths = self.workspaces.change_paths(workspace)
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
        doc = workspace / DESIGN_ROOT / slug / "design.md"
        if doc.is_symlink() or not doc.is_file():
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
        files = self.workspaces.base_files(workspace)
        for path, line in sorted(set(CITE.findall(text))):
            ok = _resolves(workspace, files, path, int(line))
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


def _resolves(workspace: Path, files: set[str], path: str, line: int) -> bool:
    """A citation counts only for a regular file of the base commit, named exactly, outside the
    design directory: no symlink, no path through one, no case variant of another path on a
    case-insensitive disk (review F5). Outside the design directory the copy equals the base."""
    if path.startswith(DESIGN_ROOT) or path not in files:
        return False
    return 1 <= line <= len((workspace / path).read_bytes().splitlines())
