"""Deterministic unified diffs for Proposal drafts."""

from difflib import unified_diff
from pathlib import Path

from amplai_foundry.domain.models import MemoryObject
from amplai_foundry.domain.project import project_root, require_contained
from amplai_foundry.ingestion.identifiers import safe_slug
from amplai_foundry.lint.rules.schema import KIND_DIRECTORIES
from amplai_foundry.parsing.markdown import parse_markdown_file
from amplai_foundry.proposals.codes import (
    PROPOSAL_DRAFT_NAMESPACE_MISMATCH,
    PROPOSAL_DRAFT_PROJECT_MISMATCH,
)
from amplai_foundry.proposals.models import OperationType, Proposal
from amplai_foundry.proposals.paths import resolve_draft_path
from amplai_foundry.repositories.markdown import MarkdownMemoryRepository

DIRECTORY_BY_KIND = {kind: directory for directory, kind in KIND_DIRECTORIES.items()}


def destination_for_create(
    vault: Path,
    draft: MemoryObject,
    *,
    project: str,
    namespace: str,
) -> Path:
    """Return a CREATE destination inside the validated Proposal project."""
    if draft.project != project:
        raise ValueError(
            f"{PROPOSAL_DRAFT_PROJECT_MISMATCH} "
            f"draft project={draft.project}와 Proposal project={project}가 다릅니다."
        )
    if draft.namespace != namespace:
        raise ValueError(
            f"{PROPOSAL_DRAFT_NAMESPACE_MISMATCH} "
            f"draft namespace={draft.namespace}와 Proposal namespace={namespace}가 다릅니다."
        )
    directory = DIRECTORY_BY_KIND[draft.kind]
    filename = f"{draft.id}-{safe_slug(draft.title)}.md"
    nested_root = vault / "projects" / project
    root = project_root(vault, project) if nested_root.is_dir() else vault.resolve()
    return require_contained(root / directory / filename, root, label="Proposal destination")


def proposal_diff(proposal: Proposal, proposal_path: Path, vault: Path) -> str:
    """Render the file changes represented by applicable Proposal drafts."""
    repository = MarkdownMemoryRepository(vault)
    chunks: list[str] = []
    for operation in proposal.operations:
        if operation.type is OperationType.CONFLICT:
            chunks.append(f"# {operation.operation_id} CONFLICT: {operation.reason}\n")
            continue
        if not operation.draft_path:
            continue
        draft_path = resolve_draft_path(operation.draft_path, proposal_path)
        draft_text = draft_path.read_text(encoding="utf-8")
        if operation.type is OperationType.CREATE:
            document = parse_markdown_file(draft_path)
            draft = MemoryObject.model_validate({**document.metadata, "content": document.content})
            destination = destination_for_create(
                vault,
                draft,
                project=proposal.project,
                namespace=proposal.namespace,
            )
            before = ""
        else:
            destination = Path(
                repository.path_for(
                    operation.target_id or "",
                    namespace=proposal.namespace,
                )
            )
            before = destination.read_text(encoding="utf-8")
        chunks.extend(
            unified_diff(
                before.splitlines(keepends=True),
                draft_text.splitlines(keepends=True),
                fromfile=str(destination) if before else "/dev/null",
                tofile=str(destination),
            )
        )
    return "".join(chunks)
