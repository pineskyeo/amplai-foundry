"""Proposal schema, evidence, target, and draft validation."""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from amplai_foundry.domain.enums import MemoryKind
from amplai_foundry.domain.lifecycle import validate_transition
from amplai_foundry.domain.models import MemoryObject
from amplai_foundry.ingestion.service import IngestionError, extract_original_content
from amplai_foundry.parsing.markdown import MarkdownParseError, parse_markdown_file
from amplai_foundry.proposals.codes import (
    PROPOSAL_DRAFT_NAMESPACE_MISMATCH,
    PROPOSAL_DRAFT_PROJECT_MISMATCH,
    PROPOSAL_EVIDENCE_PROJECT_MISMATCH,
    PROPOSAL_SOURCE_PROJECT_MISMATCH,
    PROPOSAL_TARGET_PROJECT_MISMATCH,
)
from amplai_foundry.proposals.models import OperationType, Proposal, ProposalOperation
from amplai_foundry.repositories.markdown import MarkdownMemoryRepository, MarkdownRepositoryError


@dataclass(frozen=True, slots=True)
class ProposalValidationIssue:
    code: str
    message: str


class ProposalValidator:
    def __init__(self, vault: Path = Path("vault")) -> None:
        self.vault = vault

    def validate(self, proposal: Proposal, proposal_path: Path) -> list[ProposalValidationIssue]:
        issues: list[ProposalValidationIssue] = []
        try:
            repository = MarkdownMemoryRepository(self.vault)
            existing = {memory.id: memory for memory in repository.list()}
        except MarkdownRepositoryError as error:
            return [ProposalValidationIssue("VAULT_INVALID", str(error))]

        for source_id in proposal.source_ids:
            memory = existing.get(source_id)
            if memory is None:
                issues.append(
                    ProposalValidationIssue("SOURCE_MISSING", f"Source가 없습니다: {source_id}")
                )
            elif memory.kind is not MemoryKind.SOURCE:
                issues.append(
                    ProposalValidationIssue(
                        "SOURCE_KIND", f"Proposal source_id가 Source가 아닙니다: {source_id}"
                    )
                )
            elif memory.project != proposal.project:
                issues.append(
                    ProposalValidationIssue(
                        PROPOSAL_SOURCE_PROJECT_MISMATCH,
                        (
                            f"Source {source_id} project={memory.project}와 "
                            f"Proposal project={proposal.project}가 다릅니다."
                        ),
                    )
                )

        seen_evidence: set[str] = set()
        for operation in proposal.operations:
            if operation.type is OperationType.CREATE and operation.kind == MemoryKind.SOURCE.value:
                issues.append(
                    ProposalValidationIssue(
                        "PROPOSAL_SOURCE_CREATE_FORBIDDEN",
                        "Source는 Proposal CREATE가 아니라 ingest로만 생성할 수 있습니다.",
                    )
                )
            for evidence in operation.evidence:
                seen_evidence.add(evidence.source_id)
                memory = existing.get(evidence.source_id)
                if memory is None or memory.kind is not MemoryKind.SOURCE:
                    issues.append(
                        ProposalValidationIssue(
                            "EVIDENCE_SOURCE_MISSING",
                            f"Evidence Source가 없습니다: {evidence.source_id}",
                        )
                    )
                else:
                    if memory.project != proposal.project:
                        issues.append(
                            ProposalValidationIssue(
                                PROPOSAL_EVIDENCE_PROJECT_MISMATCH,
                                (
                                    f"Evidence Source {evidence.source_id} "
                                    f"project={memory.project}와 Proposal "
                                    f"project={proposal.project}가 다릅니다."
                                ),
                            )
                        )
                    try:
                        source_path = Path(repository.path_for(evidence.source_id))
                        line_count = len(
                            extract_original_content(source_path).decode("utf-8").splitlines()
                        )
                    except (IngestionError, UnicodeError):
                        line_count = 0
                    if evidence.locator.end > line_count:
                        issues.append(
                            ProposalValidationIssue(
                                "EVIDENCE_LOCATOR",
                                (
                                    f"Evidence line range가 Source 범위를 벗어납니다: "
                                    f"{evidence.source_id} line {evidence.locator.end}"
                                ),
                            )
                        )
            if (
                operation.type is not OperationType.CREATE
                and operation.target_id
                and operation.target_id not in existing
            ):
                issues.append(
                    ProposalValidationIssue(
                        "TARGET_MISSING", f"target_id가 없습니다: {operation.target_id}"
                    )
                )
            elif operation.target_id and operation.target_id in existing:
                target = existing[operation.target_id]
                if target.project != proposal.project:
                    issues.append(
                        ProposalValidationIssue(
                            PROPOSAL_TARGET_PROJECT_MISMATCH,
                            (
                                f"target {target.id} project={target.project}와 "
                                f"Proposal project={proposal.project}가 다릅니다."
                            ),
                        )
                    )
                if target.kind is MemoryKind.SOURCE:
                    issues.append(
                        ProposalValidationIssue(
                            "PROPOSAL_SOURCE_MUTATION_FORBIDDEN",
                            "Source는 ingest 이후 Proposal로 변경할 수 없습니다.",
                        )
                    )
                if target.kind.value != operation.kind:
                    issues.append(
                        ProposalValidationIssue(
                            "TARGET_KIND",
                            (
                                f"target kind={target.kind.value}와 "
                                f"operation kind={operation.kind}가 다릅니다."
                            ),
                        )
                    )
                if proposal.status.value != "applied" and operation.type in {
                    OperationType.UPDATE,
                    OperationType.LINK,
                    OperationType.SUPERSEDE,
                }:
                    if operation.expected_revision != target.revision:
                        issues.append(
                            ProposalValidationIssue(
                                "PROPOSAL_STALE_REVISION",
                                (
                                    f"target revision={target.revision}, "
                                    f"expected_revision={operation.expected_revision}입니다."
                                ),
                            )
                        )
                    target_path = Path(repository.path_for(target.id))
                    current_hash = hashlib.sha256(target_path.read_bytes()).hexdigest()
                    if operation.expected_target_sha256 != current_hash:
                        issues.append(
                            ProposalValidationIssue(
                                "PROPOSAL_STALE_TARGET_HASH",
                                "target Markdown SHA-256가 Proposal 생성 시점과 다릅니다.",
                            )
                        )
            if operation.type is OperationType.CREATE and operation.draft_path:
                draft, draft_issues = self._load_draft(
                    operation.draft_path, proposal, proposal_path
                )
                issues.extend(draft_issues)
                if draft is not None and draft.id in existing:
                    if proposal.status.value == "applied":
                        if existing[draft.id] != draft:
                            issues.append(
                                ProposalValidationIssue(
                                    "CREATE_APPLIED_DRIFT",
                                    (
                                        "적용된 CREATE draft와 현재 Vault 대상이 다릅니다: "
                                        f"{draft.id}"
                                    ),
                                )
                            )
                    else:
                        issues.append(
                            ProposalValidationIssue(
                                "CREATE_ID_EXISTS", f"CREATE draft ID가 이미 존재합니다: {draft.id}"
                            )
                        )
                if draft is not None:
                    if draft.kind is MemoryKind.SOURCE:
                        issues.append(
                            ProposalValidationIssue(
                                "PROPOSAL_SOURCE_CREATE_FORBIDDEN",
                                "Source는 Proposal CREATE가 아니라 ingest로만 생성할 수 있습니다.",
                            )
                        )
                    issues.extend(self._validate_draft_evidence(draft, operation))
            elif operation.draft_path:
                draft, draft_issues = self._load_draft(
                    operation.draft_path, proposal, proposal_path
                )
                issues.extend(draft_issues)
                if draft is not None and operation.target_id and draft.id != operation.target_id:
                    issues.append(
                        ProposalValidationIssue(
                            "DRAFT_TARGET_MISMATCH",
                            f"draft ID {draft.id}와 target_id {operation.target_id}가 다릅니다.",
                        )
                    )
                if draft is not None:
                    issues.extend(self._validate_draft_evidence(draft, operation))
                    if (
                        proposal.status.value != "applied"
                        and operation.target_id
                        and operation.target_id in existing
                        and operation.type
                        in {OperationType.UPDATE, OperationType.LINK, OperationType.SUPERSEDE}
                    ):
                        issues.extend(
                            ProposalValidationIssue(violation.code, violation.message)
                            for violation in validate_transition(
                                existing[operation.target_id], draft, operation.type
                            )
                        )
        if not seen_evidence.issubset(set(proposal.source_ids)):
            issues.append(
                ProposalValidationIssue(
                    "EVIDENCE_NOT_DECLARED", "모든 evidence Source는 source_ids에 선언해야 합니다."
                )
            )
        return issues

    @staticmethod
    def _validate_draft_evidence(
        draft: MemoryObject, operation: ProposalOperation
    ) -> list[ProposalValidationIssue]:
        issues: list[ProposalValidationIssue] = []
        if draft.kind.value != operation.kind:
            issues.append(
                ProposalValidationIssue(
                    "DRAFT_KIND",
                    f"draft kind={draft.kind.value}와 operation kind={operation.kind}가 다릅니다.",
                )
            )
        evidence_ids = {evidence.source_id for evidence in operation.evidence}
        missing = evidence_ids - set(draft.source_refs)
        if missing:
            issues.append(
                ProposalValidationIssue(
                    "DRAFT_EVIDENCE_REF",
                    "draft source_refs에 evidence Source가 없습니다: " + ", ".join(sorted(missing)),
                )
            )
        return issues

    @staticmethod
    def _load_draft(
        draft_path: str, proposal: Proposal, proposal_path: Path
    ) -> tuple[MemoryObject | None, list[ProposalValidationIssue]]:
        path = Path(draft_path)
        if not path.is_absolute():
            path = Path.cwd() / path
        proposal_dir = proposal_path.parent.resolve()
        try:
            resolved = path.resolve()
            resolved.relative_to(proposal_dir / "drafts")
        except (OSError, ValueError):
            return None, [
                ProposalValidationIssue(
                    "DRAFT_PATH", "draft_path는 해당 Proposal drafts directory 안에 있어야 합니다."
                )
            ]
        try:
            document = parse_markdown_file(resolved)
            memory = MemoryObject.model_validate({**document.metadata, "content": document.content})
        except (MarkdownParseError, ValidationError, OSError) as error:
            return None, [ProposalValidationIssue("DRAFT_INVALID", f"{resolved}: {error}")]
        issues: list[ProposalValidationIssue] = []
        if memory.project != proposal.project:
            issues.append(
                ProposalValidationIssue(
                    PROPOSAL_DRAFT_PROJECT_MISMATCH,
                    (
                        f"draft project={memory.project}와 "
                        f"Proposal project={proposal.project}가 다릅니다: {resolved}"
                    ),
                )
            )
        if memory.namespace != proposal.namespace:
            issues.append(
                ProposalValidationIssue(
                    PROPOSAL_DRAFT_NAMESPACE_MISMATCH,
                    (
                        f"draft namespace={memory.namespace}와 "
                        f"Proposal namespace={proposal.namespace}가 다릅니다: {resolved}"
                    ),
                )
            )
        return memory, issues
