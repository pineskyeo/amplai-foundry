"""Immutable Source integrity and duplicate rules."""

import re
from collections import defaultdict
from pathlib import Path

from amplai_foundry.domain.enums import MemoryKind
from amplai_foundry.domain.models import MemoryObject
from amplai_foundry.ingestion.hashing import content_sha256, normalized_sha256
from amplai_foundry.ingestion.service import IngestionError, extract_original_content
from amplai_foundry.lint.result import LintIssue, Severity

SOURCE_ID_PATTERN = re.compile(r"^SRC-[0-9]{8}-([A-F0-9]{8})$")


def validate_sources(
    records: list[tuple[Path, MemoryObject]],
    *,
    root: Path | None = None,
) -> list[LintIssue]:
    """Verify original bytes, source IDs, locations, and normalized duplicates."""
    issues: list[LintIssue] = []
    by_normalized_hash: dict[tuple[str, str], list[tuple[Path, MemoryObject]]] = defaultdict(list)
    for path, memory in records:
        try:
            contract_path = path.relative_to(root) if root is not None else path
        except ValueError:
            contract_path = path
        if memory.kind is not MemoryKind.SOURCE:
            continue
        metadata = memory.source_metadata
        if metadata is None:  # Pydantic emits the primary schema error.
            continue
        try:
            original = extract_original_content(path)
        except IngestionError as error:
            issues.append(
                LintIssue(Severity.ERROR, "SOURCE_ORIGINAL_MISSING", str(error), path, memory.id)
            )
            continue
        exact = content_sha256(original)
        normalized = normalized_sha256(original)
        if exact != metadata.content_sha256:
            issues.append(
                LintIssue(
                    Severity.ERROR,
                    "SOURCE_CONTENT_HASH",
                    "Source 원문 SHA-256이 source_metadata와 다릅니다.",
                    path,
                    memory.id,
                )
            )
        if normalized != metadata.normalized_sha256:
            issues.append(
                LintIssue(
                    Severity.ERROR,
                    "SOURCE_NORMALIZED_HASH",
                    "Source 정규화 SHA-256이 source_metadata와 다릅니다.",
                    path,
                    memory.id,
                )
            )
        match = SOURCE_ID_PATTERN.fullmatch(memory.id)
        if match is None:
            issues.append(
                LintIssue(
                    Severity.ERROR,
                    "SOURCE_ID_FORMAT",
                    "Source ID는 SRC-YYYYMMDD-<SHA256 8자리> 형식이어야 합니다.",
                    path,
                    memory.id,
                )
            )
        elif match.group(1) != exact[:8].upper():
            issues.append(
                LintIssue(
                    Severity.ERROR,
                    "SOURCE_ID_HASH_SUFFIX",
                    "Source ID hash suffix가 실제 원문 hash와 다릅니다.",
                    path,
                    memory.id,
                )
            )
        by_normalized_hash[(memory.namespace, metadata.normalized_sha256)].append((path, memory))

        project_indexes = [
            index for index, part in enumerate(contract_path.parts) if part == "projects"
        ]
        if project_indexes:
            project_index = project_indexes[-1] + 1
            if (
                project_index >= len(contract_path.parts)
                or contract_path.parts[project_index] != memory.project
            ):
                issues.append(
                    LintIssue(
                        Severity.ERROR,
                        "SOURCE_PROJECT_PATH",
                        "Source project와 vault/projects/{project} 경로가 다릅니다.",
                        path,
                        memory.id,
                    )
                )
            if (
                project_index + 1 >= len(contract_path.parts)
                or contract_path.parts[project_index + 1] != "00-sources"
            ):
                issues.append(
                    LintIssue(
                        Severity.ERROR,
                        "SOURCE_DIRECTORY",
                        "Source는 project의 00-sources directory에 있어야 합니다.",
                        path,
                        memory.id,
                    )
                )

    for items in by_normalized_hash.values():
        if len(items) > 1:
            identifiers = ", ".join(memory.id for _, memory in items)
            for path, memory in items:
                issues.append(
                    LintIssue(
                        Severity.ERROR,
                        "SOURCE_NORMALIZED_DUPLICATE",
                        f"동일 normalized hash Source가 중복됩니다: {identifiers}",
                        path,
                        memory.id,
                    )
                )
    return issues
