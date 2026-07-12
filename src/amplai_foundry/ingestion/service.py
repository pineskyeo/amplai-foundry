"""Immutable Markdown source ingestion."""

from __future__ import annotations

import builtins
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from amplai_foundry.domain.source import SourceMetadata
from amplai_foundry.ingestion.hashing import content_sha256, normalized_sha256
from amplai_foundry.ingestion.identifiers import safe_slug, source_id
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
    marker_index = data.find(ORIGINAL_CONTENT_MARKER)
    if marker_index < 0:
        raise IngestionError("Source 본문에 '## Original Content' marker가 없습니다.")
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
        if not content:
            raise IngestionError("빈 입력은 Source로 등록할 수 없습니다.")
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise IngestionError("입력은 UTF-8이어야 합니다.") from error
        project_root = self.vault / "projects" / project
        if not project_root.is_dir():
            raise IngestionError(f"project가 존재하지 않습니다: {project}")
        resolved_namespace = namespace or f"org/default/project/{project}"
        if not resolved_namespace.endswith(f"/project/{project}"):
            raise IngestionError(
                f"namespace는 /project/{project}로 끝나야 합니다: {resolved_namespace}"
            )

        exact_hash = content_sha256(content)
        normalized_hash = normalized_sha256(content)
        duplicate = self._find_duplicate(project_root, exact_hash, normalized_hash)
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
        sources_dir = project_root / "00-sources"
        sources_dir.mkdir(parents=True, exist_ok=True)
        path = sources_dir / f"{identifier}-{safe_slug(title)}.md"
        metadata = {
            "schema_version": 1,
            "id": identifier,
            "namespace": resolved_namespace,
            "project": project,
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
        front_matter = yaml.safe_dump(
            metadata,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        ).encode("utf-8")
        prefix = b"---\n" + front_matter + b"---\n# " + title.encode("utf-8") + b"\n\n"
        payload = prefix + ORIGINAL_CONTENT_MARKER + content
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
        except FileExistsError:
            duplicate = self._find_duplicate(project_root, exact_hash, normalized_hash)
            if duplicate is None:
                raise IngestionError(f"Source ID 충돌이 발생했습니다: {identifier}") from None
            duplicate_id, duplicate_path = duplicate
            return IngestionResult(
                "duplicate", duplicate_id, str(duplicate_path), exact_hash, duplicate_id
            )
        except OSError as error:
            raise IngestionError(f"Source를 생성할 수 없습니다: {error}") from error
        return IngestionResult("created", identifier, str(path), exact_hash, None)

    def verify(self, identifier: str) -> VerificationResult:
        """Compare one source's original bytes with its stored exact hash."""
        found = self.find(identifier)
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

    def find(self, identifier: str) -> tuple[Path, SourceMetadata] | None:
        for path, metadata, source_identifier in self._source_records(self.vault):
            if source_identifier == identifier:
                return path, metadata
        return None

    def list(
        self, *, project: str | None = None
    ) -> builtins.list[tuple[Path, SourceMetadata, str]]:
        root = self.vault / "projects" / project if project else self.vault
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
