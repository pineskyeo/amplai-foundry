"""Command-line interface for source intake and knowledge curation."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Annotated, Never
from zoneinfo import ZoneInfo

import typer
from pydantic import SecretStr

from amplai_foundry.curation.context_builder import ContextBuilderError, CurateContextBuilder
from amplai_foundry.domain.models import MemoryObject
from amplai_foundry.domain.project import ProjectPathError, validate_project_id
from amplai_foundry.governance import (
    AuthorityService,
    ChannelProvider,
    ChannelRef,
    DecisionService,
    ExternalActorIdentity,
    IngressDecisionWorker,
    IngressError,
    IngressService,
)
from amplai_foundry.governance.migrations import GovernanceMigrationError
from amplai_foundry.governance.store import GovernanceStore, GovernanceStoreError
from amplai_foundry.ingestion.service import IngestionError, SourceIngestionService
from amplai_foundry.intake.models import ArtifactRef, IntentRequest
from amplai_foundry.intake.service import KnowledgeIntakeError, KnowledgeIntakeService
from amplai_foundry.lint.engine import KnowledgeLinter, LintExecutionError
from amplai_foundry.parsing.markdown import parse_markdown_file
from amplai_foundry.projects.domain_registry import DomainRegistryError
from amplai_foundry.projects.repository import ProjectPackError, ProjectPackRepository
from amplai_foundry.projects.service import ProjectPackService
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
curate_app = typer.Typer(help="Prepare agent-neutral curation context bundles.")
schema_app = typer.Typer(help="Generate and check Pydantic-derived JSON Schemas.")
project_app = typer.Typer(help="Inspect, validate, rebuild, and archive Project Packs.")
intake_app = typer.Typer(help="Process intent and artifacts through governed knowledge intake.")
roadmap_app = typer.Typer(help="Diff and inspect versioned roadmap plans.")
governance_app = typer.Typer(
    help=(
        "Read-only governance queries for operator recovery. "
        "These issue SELECT statements only — no write, no lease, no transaction that "
        "contends with a running worker."
    )
)
control_plane_app = typer.Typer(help="Operate the AMPLAI Platform 0.4 Control Plane.")
app.add_typer(source_app, name="source")
app.add_typer(proposal_app, name="proposal")
app.add_typer(curate_app, name="curate")
app.add_typer(schema_app, name="schema")
app.add_typer(project_app, name="project")
app.add_typer(intake_app, name="intake")
app.add_typer(roadmap_app, name="roadmap")
app.add_typer(governance_app, name="governance")
app.add_typer(control_plane_app, name="control-plane")
# V3 shares the original Foundry governance instead of replacing it.
from amplai_foundry.runtime.cli import ops as v3_ops
app.add_typer(v3_ops, name="v3")


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
    del proposal_id, approved_by, proposal_root
    _fatal(
        "DIRECT_MUTATION_DISABLED Proposal decision은 governed DecisionService를 사용해야 합니다.",
        code=1,
    )


@proposal_app.command("apply")
def proposal_apply_command(
    proposal_id: Annotated[str, typer.Argument()],
    vault: Annotated[Path, typer.Option("--vault")] = Path("vault"),
    proposal_root: Annotated[Path, typer.Option("--proposal-root")] = Path(".amplai/proposals"),
) -> None:
    del proposal_id, vault, proposal_root
    _fatal("APPLY_ACTION_DEFERRED ApplyGrant는 MGC-009에서 활성화됩니다.", code=1)


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
    workspace: Annotated[Path, typer.Option("--workspace")] = Path("."),
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    request_id = f"CMD-{secrets.token_hex(8).upper()}"
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
        identity=ExternalActorIdentity(
            provider=ChannelProvider.CLI,
            provider_installation_ref="local:cli",
            external_actor_id=f"uid:{os.getuid()}",
            request_id=request_id,
            channel=ChannelRef(
                provider=ChannelProvider.CLI,
                message_id=request_id,
            ),
        ),
    )
    try:
        governance_store = GovernanceStore(workspace / ".amplai/runtime/governance.db")
        governance_store.check_startup()
        results = KnowledgeIntakeService(
            workspace,
            AuthorityService(governance_store),
        ).process_batch(request)
    except (
        IngestionError,
        KnowledgeIntakeError,
        ProjectPackError,
        GovernanceStoreError,
        GovernanceMigrationError,
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
    del proposal_id, approved_by, proposal_root
    _fatal(
        "DIRECT_MUTATION_DISABLED Roadmap decision은 governed DecisionService를 사용해야 합니다.",
        code=1,
    )


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
    del proposal_id, roadmap_path, proposal_root
    _fatal("APPLY_ACTION_DEFERRED ApplyGrant는 MGC-009에서 활성화됩니다.", code=1)


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


# ---------------------------------------------------------------------------
# governance — 조회 전용 (MGC-012-P5-T026, D-044)
# ---------------------------------------------------------------------------
#
# `D-042` 가 침묵을 고른 근거 전체가 "침묵은 `stranded()` 로 회수할 수 있다" 이고
# `contracts/interaction-feedback.md` 가 그것을 "operator's entry point" 로 적었다.
# **그 수단이 실재하지 않았다** — 호출자가 test 와 docstring 뿐이었다. 근거 없는 주장을
# 계약에 남기지 않는다.
#
# 조회 대상이 둘인 이유는, 침묵 종점을 만난 operator 가 알아야 하는 것이 정확히
# "결정이 실제로 났는가" 이기 때문이다. `committed_decision()` 이 그 답을 갖고 있다.
#
# **조회만이다.** `recovery_hold` 나 `dead_letter` 에서 빼내는 governed mutation 은
# `D-044` 가 명시적으로 범위 밖에 두었다.


def _governance_worker(workspace: Path) -> IngressDecisionWorker:
    """읽기 두 개를 위한 최소 조립.

    `IngressService` 는 `ProviderAuthenticator` 를 요구하지만 `stranded()` 와
    `committed_decision()` 은 그것을 부르지 않는다. 그래도 `None` 을 넣지 않고 부르면
    실패하는 authenticator 를 넣는다 — 이 CLI 로 command 를 받아들일 길이 없음을 형으로
    못박는다.
    """

    class _ReadOnlyAuthenticator:
        def verify(self, envelope: object) -> Never:
            raise IngressError("CLI_IS_READ_ONLY")

    path = workspace / ".amplai/runtime/governance.db"
    # **`check_startup()` 을 부르지 않는다** (round 15 `C-1`, `F-3`, `F-4`).
    #
    # 그것은 `PRAGMA integrity_check`(전체 스캔), migration verify, 그리고
    # `_probe_wal_write` 의 `BEGIN IMMEDIATE` + `UPDATE` 를 한다. 조회 둘에는 필요 없고
    # 셋 다 해롭다.
    #
    # - 쓰기 probe 가 배타 lock 을 잡아 worker 가 write transaction 을 쥔 동안 이 CLI 가
    #   실패했다. **회수 도구가 회수해야 할 바로 그 순간에 안 됐다.**
    # - "조회만" 이라고 적어놓고 매 호출이 `UPDATE` 를 냈다.
    # - `check_startup()` 은 `connect()` **밖에서** filesystem 을 검증하므로 거기서 나는
    #   `GovernanceFilesystemError` 가 아래 `except` 를 빠져나가 raw traceback 이 됐다.
    #
    # 존재 확인만 손으로 한다. `connect()` 는 없는 파일을 sqlite 가 만들어 버리므로 그냥
    # 열면 빈 store 에 대해 `NONE` 을 내어 operator 를 속인다. `connect()` 안의 filesystem
    # 검증과 정규화는 그대로 쓴다 (`T024`).
    #
    # 잃는 것은 schema version 검증이다. 두 조회는 `governance_ingress_commands` 와
    # `governance_decision_results` 만 읽고, schema 가 낡거나 이 파일이 governance store 가
    # 아니면 `sqlite3.OperationalError`(`no such table`)로 실패한다 — 조용히 틀린 답을 내지
    # 않는다.
    #
    # **그 예외는 두 command 의 except tuple 이 잡는다** (round 16 `R16-1`, `FR-3`).
    # 처음에는 안 잡아서 raw traceback 이 났다. store 상태 일곱을 전수로 재서 read-only,
    # governance schema 아님, 빈 파일, 손상 파일 넷이 `sqlite3.Error` 계열임을 확인하고
    # 그 하나를 tuple 에 더했다.
    #
    # **정규화 위치가 요점이다.** `connect()` 에 넣으면 `legacy_*.py` 의 `try` 열이
    # `sqlite3.Error` 봉쇄를 잃는다 (round 15 `R-1`). CLI 는 최종 소비자라 아무도 그 아래에서
    # class 를 구분하지 않는다.
    if not path.is_file():
        _fatal(f"Governance Store가 존재하지 않습니다: {path}", code=1)
    store = GovernanceStore(path)
    ingress = IngressService(store, _ReadOnlyAuthenticator())
    return IngressDecisionWorker(
        store,
        ingress,
        DecisionService(store, AuthorityService(store)),
    )


@governance_app.command("stranded")
def governance_stranded_command(
    workspace: Annotated[Path, typer.Option("--workspace")] = Path("."),
    limit: Annotated[int, typer.Option("--limit")] = 100,
) -> None:
    """List the commands no worker will claim again.

    `--json` 은 없다. `MGC-012-P5-T026` 의 `scope.exclude` 가 "새 출력 형식(JSON 등)의
    계약화. 사람이 읽는 목록이면 충분하다" 로 배제했고 그 배제를 뒤집는 Decision 이 없다
    (round 15 `C-3`).
    """
    # **`limit + 1` 을 요청하기 전에 여기서 막는다** (`D-050`, round 20 `F20-1`).
    # 아래가 `limit + 1` 을 넘기므로 `--limit 0` 은 service 의 `limit < 1` guard 에
    # `1` 로 도착해 **우회된다.** 그 guard 가 지금까지 `--limit 0` 을 잡고 있었다
    # (round 18 `A18-5`, round 20 `A20-F3`). 같은 문구로 여기서 잡는다.
    if limit < 1:
        _fatal("limit은 1 이상이어야 합니다.", code=1)
    try:
        ingress = _governance_worker(workspace).ingress
        # **한 개 더 요청해 잘림을 판정한다** (`D-050`). `limit` 개만 왔으면 그것이 전부이고,
        # `limit + 1` 개가 왔으면 더 있다. service signature 를 안 바꾸므로
        # `D-047`·`D-048`·`D-049` 가 공통으로 건 불변이 유지된다.
        commands = ingress.stranded(limit=limit + 1)
        unreadable = ingress.unreadable(limit=limit + 1)
    except (
        GovernanceStoreError,
        GovernanceMigrationError,
        IngressError,
        ValueError,
        sqlite3.Error,
    ) as error:
        _fatal(str(error), code=1)
    # **두 목록을 각각 판정한다.** 하나만 잘려도 알려야 한다 — 둘은 다른 조회이고
    # 후보 집합도 다르다 (round 20 `F20-2`).
    truncated = len(commands) > limit or len(unreadable) > limit
    commands = commands[:limit]
    unreadable = unreadable[:limit]
    for command in commands:
        typer.echo(
            f"{command.command_id}  {command.state.value}  attempts={command.attempts}  "
            f"{command.last_error_code or '-'}"
        )
    # 읽을 수 없는 row 는 view 로 만들 수 없어 위 목록에 못 들어간다. 그렇다고 조용히
    # 빠뜨리면 목록이 완전하다고 오해한다 (round 15 `F-2`).
    for command_id in unreadable:
        typer.echo(f"{command_id}  UNREADABLE  이 row 는 읽을 수 없다")
    if not commands and not unreadable:
        typer.echo("NONE")
    # **잘렸으면 말한다** (`D-050`). `limit` 이 있는 한 어떤 규칙도 완전할 수 없다 —
    # 무엇을 미느냐가 방식마다 다를 뿐이다 (round 19 `F19-1`, round 20 `F20-1`).
    # **그것을 없애는 대신 보이게 만든다.** 이 줄이 없으면 operator 가 목록이 잘린 것을
    # 알 방법이 없다.
    #
    # **두 조회 다 python 출력 상한이다** (`D-057`). `stranded()` 가 `SQL LIMIT` 이던
    # 동안에는 위 판정이 거짓 음성을 냈다 — 가져온 개수와 반환 개수가 달랐다
    # (round 21 `F21-1`). 이제 반환 개수가 곧 발견 개수라 `len(...) > limit` 이 참이다.
    if truncated:
        typer.echo(f"...  목록이 --limit {limit} 에서 잘렸다. 더 있다")


@governance_app.command("decision")
def governance_decision_command(
    command_id: Annotated[str, typer.Argument(help="Ingress command ID.")],
    workspace: Annotated[Path, typer.Option("--workspace")] = Path("."),
) -> None:
    """Answer whether a stranded command already committed its decision."""
    try:
        worker = _governance_worker(workspace)
        decision = worker.committed_decision(command_id)
    except (
        GovernanceStoreError,
        GovernanceMigrationError,
        IngressError,
        ValueError,
        sqlite3.Error,
    ) as error:
        _fatal(str(error), code=1)
    if decision is None:
        # **"결정 없음" 과 "알 수 없음" 을 구분한다** (round 16 `C16-1`).
        #
        # 계약(`contracts/interaction-feedback.md:47-54`)이 "*we know a decision landed*" 와
        # "*we cannot tell*" 을 나누고 `D-042` 는 후자를 침묵의 근거로 쓴다. 회수 도구가
        # 그 둘을 하나로 뭉개면 읽을 수 없는 row 에 거짓 음성을 낸다 — `governance stranded`
        # 가 방금 `UNREADABLE` 로 표시한 바로 그 id 에 대해서.
        #
        # 판정은 `IngressService` 가 한다. 여기서 다시 판정하면 그것이 다음 형제가 된다.
        try:
            unreadable = worker.ingress.is_unreadable(command_id)
        except (
            GovernanceStoreError,
            GovernanceMigrationError,
            IngressError,
            ValueError,
            sqlite3.Error,
        ) as error:
            _fatal(str(error), code=1)
        if unreadable:
            typer.echo("UNKNOWN  이 command 는 읽을 수 없다. 결정 여부를 알 수 없다")
            return
        # `token_id` 도 fingerprint 도 내보내지 않는다.
        typer.echo("NONE")
        return
    typer.echo(
        f"{decision.proposal_ref.proposal_id}  {decision.action.value}  "
        f"{decision.proposal_status.value}  state_revision={decision.state_revision}"
    )


@control_plane_app.command("init")
def control_plane_init_command(
    db: Annotated[Path, typer.Option("--db")] = Path(".amplai/control-plane.db"),
) -> None:
    """Initialize the independent Platform 0.4 Control Plane store."""
    from amplai_foundry.control_plane import ControlPlaneStore

    store = ControlPlaneStore(db)
    store.initialize()
    typer.echo(str(store.path))


@control_plane_app.command("token-issue")
def control_plane_token_issue_command(
    tenant: Annotated[str, typer.Option("--tenant")],
    project: Annotated[str, typer.Option("--project")],
    permission: Annotated[list[str], typer.Option("--permission")],
    db: Annotated[Path, typer.Option("--db")] = Path(".amplai/control-plane.db"),
) -> None:
    """Issue one project-scoped bearer token. The raw token is printed once."""
    from amplai_foundry.control_plane import ApiTokenService, ControlPlaneStore

    store = ControlPlaneStore(db)
    store.initialize()
    issued = ApiTokenService(store).issue(
        tenant_id=tenant, project_id=project, permissions=set(permission)
    )
    typer.echo(json.dumps(asdict(issued), ensure_ascii=False, indent=2))


@control_plane_app.command("projection-rebuild")
def control_plane_projection_rebuild_command(
    tenant: Annotated[str, typer.Option("--tenant")],
    project: Annotated[str, typer.Option("--project")],
    db: Annotated[Path, typer.Option("--db")] = Path(".amplai/control-plane.db"),
) -> None:
    """Rebuild the project summary projection from the append-only event log."""
    from amplai_foundry.control_plane import ControlPlaneStore, ProjectionService

    store = ControlPlaneStore(db)
    store.initialize()
    state = ProjectionService(store).rebuild(tenant_id=tenant, project_id=project)
    typer.echo(json.dumps(state, ensure_ascii=False, indent=2))


@control_plane_app.command("serve")
def control_plane_serve_command(
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 8765,
    db: Annotated[Path, typer.Option("--db")] = Path(".amplai/control-plane.db"),
    project_store: Annotated[
        Path | None,
        typer.Option("--project-store", help="Authoritative AMPLAI Project Store for Work reads"),
    ] = None,
) -> None:
    """Run the dependency-free WSGI API for local/on-prem deployments."""
    from wsgiref.simple_server import make_server

    from amplai_foundry.control_plane import (
        ApiTokenService,
        ControlPlaneService,
        ControlPlaneStore,
        ControlPlaneWSGIApp,
    )

    store = ControlPlaneStore(db)
    store.initialize()
    work_reader = None
    if project_store is not None:
        # CLI composition is the host edge.  The control-plane domain receives
        # only its read protocol and never imports Loop Kit runtime itself.
        scripts_dir = Path(__file__).resolve().parents[2] / "scripts"
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        from amplai_orchestration_bridge import (  # type: ignore[import-not-found]
            ProjectStoreWorkReader,
        )
        from amplai_runtime import ProjectStore  # type: ignore[import-not-found]

        work_reader = ProjectStoreWorkReader(ProjectStore(str(project_store)))
    service = ControlPlaneService(store, ApiTokenService(store), work_reader=work_reader)
    typer.echo(f"AMPLAI Control Plane listening on http://{host}:{port}")
    with make_server(host, port, ControlPlaneWSGIApp(service)) as server:
        server.serve_forever()


@control_plane_app.command("orchestration-bridge-once")
def control_plane_orchestration_bridge_once_command(
    project_store: Annotated[
        Path, typer.Option("--project-store", help="Authoritative AMPLAI Project Store")
    ],
    db: Annotated[Path, typer.Option("--db")] = Path(".amplai/control-plane.db"),
    source_app: Annotated[str, typer.Option("--source-app")] = "amplai-foundry",
    contract_ref: Annotated[str, typer.Option("--contract-ref")] = "hermes-slack-orchestration@1",
    activation_card_outbox: Annotated[Path | None, typer.Option("--activation-card-outbox")] = None,
    activation_governance_db: Annotated[
        Path | None, typer.Option("--activation-governance-db")
    ] = None,
    activation_project_id: Annotated[str | None, typer.Option("--activation-project-id")] = None,
    activation_project_namespace: Annotated[
        str | None, typer.Option("--activation-project-namespace")
    ] = None,
    activation_workspace_id: Annotated[
        str | None, typer.Option("--activation-workspace-id")
    ] = None,
    activation_app_id: Annotated[str | None, typer.Option("--activation-app-id")] = None,
    activation_recipient_external_actor_id: Annotated[
        str | None, typer.Option("--activation-recipient-external-actor-id")
    ] = None,
    activation_feature: Annotated[
        str, typer.Option("--activation-feature")
    ] = "hermes-slack-orchestration",
) -> None:
    """Consume one lease-owned request bridge event into its DRAFT Work graph."""
    from amplai_foundry.control_plane import (
        ControlPlaneStore,
        OrchestrationBridge,
        OrchestrationBridgeConnector,
        OutboxDispatcher,
        OutboxQueue,
    )
    from amplai_foundry.control_plane.connectors import ConnectorRegistry

    scripts_dir = Path(__file__).resolve().parents[2] / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    from amplai_orchestration_bridge import (
        ProjectStoreActivationCardScheduler,
        ProjectStoreOrchestrationGateway,
    )
    from amplai_runtime import ProjectStore

    store = ControlPlaneStore(db)
    store.initialize()
    gateway = ProjectStoreOrchestrationGateway(
        ProjectStore(str(project_store)), source_app=source_app, contract_ref=contract_ref
    )
    scheduler = None
    activation_values = (
        activation_card_outbox,
        activation_governance_db,
        activation_project_id,
        activation_project_namespace,
        activation_workspace_id,
        activation_app_id,
        activation_recipient_external_actor_id,
    )
    if any(value is not None for value in activation_values):
        if any(value is None for value in activation_values):
            raise typer.BadParameter("Activation Card 설정은 모두 함께 지정해야 합니다.")
        from amplai_foundry.domain.identity import ProjectRef
        from amplai_foundry.governance import WorkActivationCardOutbox

        assert activation_governance_db is not None
        assert activation_card_outbox is not None
        assert activation_project_id is not None
        assert activation_project_namespace is not None
        assert activation_workspace_id is not None
        assert activation_app_id is not None
        assert activation_recipient_external_actor_id is not None
        governance_store = GovernanceStore(activation_governance_db)
        governance_store.initialize()
        scheduler = ProjectStoreActivationCardScheduler(
            ProjectStore(str(project_store)),
            card_outbox=WorkActivationCardOutbox(activation_card_outbox),
            authority_service=AuthorityService(governance_store),
            project_ref=ProjectRef(
                namespace=activation_project_namespace, project_id=activation_project_id
            ),
            provider_installation_ref=f"{activation_workspace_id}:{activation_app_id}",
            recipient_external_actor_id=activation_recipient_external_actor_id,
            feature=activation_feature,
        )
    connectors = ConnectorRegistry()
    connectors.register(
        OrchestrationBridgeConnector.destination,
        OrchestrationBridgeConnector(OrchestrationBridge(store, gateway), card_scheduler=scheduler),
    )
    item = OutboxDispatcher(OutboxQueue(store), connectors).run_once(
        destination=OrchestrationBridgeConnector.destination
    )
    typer.echo(
        "idle" if item is None else json.dumps({"outbox_id": item.outbox_id, "status": item.status})
    )


@control_plane_app.command("deliver-work-activation-card-once")
def control_plane_deliver_work_activation_card_once_command(
    project_store: Annotated[Path, typer.Option("--project-store")],
    activation_card_outbox: Annotated[Path, typer.Option("--activation-card-outbox")],
    activation_ledger: Annotated[Path, typer.Option("--activation-ledger")],
    slack_app_id: Annotated[str, typer.Option("--slack-app-id")],
    project_id: Annotated[str, typer.Option("--project-id")],
) -> None:
    """Deliver one queued Activation Card through the configured AMPLAI Slack App."""
    from amplai_foundry.governance import (
        DurableWorkActivationLedger,
        WorkActivationCardOutbox,
    )
    from amplai_foundry.governance.slack_http import HttpSlackTransport, load_slack_credentials

    credentials = load_slack_credentials()
    if credentials is None:
        _fatal("Slack credentials are required to deliver an Activation Card.", code=1)
    assert not isinstance(credentials, SecretStr)
    scripts_dir = Path(__file__).resolve().parents[2] / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    from amplai_orchestration_bridge import ProjectStoreWorkActivationGateway
    from amplai_runtime import ProjectStore

    delivered = WorkActivationCardOutbox(activation_card_outbox).deliver_next(
        gateway=ProjectStoreWorkActivationGateway(
            ProjectStore(str(project_store)), project_id=project_id
        ),
        ledger=DurableWorkActivationLedger(activation_ledger),
        transport=HttpSlackTransport(
            bot_token=credentials.bot_token,
            timeout_seconds=10,
            max_history_pages=1,
            lease_seconds=60,
        ),
        app_id=slack_app_id,
    )
    typer.echo("idle" if delivered is None else json.dumps({"work_id": delivered.snapshot.work_id}))


@control_plane_app.command("knowledge-intake-once")
def control_plane_knowledge_intake_once_command(
    knowledge_project: Annotated[str, typer.Option("--knowledge-project")] = "amplai",
    vault: Annotated[Path, typer.Option("--vault")] = Path("vault"),
    workspace: Annotated[Path, typer.Option("--workspace")] = Path("."),
    db: Annotated[Path, typer.Option("--db")] = Path(".amplai/control-plane.db"),
) -> None:
    """Consume one explicit knowledge-intake job through Source and curation prepare."""
    from amplai_foundry.control_plane import (
        ContextWorker,
        ControlPlaneStore,
        GovernedKnowledgeIntakeHandler,
        JobQueue,
    )

    store = ControlPlaneStore(db)
    store.initialize()
    handler = GovernedKnowledgeIntakeHandler(
        vault=vault,
        knowledge_project=knowledge_project,
        workspace=workspace,
        repository_root=workspace,
    )
    job = ContextWorker(JobQueue(store), handler, worker_id="knowledge-intake-worker").run_once(
        kind="knowledge.intake"
    )
    typer.echo("idle" if job is None else json.dumps({"job_id": job.job_id, "status": job.status}))


@control_plane_app.command("serve-slack-work-activation")
def control_plane_serve_slack_work_activation_command(
    project_store: Annotated[Path, typer.Option("--project-store")],
    governance_db: Annotated[Path, typer.Option("--governance-db")],
    activation_ledger: Annotated[Path, typer.Option("--activation-ledger")],
    project_id: Annotated[str, typer.Option("--project-id")],
    project_namespace: Annotated[str, typer.Option("--project-namespace")],
    workspace_id: Annotated[str, typer.Option("--workspace-id")],
    app_id: Annotated[str, typer.Option("--app-id")],
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 8766,
    feature: Annotated[str, typer.Option("--feature")] = "hermes-slack-orchestration",
) -> None:
    """Run the separately configured signed AMPLAI Slack Work-action endpoint."""
    from wsgiref.simple_server import make_server

    from amplai_foundry.domain.identity import ProjectRef
    from amplai_foundry.governance import (
        AuthorityServiceSlackResolver,
        DurableWorkActivationLedger,
        SlackBlockActionAuthenticator,
        SlackInstallationPolicy,
        SlackWorkActivationAuthenticator,
        SlackWorkActivationIngress,
        SlackWorkActivationWSGIApp,
        WorkActivationScope,
        WorkActivationService,
    )
    from amplai_foundry.governance.decisions import DecisionAction
    from amplai_foundry.governance.slack_http import load_slack_signing_secret

    try:
        signing_secret = load_slack_signing_secret()
    except ValueError as error:
        _fatal(str(error), code=1)
    scripts_dir = Path(__file__).resolve().parents[2] / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    from amplai_orchestration_bridge import ProjectStoreWorkActivationGateway
    from amplai_runtime import ProjectStore

    project_ref = ProjectRef(namespace=project_namespace, project_id=project_id)
    governance_store = GovernanceStore(governance_db)
    governance_store.initialize()
    ledger = DurableWorkActivationLedger(activation_ledger)
    scope = WorkActivationScope(project_ref, ChannelProvider.SLACK, feature)
    installation_ref = f"{workspace_id}:{app_id}"
    authenticator = SlackWorkActivationAuthenticator(
        SlackBlockActionAuthenticator(
            SlackInstallationPolicy(
                provider_installation_ref=installation_ref,
                signing_secret=signing_secret,
                api_app_id=app_id,
                workspace_ids=frozenset({workspace_id}),
                action_ids={
                    "amplai_work_approve": DecisionAction.APPROVE,
                    "amplai_work_reject": DecisionAction.REJECT,
                    "amplai_work_request_changes": DecisionAction.REQUEST_CHANGES,
                },
            )
        )
    )
    ingress = SlackWorkActivationIngress(
        authenticator,
        ledger,
        WorkActivationService(
            ProjectStoreWorkActivationGateway(
                ProjectStore(str(project_store)), project_id=project_id
            ),
            ledger,
            enabled_providers=frozenset({ChannelProvider.SLACK}),
            enabled_scopes=(scope,),
            ledger=ledger,
        ),
        AuthorityServiceSlackResolver(AuthorityService(governance_store)),
    )
    typer.echo(f"AMPLAI Slack Work activation listening on http://{host}:{port}")
    with make_server(
        host,
        port,
        SlackWorkActivationWSGIApp(ingress, provider_installation_ref=installation_ref),
    ) as server:
        server.serve_forever()


if __name__ == "__main__":
    app()
