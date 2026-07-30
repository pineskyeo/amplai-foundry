"""Durable fenced-publish contracts for canonical Git publication."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

from amplai_foundry.domain.identity import ProjectRef
from amplai_foundry.governance.models import Digest, ProposalRef

GitObjectId = Annotated[str, StringConstraints(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]


def _validate_canonical_branch_ref(value: str) -> str:
    forbidden = frozenset("~^:?*[\\")
    suffix = value.removeprefix("refs/heads/")
    if (
        not suffix
        or any(character.isspace() or character in forbidden for character in value)
        or ".." in value
        or "//" in value
        or "@{" in value
        or value.endswith(("/", "."))
    ):
        raise ValueError("canonical_ref는 안전한 fully qualified branch ref여야 합니다.")
    return value


CanonicalBranchRef = Annotated[
    str,
    StringConstraints(
        min_length=12,
        max_length=255,
        pattern=r"^refs/heads/.+$",
    ),
    AfterValidator(_validate_canonical_branch_ref),
]


class PublishGovernanceError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class PublishIntentState(StrEnum):
    PREPARED = "prepared"
    PUBLISHED = "published"
    PUBLISH_CONFLICT = "publish_conflict"
    CANCELLED = "cancelled"
    RECOVERY_HOLD = "recovery_hold"
    FAILED = "failed"


class PublishOutcome(StrEnum):
    PUBLISHED = "published"
    PUBLISH_CONFLICT = "publish_conflict"
    CANCELLED = "cancelled"
    FAILED = "failed"


class PublishGateState(StrEnum):
    UNLOCKED = "unlocked"
    LOCKED = "locked"
    RECOVERY_HOLD = "recovery_hold"


class PublishIntentView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    intent_id: str = Field(pattern=r"^PBI-[A-F0-9]{16}$")
    job_id: str
    snapshot_id: str
    proposal_ref: ProposalRef
    fencing_token: int = Field(ge=1)
    approved_snapshot_digest: Digest
    expected_base_revision: str = Field(pattern=r"^[0-9a-f]{7,64}$")
    staged_artifact_digest: Digest
    publish_request_digest: Digest
    canonical_ref: CanonicalBranchRef
    expected_old_ref: GitObjectId
    candidate_commit: GitObjectId
    candidate_tree_digest: Digest
    status: PublishIntentState
    prepared_at: AwareDatetime
    resolved_at: AwareDatetime | None = None
    last_error_code: str | None = None


class ProjectPublishGateView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_ref: ProjectRef
    canonical_ref: CanonicalBranchRef
    active_intent_id: str | None = Field(default=None, pattern=r"^PBI-[A-F0-9]{16}$")
    gate_revision: int = Field(ge=1)
    state: PublishGateState
    updated_at: AwareDatetime


class PublishResultView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    intent_id: str = Field(pattern=r"^PBI-[A-F0-9]{16}$")
    job_id: str
    snapshot_id: str
    proposal_ref: ProposalRef
    fencing_token: int = Field(ge=1)
    expected_old_ref: GitObjectId
    candidate_commit: GitObjectId
    actual_ref: GitObjectId
    outcome: PublishOutcome
    error_code: str | None = None
    resolved_at: AwareDatetime
