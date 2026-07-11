"""Referential integrity rules for structured memory links."""

from pathlib import Path

from amplai_foundry.domain.enums import MemoryKind
from amplai_foundry.domain.models import MemoryObject
from amplai_foundry.lint.result import LintIssue, Severity


def validate_links(records: list[tuple[Path, MemoryObject]]) -> list[LintIssue]:
    """Ensure relation targets and provenance source IDs exist."""
    existing = {memory.id for _, memory in records}
    kinds = {memory.id: memory.kind for _, memory in records}
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
        for field, target in (
            ("superseded_by", memory.superseded_by),
            ("merged_into", memory.merged_into),
        ):
            if target and target not in existing:
                issues.append(
                    LintIssue(
                        Severity.ERROR,
                        "LINK_LIFECYCLE_TARGET_MISSING",
                        f"{field} target {target}가 존재하지 않습니다.",
                        path,
                        memory.id,
                    )
                )
    return issues
