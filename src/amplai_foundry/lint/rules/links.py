"""Referential integrity rules for structured memory links."""

from pathlib import Path

from amplai_foundry.domain.enums import MemoryKind, RelationType
from amplai_foundry.domain.identity import MemoryRef
from amplai_foundry.domain.models import MemoryObject
from amplai_foundry.lint.result import LintIssue, Severity


def validate_links(
    records: list[tuple[Path, MemoryObject]],
    *,
    external_refs: set[MemoryRef] | None = None,
) -> list[LintIssue]:
    """Ensure relation targets and provenance source IDs exist."""
    existing = {memory.ref for _, memory in records} | (external_refs or set())
    kinds = {memory.ref: memory.kind for _, memory in records}
    memories = {memory.ref: memory for _, memory in records}
    issues: list[LintIssue] = []
    for path, memory in records:
        for relation in memory.relations:
            target_ref = relation.to_ref(memory.namespace)
            if target_ref not in existing:
                issues.append(
                    LintIssue(
                        Severity.ERROR,
                        "LINK_RELATION_TARGET_MISSING",
                        f"relation target {target_ref}가 존재하지 않습니다.",
                        path,
                        memory.id,
                    )
                )
        for source_ref in memory.source_refs:
            qualified_source = MemoryRef(namespace=memory.namespace, local_id=source_ref)
            if qualified_source not in existing:
                issues.append(
                    LintIssue(
                        Severity.ERROR,
                        "LINK_SOURCE_REF_MISSING",
                        f"source reference {source_ref}가 존재하지 않습니다.",
                        path,
                        memory.id,
                    )
                )
            elif kinds[qualified_source] is not MemoryKind.SOURCE:
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
            qualified_target = (
                MemoryRef(namespace=memory.namespace, local_id=lifecycle_target)
                if lifecycle_target
                else None
            )
            if qualified_target and qualified_target not in existing:
                issues.append(
                    LintIssue(
                        Severity.ERROR,
                        "LINK_LIFECYCLE_TARGET_MISSING",
                        f"{field} target {lifecycle_target}가 존재하지 않습니다.",
                        path,
                        memory.id,
                    )
                )
        superseded_by_ref = (
            MemoryRef(namespace=memory.namespace, local_id=memory.superseded_by)
            if memory.superseded_by
            else None
        )
        if superseded_by_ref and superseded_by_ref in memories:
            replacement = memories[superseded_by_ref]
            if not any(
                relation.type is RelationType.SUPERSEDES
                and relation.to_ref(replacement.namespace) == memory.ref
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
            target_ref = relation.to_ref(memory.namespace)
            if relation.type is not RelationType.SUPERSEDES or target_ref not in memories:
                continue
            superseded_target = memories[target_ref]
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
