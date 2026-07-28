"""Two-phase safe application of approved Proposal drafts."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from amplai_foundry.domain.enums import MemoryKind
from amplai_foundry.domain.identity import MemoryRef
from amplai_foundry.domain.lifecycle import validate_transition
from amplai_foundry.domain.models import MemoryObject
from amplai_foundry.lint.engine import KnowledgeLinter
from amplai_foundry.parsing.markdown import MarkdownParseError, parse_markdown_file
from amplai_foundry.proposals.codes import (
    PROPOSAL_DRAFT_NAMESPACE_MISMATCH,
    PROPOSAL_DRAFT_PROJECT_MISMATCH,
    PROPOSAL_EVIDENCE_PROJECT_MISMATCH,
    PROPOSAL_SOURCE_PROJECT_MISMATCH,
    PROPOSAL_TARGET_PROJECT_MISMATCH,
)
from amplai_foundry.proposals.diff import destination_for_create, proposal_diff
from amplai_foundry.proposals.models import OperationType, Proposal, ProposalStatus
from amplai_foundry.proposals.paths import ProposalPathError, resolve_draft_path
from amplai_foundry.proposals.repository import ProposalRepository
from amplai_foundry.proposals.validation import ProposalValidator
from amplai_foundry.repositories.markdown import MarkdownMemoryRepository


class ProposalApplyError(RuntimeError):
    """An approved Proposal cannot be applied without violating safety gates."""


@dataclass(frozen=True, slots=True)
class ApplyResult:
    proposal_id: str
    touched_paths: tuple[str, ...]
    diff: str


def approve_proposal(
    proposal: Proposal,
    *,
    approved_by: str,
    now: datetime | None = None,
) -> Proposal:
    if proposal.status not in {ProposalStatus.DRAFT, ProposalStatus.REVIEWED}:
        raise ProposalApplyError(f"status={proposal.status.value} Proposal은 승인할 수 없습니다.")
    timestamp = now or datetime.now(ZoneInfo("Asia/Seoul"))
    return Proposal.model_validate(
        {
            **proposal.model_dump(),
            "status": ProposalStatus.APPROVED,
            "approved_at": timestamp,
            "approved_by": approved_by,
        }
    )


class ProposalApplyService:
    def __init__(self, vault: Path, repository: ProposalRepository) -> None:
        self.vault = vault.resolve()
        self.repository = repository

    def apply(self, proposal: Proposal, proposal_path: Path) -> ApplyResult:
        if proposal.status is not ProposalStatus.APPROVED:
            raise ProposalApplyError("approved Proposal만 apply할 수 있습니다.")
        if any(operation.type is OperationType.CONFLICT for operation in proposal.operations):
            raise ProposalApplyError(
                "CONFLICT operation이 있는 Proposal은 자동 apply하지 않습니다."
            )
        self._reject_source_operations(proposal, proposal_path)
        unsupported = {
            operation.type
            for operation in proposal.operations
            if operation.type in {OperationType.MERGE, OperationType.SPLIT}
        }
        if unsupported:
            names = ", ".join(sorted(item.value for item in unsupported))
            raise ProposalApplyError(f"minimal safe apply가 지원하지 않는 operation입니다: {names}")
        lock_path = self.repository.root.parent / "apply.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            lock_descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise ProposalApplyError("다른 Proposal apply가 진행 중입니다.") from None

        temporary_root: Path | None = None
        touched: list[str] = []
        swapped = False
        try:
            self._reject_source_operations(proposal, proposal_path)
            self._enforce_project_boundaries(proposal, proposal_path)
            self._enforce_preconditions_and_transitions(proposal, proposal_path)
            validation_issues = ProposalValidator(self.vault).validate(proposal, proposal_path)
            if validation_issues:
                raise ProposalApplyError(
                    "Proposal 검증 실패: "
                    + "; ".join(f"{issue.code} {issue.message}" for issue in validation_issues)
                )
            before_report = KnowledgeLinter().lint(self.vault)
            if before_report.error_count:
                raise ProposalApplyError(
                    f"변경 전 Vault lint ERROR가 {before_report.error_count}개입니다."
                )
            rendered_diff = proposal_diff(proposal, proposal_path, self.vault)
            temporary_root = Path(tempfile.mkdtemp(prefix="amplai-apply-", dir=self.vault.parent))
            staging_vault = temporary_root / "vault"
            backup_vault = temporary_root / "backup"
            shutil.copytree(self.vault, staging_vault)
            current_repository = MarkdownMemoryRepository(staging_vault)
            for operation in proposal.operations:
                if not operation.draft_path:
                    continue
                draft_path = resolve_draft_path(operation.draft_path, proposal_path)
                draft_text = draft_path.read_text(encoding="utf-8")
                if operation.type is OperationType.CREATE:
                    document = parse_markdown_file(draft_path)
                    draft = MemoryObject.model_validate(
                        {**document.metadata, "content": document.content}
                    )
                    destination = destination_for_create(
                        staging_vault,
                        draft,
                        project=proposal.project,
                        namespace=proposal.namespace,
                    )
                else:
                    destination = Path(
                        current_repository.path_for(
                            operation.target_id or "",
                            namespace=proposal.namespace,
                        )
                    )
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(draft_text, encoding="utf-8")
                touched.append(str(destination.relative_to(staging_vault)))
            after_report = KnowledgeLinter().lint(staging_vault)
            if after_report.error_count:
                raise ProposalApplyError(
                    f"임시 결과 Vault lint ERROR가 {after_report.error_count}개입니다."
                )

            os.replace(self.vault, backup_vault)
            try:
                os.replace(staging_vault, self.vault)
                swapped = True
                timestamp = datetime.now(ZoneInfo("Asia/Seoul"))
                applied = Proposal.model_validate(
                    {
                        **proposal.model_dump(),
                        "status": ProposalStatus.APPLIED,
                        "applied_at": timestamp,
                        "applied_by": proposal.approved_by,
                    }
                )
                self.repository.save(applied)
            except Exception:
                if self.vault.exists():
                    shutil.rmtree(self.vault)
                os.replace(backup_vault, self.vault)
                swapped = False
                raise
            return ApplyResult(proposal.proposal_id, tuple(sorted(touched)), rendered_diff)
        except ProposalApplyError:
            raise
        except Exception as error:
            raise ProposalApplyError(f"apply를 원자적으로 완료하지 못했습니다: {error}") from error
        finally:
            os.close(lock_descriptor)
            lock_path.unlink(missing_ok=True)
            if temporary_root is not None and swapped and backup_vault.exists():
                shutil.rmtree(backup_vault)
            if temporary_root is not None:
                shutil.rmtree(temporary_root, ignore_errors=True)

    def _reject_source_operations(self, proposal: Proposal, proposal_path: Path) -> None:
        repository = MarkdownMemoryRepository(self.vault)
        for operation in proposal.operations:
            if operation.type is not OperationType.CREATE:
                target = repository.get(
                    operation.target_id or "",
                    namespace=proposal.namespace,
                )
                if operation.kind == MemoryKind.SOURCE.value or (
                    target is not None and target.kind is MemoryKind.SOURCE
                ):
                    raise ProposalApplyError(
                        "PROPOSAL_SOURCE_MUTATION_FORBIDDEN "
                        "Source는 ingest 이후 Proposal로 변경할 수 없습니다."
                    )
                continue
            draft_is_source = False
            if operation.draft_path:
                with suppress(MarkdownParseError, OSError, ProposalPathError):
                    draft_path = resolve_draft_path(operation.draft_path, proposal_path)
                    draft_is_source = (
                        parse_markdown_file(draft_path).metadata.get("kind")
                        == MemoryKind.SOURCE.value
                    )
            if operation.kind == MemoryKind.SOURCE.value or draft_is_source:
                raise ProposalApplyError(
                    "PROPOSAL_SOURCE_CREATE_FORBIDDEN Source는 ingest로만 생성할 수 있습니다."
                )

    def _enforce_project_boundaries(
        self,
        proposal: Proposal,
        proposal_path: Path,
    ) -> None:
        repository = MarkdownMemoryRepository(self.vault)
        existing = {memory.ref: memory for memory in repository.list()}
        for source_id in proposal.source_ids:
            source = ProposalValidator._resolve_for_boundary_check(
                existing,
                MemoryRef(namespace=proposal.namespace, local_id=source_id),
            )
            if source is not None and source.project != proposal.project:
                raise ProposalApplyError(
                    f"{PROPOSAL_SOURCE_PROJECT_MISMATCH} "
                    f"Source {source_id} project={source.project}와 "
                    f"Proposal project={proposal.project}가 다릅니다."
                )
        for operation in proposal.operations:
            for evidence in operation.evidence:
                source = ProposalValidator._resolve_for_boundary_check(
                    existing,
                    MemoryRef(
                        namespace=proposal.namespace,
                        local_id=evidence.source_id,
                    ),
                )
                if source is not None and source.project != proposal.project:
                    raise ProposalApplyError(
                        f"{PROPOSAL_EVIDENCE_PROJECT_MISMATCH} "
                        f"Evidence Source {evidence.source_id} project={source.project}와 "
                        f"Proposal project={proposal.project}가 다릅니다."
                    )
            if operation.target_id:
                target = ProposalValidator._resolve_for_boundary_check(
                    existing,
                    MemoryRef(
                        namespace=proposal.namespace,
                        local_id=operation.target_id,
                    ),
                )
                if target is not None and target.project != proposal.project:
                    raise ProposalApplyError(
                        f"{PROPOSAL_TARGET_PROJECT_MISMATCH} "
                        f"target {target.id} project={target.project}와 "
                        f"Proposal project={proposal.project}가 다릅니다."
                    )
            if not operation.draft_path:
                continue
            draft = self._load_apply_draft(operation.draft_path, proposal_path)
            if draft.project != proposal.project:
                raise ProposalApplyError(
                    f"{PROPOSAL_DRAFT_PROJECT_MISMATCH} "
                    f"draft project={draft.project}와 "
                    f"Proposal project={proposal.project}가 다릅니다."
                )
            if draft.namespace != proposal.namespace:
                raise ProposalApplyError(
                    f"{PROPOSAL_DRAFT_NAMESPACE_MISMATCH} "
                    f"draft namespace={draft.namespace}와 "
                    f"Proposal namespace={proposal.namespace}가 다릅니다."
                )

    @staticmethod
    def _load_apply_draft(draft_path: str, proposal_path: Path) -> MemoryObject:
        try:
            resolved = resolve_draft_path(draft_path, proposal_path)
        except (OSError, ProposalPathError) as error:
            raise ProposalApplyError(
                "DRAFT_PATH draft_path는 해당 Proposal drafts directory 안에 있어야 합니다."
            ) from error
        try:
            document = parse_markdown_file(resolved)
            return MemoryObject.model_validate({**document.metadata, "content": document.content})
        except (MarkdownParseError, OSError, ValidationError) as error:
            raise ProposalApplyError(f"DRAFT_INVALID {resolved}: {error}") from error

    def _enforce_preconditions_and_transitions(
        self,
        proposal: Proposal,
        proposal_path: Path,
    ) -> None:
        repository = MarkdownMemoryRepository(self.vault)
        for operation in proposal.operations:
            if operation.type not in {
                OperationType.UPDATE,
                OperationType.LINK,
                OperationType.SUPERSEDE,
            }:
                continue
            target = repository.get(
                operation.target_id or "",
                namespace=proposal.namespace,
            )
            if target is None:
                raise ProposalApplyError(
                    f"TARGET_MISSING target_id가 없습니다: {operation.target_id}"
                )
            if target.kind is MemoryKind.SOURCE:
                raise ProposalApplyError(
                    "PROPOSAL_SOURCE_MUTATION_FORBIDDEN "
                    "Source는 ingest 이후 Proposal로 변경할 수 없습니다."
                )
            if target.revision != operation.expected_revision:
                raise ProposalApplyError(
                    "PROPOSAL_STALE_REVISION "
                    f"target revision={target.revision}, "
                    f"expected_revision={operation.expected_revision}입니다."
                )
            target_path = Path(repository.path_for(target.ref))
            current_hash = hashlib.sha256(target_path.read_bytes()).hexdigest()
            if current_hash != operation.expected_target_sha256:
                raise ProposalApplyError(
                    "PROPOSAL_STALE_TARGET_HASH "
                    "target Markdown SHA-256가 Proposal 생성 시점과 다릅니다."
                )
            if not operation.draft_path:
                continue
            draft_path = resolve_draft_path(operation.draft_path, proposal_path)
            document = parse_markdown_file(draft_path)
            draft = MemoryObject.model_validate({**document.metadata, "content": document.content})
            violations = validate_transition(target, draft, operation.type)
            if violations:
                raise ProposalApplyError(
                    "; ".join(f"{violation.code} {violation.message}" for violation in violations)
                )
