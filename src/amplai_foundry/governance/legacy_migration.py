"""Write-free legacy Proposal snapshot and deterministic migration planning."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from amplai_foundry.domain.identity import ProjectRef
from amplai_foundry.governance.models import Digest, ProposalRef
from amplai_foundry.proposals.models import Proposal, ProposalStatus

_PROPOSAL_ID = re.compile(r"^PROP-[0-9]{8}-[A-F0-9]{8}$")
_FREEZE_ID = re.compile(r"^MFR-[A-F0-9]{16}$")
_GIT_REVISION = re.compile(r"^[0-9a-f]{7,64}$")


class LegacyMigrationScanError(RuntimeError):
    """Legacy input cannot be represented by a safe deterministic plan."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class LegacyMigrationScanConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    maximum_files: int = Field(default=10_000, ge=1, le=1_000_000)
    maximum_file_bytes: int = Field(default=16 * 1024 * 1024, ge=1)
    maximum_total_bytes: int = Field(default=512 * 1024 * 1024, ge=1)


class LegacyMutationFreeze(BaseModel):
    """Operator evidence that the legacy Proposal tree is no longer mutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    freeze_id: str
    actor_id: str = Field(min_length=1, max_length=128)
    frozen_at: AwareDatetime
    reason: str = Field(min_length=1, max_length=512)

    def model_post_init(self, __context: object) -> None:
        if _FREEZE_ID.fullmatch(self.freeze_id) is None:
            raise ValueError("freeze_id 형식이 올바르지 않습니다.")


class LegacySnapshotFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    relative_path: str = Field(min_length=1)
    byte_length: int = Field(ge=0)
    content_digest: Digest


class LegacyProposalSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: str = Field(pattern=r"^MPS-[A-F0-9]{16}$")
    project_ref: ProjectRef
    freeze: LegacyMutationFreeze
    files: tuple[LegacySnapshotFile, ...]
    total_bytes: int = Field(ge=0)
    snapshot_digest: Digest


class LegacyApprovalDisposition(StrEnum):
    NOT_REQUIRED = "not_required"
    LEGACY_AUDIT_PRESENT = "legacy_audit_present"
    SYNTHETIC_REQUIRED = "synthetic_required"


class LegacyProposalPlanItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal_ref: ProposalRef
    source_status: ProposalStatus
    source_revision: int = Field(ge=1)
    target_status: str
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
        plan_digest = self._digest(self._canonical_json(preimage))
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
        root_identities = self._validate_roots()
        before = self._enumerate_files()
        if len(before) > self.config.maximum_files:
            raise LegacyMigrationScanError("LEGACY_SNAPSHOT_FILE_LIMIT")
        payloads: dict[str, bytes] = {}
        entries: list[LegacySnapshotFile] = []
        total_bytes = 0
        for relative_path, expected_identity in before:
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
                    content_digest=self._digest(payload),
                )
            )
        after = self._enumerate_files()
        if before != after or root_identities != self._root_identities():
            raise LegacyMigrationScanError("LEGACY_SNAPSHOT_MUTATED")
        preimage = {
            "files": [entry.model_dump(mode="json") for entry in entries],
            "project_ref": self.project_ref.model_dump(mode="json"),
        }
        digest = self._digest(self._canonical_json(preimage))
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
        if _PROPOSAL_ID.fullmatch(proposal_id) is None:
            raise LegacyMigrationScanError("LEGACY_PROPOSAL_PATH_INVALID")
        try:
            raw = yaml.safe_load(payload)
            proposal = Proposal.model_validate(raw)
        except (UnicodeDecodeError, yaml.YAMLError, ValidationError) as error:
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
        target_status = (
            "legacy_approval_review_required"
            if approval is LegacyApprovalDisposition.SYNTHETIC_REQUIRED
            else proposal.status.value
        )
        return LegacyProposalPlanItem(
            proposal_ref=ProposalRef(project_ref=self.project_ref, proposal_id=proposal_id),
            source_status=proposal.status,
            source_revision=proposal.revision,
            target_status=target_status,
            content_revision=proposal.revision,
            state_revision=self._state_revision(proposal.status, target_status),
            decision_epoch=1,
            proposal_artifact_digest=self._digest(payload),
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

    def _validate_roots(self) -> tuple[tuple[int, int], tuple[int, int]]:
        if self.project_root.resolve(strict=True) != self.project_root:
            raise LegacyMigrationScanError("LEGACY_PROJECT_ROOT_SYMLINK")
        try:
            project_stat = self.project_root.lstat()
            legacy_stat = self.legacy_root.lstat()
        except OSError as error:
            raise LegacyMigrationScanError("LEGACY_SOURCE_ROOT_INVALID") from error
        if not stat.S_ISDIR(project_stat.st_mode) or not stat.S_ISDIR(legacy_stat.st_mode):
            raise LegacyMigrationScanError("LEGACY_SOURCE_ROOT_INVALID")
        if self.legacy_root.resolve(strict=True).parent.parent != self.project_root:
            raise LegacyMigrationScanError("LEGACY_SOURCE_ROOT_ESCAPE")
        return self._root_identities()

    def _root_identities(self) -> tuple[tuple[int, int], tuple[int, int]]:
        try:
            project = self.project_root.lstat()
            legacy = self.legacy_root.lstat()
        except OSError as error:
            raise LegacyMigrationScanError("LEGACY_SOURCE_ROOT_INVALID") from error
        return ((project.st_dev, project.st_ino), (legacy.st_dev, legacy.st_ino))

    def _enumerate_files(self) -> tuple[tuple[str, tuple[int, int, int, int]], ...]:
        entries: list[tuple[str, tuple[int, int, int, int]]] = []
        try:
            for directory, names, files in os.walk(self.legacy_root, followlinks=False):
                directory_path = Path(directory)
                discovered_names = tuple(names)
                for name in (*discovered_names, *files):
                    candidate = directory_path / name
                    metadata = candidate.lstat()
                    if stat.S_ISLNK(metadata.st_mode):
                        raise LegacyMigrationScanError("LEGACY_SOURCE_SYMLINK")
                    if name in names and not stat.S_ISDIR(metadata.st_mode):
                        raise LegacyMigrationScanError("LEGACY_SOURCE_NOT_REGULAR")
                    if name in files and not stat.S_ISREG(metadata.st_mode):
                        raise LegacyMigrationScanError("LEGACY_SOURCE_NOT_REGULAR")
                relative_directory = directory_path.relative_to(self.legacy_root)
                if len(relative_directory.parts) == 1:
                    names[:] = [name for name in names if name not in {"definitions", "inputs"}]
                for name in files:
                    candidate = directory_path / name
                    metadata = candidate.lstat()
                    relative = candidate.relative_to(self.legacy_root).as_posix()
                    entries.append(
                        (
                            relative,
                            (
                                metadata.st_dev,
                                metadata.st_ino,
                                metadata.st_size,
                                metadata.st_mtime_ns,
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
        expected_identity: tuple[int, int, int, int],
    ) -> bytes:
        descriptor = -1
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            before = os.fstat(descriptor)
            identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
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
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
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

    @staticmethod
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

    @staticmethod
    def _digest(value: bytes) -> str:
        return f"sha256:{hashlib.sha256(value).hexdigest()}"
