"""Portable, versioned intake-run history and provisional semantic evidence."""

from __future__ import annotations

import builtins
import os
import tempfile
from pathlib import Path

import yaml
from pydantic import ValidationError

from amplai_foundry.domain.identity import ProjectRef
from amplai_foundry.intake.models import IntakeRun, ResolutionHoldRecord
from amplai_foundry.proposals.models import ProposalStatus
from amplai_foundry.proposals.repository import ProposalRepository
from amplai_foundry.semantics.models import ComparisonRelation, SemanticAnchor


class IntakeStoreError(RuntimeError):
    pass


class IntakeRunStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path_for(self, run_id: str) -> Path:
        return self.root / "runs" / f"{run_id}.yaml"

    def get(self, run_id: str) -> IntakeRun | None:
        path = self.path_for(run_id)
        if not path.exists():
            return None
        try:
            return IntakeRun.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (OSError, yaml.YAMLError, ValidationError) as error:
            raise IntakeStoreError(f"{path}: {error}") from error

    def list(self, project: ProjectRef | None = None) -> builtins.list[IntakeRun]:
        if not self.root.exists():
            return []
        runs: builtins.list[IntakeRun] = []
        for path in sorted((self.root / "runs").glob("*.yaml")):
            try:
                run = IntakeRun.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
            except (OSError, yaml.YAMLError, ValidationError) as error:
                raise IntakeStoreError(f"{path}: {error}") from error
            if project is None or run.project == project:
                runs.append(run)
        return runs

    def save(self, run: IntakeRun) -> Path:
        path = self.path_for(run.run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = yaml.safe_dump(
            run.model_dump(mode="json", exclude_none=True),
            allow_unicode=True,
            sort_keys=False,
        )
        descriptor = -1
        temporary: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{run.run_id}.",
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
            raise IntakeStoreError(f"intake run을 저장할 수 없습니다: {error}") from error
        return path

    def provisional_anchors(
        self,
        project: ProjectRef,
        *,
        proposal_root: Path,
    ) -> builtins.list[SemanticAnchor]:
        anchors: dict[str, SemanticAnchor] = {}
        proposals = ProposalRepository(proposal_root)
        for run in self.list(project):
            if run.proposal_id is None:
                continue
            proposal = proposals.get(run.proposal_id)
            if proposal is None or proposal.status in {
                ProposalStatus.REJECTED,
                ProposalStatus.SUPERSEDED,
            }:
                continue
            comparisons = {item.candidate_id: item for item in run.comparisons}
            for candidate in run.candidates:
                comparison = comparisons.get(candidate.candidate_id)
                if comparison is None or comparison.relation is not ComparisonRelation.NEW:
                    continue
                anchor = SemanticAnchor.from_candidate(candidate, provisional=True)
                existing = anchors.get(anchor.semantic_id)
                if existing is not None:
                    anchor = existing.model_copy(
                        update={
                            "source_refs": sorted(
                                {*existing.source_refs, *anchor.source_refs},
                                key=lambda item: item.qualified,
                            )
                        }
                    )
                anchors[anchor.semantic_id] = anchor
        return sorted(anchors.values(), key=lambda item: item.semantic_id)


class ResolutionHoldStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path_for(self, hold_id: str) -> Path:
        return self.root / f"{hold_id}.yaml"

    def save(self, record: ResolutionHoldRecord) -> Path:
        path = self.path_for(record.hold_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = yaml.safe_dump(
            record.model_dump(mode="json", exclude_none=True),
            allow_unicode=True,
            sort_keys=False,
        )
        descriptor = -1
        temporary: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{record.hold_id}.",
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
            raise IntakeStoreError(f"resolution hold를 저장할 수 없습니다: {error}") from error
        return path
