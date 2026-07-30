from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from amplai_foundry.domain.identity import ProjectRef
from amplai_foundry.governance import (
    ActiveProposalRepository,
    ImmutableDefinitionObjectStore,
    LegacyApprovalDisposition,
    LegacyMigrationBackupEvidence,
    LegacyMigrationScanConfig,
    LegacyMigrationScanError,
    LegacyMutationFreeze,
    LegacyProposalDryRunService,
    LegacyProposalImportService,
    ProposalRef,
)
from amplai_foundry.governance.definitions import (
    ProposalDefinitionManifest,
    canonicalize_definition,
)
from amplai_foundry.governance.object_store import sha256_digest
from amplai_foundry.governance.store import GovernanceStore, governance_transaction

PROJECT = ProjectRef(project_id="amplai", namespace="org/default/project/amplai")
PROPOSAL_ID = "PROP-20260730-A1B2C3D4"
NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


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
    project_pack.write_bytes(
        json.dumps(_tree_inventory(root), sort_keys=True, default=list).encode("utf-8")
    )
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


class MutatingDefinitionObjectStore(ImmutableDefinitionObjectStore):
    def __init__(self, project_ref: ProjectRef, project_root: Path, legacy_file: Path) -> None:
        super().__init__(project_ref, project_root)
        self.legacy_file = legacy_file
        self.mutated = False

    def _after_publish(self, canonical: Path) -> None:
        if not self.mutated:
            self.mutated = True
            self.legacy_file.write_bytes(self.legacy_file.read_bytes() + b"mutated")


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
    audit.write_text('{"event":"approved"}\n', encoding="utf-8")

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
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_active_proposals"
        ).fetchone() == (0,)


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
    _, cortex_objects, cortex_dry, cortex_service, cortex_backup = _import_fixture(
        tmp_path / "cortex-import",
        cortex_root,
        project=cortex,
        store=shared_store,
    )
    amplai_plan = amplai_dry.create_plan(
        freeze=_freeze(amplai_root, PROJECT),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )
    cortex_plan = cortex_dry.create_plan(
        freeze=_freeze(cortex_root, cortex),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )

    amplai_service.import_state(amplai_plan, amplai_backup)
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
    assert view.status == "changes_requested"
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
