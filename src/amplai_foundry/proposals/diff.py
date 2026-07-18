"""Deterministic unified diffs for Proposal drafts."""

from difflib import unified_diff
from pathlib import Path

from amplai_foundry.domain.models import MemoryObject
from amplai_foundry.domain.project import project_root, require_contained
from amplai_foundry.ingestion.identifiers import safe_slug
from amplai_foundry.lint.rules.schema import KIND_DIRECTORIES
from amplai_foundry.parsing.markdown import parse_markdown_file
from amplai_foundry.proposals.models import OperationType, Proposal
from amplai_foundry.repositories.markdown import MarkdownMemoryRepository

DIRECTORY_BY_KIND = {kind: directory for directory, kind in KIND_DIRECTORIES.items()}


def destination_for_create(vault: Path, draft: MemoryObject) -> Path:
    directory = DIRECTORY_BY_KIND[draft.kind]
    filename = f"{draft.id}-{safe_slug(draft.title)}.md"
    root = project_root(vault, draft.project)
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
        draft_path = Path(operation.draft_path)
        if not draft_path.is_absolute():
            draft_path = Path.cwd() / draft_path
        draft_text = draft_path.read_text(encoding="utf-8")
        if operation.type is OperationType.CREATE:
            document = parse_markdown_file(draft_path)
            draft = MemoryObject.model_validate({**document.metadata, "content": document.content})
            destination = destination_for_create(vault, draft)
            before = ""
        else:
            destination = Path(repository.path_for(operation.target_id or ""))
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
