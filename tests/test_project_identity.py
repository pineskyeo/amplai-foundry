from pathlib import Path

import pytest
import yaml

from amplai_foundry.domain.identity import MemoryRef, ProjectRef
from amplai_foundry.domain.models import MemoryObject, MemoryRelation
from amplai_foundry.lint.rules.identifiers import validate_identifiers
from amplai_foundry.lint.rules.links import validate_links
from amplai_foundry.parsing.markdown import parse_markdown_file
from amplai_foundry.projects.repository import ProjectPackRepository
from amplai_foundry.projects.resolver import ProjectResolutionStatus, ProjectResolver
from amplai_foundry.projects.service import ProjectPackService
from amplai_foundry.repositories.markdown import (
    MarkdownMemoryRepository,
    MarkdownRepositoryError,
)


def _note(path: Path, *, project: str, namespace: str, identifier: str) -> None:
    metadata = {
        "schema_version": 1,
        "id": identifier,
        "namespace": namespace,
        "project": project,
        "kind": "concept",
        "status": "active",
        "title": f"{project} {identifier}",
        "summary": f"{project} project-scoped identity fixture.",
        "created_at": "2026-07-28",
        "updated_at": "2026-07-28",
        "source_refs": [],
        "relations": [],
        "revision": 1,
        "tags": ["identity-fixture"],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        + yaml.safe_dump(metadata, sort_keys=False)
        + "---\n# Identity fixture\n\nThis body is long enough for repository parsing.",
        encoding="utf-8",
    )


def _pack(
    root: Path,
    *,
    project: str,
    namespace: str,
    default: bool,
) -> None:
    (root / ".amplai").mkdir(parents=True)
    (root / "memory").mkdir()
    manifest = {
        "schema_version": 1,
        "project_id": project,
        "namespace": namespace,
        "name": project.title(),
        "aliases": [f"{project}-alias"],
        "default": default,
        "imports": [],
        "profiles": [],
        "canonical": {"memory": "memory"},
        "runtime": {"root": ".amplai/runtime"},
    }
    (root / ".amplai/project.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False),
        encoding="utf-8",
    )
    (root / ".amplai/domain.lock.yaml").write_text(
        "schema_version: 1\nimports: []\n",
        encoding="utf-8",
    )


def test_memory_ref_requires_namespace_for_local_parse() -> None:
    reference = MemoryRef.parse(
        "CON-0001",
        default_namespace="org/pinesky/project/amplai",
    )

    assert reference.qualified == "org/pinesky/project/amplai#CON-0001"
    assert MemoryRef.parse(reference.qualified) == reference
    with pytest.raises(ValueError, match="default namespace"):
        MemoryRef.parse("CON-0001")


def test_projects_can_reuse_local_ids_without_collision(tmp_path: Path) -> None:
    _note(
        tmp_path / "amplai/CON-0001.md",
        project="amplai",
        namespace="org/pinesky/project/amplai",
        identifier="CON-0001",
    )
    _note(
        tmp_path / "cortex/CON-0001.md",
        project="cortex",
        namespace="org/pinesky/project/cortex",
        identifier="CON-0001",
    )
    repository = MarkdownMemoryRepository(tmp_path)

    amplai = repository.get(
        MemoryRef(
            namespace="org/pinesky/project/amplai",
            local_id="CON-0001",
        )
    )
    cortex = repository.get(
        "CON-0001",
        namespace="org/pinesky/project/cortex",
    )

    assert amplai is not None and amplai.project == "amplai"
    assert cortex is not None and cortex.project == "cortex"
    with pytest.raises(MarkdownRepositoryError, match="ambiguous local memory ID"):
        repository.get("CON-0001")

    records = []
    for path in sorted(tmp_path.rglob("*.md")):
        document = parse_markdown_file(path)
        records.append(
            (
                path,
                MemoryObject.model_validate({**document.metadata, "content": document.content}),
            )
        )
    assert not [
        issue for issue in validate_identifiers(records) if issue.code == "IDENTIFIER_DUPLICATE"
    ]


def test_two_project_packs_roundtrip_same_local_id_and_reject_unbundled_cross_refs(
    tmp_path: Path,
) -> None:
    amplai_root = tmp_path / "amplai"
    cortex_root = tmp_path / "cortex"
    _pack(
        amplai_root,
        project="amplai",
        namespace="org/pinesky/project/amplai",
        default=True,
    )
    _pack(
        cortex_root,
        project="cortex",
        namespace="org/pinesky/project/cortex",
        default=False,
    )
    amplai_note = amplai_root / "memory/10-concepts/CON-0001.md"
    cortex_note = cortex_root / "memory/10-concepts/CON-0001.md"
    _note(
        amplai_note,
        project="amplai",
        namespace="org/pinesky/project/amplai",
        identifier="CON-0001",
    )
    _note(
        cortex_note,
        project="cortex",
        namespace="org/pinesky/project/cortex",
        identifier="CON-0001",
    )
    for path in (amplai_note, cortex_note):
        path.write_text(
            path.read_text(encoding="utf-8").replace("status: active", "status: candidate"),
            encoding="utf-8",
        )
    repository = ProjectPackRepository(tmp_path)
    service = ProjectPackService(repository)
    amplai = repository.get("amplai")
    cortex = repository.get("cortex")
    assert amplai is not None and cortex is not None

    assert service.validate(amplai).valid
    assert service.validate(cortex).valid
    assert MarkdownMemoryRepository(amplai.memory_root).get("org/pinesky/project/amplai#CON-0001")
    assert MarkdownMemoryRepository(cortex.memory_root).get("org/pinesky/project/cortex#CON-0001")
    assert service.rebuild(amplai).exists()
    assert service.rebuild(cortex).exists()

    text = amplai_note.read_text(encoding="utf-8")
    amplai_note.write_text(
        text.replace(
            "relations: []",
            "relations:\n"
            "- type: related_to\n"
            "  target: CON-0001\n"
            "  target_namespace: org/pinesky/project/cortex",
        ),
        encoding="utf-8",
    )
    validation = service.validate(amplai)
    assert not validation.valid
    assert any("LINK_RELATION_TARGET_MISSING" in issue for issue in validation.issues)
    with pytest.raises(ValueError, match="invalid Project Pack"):
        service.pack(amplai, tmp_path / "invalid.zip")


def test_explicit_cross_project_reference_must_resolve(tmp_path: Path) -> None:
    _note(
        tmp_path / "amplai/CON-0001.md",
        project="amplai",
        namespace="org/pinesky/project/amplai",
        identifier="CON-0001",
    )
    _note(
        tmp_path / "cortex/CON-0002.md",
        project="cortex",
        namespace="org/pinesky/project/cortex",
        identifier="CON-0002",
    )
    records = []
    for path in sorted(tmp_path.rglob("*.md")):
        document = parse_markdown_file(path)
        memory = MemoryObject.model_validate({**document.metadata, "content": document.content})
        if memory.project == "amplai":
            memory = memory.model_copy(
                update={
                    "relations": [
                        MemoryRelation(
                            type="related_to",
                            target="CON-0002",
                            target_namespace="org/pinesky/project/cortex",
                        )
                    ]
                }
            )
            memory = MemoryObject.model_validate(memory.model_dump())
        records.append((path, memory))

    assert not validate_links(records)

    missing_target = records[0][1].model_copy(
        update={
            "relations": [
                MemoryRelation(
                    type="related_to",
                    target="CON-9999",
                    target_namespace="org/pinesky/project/cortex",
                )
            ]
        }
    )
    missing_target = MemoryObject.model_validate(missing_target.model_dump())
    issues = validate_links([(records[0][0], missing_target), records[1]])
    assert any(issue.code == "LINK_RELATION_TARGET_MISSING" for issue in issues)


def test_project_resolver_prefers_explicit_hint_and_fails_closed(tmp_path: Path) -> None:
    _pack(
        tmp_path / "amplai",
        project="amplai",
        namespace="org/pinesky/project/amplai",
        default=False,
    )
    _pack(
        tmp_path / "cortex",
        project="cortex",
        namespace="org/pinesky/project/cortex",
        default=False,
    )
    resolver = ProjectResolver(ProjectPackRepository(tmp_path))

    explicit = resolver.resolve(
        instruction="정리해줘",
        project_hint="cortex",
    )
    ambiguous = resolver.resolve(instruction="이 문서를 정리해줘")
    invalid_hint = resolver.resolve(
        instruction="AMPLAI라고 쓰여 있어도 hint가 우선이다",
        project_hint="does-not-exist",
    )

    assert explicit.status is ProjectResolutionStatus.RESOLVED
    assert explicit.project == ProjectRef(
        project_id="cortex",
        namespace="org/pinesky/project/cortex",
    )
    assert ambiguous.status is ProjectResolutionStatus.HOLD
    assert invalid_hint.status is ProjectResolutionStatus.HOLD


def test_project_resolver_uses_artifact_containment_before_default(tmp_path: Path) -> None:
    _pack(
        tmp_path / "amplai",
        project="amplai",
        namespace="org/pinesky/project/amplai",
        default=True,
    )
    cortex_root = tmp_path / "cortex"
    _pack(
        cortex_root,
        project="cortex",
        namespace="org/pinesky/project/cortex",
        default=False,
    )
    artifact = cortex_root / "request.md"
    artifact.write_text("# Cortex request", encoding="utf-8")

    resolution = ProjectResolver(ProjectPackRepository(tmp_path)).resolve(
        instruction="이 파일을 반영해줘",
        artifact_paths=[artifact],
    )

    assert resolution.status is ProjectResolutionStatus.RESOLVED
    assert resolution.project is not None
    assert resolution.project.project_id == "cortex"


def test_project_resolver_holds_conflicting_path_and_instruction_signals(
    tmp_path: Path,
) -> None:
    amplai_root = tmp_path / "amplai"
    _pack(
        amplai_root,
        project="amplai",
        namespace="org/pinesky/project/amplai",
        default=False,
    )
    _pack(
        tmp_path / "cortex",
        project="cortex",
        namespace="org/pinesky/project/cortex",
        default=False,
    )
    artifact = amplai_root / "request.md"
    artifact.write_text("# Request", encoding="utf-8")

    resolution = ProjectResolver(ProjectPackRepository(tmp_path)).resolve(
        instruction="이 문서를 cortex에 반영해줘",
        artifact_paths=[artifact],
    )

    assert resolution.status is ProjectResolutionStatus.HOLD
    assert "충돌" in resolution.reason


def test_project_alias_requires_a_token_boundary(tmp_path: Path) -> None:
    root = tmp_path / "amplai"
    _pack(
        root,
        project="amplai",
        namespace="org/pinesky/project/amplai",
        default=False,
    )
    manifest_path = root / ".amplai/project.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["aliases"] = ["ai"]
    manifest_path.write_text(
        yaml.safe_dump(manifest, sort_keys=False),
        encoding="utf-8",
    )
    resolver = ProjectResolver(ProjectPackRepository(tmp_path))

    substring = resolver.resolve(instruction="failure report를 정리해줘")
    explicit_token = resolver.resolve(instruction="ai report를 정리해줘")

    assert substring.status is ProjectResolutionStatus.HOLD
    assert explicit_token.status is ProjectResolutionStatus.RESOLVED
