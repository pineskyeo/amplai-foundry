"""Orchestration for deterministic, rule-separated knowledge linting."""

from datetime import date
from pathlib import Path

from amplai_foundry.domain.models import MemoryObject
from amplai_foundry.lint.result import LintIssue, LintReport, Severity
from amplai_foundry.lint.rules.hygiene import HygieneConfig, validate_hygiene
from amplai_foundry.lint.rules.identifiers import validate_identifiers
from amplai_foundry.lint.rules.lifecycle import validate_lifecycles
from amplai_foundry.lint.rules.links import validate_links
from amplai_foundry.lint.rules.provenance import validate_provenance
from amplai_foundry.lint.rules.schema import validate_document
from amplai_foundry.lint.rules.sources import validate_sources
from amplai_foundry.parsing.markdown import MarkdownParseError, parse_markdown_file


class LintExecutionError(RuntimeError):
    """A fatal problem prevents the vault scan from running."""


class KnowledgeLinter:
    """Run deterministic schema, integrity, lifecycle, and hygiene rules."""

    def __init__(
        self,
        config: HygieneConfig | None = None,
        *,
        today: date | None = None,
    ) -> None:
        self.config = config or HygieneConfig()
        self.today = today or date.today()

    def lint(self, root: Path) -> LintReport:
        """Scan all Markdown notes below a vault root."""
        if not root.exists():
            raise LintExecutionError(f"vault 경로가 존재하지 않습니다: {root}")
        if not root.is_dir():
            raise LintExecutionError(f"vault 경로가 디렉터리가 아닙니다: {root}")
        try:
            paths = sorted(path for path in root.rglob("*.md") if ".obsidian" not in path.parts)
        except OSError as error:
            raise LintExecutionError(f"vault를 탐색할 수 없습니다: {error}") from error

        report = LintReport(notes_scanned=len(paths))
        records: list[tuple[Path, MemoryObject]] = []
        for path in paths:
            try:
                document = parse_markdown_file(path)
            except MarkdownParseError as error:
                report.issues.append(
                    LintIssue(
                        Severity.ERROR,
                        "FRONTMATTER_PARSE",
                        str(error),
                        path,
                        line=error.line,
                    )
                )
                continue
            memory, issues = validate_document(path, document)
            report.issues.extend(issues)
            if memory is not None:
                records.append((path, memory))

        report.memories = [memory for _, memory in records]
        report.issues.extend(validate_identifiers(records))
        report.issues.extend(validate_links(records))
        report.issues.extend(validate_lifecycles(records))
        report.issues.extend(validate_provenance(records))
        report.issues.extend(validate_sources(records))
        report.issues.extend(validate_hygiene(records, self.config, today=self.today))
        report.issues.sort(
            key=lambda issue: (str(issue.path), issue.line or 0, issue.severity, issue.code)
        )
        return report
