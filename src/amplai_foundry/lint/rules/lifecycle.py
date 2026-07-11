"""Lint adapter for storage-independent lifecycle invariants."""

from pathlib import Path

from amplai_foundry.domain.lifecycle import validate_lifecycle
from amplai_foundry.domain.models import MemoryObject
from amplai_foundry.lint.result import LintIssue, Severity


def validate_lifecycles(records: list[tuple[Path, MemoryObject]]) -> list[LintIssue]:
    """Convert domain lifecycle violations into lint issues."""
    issues: list[LintIssue] = []
    for path, memory in records:
        for violation in validate_lifecycle(memory):
            issues.append(
                LintIssue(
                    Severity.ERROR,
                    violation.code,
                    violation.message,
                    path,
                    memory.id,
                )
            )
    return issues
