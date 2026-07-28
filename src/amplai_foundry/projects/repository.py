"""Read-only discovery and validation of local Project Packs."""

from __future__ import annotations

import builtins
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from amplai_foundry.domain.identity import ProjectRef
from amplai_foundry.domain.project import require_contained
from amplai_foundry.projects.models import DomainLock, ProjectManifest, version_satisfies


class ProjectPackError(RuntimeError):
    """A Project Pack or workspace violates its portable contract."""


@dataclass(frozen=True, slots=True)
class ProjectPack:
    root: Path
    manifest_path: Path
    manifest: ProjectManifest
    domain_lock: DomainLock

    @property
    def ref(self) -> ProjectRef:
        return self.manifest.ref

    def resolve_canonical(self, field: str) -> Path:
        value = getattr(self.manifest.canonical, field)
        if value is None:
            raise ProjectPackError(f"canonical.{field}가 이 Pack에 설정되지 않았습니다.")
        return require_contained(self.root / value, self.root, label=f"canonical.{field}")

    @property
    def memory_root(self) -> Path:
        return self.resolve_canonical("memory")

    @property
    def proposal_root(self) -> Path:
        return self.resolve_canonical("proposals")

    @property
    def intake_root(self) -> Path:
        return self.resolve_canonical("intake")

    @property
    def roadmap_proposal_root(self) -> Path:
        return self.intake_root / "roadmap-proposals"

    @property
    def runtime_root(self) -> Path:
        return require_contained(
            self.root / self.manifest.runtime.root,
            self.root,
            label="runtime.root",
        )

    @property
    def roadmap_path(self) -> Path | None:
        if self.manifest.canonical.roadmap is None:
            return None
        return self.resolve_canonical("roadmap")


class ProjectPackRepository:
    """Discover manifests without depending on their absolute clone paths."""

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()

    def list(self) -> builtins.list[ProjectPack]:
        if not self.workspace_root.is_dir():
            raise ProjectPackError(f"workspace가 존재하지 않습니다: {self.workspace_root}")
        manifests = sorted(self.workspace_root.glob("**/.amplai/project.yaml"))
        packs = [self._load(path) for path in manifests if ".git" not in path.parts]
        by_id: dict[str, ProjectPack] = {}
        by_namespace: dict[str, ProjectPack] = {}
        defaults = 0
        for pack in packs:
            project_id = pack.manifest.project_id
            namespace = pack.manifest.namespace
            if project_id in by_id:
                raise ProjectPackError(f"workspace project_id가 중복됩니다: {project_id}")
            if namespace in by_namespace:
                raise ProjectPackError(f"workspace namespace가 중복됩니다: {namespace}")
            by_id[project_id] = pack
            by_namespace[namespace] = pack
            defaults += int(pack.manifest.default)
        if defaults > 1:
            raise ProjectPackError("workspace에는 default project가 하나만 있을 수 있습니다.")
        return sorted(packs, key=lambda item: item.manifest.project_id)

    def get(self, project_id: str) -> ProjectPack | None:
        return next(
            (pack for pack in self.list() if pack.manifest.project_id == project_id),
            None,
        )

    def validate(self, pack: ProjectPack) -> builtins.list[str]:
        issues: builtins.list[str] = []
        try:
            runtime_root = pack.runtime_root
        except ValueError as error:
            issues.append(str(error))
            runtime_root = None
        canonical_paths: dict[str, Path] = {}
        for field_name in type(pack.manifest.canonical).model_fields:
            declared_value = getattr(pack.manifest.canonical, field_name)
            if declared_value is None:
                continue
            declared_path = pack.root / str(declared_value)
            if declared_path.is_symlink():
                issues.append(
                    f"canonical.{field_name} root는 symlink일 수 없습니다: {declared_path}"
                )
            try:
                path = pack.resolve_canonical(field_name)
            except ValueError as error:
                issues.append(str(error))
                continue
            canonical_paths[field_name] = path
            if declared_path.is_dir():
                issues.extend(
                    f"canonical.{field_name}에 symlink가 있습니다: {item}"
                    for item in declared_path.rglob("*")
                    if item.is_symlink()
                )
            if field_name == "memory" and not path.is_dir():
                issues.append(f"canonical.memory 경로가 없습니다: {path}")
            if runtime_root is not None and (
                self._is_relative_to(path, runtime_root) or self._is_relative_to(runtime_root, path)
            ):
                issues.append(f"canonical.{field_name}과 runtime.root 경로가 겹칩니다.")
        by_path: dict[Path, list[str]] = {}
        for field_name, path in canonical_paths.items():
            by_path.setdefault(path, []).append(field_name)
        for fields in by_path.values():
            if len(fields) > 1:
                issues.append("canonical 경로가 중복됩니다: " + ", ".join(sorted(fields)))
        requested = {item.module: item.version for item in pack.manifest.imports}
        locked = {item.module: item.version for item in pack.domain_lock.imports}
        if requested.keys() != locked.keys():
            missing = sorted(requested.keys() - locked.keys())
            extra = sorted(locked.keys() - requested.keys())
            if missing:
                issues.append("domain lock 누락: " + ", ".join(missing))
            if extra:
                issues.append("manifest에 없는 domain lock: " + ", ".join(extra))
        for module, version_spec in requested.items():
            locked_version = locked.get(module)
            if locked_version is not None and not version_satisfies(locked_version, version_spec):
                issues.append(
                    f"domain lock version mismatch: {module} "
                    f"requested={version_spec} locked={locked_version}"
                )
        return issues

    @staticmethod
    def _is_relative_to(path: Path, root: Path) -> bool:
        try:
            path.resolve().relative_to(root.resolve())
        except ValueError:
            return False
        return True

    @staticmethod
    def _load(manifest_path: Path) -> ProjectPack:
        root = manifest_path.parent.parent.resolve()
        lock_path = manifest_path.parent / "domain.lock.yaml"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise ProjectPackError(
                f"Project manifest는 pack 내부 regular file이어야 합니다: {manifest_path}"
            )
        if lock_path.is_symlink() or not lock_path.is_file():
            raise ProjectPackError(
                f"domain lock은 pack 내부 regular file이어야 합니다: {lock_path}"
            )
        try:
            manifest_path.resolve().relative_to(root)
            lock_path.resolve().relative_to(root)
        except ValueError as error:
            raise ProjectPackError(
                "Project manifest/domain lock이 pack root 밖을 가리킵니다."
            ) from error
        try:
            manifest_data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
            manifest = ProjectManifest.model_validate(manifest_data)
            lock_data = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
            domain_lock = DomainLock.model_validate(lock_data)
        except (OSError, yaml.YAMLError, ValidationError) as error:
            raise ProjectPackError(f"{manifest_path}: {error}") from error
        return ProjectPack(root, manifest_path.resolve(), manifest, domain_lock)
