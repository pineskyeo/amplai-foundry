"""Immutable Markdown source ingestion."""

from __future__ import annotations

import builtins
import os
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from amplai_foundry.domain.identity import ProjectRef
from amplai_foundry.domain.models import MemoryMetadata
from amplai_foundry.domain.project import (
    ProjectPathError,
    project_root,
    require_contained,
    validate_project_id,
)
from amplai_foundry.domain.source import SourceMetadata
from amplai_foundry.ingestion.hashing import content_sha256, normalized_sha256
from amplai_foundry.ingestion.identifiers import source_id
from amplai_foundry.ingestion.result import IngestionResult, VerificationResult
from amplai_foundry.parsing.markdown import MarkdownParseError, parse_markdown_file

ORIGINAL_CONTENT_MARKER = b"## Original Content\n"


class IngestionError(RuntimeError):
    """Input cannot be safely registered as an immutable source."""


def extract_original_content(path: Path) -> bytes:
    """Extract the byte-exact original input following the source marker."""
    try:
        data = path.read_bytes()
    except OSError as error:
        raise IngestionError(f"Source를 읽을 수 없습니다: {error}") from error
    if not data.startswith(b"---\n"):
        raise IngestionError("Source가 YAML Front Matter로 시작하지 않습니다.")
    front_matter_end = data.find(b"\n---\n", 4)
    if front_matter_end < 0:
        raise IngestionError("Source YAML Front Matter 종료 marker가 없습니다.")
    body_start = front_matter_end + len(b"\n---\n")
    marker_index = data.find(ORIGINAL_CONTENT_MARKER, body_start)
    if marker_index < 0:
        raise IngestionError("Source 본문에 '## Original Content' marker가 없습니다.")
    controlled_heading = data[body_start:marker_index]
    if controlled_heading and (
        not controlled_heading.startswith(b"# ")
        or not controlled_heading.endswith(b"\n\n")
        or b"\n" in controlled_heading[:-2]
    ):
        raise IngestionError("Source 본문 header/원문 경계가 올바르지 않습니다.")
    return data[marker_index + len(ORIGINAL_CONTENT_MARKER) :]


class SourceIngestionService:
    """Register and verify immutable source notes in one project vault."""

    def __init__(self, vault: Path = Path("vault")) -> None:
        self.vault = vault

    def ingest(
        self,
        content: bytes,
        *,
        project: str,
        source_type: str,
        title: str,
        namespace: str | None = None,
        original_filename: str | None = None,
        media_type: str = "text/plain",
        created_by: str = "user",
        now: datetime | None = None,
    ) -> IngestionResult:
        """Create one source unless exact or normalized content already exists."""
        try:
            project = validate_project_id(project)
            resolved_project_root = project_root(self.vault, project)
        except ProjectPathError as error:
            raise IngestionError(str(error)) from error
        if not resolved_project_root.is_dir():
            raise IngestionError(f"project가 존재하지 않습니다: {project}")
        resolved_namespace = namespace or f"org/default/project/{project}"
        try:
            project_ref = ProjectRef(project_id=project, namespace=resolved_namespace)
        except ValueError as error:
            raise IngestionError(str(error)) from error
        return self.ingest_project(
            content,
            project=project_ref,
            project_root_path=resolved_project_root,
            source_type=source_type,
            title=title,
            original_filename=original_filename,
            media_type=media_type,
            created_by=created_by,
            now=now,
        )

    def ingest_project(
        self,
        content: bytes,
        *,
        project: ProjectRef,
        project_root_path: Path,
        source_type: str,
        title: str,
        original_filename: str | None = None,
        media_type: str = "text/plain",
        created_by: str = "user",
        now: datetime | None = None,
    ) -> IngestionResult:
        """Register a Source in a portable Project Pack memory root."""
        if not content:
            raise IngestionError("빈 입력은 Source로 등록할 수 없습니다.")
        if "\n" in title or "\r" in title:
            raise IngestionError("Source title에는 줄바꿈을 포함할 수 없습니다.")
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise IngestionError("입력은 UTF-8이어야 합니다.") from error
        resolved_project_root = project_root_path.resolve()
        if not resolved_project_root.is_dir():
            raise IngestionError(f"project memory root가 존재하지 않습니다: {project_root_path}")
        exact_hash = content_sha256(content)
        normalized_hash = normalized_sha256(content)
        duplicate = self._find_duplicate(resolved_project_root, exact_hash, normalized_hash)
        if duplicate is not None:
            duplicate_id, duplicate_path = duplicate
            return IngestionResult(
                status="duplicate",
                source_id=duplicate_id,
                path=str(duplicate_path),
                content_sha256=exact_hash,
                duplicate_of=duplicate_id,
            )

        timestamp = now or datetime.now(ZoneInfo("Asia/Seoul"))
        identifier = source_id(timestamp.date(), exact_hash)
        try:
            sources_dir = require_contained(
                resolved_project_root / "00-sources",
                resolved_project_root,
                label="Source directory",
            )
            path = require_contained(
                sources_dir / f"{identifier}.md",
                resolved_project_root,
                label="Source",
            )
        except ProjectPathError as error:
            raise IngestionError(str(error)) from error
        sources_dir.mkdir(parents=True, exist_ok=True)
        colliding_paths = [
            existing_path
            for existing_path, _metadata, existing_id in self._source_records(resolved_project_root)
            if existing_id == identifier
        ]
        if colliding_paths:
            duplicate = self._find_duplicate(resolved_project_root, exact_hash, normalized_hash)
            if duplicate is not None:
                duplicate_id, duplicate_path = duplicate
                return IngestionResult(
                    "duplicate", duplicate_id, str(duplicate_path), exact_hash, duplicate_id
                )
            raise IngestionError(
                f"Source ID hash prefix 충돌이 발생했습니다: {identifier}; "
                + ", ".join(str(item) for item in colliding_paths)
            )
        metadata = {
            "schema_version": 1,
            "id": identifier,
            "namespace": project.namespace,
            "project": project.project_id,
            "kind": "source",
            "status": "active",
            "title": title,
            "summary": f"{source_type}에서 수집한 자유 형식 원문",
            "created_at": timestamp.date(),
            "updated_at": timestamp.date(),
            "source_refs": [],
            "relations": [],
            "revision": 1,
            "tags": [source_type],
            "source_metadata": SourceMetadata(
                source_type=source_type,
                content_sha256=exact_hash,
                normalized_sha256=normalized_hash,
                original_filename=original_filename,
                media_type=media_type,
                ingested_at=timestamp,
                created_by=created_by,
            ).model_dump(mode="json"),
        }
        try:
            validated_metadata = MemoryMetadata.model_validate(metadata)
        except ValueError as error:
            raise IngestionError(f"Source metadata가 유효하지 않습니다: {error}") from error
        front_matter = yaml.safe_dump(
            validated_metadata.model_dump(mode="json", exclude_none=True),
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        ).encode("utf-8")
        prefix = b"---\n" + front_matter + b"---\n# " + title.encode("utf-8") + b"\n\n"
        payload = prefix + ORIGINAL_CONTENT_MARKER + content
        temporary_path: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{identifier}.",
                suffix=".tmp",
                dir=sources_dir,
            )
            temporary_path = Path(temporary_name)
            os.chmod(temporary_path, 0o644)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.link(temporary_path, path)
        except FileExistsError:
            duplicate = self._find_duplicate(resolved_project_root, exact_hash, normalized_hash)
            if duplicate is None:
                raise IngestionError(
                    f"Source ID hash prefix 충돌이 발생했습니다: {identifier}"
                ) from None
            duplicate_id, duplicate_path = duplicate
            return IngestionResult(
                "duplicate", duplicate_id, str(duplicate_path), exact_hash, duplicate_id
            )
        except OSError as error:
            raise IngestionError(f"Source를 생성할 수 없습니다: {error}") from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        return IngestionResult("created", identifier, str(path), exact_hash, None)

    def verify(
        self,
        identifier: str,
        *,
        project: str | None = None,
        namespace: str | None = None,
    ) -> VerificationResult:
        """Compare one source's original bytes with its stored exact hash."""
        found = self.find(identifier, project=project, namespace=namespace)
        if found is None:
            raise IngestionError(f"Source ID가 존재하지 않습니다: {identifier}")
        path, metadata = found
        original = extract_original_content(path)
        actual = content_sha256(original)
        return VerificationResult(
            source_id=identifier,
            path=str(path),
            valid=actual == metadata.content_sha256,
            content_sha256=actual,
            expected_sha256=metadata.content_sha256,
        )

    def find(
        self,
        identifier: str,
        *,
        project: str | None = None,
        namespace: str | None = None,
    ) -> tuple[Path, SourceMetadata] | None:
        matches: list[tuple[Path, SourceMetadata]] = []
        for path, metadata, source_identifier in self._source_records(self.vault):
            if source_identifier != identifier:
                continue
            try:
                document = parse_markdown_file(path)
            except MarkdownParseError as error:
                raise IngestionError(f"Source metadata를 읽을 수 없습니다: {error}") from error
            if project is not None and document.metadata.get("project") != project:
                continue
            if namespace is not None and document.metadata.get("namespace") != namespace:
                continue
            matches.append((path, metadata))
        if len(matches) > 1:
            paths = ", ".join(str(path) for path, _metadata in matches)
            raise IngestionError(
                f"Source ID가 여러 project에 존재합니다: {identifier}; "
                f"project 또는 namespace를 지정하세요: {paths}"
            )
        return matches[0] if matches else None

    def list(
        self, *, project: str | None = None
    ) -> builtins.list[tuple[Path, SourceMetadata, str]]:
        try:
            root = project_root(self.vault, project) if project else self.vault.resolve()
        except ProjectPathError as error:
            raise IngestionError(str(error)) from error
        if project and not root.is_dir():
            raise IngestionError(f"project가 존재하지 않습니다: {project}")
        return self._source_records(root)

    def _find_duplicate(
        self, project_root: Path, exact_hash: str, normalized_hash: str
    ) -> tuple[str, Path] | None:
        for path, metadata, identifier in self._source_records(project_root):
            if (
                metadata.content_sha256 == exact_hash
                or metadata.normalized_sha256 == normalized_hash
            ):
                try:
                    original = extract_original_content(path)
                except IngestionError as error:
                    raise IngestionError(
                        f"중복 후보 Source 무결성 검증에 실패했습니다: {path}: {error}"
                    ) from error
                actual_exact = content_sha256(original)
                actual_normalized = normalized_sha256(original)
                if (
                    actual_exact != metadata.content_sha256
                    or actual_normalized != metadata.normalized_sha256
                ):
                    raise IngestionError(
                        f"중복 후보 Source가 변조되었습니다: {identifier} ({path})"
                    )
                return identifier, path
        return None

    @staticmethod
    def _source_records(
        root: Path,
    ) -> builtins.list[tuple[Path, SourceMetadata, str]]:
        records: builtins.list[tuple[Path, SourceMetadata, str]] = []
        for path in sorted(root.rglob("*.md")):
            try:
                document = parse_markdown_file(path)
            except MarkdownParseError:
                continue
            if document.metadata.get("kind") != "source":
                continue
            try:
                metadata = SourceMetadata.model_validate(document.metadata.get("source_metadata"))
                identifier = str(document.metadata["id"])
            except (KeyError, ValueError) as error:
                raise IngestionError(
                    f"Source metadata가 올바르지 않습니다: {path}: {error}"
                ) from error
            records.append((path, metadata, identifier))
        return records
