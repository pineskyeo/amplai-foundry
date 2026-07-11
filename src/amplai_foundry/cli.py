"""Command-line interface for knowledge validation and inspection."""

from pathlib import Path
from typing import Annotated

import typer

from amplai_foundry.lint.engine import KnowledgeLinter, LintExecutionError
from amplai_foundry.reporting.console import print_lint_report, print_stats
from amplai_foundry.repositories.markdown import MarkdownMemoryRepository, MarkdownRepositoryError

app = typer.Typer(
    name="amplai-foundry",
    help="Validate and inspect an AMPLAI Markdown knowledge vault.",
    no_args_is_help=True,
)


@app.command("lint")
def lint_command(
    vault: Annotated[Path, typer.Argument(help="Markdown vault directory to scan.")],
) -> None:
    """Run deterministic knowledge rules against a vault."""
    try:
        report = KnowledgeLinter().lint(vault)
    except LintExecutionError as error:
        typer.echo(f"FATAL   {error}", err=True)
        raise typer.Exit(code=2) from error
    print_lint_report(report, vault)
    if report.error_count:
        raise typer.Exit(code=1)


@app.command("stats")
def stats_command(
    vault: Annotated[Path, typer.Argument(help="Markdown vault directory to summarize.")],
) -> None:
    """Print knowledge inventory, lifecycle, and lint counts."""
    try:
        report = KnowledgeLinter().lint(vault)
    except LintExecutionError as error:
        typer.echo(f"FATAL   {error}", err=True)
        raise typer.Exit(code=2) from error
    print_stats(report)
    if report.error_count:
        raise typer.Exit(code=1)


@app.command("show")
def show_command(
    memory_id: Annotated[str, typer.Argument(help="Canonical memory ID.")],
    vault: Annotated[
        Path,
        typer.Option("--vault", help="Markdown vault directory to read."),
    ] = Path("vault"),
) -> None:
    """Show one canonical memory object as JSON."""
    try:
        memory = MarkdownMemoryRepository(vault).get(memory_id)
    except MarkdownRepositoryError as error:
        typer.echo(f"FATAL   {error}", err=True)
        raise typer.Exit(code=2) from error
    if memory is None:
        typer.echo(f"ERROR   memory ID가 존재하지 않습니다: {memory_id}", err=True)
        raise typer.Exit(code=1)
    typer.echo(memory.model_dump_json(indent=2))


if __name__ == "__main__":
    app()
