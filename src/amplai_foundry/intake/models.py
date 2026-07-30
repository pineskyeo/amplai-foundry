"""Intent, classification, policy, evaluation, and intake-run contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from amplai_foundry.domain.identity import MemoryRef, ProjectRef
from amplai_foundry.governance.authority import ExternalActorIdentity
from amplai_foundry.proposals.models import ProposalId
from amplai_foundry.semantics.models import ComparisonResult, KnowledgeCandidate

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ArtifactRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: NonEmptyString
    source_type: NonEmptyString = "document"
    title: NonEmptyString | None = None
    media_type: NonEmptyString = "text/markdown"


class IntentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    instruction: NonEmptyString
    artifacts: list[ArtifactRef] = Field(min_length=1)
    project_hint: str | None = None
    expected_outcome: NonEmptyString | None = None
    identity: ExternalActorIdentity


class ArtifactKind(StrEnum):
    ROADMAP = "roadmap"
    ARCHITECTURE_PROPOSAL = "architecture_proposal"
    MEETING_NOTE = "meeting_note"
    INCIDENT = "incident"
    SOP = "sop"
    IMPLEMENTATION_RESULT = "implementation_result"
    UNKNOWN = "unknown"


class ArtifactClassification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ArtifactKind
    confidence: float = Field(ge=0, le=1)
    reason_codes: list[NonEmptyString] = Field(min_length=1)
    held: bool = False


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    RESTRICTED = "restricted"


class PolicyDisposition(StrEnum):
    AUTO_APPLY = "auto_apply"
    REVIEW_REQUIRED = "review_required"
    HOLD = "hold"
    IGNORE = "ignore"


class PolicyDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str
    risk: RiskLevel
    disposition: PolicyDisposition
    reason_codes: list[NonEmptyString] = Field(min_length=1)


class ValidationCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: NonEmptyString
    passed: bool
    detail: NonEmptyString


class MinimalEvaluationRecord(BaseModel):
    """Phase 1 seed record for the later Evaluation Observatory."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fixture_id: NonEmptyString
    project_resolution: Literal["pass", "hold", "fail"]
    classification: Literal["pass", "hold", "fail"]
    duplicate_created: bool
    unsafe_change_applied: bool
    safe_change_applied: bool = False
    proposal_reproducible: bool
    user_corrections: int = Field(ge=0)
    latency_ms: int = Field(ge=0)
    token_usage: int = Field(ge=0)
    failure_category: str | None = None


class ValidationReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    checks: list[ValidationCheck]
    evaluation: MinimalEvaluationRecord

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)


class SourceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["created", "duplicate"]
    ref: MemoryRef
    path: str
    content_sha256: str
    duplicate_of: str | None = None


class IntakeRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    run_id: NonEmptyString
    created_at: datetime
    project: ProjectRef
    request: IntentRequest
    source: SourceRecord
    classification: ArtifactClassification
    candidates: list[KnowledgeCandidate] = Field(default_factory=list)
    comparisons: list[ComparisonResult] = Field(default_factory=list)
    policy_decisions: list[PolicyDecision] = Field(default_factory=list)
    proposal_id: ProposalId | None = None
    roadmap_update_path: str | None = None
    validation: ValidationReport


class IntakeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["prepared", "hold"]
    project: ProjectRef | None = None
    resolution_reason: str
    source: SourceRecord | None = None
    classification: ArtifactClassification | None = None
    candidates: list[KnowledgeCandidate] = Field(default_factory=list)
    comparisons: list[ComparisonResult] = Field(default_factory=list)
    policy_decisions: list[PolicyDecision] = Field(default_factory=list)
    proposal_id: str | None = None
    intake_run_path: str | None = None
    roadmap_update_path: str | None = None
    validation: ValidationReport


class ResolutionHoldRecord(BaseModel):
    """Workspace-level replay seed written before any Project Pack is selected."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    hold_id: Annotated[str, StringConstraints(pattern=r"^HOLD-[A-F0-9]{16}$")]
    created_at: datetime
    request_fingerprint: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    artifact_count: int = Field(ge=1)
    project_hint: str | None = None
    project: ProjectRef | None = None
    reason: NonEmptyString
    validation: ValidationReport
