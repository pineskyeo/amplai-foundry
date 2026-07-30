from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from amplai_foundry.domain.identity import ProjectRef
from amplai_foundry.governance import (
    LegacyApprovalDisposition,
    LegacyMigrationScanConfig,
    LegacyMigrationScanError,
    LegacyMutationFreeze,
    LegacyProposalDryRunService,
)

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
) -> bytes:
    payload: dict[str, object] = {
        "proposal_version": 1,
        "revision": 1,
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
) -> Path:
    root = tmp_path / project.project_id
    proposal = root / ".amplai" / "proposals" / PROPOSAL_ID
    drafts = proposal / "drafts"
    drafts.mkdir(parents=True)
    (proposal / "proposal.yaml").write_bytes(_proposal_payload(project, status=status))
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

    total_root = _legacy_tree(tmp_path / "total")
    with pytest.raises(LegacyMigrationScanError, match="LEGACY_SNAPSHOT_TOTAL_LIMIT"):
        LegacyProposalDryRunService(
            total_root,
            PROJECT,
            config=LegacyMigrationScanConfig(maximum_total_bytes=1),
        ).create_snapshot(_freeze(total_root))

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
