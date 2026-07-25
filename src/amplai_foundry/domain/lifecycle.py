"""Lifecycle invariants that apply across storage adapters."""

from dataclasses import dataclass

from amplai_foundry.domain.enums import MemoryKind, MemoryStatus, RelationType
from amplai_foundry.domain.models import MemoryObject


@dataclass(frozen=True, slots=True)
class LifecycleViolation:
    """A stable lifecycle violation emitted by validation clients."""

    code: str
    message: str


ALLOWED_STATUS_TRANSITIONS: dict[MemoryStatus, frozenset[MemoryStatus]] = {
    MemoryStatus.CANDIDATE: frozenset(
        {
            MemoryStatus.CANDIDATE,
            MemoryStatus.ACTIVE,
            MemoryStatus.REJECTED,
            MemoryStatus.ARCHIVED,
        }
    ),
    MemoryStatus.ACTIVE: frozenset(
        {
            MemoryStatus.ACTIVE,
            MemoryStatus.DEPRECATED,
            MemoryStatus.SUPERSEDED,
            MemoryStatus.MERGED,
            MemoryStatus.ARCHIVED,
        }
    ),
    MemoryStatus.DEPRECATED: frozenset(
        {
            MemoryStatus.DEPRECATED,
            MemoryStatus.ACTIVE,
            MemoryStatus.SUPERSEDED,
            MemoryStatus.ARCHIVED,
        }
    ),
    MemoryStatus.SUPERSEDED: frozenset({MemoryStatus.SUPERSEDED}),
    MemoryStatus.MERGED: frozenset({MemoryStatus.MERGED}),
    MemoryStatus.REJECTED: frozenset({MemoryStatus.REJECTED}),
    MemoryStatus.ARCHIVED: frozenset({MemoryStatus.ARCHIVED}),
}


def validate_transition(
    before: MemoryObject,
    after: MemoryObject,
    operation_type: object,
) -> list[LifecycleViolation]:
    """Validate immutable fields, revision, timestamps, and lifecycle transitions."""
    violations: list[LifecycleViolation] = []
    immutable_fields = (
        ("id", "ID"),
        ("project", "project"),
        ("namespace", "namespace"),
        ("kind", "kind"),
        ("created_at", "created_at"),
    )
    for field, label in immutable_fields:
        if getattr(after, field) != getattr(before, field):
            violations.append(
                LifecycleViolation(
                    f"PROPOSAL_TRANSITION_{field.upper()}",
                    f"{label}는 변경할 수 없습니다.",
                )
            )
    if after.revision != before.revision + 1:
        violations.append(
            LifecycleViolation(
                "PROPOSAL_TRANSITION_REVISION",
                "revision은 기존 revision보다 정확히 1 증가해야 합니다.",
            )
        )
    if after.updated_at < before.updated_at:
        violations.append(
            LifecycleViolation(
                "PROPOSAL_TRANSITION_UPDATED_AT",
                "updated_at은 기존 updated_at보다 이전일 수 없습니다.",
            )
        )
    if after.status not in ALLOWED_STATUS_TRANSITIONS[before.status]:
        violations.append(
            LifecycleViolation(
                "PROPOSAL_TRANSITION_STATUS",
                f"status={before.status.value}에서 {after.status.value}(으)로 전환할 수 없습니다.",
            )
        )
    if before.kind is MemoryKind.SOURCE or after.kind is MemoryKind.SOURCE:
        violations.append(
            LifecycleViolation(
                "PROPOSAL_SOURCE_MUTATION_FORBIDDEN",
                "Source는 ingest 이후 Proposal로 변경할 수 없습니다.",
            )
        )
    operation_name = getattr(operation_type, "value", operation_type)
    if operation_name == "SUPERSEDE":
        if after.status is not MemoryStatus.SUPERSEDED:
            violations.append(
                LifecycleViolation(
                    "PROPOSAL_SUPERSEDE_STATUS_REQUIRED",
                    "SUPERSEDE 결과 target status는 superseded여야 합니다.",
                )
            )
        if not after.superseded_by:
            violations.append(
                LifecycleViolation(
                    "PROPOSAL_SUPERSEDE_TARGET_REQUIRED",
                    "SUPERSEDE 결과 target에는 superseded_by가 필요합니다.",
                )
            )
    return violations


def validate_lifecycle(memory: MemoryObject) -> list[LifecycleViolation]:
    """Evaluate storage-independent lifecycle invariants."""
    violations: list[LifecycleViolation] = []
    if memory.status is MemoryStatus.SUPERSEDED and not memory.superseded_by:
        violations.append(
            LifecycleViolation(
                "LIFECYCLE_SUPERSEDED_BY_REQUIRED",
                "status=superseded이지만 superseded_by가 없습니다.",
            )
        )
    if memory.status is MemoryStatus.MERGED and not memory.merged_into:
        violations.append(
            LifecycleViolation(
                "LIFECYCLE_MERGED_INTO_REQUIRED",
                "status=merged이지만 merged_into가 없습니다.",
            )
        )
    if memory.kind is MemoryKind.SOURCE and (
        memory.status is MemoryStatus.SUPERSEDED or memory.superseded_by is not None
    ):
        violations.append(
            LifecycleViolation(
                "LIFECYCLE_SOURCE_IMMUTABLE",
                "Source는 superseded lifecycle을 사용할 수 없습니다.",
            )
        )
    if (
        memory.kind is MemoryKind.DECISION
        and memory.status in {MemoryStatus.ACTIVE, MemoryStatus.SUPERSEDED}
        and "## 결정" not in memory.content
        and "## Decision" not in memory.content
    ):
        violations.append(
            LifecycleViolation(
                "LIFECYCLE_DECISION_BODY_REQUIRED",
                "active 또는 superseded decision에는 '## 결정' 본문이 필요합니다.",
            )
        )
    if memory.kind is MemoryKind.QUESTION and memory.status is not MemoryStatus.ACTIVE:
        resolution_types = {
            RelationType.RELATED_TO,
            RelationType.SUPERSEDES,
            RelationType.DERIVED_FROM,
        }
        if not any(relation.type in resolution_types for relation in memory.relations):
            violations.append(
                LifecycleViolation(
                    "LIFECYCLE_QUESTION_RESOLUTION_REQUIRED",
                    "종료된 question은 관련 decision 또는 knowledge와 연결되어야 합니다.",
                )
            )
    return violations
