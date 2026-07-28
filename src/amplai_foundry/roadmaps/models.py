"""Roadmap definition, revision proposal, and impact models."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
RoadmapItemId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]+$")]
RoadmapProposalId = Annotated[
    str,
    StringConstraints(pattern=r"^RMAP-[0-9]{8}-[A-F0-9]{8}$"),
]


class RoadmapPhaseStatus(StrEnum):
    BACKLOG = "backlog"
    PLANNED = "planned"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


class RoadmapStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    ARCHIVED = "archived"
    SUPERSEDED = "superseded"


class RoadmapPhase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: RoadmapItemId
    order: int = Field(ge=0)
    status: RoadmapPhaseStatus
    depends_on: list[RoadmapItemId] = Field(default_factory=list)
    definition_of_done: list[NonEmptyString] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_dependencies(self) -> RoadmapPhase:
        if self.id in self.depends_on:
            raise ValueError("roadmap phase는 자신에게 의존할 수 없습니다.")
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("roadmap phase dependency는 중복될 수 없습니다.")
        return self


class RoadmapDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    roadmap_id: RoadmapItemId
    version: int = Field(ge=1)
    status: RoadmapStatus
    updated_at: date
    approved_at: date | None = None
    approved_by: NonEmptyString | None = None
    applied_proposal_id: RoadmapProposalId | None = None
    current_focus: RoadmapItemId | None = None
    north_star: dict[str, Any] = Field(default_factory=dict)
    phases: list[RoadmapPhase]

    @model_validator(mode="after")
    def validate_graph(self) -> RoadmapDefinition:
        ids = [phase.id for phase in self.phases]
        if len(ids) != len(set(ids)):
            raise ValueError("roadmap phase id는 고유해야 합니다.")
        orders = [phase.order for phase in self.phases]
        if len(orders) != len(set(orders)):
            raise ValueError("roadmap phase order는 고유해야 합니다.")
        known = set(ids)
        for phase in self.phases:
            missing = set(phase.depends_on) - known
            if missing:
                raise ValueError(
                    f"{phase.id} dependency가 존재하지 않습니다: {', '.join(sorted(missing))}"
                )
        if self.current_focus is not None and self.current_focus not in known:
            raise ValueError("current_focus는 존재하는 phase를 가리켜야 합니다.")
        visiting: set[str] = set()
        visited: set[str] = set()
        dependencies = {phase.id: phase.depends_on for phase in self.phases}

        def visit(item_id: str) -> None:
            if item_id in visiting:
                raise ValueError(f"roadmap dependency cycle이 있습니다: {item_id}")
            if item_id in visited:
                return
            visiting.add(item_id)
            for dependency in dependencies[item_id]:
                visit(dependency)
            visiting.remove(item_id)
            visited.add(item_id)

        for item_id in sorted(known):
            visit(item_id)
        return self


class RoadmapChangeType(StrEnum):
    ADD = "ADD"
    UPDATE = "UPDATE"
    MOVE = "MOVE"
    CANCEL = "CANCEL"
    SUPERSEDE = "SUPERSEDE"


class RoadmapChange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    item_id: RoadmapItemId
    type: RoadmapChangeType
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    reason: NonEmptyString


class RoadmapImpactAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    changed_items: list[RoadmapItemId]
    directly_dependent_items: list[RoadmapItemId]
    transitive_dependent_items: list[RoadmapItemId]
    current_focus_affected: bool
    requires_replan: bool


class RoadmapProposalStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    APPLIED = "applied"
    REJECTED = "rejected"


class RoadmapChangeProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    proposal_id: RoadmapProposalId
    roadmap_id: RoadmapItemId
    base_version: int = Field(ge=1)
    proposed_version: int = Field(ge=2)
    status: RoadmapProposalStatus = RoadmapProposalStatus.DRAFT
    created_at: datetime
    created_by: NonEmptyString
    changes: list[RoadmapChange] = Field(min_length=1)
    impact: RoadmapImpactAnalysis
    approved_at: datetime | None = None
    approved_by: NonEmptyString | None = None

    @model_validator(mode="after")
    def validate_versions_and_approval(self) -> RoadmapChangeProposal:
        if self.proposed_version != self.base_version + 1:
            raise ValueError("roadmap proposed_version은 base_version보다 정확히 1 커야 합니다.")
        if self.status in {RoadmapProposalStatus.APPROVED, RoadmapProposalStatus.APPLIED} and (
            self.approved_at is None or self.approved_by is None
        ):
            raise ValueError("approved/applied roadmap proposal에는 승인 정보가 필요합니다.")
        return self


class RoadmapArtifactReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    roadmap_id: RoadmapItemId
    referenced_phase_ids: list[RoadmapItemId]
    unmatched_phase_labels: list[str]
    disposition: Literal["no_structural_change", "review_required", "hold"]
    reason_codes: list[str]
    change_proposal: RoadmapChangeProposal | None = None
