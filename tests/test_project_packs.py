import hashlib
import json
import shutil
import zipfile
from pathlib import Path

import pytest
import yaml

from amplai_foundry.projects.domain_registry import DomainRegistry
from amplai_foundry.projects.repository import ProjectPackError, ProjectPackRepository
from amplai_foundry.projects.service import ProjectPackService
from amplai_foundry.repositories.markdown import MarkdownMemoryRepository, MarkdownRepositoryError


def _write_pack(
    root: Path,
    *,
    imports: list[dict[str, str]] | None = None,
    locks: list[dict[str, str]] | None = None,
) -> None:
    (root / ".amplai").mkdir(parents=True)
    (root / "memory").mkdir()
    (root / ".amplai/project.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "project_id": "amplai",
                "namespace": "org/pinesky/project/amplai",
                "name": "AMPLAI",
                "default": True,
                "imports": imports or [],
                "canonical": {"memory": "memory"},
                "runtime": {"root": ".amplai/runtime"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (root / ".amplai/domain.lock.yaml").write_text(
        yaml.safe_dump(
            {"schema_version": 1, "imports": locks or []},
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _domain(
    root: Path,
    module: str,
    *,
    version: str = "1.0.0",
    imports: list[dict[str, str]] | None = None,
) -> None:
    path = root / "domains" / module
    path.mkdir(parents=True)
    (path / "module.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "module": module,
                "version": version,
                "imports": imports or [],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_pack_is_clone_path_independent_and_runtime_rebuildable(tmp_path: Path) -> None:
    original = tmp_path / "original"
    _write_pack(original)
    repository = ProjectPackRepository(original)
    pack = repository.get("amplai")
    assert pack is not None

    index = ProjectPackService(repository).rebuild(pack)
    payload = json.loads(index.read_text(encoding="utf-8"))
    assert payload["namespace"] == "org/pinesky/project/amplai"
    assert payload["records"] == []

    moved = tmp_path / "projects/another/location"
    shutil.copytree(original, moved)
    moved_pack = ProjectPackRepository(moved).get("amplai")
    assert moved_pack is not None
    assert moved_pack.ref == pack.ref
    assert moved_pack.memory_root == moved / "memory"

    shutil.rmtree(moved_pack.runtime_root)
    rebuilt = ProjectPackService(ProjectPackRepository(moved)).rebuild(moved_pack)
    assert rebuilt.exists()


def test_pack_archive_is_stable_inside_canonical_output_and_preserves_empty_memory(
    tmp_path: Path,
) -> None:
    root = tmp_path / "pack"
    _write_pack(root)
    repository = ProjectPackRepository(root)
    pack = repository.get("amplai")
    assert pack is not None
    output = root / ".amplai/evals/amplai.zip"
    service = ProjectPackService(repository)

    service.pack(pack, output)
    first_hash = hashlib.sha256(output.read_bytes()).hexdigest()
    service.pack(pack, output)
    second_hash = hashlib.sha256(output.read_bytes()).hexdigest()

    assert first_hash == second_hash
    with zipfile.ZipFile(output) as archive:
        assert "memory/" in archive.namelist()
        assert ".amplai/evals/" in archive.namelist()
        assert ".amplai/evals/amplai.zip" not in archive.namelist()

    extracted = tmp_path / "extracted"
    with zipfile.ZipFile(output) as archive:
        archive.extractall(extracted)
    extracted_pack = ProjectPackRepository(extracted).get("amplai")
    assert extracted_pack is not None
    assert ProjectPackService(ProjectPackRepository(extracted)).validate(extracted_pack).valid


def test_pack_archive_excludes_runtime_and_secret_like_files(tmp_path: Path) -> None:
    root = tmp_path / "credentials-workspace/pack"
    _write_pack(root)
    repository = ProjectPackRepository(root)
    pack = repository.get("amplai")
    assert pack is not None
    (root / ".amplai/runtime/cache").mkdir(parents=True)
    (root / ".amplai/runtime/cache/state.json").write_text("{}", encoding="utf-8")
    (root / ".amplai/.env").write_text("TOKEN=secret", encoding="utf-8")
    (root / ".amplai/policies").mkdir()
    (root / ".amplai/policies/read-only.md").write_text("safe", encoding="utf-8")
    (root / ".amplai/policies/.env").mkdir()
    (root / ".amplai/policies/.env/token.txt").write_text("secret", encoding="utf-8")

    output = ProjectPackService(repository).pack(pack, tmp_path / "pack.zip")

    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
    assert ".amplai/project.yaml" in names
    assert ".amplai/policies/read-only.md" in names
    assert not any("runtime" in name for name in names)
    assert not any(".env" in name for name in names)


def test_domain_lock_mismatch_is_reported(tmp_path: Path) -> None:
    _write_pack(
        tmp_path,
        imports=[{"module": "foundation", "version": "1.x"}],
        locks=[{"module": "foundation", "version": "2.0.0"}],
    )
    _domain(tmp_path, "foundation", version="2.0.0")
    repository = ProjectPackRepository(tmp_path)
    pack = repository.get("amplai")
    assert pack is not None

    issues = ProjectPackService(repository).validate(pack).issues

    assert any("domain lock version mismatch" in issue for issue in issues)


def test_domain_import_cycle_is_reported(tmp_path: Path) -> None:
    _domain(
        tmp_path,
        "foundation",
        imports=[{"module": "work-management", "version": "1.x"}],
    )
    _domain(
        tmp_path,
        "work-management",
        imports=[{"module": "foundation", "version": "1.x"}],
    )

    issues = DomainRegistry(tmp_path / "domains").validate_graph()

    assert any("domain import cycle" in issue for issue in issues)


def test_pack_and_memory_repository_do_not_follow_external_symlink(
    tmp_path: Path,
) -> None:
    root = tmp_path / "pack"
    _write_pack(root)
    outside = tmp_path / "outside.md"
    outside.write_text("---\nnot: canonical\n---\nsecret\n", encoding="utf-8")
    leak = root / "memory/leak.md"
    leak.symlink_to(outside)
    repository = ProjectPackRepository(root)
    pack = repository.get("amplai")
    assert pack is not None

    with pytest.raises(MarkdownRepositoryError, match="escapes repository root"):
        MarkdownMemoryRepository(pack.memory_root).list()

    issues = ProjectPackService(repository).validate(pack).issues
    assert any("escapes repository root" in issue for issue in issues)


def test_pack_rejects_symlinked_domain_lock(tmp_path: Path) -> None:
    root = tmp_path / "pack"
    _write_pack(root)
    lock = root / ".amplai/domain.lock.yaml"
    outside = tmp_path / "outside-lock.yaml"
    outside.write_text("schema_version: 1\nimports: []\n", encoding="utf-8")
    lock.unlink()
    lock.symlink_to(outside)

    with pytest.raises(ProjectPackError, match="regular file"):
        ProjectPackRepository(root).list()


def test_pack_validation_parses_typed_semantic_artifacts(tmp_path: Path) -> None:
    root = tmp_path / "pack"
    _write_pack(root)
    semantic = root / ".amplai/semantic"
    semantic.mkdir()
    (semantic / "anchors.yaml").write_text(
        "schema_version: 2\nanchors: []\n",
        encoding="utf-8",
    )
    repository = ProjectPackRepository(root)
    pack = repository.get("amplai")
    assert pack is not None

    issues = ProjectPackService(repository).validate(pack).issues

    assert any("anchors.yaml" in issue for issue in issues)


def test_canonical_and_runtime_paths_must_not_overlap(tmp_path: Path) -> None:
    _write_pack(tmp_path)
    manifest_path = tmp_path / ".amplai/project.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["canonical"]["memory"] = ".amplai/runtime"
    manifest_path.write_text(
        yaml.safe_dump(manifest, sort_keys=False),
        encoding="utf-8",
    )
    repository = ProjectPackRepository(tmp_path)
    pack = repository.get("amplai")
    assert pack is not None

    issues = repository.validate(pack)

    assert any("경로가 겹칩니다" in issue for issue in issues)


@pytest.mark.parametrize(
    "memory_path",
    [r"..\outside", r"C:\outside", r"folder\..\outside", "C:/outside"],
)
def test_manifest_rejects_windows_style_escape_paths(
    tmp_path: Path,
    memory_path: str,
) -> None:
    _write_pack(tmp_path)
    manifest_path = tmp_path / ".amplai/project.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["canonical"]["memory"] = memory_path
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    with pytest.raises(ProjectPackError, match="상대 경로"):
        ProjectPackRepository(tmp_path).list()
