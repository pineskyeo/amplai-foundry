"""Write-free legacy Proposal snapshot and deterministic migration planning."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath

import yaml
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator
from yaml.events import (
    AliasEvent,
    MappingEndEvent,
    MappingStartEvent,
    SequenceEndEvent,
    SequenceStartEvent,
)

from amplai_foundry.domain.identity import ProjectRef
from amplai_foundry.governance.definitions import (
    ApplyInputDescriptor,
    ProposalDefinitionManifest,
    canonicalize_definition,
)
from amplai_foundry.governance.models import Digest, ProposalRef
from amplai_foundry.governance.object_store import (
    DefinitionObjectRef,
    DefinitionObjectStoreError,
    ImmutableDefinitionObjectStore,
    sha256_digest,
)
from amplai_foundry.governance.store import GovernanceStore, governance_transaction
from amplai_foundry.proposals.models import Proposal, ProposalStatus

_PROPOSAL_ID = re.compile(r"^PROP-[0-9]{8}-[A-F0-9]{8}$")
_FREEZE_ID = re.compile(r"^MFR-[A-F0-9]{16}$")
_GIT_REVISION = re.compile(r"^[0-9a-f]{7,64}$")


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _digest(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


class LegacyMigrationScanError(RuntimeError):
    """Legacy input cannot be represented by a safe deterministic plan."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class LegacyMigrationScanConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    maximum_files: int = Field(default=10_000, ge=1, le=1_000_000)
    maximum_entries: int = Field(default=100_000, ge=1, le=10_000_000)
    maximum_file_bytes: int = Field(default=16 * 1024 * 1024, ge=1)
    maximum_total_bytes: int = Field(default=512 * 1024 * 1024, ge=1)
    maximum_yaml_depth: int = Field(default=64, ge=1, le=1_024)
    maximum_yaml_events: int = Field(default=100_000, ge=1)
    maximum_yaml_aliases: int = Field(default=100, ge=0)
    maximum_backup_bytes: int = Field(default=2 * 1024 * 1024 * 1024, ge=1)


class LegacyMutationFreeze(BaseModel):
    """Operator evidence that the legacy Proposal tree is no longer mutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    freeze_id: str
    project_ref: ProjectRef
    source_root: str = Field(min_length=1)
    base_revision: str
    actor_id: str = Field(min_length=1, max_length=128)
    frozen_at: AwareDatetime
    reason: str = Field(min_length=1, max_length=512)

    def model_post_init(self, __context: object) -> None:
        if _FREEZE_ID.fullmatch(self.freeze_id) is None:
            raise ValueError("freeze_id 형식이 올바르지 않습니다.")
        source_root = Path(self.source_root)
        if not source_root.is_absolute() or str(source_root) != self.source_root:
            raise ValueError("source_root는 normalized absolute path여야 합니다.")
        if _GIT_REVISION.fullmatch(self.base_revision) is None:
            raise ValueError("base_revision 형식이 올바르지 않습니다.")


class LegacySnapshotFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    relative_path: str = Field(min_length=1)
    byte_length: int = Field(ge=0)
    content_digest: Digest

    @model_validator(mode="after")
    def validate_relative_path(self) -> LegacySnapshotFile:
        path = PurePosixPath(self.relative_path)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError("relative_path가 canonical relative path가 아닙니다.")
        return self


class LegacyProposalSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: str = Field(pattern=r"^MPS-[A-F0-9]{16}$")
    project_ref: ProjectRef
    freeze: LegacyMutationFreeze
    files: tuple[LegacySnapshotFile, ...]
    total_bytes: int = Field(ge=0)
    snapshot_digest: Digest

    @model_validator(mode="after")
    def validate_derived_identity(self) -> LegacyProposalSnapshot:
        paths = tuple(item.relative_path for item in self.files)
        if paths != tuple(sorted(paths, key=lambda value: value.encode("utf-8"))):
            raise ValueError("snapshot file order가 canonical하지 않습니다.")
        if len(paths) != len(set(paths)) or self.total_bytes != sum(
            item.byte_length for item in self.files
        ):
            raise ValueError("snapshot file identity 또는 byte count가 일치하지 않습니다.")
        preimage = {
            "files": [entry.model_dump(mode="json") for entry in self.files],
            "project_ref": self.project_ref.model_dump(mode="json"),
        }
        digest = _digest(_canonical_json(preimage))
        if self.snapshot_digest != digest or self.snapshot_id != f"MPS-{digest[-16:].upper()}":
            raise ValueError("snapshot derived identity가 일치하지 않습니다.")
        return self


class LegacyApprovalDisposition(StrEnum):
    NOT_REQUIRED = "not_required"
    LEGACY_AUDIT_PRESENT = "legacy_audit_present"
    SYNTHETIC_REQUIRED = "synthetic_required"


class LegacyTargetStatus(StrEnum):
    DRAFT = "draft"
    REVIEWED = "reviewed"
    CHANGES_REQUESTED = "changes_requested"
    APPROVED = "approved"
    APPLIED = "applied"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    LEGACY_APPROVAL_REVIEW_REQUIRED = "legacy_approval_review_required"


class LegacyProposalPlanItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal_ref: ProposalRef
    source_status: ProposalStatus
    source_revision: int = Field(ge=1)
    target_status: LegacyTargetStatus
    content_revision: int = Field(ge=1)
    state_revision: int = Field(ge=1)
    decision_epoch: int = Field(ge=1)
    proposal_artifact_digest: Digest
    approval_disposition: LegacyApprovalDisposition
    legacy_git_revision: str | None = None
    source_ids: tuple[str, ...] = Field(min_length=1)


class LegacyProposalMigrationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_id: str = Field(pattern=r"^MPL-[A-F0-9]{16}$")
    mapping_policy_version: int = Field(default=1, ge=1)
    project_ref: ProjectRef
    freeze: LegacyMutationFreeze
    snapshot_id: str = Field(pattern=r"^MPS-[A-F0-9]{16}$")
    snapshot_digest: Digest
    base_revision: str
    validation_policy_ref: str = Field(min_length=1, max_length=256)
    proposals: tuple[LegacyProposalPlanItem, ...]
    plan_digest: Digest

    @model_validator(mode="after")
    def validate_derived_identity(self) -> LegacyProposalMigrationPlan:
        refs = tuple(item.proposal_ref for item in self.proposals)
        if len(refs) != len(set(refs)):
            raise ValueError("migration plan ProposalRef는 고유해야 합니다.")
        if any(ref.project_ref != self.project_ref for ref in refs):
            raise ValueError("migration plan Project scope가 일치하지 않습니다.")
        if self.freeze.project_ref != self.project_ref:
            raise ValueError("migration plan freeze Project scope가 일치하지 않습니다.")
        if self.freeze.base_revision != self.base_revision:
            raise ValueError("migration plan freeze base revision이 일치하지 않습니다.")
        if self.snapshot_id != f"MPS-{self.snapshot_digest[-16:].upper()}":
            raise ValueError("migration plan snapshot identity가 일치하지 않습니다.")
        preimage = {
            "base_revision": self.base_revision,
            "freeze": self.freeze.model_dump(mode="json"),
            "mapping_policy_version": self.mapping_policy_version,
            "project_ref": self.project_ref.model_dump(mode="json"),
            "proposals": [item.model_dump(mode="json") for item in self.proposals],
            "snapshot_digest": self.snapshot_digest,
            "snapshot_id": self.snapshot_id,
            "validation_policy_ref": self.validation_policy_ref,
        }
        digest = _digest(_canonical_json(preimage))
        if self.plan_digest != digest or self.plan_id != f"MPL-{digest[-16:].upper()}":
            raise ValueError("migration plan derived identity가 일치하지 않습니다.")
        return self


class LegacyMigrationBackupEvidence(BaseModel):
    """Operator-created Project Pack and Governance DB backup evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    project_ref: ProjectRef
    project_pack_backup_path: str = Field(min_length=1)
    project_pack_backup_digest: Digest
    governance_backup_path: str = Field(min_length=1)
    governance_backup_digest: Digest
    actor_id: str = Field(min_length=1, max_length=128)
    captured_at: AwareDatetime

    @model_validator(mode="after")
    def validate_paths(self) -> LegacyMigrationBackupEvidence:
        paths = (
            Path(self.project_pack_backup_path),
            Path(self.governance_backup_path),
        )
        if any(not path.is_absolute() or str(path) != str(path.absolute()) for path in paths):
            raise ValueError("backup path는 normalized absolute path여야 합니다.")
        if paths[0] == paths[1]:
            raise ValueError("Project Pack과 Governance backup path는 달라야 합니다.")
        return self


class LegacyMigrationImportResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    migration_id: str = Field(pattern=r"^MPL-[A-F0-9]{16}$")
    project_ref: ProjectRef
    snapshot_id: str = Field(pattern=r"^MPS-[A-F0-9]{16}$")
    plan_digest: Digest
    status: str = Field(pattern=r"^(prepared|state_imported)$")
    proposal_count: int = Field(ge=1)
    definition_digests: tuple[Digest, ...]


@dataclass(frozen=True, slots=True)
class _PreparedLegacyDefinition:
    plan_item: LegacyProposalPlanItem
    proposal: Proposal
    object_ref: DefinitionObjectRef


class LegacyProposalDryRunService:
    """Read a frozen legacy tree without writing files, objects, or Governance state."""

    def __init__(
        self,
        project_root: Path,
        project_ref: ProjectRef,
        *,
        config: LegacyMigrationScanConfig | None = None,
        after_file_read: Callable[[Path], None] | None = None,
    ) -> None:
        self.project_root = project_root.absolute()
        self.project_ref = project_ref
        self.config = config or LegacyMigrationScanConfig()
        self._after_file_read = after_file_read or (lambda _path: None)
        self.legacy_root = self.project_root / ".amplai" / "proposals"

    def create_plan(
        self,
        *,
        freeze: LegacyMutationFreeze,
        base_revision: str,
        validation_policy_ref: str,
    ) -> LegacyProposalMigrationPlan:
        if _GIT_REVISION.fullmatch(base_revision) is None:
            raise LegacyMigrationScanError("LEGACY_BASE_REVISION_INVALID")
        if not validation_policy_ref.strip():
            raise LegacyMigrationScanError("LEGACY_VALIDATION_POLICY_INVALID")
        self._validate_freeze(freeze, base_revision=base_revision)
        snapshot, payloads = self._snapshot(freeze)
        items = tuple(
            self._plan_item(relative_path, payloads[relative_path], snapshot)
            for relative_path in sorted(payloads)
            if relative_path.endswith("/proposal.yaml")
        )
        if not items:
            raise LegacyMigrationScanError("LEGACY_PROPOSAL_NOT_FOUND")
        preimage = {
            "base_revision": base_revision,
            "freeze": freeze.model_dump(mode="json"),
            "mapping_policy_version": 1,
            "project_ref": self.project_ref.model_dump(mode="json"),
            "proposals": [item.model_dump(mode="json") for item in items],
            "snapshot_digest": snapshot.snapshot_digest,
            "snapshot_id": snapshot.snapshot_id,
            "validation_policy_ref": validation_policy_ref,
        }
        plan_digest = _digest(_canonical_json(preimage))
        return LegacyProposalMigrationPlan(
            plan_id=f"MPL-{plan_digest[-16:].upper()}",
            project_ref=self.project_ref,
            freeze=freeze,
            snapshot_id=snapshot.snapshot_id,
            snapshot_digest=snapshot.snapshot_digest,
            base_revision=base_revision,
            validation_policy_ref=validation_policy_ref,
            proposals=items,
            plan_digest=plan_digest,
        )

    def create_snapshot(self, freeze: LegacyMutationFreeze) -> LegacyProposalSnapshot:
        snapshot, _payloads = self._snapshot(freeze)
        return snapshot

    def _snapshot(
        self,
        freeze: LegacyMutationFreeze,
    ) -> tuple[LegacyProposalSnapshot, dict[str, bytes]]:
        self._validate_freeze(freeze)
        root_identities = self._validate_roots()
        before = self._enumerate_files()
        if len(before) > self.config.maximum_files:
            raise LegacyMigrationScanError("LEGACY_SNAPSHOT_FILE_LIMIT")
        payloads: dict[str, bytes] = {}
        entries: list[LegacySnapshotFile] = []
        total_bytes = 0
        for relative_path, expected_identity in before:
            if total_bytes + expected_identity[2] > self.config.maximum_total_bytes:
                raise LegacyMigrationScanError("LEGACY_SNAPSHOT_TOTAL_LIMIT")
            path = self.legacy_root / relative_path
            payload = self._read_stable(path, expected_identity)
            self._after_file_read(path)
            total_bytes += len(payload)
            if total_bytes > self.config.maximum_total_bytes:
                raise LegacyMigrationScanError("LEGACY_SNAPSHOT_TOTAL_LIMIT")
            payloads[relative_path] = payload
            entries.append(
                LegacySnapshotFile(
                    relative_path=relative_path,
                    byte_length=len(payload),
                    content_digest=_digest(payload),
                )
            )
        after = self._enumerate_files()
        if before != after or root_identities != self._root_identities():
            raise LegacyMigrationScanError("LEGACY_SNAPSHOT_MUTATED")
        preimage = {
            "files": [entry.model_dump(mode="json") for entry in entries],
            "project_ref": self.project_ref.model_dump(mode="json"),
        }
        digest = _digest(_canonical_json(preimage))
        return (
            LegacyProposalSnapshot(
                snapshot_id=f"MPS-{digest[-16:].upper()}",
                project_ref=self.project_ref,
                freeze=freeze,
                files=tuple(entries),
                total_bytes=total_bytes,
                snapshot_digest=digest,
            ),
            payloads,
        )

    def _plan_item(
        self,
        relative_path: str,
        payload: bytes,
        snapshot: LegacyProposalSnapshot,
    ) -> LegacyProposalPlanItem:
        proposal_id = Path(relative_path).parent.name
        if (
            _PROPOSAL_ID.fullmatch(proposal_id) is None
            or relative_path != f"{proposal_id}/proposal.yaml"
        ):
            raise LegacyMigrationScanError("LEGACY_PROPOSAL_PATH_INVALID")
        try:
            self._validate_yaml_structure(payload)
            raw = yaml.safe_load(payload)
            proposal = Proposal.model_validate(raw)
        except (UnicodeDecodeError, yaml.YAMLError, ValidationError, RecursionError) as error:
            raise LegacyMigrationScanError("LEGACY_PROPOSAL_INVALID") from error
        if proposal.proposal_id != proposal_id:
            raise LegacyMigrationScanError("LEGACY_PROPOSAL_PATH_MISMATCH")
        if (
            proposal.project != self.project_ref.project_id
            or proposal.namespace != self.project_ref.namespace
        ):
            raise LegacyMigrationScanError("LEGACY_PROJECT_SCOPE_MISMATCH")
        audit_path = f"{proposal_id}/approval-audit.json"
        approval = self._approval_disposition(proposal.status, audit_path, snapshot)
        target_status = LegacyTargetStatus(
            "legacy_approval_review_required"
            if approval is LegacyApprovalDisposition.SYNTHETIC_REQUIRED
            else proposal.status.value
        )
        return LegacyProposalPlanItem(
            proposal_ref=ProposalRef(project_ref=self.project_ref, proposal_id=proposal_id),
            source_status=proposal.status,
            source_revision=proposal.revision,
            target_status=target_status,
            content_revision=1,
            state_revision=self._state_revision(proposal.status, target_status),
            decision_epoch=1,
            proposal_artifact_digest=_digest(payload),
            approval_disposition=approval,
            legacy_git_revision=proposal.git_commit_sha,
            source_ids=tuple(proposal.source_ids),
        )

    @staticmethod
    def _approval_disposition(
        status: ProposalStatus,
        audit_path: str,
        snapshot: LegacyProposalSnapshot,
    ) -> LegacyApprovalDisposition:
        if status not in {ProposalStatus.APPROVED, ProposalStatus.APPLIED}:
            return LegacyApprovalDisposition.NOT_REQUIRED
        if any(item.relative_path == audit_path for item in snapshot.files):
            return LegacyApprovalDisposition.LEGACY_AUDIT_PRESENT
        return LegacyApprovalDisposition.SYNTHETIC_REQUIRED

    @staticmethod
    def _state_revision(status: ProposalStatus, target_status: str) -> int:
        if target_status == "legacy_approval_review_required":
            return 3
        return {
            ProposalStatus.DRAFT: 1,
            ProposalStatus.REVIEWED: 2,
            ProposalStatus.CHANGES_REQUESTED: 3,
            ProposalStatus.APPROVED: 3,
            ProposalStatus.APPLIED: 5,
            ProposalStatus.REJECTED: 3,
            ProposalStatus.SUPERSEDED: 2,
        }[status]

    def _validate_roots(self) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
        try:
            if self.project_root.resolve(strict=True) != self.project_root:
                raise LegacyMigrationScanError("LEGACY_PROJECT_ROOT_SYMLINK")
            project_stat = self.project_root.lstat()
            legacy_stat = self.legacy_root.lstat()
            resolved_legacy = self.legacy_root.resolve(strict=True)
        except LegacyMigrationScanError:
            raise
        except (OSError, RuntimeError) as error:
            raise LegacyMigrationScanError("LEGACY_SOURCE_ROOT_INVALID") from error
        if not stat.S_ISDIR(project_stat.st_mode) or not stat.S_ISDIR(legacy_stat.st_mode):
            raise LegacyMigrationScanError("LEGACY_SOURCE_ROOT_INVALID")
        if resolved_legacy.parent.parent != self.project_root:
            raise LegacyMigrationScanError("LEGACY_SOURCE_ROOT_ESCAPE")
        return self._root_identities()

    def _validate_freeze(
        self,
        freeze: LegacyMutationFreeze,
        *,
        base_revision: str | None = None,
    ) -> None:
        if freeze.project_ref != self.project_ref:
            raise LegacyMigrationScanError("LEGACY_FREEZE_PROJECT_MISMATCH")
        if freeze.source_root != str(self.legacy_root):
            raise LegacyMigrationScanError("LEGACY_FREEZE_SOURCE_ROOT_MISMATCH")
        if base_revision is not None and freeze.base_revision != base_revision:
            raise LegacyMigrationScanError("LEGACY_FREEZE_BASE_REVISION_MISMATCH")

    def _root_identities(self) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
        try:
            project = self.project_root.lstat()
            legacy = self.legacy_root.lstat()
        except OSError as error:
            raise LegacyMigrationScanError("LEGACY_SOURCE_ROOT_INVALID") from error
        return (
            (project.st_dev, project.st_ino, project.st_ctime_ns),
            (legacy.st_dev, legacy.st_ino, legacy.st_ctime_ns),
        )

    def _enumerate_files(self) -> tuple[tuple[str, tuple[int, int, int, int, int]], ...]:
        entries: list[tuple[str, tuple[int, int, int, int, int]]] = []
        pending = [self.legacy_root]
        discovered_entries = 0
        discovered_files = 0
        try:
            while pending:
                directory_path = pending.pop()
                with os.scandir(directory_path) as iterator:
                    for child in iterator:
                        discovered_entries += 1
                        if discovered_entries > self.config.maximum_entries:
                            raise LegacyMigrationScanError("LEGACY_SNAPSHOT_ENTRY_LIMIT")
                        candidate = directory_path / child.name
                        metadata = child.stat(follow_symlinks=False)
                        if stat.S_ISLNK(metadata.st_mode):
                            raise LegacyMigrationScanError("LEGACY_SOURCE_SYMLINK")
                        if stat.S_ISDIR(metadata.st_mode):
                            relative_directory = directory_path.relative_to(self.legacy_root)
                            if not (
                                len(relative_directory.parts) == 1
                                and child.name in {"definitions", "inputs"}
                            ):
                                pending.append(candidate)
                            continue
                        if not stat.S_ISREG(metadata.st_mode):
                            raise LegacyMigrationScanError("LEGACY_SOURCE_NOT_REGULAR")
                        discovered_files += 1
                        if discovered_files > self.config.maximum_files:
                            raise LegacyMigrationScanError("LEGACY_SNAPSHOT_FILE_LIMIT")
                        relative = candidate.relative_to(self.legacy_root).as_posix()
                        entries.append(
                            (
                                relative,
                                (
                                    metadata.st_dev,
                                    metadata.st_ino,
                                    metadata.st_size,
                                    metadata.st_mtime_ns,
                                    metadata.st_ctime_ns,
                                ),
                            )
                        )
        except OSError as error:
            raise LegacyMigrationScanError("LEGACY_SOURCE_READ_FAILED") from error
        entries.sort(key=lambda item: item[0].encode("utf-8"))
        return tuple(entries)

    def _read_stable(
        self,
        path: Path,
        expected_identity: tuple[int, int, int, int, int],
    ) -> bytes:
        descriptor = -1
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            before = os.fstat(descriptor)
            identity = (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            if identity != expected_identity or not stat.S_ISREG(before.st_mode):
                raise LegacyMigrationScanError("LEGACY_SNAPSHOT_MUTATED")
            if before.st_size > self.config.maximum_file_bytes:
                raise LegacyMigrationScanError("LEGACY_SNAPSHOT_FILE_SIZE_LIMIT")
            chunks: list[bytes] = []
            remaining = self.config.maximum_file_bytes + 1
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            after = os.fstat(descriptor)
            if len(payload) > self.config.maximum_file_bytes:
                raise LegacyMigrationScanError("LEGACY_SNAPSHOT_FILE_SIZE_LIMIT")
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise LegacyMigrationScanError("LEGACY_SNAPSHOT_MUTATED")
            return payload
        except LegacyMigrationScanError:
            raise
        except OSError as error:
            raise LegacyMigrationScanError("LEGACY_SOURCE_READ_FAILED") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _validate_yaml_structure(self, payload: bytes) -> None:
        depth = 0
        events = 0
        aliases = 0
        try:
            for event in yaml.parse(payload):
                events += 1
                if events > self.config.maximum_yaml_events:
                    raise LegacyMigrationScanError("LEGACY_PROPOSAL_STRUCTURE_LIMIT")
                if isinstance(event, (MappingStartEvent, SequenceStartEvent)):
                    depth += 1
                    if depth > self.config.maximum_yaml_depth:
                        raise LegacyMigrationScanError("LEGACY_PROPOSAL_STRUCTURE_LIMIT")
                elif isinstance(event, (MappingEndEvent, SequenceEndEvent)):
                    depth -= 1
                elif isinstance(event, AliasEvent):
                    aliases += 1
                    if aliases > self.config.maximum_yaml_aliases:
                        raise LegacyMigrationScanError("LEGACY_PROPOSAL_STRUCTURE_LIMIT")
        except LegacyMigrationScanError:
            raise
        except (yaml.YAMLError, RecursionError) as error:
            raise LegacyMigrationScanError("LEGACY_PROPOSAL_INVALID") from error


class LegacyProposalImportService:
    """Prepare durable backup evidence and atomically import qualified Proposal state."""

    def __init__(
        self,
        dry_run: LegacyProposalDryRunService,
        store: GovernanceStore,
        objects: ImmutableDefinitionObjectStore,
    ) -> None:
        if objects.project_ref != dry_run.project_ref:
            raise ValueError("legacy import object store Project scope가 일치하지 않습니다.")
        self.dry_run = dry_run
        self.store = store
        self.objects = objects

    def prepare(
        self,
        plan: LegacyProposalMigrationPlan,
        backup: LegacyMigrationBackupEvidence,
    ) -> LegacyMigrationImportResult:
        self._validate_scope(plan, backup)
        self._verify_backup_evidence(backup)
        self._read_plan_payloads(plan)
        values = self._root_values(plan, backup)
        try:
            with self.store.connect() as connection, governance_transaction(connection):
                row = self._select_root(connection, plan.plan_id)
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO governance_legacy_migrations(
                            migration_id, project_namespace, project_id, freeze_id,
                            snapshot_id, snapshot_digest, plan_digest,
                            mapping_policy_version, base_revision, validation_policy_ref,
                            project_pack_backup_path, project_pack_backup_digest,
                            governance_backup_path, governance_backup_digest,
                            proposal_count, status, prepared_at, state_imported_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                  'prepared', ?, NULL)
                        """,
                        (*values, backup.captured_at.isoformat()),
                    )
                elif tuple(
                    str(value) if value is not None else None for value in row[:15]
                ) != tuple(str(value) for value in values):
                    raise LegacyMigrationScanError("LEGACY_MIGRATION_IDENTITY_CONFLICT")
        except LegacyMigrationScanError:
            raise
        except sqlite3.Error as error:
            raise LegacyMigrationScanError("LEGACY_MIGRATION_PREPARE_FAILED") from error
        return self._result(plan)

    def import_state(
        self,
        plan: LegacyProposalMigrationPlan,
        backup: LegacyMigrationBackupEvidence,
    ) -> LegacyMigrationImportResult:
        self.prepare(plan, backup)
        payloads = self._read_plan_payloads(plan)
        try:
            definitions = self._prepare_definition_objects(plan, payloads)
        except DefinitionObjectStoreError as error:
            raise LegacyMigrationScanError("LEGACY_DEFINITION_INTEGRITY_FAILURE") from error
        self._read_plan_payloads(plan)
        try:
            with self.store.connect() as connection, governance_transaction(connection):
                root = self._select_root(connection, plan.plan_id)
                if root is None:
                    raise LegacyMigrationScanError("LEGACY_MIGRATION_NOT_PREPARED")
                if str(root[15]) == "state_imported":
                    self._verify_imported_rows(connection, plan, definitions)
                elif str(root[15]) == "prepared":
                    for prepared in definitions:
                        self._insert_proposal_state(connection, plan, prepared)
                    updated = connection.execute(
                        """
                        UPDATE governance_legacy_migrations
                        SET status = 'state_imported',
                            state_imported_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                        WHERE migration_id = ? AND status = 'prepared'
                        """,
                        (plan.plan_id,),
                    )
                    if updated.rowcount != 1:
                        raise LegacyMigrationScanError("LEGACY_MIGRATION_STATE_CONFLICT")
                else:
                    raise LegacyMigrationScanError("LEGACY_MIGRATION_STATE_CONFLICT")
        except LegacyMigrationScanError:
            raise
        except sqlite3.Error as error:
            raise LegacyMigrationScanError("LEGACY_MIGRATION_STATE_CONFLICT") from error
        return self._result(plan)

    def _validate_scope(
        self,
        plan: LegacyProposalMigrationPlan,
        backup: LegacyMigrationBackupEvidence,
    ) -> None:
        if plan.project_ref != self.dry_run.project_ref or backup.project_ref != plan.project_ref:
            raise LegacyMigrationScanError("LEGACY_MIGRATION_PROJECT_MISMATCH")
        if plan.freeze.project_ref != plan.project_ref:
            raise LegacyMigrationScanError("LEGACY_MIGRATION_PROJECT_MISMATCH")

    def _verify_backup_evidence(self, backup: LegacyMigrationBackupEvidence) -> None:
        project_pack = Path(backup.project_pack_backup_path)
        governance = Path(backup.governance_backup_path)
        if governance.resolve(strict=False) == self.store.path:
            raise LegacyMigrationScanError("LEGACY_GOVERNANCE_BACKUP_INVALID")
        self._verify_backup_file(
            project_pack,
            backup.project_pack_backup_digest,
            "LEGACY_PROJECT_PACK_BACKUP_INVALID",
        )
        self._verify_backup_file(
            governance,
            backup.governance_backup_digest,
            "LEGACY_GOVERNANCE_BACKUP_INVALID",
        )
        try:
            with sqlite3.connect(
                f"{governance.as_uri()}?mode=ro&immutable=1", uri=True
            ) as connection:
                integrity = connection.execute("PRAGMA integrity_check").fetchone()
                if integrity != ("ok",):
                    raise LegacyMigrationScanError("LEGACY_GOVERNANCE_BACKUP_INVALID")
                version = self.store.migration_runner.verify(connection)
                self.store.migration_runner.verify_schema(connection, version)
            with self.store.connect() as live_connection:
                live_version = self.store.migration_runner.verify(live_connection)
            if version != live_version:
                raise LegacyMigrationScanError("LEGACY_GOVERNANCE_BACKUP_INVALID")
        except LegacyMigrationScanError:
            raise
        except (OSError, sqlite3.Error) as error:
            raise LegacyMigrationScanError("LEGACY_GOVERNANCE_BACKUP_INVALID") from error

    def _verify_backup_file(self, path: Path, expected_digest: str, error_code: str) -> None:
        descriptor = -1
        try:
            if path.resolve(strict=True) != path:
                raise LegacyMigrationScanError(error_code)
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise LegacyMigrationScanError(error_code)
            if before.st_size > self.dry_run.config.maximum_backup_bytes:
                raise LegacyMigrationScanError(error_code)
            digest = hashlib.sha256()
            remaining = self.dry_run.config.maximum_backup_bytes + 1
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                digest.update(chunk)
                remaining -= len(chunk)
            after = os.fstat(descriptor)
            identity_before = (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            identity_after = (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            actual_digest = f"sha256:{digest.hexdigest()}"
            if (
                identity_before != identity_after
                or before.st_size > self.dry_run.config.maximum_backup_bytes
                or actual_digest != expected_digest
            ):
                raise LegacyMigrationScanError(error_code)
        except LegacyMigrationScanError:
            raise
        except (OSError, RuntimeError) as error:
            raise LegacyMigrationScanError(error_code) from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _read_plan_payloads(self, plan: LegacyProposalMigrationPlan) -> dict[str, bytes]:
        expected = self.dry_run.create_plan(
            freeze=plan.freeze,
            base_revision=plan.base_revision,
            validation_policy_ref=plan.validation_policy_ref,
        )
        if expected != plan:
            raise LegacyMigrationScanError("LEGACY_MIGRATION_PLAN_STALE")
        snapshot, payloads = self.dry_run._snapshot(plan.freeze)
        if (
            snapshot.snapshot_id != plan.snapshot_id
            or snapshot.snapshot_digest != plan.snapshot_digest
        ):
            raise LegacyMigrationScanError("LEGACY_MIGRATION_PLAN_STALE")
        return payloads

    def _prepare_definition_objects(
        self,
        plan: LegacyProposalMigrationPlan,
        payloads: dict[str, bytes],
    ) -> tuple[_PreparedLegacyDefinition, ...]:
        prepared: list[_PreparedLegacyDefinition] = []
        for item in plan.proposals:
            proposal_path = f"{item.proposal_ref.proposal_id}/proposal.yaml"
            proposal = Proposal.model_validate(yaml.safe_load(payloads[proposal_path]))
            if proposal.created_at.tzinfo is None or proposal.created_at.utcoffset() is None:
                raise LegacyMigrationScanError("LEGACY_PROPOSAL_TIMESTAMP_INVALID")
            descriptors: list[ApplyInputDescriptor] = []
            prefix = f"{item.proposal_ref.proposal_id}/"
            for relative_path in sorted(payloads, key=lambda value: value.encode("utf-8")):
                if not relative_path.startswith(prefix):
                    continue
                object_bytes = payloads[relative_path]
                object_digest = sha256_digest(object_bytes)
                object_ref = self.objects.put_input_object(
                    item.proposal_ref,
                    object_bytes,
                    object_digest,
                )
                if self.objects.get_input_object(item.proposal_ref, object_digest) != object_bytes:
                    raise LegacyMigrationScanError("LEGACY_INPUT_INTEGRITY_FAILURE")
                descriptors.append(
                    ApplyInputDescriptor(
                        logical_name=relative_path.removeprefix(prefix),
                        object_digest=object_ref.digest,
                        media_type=self._media_type(relative_path),
                    )
                )
            manifest = ProposalDefinitionManifest(
                proposal_ref=item.proposal_ref,
                operations=tuple(
                    operation.model_dump(mode="json") for operation in proposal.operations
                ),
                apply_inputs=tuple(descriptors),
                preconditions=(
                    {
                        "legacy_migration": {
                            "mapping_policy_version": plan.mapping_policy_version,
                            "proposal_artifact_digest": item.proposal_artifact_digest,
                            "snapshot_digest": plan.snapshot_digest,
                            "source_revision": item.source_revision,
                            "source_status": item.source_status.value,
                        }
                    },
                ),
                base_revision=plan.base_revision,
                validation_policy_ref=plan.validation_policy_ref,
            )
            canonical = canonicalize_definition(manifest)
            object_ref = self.objects.put_definition_object(
                item.proposal_ref,
                canonical.canonical_bytes,
                canonical.digest,
            )
            if (
                self.objects.get_definition_object(item.proposal_ref, canonical.digest)
                != canonical.canonical_bytes
            ):
                raise LegacyMigrationScanError("LEGACY_DEFINITION_INTEGRITY_FAILURE")
            prepared.append(_PreparedLegacyDefinition(item, proposal, object_ref))
        return tuple(prepared)

    @staticmethod
    def _media_type(relative_path: str) -> str:
        suffix = Path(relative_path).suffix.lower()
        return {
            ".json": "application/json",
            ".md": "text/markdown",
            ".yaml": "application/yaml",
            ".yml": "application/yaml",
        }.get(suffix, "application/octet-stream")

    @staticmethod
    def _runtime_status(target: LegacyTargetStatus) -> str:
        if target is LegacyTargetStatus.LEGACY_APPROVAL_REVIEW_REQUIRED:
            return "changes_requested"
        return target.value

    def _insert_proposal_state(
        self,
        connection: sqlite3.Connection,
        plan: LegacyProposalMigrationPlan,
        prepared: _PreparedLegacyDefinition,
    ) -> None:
        item = prepared.plan_item
        proposal = prepared.proposal
        identity = (
            item.proposal_ref.project_ref.namespace,
            item.proposal_ref.project_ref.project_id,
            item.proposal_ref.proposal_id,
        )
        if (
            connection.execute(
                """
            SELECT 1 FROM governance_active_proposals
            WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?
            """,
                identity,
            ).fetchone()
            is not None
        ):
            raise LegacyMigrationScanError("LEGACY_MIGRATION_PROPOSAL_CONFLICT")
        timestamp = proposal.created_at.isoformat()
        runtime_status = self._runtime_status(item.target_status)
        connection.execute(
            """
            INSERT INTO governance_active_proposals(
                project_namespace, project_id, proposal_id, active_definition_digest,
                content_revision, state_revision, decision_epoch, status,
                created_at, updated_at, applied_revision
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                *identity,
                prepared.object_ref.digest,
                item.content_revision,
                item.state_revision,
                item.decision_epoch,
                runtime_status,
                timestamp,
                timestamp,
                item.legacy_git_revision if runtime_status == "applied" else None,
            ),
        )
        connection.execute(
            """
            INSERT INTO governance_definition_revisions(
                project_namespace, project_id, proposal_id, content_revision,
                definition_digest, previous_definition_digest, activated_from_status,
                activated_at
            ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?)
            """,
            (*identity, item.content_revision, prepared.object_ref.digest, timestamp),
        )
        connection.execute(
            """
            INSERT INTO governance_legacy_migration_items(
                migration_id, project_namespace, project_id, proposal_id,
                source_status, source_revision, target_status, definition_digest,
                proposal_artifact_digest, content_revision, state_revision,
                decision_epoch, approval_disposition, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan.plan_id,
                *identity,
                item.source_status.value,
                item.source_revision,
                item.target_status.value,
                prepared.object_ref.digest,
                item.proposal_artifact_digest,
                item.content_revision,
                item.state_revision,
                item.decision_epoch,
                item.approval_disposition.value,
                timestamp,
            ),
        )

    def _verify_imported_rows(
        self,
        connection: sqlite3.Connection,
        plan: LegacyProposalMigrationPlan,
        definitions: tuple[_PreparedLegacyDefinition, ...],
    ) -> None:
        rows = connection.execute(
            """
            SELECT proposal_id, source_status, source_revision, target_status,
                   definition_digest, proposal_artifact_digest, content_revision,
                   state_revision, decision_epoch, approval_disposition
            FROM governance_legacy_migration_items
            WHERE migration_id = ?
            ORDER BY proposal_id
            """,
            (plan.plan_id,),
        ).fetchall()
        expected = tuple(
            (
                prepared.plan_item.proposal_ref.proposal_id,
                prepared.plan_item.source_status.value,
                prepared.plan_item.source_revision,
                prepared.plan_item.target_status.value,
                prepared.object_ref.digest,
                prepared.plan_item.proposal_artifact_digest,
                prepared.plan_item.content_revision,
                prepared.plan_item.state_revision,
                prepared.plan_item.decision_epoch,
                prepared.plan_item.approval_disposition.value,
            )
            for prepared in definitions
        )
        actual = tuple(tuple(row) for row in rows)
        if actual != expected:
            raise LegacyMigrationScanError("LEGACY_MIGRATION_REPLAY_CONFLICT")
        for prepared in definitions:
            item = prepared.plan_item
            active = connection.execute(
                """
                SELECT active_definition_digest, content_revision, state_revision,
                       decision_epoch, status, applied_revision
                FROM governance_active_proposals
                WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?
                """,
                (
                    item.proposal_ref.project_ref.namespace,
                    item.proposal_ref.project_ref.project_id,
                    item.proposal_ref.proposal_id,
                ),
            ).fetchone()
            expected_active = (
                prepared.object_ref.digest,
                item.content_revision,
                item.state_revision,
                item.decision_epoch,
                self._runtime_status(item.target_status),
                (
                    item.legacy_git_revision
                    if self._runtime_status(item.target_status) == "applied"
                    else None
                ),
            )
            if active is None or tuple(active) != expected_active:
                raise LegacyMigrationScanError("LEGACY_MIGRATION_REPLAY_CONFLICT")

    @staticmethod
    def _select_root(
        connection: sqlite3.Connection,
        migration_id: str,
    ) -> tuple[object, ...] | None:
        row = connection.execute(
            """
            SELECT migration_id, project_namespace, project_id, freeze_id,
                   snapshot_id, snapshot_digest, plan_digest, mapping_policy_version,
                   base_revision, validation_policy_ref, project_pack_backup_path,
                   project_pack_backup_digest, governance_backup_path,
                   governance_backup_digest, proposal_count, status,
                   prepared_at, state_imported_at
            FROM governance_legacy_migrations
            WHERE migration_id = ?
            """,
            (migration_id,),
        ).fetchone()
        return tuple(row) if row is not None else None

    @staticmethod
    def _root_values(
        plan: LegacyProposalMigrationPlan,
        backup: LegacyMigrationBackupEvidence,
    ) -> tuple[object, ...]:
        return (
            plan.plan_id,
            plan.project_ref.namespace,
            plan.project_ref.project_id,
            plan.freeze.freeze_id,
            plan.snapshot_id,
            plan.snapshot_digest,
            plan.plan_digest,
            plan.mapping_policy_version,
            plan.base_revision,
            plan.validation_policy_ref,
            backup.project_pack_backup_path,
            backup.project_pack_backup_digest,
            backup.governance_backup_path,
            backup.governance_backup_digest,
            len(plan.proposals),
        )

    def _result(self, plan: LegacyProposalMigrationPlan) -> LegacyMigrationImportResult:
        with self.store.connect() as connection:
            root = self._select_root(connection, plan.plan_id)
            if root is None:
                raise LegacyMigrationScanError("LEGACY_MIGRATION_NOT_PREPARED")
            digests = tuple(
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT definition_digest
                    FROM governance_legacy_migration_items
                    WHERE migration_id = ?
                    ORDER BY proposal_id
                    """,
                    (plan.plan_id,),
                ).fetchall()
            )
        return LegacyMigrationImportResult(
            migration_id=plan.plan_id,
            project_ref=plan.project_ref,
            snapshot_id=plan.snapshot_id,
            plan_digest=plan.plan_digest,
            status=str(root[15]),
            proposal_count=len(plan.proposals),
            definition_digests=digests,
        )
