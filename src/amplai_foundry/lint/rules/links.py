"""Referential integrity rules for structured memory links."""

from pathlib import Path

from amplai_foundry.domain.enums import MemoryKind, RelationType
from amplai_foundry.domain.models import MemoryObject
from amplai_foundry.lint.result import LintIssue, Severity


def validate_links(records: list[tuple[Path, MemoryObject]]) -> list[LintIssue]:
    """Ensure relation targets and provenance source IDs exist."""
    existing = {memory.id for _, memory in records}
    kinds = {memory.id: memory.kind for _, memory in records}
    memories = {memory.id: memory for _, memory in records}
    issues: list[LintIssue] = []
    for path, memory in records:
        for relation in memory.relations:
            if relation.target not in existing:
                issues.append(
                    LintIssue(
                        Severity.ERROR,
                        "LINK_RELATION_TARGET_MISSING",
                        f"relation target {relation.target}가 존재하지 않습니다.",
                        path,
                        memory.id,
                    )
                )
        for source_ref in memory.source_refs:
            if source_ref not in existing:
                issues.append(
                    LintIssue(
                        Severity.ERROR,
                        "LINK_SOURCE_REF_MISSING",
                        f"source reference {source_ref}가 존재하지 않습니다.",
                        path,
                        memory.id,
                    )
                )
            elif kinds[source_ref] is not MemoryKind.SOURCE:
                issues.append(
                    LintIssue(
                        Severity.ERROR,
                        "LINK_SOURCE_REF_KIND",
                        f"source reference {source_ref}는 kind=source note를 가리켜야 합니다.",
                        path,
                        memory.id,
                    )
                )
        for field, lifecycle_target in (
            ("superseded_by", memory.superseded_by),
            ("merged_into", memory.merged_into),
        ):
            if lifecycle_target and lifecycle_target not in existing:
                issues.append(
                    LintIssue(
                        Severity.ERROR,
                        "LINK_LIFECYCLE_TARGET_MISSING",
                        f"{field} target {lifecycle_target}가 존재하지 않습니다.",
                        path,
                        memory.id,
                    )
                )
        if memory.superseded_by and memory.superseded_by in memories:
            replacement = memories[memory.superseded_by]
            if not any(
                relation.type is RelationType.SUPERSEDES and relation.target == memory.id
                for relation in replacement.relations
            ):
                issues.append(
                    LintIssue(
                        Severity.ERROR,
                        "LINK_SUPERSEDED_BIDIRECTIONAL",
                        f"{memory.superseded_by}에 {memory.id} supersedes relation이 없습니다.",
                        path,
                        memory.id,
                    )
                )
        for relation in memory.relations:
            if relation.type is not RelationType.SUPERSEDES or relation.target not in memories:
                continue
            superseded_target = memories[relation.target]
            if superseded_target.superseded_by != memory.id:
                issues.append(
                    LintIssue(
                        Severity.ERROR,
                        "LINK_SUPERSEDES_BIDIRECTIONAL",
                        f"{relation.target}의 superseded_by가 {memory.id}가 아닙니다.",
                        path,
                        memory.id,
                    )
                )
            if superseded_target.kind is MemoryKind.SOURCE:
                issues.append(
                    LintIssue(
                        Severity.ERROR,
                        "LINK_SOURCE_SUPERSEDED",
                        "Source는 다른 지식에 의해 superseded될 수 없습니다.",
                        path,
                        memory.id,
                    )
                )
    return issues
