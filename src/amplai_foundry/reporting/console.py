"""Plain-text console reporting suitable for humans and CI logs."""

from collections import Counter
from pathlib import Path

import typer

from amplai_foundry.domain.enums import MemoryKind, MemoryStatus
from amplai_foundry.lint.result import LintReport


def _display_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def print_lint_report(report: LintReport, root: Path) -> None:
    """Print issues followed by a stable count summary."""
    for issue in report.issues:
        location = _display_path(issue.path, root)
        if issue.line is not None:
            location = f"{location}:{issue.line}"
        typer.echo(f"{issue.severity.value:<7} {issue.memory_id:<14} {location}  {issue.message}")
    typer.echo(
        f"{report.error_count} errors, {report.warning_count} warnings, "
        f"{report.notes_scanned} notes scanned"
    )


def print_stats(report: LintReport) -> None:
    """Print vault counts and lint health."""
    kind_counts = Counter(memory.kind for memory in report.memories)
    status_counts = Counter(memory.status for memory in report.memories)
    open_questions = sum(
        memory.kind is MemoryKind.QUESTION and memory.status is MemoryStatus.ACTIVE
        for memory in report.memories
    )
    superseded_decisions = sum(
        memory.kind is MemoryKind.DECISION and memory.status is MemoryStatus.SUPERSEDED
        for memory in report.memories
    )
    orphan_ids = {issue.memory_id for issue in report.issues_with_code("HYGIENE_ORPHAN")}

    typer.echo(f"Total notes: {report.notes_scanned}")
    typer.echo("By kind:")
    for kind in MemoryKind:
        typer.echo(f"  {kind.value}: {kind_counts[kind]}")
    typer.echo("By status:")
    for status in MemoryStatus:
        typer.echo(f"  {status.value}: {status_counts[status]}")
    typer.echo(f"Open questions: {open_questions}")
    typer.echo(f"Superseded decisions: {superseded_decisions}")
    typer.echo(f"Orphan notes: {len(orphan_ids)}")
    typer.echo(f"Errors: {report.error_count}")
    typer.echo(f"Warnings: {report.warning_count}")
