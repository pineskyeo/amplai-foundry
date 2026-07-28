import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from amplai_foundry.cli import app
from amplai_foundry.domain.models import MemoryObject
from amplai_foundry.ingestion.hashing import content_sha256, normalized_sha256
from amplai_foundry.ingestion.identifiers import safe_slug, source_id
from amplai_foundry.ingestion.service import (
    IngestionError,
    SourceIngestionService,
    extract_original_content,
)
from amplai_foundry.lint.engine import KnowledgeLinter

runner = CliRunner()
NOW = datetime(2026, 7, 12, 12, 0, tzinfo=ZoneInfo("Asia/Seoul"))


def project_vault(tmp_path: Path, project: str = "amplai") -> Path:
    vault = tmp_path / "vault"
    (vault / "projects" / project / "00-sources").mkdir(parents=True)
    return vault


def ingest(vault: Path, content: bytes, **overrides: object):
    arguments = {
        "project": "amplai",
        "source_type": "chatgpt",
        "title": "AMPLAI Memory Layer 스터디",
        "original_filename": "answer.md",
        "media_type": "text/markdown",
        "now": NOW,
    }
    arguments.update(overrides)
    return SourceIngestionService(vault).ingest(content, **arguments)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("filename", "media_type"),
    [("answer.md", "text/markdown"), ("answer.txt", "text/plain")],
)
def test_markdown_and_text_file_create_source(
    tmp_path: Path, filename: str, media_type: str
) -> None:
    vault = project_vault(tmp_path)
    result = ingest(
        vault, "한글 원문 전체".encode(), original_filename=filename, media_type=media_type
    )

    assert result.status == "created"
    assert result.source_id.startswith("SRC-20260712-")
    assert extract_original_content(Path(result.path)) == "한글 원문 전체".encode()
    found = SourceIngestionService(vault).find(result.source_id)
    assert found is not None
    assert found[1].media_type == media_type


def test_immutable_source_is_exempt_from_atomic_body_size_hygiene(tmp_path: Path) -> None:
    vault = project_vault(tmp_path)
    ingest(vault, b"x" * 8_001)

    codes = {issue.code for issue in KnowledgeLinter().lint(vault).issues}

    assert "HYGIENE_BODY_LARGE" not in codes


def test_exact_and_normalized_duplicates_do_not_create_files(tmp_path: Path) -> None:
    vault = project_vault(tmp_path)
    first = ingest(vault, b"line one\r\nline two\r\n")
    exact = ingest(vault, b"line one\r\nline two\r\n")
    normalized = ingest(vault, b"line one\nline two\n\n")

    assert exact.status == "duplicate"
    assert normalized.status == "duplicate"
    assert exact.source_id == normalized.source_id == first.source_id
    assert len(list((vault / "projects/amplai/00-sources").glob("*.md"))) == 1


def test_concurrent_duplicate_ingestion_is_idempotent(tmp_path: Path) -> None:
    vault = project_vault(tmp_path)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _index: ingest(vault, b"concurrent immutable source"),
                range(2),
            )
        )

    assert sorted(result.status for result in results) == ["created", "duplicate"]
    assert results[0].source_id == results[1].source_id
    assert len(list((vault / "projects/amplai/00-sources").glob("*.md"))) == 1


def test_tampered_duplicate_candidate_fails_closed(tmp_path: Path) -> None:
    vault = project_vault(tmp_path)
    first = ingest(vault, b"immutable duplicate")
    path = Path(first.path)
    path.write_bytes(path.read_bytes() + b" tampered")

    with pytest.raises(IngestionError, match="변조"):
        ingest(vault, b"immutable duplicate")


def test_source_id_prefix_collision_fails_closed(tmp_path: Path) -> None:
    vault = project_vault(tmp_path)
    first = b"collision-validation-47709\n"
    second = b"collision-validation-60816\n"

    created = ingest(vault, first)
    assert created.source_id == "SRC-20260712-C3F8A156"
    with pytest.raises(IngestionError, match="hash prefix 충돌"):
        ingest(vault, second)


@pytest.mark.parametrize("title", ["   ", "unsafe\n## Original Content"])
def test_invalid_source_title_creates_no_file(tmp_path: Path, title: str) -> None:
    vault = project_vault(tmp_path)

    with pytest.raises(IngestionError, match=r"title|metadata"):
        ingest(vault, b"safe bytes", title=title)

    assert not list((vault / "projects/amplai/00-sources").glob("*.md"))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_type", "unsafe\n## Original Content\nevil"),
        ("original_filename", "unsafe\n## Original Content\nevil"),
        ("created_by", "unsafe\n## Original Content\nevil"),
        ("media_type", "text/plain\n## Original Content\nevil"),
    ],
)
def test_metadata_marker_text_cannot_shift_original_boundary(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    vault = project_vault(tmp_path)
    result = ingest(vault, b"ORIGINAL", **{field: value})

    assert extract_original_content(Path(result.path)) == b"ORIGINAL"
    assert SourceIngestionService(vault).verify(result.source_id).valid


def test_original_payload_may_contain_the_source_marker(tmp_path: Path) -> None:
    vault = project_vault(tmp_path)
    original = b"before\n## Original Content\nafter"

    result = ingest(vault, original)

    assert extract_original_content(Path(result.path)) == original


def test_hashes_and_source_id_are_deterministic() -> None:
    content = "결정론적 입력\n".encode()

    assert content_sha256(content) == content_sha256(content)
    assert normalized_sha256(content) == normalized_sha256(content.rstrip(b"\n"))
    assert source_id(NOW.date(), content_sha256(content)) == "SRC-20260712-810BC28C"


def test_safe_slug_removes_path_traversal_and_keeps_korean() -> None:
    slug = safe_slug("../../AMPLAI Memory / 지식 스터디?!")

    assert slug == "amplai-memory-지식-스터디"
    assert "/" not in slug and ".." not in slug


@pytest.mark.parametrize("content", [b"", b"\xff"])
def test_empty_or_non_utf8_input_is_rejected(tmp_path: Path, content: bytes) -> None:
    vault = project_vault(tmp_path)

    with pytest.raises(IngestionError):
        ingest(vault, content)


def test_missing_project_and_mismatched_namespace_are_rejected(tmp_path: Path) -> None:
    vault = project_vault(tmp_path)

    with pytest.raises(IngestionError, match="project가 존재하지"):
        ingest(vault, b"content", project="missing")
    with pytest.raises(IngestionError, match="namespace"):
        ingest(vault, b"content", namespace="org/default/project/other")


@pytest.mark.parametrize(
    "project",
    [".", "..", "../..", "/absolute", "a/b", r"a\b", "has space", "", "bad!project"],
)
def test_invalid_project_id_is_rejected_without_creating_files(
    tmp_path: Path, project: str
) -> None:
    vault = project_vault(tmp_path)
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    with pytest.raises((IngestionError, ValidationError, ValueError)):
        ingest(vault, b"path traversal payload", project=project)

    after = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_source_destination_symlink_escape_is_rejected(tmp_path: Path) -> None:
    vault = project_vault(tmp_path)
    sources = vault / "projects/amplai/00-sources"
    sources.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sources.symlink_to(outside, target_is_directory=True)

    with pytest.raises(IngestionError, match="root 밖"):
        ingest(vault, b"must not escape")

    assert list(outside.iterdir()) == []


def test_cli_rejects_invalid_project_before_ingestion(tmp_path: Path) -> None:
    vault = project_vault(tmp_path)

    result = runner.invoke(
        app,
        ["ingest", "-", "--project", "../..", "--vault", str(vault)],
        input="unsafe",
    )

    assert result.exit_code == 2
    assert not list((vault / "projects/amplai/00-sources").glob("*.md"))


def test_memory_object_rejects_invalid_project_id() -> None:
    with pytest.raises(ValidationError):
        MemoryObject.model_validate(
            {
                "schema_version": 1,
                "id": "CON-9000",
                "namespace": "org/default/project/../..",
                "project": "../..",
                "kind": "concept",
                "status": "candidate",
                "title": "Invalid project",
                "summary": "경로로 사용할 수 없는 project를 가진 fixture다.",
                "created_at": "2026-07-12",
                "updated_at": "2026-07-12",
                "source_refs": [],
                "relations": [],
                "revision": 1,
                "tags": [],
                "content": "충분한 본문",
            }
        )


def test_verify_detects_original_content_mutation(tmp_path: Path) -> None:
    vault = project_vault(tmp_path)
    result = ingest(vault, b"immutable source")
    service = SourceIngestionService(vault)

    assert service.verify(result.source_id).valid
    path = Path(result.path)
    path.write_bytes(path.read_bytes() + b" mutated")

    assert not service.verify(result.source_id).valid
    codes = {issue.code for issue in KnowledgeLinter().lint(vault).issues}
    assert "SOURCE_CONTENT_HASH" in codes


def test_source_lookup_fails_closed_when_local_id_exists_in_multiple_projects(
    tmp_path: Path,
) -> None:
    vault = project_vault(tmp_path)
    (vault / "projects/cortex/00-sources").mkdir(parents=True)
    first = ingest(vault, b"shared source bytes")
    second = ingest(
        vault,
        b"shared source bytes",
        project="cortex",
        title="Cortex source",
    )
    assert first.source_id == second.source_id
    service = SourceIngestionService(vault)

    with pytest.raises(IngestionError, match="여러 project"):
        service.find(first.source_id)
    with pytest.raises(IngestionError, match="여러 project"):
        service.verify(first.source_id)

    amplai = service.find(first.source_id, project="amplai")
    cortex = service.find(first.source_id, project="cortex")
    assert amplai is not None and "projects/amplai" in str(amplai[0])
    assert cortex is not None and "projects/cortex" in str(cortex[0])


def test_linter_detects_missing_source_metadata_and_bad_hash(tmp_path: Path) -> None:
    vault = project_vault(tmp_path)
    result = ingest(vault, b"source")
    path = Path(result.path)
    text = path.read_text(encoding="utf-8")
    start = text.index("source_metadata:")
    end = text.index("---\n", start)
    path.write_text(text[:start] + text[end:], encoding="utf-8")

    codes = {issue.code for issue in KnowledgeLinter().lint(vault).issues}
    assert "SOURCE_METADATA_REQUIRED" in codes

    path.unlink()
    result = ingest(vault, b"another source")
    path = Path(result.path)
    text = path.read_text(encoding="utf-8").replace(
        content_sha256(b"another source"), "not-a-sha256"
    )
    path.write_text(text, encoding="utf-8")
    codes = {issue.code for issue in KnowledgeLinter().lint(vault).issues}
    assert "SCHEMA_INVALID" in codes


def test_project_path_namespace_consistency_lint(tmp_path: Path) -> None:
    vault = project_vault(tmp_path)
    result = ingest(vault, b"scoped source")
    path = Path(result.path)
    text = path.read_text(encoding="utf-8").replace(
        "namespace: org/default/project/amplai", "namespace: org/default/project/other"
    )
    path.write_text(text, encoding="utf-8")

    codes = {issue.code for issue in KnowledgeLinter().lint(vault).issues}
    assert "SCHEMA_NAMESPACE_PROJECT" in codes


def test_source_id_hash_suffix_and_project_path_are_linted(tmp_path: Path) -> None:
    vault = project_vault(tmp_path)
    result = ingest(vault, b"scoped source")
    path = Path(result.path)
    path.write_text(
        path.read_text(encoding="utf-8").replace(result.source_id, "SRC-20260712-FFFFFFFF"),
        encoding="utf-8",
    )
    moved = vault / "projects/other/00-sources/source.md"
    moved.parent.mkdir(parents=True)
    path.rename(moved)

    codes = {issue.code for issue in KnowledgeLinter().lint(vault).issues}
    assert "SOURCE_ID_HASH_SUFFIX" in codes
    assert "SCHEMA_PROJECT_PATH" in codes
    assert "SOURCE_PROJECT_PATH" in codes


def test_linter_detects_manually_created_normalized_duplicate(tmp_path: Path) -> None:
    vault = project_vault(tmp_path)
    first = ingest(vault, b"line one\nline two\n")
    first_path = Path(first.path)
    original_file = first_path.read_bytes()
    marker = b"## Original Content\n"
    prefix, raw = original_file.split(marker, 1)
    duplicate_raw = raw.replace(b"\n", b"\r\n") + b"\r\n"
    duplicate_hash = content_sha256(duplicate_raw)
    duplicate_id = source_id(NOW.date(), duplicate_hash)
    duplicate_prefix = prefix.replace(first.source_id.encode(), duplicate_id.encode()).replace(
        first.content_sha256.encode(), duplicate_hash.encode()
    )
    duplicate_path = first_path.with_name(f"{duplicate_id}-duplicate.md")
    duplicate_path.write_bytes(duplicate_prefix + marker + duplicate_raw)

    codes = {issue.code for issue in KnowledgeLinter().lint(vault).issues}
    assert "SOURCE_NORMALIZED_DUPLICATE" in codes


def test_cli_file_stdin_duplicate_and_json_output(tmp_path: Path) -> None:
    vault = project_vault(tmp_path)
    source = tmp_path / "response.md"
    source.write_text("GPT 답변\n둘째 줄", encoding="utf-8")
    created = runner.invoke(
        app,
        [
            "ingest",
            str(source),
            "--project",
            "amplai",
            "--vault",
            str(vault),
            "--json",
        ],
    )

    assert created.exit_code == 0
    payload = json.loads(created.stdout)
    assert payload["status"] == "created"

    duplicate = runner.invoke(
        app,
        ["ingest", "-", "--project", "amplai", "--vault", str(vault), "--json"],
        input="GPT 답변\n둘째 줄",
    )
    assert duplicate.exit_code == 0
    assert json.loads(duplicate.stdout)["status"] == "duplicate"

    listed = runner.invoke(
        app, ["source", "list", "--project", "amplai", "--vault", str(vault), "--json"]
    )
    shown = runner.invoke(app, ["source", "show", payload["source_id"], "--vault", str(vault)])
    verified = runner.invoke(app, ["source", "verify", payload["source_id"], "--vault", str(vault)])
    assert listed.exit_code == shown.exit_code == verified.exit_code == 0
    assert json.loads(listed.stdout)[0]["source_id"] == payload["source_id"]


def test_cli_rejects_bad_extension(tmp_path: Path) -> None:
    vault = project_vault(tmp_path)
    source = tmp_path / "response.pdf"
    source.write_bytes(b"not markdown")

    result = runner.invoke(
        app, ["ingest", str(source), "--project", "amplai", "--vault", str(vault)]
    )

    assert result.exit_code == 2
