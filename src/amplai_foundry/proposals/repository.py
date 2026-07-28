"""Filesystem storage for reviewable Proposal artifacts."""

from __future__ import annotations

import os
import re
import tempfile
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
        if re.fullmatch(r"PROP-[0-9]{8}-[A-F0-9]{8}", identifier) is None:
            raise ProposalRepositoryError("Proposal ID 형식이 올바르지 않습니다.")
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
        payload = yaml.safe_dump(
            proposal.model_dump(mode="json", exclude_none=True),
            allow_unicode=True,
            sort_keys=False,
        )
        descriptor = -1
        temporary: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".proposal.",
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
            raise ProposalRepositoryError(f"Proposal을 저장할 수 없습니다: {error}") from error
        return path
