"""Command-line interface for source intake and knowledge curation."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Annotated, Never
from zoneinfo import ZoneInfo

import typer

from amplai_foundry.curation.context_builder import ContextBuilderError, CurateContextBuilder
from amplai_foundry.domain.models import MemoryObject
from amplai_foundry.domain.project import ProjectPathError, validate_project_id
from amplai_foundry.ingestion.service import IngestionError, SourceIngestionService
from amplai_foundry.intake.models import (
    ArtifactRef,
    AuthorityContext,
    AuthorityKind,
    IntentRequest,
)
from amplai_foundry.intake.service import KnowledgeIntakeError, KnowledgeIntakeService
from amplai_foundry.lint.engine import KnowledgeLinter, LintExecutionError
from amplai_foundry.parsing.markdown import parse_markdown_file
from amplai_foundry.projects.domain_registry import DomainRegistryError
from amplai_foundry.projects.repository import ProjectPackError, ProjectPackRepository
from amplai_foundry.projects.service import ProjectPackService
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
from amplai_foundry.roadmaps.proposals import (
    RoadmapProposalRepository,
    RoadmapProposalRepositoryError,
)
from amplai_foundry.roadmaps.repository import RoadmapRepository, RoadmapRepositoryError
from amplai_foundry.roadmaps.service import RoadmapService
from amplai_foundry.schema import check_schemas, write_schemas
from amplai_foundry.search.lexical import LexicalKnowledgeSearch
from amplai_foundry.verification.runner import VerificationRunner

app = typer.Typer(
    name="amplai-foundry",
    help="Ingest, validate, search, and safely curate an AMPLAI knowledge vault.",
    no_args_is_help=True,
)
source_app = typer.Typer(help="List, show, and verify immutable Sources.")
proposal_app = typer.Typer(help="Validate, review, approve, and apply Proposals.")
curate_app = typer.Typer(help="Prepare Codex curation context bundles.")
schema_app = typer.Typer(help="Generate and check Pydantic-derived JSON Schemas.")
project_app = typer.Typer(help="Inspect, validate, rebuild, and archive Project Packs.")
intake_app = typer.Typer(help="Process intent and artifacts through governed knowledge intake.")
roadmap_app = typer.Typer(help="Diff and inspect versioned roadmap plans.")
app.add_typer(source_app, name="source")
app.add_typer(proposal_app, name="proposal")
app.add_typer(curate_app, name="curate")
app.add_typer(schema_app, name="schema")
app.add_typer(project_app, name="project")
app.add_typer(intake_app, name="intake")
app.add_typer(roadmap_app, name="roadmap")


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
    project: Annotated[str | None, typer.Option("--project")] = None,
    vault: Annotated[Path, typer.Option("--vault")] = Path("vault"),
) -> None:
    found = SourceIngestionService(vault).find(source_id, project=project)
    if found is None:
        _fatal(f"Source ID가 존재하지 않습니다: {source_id}", code=1)
    path, _metadata = found
    document = parse_markdown_file(path)
    memory = MemoryObject.model_validate({**document.metadata, "content": document.content})
    typer.echo(memory.model_dump_json(indent=2))


@source_app.command("verify")
def source_verify_command(
    source_id: Annotated[str, typer.Argument()],
    project: Annotated[str | None, typer.Option("--project")] = None,
    vault: Annotated[Path, typer.Option("--vault")] = Path("vault"),
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    try:
        result = SourceIngestionService(vault).verify(source_id, project=project)
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


@project_app.command("list")
def project_list_command(
    workspace: Annotated[Path, typer.Option("--workspace")] = Path("."),
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    try:
        packs = ProjectPackRepository(workspace).list()
    except ProjectPackError as error:
        _fatal(str(error), code=1)
    items = [
        {
            "project_id": pack.manifest.project_id,
            "namespace": pack.manifest.namespace,
            "name": pack.manifest.name,
            "default": pack.manifest.default,
            "root": str(pack.root),
        }
        for pack in packs
    ]
    if json_output:
        typer.echo(json.dumps(items, ensure_ascii=False, indent=2))
    else:
        for item in items:
            marker = "*" if item["default"] else " "
            typer.echo(f"{marker} {item['project_id']}  {item['namespace']}  {item['root']}")


@project_app.command("show")
def project_show_command(
    project_id: Annotated[str, typer.Argument()],
    workspace: Annotated[Path, typer.Option("--workspace")] = Path("."),
) -> None:
    try:
        pack = ProjectPackRepository(workspace).get(project_id)
    except ProjectPackError as error:
        _fatal(str(error), code=1)
    if pack is None:
        _fatal(f"Project Pack이 없습니다: {project_id}", code=1)
    typer.echo(pack.manifest.model_dump_json(indent=2))


@project_app.command("validate")
def project_validate_command(
    project_id: Annotated[str | None, typer.Argument()] = None,
    workspace: Annotated[Path, typer.Option("--workspace")] = Path("."),
) -> None:
    repository = ProjectPackRepository(workspace)
    try:
        packs = repository.list()
        if project_id is not None:
            packs = [pack for pack in packs if pack.manifest.project_id == project_id]
            if not packs:
                _fatal(f"Project Pack이 없습니다: {project_id}", code=1)
        service = ProjectPackService(repository)
        failed = False
        for pack in packs:
            result = service.validate(pack)
            if result.valid:
                typer.echo(f"OK {result.project_id}")
                continue
            failed = True
            for issue in result.issues:
                typer.echo(f"ERROR {result.project_id} {issue}", err=True)
        if failed:
            raise typer.Exit(code=1)
    except (ProjectPackError, DomainRegistryError, LintExecutionError) as error:
        _fatal(str(error), code=1)


@project_app.command("rebuild")
def project_rebuild_command(
    project_id: Annotated[str, typer.Argument()],
    workspace: Annotated[Path, typer.Option("--workspace")] = Path("."),
) -> None:
    repository = ProjectPackRepository(workspace)
    try:
        pack = repository.get(project_id)
        if pack is None:
            _fatal(f"Project Pack이 없습니다: {project_id}", code=1)
        path = ProjectPackService(repository).rebuild(pack)
    except (ProjectPackError, DomainRegistryError, LintExecutionError, ValueError) as error:
        _fatal(str(error), code=1)
    typer.echo(f"REBUILT {path}")


@project_app.command("pack")
def project_pack_command(
    project_id: Annotated[str, typer.Argument()],
    output: Annotated[Path, typer.Option("--output")],
    workspace: Annotated[Path, typer.Option("--workspace")] = Path("."),
) -> None:
    repository = ProjectPackRepository(workspace)
    try:
        pack = repository.get(project_id)
        if pack is None:
            _fatal(f"Project Pack이 없습니다: {project_id}", code=1)
        path = ProjectPackService(repository).pack(pack, output)
    except (ProjectPackError, OSError, ValueError) as error:
        _fatal(str(error), code=1)
    typer.echo(f"PACKED {path}")


@intake_app.command("process")
def intake_process_command(
    artifact_paths: Annotated[
        list[Path],
        typer.Argument(help="One or more UTF-8 artifacts."),
    ],
    instruction: Annotated[str, typer.Option("--instruction")],
    project: Annotated[str | None, typer.Option("--project")] = None,
    source_type: Annotated[str, typer.Option("--source-type")] = "document",
    actor: Annotated[str, typer.Option("--actor")] = "user",
    workspace: Annotated[Path, typer.Option("--workspace")] = Path("."),
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    request = IntentRequest(
        instruction=instruction,
        artifacts=[
            ArtifactRef(
                path=str(path),
                source_type=source_type,
                title=path.stem,
                media_type=("text/markdown" if path.suffix.casefold() == ".md" else "text/plain"),
            )
            for path in artifact_paths
        ],
        project_hint=project,
        authority=AuthorityContext(
            kind=AuthorityKind.USER,
            actor=actor,
            can_approve_authoritative=False,
            can_auto_apply_low_risk=True,
        ),
    )
    try:
        results = KnowledgeIntakeService(workspace).process_batch(request)
    except (
        IngestionError,
        KnowledgeIntakeError,
        ProjectPackError,
        OSError,
        ValueError,
    ) as error:
        _fatal(str(error), code=1)
    if json_output:
        typer.echo(
            json.dumps(
                [item.model_dump(mode="json", exclude_none=True) for item in results],
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        for result in results:
            typer.echo(f"{result.status.upper()} {result.resolution_reason}")
            if result.source is not None:
                typer.echo(f"  Source: {result.source.ref.qualified}")
            if result.proposal_id is not None:
                typer.echo(f"  Proposal: {result.proposal_id}")
            if result.intake_run_path is not None:
                typer.echo(f"  IntakeRun: {result.intake_run_path}")
    if any(result.status == "hold" for result in results):
        raise typer.Exit(code=1)


@roadmap_app.command("diff")
def roadmap_diff_command(
    desired: Annotated[Path, typer.Argument()],
    current: Annotated[Path, typer.Option("--current")] = Path("plans/amplai-master-roadmap.yaml"),
    created_by: Annotated[str, typer.Option("--created-by")] = "user",
) -> None:
    try:
        current_roadmap = RoadmapRepository(current).load()
        desired_roadmap = RoadmapRepository(desired).load()
        proposal = RoadmapService().propose_change(
            current_roadmap,
            desired_roadmap,
            created_at=datetime.now(ZoneInfo("Asia/Seoul")),
            created_by=created_by,
        )
    except (RoadmapRepositoryError, ValueError) as error:
        _fatal(str(error), code=1)
    typer.echo(proposal.model_dump_json(indent=2))


@roadmap_app.command("show")
def roadmap_show_command(
    proposal_id: Annotated[str, typer.Argument()],
    proposal_root: Annotated[
        Path,
        typer.Option("--proposal-root"),
    ] = Path(".amplai/intake/roadmap-proposals"),
) -> None:
    try:
        proposal = RoadmapProposalRepository(proposal_root).get(proposal_id)
    except RoadmapProposalRepositoryError as error:
        _fatal(str(error), code=1)
    if proposal is None:
        _fatal(f"Roadmap Proposal ID가 존재하지 않습니다: {proposal_id}", code=1)
    typer.echo(proposal.model_dump_json(indent=2))


@roadmap_app.command("approve")
def roadmap_approve_command(
    proposal_id: Annotated[str, typer.Argument()],
    approved_by: Annotated[str, typer.Option("--approved-by")],
    proposal_root: Annotated[
        Path,
        typer.Option("--proposal-root"),
    ] = Path(".amplai/intake/roadmap-proposals"),
) -> None:
    repository = RoadmapProposalRepository(proposal_root)
    try:
        proposal = repository.get(proposal_id)
        if proposal is None:
            _fatal(f"Roadmap Proposal ID가 존재하지 않습니다: {proposal_id}", code=1)
        approved = RoadmapService.approve(
            proposal,
            approved_by=approved_by,
            approved_at=datetime.now(ZoneInfo("Asia/Seoul")),
        )
        repository.save(approved)
    except (RoadmapProposalRepositoryError, ValueError) as error:
        _fatal(str(error), code=1)
    typer.echo(f"APPROVED {proposal_id} by {approved_by}")


@roadmap_app.command("apply")
def roadmap_apply_command(
    proposal_id: Annotated[str, typer.Argument()],
    roadmap_path: Annotated[
        Path,
        typer.Option("--roadmap"),
    ] = Path("plans/amplai-master-roadmap.yaml"),
    proposal_root: Annotated[
        Path,
        typer.Option("--proposal-root"),
    ] = Path(".amplai/intake/roadmap-proposals"),
) -> None:
    proposals = RoadmapProposalRepository(proposal_root)
    try:
        proposal = proposals.get(proposal_id)
        if proposal is None:
            _fatal(f"Roadmap Proposal ID가 존재하지 않습니다: {proposal_id}", code=1)
        updated = RoadmapService().apply(
            RoadmapRepository(roadmap_path),
            proposal,
            applied_on=datetime.now(ZoneInfo("Asia/Seoul")).date(),
            proposal_repository=proposals,
        )
    except (
        RoadmapProposalRepositoryError,
        RoadmapRepositoryError,
        ValueError,
    ) as error:
        _fatal(str(error), code=1)
    typer.echo(f"APPLIED {proposal_id} roadmap_version={updated.version}")


@roadmap_app.command("next")
def roadmap_next_command(
    path: Annotated[Path, typer.Option("--path")] = Path("plans/amplai-master-roadmap.yaml"),
) -> None:
    try:
        roadmap = RoadmapRepository(path).load()
    except RoadmapRepositoryError as error:
        _fatal(str(error), code=1)
    phase = RoadmapService.next_phase(roadmap)
    if phase is None:
        typer.echo("NONE")
        return
    typer.echo(phase.model_dump_json(indent=2))


@app.command("verify")
def verify_command(
    root: Annotated[Path, typer.Option("--root")] = Path("."),
) -> None:
    report = VerificationRunner().run(root.resolve())
    for step in report.steps:
        typer.echo(f"{'PASS' if step.passed else 'FAIL'} {step.name}")
        if not step.passed:
            typer.echo(step.output, err=True)
    if not report.passed:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
