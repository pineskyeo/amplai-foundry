"""Lifecycle invariants that apply across storage adapters."""

from dataclasses import dataclass

from amplai_foundry.domain.enums import MemoryKind, MemoryStatus, RelationType
from amplai_foundry.domain.models import MemoryObject


@dataclass(frozen=True, slots=True)
class LifecycleViolation:
    """A stable lifecycle violation emitted by validation clients."""

    code: str
    message: str


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
