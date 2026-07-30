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
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Literal

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
from amplai_foundry.governance.authority import (
    AuthorityResolutionError,
    AuthorityService,
    DirectAuthorityRequest,
)
from amplai_foundry.governance.definitions import (
    ApplyInputDescriptor,
    ProposalDefinitionManifest,
    canonicalize_definition,
)
from amplai_foundry.governance.events import (
    GovernanceEventError,
    GovernanceEventService,
    LegacyApprovalReviewProjectionPayload,
    LegacyMigrationProjectionPayload,
)
from amplai_foundry.governance.models import (
    ActorType,
    AuthorityPermission,
    Digest,
    ProposalRef,
)
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


def _compact_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


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


class LegacyApprovalAuditEvidence(BaseModel):
    """Strict qualified approval evidence imported from a legacy artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    event: Literal["approved"]
    proposal_ref: ProposalRef
    actor_id: str = Field(min_length=1, max_length=128)
    actor_type: Literal["human"] = "human"
    occurred_at: AwareDatetime
    idempotency_key: str = Field(min_length=1, max_length=256)
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_fingerprint(self) -> LegacyApprovalAuditEvidence:
        preimage = self.model_dump(mode="json", exclude={"request_fingerprint"})
        expected = hashlib.sha256(_canonical_json(preimage)).hexdigest()
        if self.request_fingerprint != expected:
            raise ValueError("legacy approval audit fingerprint가 일치하지 않습니다.")
        return self


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


class LegacyProjectPackBackupManifest(BaseModel):
    """Canonical backup manifest bound to the frozen Project and legacy snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    project_ref: ProjectRef
    base_revision: str
    freeze_id: str
    snapshot_id: str = Field(pattern=r"^MPS-[A-F0-9]{16}$")
    snapshot_digest: Digest
    files: tuple[LegacySnapshotFile, ...]
    total_bytes: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_snapshot_identity(self) -> LegacyProjectPackBackupManifest:
        paths = tuple(item.relative_path for item in self.files)
        if paths != tuple(sorted(paths, key=lambda value: value.encode("utf-8"))):
            raise ValueError("Project Pack backup file order가 canonical하지 않습니다.")
        if len(paths) != len(set(paths)) or self.total_bytes != sum(
            item.byte_length for item in self.files
        ):
            raise ValueError("Project Pack backup file identity가 일치하지 않습니다.")
        preimage = {
            "files": [entry.model_dump(mode="json") for entry in self.files],
            "project_ref": self.project_ref.model_dump(mode="json"),
        }
        digest = _digest(_canonical_json(preimage))
        if self.snapshot_digest != digest or self.snapshot_id != f"MPS-{digest[-16:].upper()}":
            raise ValueError("Project Pack backup snapshot identity가 일치하지 않습니다.")
        if _FREEZE_ID.fullmatch(self.freeze_id) is None:
            raise ValueError("Project Pack backup freeze identity가 올바르지 않습니다.")
        if _GIT_REVISION.fullmatch(self.base_revision) is None:
            raise ValueError("Project Pack backup base revision이 올바르지 않습니다.")
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.model_dump(mode="json"))


class LegacyMigrationImportResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    migration_id: str = Field(pattern=r"^MPL-[A-F0-9]{16}$")
    project_ref: ProjectRef
    snapshot_id: str = Field(pattern=r"^MPS-[A-F0-9]{16}$")
    plan_digest: Digest
    status: str = Field(pattern=r"^(prepared|state_imported)$")
    proposal_count: int = Field(ge=1)
    definition_digests: tuple[Digest, ...]


class LegacyApprovalReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    review_id: str = Field(pattern=r"^LAR-[A-F0-9]{16}$")
    migration_id: str = Field(pattern=r"^MPL-[A-F0-9]{16}$")
    proposal_ref: ProposalRef
    proposal_status: Literal["draft"] = "draft"
    state_revision: int = Field(ge=2)
    audit_event_id: str = Field(pattern=r"^EVT-[A-F0-9]{16}$")
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class _PreparedLegacyDefinition:
    plan_item: LegacyProposalPlanItem
    proposal: Proposal
    object_ref: DefinitionObjectRef
    approval_evidence: LegacyApprovalAuditEvidence | None
    approval_artifact_digest: str


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
        audit_present = any(item.relative_path == audit_path for item in snapshot.files)
        if status not in {ProposalStatus.APPROVED, ProposalStatus.APPLIED}:
            if audit_present:
                raise LegacyMigrationScanError("LEGACY_APPROVAL_AUDIT_STATE_INVALID")
            return LegacyApprovalDisposition.NOT_REQUIRED
        if audit_present:
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


class LegacyApprovalReviewService:
    """Resolve one synthetic approval hold through an auditable human-only command."""

    def __init__(
        self,
        store: GovernanceStore,
        authority_service: AuthorityService,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self.store = store
        self.authority_service = authority_service
        self._clock = clock

    def resolve(
        self,
        ref: ProposalRef,
        *,
        authority_request: DirectAuthorityRequest,
        reason: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> LegacyApprovalReviewResult:
        if len(reason.strip()) < 3 or len(reason) > 512:
            raise ValueError("reason 길이가 올바르지 않습니다.")
        if not idempotency_key.strip() or len(idempotency_key) > 512:
            raise ValueError("idempotency_key 길이가 올바르지 않습니다.")
        if len(request_fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in request_fingerprint
        ):
            raise ValueError("request_fingerprint는 lowercase SHA-256 hex여야 합니다.")
        occurred_at = self._aware(self._clock())
        occurred_text = GovernanceEventService._timestamp(occurred_at)
        try:
            with self.store.connect() as connection, governance_transaction(connection):
                authority = self.authority_service.authenticate(
                    authority_request,
                    connection=connection,
                )
                if (
                    authority.project_ref != ref.project_ref
                    or authority.actor_ref.actor_type is not ActorType.HUMAN
                    or AuthorityPermission.PROPOSAL_DECIDE not in authority.permissions
                ):
                    raise LegacyMigrationScanError("AUTHORITY_DENIED")
                channel_json = _compact_json(authority.source.channel.model_dump(mode="json"))
                replay = connection.execute(
                    """
                    SELECT review_id, migration_id, project_namespace, project_id,
                           proposal_id, request_fingerprint, actor_id, actor_type,
                           channel_json, source_state_revision
                    FROM governance_legacy_approval_reviews
                    WHERE idempotency_key = ?
                    """,
                    (idempotency_key,),
                ).fetchone()
                if replay is not None:
                    if tuple(replay[2:5]) != (
                        ref.project_ref.namespace,
                        ref.project_ref.project_id,
                        ref.proposal_id,
                    ) or tuple(str(value) for value in replay[5:9]) != (
                        request_fingerprint,
                        authority.actor_ref.actor_id,
                        authority.actor_ref.actor_type.value,
                        channel_json,
                    ):
                        raise LegacyMigrationScanError("IDEMPOTENCY_CONFLICT")
                    audit = connection.execute(
                        "SELECT event_id FROM governance_audit_events WHERE command_id = ?",
                        (replay[0],),
                    ).fetchone()
                    if audit is None:
                        raise LegacyMigrationScanError("LEGACY_APPROVAL_REVIEW_CONFLICT")
                    return LegacyApprovalReviewResult(
                        review_id=str(replay[0]),
                        migration_id=str(replay[1]),
                        proposal_ref=ref,
                        state_revision=int(replay[9]),
                        audit_event_id=str(audit[0]),
                        replayed=True,
                    )
                self._require_unique_idempotency(connection, idempotency_key)
                pending = connection.execute(
                    """
                    SELECT 1 FROM governance_legacy_event_backfill_pending
                    WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?
                    """,
                    (ref.project_ref.namespace, ref.project_ref.project_id, ref.proposal_id),
                ).fetchone()
                if pending is not None:
                    raise LegacyMigrationScanError("LEGACY_MIGRATION_EVENT_BACKFILL_PENDING")
                hold = connection.execute(
                    """
                    SELECT h.migration_id
                    FROM governance_legacy_approval_holds h
                    LEFT JOIN governance_legacy_approval_reviews r
                      ON r.migration_id = h.migration_id
                     AND r.project_namespace = h.project_namespace
                     AND r.project_id = h.project_id AND r.proposal_id = h.proposal_id
                    WHERE h.project_namespace = ? AND h.project_id = ? AND h.proposal_id = ?
                      AND r.review_id IS NULL
                    """,
                    (ref.project_ref.namespace, ref.project_ref.project_id, ref.proposal_id),
                ).fetchone()
                if hold is None:
                    raise LegacyMigrationScanError("LEGACY_APPROVAL_REVIEW_NOT_REQUIRED")
                active = connection.execute(
                    """
                    SELECT active_definition_digest, state_revision, decision_epoch, status
                    FROM governance_active_proposals
                    WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?
                    """,
                    (ref.project_ref.namespace, ref.project_ref.project_id, ref.proposal_id),
                ).fetchone()
                if active is None:
                    raise LegacyMigrationScanError("PROPOSAL_NOT_FOUND")
                if str(active[3]) in {"apply_requested", "applied"}:
                    raise LegacyMigrationScanError(
                        "LEGACY_APPROVAL_REVIEW_FORWARD_RECOVERY_REQUIRED"
                    )
                next_state_revision = int(active[1]) + 1
                next_decision_epoch = int(active[2]) + 1
                payload = LegacyApprovalReviewProjectionPayload(
                    aggregate_ref=ref,
                    idempotency_key=idempotency_key,
                    migration_id=str(hold[0]),
                    reason=reason.strip(),
                    source_state_revision=next_state_revision,
                    before_status="legacy_approval_review_required",
                )
                payload_json = _compact_json(payload.model_dump(mode="json"))
                payload_digest = sha256_digest(payload_json.encode("utf-8"))
                idempotency_digest = hashlib.sha256(idempotency_key.encode()).hexdigest()
                review_id = f"LAR-{idempotency_digest[-16:].upper()}"
                connection.execute(
                    """
                    INSERT INTO governance_legacy_approval_reviews(
                        review_id, migration_id, project_namespace, project_id, proposal_id,
                        idempotency_key, request_fingerprint, actor_id, actor_type,
                        occurred_at, reason, request_id, channel_json, definition_digest,
                        source_state_revision, payload_digest, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        review_id,
                        hold[0],
                        ref.project_ref.namespace,
                        ref.project_ref.project_id,
                        ref.proposal_id,
                        idempotency_key,
                        request_fingerprint,
                        authority.actor_ref.actor_id,
                        authority.actor_ref.actor_type.value,
                        occurred_text,
                        reason.strip(),
                        authority.source.request_id,
                        channel_json,
                        active[0],
                        next_state_revision,
                        payload_digest,
                        payload_json,
                    ),
                )
                updated = connection.execute(
                    """
                    UPDATE governance_active_proposals
                    SET status = 'draft', state_revision = ?, decision_epoch = ?,
                        updated_at = ?
                    WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?
                      AND active_definition_digest = ? AND state_revision = ?
                      AND decision_epoch = ? AND status = ?
                    """,
                    (
                        next_state_revision,
                        next_decision_epoch,
                        occurred_text,
                        ref.project_ref.namespace,
                        ref.project_ref.project_id,
                        ref.proposal_id,
                        active[0],
                        active[1],
                        active[2],
                        active[3],
                    ),
                )
                if updated.rowcount != 1:
                    raise LegacyMigrationScanError("LEGACY_APPROVAL_REVIEW_CONFLICT")
                audit, _outbox = GovernanceEventService(
                    self.store,
                    clock=lambda: occurred_at,
                )._append_legacy_approval_review_in_transaction(
                    connection,
                    ref,
                    review_id=review_id,
                    payload=payload,
                )
                GovernanceEventService.reconcile_connection(connection)
                return LegacyApprovalReviewResult(
                    review_id=review_id,
                    migration_id=str(hold[0]),
                    proposal_ref=ref,
                    state_revision=next_state_revision,
                    audit_event_id=audit.event_id,
                )
        except LegacyMigrationScanError:
            raise
        except AuthorityResolutionError as error:
            raise LegacyMigrationScanError(error.code) from error
        except GovernanceEventError as error:
            raise LegacyMigrationScanError(error.code) from error
        except sqlite3.Error as error:
            raise LegacyMigrationScanError("LEGACY_APPROVAL_REVIEW_CONFLICT") from error

    @staticmethod
    def _require_unique_idempotency(
        connection: sqlite3.Connection,
        idempotency_key: str,
    ) -> None:
        collision = connection.execute(
            """
            SELECT 1 FROM (
                SELECT idempotency_key FROM governance_legacy_import_commands
                UNION ALL SELECT idempotency_key FROM governance_decision_results
                UNION ALL SELECT idempotency_key FROM governance_apply_request_results
            ) WHERE idempotency_key = ? LIMIT 1
            """,
            (idempotency_key,),
        ).fetchone()
        if collision is not None:
            raise LegacyMigrationScanError("IDEMPOTENCY_CONFLICT")

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock은 timezone-aware datetime을 반환해야 합니다.")
        return value


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
        completed = self._safe_completed_result(plan, backup)
        if completed is not None:
            return completed
        self._read_plan_payloads(plan)
        values = self._root_values(plan, backup)
        try:
            with self.store.connect() as connection, governance_transaction(connection):
                row = self._select_root(connection, plan.plan_id)
                if row is None:
                    self._verify_backup_evidence(plan, backup, live_connection=connection)
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
                else:
                    self._require_root_identity(row, values)
                    self._verify_backup_evidence(plan, backup)
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
        self._validate_scope(plan, backup)
        completed = self._safe_completed_result(plan, backup)
        if completed is not None:
            return completed
        self.prepare(plan, backup)
        payloads = self._read_plan_payloads(plan)
        try:
            definitions = self._prepare_definition_objects(plan, payloads)
        except DefinitionObjectStoreError as error:
            raise LegacyMigrationScanError("LEGACY_DEFINITION_INTEGRITY_FAILURE") from error
        self._read_plan_payloads(plan)
        self._verify_backup_evidence(plan, backup)
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
                GovernanceEventService.reconcile_connection(connection)
        except LegacyMigrationScanError:
            raise
        except GovernanceEventError as error:
            raise LegacyMigrationScanError(error.code) from error
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

    def _verify_backup_evidence(
        self,
        plan: LegacyProposalMigrationPlan,
        backup: LegacyMigrationBackupEvidence,
        *,
        live_connection: sqlite3.Connection | None = None,
    ) -> None:
        project_pack = Path(backup.project_pack_backup_path)
        governance = Path(backup.governance_backup_path)
        if governance.resolve(strict=False) == self.store.path:
            raise LegacyMigrationScanError("LEGACY_GOVERNANCE_BACKUP_INVALID")
        project_pack_bytes = self._verify_backup_file(
            project_pack,
            backup.project_pack_backup_digest,
            "LEGACY_PROJECT_PACK_BACKUP_INVALID",
            capture=True,
        )
        if project_pack_bytes is None:
            raise LegacyMigrationScanError("LEGACY_PROJECT_PACK_BACKUP_INVALID")
        try:
            manifest = LegacyProjectPackBackupManifest.model_validate_json(project_pack_bytes)
        except (ValidationError, ValueError) as error:
            raise LegacyMigrationScanError("LEGACY_PROJECT_PACK_BACKUP_INVALID") from error
        if (
            manifest.canonical_bytes() != project_pack_bytes
            or manifest.project_ref != plan.project_ref
            or manifest.base_revision != plan.base_revision
            or manifest.freeze_id != plan.freeze.freeze_id
            or manifest.snapshot_id != plan.snapshot_id
            or manifest.snapshot_digest != plan.snapshot_digest
        ):
            raise LegacyMigrationScanError("LEGACY_PROJECT_PACK_BACKUP_INVALID")
        self._verify_backup_file(
            governance,
            backup.governance_backup_digest,
            "LEGACY_GOVERNANCE_BACKUP_INVALID",
        )
        try:
            live_stat = self.store.path.stat()
            backup_stat = governance.stat()
            if (live_stat.st_dev, live_stat.st_ino) == (backup_stat.st_dev, backup_stat.st_ino):
                raise LegacyMigrationScanError("LEGACY_GOVERNANCE_BACKUP_INVALID")
            if Path(f"{governance}-wal").exists() or Path(f"{governance}-shm").exists():
                raise LegacyMigrationScanError("LEGACY_GOVERNANCE_BACKUP_INVALID")
            with sqlite3.connect(
                f"{governance.as_uri()}?mode=ro&immutable=1", uri=True
            ) as connection:
                integrity = connection.execute("PRAGMA integrity_check").fetchone()
                if integrity != ("ok",):
                    raise LegacyMigrationScanError("LEGACY_GOVERNANCE_BACKUP_INVALID")
                if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise LegacyMigrationScanError("LEGACY_GOVERNANCE_BACKUP_INVALID")
                version = self.store.migration_runner.verify(connection)
                self.store.migration_runner.verify_schema(connection, version)
                backup_logical_digest = self._sqlite_logical_digest(connection)
            if live_connection is not None:
                live_version = self.store.migration_runner.verify(live_connection)
                if version != live_version or backup_logical_digest != self._sqlite_logical_digest(
                    live_connection
                ):
                    raise LegacyMigrationScanError("LEGACY_GOVERNANCE_BACKUP_INVALID")
        except LegacyMigrationScanError:
            raise
        except (OSError, sqlite3.Error, RuntimeError) as error:
            raise LegacyMigrationScanError("LEGACY_GOVERNANCE_BACKUP_INVALID") from error

    def _verify_backup_file(
        self,
        path: Path,
        expected_digest: str,
        error_code: str,
        *,
        capture: bool = False,
    ) -> bytes | None:
        descriptor = -1
        chunks: list[bytes] = []
        try:
            if path.resolve(strict=True) != path:
                raise LegacyMigrationScanError(error_code)
            path_metadata = path.lstat()
            if not stat.S_ISREG(path_metadata.st_mode):
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
                if capture:
                    chunks.append(chunk)
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
            return b"".join(chunks) if capture else None
        except LegacyMigrationScanError:
            raise
        except (OSError, RuntimeError) as error:
            raise LegacyMigrationScanError(error_code) from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    @staticmethod
    def _sqlite_logical_digest(connection: sqlite3.Connection) -> str:
        digest = hashlib.sha256()
        for statement in connection.iterdump():
            digest.update(statement.encode("utf-8"))
            digest.update(b"\n")
        return f"sha256:{digest.hexdigest()}"

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
            approval_evidence: LegacyApprovalAuditEvidence | None = None
            approval_artifact_digest = item.proposal_artifact_digest
            if item.approval_disposition is LegacyApprovalDisposition.LEGACY_AUDIT_PRESENT:
                audit_path = f"{item.proposal_ref.proposal_id}/approval-audit.json"
                audit_bytes = payloads.get(audit_path)
                if audit_bytes is None:
                    raise LegacyMigrationScanError("LEGACY_APPROVAL_AUDIT_INVALID")
                try:
                    approval_evidence = LegacyApprovalAuditEvidence.model_validate_json(audit_bytes)
                except ValidationError as error:
                    raise LegacyMigrationScanError("LEGACY_APPROVAL_AUDIT_INVALID") from error
                if (
                    approval_evidence.proposal_ref != item.proposal_ref
                    or approval_evidence.actor_id != proposal.approved_by
                    or proposal.approved_at is None
                    or proposal.approved_at.tzinfo is None
                    or proposal.approved_at.utcoffset() is None
                    or approval_evidence.occurred_at != proposal.approved_at
                ):
                    raise LegacyMigrationScanError("LEGACY_APPROVAL_AUDIT_INVALID")
                approval_artifact_digest = sha256_digest(audit_bytes)
            prepared.append(
                _PreparedLegacyDefinition(
                    item,
                    proposal,
                    object_ref,
                    approval_evidence,
                    approval_artifact_digest,
                )
            )
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
        if target in {LegacyTargetStatus.DRAFT, LegacyTargetStatus.REVIEWED}:
            return target.value
        return "draft"

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
                decision_epoch, approval_disposition, legacy_git_revision, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                item.legacy_git_revision,
                timestamp,
            ),
        )
        self._insert_migration_event(connection, plan, prepared)

    def _insert_migration_event(
        self,
        connection: sqlite3.Connection,
        plan: LegacyProposalMigrationPlan,
        prepared: _PreparedLegacyDefinition,
    ) -> None:
        item = prepared.plan_item
        evidence = prepared.approval_evidence
        actor_type: str
        occurred_at: datetime | None
        if evidence is not None:
            event_type = "migration.legacy_approval"
            actor_id = evidence.actor_id
            actor_type = evidence.actor_type
            occurred_at = evidence.occurred_at
            idempotency_key = evidence.idempotency_key
            request_fingerprint = evidence.request_fingerprint
            reason: str | None = None
        elif item.approval_disposition is LegacyApprovalDisposition.SYNTHETIC_REQUIRED:
            event_type = "migration.synthetic_approval"
            actor_id = "ACT-SYSTEM-MIGRATION"
            actor_type = "service"
            occurred_at = prepared.proposal.approved_at
            idempotency_key = (
                f"legacy:{plan.plan_id}:{item.proposal_ref.proposal_id}:synthetic-approval"
            )
            reason = "legacy_approval_without_audit"
            request_fingerprint = hashlib.sha256(
                _compact_json(
                    {
                        "actor_id": actor_id,
                        "event_type": event_type,
                        "idempotency_key": idempotency_key,
                        "proposal_ref": item.proposal_ref.model_dump(mode="json"),
                        "source_artifact_digest": prepared.approval_artifact_digest,
                    }
                ).encode("utf-8")
            ).hexdigest()
        else:
            event_type = "migration.state_imported"
            actor_id = plan.freeze.actor_id
            actor_type = "service"
            occurred_at = plan.freeze.frozen_at
            idempotency_key = (
                f"legacy:{plan.plan_id}:{item.proposal_ref.proposal_id}:state-imported"
            )
            reason = None
            request_fingerprint = hashlib.sha256(
                _compact_json(
                    {
                        "actor_id": actor_id,
                        "event_type": event_type,
                        "idempotency_key": idempotency_key,
                        "proposal_ref": item.proposal_ref.model_dump(mode="json"),
                        "source_artifact_digest": prepared.approval_artifact_digest,
                    }
                ).encode("utf-8")
            ).hexdigest()
        if occurred_at is None or occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise LegacyMigrationScanError("LEGACY_APPROVAL_AUDIT_INVALID")
        payload = LegacyMigrationProjectionPayload(
            aggregate_ref=item.proposal_ref,
            approval_disposition=item.approval_disposition.value,
            event_type=event_type,
            idempotency_key=idempotency_key,
            mapping_policy_version=plan.mapping_policy_version,
            migration_id=plan.plan_id,
            reason=reason,
            source_artifact_digest=prepared.approval_artifact_digest,
            source_revision=item.source_revision,
            source_state_revision=item.state_revision,
            source_status=item.source_status.value,
            target_status=item.target_status.value,
            validation_policy_ref=plan.validation_policy_ref,
        )
        payload_json = _compact_json(payload.model_dump(mode="json"))
        payload_digest = sha256_digest(payload_json.encode("utf-8"))
        existing_idempotency = connection.execute(
            """
            SELECT request_fingerprint FROM governance_legacy_import_commands
            WHERE idempotency_key = ?
            UNION ALL
            SELECT request_fingerprint FROM governance_legacy_approval_reviews
            WHERE idempotency_key = ?
            UNION ALL
            SELECT request_fingerprint FROM governance_decision_results
            WHERE idempotency_key = ?
            UNION ALL
            SELECT request_fingerprint FROM governance_apply_request_results
            WHERE idempotency_key = ?
            """,
            (idempotency_key, idempotency_key, idempotency_key, idempotency_key),
        ).fetchall()
        if existing_idempotency:
            if any(str(row[0]) != request_fingerprint for row in existing_idempotency):
                raise LegacyMigrationScanError("IDEMPOTENCY_CONFLICT")
            raise LegacyMigrationScanError("LEGACY_MIGRATION_REPLAY_CONFLICT")
        command_digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        command_id = f"MCM-{command_digest[-16:].upper()}"
        occurred_text = GovernanceEventService._timestamp(occurred_at)
        connection.execute(
            """
            INSERT INTO governance_legacy_import_commands(
                command_id, migration_id, project_namespace, project_id, proposal_id,
                idempotency_key, request_fingerprint, event_type, actor_id, actor_type,
                occurred_at, source_artifact_digest, reason, definition_digest,
                source_state_revision, payload_digest, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                command_id,
                plan.plan_id,
                item.proposal_ref.project_ref.namespace,
                item.proposal_ref.project_ref.project_id,
                item.proposal_ref.proposal_id,
                idempotency_key,
                request_fingerprint,
                event_type,
                actor_id,
                actor_type,
                occurred_text,
                prepared.approval_artifact_digest,
                reason,
                prepared.object_ref.digest,
                item.state_revision,
                payload_digest,
                payload_json,
            ),
        )
        if item.approval_disposition is LegacyApprovalDisposition.SYNTHETIC_REQUIRED:
            connection.execute(
                """
                INSERT INTO governance_legacy_approval_holds(
                    migration_id, project_namespace, project_id, proposal_id,
                    reason_code, source_artifact_digest, created_at
                ) VALUES (?, ?, ?, ?, 'legacy_approval_without_audit', ?, ?)
                """,
                (
                    plan.plan_id,
                    item.proposal_ref.project_ref.namespace,
                    item.proposal_ref.project_ref.project_id,
                    item.proposal_ref.proposal_id,
                    prepared.approval_artifact_digest,
                    occurred_text,
                ),
            )
        GovernanceEventService(
            self.store,
            clock=lambda: occurred_at,
        )._append_legacy_import_in_transaction(
            connection,
            item.proposal_ref,
            command_id=command_id,
            payload=payload,
        )
        connection.execute(
            """
            DELETE FROM governance_legacy_event_backfill_pending
            WHERE migration_id = ? AND project_namespace = ?
              AND project_id = ? AND proposal_id = ?
            """,
            (
                plan.plan_id,
                item.proposal_ref.project_ref.namespace,
                item.proposal_ref.project_ref.project_id,
                item.proposal_ref.proposal_id,
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
                   state_revision, decision_epoch, approval_disposition,
                   legacy_git_revision
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
                prepared.plan_item.legacy_git_revision,
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

    def _safe_completed_result(
        self,
        plan: LegacyProposalMigrationPlan,
        backup: LegacyMigrationBackupEvidence,
    ) -> LegacyMigrationImportResult | None:
        try:
            return self._completed_result(plan, backup)
        except LegacyMigrationScanError:
            raise
        except GovernanceEventError as error:
            raise LegacyMigrationScanError("LEGACY_MIGRATION_REPLAY_CONFLICT") from error
        except sqlite3.Error as error:
            raise LegacyMigrationScanError("LEGACY_MIGRATION_REPLAY_CONFLICT") from error

    def _completed_result(
        self,
        plan: LegacyProposalMigrationPlan,
        backup: LegacyMigrationBackupEvidence,
    ) -> LegacyMigrationImportResult | None:
        values = self._root_values(plan, backup)
        with self.store.connect() as connection, governance_transaction(connection):
            root = self._select_root(connection, plan.plan_id)
            if root is None or str(root[15]) != "state_imported":
                return None
            self._require_root_identity(root, values)
            rows = connection.execute(
                """
                SELECT proposal_id, source_status, source_revision, target_status,
                       definition_digest, proposal_artifact_digest, content_revision,
                       state_revision, decision_epoch, approval_disposition,
                       legacy_git_revision
                FROM governance_legacy_migration_items
                WHERE migration_id = ? AND project_namespace = ? AND project_id = ?
                ORDER BY proposal_id
                """,
                (plan.plan_id, plan.project_ref.namespace, plan.project_ref.project_id),
            ).fetchall()
            if len(rows) != len(plan.proposals):
                raise LegacyMigrationScanError("LEGACY_MIGRATION_REPLAY_CONFLICT")
            for row, item in zip(rows, plan.proposals, strict=True):
                actual_without_digest = (
                    str(row[0]),
                    str(row[1]),
                    int(row[2]),
                    str(row[3]),
                    str(row[5]),
                    int(row[6]),
                    int(row[7]),
                    int(row[8]),
                    str(row[9]),
                )
                expected_without_digest = (
                    item.proposal_ref.proposal_id,
                    item.source_status.value,
                    item.source_revision,
                    item.target_status.value,
                    item.proposal_artifact_digest,
                    item.content_revision,
                    item.state_revision,
                    item.decision_epoch,
                    item.approval_disposition.value,
                )
                if actual_without_digest != expected_without_digest:
                    raise LegacyMigrationScanError("LEGACY_MIGRATION_REPLAY_CONFLICT")
                persisted_git_revision = str(row[10]) if row[10] is not None else None
                external_provenance_required = (
                    persisted_git_revision is None and item.legacy_git_revision is not None
                )
                if not external_provenance_required and (
                    persisted_git_revision != item.legacy_git_revision
                ):
                    raise LegacyMigrationScanError("LEGACY_MIGRATION_REPLAY_CONFLICT")
                definition_digest = str(row[4])
                active = connection.execute(
                    """
                    SELECT active_definition_digest, content_revision, state_revision,
                           decision_epoch, status, applied_revision
                    FROM governance_active_proposals
                    WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?
                    """,
                    (
                        plan.project_ref.namespace,
                        plan.project_ref.project_id,
                        item.proposal_ref.proposal_id,
                    ),
                ).fetchone()
                if active is None:
                    raise LegacyMigrationScanError("LEGACY_MIGRATION_REPLAY_CONFLICT")
                active_content_revision = int(active[1])
                if (
                    active_content_revision < item.content_revision
                    or int(active[2]) < item.state_revision
                    or int(active[3]) < item.decision_epoch
                    or (
                        active_content_revision == item.content_revision
                        and str(active[0]) != definition_digest
                    )
                ):
                    raise LegacyMigrationScanError("LEGACY_MIGRATION_REPLAY_CONFLICT")
                revision = connection.execute(
                    """
                    SELECT definition_digest, previous_definition_digest,
                           activated_from_status
                    FROM governance_definition_revisions
                    WHERE project_namespace = ? AND project_id = ?
                      AND proposal_id = ? AND content_revision = ?
                    """,
                    (
                        plan.project_ref.namespace,
                        plan.project_ref.project_id,
                        item.proposal_ref.proposal_id,
                        item.content_revision,
                    ),
                ).fetchone()
                if revision is None or tuple(revision) != (
                    definition_digest,
                    None,
                    None,
                ):
                    raise LegacyMigrationScanError("LEGACY_MIGRATION_REPLAY_CONFLICT")
                try:
                    definition_bytes = self.objects.get_definition_object(
                        item.proposal_ref,
                        definition_digest,
                    )
                    manifest = ProposalDefinitionManifest.model_validate_json(definition_bytes)
                    expected_mapping = {
                        "legacy_migration": {
                            "mapping_policy_version": plan.mapping_policy_version,
                            "proposal_artifact_digest": item.proposal_artifact_digest,
                            "snapshot_digest": plan.snapshot_digest,
                            "source_revision": item.source_revision,
                            "source_status": item.source_status.value,
                        }
                    }
                    if (
                        manifest.proposal_ref != item.proposal_ref
                        or manifest.base_revision != plan.base_revision
                        or manifest.validation_policy_ref != plan.validation_policy_ref
                        or manifest.preconditions != (expected_mapping,)
                    ):
                        raise LegacyMigrationScanError("LEGACY_MIGRATION_REPLAY_CONFLICT")
                    proposal_payload: bytes | None = None
                    approval_payload: bytes | None = None
                    for descriptor in manifest.apply_inputs:
                        input_payload = self.objects.get_input_object(
                            item.proposal_ref,
                            descriptor.object_digest,
                        )
                        if descriptor.logical_name == "proposal.yaml":
                            proposal_payload = input_payload
                        elif descriptor.logical_name == "approval-audit.json":
                            approval_payload = input_payload
                    if proposal_payload is None:
                        raise LegacyMigrationScanError("LEGACY_MIGRATION_REPLAY_CONFLICT")
                    legacy_proposal = Proposal.model_validate(yaml.safe_load(proposal_payload))
                    if (
                        external_provenance_required
                        and legacy_proposal.git_commit_sha != item.legacy_git_revision
                    ):
                        raise LegacyMigrationScanError("LEGACY_MIGRATION_REPLAY_CONFLICT")
                    evidence: LegacyApprovalAuditEvidence | None = None
                    expected_source_digest = item.proposal_artifact_digest
                    expected_command: tuple[str, str, str, str, str, str, str]
                    if item.approval_disposition is LegacyApprovalDisposition.LEGACY_AUDIT_PRESENT:
                        if approval_payload is None:
                            raise LegacyMigrationScanError("LEGACY_MIGRATION_REPLAY_CONFLICT")
                        evidence = LegacyApprovalAuditEvidence.model_validate_json(approval_payload)
                        if (
                            evidence.proposal_ref != item.proposal_ref
                            or evidence.actor_id != legacy_proposal.approved_by
                            or legacy_proposal.approved_at is None
                            or evidence.occurred_at != legacy_proposal.approved_at
                        ):
                            raise LegacyMigrationScanError("LEGACY_MIGRATION_REPLAY_CONFLICT")
                        expected_source_digest = sha256_digest(approval_payload)
                        expected_command = (
                            evidence.idempotency_key,
                            evidence.request_fingerprint,
                            "migration.legacy_approval",
                            evidence.actor_id,
                            evidence.actor_type,
                            GovernanceEventService._timestamp(evidence.occurred_at),
                            expected_source_digest,
                        )
                    elif item.approval_disposition is LegacyApprovalDisposition.SYNTHETIC_REQUIRED:
                        if legacy_proposal.approved_at is None:
                            raise LegacyMigrationScanError("LEGACY_MIGRATION_REPLAY_CONFLICT")
                        expected_idempotency_key = (
                            f"legacy:{plan.plan_id}:{item.proposal_ref.proposal_id}:"
                            "synthetic-approval"
                        )
                        expected_fingerprint = hashlib.sha256(
                            _compact_json(
                                {
                                    "actor_id": "ACT-SYSTEM-MIGRATION",
                                    "event_type": "migration.synthetic_approval",
                                    "idempotency_key": expected_idempotency_key,
                                    "proposal_ref": item.proposal_ref.model_dump(mode="json"),
                                    "source_artifact_digest": expected_source_digest,
                                }
                            ).encode("utf-8")
                        ).hexdigest()
                        expected_command = (
                            expected_idempotency_key,
                            expected_fingerprint,
                            "migration.synthetic_approval",
                            "ACT-SYSTEM-MIGRATION",
                            "service",
                            GovernanceEventService._timestamp(legacy_proposal.approved_at),
                            expected_source_digest,
                        )
                    else:
                        expected_idempotency_key = (
                            f"legacy:{plan.plan_id}:{item.proposal_ref.proposal_id}:state-imported"
                        )
                        expected_fingerprint = hashlib.sha256(
                            _compact_json(
                                {
                                    "actor_id": plan.freeze.actor_id,
                                    "event_type": "migration.state_imported",
                                    "idempotency_key": expected_idempotency_key,
                                    "proposal_ref": item.proposal_ref.model_dump(mode="json"),
                                    "source_artifact_digest": expected_source_digest,
                                }
                            ).encode("utf-8")
                        ).hexdigest()
                        expected_command = (
                            expected_idempotency_key,
                            expected_fingerprint,
                            "migration.state_imported",
                            plan.freeze.actor_id,
                            "service",
                            GovernanceEventService._timestamp(plan.freeze.frozen_at),
                            expected_source_digest,
                        )
                    command = connection.execute(
                        """
                        SELECT idempotency_key, request_fingerprint, event_type,
                               actor_id, actor_type, occurred_at, source_artifact_digest
                        FROM governance_legacy_import_commands
                        WHERE migration_id = ? AND project_namespace = ?
                          AND project_id = ? AND proposal_id = ?
                        """,
                        (
                            plan.plan_id,
                            plan.project_ref.namespace,
                            plan.project_ref.project_id,
                            item.proposal_ref.proposal_id,
                        ),
                    ).fetchone()
                    pending = connection.execute(
                        """
                        SELECT 1 FROM governance_legacy_event_backfill_pending
                        WHERE migration_id = ? AND project_namespace = ?
                          AND project_id = ? AND proposal_id = ?
                        """,
                        (
                            plan.plan_id,
                            plan.project_ref.namespace,
                            plan.project_ref.project_id,
                            item.proposal_ref.proposal_id,
                        ),
                    ).fetchone()
                    if pending is None:
                        if command is None:
                            raise LegacyMigrationScanError("LEGACY_MIGRATION_REPLAY_CONFLICT")
                        actual_command = tuple(command)
                        if actual_command != expected_command:
                            raise LegacyMigrationScanError("LEGACY_MIGRATION_REPLAY_CONFLICT")
                    if pending is not None:
                        self._insert_migration_event(
                            connection,
                            plan,
                            _PreparedLegacyDefinition(
                                item,
                                legacy_proposal,
                                DefinitionObjectRef(
                                    proposal_ref=item.proposal_ref,
                                    object_kind="definition",
                                    digest=definition_digest,
                                    path=Path("."),
                                ),
                                evidence,
                                expected_source_digest,
                            ),
                        )
                except (DefinitionObjectStoreError, ValidationError, yaml.YAMLError) as error:
                    raise LegacyMigrationScanError("LEGACY_MIGRATION_REPLAY_CONFLICT") from error
            try:
                GovernanceEventService.reconcile_connection(connection)
            except GovernanceEventError as error:
                raise LegacyMigrationScanError("LEGACY_MIGRATION_REPLAY_CONFLICT") from error
        return self._result(plan)

    @staticmethod
    def _require_root_identity(
        root: tuple[object, ...],
        values: tuple[object, ...],
    ) -> None:
        actual = tuple(str(value) if value is not None else None for value in root[:15])
        expected = tuple(str(value) for value in values)
        if actual != expected:
            raise LegacyMigrationScanError("LEGACY_MIGRATION_IDENTITY_CONFLICT")

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
