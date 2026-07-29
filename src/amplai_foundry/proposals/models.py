"""Storage-independent proposal domain models."""

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from amplai_foundry.domain.project import ProjectId

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
ProposalId = Annotated[str, StringConstraints(pattern=r"^PROP-[0-9]{8}-[A-F0-9]{8}$")]
OperationId = Annotated[str, StringConstraints(pattern=r"^OP-[0-9]{3}$")]
MemoryId = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9]*-[A-Z0-9-]+$")]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class ProposalStatus(StrEnum):
    DRAFT = "draft"
    REVIEWED = "reviewed"
    CHANGES_REQUESTED = "changes_requested"
    APPROVED = "approved"
    APPLIED = "applied"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class OperationType(StrEnum):
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    LINK = "LINK"
    MERGE = "MERGE"
    SPLIT = "SPLIT"
    SUPERSEDE = "SUPERSEDE"
    CONFLICT = "CONFLICT"
    IGNORE = "IGNORE"


class Confidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class EvidenceLocator(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["line_range"] = "line_range"
    start: int = Field(ge=1)
    end: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_range(self) -> "EvidenceLocator":
        if self.end < self.start:
            raise ValueError("evidence locator end는 start 이상이어야 합니다.")
        return self


class ProposalEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: MemoryId
    locator: EvidenceLocator


class ProposalOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: OperationId
    type: OperationType
    target_id: MemoryId | None = None
    kind: NonEmptyString
    title: NonEmptyString
    reason: NonEmptyString
    evidence: list[ProposalEvidence] = Field(min_length=1)
    confidence: Confidence
    draft_path: NonEmptyString | None = None
    expected_revision: int | None = Field(default=None, ge=1)
    expected_target_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> "ProposalOperation":
        target_required = {
            OperationType.UPDATE,
            OperationType.LINK,
            OperationType.MERGE,
            OperationType.SPLIT,
            OperationType.SUPERSEDE,
            OperationType.CONFLICT,
        }
        if self.type in target_required and self.target_id is None:
            raise ValueError(f"{self.type.value} operation에는 target_id가 필요합니다.")
        if self.type is OperationType.CREATE and self.target_id is not None:
            raise ValueError("CREATE operation에는 target_id를 지정하지 않습니다.")
        draft_required = {
            OperationType.CREATE,
            OperationType.UPDATE,
            OperationType.LINK,
            OperationType.SUPERSEDE,
        }
        if self.type in draft_required and not self.draft_path:
            raise ValueError(f"{self.type.value} operation에는 draft_path가 필요합니다.")
        precondition_types = {
            OperationType.UPDATE,
            OperationType.LINK,
            OperationType.SUPERSEDE,
        }
        if self.type not in precondition_types and (
            self.expected_revision is not None or self.expected_target_sha256 is not None
        ):
            raise ValueError(
                f"{self.type.value} operation에는 target precondition을 지정하지 않습니다."
            )
        return self


class Proposal(BaseModel):
    """Reviewed change set derived from immutable source evidence."""

    model_config = ConfigDict(extra="forbid")

    proposal_version: Literal[1] = 1
    revision: int = Field(default=1, ge=1)
    proposal_id: ProposalId
    project: ProjectId
    namespace: NonEmptyString
    status: ProposalStatus = ProposalStatus.DRAFT
    source_ids: list[MemoryId] = Field(min_length=1)
    created_at: datetime
    created_by: NonEmptyString
    operations: list[ProposalOperation] = Field(min_length=1)
    notes: list[NonEmptyString] = Field(default_factory=list)
    approved_at: datetime | None = None
    approved_by: NonEmptyString | None = None
    applied_at: datetime | None = None
    applied_by: NonEmptyString | None = None
    git_commit_sha: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{7,40}$")] | None = None

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "Proposal":
        if self.status in {ProposalStatus.APPROVED, ProposalStatus.APPLIED} and (
            self.approved_at is None or self.approved_by is None
        ):
            raise ValueError(
                "approved/applied Proposal에는 approved_at과 approved_by가 필요합니다."
            )
        if self.status is ProposalStatus.APPLIED and (
            self.applied_at is None or self.applied_by is None
        ):
            raise ValueError("applied Proposal에는 applied_at과 applied_by가 필요합니다.")
        operation_ids = [operation.operation_id for operation in self.operations]
        if len(operation_ids) != len(set(operation_ids)):
            raise ValueError("operation_id는 Proposal 안에서 고유해야 합니다.")
        if len(self.source_ids) != len(set(self.source_ids)):
            raise ValueError("source_ids는 Proposal 안에서 고유해야 합니다.")
        if not self.namespace.endswith(f"/project/{self.project}"):
            raise ValueError("Proposal namespace와 project가 일치하지 않습니다.")
        requires_preconditions = self.status in {
            ProposalStatus.DRAFT,
            ProposalStatus.REVIEWED,
            ProposalStatus.APPROVED,
        }
        mutating_types = {
            OperationType.UPDATE,
            OperationType.LINK,
            OperationType.SUPERSEDE,
        }
        if requires_preconditions:
            for operation in self.operations:
                if operation.type in mutating_types and (
                    operation.expected_revision is None or operation.expected_target_sha256 is None
                ):
                    raise ValueError(
                        f"{operation.type.value} operation에는 expected_revision과 "
                        "expected_target_sha256가 필요합니다."
                    )
        return self


def proposal_id(for_date: date, seed: str) -> str:
    """Build a deterministic proposal ID from a date and hexadecimal seed."""
    return f"PROP-{for_date:%Y%m%d}-{seed[:8].upper()}"
