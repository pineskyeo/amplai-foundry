"""Command-line interface for source intake and knowledge curation."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Never

import typer

from amplai_foundry.curation.context_builder import ContextBuilderError, CurateContextBuilder
from amplai_foundry.domain.models import MemoryObject
from amplai_foundry.domain.project import ProjectPathError, validate_project_id
from amplai_foundry.ingestion.service import IngestionError, SourceIngestionService
from amplai_foundry.lint.engine import KnowledgeLinter, LintExecutionError
from amplai_foundry.parsing.markdown import parse_markdown_file
from amplai_foundry.proposals.apply import (
    ProposalApplyError,
    ProposalApplyService,
    approve_proposal,
)
from amplai_foundry.proposals.diff import proposal_diff
from amplai_foundry.proposals.repository import ProposalRepository, ProposalRepositoryError
from amplai_foundry.proposals.validation import ProposalValidator
from amplai_foundry.reporting.console import print_lint_report, print_stats
from amplai_foundry.repositories.markdown import MarkdownMemoryRepository, MarkdownRepositoryError
from amplai_foundry.schema import check_schemas, write_schemas
from amplai_foundry.search.lexical import LexicalKnowledgeSearch

app = typer.Typer(
    name="amplai-foundry",
    help="Ingest, validate, search, and safely curate an AMPLAI knowledge vault.",
    no_args_is_help=True,
)
source_app = typer.Typer(help="List, show, and verify immutable Sources.")
proposal_app = typer.Typer(help="Validate, review, approve, and apply Proposals.")
curate_app = typer.Typer(help="Prepare Codex curation context bundles.")
schema_app = typer.Typer(help="Generate and check Pydantic-derived JSON Schemas.")
app.add_typer(source_app, name="source")
app.add_typer(proposal_app, name="proposal")
app.add_typer(curate_app, name="curate")
app.add_typer(schema_app, name="schema")


def _fatal(message: str, *, code: int = 2) -> Never:
    typer.echo(f"ERROR   {message}", err=True)
    raise typer.Exit(code=code)


def _project(value: str) -> str:
    try:
        return validate_project_id(value)
    except ProjectPathError as error:
        _fatal(str(error))


def _optional_project(value: str | None) -> str | None:
    return _project(value) if value is not None else None


@app.command("ingest")
def ingest_command(
    input_path: Annotated[str, typer.Argument(help="UTF-8 .md/.txt path, or '-' for stdin.")],
    project: Annotated[str, typer.Option("--project", help="Existing Vault project.")],
    source_type: Annotated[str, typer.Option("--source-type")] = "chatgpt",
    title: Annotated[str | None, typer.Option("--title")] = None,
    namespace: Annotated[str | None, typer.Option("--namespace")] = None,
    vault: Annotated[Path, typer.Option("--vault")] = Path("vault"),
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Register exact file or stdin bytes as an immutable Source."""
    project = _project(project)
    original_filename: str | None = None
    media_type = "text/plain"
    if input_path == "-":
        content = sys.stdin.buffer.read()
        resolved_title = title or "Pasted ChatGPT Response"
    else:
        path = Path(input_path).expanduser()
        if path.suffix.casefold() not in {".md", ".txt"}:
            _fatal("입력 파일은 .md 또는 .txt여야 합니다.")
        try:
            content = path.read_bytes()
        except OSError as error:
            _fatal(f"입력 파일을 읽을 수 없습니다: {error}")
        original_filename = path.name
        media_type = "text/markdown" if path.suffix.casefold() == ".md" else "text/plain"
        resolved_title = title or path.stem
    try:
        result = SourceIngestionService(vault).ingest(
            content,
            project=project,
            source_type=source_type,
            title=resolved_title,
            namespace=namespace,
            original_filename=original_filename,
            media_type=media_type,
        )
    except IngestionError as error:
        _fatal(str(error))
    if json_output:
        typer.echo(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    else:
        typer.echo(f"{result.status.upper()} {result.source_id}")
        typer.echo(f"path: {result.path}")
        typer.echo(f"content_sha256: {result.content_sha256}")


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
    vault: Annotated[Path, typer.Option("--vault")] = Path("vault"),
) -> None:
    """Show one canonical memory object as JSON."""
    try:
        memory = MarkdownMemoryRepository(vault).get(memory_id)
    except MarkdownRepositoryError as error:
        _fatal(str(error))
    if memory is None:
        _fatal(f"memory ID가 존재하지 않습니다: {memory_id}", code=1)
    typer.echo(memory.model_dump_json(indent=2))


@app.command("search")
def search_command(
    query: Annotated[str, typer.Argument(help="Deterministic lexical query.")],
    project: Annotated[str | None, typer.Option("--project")] = None,
    status: Annotated[str, typer.Option("--status", help="Lifecycle status or 'all'.")] = "active",
    limit: Annotated[int, typer.Option("--limit", min=1)] = 10,
    vault: Annotated[Path, typer.Option("--vault")] = Path("vault"),
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Search IDs, titles, summaries, tags, bodies, and relation targets."""
    project = _optional_project(project)
    try:
        repository = MarkdownMemoryRepository(vault)
        results = LexicalKnowledgeSearch(repository, repository.path_for).search(
            query,
            project=project,
            status=None if status == "all" else status,
            limit=limit,
        )
    except MarkdownRepositoryError as error:
        _fatal(str(error))
    if json_output:
        typer.echo(json.dumps([asdict(result) for result in results], ensure_ascii=False, indent=2))
        return
    for result in results:
        typer.echo(
            f"{result.score:4d}  {result.id}  {result.kind}/{result.status}  {result.title}\n"
            f"      {result.summary}\n      {result.path}"
        )


@source_app.command("list")
def source_list_command(
    project: Annotated[str | None, typer.Option("--project")] = None,
    vault: Annotated[Path, typer.Option("--vault")] = Path("vault"),
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    project = _optional_project(project)
    try:
        records = SourceIngestionService(vault).list(project=project)
    except IngestionError as error:
        _fatal(str(error))
    items = [
        {
            "source_id": identifier,
            "path": str(path),
            "content_sha256": metadata.content_sha256,
            "source_type": metadata.source_type,
        }
        for path, metadata, identifier in records
    ]
    if json_output:
        typer.echo(json.dumps(items, ensure_ascii=False, indent=2))
    else:
        for item in items:
            typer.echo(f"{item['source_id']}  {item['source_type']}  {item['path']}")


@source_app.command("show")
def source_show_command(
    source_id: Annotated[str, typer.Argument()],
    vault: Annotated[Path, typer.Option("--vault")] = Path("vault"),
) -> None:
    found = SourceIngestionService(vault).find(source_id)
    if found is None:
        _fatal(f"Source ID가 존재하지 않습니다: {source_id}", code=1)
    path, _metadata = found
    document = parse_markdown_file(path)
    memory = MemoryObject.model_validate({**document.metadata, "content": document.content})
    typer.echo(memory.model_dump_json(indent=2))


@source_app.command("verify")
def source_verify_command(
    source_id: Annotated[str, typer.Argument()],
    vault: Annotated[Path, typer.Option("--vault")] = Path("vault"),
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    try:
        result = SourceIngestionService(vault).verify(source_id)
    except IngestionError as error:
        _fatal(str(error), code=1)
    if json_output:
        typer.echo(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    else:
        typer.echo(f"{'OK' if result.valid else 'ERROR'} {result.source_id} {result.path}")
        typer.echo(f"content_sha256: {result.content_sha256}")
    if not result.valid:
        raise typer.Exit(code=1)


@proposal_app.command("validate")
def proposal_validate_command(
    path: Annotated[Path, typer.Argument()],
    vault: Annotated[Path, typer.Option("--vault")] = Path("vault"),
) -> None:
    try:
        proposal = ProposalRepository.load(path)
    except ProposalRepositoryError as error:
        _fatal(str(error), code=1)
    issues = ProposalValidator(vault).validate(proposal, path)
    if issues:
        for issue in issues:
            typer.echo(f"ERROR {issue.code} {issue.message}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"OK {proposal.proposal_id}")


@proposal_app.command("show")
def proposal_show_command(
    proposal_id: Annotated[str, typer.Argument()],
    proposal_root: Annotated[Path, typer.Option("--proposal-root")] = Path(".amplai/proposals"),
) -> None:
    try:
        proposal = ProposalRepository(proposal_root).get(proposal_id)
    except ProposalRepositoryError as error:
        _fatal(str(error), code=1)
    if proposal is None:
        _fatal(f"Proposal ID가 존재하지 않습니다: {proposal_id}", code=1)
    typer.echo(proposal.model_dump_json(indent=2))


@proposal_app.command("diff")
def proposal_diff_command(
    proposal_id: Annotated[str, typer.Argument()],
    vault: Annotated[Path, typer.Option("--vault")] = Path("vault"),
    proposal_root: Annotated[Path, typer.Option("--proposal-root")] = Path(".amplai/proposals"),
) -> None:
    repository = ProposalRepository(proposal_root)
    try:
        proposal = repository.get(proposal_id)
        if proposal is None:
            _fatal(f"Proposal ID가 존재하지 않습니다: {proposal_id}", code=1)
        typer.echo(proposal_diff(proposal, repository.path_for(proposal_id), vault))
    except (ProposalRepositoryError, OSError, ValueError) as error:
        _fatal(str(error), code=1)


@proposal_app.command("approve")
def proposal_approve_command(
    proposal_id: Annotated[str, typer.Argument()],
    approved_by: Annotated[str, typer.Option("--approved-by")],
    proposal_root: Annotated[Path, typer.Option("--proposal-root")] = Path(".amplai/proposals"),
) -> None:
    repository = ProposalRepository(proposal_root)
    try:
        proposal = repository.get(proposal_id)
        if proposal is None:
            _fatal(f"Proposal ID가 존재하지 않습니다: {proposal_id}", code=1)
        approved = approve_proposal(proposal, approved_by=approved_by)
        repository.save(approved)
    except (ProposalRepositoryError, ProposalApplyError) as error:
        _fatal(str(error), code=1)
    typer.echo(f"APPROVED {proposal_id} by {approved_by}")


@proposal_app.command("apply")
def proposal_apply_command(
    proposal_id: Annotated[str, typer.Argument()],
    vault: Annotated[Path, typer.Option("--vault")] = Path("vault"),
    proposal_root: Annotated[Path, typer.Option("--proposal-root")] = Path(".amplai/proposals"),
) -> None:
    repository = ProposalRepository(proposal_root)
    try:
        proposal = repository.get(proposal_id)
        if proposal is None:
            _fatal(f"Proposal ID가 존재하지 않습니다: {proposal_id}", code=1)
        result = ProposalApplyService(vault, repository).apply(
            proposal, repository.path_for(proposal_id)
        )
    except (ProposalRepositoryError, ProposalApplyError, LintExecutionError) as error:
        _fatal(str(error), code=1)
    typer.echo(f"APPLIED {result.proposal_id}")
    for path in result.touched_paths:
        typer.echo(f"  {path}")


@curate_app.command("prepare")
def curate_prepare_command(
    source_id: Annotated[str, typer.Argument()],
    project: Annotated[str, typer.Option("--project")],
    output: Annotated[Path, typer.Option("--output")],
    vault: Annotated[Path, typer.Option("--vault")] = Path("vault"),
) -> None:
    project = _project(project)
    try:
        path = CurateContextBuilder(vault).write(source_id, project=project, output=output)
    except (ContextBuilderError, IngestionError, OSError, UnicodeError) as error:
        _fatal(str(error), code=1)
    typer.echo(f"CREATED {path}")


@schema_app.command("generate")
def schema_generate_command() -> None:
    for path in write_schemas():
        typer.echo(f"GENERATED {path}")


@schema_app.command("check")
def schema_check_command() -> None:
    changed = check_schemas()
    if changed:
        for path in changed:
            typer.echo(f"OUTDATED {path}", err=True)
        raise typer.Exit(code=1)
    typer.echo("OK schemas are current")


if __name__ == "__main__":
    app()
