"""Filesystem storage for reviewable Proposal artifacts."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import ValidationError

from amplai_foundry.proposals.models import Proposal


class ProposalRepositoryError(RuntimeError):
    """A Proposal artifact cannot be found, parsed, or stored."""


class ProposalRepository:
    def __init__(self, root: Path = Path(".amplai/proposals")) -> None:
        self.root = root

    def path_for(self, identifier: str) -> Path:
        return self.root / identifier / "proposal.yaml"

    def get(self, identifier: str) -> Proposal | None:
        path = self.path_for(identifier)
        return self.load(path) if path.exists() else None

    @staticmethod
    def load(path: Path) -> Proposal:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            return Proposal.model_validate(data)
        except (OSError, yaml.YAMLError, ValidationError) as error:
            raise ProposalRepositoryError(f"{path}: {error}") from error

    def save(self, proposal: Proposal) -> Path:
        path = self.path_for(proposal.proposal_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".yaml.tmp")
        payload = yaml.safe_dump(
            proposal.model_dump(mode="json", exclude_none=True),
            allow_unicode=True,
            sort_keys=False,
        )
        try:
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, path)
        except OSError as error:
            temporary.unlink(missing_ok=True)
            raise ProposalRepositoryError(f"Proposal을 저장할 수 없습니다: {error}") from error
        return path
