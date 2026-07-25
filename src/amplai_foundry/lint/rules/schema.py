"""Front Matter schema and path contract rules."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from amplai_foundry.domain.enums import MemoryKind
from amplai_foundry.domain.models import MemoryObject
from amplai_foundry.lint.result import LintIssue, Severity
from amplai_foundry.parsing.markdown import ParsedMarkdown

KIND_DIRECTORIES: dict[str, MemoryKind] = {
    "00-sources": MemoryKind.SOURCE,
    "10-concepts": MemoryKind.CONCEPT,
    "20-principles": MemoryKind.PRINCIPLE,
    "30-decisions": MemoryKind.DECISION,
    "40-architecture": MemoryKind.ARCHITECTURE,
    "50-questions": MemoryKind.QUESTION,
    "60-experiments": MemoryKind.EXPERIMENT,
    "70-maps": MemoryKind.MAP,
}


def _schema_message(error: Mapping[str, Any]) -> tuple[str, str]:
    location = ".".join(str(part) for part in error["loc"])
    error_type = str(error["type"])
    if error_type == "missing":
        return "SCHEMA_REQUIRED_FIELD", f"필수 필드 '{location}'가 없습니다."
    if "kind=source에는 source_metadata가 필요" in str(error["msg"]):
        return "SOURCE_METADATA_REQUIRED", "kind=source에는 source_metadata가 필요합니다."
    if location == "kind" and error_type == "enum":
        return "SCHEMA_KIND", "허용되지 않은 kind입니다."
    if location == "status" and error_type == "enum":
        return "SCHEMA_STATUS", "허용되지 않은 status입니다."
    if location.endswith(".type") and error_type == "enum":
        return "SCHEMA_RELATION_TYPE", "허용되지 않은 relation type입니다."
    if location == "revision" and error_type == "greater_than_equal":
        return "SCHEMA_REVISION", "revision은 1 이상이어야 합니다."
    return "SCHEMA_INVALID", f"필드 '{location}'가 올바르지 않습니다: {error['msg']}"


def validate_document(
    path: Path, document: ParsedMarkdown
) -> tuple[MemoryObject | None, list[LintIssue]]:
    """Validate parsed metadata and path placement."""
    raw_id = str(document.metadata.get("id", "UNKNOWN"))
    try:
        memory = MemoryObject.model_validate({**document.metadata, "content": document.content})
    except ValidationError as error:
        issues: list[LintIssue] = []
        for detail in error.errors(include_url=False):
            code, message = _schema_message(detail)
            top_field = str(detail["loc"][0]) if detail["loc"] else ""
            issues.append(
                LintIssue(
                    Severity.ERROR,
                    code,
                    message,
                    path,
                    raw_id,
                    document.field_lines.get(top_field, 2),
                )
            )
        return None, issues

    issues = []
    expected_kind = next(
        (kind for directory, kind in KIND_DIRECTORIES.items() if directory in path.parts), None
    )
    if expected_kind is not None and memory.kind is not expected_kind:
        issues.append(
            LintIssue(
                Severity.ERROR,
                "SCHEMA_PATH_KIND",
                (
                    f"경로가 요구하는 kind={expected_kind.value}와 "
                    f"실제 kind={memory.kind.value}가 다릅니다."
                ),
                path,
                memory.id,
                document.field_lines.get("kind"),
            )
        )
    if "90-archive" in path.parts and memory.status.value != "archived":
        issues.append(
            LintIssue(
                Severity.ERROR,
                "SCHEMA_ARCHIVE_STATUS",
                "90-archive의 노트는 status=archived여야 합니다.",
                path,
                memory.id,
                document.field_lines.get("status"),
            )
        )
    if "projects" in path.parts:
        project_index = path.parts.index("projects") + 1
        path_project = path.parts[project_index] if project_index < len(path.parts) else ""
        if memory.project != path_project:
            issues.append(
                LintIssue(
                    Severity.ERROR,
                    "SCHEMA_PROJECT_PATH",
                    (
                        f"Front Matter project={memory.project}와 "
                        f"경로 project={path_project}가 다릅니다."
                    ),
                    path,
                    memory.id,
                    document.field_lines.get("project"),
                )
            )
        if not memory.namespace.endswith(f"/project/{memory.project}"):
            issues.append(
                LintIssue(
                    Severity.ERROR,
                    "SCHEMA_NAMESPACE_PROJECT",
                    f"namespace는 /project/{memory.project}로 끝나야 합니다.",
                    path,
                    memory.id,
                    document.field_lines.get("namespace"),
                )
            )
    return memory, issues
