"""Identifier uniqueness and self-reference rules."""

from collections import defaultdict
from pathlib import Path

from amplai_foundry.domain.models import MemoryObject
from amplai_foundry.lint.result import LintIssue, Severity

KIND_PREFIXES = {
    "source": "SRC-",
    "concept": "CON-",
    "principle": "PRI-",
    "decision": "DEC-",
    "question": "QUE-",
    "architecture": "ARC-",
    "experiment": "EXP-",
    "map": "MAP-",
}


def validate_identifiers(
    records: list[tuple[Path, MemoryObject]],
) -> list[LintIssue]:
    """Check ID uniqueness, prefix consistency, and self references."""
    issues: list[LintIssue] = []
    grouped: dict[str, list[Path]] = defaultdict(list)
    for path, memory in records:
        grouped[memory.id].append(path)
        expected_prefix = KIND_PREFIXES[memory.kind.value]
        if not memory.id.startswith(expected_prefix):
            issues.append(
                LintIssue(
                    Severity.ERROR,
                    "IDENTIFIER_KIND_PREFIX",
                    f"kind={memory.kind.value} ID는 '{expected_prefix}'로 시작해야 합니다.",
                    path,
                    memory.id,
                )
            )
        if memory.id in memory.referenced_ids():
            issues.append(
                LintIssue(
                    Severity.ERROR,
                    "IDENTIFIER_SELF_REFERENCE",
                    "자기 자신을 relation 또는 reference target으로 참조합니다.",
                    path,
                    memory.id,
                )
            )
    for memory_id, paths in grouped.items():
        if len(paths) > 1:
            for path in paths:
                issues.append(
                    LintIssue(
                        Severity.ERROR,
                        "IDENTIFIER_DUPLICATE",
                        f"ID {memory_id}가 {len(paths)}개 파일에서 중복됩니다.",
                        path,
                        memory_id,
                    )
                )
    return issues
