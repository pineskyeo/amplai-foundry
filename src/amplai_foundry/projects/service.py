"""Project Pack validation, derived-state rebuild, and deterministic packing."""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass
from pathlib import Path

from amplai_foundry.domain.enums import MemoryKind, MemoryStatus
from amplai_foundry.intake.store import IntakeRunStore, IntakeStoreError
from amplai_foundry.lint.engine import KnowledgeLinter
from amplai_foundry.projects.domain_registry import DomainRegistry
from amplai_foundry.projects.repository import ProjectPack, ProjectPackRepository
from amplai_foundry.proposals.repository import ProposalRepository, ProposalRepositoryError
from amplai_foundry.proposals.validation import ProposalValidator
from amplai_foundry.repositories.markdown import (
    MarkdownMemoryRepository,
    MarkdownRepositoryError,
)
from amplai_foundry.roadmaps.proposals import (
    RoadmapProposalRepository,
    RoadmapProposalRepositoryError,
)
from amplai_foundry.roadmaps.repository import RoadmapRepository, RoadmapRepositoryError
from amplai_foundry.semantics.repository import (
    SemanticAnchorRepository,
    SemanticRepositoryError,
)


@dataclass(frozen=True, slots=True)
class ProjectValidation:
    project_id: str
    issues: tuple[str, ...]

    @property
    def valid(self) -> bool:
        return not self.issues


class ProjectPackService:
    def __init__(self, repository: ProjectPackRepository) -> None:
        self.repository = repository

    def validate(self, pack: ProjectPack) -> ProjectValidation:
        issues = self.repository.validate(pack)
        memories = []
        if pack.memory_root.is_dir():
            report = KnowledgeLinter().lint(pack.memory_root)
            issues.extend(
                f"{issue.code}: {issue.message}"
                for issue in report.issues
                if issue.severity.value == "ERROR"
            )
            try:
                memories = MarkdownMemoryRepository(pack.memory_root).list()
            except MarkdownRepositoryError as error:
                issues.append(str(error))
            else:
                issues.extend(
                    (
                        f"memory project boundary mismatch: {memory.ref.qualified} "
                        f"project={memory.project}, expected project={pack.manifest.project_id} "
                        f"namespace={pack.manifest.namespace}"
                    )
                    for memory in memories
                    if memory.project != pack.manifest.project_id
                    or memory.namespace != pack.manifest.namespace
                )
        registry = DomainRegistry(self.repository.workspace_root / "domains")
        issues.extend(registry.validate_graph())
        issues.extend(registry.validate_pack(pack))
        semantic_path = pack.resolve_canonical("semantic") / "anchors.yaml"
        try:
            anchors = SemanticAnchorRepository(semantic_path).list()
        except SemanticRepositoryError as error:
            issues.append(str(error))
        else:
            memory_by_ref = {memory.ref: memory for memory in memories}
            issues.extend(
                f"semantic anchor project boundary mismatch: {anchor.semantic_id}"
                for anchor in anchors
                if anchor.project != pack.ref
            )
            for anchor in anchors:
                for target_ref in anchor.target_refs:
                    target = memory_by_ref.get(target_ref)
                    if target is None:
                        issues.append(
                            f"semantic anchor target missing: "
                            f"{anchor.semantic_id} -> {target_ref.qualified}"
                        )
                    elif target.status is not MemoryStatus.ACTIVE or target.kind in {
                        MemoryKind.SOURCE,
                        MemoryKind.MAP,
                    }:
                        issues.append(
                            f"semantic anchor target is not active semantic knowledge: "
                            f"{anchor.semantic_id} -> {target_ref.qualified}"
                        )
                for source_ref in anchor.source_refs:
                    source = memory_by_ref.get(source_ref)
                    if source is None or source.kind is not MemoryKind.SOURCE:
                        issues.append(
                            f"semantic anchor source missing/not Source: "
                            f"{anchor.semantic_id} -> {source_ref.qualified}"
                        )
        runs = []
        try:
            runs = IntakeRunStore(pack.intake_root).list()
        except IntakeStoreError as error:
            issues.append(str(error))
        else:
            issues.extend(
                f"intake run project boundary mismatch: {run.run_id}"
                for run in runs
                if run.project != pack.ref
            )
        for proposal_path in sorted(pack.proposal_root.glob("*/proposal.yaml")):
            try:
                proposal = ProposalRepository.load(proposal_path)
            except ProposalRepositoryError as error:
                issues.append(str(error))
                continue
            if (
                proposal.project != pack.manifest.project_id
                or proposal.namespace != pack.manifest.namespace
            ):
                issues.append(f"proposal project boundary mismatch: {proposal.proposal_id}")
            issues.extend(
                f"proposal {proposal.proposal_id} {issue.code}: {issue.message}"
                for issue in ProposalValidator(pack.memory_root).validate(
                    proposal,
                    proposal_path,
                )
            )
        proposal_repository = ProposalRepository(pack.proposal_root)
        for run in runs:
            if run.proposal_id is not None:
                try:
                    linked_proposal = proposal_repository.get(run.proposal_id)
                except ProposalRepositoryError as error:
                    issues.append(str(error))
                else:
                    if linked_proposal is None:
                        issues.append(
                            f"intake run proposal missing: {run.run_id} -> {run.proposal_id}"
                        )
            if run.roadmap_update_path is not None:
                report_path = (pack.root / run.roadmap_update_path).resolve()
                try:
                    report_path.relative_to(pack.root.resolve())
                except ValueError:
                    issues.append(f"intake roadmap report escapes pack: {run.run_id}")
                else:
                    if not report_path.is_file():
                        issues.append(
                            f"intake roadmap report missing: {run.run_id} -> "
                            f"{run.roadmap_update_path}"
                        )
        if pack.roadmap_path is not None:
            if not pack.roadmap_path.is_file():
                issues.append(f"canonical.roadmap 파일이 없습니다: {pack.roadmap_path}")
            else:
                try:
                    RoadmapRepository(pack.roadmap_path).load()
                except RoadmapRepositoryError as error:
                    issues.append(str(error))
        try:
            roadmap_proposals = RoadmapProposalRepository(pack.roadmap_proposal_root).list()
        except RoadmapProposalRepositoryError as error:
            issues.append(str(error))
        else:
            if pack.roadmap_path is None and roadmap_proposals:
                issues.append("roadmap proposal이 있지만 canonical.roadmap이 없습니다.")
            elif pack.roadmap_path is not None and pack.roadmap_path.is_file():
                try:
                    roadmap_id = RoadmapRepository(pack.roadmap_path).load().roadmap_id
                except RoadmapRepositoryError:
                    pass
                else:
                    issues.extend(
                        f"roadmap proposal identity mismatch: {proposal.proposal_id}"
                        for proposal in roadmap_proposals
                        if proposal.roadmap_id != roadmap_id
                    )
        return ProjectValidation(pack.manifest.project_id, tuple(sorted(set(issues))))

    def rebuild(self, pack: ProjectPack) -> Path:
        """Recreate a deterministic local memory index from canonical assets."""
        validation = self.validate(pack)
        if not validation.valid:
            raise ValueError(
                "invalid Project Pack은 rebuild할 수 없습니다: " + "; ".join(validation.issues)
            )
        repository = MarkdownMemoryRepository(pack.memory_root)
        records = [
            {
                "ref": memory.ref.qualified,
                "project": memory.project,
                "kind": memory.kind.value,
                "status": memory.status.value,
                "title": memory.title,
                "revision": memory.revision,
                "path": str(Path(repository.path_for(memory.ref)).relative_to(pack.root)).replace(
                    "\\", "/"
                ),
            }
            for memory in repository.list(namespace=pack.manifest.namespace)
        ]
        index_root = pack.runtime_root / "index"
        index_root.mkdir(parents=True, exist_ok=True)
        path = index_root / "memory-index.json"
        payload = {
            "schema_version": 1,
            "project": pack.manifest.project_id,
            "namespace": pack.manifest.namespace,
            "records": records,
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return path

    def pack(self, pack: ProjectPack, output: Path) -> Path:
        """Create a deterministic archive that excludes runtime state and secrets."""
        validation = self.validate(pack)
        if not validation.valid:
            raise ValueError(
                "invalid Project Pack은 archive할 수 없습니다: " + "; ".join(validation.issues)
            )
        output = output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        if output in {
            pack.manifest_path.resolve(),
            (pack.manifest_path.parent / "domain.lock.yaml").resolve(),
        }:
            raise ValueError("archive output은 Project Pack manifest/lock을 덮어쓸 수 없습니다.")
        roots = {
            pack.manifest_path,
            pack.manifest_path.parent / "domain.lock.yaml",
        }
        empty_directories: set[Path] = set()
        for field_name in type(pack.manifest.canonical).model_fields:
            if getattr(pack.manifest.canonical, field_name) is None:
                continue
            path = pack.resolve_canonical(field_name)
            if path.is_file():
                roots.add(path)
            elif path.is_dir():
                files = [
                    item
                    for item in path.rglob("*")
                    if item.is_file()
                    and item.resolve() != output
                    and not item.is_symlink()
                    and self._is_relative_to(item, pack.root)
                    and not self._is_secret_like(item.relative_to(pack.root))
                    and not self._is_relative_to(item, pack.runtime_root)
                ]
                roots.update(files)
                if not files:
                    empty_directories.add(path)
        members = sorted(
            (
                path
                for path in roots
                if path.exists()
                and path.resolve() != output
                and not path.is_symlink()
                and self._is_relative_to(path, pack.root)
                and not self._is_secret_like(path.relative_to(pack.root))
                and not self._is_relative_to(path, pack.runtime_root)
            ),
            key=lambda item: str(item.relative_to(pack.root)),
        )
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(
                empty_directories,
                key=lambda item: str(item.relative_to(pack.root)),
            ):
                relative = str(path.relative_to(pack.root)).replace("\\", "/").rstrip("/") + "/"
                info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
                info.external_attr = (0o40755 << 16) | 0x10
                archive.writestr(info, b"")
            for path in members:
                relative = str(path.relative_to(pack.root)).replace("\\", "/")
                info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, path.read_bytes())
        return output

    @staticmethod
    def _is_relative_to(path: Path, root: Path) -> bool:
        try:
            path.resolve().relative_to(root.resolve())
        except ValueError:
            return False
        return True

    @staticmethod
    def _is_secret_like(path: Path) -> bool:
        for part in path.parts:
            name = part.casefold()
            if (
                name == ".env"
                or name.startswith(".env.")
                or "credential" in name
                or name.endswith(".key")
                or name.endswith(".pem")
                or name in {"id_rsa", "id_ed25519"}
            ):
                return True
        return False
