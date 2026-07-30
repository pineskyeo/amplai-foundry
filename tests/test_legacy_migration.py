from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import sqlite3
import stat
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from amplai_foundry.domain.identity import ProjectRef
from amplai_foundry.governance import (
    ActiveProposalError,
    ActiveProposalRepository,
    ActorBindingService,
    ActorRef,
    ActorType,
    AuthorityPermission,
    AuthorityService,
    BindingApproval,
    BindingTarget,
    CandidateCommitEvidence,
    ChannelProvider,
    ChannelRef,
    DecisionAction,
    DirectAuthorityRequest,
    ImmutableDefinitionObjectStore,
    LegacyApprovalDisposition,
    LegacyApprovalReviewService,
    LegacyMigrationActivationResult,
    LegacyMigrationActivationService,
    LegacyMigrationBackupEvidence,
    LegacyMigrationLifecycleError,
    LegacyMigrationScanConfig,
    LegacyMigrationScanError,
    LegacyMigrationVerificationReport,
    LegacyMutationFreeze,
    LegacyProjectPackBackupManifest,
    LegacyProposalDryRunService,
    LegacyProposalImportService,
    LegacyProposalMigrationPlan,
    LegacyProposalPlanItem,
    OutboxDispatcher,
    ProposalRef,
    ProposalSubmissionService,
    PublishGovernanceError,
    PublishPreparationService,
    PublishResolutionService,
    YamlProjectionDestination,
)
from amplai_foundry.governance.apply_jobs import (
    ApplyGovernanceError,
    ApplyGrantService,
    ApplyJobService,
    ApplyRequestService,
)
from amplai_foundry.governance.decisions import DecisionError, DecisionService
from amplai_foundry.governance.definitions import (
    ProposalDefinitionManifest,
    canonicalize_definition,
)
from amplai_foundry.governance.events import GovernanceEventError, GovernanceEventService
from amplai_foundry.governance.migrations import INITIAL_MIGRATIONS, MigrationRunner
from amplai_foundry.governance.object_store import sha256_digest
from amplai_foundry.governance.store import (
    GovernanceCommitAmbiguousError,
    GovernanceStore,
    governance_transaction,
)

PROJECT = ProjectRef(project_id="amplai", namespace="org/default/project/amplai")
AUTHORITY_PROJECT = ProjectRef(
    project_id="governance",
    namespace="org/default/project/governance",
)
PROPOSAL_ID = "PROP-20260730-A1B2C3D4"
NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
MANAGER = ActorRef(actor_id="ACT-MANAGER-1", actor_type=ActorType.HUMAN)
REVIEWER = ActorRef(actor_id="ACT-REVIEWER-1", actor_type=ActorType.HUMAN)
CHANNEL = ChannelRef(
    provider=ChannelProvider.SLACK,
    workspace_id="T123",
    channel_id="C456",
    message_id="1710000000.000200",
)


def _freeze(root: Path, project: ProjectRef = PROJECT) -> LegacyMutationFreeze:
    return LegacyMutationFreeze(
        freeze_id="MFR-0000000000000001",
        project_ref=project,
        source_root=str(root / ".amplai" / "proposals"),
        base_revision="a13d92f",
        actor_id="ACT-MIGRATION-OPERATOR",
        frozen_at=NOW,
        reason="legacy mutation disabled before v3 import",
    )


def _proposal_payload(
    project: ProjectRef = PROJECT,
    *,
    status: str = "draft",
    revision: int = 1,
) -> bytes:
    payload: dict[str, object] = {
        "proposal_version": 1,
        "revision": revision,
        "proposal_id": PROPOSAL_ID,
        "project": project.project_id,
        "namespace": project.namespace,
        "status": status,
        "source_ids": ["SRC-20260730-A1B2C3D4"],
        "created_at": NOW.isoformat(),
        "created_by": "legacy-codex",
        "operations": [
            {
                "operation_id": "OP-001",
                "type": "CREATE",
                "kind": "concept",
                "title": "Legacy proposal",
                "reason": "migration fixture",
                "evidence": [
                    {
                        "source_id": "SRC-20260730-A1B2C3D4",
                        "locator": {"type": "line_range", "start": 1, "end": 1},
                    }
                ],
                "confidence": "high",
                "draft_path": (f".amplai/proposals/{PROPOSAL_ID}/drafts/CON-0001.md"),
            }
        ],
    }
    if status in {"approved", "applied"}:
        payload.update(
            {
                "approved_at": NOW.isoformat(),
                "approved_by": "legacy-user",
            }
        )
    if status == "applied":
        payload.update(
            {
                "applied_at": NOW.isoformat(),
                "applied_by": "legacy-user",
                "git_commit_sha": "a13d92f",
            }
        )
    return yaml.safe_dump(payload, sort_keys=False).encode("utf-8")


def _approval_audit_payload(
    project: ProjectRef = PROJECT,
    *,
    idempotency_key: str | None = None,
) -> bytes:
    payload: dict[str, object] = {
        "schema_version": 1,
        "event": "approved",
        "proposal_ref": {
            "project_ref": project.model_dump(mode="json"),
            "proposal_id": PROPOSAL_ID,
        },
        "actor_id": "legacy-user",
        "actor_type": "human",
        "occurred_at": NOW.isoformat().replace("+00:00", "Z"),
        "idempotency_key": idempotency_key or f"legacy-approval:{project.namespace}:{PROPOSAL_ID}",
    }
    payload["request_fingerprint"] = _canonical_digest(payload).removeprefix("sha256:")
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _legacy_tree(
    tmp_path: Path,
    *,
    project: ProjectRef = PROJECT,
    status: str = "draft",
    revision: int = 1,
) -> Path:
    root = tmp_path / project.project_id
    proposal = root / ".amplai" / "proposals" / PROPOSAL_ID
    drafts = proposal / "drafts"
    drafts.mkdir(parents=True)
    (proposal / "proposal.yaml").write_bytes(
        _proposal_payload(project, status=status, revision=revision)
    )
    (proposal / "summary.md").write_text("# Legacy summary\n", encoding="utf-8")
    (drafts / "CON-0001.md").write_text("# Legacy draft\n", encoding="utf-8")
    return root


def _tree_inventory(root: Path) -> dict[str, tuple[int, int, int, str | None]]:
    inventory: dict[str, tuple[int, int, int, str | None]] = {}
    for path in (root, *sorted(root.rglob("*"))):
        metadata = path.lstat()
        digest = (
            hashlib.sha256(path.read_bytes()).hexdigest()
            if stat.S_ISREG(metadata.st_mode)
            else None
        )
        relative = "." if path == root else path.relative_to(root).as_posix()
        inventory[relative] = (
            stat.S_IFMT(metadata.st_mode),
            stat.S_IMODE(metadata.st_mode),
            metadata.st_size,
            digest,
        )
    return inventory


def _canonical_digest(value: object) -> str:
    payload = (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _backup_evidence(
    tmp_path: Path,
    root: Path,
    store: GovernanceStore,
    *,
    project: ProjectRef = PROJECT,
) -> LegacyMigrationBackupEvidence:
    backup_root = tmp_path / f"backups-{project.project_id}"
    backup_root.mkdir(parents=True)
    project_pack = backup_root / "project-pack.snapshot"
    snapshot = LegacyProposalDryRunService(root, project).create_snapshot(_freeze(root, project))
    project_pack_manifest = LegacyProjectPackBackupManifest(
        project_ref=project,
        base_revision="a13d92f",
        freeze_id="MFR-0000000000000001",
        snapshot_id=snapshot.snapshot_id,
        snapshot_digest=snapshot.snapshot_digest,
        files=snapshot.files,
        total_bytes=snapshot.total_bytes,
    )
    project_pack.write_bytes(project_pack_manifest.canonical_bytes())
    governance = backup_root / "governance.db"
    with store.connect() as source, sqlite3.connect(governance) as destination:
        source.backup(destination)
    return LegacyMigrationBackupEvidence(
        project_ref=project,
        project_pack_backup_path=str(project_pack),
        project_pack_backup_digest=sha256_digest(project_pack.read_bytes()),
        governance_backup_path=str(governance),
        governance_backup_digest=sha256_digest(governance.read_bytes()),
        actor_id="ACT-MIGRATION-OPERATOR",
        captured_at=NOW,
    )


def _import_fixture(
    tmp_path: Path,
    root: Path,
    *,
    project: ProjectRef = PROJECT,
    store: GovernanceStore | None = None,
) -> tuple[
    GovernanceStore,
    ImmutableDefinitionObjectStore,
    LegacyProposalDryRunService,
    LegacyProposalImportService,
    LegacyMigrationBackupEvidence,
]:
    governance = store or GovernanceStore(tmp_path / "runtime" / "governance.db")
    governance.initialize()
    objects = ImmutableDefinitionObjectStore(project, root)
    dry_run = LegacyProposalDryRunService(root, project)
    service = LegacyProposalImportService(dry_run, governance, objects)
    backup = _backup_evidence(tmp_path, root, governance, project=project)
    return governance, objects, dry_run, service, backup


def _migration_authority(
    store: GovernanceStore,
    *,
    grant_activation: bool = True,
) -> DirectAuthorityRequest:
    bindings = ActorBindingService(store, AUTHORITY_PROJECT, clock=lambda: NOW)
    bindings.bootstrap_manager(MANAGER)

    def approval(index: int) -> BindingApproval:
        return BindingApproval(
            approval_id=f"APR-{index:016X}",
            approved_by=MANAGER,
            reason="approve migration governance test setup",
        )

    bindings.register_actor(REVIEWER, approval=approval(1))
    bindings.grant_permission(
        REVIEWER,
        PROJECT,
        AuthorityPermission.PROPOSAL_SUBMIT_REVIEW,
        approval=approval(2),
    )
    bindings.grant_permission(
        REVIEWER,
        PROJECT,
        AuthorityPermission.PROPOSAL_DECIDE,
        approval=approval(3),
    )
    bindings.grant_permission(
        REVIEWER,
        PROJECT,
        AuthorityPermission.PROPOSAL_REQUEST_APPLY,
        approval=approval(4),
    )
    if grant_activation:
        bindings.grant_permission(
            REVIEWER,
            PROJECT,
            AuthorityPermission.ACTIVATION_MANAGE,
            approval=approval(5),
        )
    bindings.create_binding(
        BindingTarget(
            provider=ChannelProvider.SLACK,
            provider_installation_ref="T123:APP1",
            external_actor_id="U456",
            actor_ref=REVIEWER,
        ),
        approval=approval(6 if grant_activation else 5),
    )
    return DirectAuthorityRequest(
        provider=ChannelProvider.SLACK,
        provider_installation_ref="T123:APP1",
        external_actor_id="U456",
        project_ref=PROJECT,
        request_id="REQ-MIGRATION-HOLD",
        channel=CHANNEL,
    )


def _seed_v18_imported_proposal(
    path: Path,
    *,
    plan: LegacyProposalMigrationPlan,
    item: LegacyProposalPlanItem,
    definition_digest: str,
    backup: LegacyMigrationBackupEvidence,
) -> GovernanceStore:
    store = GovernanceStore(path, migration_runner=MigrationRunner(INITIAL_MIGRATIONS[:18]))
    store.initialize()
    with store.connect() as connection, governance_transaction(connection):
        connection.execute(
            """
            INSERT INTO governance_active_proposals(
                project_namespace, project_id, proposal_id, active_definition_digest,
                content_revision, state_revision, decision_epoch, status,
                created_at, updated_at, applied_revision
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                PROJECT.namespace,
                PROJECT.project_id,
                PROPOSAL_ID,
                definition_digest,
                item.content_revision,
                item.state_revision,
                item.decision_epoch,
                item.target_status.value,
                NOW.isoformat(),
                NOW.isoformat(),
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
            (
                PROJECT.namespace,
                PROJECT.project_id,
                PROPOSAL_ID,
                item.content_revision,
                definition_digest,
                NOW.isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO governance_legacy_migrations(
                migration_id, project_namespace, project_id, freeze_id,
                snapshot_id, snapshot_digest, plan_digest, mapping_policy_version,
                base_revision, validation_policy_ref, project_pack_backup_path,
                project_pack_backup_digest, governance_backup_path,
                governance_backup_digest, proposal_count, status, prepared_at,
                state_imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1,
                      'state_imported', ?, ?)
            """,
            (
                plan.plan_id,
                PROJECT.namespace,
                PROJECT.project_id,
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
                NOW.isoformat(),
                NOW.isoformat(),
            ),
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
                PROJECT.namespace,
                PROJECT.project_id,
                PROPOSAL_ID,
                item.source_status.value,
                item.source_revision,
                item.target_status.value,
                definition_digest,
                item.proposal_artifact_digest,
                item.content_revision,
                item.state_revision,
                item.decision_epoch,
                item.approval_disposition.value,
                NOW.isoformat(),
            ),
        )
    return store


class MutatingDefinitionObjectStore(ImmutableDefinitionObjectStore):
    def __init__(self, project_ref: ProjectRef, project_root: Path, legacy_file: Path) -> None:
        super().__init__(project_ref, project_root)
        self.legacy_file = legacy_file
        self.mutated = False

    def _after_publish(self, canonical: Path) -> None:
        if not self.mutated:
            self.mutated = True
            self.legacy_file.write_bytes(self.legacy_file.read_bytes() + b"mutated")


class CorruptingBackupObjectStore(ImmutableDefinitionObjectStore):
    def __init__(self, project_ref: ProjectRef, project_root: Path, backup: Path) -> None:
        super().__init__(project_ref, project_root)
        self.backup = backup
        self.corrupted = False

    def _after_publish(self, canonical: Path) -> None:
        if not self.corrupted:
            self.corrupted = True
            self.backup.write_bytes(b"destroyed backup")


class BlockingMigrationObjectStore(ImmutableDefinitionObjectStore):
    def __init__(self, project_ref: ProjectRef, project_root: Path, marker: Path) -> None:
        super().__init__(project_ref, project_root)
        self.marker = marker

    def _before_publish(self, temporary: Path, canonical: Path) -> None:
        self.marker.write_text("ready", encoding="utf-8")
        time.sleep(60)


class LegacyMigrationPublishGit:
    current_ref = "a13d92f" + "a" * 33
    candidate_commit = "b" * 40

    def read_ref(self, canonical_ref: str) -> str:
        assert canonical_ref == "refs/heads/main"
        return self.current_ref

    def inspect_candidate(
        self,
        candidate_commit: str,
        *,
        artifact_bytes: bytes,
        publish_request_bytes: bytes,
    ) -> CandidateCommitEvidence:
        assert candidate_commit == self.candidate_commit
        assert publish_request_bytes
        return CandidateCommitEvidence(
            candidate_commit=candidate_commit,
            parent_commit=self.current_ref,
            candidate_tree_digest=sha256_digest(artifact_bytes),
            canonical_ref="refs/heads/main",
        )


class AmbiguousCommitConnection:
    def __init__(self, connection: sqlite3.Connection, store: AmbiguousCommitStore) -> None:
        self._connection = connection
        self._store = store

    @property
    def in_transaction(self) -> bool:
        return self._connection.in_transaction

    def execute(self, statement: str, parameters: object = ()) -> sqlite3.Cursor:
        cursor = (
            self._connection.execute(statement)
            if parameters == ()
            else self._connection.execute(statement, parameters)  # type: ignore[arg-type]
        )
        if statement == "COMMIT":
            self._store.commit_count += 1
            if self._store.commit_count == self._store.fail_commit_number:
                raise sqlite3.OperationalError("injected after durable commit")
        return cursor

    def __getattr__(self, name: str) -> object:
        return getattr(self._connection, name)


class AmbiguousCommitStore(GovernanceStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.commit_count = 0
        self.fail_commit_number = -1

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        with super().connect() as connection:
            yield AmbiguousCommitConnection(connection, self)  # type: ignore[misc]


def _run_blocking_legacy_import(
    project_root: str,
    database: str,
    plan_json: str,
    backup_json: str,
    marker: str,
) -> None:
    root = Path(project_root)
    dry_run = LegacyProposalDryRunService(root, PROJECT)
    objects = BlockingMigrationObjectStore(PROJECT, root, Path(marker))
    service = LegacyProposalImportService(dry_run, GovernanceStore(Path(database)), objects)
    service.import_state(
        LegacyProposalMigrationPlan.model_validate_json(plan_json),
        LegacyMigrationBackupEvidence.model_validate_json(backup_json),
    )


def test_dry_run_is_deterministic_and_write_free(tmp_path: Path) -> None:
    root = _legacy_tree(tmp_path)
    before = _tree_inventory(root)
    service = LegacyProposalDryRunService(root, PROJECT)

    first = service.create_plan(
        freeze=_freeze(root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )
    second = service.create_plan(
        freeze=_freeze(root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )

    assert first == second
    assert first.plan_id == f"MPL-{first.plan_digest[-16:].upper()}"
    assert first.project_ref == PROJECT
    assert first.proposals[0].proposal_ref.project_ref == PROJECT
    assert first.proposals[0].target_status == "draft"
    assert first.proposals[0].approval_disposition is LegacyApprovalDisposition.NOT_REQUIRED
    plan_preimage = {
        "base_revision": first.base_revision,
        "freeze": first.freeze.model_dump(mode="json"),
        "mapping_policy_version": first.mapping_policy_version,
        "project_ref": first.project_ref.model_dump(mode="json"),
        "proposals": [item.model_dump(mode="json") for item in first.proposals],
        "snapshot_digest": first.snapshot_digest,
        "snapshot_id": first.snapshot_id,
        "validation_policy_ref": first.validation_policy_ref,
    }
    assert first.plan_digest == _canonical_digest(plan_preimage)
    assert _tree_inventory(root) == before
    assert not (root / ".amplai" / "runtime").exists()


def test_snapshot_hashes_every_file_in_byte_sorted_relative_path_order(tmp_path: Path) -> None:
    root = _legacy_tree(tmp_path)
    snapshot = LegacyProposalDryRunService(root, PROJECT).create_snapshot(_freeze(root))

    paths = tuple(item.relative_path for item in snapshot.files)
    assert paths == tuple(sorted(paths, key=lambda value: value.encode("utf-8")))
    assert paths == (
        f"{PROPOSAL_ID}/drafts/CON-0001.md",
        f"{PROPOSAL_ID}/proposal.yaml",
        f"{PROPOSAL_ID}/summary.md",
    )
    assert snapshot.total_bytes == sum(item.byte_length for item in snapshot.files)
    assert snapshot.snapshot_id == f"MPS-{snapshot.snapshot_digest[-16:].upper()}"
    for item in snapshot.files:
        payload = (root / ".amplai" / "proposals" / item.relative_path).read_bytes()
        assert item.content_digest == f"sha256:{hashlib.sha256(payload).hexdigest()}"
    snapshot_preimage = {
        "files": [item.model_dump(mode="json") for item in snapshot.files],
        "project_ref": PROJECT.model_dump(mode="json"),
    }
    assert snapshot.snapshot_digest == _canonical_digest(snapshot_preimage)


def test_plan_binds_freeze_evidence_but_snapshot_ignores_v3_object_directories(
    tmp_path: Path,
) -> None:
    root = _legacy_tree(tmp_path)
    definition = root / ".amplai" / "proposals" / PROPOSAL_ID / "definitions" / "sha256-object.yaml"
    definition.parent.mkdir()
    definition.write_text("v3 object\n", encoding="utf-8")
    service = LegacyProposalDryRunService(root, PROJECT)
    first = service.create_plan(
        freeze=_freeze(root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )
    second_freeze = _freeze(root).model_copy(update={"freeze_id": "MFR-0000000000000002"})
    second = service.create_plan(
        freeze=second_freeze,
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )

    assert first.snapshot_digest == second.snapshot_digest
    assert first.plan_digest != second.plan_digest
    assert all(
        "/definitions/" not in item.relative_path
        for item in service.create_snapshot(_freeze(root)).files
    )


def test_approved_or_applied_without_audit_requires_synthetic_review_hold(
    tmp_path: Path,
) -> None:
    for status in ("approved", "applied"):
        case = tmp_path / status
        root = _legacy_tree(case, status=status)
        item = (
            LegacyProposalDryRunService(root, PROJECT)
            .create_plan(
                freeze=_freeze(root),
                base_revision="a13d92f",
                validation_policy_ref="policy/migration/v1",
            )
            .proposals[0]
        )
        assert item.source_status.value == status
        assert item.target_status == "legacy_approval_review_required"
        assert item.approval_disposition is LegacyApprovalDisposition.SYNTHETIC_REQUIRED
        assert item.state_revision == 3


def test_approval_audit_artifact_changes_evidence_disposition(tmp_path: Path) -> None:
    root = _legacy_tree(tmp_path, status="approved")
    audit = root / ".amplai" / "proposals" / PROPOSAL_ID / "approval-audit.json"
    audit.write_bytes(_approval_audit_payload())

    item = (
        LegacyProposalDryRunService(root, PROJECT)
        .create_plan(
            freeze=_freeze(root),
            base_revision="a13d92f",
            validation_policy_ref="policy/migration/v1",
        )
        .proposals[0]
    )

    assert item.target_status == "approved"
    assert item.approval_disposition is LegacyApprovalDisposition.LEGACY_AUDIT_PRESENT


def test_project_scope_and_proposal_directory_are_qualified(tmp_path: Path) -> None:
    root = _legacy_tree(tmp_path)
    wrong_project = ProjectRef(project_id="cortex", namespace="org/default/project/cortex")
    with pytest.raises(LegacyMigrationScanError, match="LEGACY_PROJECT_SCOPE_MISMATCH"):
        LegacyProposalDryRunService(root, wrong_project).create_plan(
            freeze=_freeze(root, wrong_project),
            base_revision="a13d92f",
            validation_policy_ref="policy/migration/v1",
        )

    proposal_path = root / ".amplai" / "proposals" / PROPOSAL_ID / "proposal.yaml"
    renamed = proposal_path.parent.with_name("PROP-20260730-FFFFFFFF")
    proposal_path.parent.rename(renamed)
    with pytest.raises(LegacyMigrationScanError, match="LEGACY_PROPOSAL_PATH_MISMATCH"):
        LegacyProposalDryRunService(root, PROJECT).create_plan(
            freeze=_freeze(root),
            base_revision="a13d92f",
            validation_policy_ref="policy/migration/v1",
        )


def test_nested_proposal_yaml_cannot_duplicate_qualified_identity(tmp_path: Path) -> None:
    root = _legacy_tree(tmp_path)
    nested = root / ".amplai" / "proposals" / PROPOSAL_ID / "drafts" / PROPOSAL_ID / "proposal.yaml"
    nested.parent.mkdir()
    nested.write_bytes(_proposal_payload())

    with pytest.raises(LegacyMigrationScanError, match="LEGACY_PROPOSAL_PATH_INVALID"):
        LegacyProposalDryRunService(root, PROJECT).create_plan(
            freeze=_freeze(root),
            base_revision="a13d92f",
            validation_policy_ref="policy/migration/v1",
        )


@pytest.mark.parametrize(
    ("change", "code"),
    (
        (
            {
                "project_ref": ProjectRef(
                    project_id="cortex", namespace="org/default/project/cortex"
                )
            },
            "LEGACY_FREEZE_PROJECT_MISMATCH",
        ),
        ({"source_root": "/tmp/not-the-source"}, "LEGACY_FREEZE_SOURCE_ROOT_MISMATCH"),
        ({"base_revision": "b" * 40}, "LEGACY_FREEZE_BASE_REVISION_MISMATCH"),
    ),
)
def test_freeze_evidence_is_scoped_to_project_source_and_base(
    tmp_path: Path,
    change: dict[str, object],
    code: str,
) -> None:
    root = _legacy_tree(tmp_path)
    freeze = _freeze(root).model_copy(update=change)
    with pytest.raises(LegacyMigrationScanError, match=code):
        LegacyProposalDryRunService(root, PROJECT).create_plan(
            freeze=freeze,
            base_revision="a13d92f",
            validation_policy_ref="policy/migration/v1",
        )


def test_exported_plan_and_snapshot_reject_derived_identity_tamper(tmp_path: Path) -> None:
    root = _legacy_tree(tmp_path)
    service = LegacyProposalDryRunService(root, PROJECT)
    snapshot = service.create_snapshot(_freeze(root))
    plan = service.create_plan(
        freeze=_freeze(root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )

    snapshot_payload = snapshot.model_dump(mode="json")
    snapshot_payload["total_bytes"] += 1
    with pytest.raises(ValidationError):
        type(snapshot).model_validate(snapshot_payload)
    plan_payload = plan.model_dump(mode="json")
    plan_payload["plan_digest"] = f"sha256:{'f' * 64}"
    with pytest.raises(ValidationError):
        type(plan).model_validate(plan_payload)
    snapshot_ref_payload = plan.model_dump(mode="json")
    snapshot_ref_payload["snapshot_id"] = "MPS-0000000000000000"
    with pytest.raises(ValidationError):
        type(plan).model_validate(snapshot_ref_payload)


def test_same_local_proposal_id_in_different_projects_has_distinct_plan_identity(
    tmp_path: Path,
) -> None:
    cortex = ProjectRef(project_id="cortex", namespace="org/default/project/cortex")
    amplai_root = _legacy_tree(tmp_path / "one", project=PROJECT)
    cortex_root = _legacy_tree(tmp_path / "two", project=cortex)

    amplai = LegacyProposalDryRunService(amplai_root, PROJECT).create_plan(
        freeze=_freeze(amplai_root, PROJECT),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )
    cortex_plan = LegacyProposalDryRunService(cortex_root, cortex).create_plan(
        freeze=_freeze(cortex_root, cortex),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )

    assert (
        amplai.proposals[0].proposal_ref.proposal_id
        == cortex_plan.proposals[0].proposal_ref.proposal_id
    )
    assert amplai.plan_digest != cortex_plan.plan_digest
    assert amplai.snapshot_digest != cortex_plan.snapshot_digest


def test_symlink_nonregular_and_size_limit_fail_closed(tmp_path: Path) -> None:
    root = _legacy_tree(tmp_path / "symlink")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    os.symlink(
        outside,
        root / ".amplai" / "proposals" / PROPOSAL_ID / "drafts" / "linked.md",
    )
    with pytest.raises(LegacyMigrationScanError, match="LEGACY_SOURCE_SYMLINK"):
        LegacyProposalDryRunService(root, PROJECT).create_snapshot(_freeze(root))

    limited_root = _legacy_tree(tmp_path / "limited")
    with pytest.raises(LegacyMigrationScanError, match="LEGACY_SNAPSHOT_FILE_SIZE_LIMIT"):
        LegacyProposalDryRunService(
            limited_root,
            PROJECT,
            config=LegacyMigrationScanConfig(maximum_file_bytes=8),
        ).create_snapshot(_freeze(limited_root))


def test_nonregular_file_count_total_limit_and_root_symlink_fail_closed(
    tmp_path: Path,
) -> None:
    fifo_root = _legacy_tree(tmp_path / "fifo")
    os.mkfifo(fifo_root / ".amplai" / "proposals" / PROPOSAL_ID / "legacy.pipe")
    with pytest.raises(LegacyMigrationScanError, match="LEGACY_SOURCE_NOT_REGULAR"):
        LegacyProposalDryRunService(fifo_root, PROJECT).create_snapshot(_freeze(fifo_root))

    count_root = _legacy_tree(tmp_path / "count")
    with pytest.raises(LegacyMigrationScanError, match="LEGACY_SNAPSHOT_FILE_LIMIT"):
        LegacyProposalDryRunService(
            count_root,
            PROJECT,
            config=LegacyMigrationScanConfig(maximum_files=2),
        ).create_snapshot(_freeze(count_root))

    inclusive_count_root = _legacy_tree(tmp_path / "inclusive-count")
    snapshot = LegacyProposalDryRunService(
        inclusive_count_root,
        PROJECT,
        config=LegacyMigrationScanConfig(maximum_files=3),
    ).create_snapshot(_freeze(inclusive_count_root))
    assert len(snapshot.files) == 3

    entry_root = _legacy_tree(tmp_path / "entries")
    with pytest.raises(LegacyMigrationScanError, match="LEGACY_SNAPSHOT_ENTRY_LIMIT"):
        LegacyProposalDryRunService(
            entry_root,
            PROJECT,
            config=LegacyMigrationScanConfig(maximum_entries=2),
        ).create_snapshot(_freeze(entry_root))

    total_root = _legacy_tree(tmp_path / "total")
    total_read_paths: list[Path] = []
    with pytest.raises(LegacyMigrationScanError, match="LEGACY_SNAPSHOT_TOTAL_LIMIT"):
        LegacyProposalDryRunService(
            total_root,
            PROJECT,
            config=LegacyMigrationScanConfig(maximum_total_bytes=1),
            after_file_read=total_read_paths.append,
        ).create_snapshot(_freeze(total_root))
    assert total_read_paths == []

    actual_root = _legacy_tree(tmp_path / "actual")
    linked_root = tmp_path / "linked-project"
    os.symlink(actual_root, linked_root)
    with pytest.raises(LegacyMigrationScanError, match="LEGACY_PROJECT_ROOT_SYMLINK"):
        LegacyProposalDryRunService(linked_root, PROJECT).create_snapshot(_freeze(linked_root))


def test_scan_detects_file_mutation_between_read_and_final_enumeration(tmp_path: Path) -> None:
    root = _legacy_tree(tmp_path)
    mutated = False

    def mutate_once(path: Path) -> None:
        nonlocal mutated
        if not mutated:
            mutated = True
            path.write_bytes(path.read_bytes() + b"changed")

    service = LegacyProposalDryRunService(
        root,
        PROJECT,
        after_file_read=mutate_once,
    )
    with pytest.raises(LegacyMigrationScanError, match="LEGACY_SNAPSHOT_MUTATED"):
        service.create_snapshot(_freeze(root))


def test_scan_detects_same_length_rewrite_with_restored_mtime(tmp_path: Path) -> None:
    root = _legacy_tree(tmp_path)
    mutated = False

    def mutate_once(path: Path) -> None:
        nonlocal mutated
        if mutated:
            return
        mutated = True
        metadata = path.stat()
        payload = bytearray(path.read_bytes())
        payload[0] ^= 1
        path.write_bytes(payload)
        os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))

    service = LegacyProposalDryRunService(
        root,
        PROJECT,
        after_file_read=mutate_once,
    )
    with pytest.raises(LegacyMigrationScanError, match="LEGACY_SNAPSHOT_MUTATED"):
        service.create_snapshot(_freeze(root))


def test_deep_yaml_and_event_limit_fail_with_normalized_error(tmp_path: Path) -> None:
    deep_root = _legacy_tree(tmp_path / "deep")
    proposal = deep_root / ".amplai" / "proposals" / PROPOSAL_ID / "proposal.yaml"
    proposal.write_bytes(b"[" * 1_200 + b"0" + b"]" * 1_200)
    with pytest.raises(
        LegacyMigrationScanError,
        match=r"LEGACY_PROPOSAL_STRUCTURE_LIMIT|LEGACY_PROPOSAL_INVALID",
    ):
        LegacyProposalDryRunService(deep_root, PROJECT).create_plan(
            freeze=_freeze(deep_root),
            base_revision="a13d92f",
            validation_policy_ref="policy/migration/v1",
        )

    event_root = _legacy_tree(tmp_path / "events")
    with pytest.raises(
        LegacyMigrationScanError,
        match="LEGACY_PROPOSAL_STRUCTURE_LIMIT",
    ):
        LegacyProposalDryRunService(
            event_root,
            PROJECT,
            config=LegacyMigrationScanConfig(maximum_yaml_events=2),
        ).create_plan(
            freeze=_freeze(event_root),
            base_revision="a13d92f",
            validation_policy_ref="policy/migration/v1",
        )


def test_missing_source_root_fails_with_normalized_error(tmp_path: Path) -> None:
    root = tmp_path / "missing-project"
    with pytest.raises(LegacyMigrationScanError, match="LEGACY_SOURCE_ROOT_INVALID"):
        LegacyProposalDryRunService(root, PROJECT).create_snapshot(_freeze(root))


@pytest.mark.parametrize(
    ("base_revision", "policy", "code"),
    (
        ("not-a-revision", "policy/migration/v1", "LEGACY_BASE_REVISION_INVALID"),
        ("a13d92f", " ", "LEGACY_VALIDATION_POLICY_INVALID"),
    ),
)
def test_plan_rejects_invalid_migration_contract(
    tmp_path: Path,
    base_revision: str,
    policy: str,
    code: str,
) -> None:
    root = _legacy_tree(tmp_path)
    with pytest.raises(LegacyMigrationScanError, match=code):
        LegacyProposalDryRunService(root, PROJECT).create_plan(
            freeze=_freeze(root),
            base_revision=base_revision,
            validation_policy_ref=policy,
        )


def test_import_requires_durable_backups_and_atomically_creates_qualified_state(
    tmp_path: Path,
) -> None:
    root = _legacy_tree(tmp_path / "project", revision=7)
    store, objects, dry_run, service, backup = _import_fixture(
        tmp_path / "fixture",
        root,
    )
    plan = dry_run.create_plan(
        freeze=_freeze(root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )
    assert plan.proposals[0].source_revision == 7
    assert plan.proposals[0].content_revision == 1

    prepared = service.prepare(plan, backup)
    assert prepared.status == "prepared"
    assert prepared.definition_digests == ()
    assert ActiveProposalRepository(store, objects).get(plan.proposals[0].proposal_ref) is None

    imported = service.import_state(plan, backup)
    assert imported.status == "state_imported"
    assert imported.proposal_count == 1
    assert len(imported.definition_digests) == 1
    view = ActiveProposalRepository(store, objects).get(plan.proposals[0].proposal_ref)
    assert view is not None
    assert view.active_definition_digest == imported.definition_digests[0]
    assert (view.content_revision, view.state_revision, view.decision_epoch) == (1, 1, 1)
    assert view.status == "draft"
    definition_bytes = objects.get_definition_object(
        plan.proposals[0].proposal_ref,
        view.active_definition_digest,
    )
    manifest = ProposalDefinitionManifest.model_validate_json(definition_bytes)
    assert len(manifest.apply_inputs) == 3
    expected_inputs = {
        path.relative_to(root / ".amplai" / "proposals" / PROPOSAL_ID).as_posix(): path.read_bytes()
        for path in (root / ".amplai" / "proposals" / PROPOSAL_ID).rglob("*")
        if path.is_file() and "definitions" not in path.parts and "inputs" not in path.parts
    }
    assert {item.logical_name for item in manifest.apply_inputs} == set(expected_inputs)
    for descriptor in manifest.apply_inputs:
        payload = objects.get_input_object(
            plan.proposals[0].proposal_ref,
            descriptor.object_digest,
        )
        assert payload == expected_inputs[descriptor.logical_name]
        assert descriptor.object_digest == (f"sha256:{hashlib.sha256(payload).hexdigest()}")
    source_payload = yaml.safe_load(expected_inputs["proposal.yaml"])
    expected_descriptors = [
        {
            "logical_name": logical_name,
            "object_digest": f"sha256:{hashlib.sha256(payload).hexdigest()}",
            "media_type": ("text/markdown" if logical_name.endswith(".md") else "application/yaml"),
        }
        for logical_name, payload in sorted(
            expected_inputs.items(), key=lambda entry: entry[0].encode("utf-8")
        )
    ]
    definition_preimage = {
        "schema_version": 3,
        "proposal_ref": {
            "project_ref": {
                "project_id": "amplai",
                "namespace": "org/default/project/amplai",
            },
            "proposal_id": PROPOSAL_ID,
        },
        "canonicalization_version": 1,
        "operations": [
            {
                **source_payload["operations"][0],
                "expected_revision": None,
                "expected_target_sha256": None,
                "target_id": None,
            }
        ],
        "evidence": [],
        "apply_inputs": expected_descriptors,
        "preconditions": [
            {
                "legacy_migration": {
                    "mapping_policy_version": 1,
                    "proposal_artifact_digest": plan.proposals[0].proposal_artifact_digest,
                    "snapshot_digest": plan.snapshot_digest,
                    "source_revision": 7,
                    "source_status": "draft",
                }
            }
        ],
        "base_revision": "a13d92f",
        "validation_policy_ref": "policy/migration/v1",
    }
    independent_definition_digest = _canonical_digest(definition_preimage)
    assert manifest.definition_digest == independent_definition_digest
    expected_manifest = {
        **definition_preimage,
        "definition_digest": independent_definition_digest,
    }
    expected_definition_bytes = (
        json.dumps(
            expected_manifest,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    assert definition_bytes == expected_definition_bytes
    proposal_input = next(
        item for item in manifest.apply_inputs if item.logical_name == "proposal.yaml"
    )
    assert (
        objects.get_input_object(
            plan.proposals[0].proposal_ref,
            proposal_input.object_digest,
        )
        == (root / ".amplai" / "proposals" / PROPOSAL_ID / "proposal.yaml").read_bytes()
    )
    assert manifest.preconditions[0]["legacy_migration"]["source_revision"] == 7

    repeated = service.import_state(plan, backup)
    assert repeated == imported
    Path(backup.project_pack_backup_path).unlink()
    Path(backup.governance_backup_path).unlink()
    for legacy_source in expected_inputs:
        (root / ".amplai" / "proposals" / PROPOSAL_ID / legacy_source).unlink()
    assert service.import_state(plan, backup) == imported
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_active_proposals"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT source_revision, content_revision FROM governance_legacy_migration_items"
        ).fetchone() == (7, 1)
        root_evidence = connection.execute(
            """
            SELECT project_pack_backup_digest, governance_backup_digest, status
            FROM governance_legacy_migrations
            """
        ).fetchone()
    assert root_evidence == (
        backup.project_pack_backup_digest,
        backup.governance_backup_digest,
        "state_imported",
    )
    with store.connect() as connection, governance_transaction(connection):
        connection.execute(
            """
            DELETE FROM governance_definition_revisions
            WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?
            """,
            (PROJECT.namespace, PROJECT.project_id, PROPOSAL_ID),
        )
    with pytest.raises(
        LegacyMigrationScanError,
        match="LEGACY_MIGRATION_REPLAY_CONFLICT",
    ):
        service.import_state(plan, backup)


def test_verification_rescans_source_and_persists_bidirectional_report(
    tmp_path: Path,
) -> None:
    root = _legacy_tree(tmp_path / "project")
    store, _objects, dry_run, service, backup = _import_fixture(tmp_path / "fixture", root)
    plan = dry_run.create_plan(
        freeze=_freeze(root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )
    imported = service.import_state(plan, backup)

    report = service.verify_import(plan, backup)
    replay = service.verify_import(plan, backup)

    assert report.migration_id == imported.migration_id
    assert report.snapshot_digest == plan.snapshot_digest
    assert report.plan_digest == plan.plan_digest
    assert len(report.source_files) == 3
    assert len(report.proposals) == 1
    assert report.proposals[0].definition_digest == imported.definition_digests[0]
    assert report.proposals[0].runtime_status == "draft"
    assert not report.replayed
    assert replay.model_copy(update={"replayed": False}) == report
    assert replay.replayed
    with store.connect() as connection:
        persisted = connection.execute(
            """
            SELECT verification_id, report_digest, proposal_count, source_file_count,
                   verified_by
            FROM governance_legacy_migration_verifications
            """
        ).fetchone()
        assert persisted == (
            report.verification_id,
            report.report_digest,
            1,
            3,
            plan.freeze.actor_id,
        )
        with pytest.raises(sqlite3.DatabaseError, match="verification is immutable"):
            connection.execute(
                "UPDATE governance_legacy_migration_verifications SET verified_by = 'other'"
            )
        with pytest.raises(sqlite3.DatabaseError, match="verification is durable"):
            connection.execute("DELETE FROM governance_legacy_migration_verifications")


def test_verification_insert_guard_rejects_prepared_or_unbound_root(tmp_path: Path) -> None:
    root = _legacy_tree(tmp_path / "project")
    store, _objects, dry_run, service, backup = _import_fixture(tmp_path / "fixture", root)
    plan = dry_run.create_plan(
        freeze=_freeze(root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )
    service.prepare(plan, backup)

    with (
        store.connect() as connection,
        pytest.raises(
            sqlite3.DatabaseError,
            match="legacy verification root mismatch",
        ),
    ):
        connection.execute(
            """
            INSERT INTO governance_legacy_migration_verifications(
                verification_id, migration_id, project_namespace, project_id,
                snapshot_id, snapshot_digest, plan_digest, proposal_count,
                source_file_count, report_digest, report_json, verified_by,
                verified_at
            ) VALUES ('MVF-0000000000000001', ?, ?, ?, ?, ?, ?, 1, 1, ?, '{}',
                      'forged', ?)
            """,
            (
                plan.plan_id,
                PROJECT.namespace,
                PROJECT.project_id,
                plan.snapshot_id,
                plan.snapshot_digest,
                plan.plan_digest,
                f"sha256:{'f' * 64}",
                NOW.isoformat(),
            ),
        )


@pytest.mark.parametrize("forgery", ["scalar_json", "source_hash", "command"])
def test_v21_forged_verification_json_is_rejected_on_replay_and_upgrade(
    tmp_path: Path,
    forgery: str,
) -> None:
    root = _legacy_tree(tmp_path / "project")
    path = tmp_path / "runtime" / "governance.db"
    v21 = GovernanceStore(path, migration_runner=MigrationRunner(INITIAL_MIGRATIONS[:21]))
    v21.initialize()
    store, _objects, dry_run, service, backup = _import_fixture(
        tmp_path / "fixture",
        root,
        store=v21,
    )
    plan = dry_run.create_plan(
        freeze=_freeze(root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )
    service.import_state(plan, backup)
    snapshot = dry_run.create_snapshot(plan.freeze)
    with store.connect() as connection:
        proposals = service._verification_proposals(connection, plan)
    verification_id, report_digest = service._verification_identity(plan, snapshot, proposals)
    source_files = snapshot.files
    forged_proposals = proposals
    verified_by = plan.freeze.actor_id
    if forgery == "source_hash":
        source_files = (
            source_files[0].model_copy(update={"content_digest": f"sha256:{'f' * 64}"}),
            *source_files[1:],
        )
    elif forgery == "command":
        forged_proposals = (
            forged_proposals[0].model_copy(update={"command_id": "MCM-0000000000000001"}),
            *forged_proposals[1:],
        )
    else:
        verified_by = "different-actor"
    foreign_preimage = {
        "migration_id": plan.plan_id,
        "plan_digest": plan.plan_digest,
        "project_ref": plan.project_ref.model_dump(mode="json"),
        "proposals": [item.model_dump(mode="json") for item in forged_proposals],
        "snapshot_digest": snapshot.snapshot_digest,
        "snapshot_id": snapshot.snapshot_id,
        "source_files": [item.model_dump(mode="json") for item in source_files],
        "source_total_bytes": snapshot.total_bytes,
        "verified_by": verified_by,
    }
    foreign_digest = _canonical_digest(foreign_preimage)
    foreign = LegacyMigrationVerificationReport(
        verification_id=f"MVF-{foreign_digest[-16:].upper()}",
        migration_id=plan.plan_id,
        project_ref=plan.project_ref,
        snapshot_id=plan.snapshot_id,
        snapshot_digest=plan.snapshot_digest,
        plan_digest=plan.plan_digest,
        source_files=source_files,
        source_total_bytes=snapshot.total_bytes,
        proposals=forged_proposals,
        report_digest=foreign_digest,
        verified_by=verified_by,
        verified_at=NOW,
    )
    row_verification_id = verification_id if forgery == "scalar_json" else foreign.verification_id
    row_report_digest = report_digest if forgery == "scalar_json" else foreign.report_digest
    foreign_payload = foreign.model_dump(mode="json")
    with store.connect() as connection:
        connection.execute(
            """
            INSERT INTO governance_legacy_migration_verifications(
                verification_id, migration_id, project_namespace, project_id,
                snapshot_id, snapshot_digest, plan_digest, proposal_count,
                source_file_count, report_digest, report_json, verified_by,
                verified_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row_verification_id,
                plan.plan_id,
                PROJECT.namespace,
                PROJECT.project_id,
                plan.snapshot_id,
                plan.snapshot_digest,
                plan.plan_digest,
                len(proposals),
                len(snapshot.files),
                row_report_digest,
                json.dumps(
                    foreign_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                foreign.verified_by,
                str(foreign_payload["verified_at"]),
            ),
        )

    with pytest.raises(
        LegacyMigrationScanError,
        match="LEGACY_MIGRATION_VERIFICATION_CONFLICT",
    ):
        service.verify_import(plan, backup)
    with pytest.raises(GovernanceEventError, match="LEGACY_MIGRATION_VERIFICATION_ROOT_MISMATCH"):
        GovernanceStore(path).initialize()
    with v21.connect() as connection:
        assert connection.execute(
            "SELECT MAX(version) FROM governance_schema_migrations"
        ).fetchone() == (21,)


def test_v21_verification_without_command_idempotency_key_upgrades_and_replays(
    tmp_path: Path,
) -> None:
    root = _legacy_tree(tmp_path / "project")
    path = tmp_path / "runtime" / "governance.db"
    v21 = GovernanceStore(path, migration_runner=MigrationRunner(INITIAL_MIGRATIONS[:21]))
    v21.initialize()
    store, _objects, dry_run, service, backup = _import_fixture(
        tmp_path / "fixture",
        root,
        store=v21,
    )
    plan = dry_run.create_plan(
        freeze=_freeze(root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )
    service.import_state(plan, backup)
    snapshot = dry_run.create_snapshot(plan.freeze)
    with store.connect() as connection:
        current_proposals = service._verification_proposals(connection, plan)
    predecessor_proposals = tuple(
        item.model_copy(update={"idempotency_key": None}) for item in current_proposals
    )
    verification_id, report_digest = service._verification_identity(
        plan,
        snapshot,
        predecessor_proposals,
    )
    predecessor = LegacyMigrationVerificationReport(
        verification_id=verification_id,
        migration_id=plan.plan_id,
        project_ref=plan.project_ref,
        snapshot_id=snapshot.snapshot_id,
        snapshot_digest=snapshot.snapshot_digest,
        plan_digest=plan.plan_digest,
        source_files=snapshot.files,
        source_total_bytes=snapshot.total_bytes,
        proposals=predecessor_proposals,
        report_digest=report_digest,
        verified_by=plan.freeze.actor_id,
        verified_at=NOW,
    )
    predecessor_payload = predecessor.model_dump(mode="json", exclude_none=True)
    with store.connect() as connection:
        connection.execute(
            """
            INSERT INTO governance_legacy_migration_verifications(
                verification_id, migration_id, project_namespace, project_id,
                snapshot_id, snapshot_digest, plan_digest, proposal_count,
                source_file_count, report_digest, report_json, verified_by,
                verified_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                predecessor.verification_id,
                plan.plan_id,
                PROJECT.namespace,
                PROJECT.project_id,
                snapshot.snapshot_id,
                snapshot.snapshot_digest,
                plan.plan_digest,
                len(predecessor.proposals),
                len(snapshot.files),
                predecessor.report_digest,
                json.dumps(
                    predecessor_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                predecessor.verified_by,
                str(predecessor_payload["verified_at"]),
            ),
        )

    assert GovernanceStore(path).initialize().schema_version == len(INITIAL_MIGRATIONS)
    replay = service.verify_import(plan, backup)
    assert replay.replayed
    assert replay.proposals[0].idempotency_key is None


def test_latest_schema_rejects_new_predecessor_shaped_verification(tmp_path: Path) -> None:
    root = _legacy_tree(tmp_path / "project")
    store, _objects, dry_run, service, backup = _import_fixture(tmp_path / "fixture", root)
    plan = dry_run.create_plan(
        freeze=_freeze(root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )
    service.import_state(plan, backup)
    snapshot = dry_run.create_snapshot(plan.freeze)
    with store.connect() as connection:
        proposals = service._verification_proposals(connection, plan)
    predecessor_proposals = tuple(
        item.model_copy(update={"idempotency_key": None}) for item in proposals
    )
    verification_id, report_digest = service._verification_identity(
        plan,
        snapshot,
        predecessor_proposals,
    )
    predecessor = LegacyMigrationVerificationReport(
        verification_id=verification_id,
        migration_id=plan.plan_id,
        project_ref=plan.project_ref,
        snapshot_id=snapshot.snapshot_id,
        snapshot_digest=snapshot.snapshot_digest,
        plan_digest=plan.plan_digest,
        source_files=snapshot.files,
        source_total_bytes=snapshot.total_bytes,
        proposals=predecessor_proposals,
        report_digest=report_digest,
        verified_by=plan.freeze.actor_id,
        verified_at=NOW,
    )
    predecessor_payload = predecessor.model_dump(mode="json", exclude_none=True)

    with (
        store.connect() as connection,
        pytest.raises(
            sqlite3.DatabaseError,
            match="legacy verification idempotency evidence is required",
        ),
    ):
        connection.execute(
            """
            INSERT INTO governance_legacy_migration_verifications(
                verification_id, migration_id, project_namespace, project_id,
                snapshot_id, snapshot_digest, plan_digest, proposal_count,
                source_file_count, report_digest, report_json, verified_by,
                verified_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                predecessor.verification_id,
                plan.plan_id,
                PROJECT.namespace,
                PROJECT.project_id,
                snapshot.snapshot_id,
                snapshot.snapshot_digest,
                plan.plan_digest,
                len(predecessor.proposals),
                len(snapshot.files),
                predecessor.report_digest,
                json.dumps(
                    predecessor_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                predecessor.verified_by,
                str(predecessor_payload["verified_at"]),
            ),
        )


def test_verification_reconciles_after_durable_ambiguous_commit(tmp_path: Path) -> None:
    root = _legacy_tree(tmp_path / "project")
    store = AmbiguousCommitStore(tmp_path / "runtime" / "governance.db")
    store.initialize()
    objects = ImmutableDefinitionObjectStore(PROJECT, root)
    dry_run = LegacyProposalDryRunService(root, PROJECT)
    service = LegacyProposalImportService(dry_run, store, objects)
    backup = _backup_evidence(tmp_path / "fixture", root, store)
    plan = dry_run.create_plan(
        freeze=_freeze(root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )
    service.import_state(plan, backup)
    store.commit_count = 0
    store.fail_commit_number = 2

    report = service.verify_import(plan, backup)

    assert report.replayed
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_legacy_migration_verifications"
        ).fetchone() == (1,)


def test_verified_legacy_approval_can_progress_and_restart(tmp_path: Path) -> None:
    root = _legacy_tree(tmp_path / "project", status="approved")
    store, _objects, dry_run, service, backup = _import_fixture(tmp_path / "fixture", root)
    plan = dry_run.create_plan(
        freeze=_freeze(root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )
    service.import_state(plan, backup)
    service.verify_import(plan, backup)
    authority_request = _migration_authority(store)
    LegacyApprovalReviewService(
        store,
        AuthorityService(store, clock=lambda: NOW),
        clock=lambda: NOW,
    ).resolve(
        plan.proposals[0].proposal_ref,
        authority_request=authority_request,
        reason="human reviewed verified legacy approval",
        idempotency_key="legacy-review:verified:1",
        request_fingerprint="c" * 64,
    )

    assert GovernanceStore(store.path).initialize().schema_version == len(INITIAL_MIGRATIONS)


def test_verified_migration_activation_releases_outbox_and_replays(tmp_path: Path) -> None:
    root = _legacy_tree(tmp_path / "project")
    store, _objects, dry_run, import_service, backup = _import_fixture(
        tmp_path / "fixture",
        root,
    )
    plan = dry_run.create_plan(
        freeze=_freeze(root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )
    import_service.import_state(plan, backup)
    report = import_service.verify_import(plan, backup)
    with store.connect() as connection:
        assert connection.execute(
            """
            SELECT verification_id, report_digest, state, lifecycle_revision
            FROM governance_legacy_migration_lifecycle_heads
            WHERE migration_id = ?
            """,
            (plan.plan_id,),
        ).fetchone() == (
            report.verification_id,
            report.report_digest,
            "staged_verified",
            2,
        )
    dispatcher = OutboxDispatcher(store, clock=lambda: NOW)
    assert dispatcher.claim_next("before-activation") is None
    authority_request = _migration_authority(store)
    activation = LegacyMigrationActivationService(
        store,
        AuthorityService(store, clock=lambda: NOW),
        clock=lambda: NOW,
    )

    result = activation.activate(
        plan.plan_id,
        authority_request=authority_request,
        expected_lifecycle_revision=2,
        reason="release verified migration projections",
        idempotency_key="legacy-activation:verified:1",
        request_fingerprint="a" * 64,
    )
    replay = activation.activate(
        plan.plan_id,
        authority_request=authority_request,
        expected_lifecycle_revision=2,
        reason="release verified migration projections",
        idempotency_key="legacy-activation:verified:1",
        request_fingerprint="a" * 64,
    )

    assert result.state.value == "activated"
    assert result.lifecycle_revision == 3
    assert replay.model_copy(update={"replayed": False}) == result
    assert replay.replayed
    with pytest.raises(LegacyMigrationLifecycleError, match="IDEMPOTENCY_CONFLICT"):
        activation.activate(
            plan.plan_id,
            authority_request=authority_request,
            expected_lifecycle_revision=2,
            reason="release verified migration projections",
            idempotency_key="legacy-activation:verified:1",
            request_fingerprint="f" * 64,
        )
    assert dispatcher.claim_next("after-activation") is not None
    with store.connect() as connection:
        head = connection.execute(
            "SELECT state, lifecycle_revision, last_event_digest "
            "FROM governance_legacy_migration_lifecycle_heads WHERE migration_id = ?",
            (plan.plan_id,),
        ).fetchone()
        event_digest = connection.execute(
            "SELECT event_digest FROM governance_legacy_migration_lifecycle_events "
            "WHERE migration_id = ?",
            (plan.plan_id,),
        ).fetchone()
    assert event_digest is not None
    assert head == ("activated", 3, event_digest[0])


def test_activation_requires_verification_and_does_not_release_outbox(tmp_path: Path) -> None:
    root = _legacy_tree(tmp_path / "project")
    store, _objects, dry_run, import_service, backup = _import_fixture(
        tmp_path / "fixture",
        root,
    )
    plan = dry_run.create_plan(
        freeze=_freeze(root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )
    import_service.import_state(plan, backup)
    authority_request = _migration_authority(store)

    with pytest.raises(
        LegacyMigrationLifecycleError,
        match="LEGACY_MIGRATION_NOT_VERIFIED",
    ):
        LegacyMigrationActivationService(
            store,
            AuthorityService(store, clock=lambda: NOW),
            clock=lambda: NOW,
        ).activate(
            plan.plan_id,
            authority_request=authority_request,
            expected_lifecycle_revision=1,
            reason="activation must require verification",
            idempotency_key="legacy-activation:unverified:1",
            request_fingerprint="b" * 64,
        )

    assert OutboxDispatcher(store, clock=lambda: NOW).claim_next("still-staged") is None
    with store.connect() as connection:
        assert connection.execute(
            "SELECT state, lifecycle_revision FROM "
            "governance_legacy_migration_lifecycle_heads WHERE migration_id = ?",
            (plan.plan_id,),
        ).fetchone() == ("imported", 1)
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_legacy_migration_lifecycle_commands"
        ).fetchone() == (0,)


def test_activation_requires_human_activation_permission(tmp_path: Path) -> None:
    root = _legacy_tree(tmp_path / "project")
    store, _objects, dry_run, import_service, backup = _import_fixture(
        tmp_path / "fixture",
        root,
    )
    plan = dry_run.create_plan(
        freeze=_freeze(root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )
    import_service.import_state(plan, backup)
    import_service.verify_import(plan, backup)
    authority_request = _migration_authority(store, grant_activation=False)

    with pytest.raises(LegacyMigrationLifecycleError, match="AUTHORITY_DENIED"):
        LegacyMigrationActivationService(
            store,
            AuthorityService(store, clock=lambda: NOW),
            clock=lambda: NOW,
        ).activate(
            plan.plan_id,
            authority_request=authority_request,
            expected_lifecycle_revision=2,
            reason="unauthorized migration activation",
            idempotency_key="legacy-activation:unauthorized:1",
            request_fingerprint="c" * 64,
        )

    assert OutboxDispatcher(store, clock=lambda: NOW).claim_next("unauthorized") is None


def test_concurrent_identical_activation_converges_to_one_result(tmp_path: Path) -> None:
    root = _legacy_tree(tmp_path / "project")
    store, _objects, dry_run, import_service, backup = _import_fixture(
        tmp_path / "fixture",
        root,
    )
    plan = dry_run.create_plan(
        freeze=_freeze(root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )
    import_service.import_state(plan, backup)
    import_service.verify_import(plan, backup)
    authority_request = _migration_authority(store)
    service = LegacyMigrationActivationService(
        store,
        AuthorityService(store, clock=lambda: NOW),
        clock=lambda: NOW,
    )
    barrier = threading.Barrier(2)

    def activate() -> LegacyMigrationActivationResult:
        barrier.wait()
        return service.activate(
            plan.plan_id,
            authority_request=authority_request,
            expected_lifecycle_revision=2,
            reason="concurrent exact activation",
            idempotency_key="legacy-activation:concurrent:1",
            request_fingerprint="d" * 64,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _index: activate(), range(2)))

    assert results[0].result_digest == results[1].result_digest
    assert sorted(result.replayed for result in results) == [False, True]
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_legacy_migration_lifecycle_commands"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_legacy_migration_lifecycle_events"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_legacy_migration_lifecycle_results"
        ).fetchone() == (1,)


def test_activation_reconciles_after_durable_ambiguous_commit(tmp_path: Path) -> None:
    root = _legacy_tree(tmp_path / "project")
    store = AmbiguousCommitStore(tmp_path / "runtime" / "governance.db")
    store.initialize()
    store, _objects, dry_run, import_service, backup = _import_fixture(
        tmp_path / "fixture",
        root,
        store=store,
    )
    plan = dry_run.create_plan(
        freeze=_freeze(root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )
    import_service.import_state(plan, backup)
    import_service.verify_import(plan, backup)
    authority_request = _migration_authority(store)
    store.commit_count = 0
    store.fail_commit_number = 1

    result = LegacyMigrationActivationService(
        store,
        AuthorityService(store, clock=lambda: NOW),
        clock=lambda: NOW,
    ).activate(
        plan.plan_id,
        authority_request=authority_request,
        expected_lifecycle_revision=2,
        reason="recover durable activation commit",
        idempotency_key="legacy-activation:ambiguous:1",
        request_fingerprint="9" * 64,
    )

    assert result.replayed
    assert result.state.value == "activated"
    assert OutboxDispatcher(store, clock=lambda: NOW).claim_next("after-ambiguous") is not None


def test_startup_rejects_incomplete_lifecycle_command_root(tmp_path: Path) -> None:
    root = _legacy_tree(tmp_path / "project")
    store, _objects, dry_run, import_service, backup = _import_fixture(
        tmp_path / "fixture",
        root,
    )
    plan = dry_run.create_plan(
        freeze=_freeze(root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )
    import_service.import_state(plan, backup)
    import_service.verify_import(plan, backup)
    with store.connect() as connection:
        connection.execute(
            """
            INSERT INTO governance_legacy_migration_lifecycle_commands(
                command_id, migration_id, project_namespace, project_id, action,
                expected_lifecycle_revision, idempotency_key, request_fingerprint,
                actor_id, actor_type, request_id, channel_json, reason, occurred_at
            ) VALUES (
                'LMC-0000000000000001', ?, ?, ?, 'activate', 2,
                'forged-lifecycle-command', ?, 'ACT-FORGED', 'human',
                'REQ-FORGED', '{}', 'forged incomplete activation', ?
            )
            """,
            (
                plan.plan_id,
                PROJECT.namespace,
                PROJECT.project_id,
                "e" * 64,
                NOW.isoformat(),
            ),
        )

    with pytest.raises(
        GovernanceEventError,
        match="LEGACY_MIGRATION_LIFECYCLE_ROOT_MISMATCH",
    ):
        GovernanceStore(store.path).initialize()


def test_concurrent_verification_converges_to_one_exact_report(tmp_path: Path) -> None:
    root = _legacy_tree(tmp_path / "project")
    store, _objects, dry_run, service, backup = _import_fixture(tmp_path / "fixture", root)
    plan = dry_run.create_plan(
        freeze=_freeze(root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )
    service.import_state(plan, backup)
    barrier = threading.Barrier(2)

    def verify() -> LegacyMigrationVerificationReport:
        barrier.wait()
        return service.verify_import(plan, backup)

    with ThreadPoolExecutor(max_workers=2) as executor:
        reports = tuple(executor.map(lambda _index: verify(), range(2)))

    assert reports[0].report_digest == reports[1].report_digest
    assert sorted(report.replayed for report in reports) == [False, True]
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_legacy_migration_verifications"
        ).fetchone() == (1,)


@pytest.mark.parametrize(
    "tamper",
    ["source", "source_same_length", "source_add", "source_delete", "active"],
)
def test_verification_rejects_source_or_import_root_mismatch(
    tmp_path: Path,
    tamper: str,
) -> None:
    root = _legacy_tree(tmp_path / "project")
    store, _objects, dry_run, service, backup = _import_fixture(tmp_path / "fixture", root)
    plan = dry_run.create_plan(
        freeze=_freeze(root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )
    service.import_state(plan, backup)
    if tamper.startswith("source"):
        summary = root / ".amplai" / "proposals" / PROPOSAL_ID / "summary.md"
        if tamper == "source":
            summary.write_bytes(summary.read_bytes() + b"tampered")
        elif tamper == "source_same_length":
            summary.write_bytes(b"x" * len(summary.read_bytes()))
        elif tamper == "source_add":
            summary.with_name("extra.md").write_bytes(b"extra")
        else:
            summary.unlink()
        expected = "LEGACY_MIGRATION_VERIFICATION_SOURCE_MISMATCH"
    else:
        with store.connect() as connection:
            connection.execute(
                """
                UPDATE governance_active_proposals
                SET status = 'reviewed', state_revision = state_revision + 1
                WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?
                """,
                (PROJECT.namespace, PROJECT.project_id, PROPOSAL_ID),
            )
        expected = "LEGACY_MIGRATION_VERIFICATION_ROOT_MISMATCH"

    with pytest.raises(LegacyMigrationScanError, match=expected):
        service.verify_import(plan, backup)
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_legacy_migration_verifications"
        ).fetchone() == (0,)


def test_migration_root_and_items_are_database_enforced_durable_evidence(
    tmp_path: Path,
) -> None:
    root = _legacy_tree(tmp_path / "project")
    store, _objects, dry_run, service, backup = _import_fixture(
        tmp_path / "fixture",
        root,
    )
    plan = dry_run.create_plan(
        freeze=_freeze(root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )
    service.import_state(plan, backup)

    with store.connect() as connection:
        with pytest.raises(sqlite3.DatabaseError, match="root transition is invalid"):
            connection.execute(
                "UPDATE governance_legacy_migrations SET snapshot_digest = ?",
                (f"sha256:{'f' * 64}",),
            )
        with pytest.raises(sqlite3.DatabaseError, match="root is durable"):
            connection.execute("DELETE FROM governance_legacy_migrations")
        with pytest.raises(sqlite3.DatabaseError, match="item is immutable"):
            connection.execute("UPDATE governance_legacy_migration_items SET source_revision = 2")
        with pytest.raises(sqlite3.DatabaseError, match="item is durable"):
            connection.execute("DELETE FROM governance_legacy_migration_items")
        assert connection.execute(
            "SELECT status, snapshot_digest FROM governance_legacy_migrations"
        ).fetchone() == ("state_imported", plan.snapshot_digest)


def test_synthetic_approval_import_creates_audit_outbox_hold_and_enforcement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _legacy_tree(tmp_path / "project", status="approved")
    store, _objects, dry_run, service, backup = _import_fixture(tmp_path / "fixture", root)
    plan = dry_run.create_plan(
        freeze=_freeze(root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )
    imported = service.import_state(plan, backup)
    item = plan.proposals[0]

    with store.connect() as connection:
        command = connection.execute(
            """
            SELECT event_type, actor_id, actor_type, reason,
                   source_artifact_digest, source_state_revision,
                   payload_digest, payload_json
            FROM governance_legacy_import_commands
            """
        ).fetchone()
        assert command is not None
        assert tuple(command[:6]) == (
            "migration.synthetic_approval",
            "ACT-SYSTEM-MIGRATION",
            "service",
            "legacy_approval_without_audit",
            item.proposal_artifact_digest,
            item.state_revision,
        )
        assert command[6] == sha256_digest(str(command[7]).encode())
        assert connection.execute(
            """
            SELECT event_type, aggregate_sequence, actor_id, actor_type,
                   before_state, after_state, definition_digest,
                   previous_event_hash, destination_count
            FROM governance_audit_events
            """
        ).fetchone() == (
            "migration.synthetic_approval",
            1,
            "ACT-SYSTEM-MIGRATION",
            "service",
            "approved",
            "legacy_approval_review_required",
            imported.definition_digests[0],
            None,
            1,
        )
        assert connection.execute(
            """
            SELECT destination_ref, destination_sequence, source_state_revision,
                   payload_digest, payload_json, state
            FROM governance_outbox_events
            """
        ).fetchone() == (
            f"yaml:{PROJECT.namespace}:{PROJECT.project_id}:{PROPOSAL_ID}",
            1,
            item.state_revision,
            command[6],
            command[7],
            "pending",
        )
        assert connection.execute(
            """
            SELECT reason_code, source_artifact_digest
            FROM governance_legacy_approval_holds
            """
        ).fetchone() == (
            "legacy_approval_without_audit",
            item.proposal_artifact_digest,
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_legacy_event_backfill_pending"
        ).fetchone() == (0,)
        with pytest.raises(sqlite3.DatabaseError, match="import command is immutable"):
            connection.execute(
                "UPDATE governance_legacy_import_commands SET actor_id = 'ACT-TAMPER'"
            )
        with pytest.raises(sqlite3.DatabaseError, match="approval hold is durable"):
            connection.execute("DELETE FROM governance_legacy_approval_holds")
    GovernanceEventService(store).reconcile()

    authority_request = _migration_authority(store)
    authority = AuthorityService(store, clock=lambda: NOW)
    with pytest.raises(DecisionError, match="LEGACY_APPROVAL_REVIEW_REQUIRED"):
        DecisionService(store, authority, clock=lambda: NOW).issue_tokens(
            item.proposal_ref,
            authority_request=authority_request,
        )
    with pytest.raises(DecisionError, match="LEGACY_APPROVAL_REVIEW_REQUIRED"):
        DecisionService(store, authority, clock=lambda: NOW).decide(
            item.proposal_ref,
            action=DecisionAction.APPROVE,
            authority_request=authority_request,
            raw_token="preissued-action-token",
            idempotency_key="legacy-hold:preissued-decision",
            request_fingerprint="e" * 64,
        )
    grant_issuer = ApplyGrantService(store, authority, _objects, clock=lambda: NOW)
    synthetic_approved_source = (
        PROJECT.namespace,
        PROJECT.project_id,
        PROPOSAL_ID,
        imported.definition_digests[0],
        item.content_revision,
        item.state_revision,
        item.decision_epoch,
        NOW.isoformat(),
    )
    monkeypatch.setattr(
        grant_issuer,
        "_approved_decision_row",
        lambda _connection, _key: synthetic_approved_source,
    )
    with pytest.raises(ApplyGovernanceError, match="LEGACY_APPROVAL_REVIEW_REQUIRED"):
        grant_issuer._issue_from_approved_decision(
            "legacy-hold:preexisting-approved-decision",
            authority_request=authority_request,
        )
    with pytest.raises(ApplyGovernanceError, match="LEGACY_APPROVAL_REVIEW_REQUIRED"):
        ApplyRequestService(store, authority, clock=lambda: NOW).request_apply(
            item.proposal_ref,
            authority_request=authority_request,
            raw_grant="not-a-real-grant",
            idempotency_key="apply:legacy-hold:test",
            request_fingerprint="a" * 64,
        )
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM governance_action_tokens").fetchone() == (
            0,
        )
        assert connection.execute("SELECT COUNT(*) FROM governance_apply_jobs").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM governance_apply_grants").fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_decision_results"
        ).fetchone() == (0,)

    review_service = LegacyApprovalReviewService(store, authority, clock=lambda: NOW)
    with store.connect() as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_legacy_review_outbox
            BEFORE INSERT ON governance_outbox_events
            BEGIN SELECT RAISE(ABORT, 'forced legacy review outbox failure'); END
            """
        )
    with pytest.raises(
        LegacyMigrationScanError,
        match="LEGACY_APPROVAL_REVIEW_CONFLICT",
    ):
        review_service.resolve(
            item.proposal_ref,
            authority_request=authority_request,
            reason="human reviewed untrusted legacy approval",
            idempotency_key="legacy-review:synthetic:failed",
            request_fingerprint="d" * 64,
        )
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_legacy_approval_reviews"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT state_revision, decision_epoch, status FROM governance_active_proposals"
        ).fetchone() == (
            item.state_revision,
            item.decision_epoch,
            "draft",
        )
        assert connection.execute("SELECT COUNT(*) FROM governance_audit_events").fetchone() == (1,)
        connection.execute("DROP TRIGGER fail_legacy_review_outbox")
    review = review_service.resolve(
        item.proposal_ref,
        authority_request=authority_request,
        reason="human reviewed untrusted legacy approval",
        idempotency_key="legacy-review:synthetic:1",
        request_fingerprint="b" * 64,
    )
    replay = review_service.resolve(
        item.proposal_ref,
        authority_request=authority_request,
        reason="human reviewed untrusted legacy approval",
        idempotency_key="legacy-review:synthetic:1",
        request_fingerprint="b" * 64,
    )
    assert replay.model_copy(update={"replayed": False}) == review
    assert replay.replayed is True

    active = ActiveProposalRepository(store, _objects)
    reviewed = ProposalSubmissionService(store, active, authority).submit_for_review(
        item.proposal_ref,
        authority_request=authority_request,
        expected_state_revision=review.state_revision,
    )
    decisions = DecisionService(store, authority, clock=lambda: NOW)
    approve = next(
        token
        for token in decisions.issue_tokens(
            item.proposal_ref,
            authority_request=authority_request,
        )
        if token.record.allowed_action is DecisionAction.APPROVE
    )
    decision = decisions.decide(
        item.proposal_ref,
        action=DecisionAction.APPROVE,
        authority_request=authority_request,
        raw_token=approve.raw_token,
        idempotency_key="legacy-review:decision:approve",
        request_fingerprint="c" * 64,
    )
    issued_grant = ApplyGrantService(
        store,
        authority,
        _objects,
        clock=lambda: NOW,
    )._issue_from_approved_decision(
        "legacy-review:decision:approve",
        authority_request=authority_request,
    )
    assert reviewed.status.value == "reviewed"
    assert decision.proposal_status.value == "approved"
    assert issued_grant.record.state.value == "issued"
    with store.connect() as connection:
        assert connection.execute(
            "SELECT actor_id, reason FROM governance_legacy_approval_reviews"
        ).fetchone() == (
            REVIEWER.actor_id,
            "human reviewed untrusted legacy approval",
        )
        assert connection.execute(
            "SELECT aggregate_sequence FROM governance_audit_events ORDER BY aggregate_sequence"
        ).fetchall()[:2] == [(1,), (2,)]
        audit_chain = connection.execute(
            "SELECT event_hash, previous_event_hash FROM governance_audit_events "
            "ORDER BY aggregate_sequence"
        ).fetchall()
        assert audit_chain[0][1] is None
        assert audit_chain[1][1] == audit_chain[0][0]
        assert connection.execute(
            "SELECT destination_sequence FROM governance_outbox_events "
            "WHERE destination_ref = ? ORDER BY destination_sequence",
            (f"yaml:{PROJECT.namespace}:{PROJECT.project_id}:{PROPOSAL_ID}",),
        ).fetchall()[:2] == [(1,), (2,)]
        with pytest.raises(sqlite3.DatabaseError, match="approval review is immutable"):
            connection.execute(
                "UPDATE governance_legacy_approval_reviews SET actor_id = 'ACT-TAMPER'"
            )
        with pytest.raises(sqlite3.DatabaseError, match="approval review is durable"):
            connection.execute("DELETE FROM governance_legacy_approval_reviews")
    GovernanceEventService(store).reconcile()


def test_strict_legacy_approval_audit_imports_human_evidence(tmp_path: Path) -> None:
    root = _legacy_tree(tmp_path / "project", status="approved")
    audit_path = root / ".amplai" / "proposals" / PROPOSAL_ID / "approval-audit.json"
    audit_bytes = _approval_audit_payload()
    audit_path.write_bytes(audit_bytes)
    store, _objects, dry_run, service, backup = _import_fixture(tmp_path / "fixture", root)
    plan = dry_run.create_plan(
        freeze=_freeze(root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )

    service.import_state(plan, backup)

    evidence = json.loads(audit_bytes)
    with store.connect() as connection:
        assert connection.execute(
            """
            SELECT idempotency_key, request_fingerprint, event_type,
                   actor_id, actor_type, occurred_at, source_artifact_digest, reason
            FROM governance_legacy_import_commands
            """
        ).fetchone() == (
            evidence["idempotency_key"],
            evidence["request_fingerprint"],
            "migration.legacy_approval",
            "legacy-user",
            "human",
            "2026-07-30T12:00:00.000000Z",
            sha256_digest(audit_bytes),
            None,
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_legacy_approval_holds"
        ).fetchone() == (0,)
    GovernanceEventService(store).reconcile()


def test_invalid_legacy_approval_audit_rolls_back_authoritative_state(tmp_path: Path) -> None:
    root = _legacy_tree(tmp_path / "project", status="approved")
    audit_path = root / ".amplai" / "proposals" / PROPOSAL_ID / "approval-audit.json"
    evidence = json.loads(_approval_audit_payload())
    evidence["request_fingerprint"] = "f" * 64
    audit_path.write_text(json.dumps(evidence), encoding="utf-8")
    store, _objects, dry_run, service, backup = _import_fixture(tmp_path / "fixture", root)
    plan = dry_run.create_plan(
        freeze=_freeze(root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )

    with pytest.raises(LegacyMigrationScanError, match="LEGACY_APPROVAL_AUDIT_INVALID"):
        service.import_state(plan, backup)

    with store.connect() as connection:
        for table in (
            "governance_active_proposals",
            "governance_definition_revisions",
            "governance_legacy_migration_items",
            "governance_legacy_import_commands",
            "governance_audit_events",
            "governance_outbox_events",
            "governance_legacy_approval_holds",
        ):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("actor_id", "different-legacy-user"),
        ("occurred_at", "2026-07-30T12:00:01Z"),
    ),
)
def test_validly_fingerprinted_mismatched_approval_evidence_fails_closed(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    root = _legacy_tree(tmp_path / field, status="approved")
    audit_path = root / ".amplai" / "proposals" / PROPOSAL_ID / "approval-audit.json"
    evidence = json.loads(_approval_audit_payload())
    evidence[field] = value
    evidence.pop("request_fingerprint")
    evidence["request_fingerprint"] = _canonical_digest(evidence).removeprefix("sha256:")
    audit_path.write_text(
        json.dumps(evidence, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    store, _objects, dry_run, service, backup = _import_fixture(tmp_path / "fixture", root)
    plan = dry_run.create_plan(
        freeze=_freeze(root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )

    with pytest.raises(LegacyMigrationScanError, match="LEGACY_APPROVAL_AUDIT_INVALID"):
        service.import_state(plan, backup)

    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_active_proposals"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_legacy_import_commands"
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    "status",
    ("draft", "reviewed", "changes_requested", "rejected", "superseded"),
)
def test_non_approved_state_rejects_approval_audit_artifact(
    tmp_path: Path,
    status: str,
) -> None:
    root = _legacy_tree(tmp_path / status, status=status)
    audit_path = root / ".amplai" / "proposals" / PROPOSAL_ID / "approval-audit.json"
    audit_path.write_bytes(_approval_audit_payload())

    with pytest.raises(
        LegacyMigrationScanError,
        match="LEGACY_APPROVAL_AUDIT_STATE_INVALID",
    ):
        LegacyProposalDryRunService(root, PROJECT).create_plan(
            freeze=_freeze(root),
            base_revision="a13d92f",
            validation_policy_ref="policy/migration/v1",
        )


def test_legacy_event_outbox_failure_rolls_back_state_audit_and_hold(tmp_path: Path) -> None:
    root = _legacy_tree(tmp_path / "project", status="approved")
    store, _objects, dry_run, service, backup = _import_fixture(tmp_path / "fixture", root)
    plan = dry_run.create_plan(
        freeze=_freeze(root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )
    service.prepare(plan, backup)
    with store.connect() as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_legacy_outbox
            BEFORE INSERT ON governance_outbox_events
            BEGIN
                SELECT RAISE(ABORT, 'forced legacy outbox failure');
            END
            """
        )

    with pytest.raises(LegacyMigrationScanError, match="LEGACY_MIGRATION_STATE_CONFLICT"):
        service.import_state(plan, backup)

    with store.connect() as connection:
        assert connection.execute("SELECT status FROM governance_legacy_migrations").fetchone() == (
            "prepared",
        )
        for table in (
            "governance_active_proposals",
            "governance_definition_revisions",
            "governance_legacy_migration_items",
            "governance_legacy_import_commands",
            "governance_audit_events",
            "governance_outbox_events",
            "governance_legacy_approval_holds",
        ):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)


def test_legacy_approval_idempotency_collision_rolls_back_second_project(
    tmp_path: Path,
) -> None:
    shared_key = "legacy-approval:shared-cross-project-key"
    first_root = _legacy_tree(tmp_path / "first", status="approved")
    first_audit = first_root / ".amplai" / "proposals" / PROPOSAL_ID / "approval-audit.json"
    first_audit.write_bytes(_approval_audit_payload(idempotency_key=shared_key))
    store, _objects, first_dry_run, first_service, first_backup = _import_fixture(
        tmp_path / "first-fixture",
        first_root,
    )
    first_plan = first_dry_run.create_plan(
        freeze=_freeze(first_root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )
    first_service.import_state(first_plan, first_backup)

    second_project = ProjectRef(
        project_id="cortex",
        namespace="org/default/project/cortex",
    )
    second_root = _legacy_tree(
        tmp_path / "second",
        project=second_project,
        status="approved",
    )
    second_audit = second_root / ".amplai" / "proposals" / PROPOSAL_ID / "approval-audit.json"
    second_audit.write_bytes(_approval_audit_payload(second_project, idempotency_key=shared_key))
    _same_store, _second_objects, second_dry_run, second_service, second_backup = _import_fixture(
        tmp_path / "second-fixture",
        second_root,
        project=second_project,
        store=store,
    )
    second_plan = second_dry_run.create_plan(
        freeze=_freeze(second_root, second_project),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )

    with pytest.raises(LegacyMigrationScanError, match="IDEMPOTENCY_CONFLICT"):
        second_service.import_state(second_plan, second_backup)

    with store.connect() as connection:
        assert connection.execute(
            """
            SELECT COUNT(*) FROM governance_active_proposals
            WHERE project_namespace = ? AND project_id = ?
            """,
            (second_project.namespace, second_project.project_id),
        ).fetchone() == (0,)
        assert connection.execute(
            """
            SELECT status FROM governance_legacy_migrations
            WHERE project_namespace = ? AND project_id = ?
            """,
            (second_project.namespace, second_project.project_id),
        ).fetchone() == ("prepared",)
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_legacy_import_commands"
        ).fetchone() == (1,)
    GovernanceEventService(store).reconcile()


def test_populated_v17_store_upgrades_to_latest_without_rewriting_active_state(
    tmp_path: Path,
) -> None:
    root = _legacy_tree(tmp_path / "project")
    path = tmp_path / "runtime" / "governance.db"
    v17 = GovernanceStore(
        path,
        migration_runner=MigrationRunner(INITIAL_MIGRATIONS[:17]),
    )
    assert v17.initialize().schema_version == 17
    objects = ImmutableDefinitionObjectStore(PROJECT, root)
    ref = ProposalRef(project_ref=PROJECT, proposal_id=PROPOSAL_ID)
    canonical = canonicalize_definition(
        ProposalDefinitionManifest(
            proposal_ref=ref,
            operations=({"marker": "v17", "type": "CREATE"},),
            base_revision="a13d92f",
            validation_policy_ref="policy/proposal-v3",
        )
    )
    object_ref = objects.put_definition_object(
        ref,
        canonical.canonical_bytes,
        canonical.digest,
    )
    before = ActiveProposalRepository(v17, objects).activate_definition_revision(
        ref,
        expected_active_digest=None,
        expected_state_revision=0,
        next_object_ref=object_ref,
    )

    latest = GovernanceStore(path)
    assert latest.initialize().schema_version == len(INITIAL_MIGRATIONS)
    assert ActiveProposalRepository(latest, objects).get(ref) == before
    with latest.connect() as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name LIKE 'governance_legacy_migration%'
                """
            ).fetchall()
        }
        triggers = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'trigger' AND name LIKE 'governance_legacy_migration%'
                """
            ).fetchall()
        }
    assert tables == {
        "governance_legacy_migrations",
        "governance_legacy_migration_items",
        "governance_legacy_migration_verifications",
        "governance_legacy_migration_lifecycle_heads",
        "governance_legacy_migration_lifecycle_commands",
        "governance_legacy_migration_lifecycle_events",
        "governance_legacy_migration_lifecycle_results",
    }
    assert triggers == {
        "governance_legacy_migrations_insert_guard",
        "governance_legacy_migrations_transition_guard",
        "governance_legacy_migrations_no_delete",
        "governance_legacy_migration_items_insert_guard",
        "governance_legacy_migration_items_no_update",
        "governance_legacy_migration_items_no_delete",
        "governance_legacy_migration_verifications_no_update",
        "governance_legacy_migration_verifications_no_delete",
        "governance_legacy_migration_verifications_insert_guard",
        "governance_legacy_migration_verifications_idempotency_guard",
        "governance_legacy_migration_lifecycle_heads_insert_guard",
        "governance_legacy_migration_lifecycle_heads_update_guard",
        "governance_legacy_migration_lifecycle_heads_no_delete",
    }


def test_populated_v18_store_upgrades_to_v19_without_rewriting_migration_evidence(
    tmp_path: Path,
) -> None:
    root = _legacy_tree(tmp_path / "project")
    path = tmp_path / "runtime" / "governance.db"
    v18 = GovernanceStore(
        path,
        migration_runner=MigrationRunner(INITIAL_MIGRATIONS[:18]),
    )
    assert v18.initialize().schema_version == 18
    objects = ImmutableDefinitionObjectStore(PROJECT, root)
    ref = ProposalRef(project_ref=PROJECT, proposal_id=PROPOSAL_ID)
    canonical = canonicalize_definition(
        ProposalDefinitionManifest(
            proposal_ref=ref,
            operations=({"marker": "v18", "type": "CREATE"},),
            base_revision="a13d92f",
            validation_policy_ref="policy/proposal-v3",
        )
    )
    object_ref = objects.put_definition_object(
        ref,
        canonical.canonical_bytes,
        canonical.digest,
    )
    active = ActiveProposalRepository(v18, objects).activate_definition_revision(
        ref,
        expected_active_digest=None,
        expected_state_revision=0,
        next_object_ref=object_ref,
    )
    digest = f"sha256:{'1' * 64}"
    migration_id = "MPL-0000000000000001"
    with v18.connect() as connection, governance_transaction(connection):
        connection.execute(
            """
            INSERT INTO governance_legacy_migrations(
                migration_id, project_namespace, project_id, freeze_id,
                snapshot_id, snapshot_digest, plan_digest, mapping_policy_version,
                base_revision, validation_policy_ref, project_pack_backup_path,
                project_pack_backup_digest, governance_backup_path,
                governance_backup_digest, proposal_count, status, prepared_at,
                state_imported_at
            )
            VALUES (?, ?, ?, 'MFR-0000000000000001', 'MPS-0000000000000001',
                    ?, ?, 1, 'a13d92f', 'policy/migration/v1',
                    '/backup/project-pack.snapshot', ?, '/backup/governance.db', ?,
                    1, 'state_imported', ?, ?)
            """,
            (
                migration_id,
                PROJECT.namespace,
                PROJECT.project_id,
                digest,
                f"sha256:{'2' * 64}",
                f"sha256:{'3' * 64}",
                f"sha256:{'4' * 64}",
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO governance_legacy_migration_items(
                migration_id, project_namespace, project_id, proposal_id,
                source_status, source_revision, target_status, definition_digest,
                proposal_artifact_digest, content_revision, state_revision,
                decision_epoch, approval_disposition, imported_at
            )
            VALUES (?, ?, ?, ?, 'draft', 1, 'draft', ?, ?, ?, ?, ?,
                    'not_required', ?)
            """,
            (
                migration_id,
                PROJECT.namespace,
                PROJECT.project_id,
                PROPOSAL_ID,
                active.active_definition_digest,
                f"sha256:{'5' * 64}",
                active.content_revision,
                active.state_revision,
                active.decision_epoch,
                NOW.isoformat(),
            ),
        )

    latest = GovernanceStore(path)
    assert latest.initialize().schema_version == len(INITIAL_MIGRATIONS)
    with latest.connect() as connection:
        assert connection.execute(
            """
            SELECT migration_id, project_namespace, project_id, proposal_id,
                   definition_digest, legacy_git_revision
            FROM governance_legacy_migration_items
            """
        ).fetchone() == (
            migration_id,
            PROJECT.namespace,
            PROJECT.project_id,
            PROPOSAL_ID,
            active.active_definition_digest,
            None,
        )
        assert connection.execute(
            "SELECT status, proposal_count FROM governance_legacy_migrations"
        ).fetchone() == ("state_imported", 1)
        assert connection.execute(
            "SELECT migration_id, proposal_id FROM governance_legacy_event_backfill_pending"
        ).fetchone() == (migration_id, PROPOSAL_ID)
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_legacy_import_commands"
        ).fetchone() == (0,)
        with pytest.raises(sqlite3.DatabaseError, match="item is immutable"):
            connection.execute("UPDATE governance_legacy_migration_items SET source_revision = 2")
        with pytest.raises(sqlite3.DatabaseError, match="root is durable"):
            connection.execute("DELETE FROM governance_legacy_migrations")


def test_populated_v18_applied_hold_upgrade_verifies_git_provenance_on_replay(
    tmp_path: Path,
) -> None:
    root = _legacy_tree(tmp_path / "project", status="applied")
    _source_store, objects, dry_run, source_service, backup = _import_fixture(
        tmp_path / "source-fixture",
        root,
    )
    plan = dry_run.create_plan(
        freeze=_freeze(root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )
    imported = source_service.import_state(plan, backup)
    item = plan.proposals[0]
    assert item.source_status.value == "applied"
    assert item.target_status.value == "legacy_approval_review_required"
    assert item.legacy_git_revision == "a13d92f"

    path = tmp_path / "v18-runtime" / "governance.db"
    v18 = GovernanceStore(
        path,
        migration_runner=MigrationRunner(INITIAL_MIGRATIONS[:18]),
    )
    v18.initialize()
    with v18.connect() as connection, governance_transaction(connection):
        connection.execute(
            """
            INSERT INTO governance_active_proposals(
                project_namespace, project_id, proposal_id, active_definition_digest,
                content_revision, state_revision, decision_epoch, status,
                created_at, updated_at, applied_revision
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?, NULL)
            """,
            (
                PROJECT.namespace,
                PROJECT.project_id,
                PROPOSAL_ID,
                imported.definition_digests[0],
                item.content_revision,
                item.state_revision,
                item.decision_epoch,
                NOW.isoformat(),
                NOW.isoformat(),
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
            (
                PROJECT.namespace,
                PROJECT.project_id,
                PROPOSAL_ID,
                item.content_revision,
                imported.definition_digests[0],
                NOW.isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO governance_legacy_migrations(
                migration_id, project_namespace, project_id, freeze_id,
                snapshot_id, snapshot_digest, plan_digest, mapping_policy_version,
                base_revision, validation_policy_ref, project_pack_backup_path,
                project_pack_backup_digest, governance_backup_path,
                governance_backup_digest, proposal_count, status, prepared_at,
                state_imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1,
                      'state_imported', ?, ?)
            """,
            (
                plan.plan_id,
                PROJECT.namespace,
                PROJECT.project_id,
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
                NOW.isoformat(),
                NOW.isoformat(),
            ),
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
                PROJECT.namespace,
                PROJECT.project_id,
                PROPOSAL_ID,
                item.source_status.value,
                item.source_revision,
                item.target_status.value,
                imported.definition_digests[0],
                item.proposal_artifact_digest,
                item.content_revision,
                item.state_revision,
                item.decision_epoch,
                item.approval_disposition.value,
                NOW.isoformat(),
            ),
        )

    progressed_path = tmp_path / "progressed-v18-runtime" / "governance.db"
    progressed_path.parent.mkdir(parents=True)
    with v18.connect() as source, sqlite3.connect(progressed_path) as destination:
        source.backup(destination)
    progressed_v18 = GovernanceStore(
        progressed_path,
        migration_runner=MigrationRunner(INITIAL_MIGRATIONS[:18]),
    )
    progressed_v18.initialize()
    v18_authority_request = _migration_authority(progressed_v18)
    v18_authority = AuthorityService(progressed_v18, clock=lambda: NOW)
    v18_active = ActiveProposalRepository(progressed_v18, objects)
    ProposalSubmissionService(progressed_v18, v18_active, v18_authority).submit_for_review(
        item.proposal_ref,
        authority_request=v18_authority_request,
        expected_state_revision=item.state_revision,
    )
    v18_decisions = DecisionService(progressed_v18, v18_authority, clock=lambda: NOW)
    v18_approve = next(
        token
        for token in v18_decisions.issue_tokens(
            item.proposal_ref,
            authority_request=v18_authority_request,
        )
        if token.record.allowed_action is DecisionAction.APPROVE
    )
    v18_decisions.decide(
        item.proposal_ref,
        action=DecisionAction.APPROVE,
        authority_request=v18_authority_request,
        raw_token=v18_approve.raw_token,
        idempotency_key="v18-progressed-approved-decision",
        request_fingerprint="f" * 64,
    )
    v18_grant = ApplyGrantService(
        progressed_v18,
        v18_authority,
        objects,
        clock=lambda: NOW,
    )._issue_from_approved_decision(
        "v18-progressed-approved-decision",
        authority_request=v18_authority_request,
    )
    v18_apply = ApplyRequestService(
        progressed_v18,
        v18_authority,
        clock=lambda: NOW,
    ).request_apply(
        item.proposal_ref,
        authority_request=v18_authority_request,
        raw_grant=v18_grant.raw_grant,
        idempotency_key="v18-progressed-apply-request",
        request_fingerprint="a" * 64,
    )
    v18_jobs = ApplyJobService(progressed_v18, clock=lambda: NOW)
    v18_leased = v18_jobs.claim_next("worker-v18-migration")
    assert v18_leased is not None
    v18_jobs.start(
        v18_apply.job_id,
        worker_id="worker-v18-migration",
        fencing_token=v18_leased.fencing_token,
    )
    v18_jobs.stage_for_publish(
        v18_apply.job_id,
        worker_id="worker-v18-migration",
        fencing_token=v18_leased.fencing_token,
        artifact_bytes=b"legacy staged artifact",
        publish_request_bytes=b"legacy publish request",
    )
    publish_git = LegacyMigrationPublishGit()
    v18_intent = PublishPreparationService(
        progressed_v18,
        publish_git,
        clock=lambda: NOW,
    ).prepare(
        v18_apply.job_id,
        fencing_token=v18_leased.fencing_token,
        canonical_ref="refs/heads/main",
        candidate_commit=publish_git.candidate_commit,
    )

    latest = GovernanceStore(path)
    assert latest.initialize().schema_version == len(INITIAL_MIGRATIONS)
    with latest.connect() as connection:
        assert connection.execute(
            """
            SELECT status, applied_revision FROM governance_active_proposals
            WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?
            """,
            (PROJECT.namespace, PROJECT.project_id, PROPOSAL_ID),
        ).fetchone() == ("draft", None)
        assert connection.execute(
            "SELECT legacy_git_revision FROM governance_legacy_migration_items"
        ).fetchone() == (None,)

    replay_service = LegacyProposalImportService(dry_run, latest, objects)
    for trigger_name, table in (
        ("fail_legacy_backfill_audit", "governance_audit_events"),
        ("fail_legacy_backfill_outbox", "governance_outbox_events"),
    ):
        with latest.connect() as connection:
            connection.execute(
                f"""
                CREATE TRIGGER {trigger_name}
                BEFORE INSERT ON {table}
                BEGIN SELECT RAISE(ABORT, 'forced legacy backfill failure'); END
                """
            )
        with pytest.raises(
            LegacyMigrationScanError,
            match="LEGACY_MIGRATION_REPLAY_CONFLICT",
        ):
            replay_service.import_state(plan, backup)
        with latest.connect() as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM governance_legacy_event_backfill_pending"
            ).fetchone() == (1,)
            for authoritative_table in (
                "governance_legacy_import_commands",
                "governance_legacy_approval_holds",
                "governance_audit_events",
                "governance_outbox_events",
                "governance_aggregate_sequences",
                "governance_outbox_destinations",
            ):
                assert connection.execute(
                    f"SELECT COUNT(*) FROM {authoritative_table}"
                ).fetchone() == (0,)
            connection.execute(f"DROP TRIGGER {trigger_name}")
    assert replay_service.import_state(plan, backup) == imported
    with latest.connect() as connection:
        assert connection.execute(
            "SELECT legacy_git_revision FROM governance_legacy_migration_items"
        ).fetchone() == (None,)
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_legacy_event_backfill_pending"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT event_type FROM governance_legacy_import_commands"
        ).fetchone() == ("migration.synthetic_approval",)
        assert connection.execute(
            "SELECT reason_code FROM governance_legacy_approval_holds"
        ).fetchone() == ("legacy_approval_without_audit",)
        assert connection.execute(
            "SELECT event_type, aggregate_sequence FROM governance_audit_events"
        ).fetchone() == ("migration.synthetic_approval", 1)
        assert connection.execute(
            "SELECT destination_sequence, state FROM governance_outbox_events"
        ).fetchone() == (1, "pending")
        with pytest.raises(sqlite3.DatabaseError, match="item is immutable"):
            connection.execute(
                """
                UPDATE governance_legacy_migration_items
                SET legacy_git_revision = 'fffffff'
                """
            )

    progressed = GovernanceStore(progressed_path)
    assert progressed.initialize().schema_version == len(INITIAL_MIGRATIONS)
    progressed_authority = v18_authority_request
    with progressed.connect() as connection:
        assert connection.execute(
            """
            SELECT state_revision, decision_epoch, status, applied_revision
            FROM governance_active_proposals
            WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?
            """,
            (PROJECT.namespace, PROJECT.project_id, PROPOSAL_ID),
        ).fetchone() == (
            item.state_revision + 3,
            item.decision_epoch,
            "apply_requested",
            None,
        )
        assert connection.execute(
            "SELECT legacy_git_revision FROM governance_legacy_migration_items"
        ).fetchone() == (None,)
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_legacy_event_backfill_pending"
        ).fetchone() == (1,)
    with pytest.raises(
        ApplyGovernanceError,
        match="LEGACY_MIGRATION_EVENT_BACKFILL_PENDING",
    ):
        ApplyJobService(progressed, clock=lambda: NOW).stage_for_publish(
            v18_apply.job_id,
            worker_id="worker-v18-migration",
            fencing_token=v18_leased.fencing_token,
            artifact_bytes=b"blocked artifact",
            publish_request_bytes=b"blocked publish request",
        )
    with pytest.raises(
        PublishGovernanceError,
        match="LEGACY_MIGRATION_EVENT_BACKFILL_PENDING",
    ):
        PublishResolutionService(
            progressed,
            publish_git,
            coordinator_id="migration-publish-gate",
            clock=lambda: NOW,
        ).recover(v18_intent.intent_id)
    with pytest.raises(ActiveProposalError, match="LEGACY_MIGRATION_EVENT_BACKFILL_PENDING"):
        ProposalSubmissionService(
            progressed,
            ActiveProposalRepository(progressed, objects),
            AuthorityService(progressed, clock=lambda: NOW),
        ).submit_for_review(
            item.proposal_ref,
            authority_request=progressed_authority,
            expected_state_revision=item.state_revision + 3,
        )

    assert (
        LegacyProposalImportService(dry_run, progressed, objects).import_state(plan, backup)
        == imported
    )
    with progressed.connect() as connection:
        assert connection.execute(
            """
            SELECT state_revision, decision_epoch, status
            FROM governance_active_proposals
            WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?
            """,
            (PROJECT.namespace, PROJECT.project_id, PROPOSAL_ID),
        ).fetchone() == (
            item.state_revision + 3,
            item.decision_epoch,
            "apply_requested",
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_legacy_event_backfill_pending"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT event_type FROM governance_audit_events ORDER BY aggregate_sequence"
        ).fetchall()[-1] == ("migration.synthetic_approval",)
        yaml_rows = connection.execute(
            """
            SELECT destination_sequence, source_state_revision
            FROM governance_outbox_events
            WHERE destination_ref = ? ORDER BY destination_sequence
            """,
            (f"yaml:{PROJECT.namespace}:{PROJECT.project_id}:{PROPOSAL_ID}",),
        ).fetchall()
        assert yaml_rows == [
            (1, item.state_revision + 2),
            (2, item.state_revision + 3),
        ]
        assert connection.execute(
            """
            SELECT destination_sequence, source_state_revision
            FROM governance_outbox_events
            WHERE destination_ref = ?
            """,
            (
                f"migration-history:{plan.plan_id}:{PROJECT.namespace}:"
                f"{PROJECT.project_id}:{PROPOSAL_ID}",
            ),
        ).fetchall() == [(1, item.state_revision)]
        assert connection.execute(
            "SELECT status FROM governance_apply_jobs WHERE job_id = ?",
            (v18_apply.job_id,),
        ).fetchone() == ("publish_pending",)
    GovernanceEventService(progressed).reconcile()
    with pytest.raises(ApplyGovernanceError, match="LEGACY_APPROVAL_REVIEW_REQUIRED"):
        ApplyJobService(progressed, clock=lambda: NOW).stage_for_publish(
            v18_apply.job_id,
            worker_id="worker-v18-migration",
            fencing_token=v18_leased.fencing_token,
            artifact_bytes=b"blocked artifact",
            publish_request_bytes=b"blocked publish request",
        )
    with pytest.raises(PublishGovernanceError, match="LEGACY_APPROVAL_REVIEW_REQUIRED"):
        PublishResolutionService(
            progressed,
            publish_git,
            coordinator_id="migration-publish-gate",
            clock=lambda: NOW,
        ).recover(v18_intent.intent_id)
    with pytest.raises(
        LegacyMigrationScanError,
        match="LEGACY_APPROVAL_REVIEW_FORWARD_RECOVERY_REQUIRED",
    ):
        LegacyApprovalReviewService(
            progressed,
            AuthorityService(progressed, clock=lambda: NOW),
            clock=lambda: NOW,
        ).resolve(
            item.proposal_ref,
            authority_request=progressed_authority,
            reason="review in-flight legacy apply",
            idempotency_key="v18-progressed-review",
            request_fingerprint="b" * 64,
        )


def test_progressed_v18_backfill_preserves_next_active_projection_revision(
    tmp_path: Path,
) -> None:
    root = _legacy_tree(tmp_path / "project", status="reviewed")
    _source_store, objects, dry_run, source_service, backup = _import_fixture(
        tmp_path / "source-fixture",
        root,
    )
    plan = dry_run.create_plan(
        freeze=_freeze(root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )
    imported = source_service.import_state(plan, backup)
    item = plan.proposals[0]
    assert item.approval_disposition is LegacyApprovalDisposition.NOT_REQUIRED

    path = tmp_path / "reviewed-v18-runtime" / "governance.db"
    v18 = GovernanceStore(path, migration_runner=MigrationRunner(INITIAL_MIGRATIONS[:18]))
    v18.initialize()
    with v18.connect() as connection, governance_transaction(connection):
        connection.execute(
            """
            INSERT INTO governance_active_proposals(
                project_namespace, project_id, proposal_id, active_definition_digest,
                content_revision, state_revision, decision_epoch, status,
                created_at, updated_at, applied_revision
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'reviewed', ?, ?, NULL)
            """,
            (
                PROJECT.namespace,
                PROJECT.project_id,
                PROPOSAL_ID,
                imported.definition_digests[0],
                item.content_revision,
                item.state_revision,
                item.decision_epoch,
                NOW.isoformat(),
                NOW.isoformat(),
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
            (
                PROJECT.namespace,
                PROJECT.project_id,
                PROPOSAL_ID,
                item.content_revision,
                imported.definition_digests[0],
                NOW.isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO governance_legacy_migrations(
                migration_id, project_namespace, project_id, freeze_id,
                snapshot_id, snapshot_digest, plan_digest, mapping_policy_version,
                base_revision, validation_policy_ref, project_pack_backup_path,
                project_pack_backup_digest, governance_backup_path,
                governance_backup_digest, proposal_count, status, prepared_at,
                state_imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1,
                      'state_imported', ?, ?)
            """,
            (
                plan.plan_id,
                PROJECT.namespace,
                PROJECT.project_id,
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
                NOW.isoformat(),
                NOW.isoformat(),
            ),
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
                PROJECT.namespace,
                PROJECT.project_id,
                PROPOSAL_ID,
                item.source_status.value,
                item.source_revision,
                item.target_status.value,
                imported.definition_digests[0],
                item.proposal_artifact_digest,
                item.content_revision,
                item.state_revision,
                item.decision_epoch,
                item.approval_disposition.value,
                NOW.isoformat(),
            ),
        )

    authority_request = _migration_authority(v18)
    authority = AuthorityService(v18, clock=lambda: NOW)
    decisions = DecisionService(v18, authority, clock=lambda: NOW)
    approve = next(
        token
        for token in decisions.issue_tokens(
            item.proposal_ref,
            authority_request=authority_request,
        )
        if token.record.allowed_action is DecisionAction.APPROVE
    )
    decision_key = "v18-reviewed-approved-decision"
    decisions.decide(
        item.proposal_ref,
        action=DecisionAction.APPROVE,
        authority_request=authority_request,
        raw_token=approve.raw_token,
        idempotency_key=decision_key,
        request_fingerprint="c" * 64,
    )

    latest = GovernanceStore(path)
    assert latest.initialize().schema_version == len(INITIAL_MIGRATIONS)
    assert (
        LegacyProposalImportService(dry_run, latest, objects).import_state(plan, backup) == imported
    )
    latest_authority = AuthorityService(latest, clock=lambda: NOW)
    grant = ApplyGrantService(
        latest,
        latest_authority,
        objects,
        clock=lambda: NOW,
    )._issue_from_approved_decision(
        decision_key,
        authority_request=authority_request,
    )
    ApplyRequestService(latest, latest_authority, clock=lambda: NOW).request_apply(
        item.proposal_ref,
        authority_request=authority_request,
        raw_grant=grant.raw_grant,
        idempotency_key="post-backfill-apply-request",
        request_fingerprint="d" * 64,
    )

    yaml_destination = f"yaml:{PROJECT.namespace}:{PROJECT.project_id}:{PROPOSAL_ID}"
    history_destination = (
        f"migration-history:{plan.plan_id}:{PROJECT.namespace}:{PROJECT.project_id}:{PROPOSAL_ID}"
    )
    with latest.connect() as connection:
        assert connection.execute(
            """
            SELECT destination_sequence, source_state_revision
            FROM governance_outbox_events
            WHERE destination_ref = ? ORDER BY destination_sequence
            """,
            (yaml_destination,),
        ).fetchall() == [
            (1, item.state_revision + 1),
            (2, item.state_revision + 2),
        ]
        assert connection.execute(
            """
            SELECT destination_sequence, source_state_revision
            FROM governance_outbox_events WHERE destination_ref = ?
            """,
            (history_destination,),
        ).fetchall() == [(1, item.state_revision)]
        assert connection.execute(
            """
            SELECT status, state_revision FROM governance_active_proposals
            WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?
            """,
            (PROJECT.namespace, PROJECT.project_id, PROPOSAL_ID),
        ).fetchone() == ("apply_requested", item.state_revision + 2)
    GovernanceEventService(latest).reconcile()
    dispatcher = OutboxDispatcher(latest, clock=lambda: NOW)
    live_projection = YamlProjectionDestination(
        tmp_path / "projections" / "reviewed-live.yaml",
        destination_ref=yaml_destination,
    )
    history_projection = YamlProjectionDestination(
        tmp_path / "projections" / "reviewed-history.yaml",
        destination_ref=history_destination,
    )
    assert dispatcher.deliver_next("live-dispatch-1", live_projection) is not None
    assert dispatcher.deliver_next("live-dispatch-2", live_projection) is not None
    assert dispatcher.deliver_next("live-dispatch-3", live_projection) is None
    assert dispatcher.deliver_next("history-dispatch", history_projection) is not None


def test_progressed_v18_without_live_outbox_routes_backfill_to_history(
    tmp_path: Path,
) -> None:
    root = _legacy_tree(tmp_path / "project", status="draft")
    _source_store, objects, dry_run, source_service, backup = _import_fixture(
        tmp_path / "source-fixture",
        root,
    )
    plan = dry_run.create_plan(
        freeze=_freeze(root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )
    imported = source_service.import_state(plan, backup)
    item = plan.proposals[0]
    v18 = _seed_v18_imported_proposal(
        tmp_path / "draft-v18-runtime" / "governance.db",
        plan=plan,
        item=item,
        definition_digest=imported.definition_digests[0],
        backup=backup,
    )
    authority_request = _migration_authority(v18)
    ProposalSubmissionService(
        v18,
        ActiveProposalRepository(v18, objects),
        AuthorityService(v18, clock=lambda: NOW),
    ).submit_for_review(
        item.proposal_ref,
        authority_request=authority_request,
        expected_state_revision=item.state_revision,
    )
    with v18.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM governance_outbox_events").fetchone() == (
            0,
        )

    latest = GovernanceStore(v18.path)
    assert latest.initialize().schema_version == len(INITIAL_MIGRATIONS)
    assert (
        LegacyProposalImportService(dry_run, latest, objects).import_state(plan, backup) == imported
    )
    live_destination = f"yaml:{PROJECT.namespace}:{PROJECT.project_id}:{PROPOSAL_ID}"
    history_destination = (
        f"migration-history:{plan.plan_id}:{PROJECT.namespace}:{PROJECT.project_id}:{PROPOSAL_ID}"
    )
    with latest.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_outbox_events WHERE destination_ref = ?",
            (live_destination,),
        ).fetchone() == (0,)
        assert connection.execute(
            """
            SELECT destination_sequence, source_state_revision
            FROM governance_outbox_events WHERE destination_ref = ?
            """,
            (history_destination,),
        ).fetchall() == [(1, item.state_revision)]
        assert connection.execute(
            """
            SELECT status, state_revision FROM governance_active_proposals
            WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?
            """,
            (PROJECT.namespace, PROJECT.project_id, PROPOSAL_ID),
        ).fetchone() == ("reviewed", item.state_revision + 1)
    GovernanceEventService(latest).reconcile()
    decisions = DecisionService(
        latest, AuthorityService(latest, clock=lambda: NOW), clock=lambda: NOW
    )
    approve = next(
        token
        for token in decisions.issue_tokens(
            item.proposal_ref,
            authority_request=authority_request,
        )
        if token.record.allowed_action is DecisionAction.APPROVE
    )
    decisions.decide(
        item.proposal_ref,
        action=DecisionAction.APPROVE,
        authority_request=authority_request,
        raw_token=approve.raw_token,
        idempotency_key="post-history-approved-decision",
        request_fingerprint="e" * 64,
    )
    dispatcher = OutboxDispatcher(latest, clock=lambda: NOW)
    assert (
        dispatcher.deliver_next(
            "history-first-dispatch",
            YamlProjectionDestination(
                tmp_path / "projections" / "draft-history.yaml",
                destination_ref=history_destination,
            ),
        )
        is not None
    )
    assert (
        dispatcher.deliver_next(
            "live-after-history-dispatch",
            YamlProjectionDestination(
                tmp_path / "projections" / "draft-live.yaml",
                destination_ref=live_destination,
            ),
        )
        is not None
    )


def test_backup_failure_prevents_migration_root_and_state_import(tmp_path: Path) -> None:
    root = _legacy_tree(tmp_path / "project")
    store, _objects, dry_run, service, backup = _import_fixture(
        tmp_path / "fixture",
        root,
    )
    plan = dry_run.create_plan(
        freeze=_freeze(root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )
    invalid = backup.model_copy(update={"governance_backup_digest": f"sha256:{'f' * 64}"})

    with pytest.raises(
        LegacyMigrationScanError,
        match="LEGACY_GOVERNANCE_BACKUP_INVALID",
    ):
        service.import_state(plan, invalid)
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_legacy_migrations"
        ).fetchone() == (0,)
        for table in (
            "governance_active_proposals",
            "governance_definition_revisions",
            "governance_legacy_migration_items",
        ):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)


def test_project_pack_backup_must_be_canonical_and_plan_bound(tmp_path: Path) -> None:
    root = _legacy_tree(tmp_path / "project")
    store, _objects, dry_run, service, backup = _import_fixture(
        tmp_path / "fixture",
        root,
    )
    plan = dry_run.create_plan(
        freeze=_freeze(root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )
    project_pack = Path(backup.project_pack_backup_path)
    project_pack.write_bytes(b"arbitrary unrelated bytes")
    unrelated = backup.model_copy(
        update={
            "project_pack_backup_digest": (
                f"sha256:{hashlib.sha256(project_pack.read_bytes()).hexdigest()}"
            )
        }
    )

    with pytest.raises(
        LegacyMigrationScanError,
        match="LEGACY_PROJECT_PACK_BACKUP_INVALID",
    ):
        service.prepare(plan, unrelated)
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_legacy_migrations"
        ).fetchone() == (0,)


def test_governance_backup_must_match_live_store_and_be_independent(tmp_path: Path) -> None:
    stale_root = _legacy_tree(tmp_path / "stale-project")
    stale_store, _objects, stale_dry, stale_service, stale_backup = _import_fixture(
        tmp_path / "stale-fixture",
        stale_root,
    )
    stale_plan = stale_dry.create_plan(
        freeze=_freeze(stale_root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )
    with stale_store.connect() as connection, governance_transaction(connection):
        connection.execute(
            "INSERT INTO governance_store_metadata(key, value) VALUES ('live-marker', '1')"
        )
    with pytest.raises(
        LegacyMigrationScanError,
        match="LEGACY_GOVERNANCE_BACKUP_INVALID",
    ):
        stale_service.prepare(stale_plan, stale_backup)

    linked_root = _legacy_tree(tmp_path / "linked-project")
    linked_store, _objects, linked_dry, linked_service, linked_backup = _import_fixture(
        tmp_path / "linked-fixture",
        linked_root,
    )
    linked_plan = linked_dry.create_plan(
        freeze=_freeze(linked_root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )
    governance_backup = Path(linked_backup.governance_backup_path)
    governance_backup.unlink()
    os.link(linked_store.path, governance_backup)
    hardlinked = linked_backup.model_copy(
        update={
            "governance_backup_digest": (
                f"sha256:{hashlib.sha256(governance_backup.read_bytes()).hexdigest()}"
            )
        }
    )
    with pytest.raises(
        LegacyMigrationScanError,
        match="LEGACY_GOVERNANCE_BACKUP_INVALID",
    ):
        linked_service.prepare(linked_plan, hardlinked)


def test_backup_symlink_nonregular_size_and_schema_fail_closed(tmp_path: Path) -> None:
    symlink_root = _legacy_tree(tmp_path / "symlink-project")
    _store, _objects, symlink_dry, symlink_service, symlink_backup = _import_fixture(
        tmp_path / "symlink-fixture",
        symlink_root,
    )
    symlink_plan = symlink_dry.create_plan(
        freeze=_freeze(symlink_root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )
    project_pack = Path(symlink_backup.project_pack_backup_path)
    actual_pack = project_pack.with_name("actual-pack.snapshot")
    project_pack.rename(actual_pack)
    os.symlink(actual_pack, project_pack)
    with pytest.raises(
        LegacyMigrationScanError,
        match="LEGACY_PROJECT_PACK_BACKUP_INVALID",
    ):
        symlink_service.prepare(symlink_plan, symlink_backup)

    fifo_root = _legacy_tree(tmp_path / "fifo-project")
    _store, _objects, fifo_dry, fifo_service, fifo_backup = _import_fixture(
        tmp_path / "fifo-fixture",
        fifo_root,
    )
    fifo_plan = fifo_dry.create_plan(
        freeze=_freeze(fifo_root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )
    fifo_pack = Path(fifo_backup.project_pack_backup_path)
    fifo_pack.unlink()
    os.mkfifo(fifo_pack)
    with pytest.raises(
        LegacyMigrationScanError,
        match="LEGACY_PROJECT_PACK_BACKUP_INVALID",
    ):
        fifo_service.prepare(fifo_plan, fifo_backup)

    size_root = _legacy_tree(tmp_path / "size-project")
    size_store = GovernanceStore(tmp_path / "size-runtime" / "governance.db")
    size_store.initialize()
    size_backup = _backup_evidence(tmp_path / "size-fixture", size_root, size_store)
    size_dry = LegacyProposalDryRunService(
        size_root,
        PROJECT,
        config=LegacyMigrationScanConfig(maximum_backup_bytes=1),
    )
    size_service = LegacyProposalImportService(
        size_dry,
        size_store,
        ImmutableDefinitionObjectStore(PROJECT, size_root),
    )
    size_plan = size_dry.create_plan(
        freeze=_freeze(size_root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )
    with pytest.raises(
        LegacyMigrationScanError,
        match="LEGACY_PROJECT_PACK_BACKUP_INVALID",
    ):
        size_service.prepare(size_plan, size_backup)

    schema_root = _legacy_tree(tmp_path / "schema-project")
    _store, _objects, schema_dry, schema_service, schema_backup = _import_fixture(
        tmp_path / "schema-fixture",
        schema_root,
    )
    schema_plan = schema_dry.create_plan(
        freeze=_freeze(schema_root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )
    governance_backup = Path(schema_backup.governance_backup_path)
    governance_backup.unlink()
    with sqlite3.connect(governance_backup) as invalid_database:
        invalid_database.execute("CREATE TABLE unrelated(value TEXT)")
    invalid_schema = schema_backup.model_copy(
        update={
            "governance_backup_digest": (
                f"sha256:{hashlib.sha256(governance_backup.read_bytes()).hexdigest()}"
            )
        }
    )
    with pytest.raises(
        LegacyMigrationScanError,
        match="LEGACY_GOVERNANCE_BACKUP_INVALID",
    ):
        schema_service.prepare(schema_plan, invalid_schema)


def test_state_import_failure_rolls_back_all_active_rows_but_keeps_prepared_root(
    tmp_path: Path,
) -> None:
    root = _legacy_tree(tmp_path / "project")
    store, _objects, dry_run, service, backup = _import_fixture(
        tmp_path / "fixture",
        root,
    )
    plan = dry_run.create_plan(
        freeze=_freeze(root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )
    service.prepare(plan, backup)
    with (
        store.connect() as connection,
        pytest.raises(sqlite3.DatabaseError, match="root transition is invalid"),
    ):
        connection.execute(
            """
            UPDATE governance_legacy_migrations
            SET status = 'state_imported', state_imported_at = ?
            """,
            (NOW.isoformat(),),
        )
    with store.connect() as connection, governance_transaction(connection):
        connection.execute(
            """
            CREATE TRIGGER fail_legacy_migration_item
            BEFORE INSERT ON governance_legacy_migration_items
            BEGIN SELECT RAISE(ABORT, 'injected item failure'); END
            """
        )

    with pytest.raises(
        LegacyMigrationScanError,
        match="LEGACY_MIGRATION_STATE_CONFLICT",
    ):
        service.import_state(plan, backup)
    with store.connect() as connection:
        assert connection.execute("SELECT status FROM governance_legacy_migrations").fetchone() == (
            "prepared",
        )
        for table in (
            "governance_active_proposals",
            "governance_definition_revisions",
            "governance_legacy_migration_items",
        ):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)


def test_legacy_mutation_during_object_materialization_prevents_state_commit(
    tmp_path: Path,
) -> None:
    root = _legacy_tree(tmp_path / "project")
    store = GovernanceStore(tmp_path / "runtime" / "governance.db")
    store.initialize()
    dry_run = LegacyProposalDryRunService(root, PROJECT)
    summary = root / ".amplai" / "proposals" / PROPOSAL_ID / "summary.md"
    objects = MutatingDefinitionObjectStore(PROJECT, root, summary)
    service = LegacyProposalImportService(dry_run, store, objects)
    backup = _backup_evidence(tmp_path / "fixture", root, store)
    plan = dry_run.create_plan(
        freeze=_freeze(root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )

    with pytest.raises(LegacyMigrationScanError, match="LEGACY_MIGRATION_PLAN_STALE"):
        service.import_state(plan, backup)
    with store.connect() as connection:
        assert connection.execute("SELECT status FROM governance_legacy_migrations").fetchone() == (
            "prepared",
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_active_proposals"
        ).fetchone() == (0,)


def test_backup_corruption_during_object_materialization_prevents_state_commit(
    tmp_path: Path,
) -> None:
    root = _legacy_tree(tmp_path / "project")
    store = GovernanceStore(tmp_path / "runtime" / "governance.db")
    store.initialize()
    dry_run = LegacyProposalDryRunService(root, PROJECT)
    backup = _backup_evidence(tmp_path / "fixture", root, store)
    objects = CorruptingBackupObjectStore(
        PROJECT,
        root,
        Path(backup.governance_backup_path),
    )
    service = LegacyProposalImportService(dry_run, store, objects)
    plan = dry_run.create_plan(
        freeze=_freeze(root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )

    with pytest.raises(
        LegacyMigrationScanError,
        match="LEGACY_GOVERNANCE_BACKUP_INVALID",
    ):
        service.import_state(plan, backup)
    with store.connect() as connection:
        assert connection.execute("SELECT status FROM governance_legacy_migrations").fetchone() == (
            "prepared",
        )
        for table in (
            "governance_active_proposals",
            "governance_definition_revisions",
            "governance_legacy_migration_items",
        ):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)


def test_process_kill_during_object_materialization_leaves_only_prepared_root(
    tmp_path: Path,
) -> None:
    root = _legacy_tree(tmp_path / "project")
    store, objects, dry_run, service, backup = _import_fixture(
        tmp_path / "fixture",
        root,
    )
    plan = dry_run.create_plan(
        freeze=_freeze(root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )
    marker = tmp_path / "blocked.marker"
    process = multiprocessing.get_context("spawn").Process(
        target=_run_blocking_legacy_import,
        args=(
            str(root),
            str(store.path),
            plan.model_dump_json(),
            backup.model_dump_json(),
            str(marker),
        ),
    )
    process.start()
    for _attempt in range(200):
        if marker.exists():
            break
        if not process.is_alive():
            break
        time.sleep(0.05)
    assert marker.exists()
    process.terminate()
    process.join(timeout=10)
    assert process.exitcode is not None

    with store.connect() as connection:
        assert connection.execute("SELECT status FROM governance_legacy_migrations").fetchone() == (
            "prepared",
        )
        for table in (
            "governance_active_proposals",
            "governance_definition_revisions",
            "governance_legacy_migration_items",
        ):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)

    result = service.import_state(plan, backup)
    assert result.status == "state_imported"
    assert ActiveProposalRepository(store, objects).get(plan.proposals[0].proposal_ref) is not None


def test_ambiguous_state_commit_reconciles_to_completed_replay(tmp_path: Path) -> None:
    root = _legacy_tree(tmp_path / "project")
    store = AmbiguousCommitStore(tmp_path / "runtime" / "governance.db")
    store.initialize()
    store.commit_count = 0
    store.fail_commit_number = 2
    objects = ImmutableDefinitionObjectStore(PROJECT, root)
    dry_run = LegacyProposalDryRunService(root, PROJECT)
    service = LegacyProposalImportService(dry_run, store, objects)
    backup = _backup_evidence(tmp_path / "fixture", root, store)
    plan = dry_run.create_plan(
        freeze=_freeze(root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )

    with pytest.raises(GovernanceCommitAmbiguousError):
        service.import_state(plan, backup)

    result = service.import_state(plan, backup)
    assert result.status == "state_imported"
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_active_proposals"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_legacy_migration_items"
        ).fetchone() == (1,)


def test_existing_qualified_proposal_conflict_preserves_original_state(tmp_path: Path) -> None:
    root = _legacy_tree(tmp_path / "project")
    store = GovernanceStore(tmp_path / "runtime" / "governance.db")
    store.initialize()
    objects = ImmutableDefinitionObjectStore(PROJECT, root)
    ref = ProposalRef(project_ref=PROJECT, proposal_id=PROPOSAL_ID)
    original = canonicalize_definition(
        ProposalDefinitionManifest(
            proposal_ref=ref,
            operations=({"marker": "existing", "type": "CREATE"},),
            base_revision="a13d92f",
            validation_policy_ref="policy/proposal-v3",
        )
    )
    original_ref = objects.put_definition_object(
        ref,
        original.canonical_bytes,
        original.digest,
    )
    repository = ActiveProposalRepository(store, objects)
    repository.activate_definition_revision(
        ref,
        expected_active_digest=None,
        expected_state_revision=0,
        next_object_ref=original_ref,
    )
    dry_run = LegacyProposalDryRunService(root, PROJECT)
    service = LegacyProposalImportService(dry_run, store, objects)
    backup = _backup_evidence(tmp_path / "fixture", root, store)
    plan = dry_run.create_plan(
        freeze=_freeze(root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )

    with pytest.raises(
        LegacyMigrationScanError,
        match="LEGACY_MIGRATION_PROPOSAL_CONFLICT",
    ):
        service.import_state(plan, backup)
    current = repository.get(ref)
    assert current is not None
    assert current.active_definition_digest == original.digest
    with store.connect() as connection:
        assert connection.execute("SELECT status FROM governance_legacy_migrations").fetchone() == (
            "prepared",
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_legacy_migration_items"
        ).fetchone() == (0,)


def test_same_local_proposal_imports_under_two_qualified_projects(tmp_path: Path) -> None:
    cortex = ProjectRef(project_id="cortex", namespace="org/default/project/cortex")
    shared_store = GovernanceStore(tmp_path / "runtime" / "governance.db")
    shared_store.initialize()
    amplai_root = _legacy_tree(tmp_path / "amplai-tree", project=PROJECT)
    cortex_root = _legacy_tree(tmp_path / "cortex-tree", project=cortex)
    _, amplai_objects, amplai_dry, amplai_service, amplai_backup = _import_fixture(
        tmp_path / "amplai-import",
        amplai_root,
        store=shared_store,
    )
    amplai_plan = amplai_dry.create_plan(
        freeze=_freeze(amplai_root, PROJECT),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )
    amplai_service.import_state(amplai_plan, amplai_backup)
    _, cortex_objects, cortex_dry, cortex_service, cortex_backup = _import_fixture(
        tmp_path / "cortex-import",
        cortex_root,
        project=cortex,
        store=shared_store,
    )
    cortex_plan = cortex_dry.create_plan(
        freeze=_freeze(cortex_root, cortex),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )
    cortex_service.import_state(cortex_plan, cortex_backup)

    assert (
        ActiveProposalRepository(shared_store, amplai_objects).get(
            amplai_plan.proposals[0].proposal_ref
        )
        is not None
    )
    assert (
        ActiveProposalRepository(shared_store, cortex_objects).get(
            cortex_plan.proposals[0].proposal_ref
        )
        is not None
    )
    with shared_store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_active_proposals WHERE proposal_id = ?",
            (PROPOSAL_ID,),
        ).fetchone() == (2,)


def test_concurrent_same_plan_import_converges_to_one_completed_result(tmp_path: Path) -> None:
    root = _legacy_tree(tmp_path / "project")
    store, _objects, dry_run, service, backup = _import_fixture(
        tmp_path / "fixture",
        root,
    )
    plan = dry_run.create_plan(
        freeze=_freeze(root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda _index: service.import_state(plan, backup), range(2)))

    assert results[0] == results[1]
    assert results[0].status == "state_imported"
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_active_proposals"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_legacy_migration_items"
        ).fetchone() == (1,)


@pytest.mark.parametrize("status", ("approved", "applied"))
def test_synthetic_approval_target_imports_in_fail_closed_runtime_state(
    tmp_path: Path,
    status: str,
) -> None:
    root = _legacy_tree(tmp_path / "project", status=status)
    store, objects, dry_run, service, backup = _import_fixture(
        tmp_path / "fixture",
        root,
    )
    plan = dry_run.create_plan(
        freeze=_freeze(root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )
    service.import_state(plan, backup)

    view = ActiveProposalRepository(store, objects).get(plan.proposals[0].proposal_ref)
    assert view is not None
    assert view.status == "draft"
    with store.connect() as connection:
        assert connection.execute(
            """
            SELECT target_status, approval_disposition
            FROM governance_legacy_migration_items
            """
        ).fetchone() == (
            "legacy_approval_review_required",
            "synthetic_required",
        )


@pytest.mark.parametrize(
    "status",
    ("changes_requested", "approved", "applied", "rejected", "superseded"),
)
def test_terminal_legacy_state_is_staged_without_breaking_startup_roots(
    tmp_path: Path,
    status: str,
) -> None:
    root = _legacy_tree(tmp_path / "project", status=status)
    if status in {"approved", "applied"}:
        audit = root / ".amplai" / "proposals" / PROPOSAL_ID / "approval-audit.json"
        audit.write_bytes(_approval_audit_payload())
    store, objects, dry_run, service, backup = _import_fixture(
        tmp_path / "fixture",
        root,
    )
    plan = dry_run.create_plan(
        freeze=_freeze(root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )

    service.import_state(plan, backup)

    view = ActiveProposalRepository(store, objects).get(plan.proposals[0].proposal_ref)
    assert view is not None
    assert view.status == "draft"
    assert store.check_startup().healthy
    with store.connect() as connection:
        item = connection.execute(
            """
            SELECT target_status, legacy_git_revision
            FROM governance_legacy_migration_items
            """
        ).fetchone()
    assert item == (status, "a13d92f" if status == "applied" else None)
