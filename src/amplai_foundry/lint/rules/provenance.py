"""Provenance requirements for reviewed official knowledge."""

from pathlib import Path

from amplai_foundry.domain.enums import MemoryKind, MemoryStatus
from amplai_foundry.domain.models import MemoryObject
from amplai_foundry.lint.result import LintIssue, Severity


def validate_provenance(records: list[tuple[Path, MemoryObject]]) -> list[LintIssue]:
    """Require source references for active knowledge claims."""
    issues: list[LintIssue] = []
    for path, memory in records:
        if (
            memory.status is MemoryStatus.ACTIVE
            and memory.kind is not MemoryKind.SOURCE
            and not memory.source_refs
        ):
            issues.append(
                LintIssue(
                    Severity.ERROR,
                    "PROVENANCE_ACTIVE_SOURCE_REQUIRED",
                    "active 공식 지식에는 하나 이상의 source_refs가 필요합니다.",
                    path,
                    memory.id,
                )
            )
    return issues
