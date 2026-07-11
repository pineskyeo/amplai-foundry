"""Stable result types shared by lint rules and reporters."""

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from amplai_foundry.domain.models import MemoryObject


class Severity(StrEnum):
    """Severity used to determine process exit behavior."""

    ERROR = "ERROR"
    WARNING = "WARNING"


@dataclass(frozen=True, slots=True)
class LintIssue:
    """One actionable problem associated with a knowledge file."""

    severity: Severity
    code: str
    message: str
    path: Path
    memory_id: str = "UNKNOWN"
    line: int | None = None


@dataclass(slots=True)
class LintReport:
    """Complete result of scanning one vault."""

    issues: list[LintIssue] = field(default_factory=list)
    notes_scanned: int = 0
    memories: list[MemoryObject] = field(default_factory=list)

    @property
    def error_count(self) -> int:
        """Return the number of errors."""
        return sum(issue.severity is Severity.ERROR for issue in self.issues)

    @property
    def warning_count(self) -> int:
        """Return the number of warnings."""
        return sum(issue.severity is Severity.WARNING for issue in self.issues)

    def issues_with_code(self, code: str) -> list[LintIssue]:
        """Return issues matching one stable rule code."""
        return [issue for issue in self.issues if issue.code == code]
