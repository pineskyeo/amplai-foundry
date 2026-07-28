"""Atomic persistence for roadmap change proposal lifecycle state."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

import yaml
from pydantic import ValidationError

from amplai_foundry.roadmaps.models import RoadmapChangeProposal


class RoadmapProposalRepositoryError(RuntimeError):
    pass


class RoadmapProposalRepository:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path_for(self, proposal_id: str) -> Path:
        if re.fullmatch(r"RMAP-[0-9]{8}-[A-F0-9]{8}", proposal_id) is None:
            raise RoadmapProposalRepositoryError("Roadmap Proposal ID 형식이 올바르지 않습니다.")
        return self.root / f"{proposal_id}.yaml"

    def get(self, proposal_id: str) -> RoadmapChangeProposal | None:
        path = self.path_for(proposal_id)
        if not path.exists():
            return None
        return self.load(path)

    def list(self) -> list[RoadmapChangeProposal]:
        if not self.root.exists():
            return []
        return [self.load(path) for path in sorted(self.root.glob("RMAP-*.yaml"))]

    def save(self, proposal: RoadmapChangeProposal) -> Path:
        path = self.path_for(proposal.proposal_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = yaml.safe_dump(
            proposal.model_dump(mode="json", exclude_none=True),
            allow_unicode=True,
            sort_keys=False,
        )
        descriptor = -1
        temporary: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{path.name}.",
                suffix=".tmp",
                dir=path.parent,
            )
            temporary = Path(temporary_name)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except OSError as error:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise RoadmapProposalRepositoryError(
                f"roadmap proposal을 저장할 수 없습니다: {error}"
            ) from error
        return path

    @staticmethod
    def load(path: Path) -> RoadmapChangeProposal:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            return RoadmapChangeProposal.model_validate(data)
        except (OSError, yaml.YAMLError, ValidationError) as error:
            raise RoadmapProposalRepositoryError(f"{path}: {error}") from error
