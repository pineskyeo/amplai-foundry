"""Validated project identifiers and project-scoped path containment."""

from pathlib import Path
from typing import Annotated

from pydantic import StringConstraints, TypeAdapter, ValidationError

PROJECT_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
ProjectId = Annotated[str, StringConstraints(pattern=PROJECT_ID_PATTERN)]
_PROJECT_ID_ADAPTER = TypeAdapter(ProjectId)


class ProjectPathError(ValueError):
    """A project identifier or derived path is unsafe."""


def validate_project_id(value: str) -> str:
    """Validate and return one project identifier."""
    try:
        return _PROJECT_ID_ADAPTER.validate_python(value)
    except ValidationError as error:
        raise ProjectPathError(
            "project는 소문자 영숫자로 시작하고 영숫자, '_', '-'만 포함해야 합니다."
        ) from error


def require_contained(path: Path, root: Path, *, label: str) -> Path:
    """Resolve a path and require it to remain under the resolved root."""
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as error:
        raise ProjectPathError(f"{label} 경로가 허용된 root 밖을 가리킵니다.") from error
    return resolved_path


def project_root(vault: Path, project: str) -> Path:
    """Resolve a validated project root contained by ``vault/projects``."""
    validated = validate_project_id(project)
    projects_root = (vault / "projects").resolve()
    resolved = require_contained(projects_root / validated, projects_root, label="project")
    if resolved.parent != projects_root or resolved.name != validated:
        raise ProjectPathError("project root는 다른 project를 가리키는 symlink일 수 없습니다.")
    return resolved
