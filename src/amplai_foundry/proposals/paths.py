"""Portable, contained Proposal artifact paths."""

from __future__ import annotations

from pathlib import Path


class ProposalPathError(ValueError):
    """A Proposal path escapes its own artifact directory."""


def resolve_draft_path(value: str, proposal_path: Path) -> Path:
    """Resolve new proposal-relative and legacy pack-relative draft paths."""
    path = Path(value)
    proposal_dir = proposal_path.resolve().parent
    drafts_root = (proposal_dir / "drafts").resolve()
    candidates: list[Path]
    if path.is_absolute():
        candidates = [path]
    else:
        candidates = [Path.cwd() / path, proposal_dir / path]
        parts = path.parts
        if len(parts) >= 3 and parts[-3] == proposal_dir.name and parts[-2] == "drafts":
            candidates.append(drafts_root / parts[-1])

    contained: list[Path] = []
    for candidate in candidates:
        resolved = candidate.resolve()
        try:
            resolved.relative_to(drafts_root)
        except ValueError:
            continue
        contained.append(resolved)
        if resolved.exists():
            return resolved
    if contained:
        return contained[0]
    raise ProposalPathError("draft_path는 해당 Proposal drafts directory 안에 있어야 합니다.")
