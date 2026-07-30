from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

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


def _freeze() -> LegacyMutationFreeze:
    return LegacyMutationFreeze(
        freeze_id="MFR-0000000000000001",
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


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_dry_run_is_deterministic_and_write_free(tmp_path: Path) -> None:
    root = _legacy_tree(tmp_path)
    before = _tree_bytes(root)
    service = LegacyProposalDryRunService(root, PROJECT)

    first = service.create_plan(
        freeze=_freeze(),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )
    second = service.create_plan(
        freeze=_freeze(),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )

    assert first == second
    assert first.plan_id == f"MPL-{first.plan_digest[-16:].upper()}"
    assert first.project_ref == PROJECT
    assert first.proposals[0].proposal_ref.project_ref == PROJECT
    assert first.proposals[0].target_status == "draft"
    assert first.proposals[0].approval_disposition is LegacyApprovalDisposition.NOT_REQUIRED
    assert _tree_bytes(root) == before
    assert not (root / ".amplai" / "runtime").exists()


def test_snapshot_hashes_every_file_in_byte_sorted_relative_path_order(tmp_path: Path) -> None:
    root = _legacy_tree(tmp_path)
    snapshot = LegacyProposalDryRunService(root, PROJECT).create_snapshot(_freeze())

    paths = tuple(item.relative_path for item in snapshot.files)
    assert paths == tuple(sorted(paths, key=lambda value: value.encode("utf-8")))
    assert paths == (
        f"{PROPOSAL_ID}/drafts/CON-0001.md",
        f"{PROPOSAL_ID}/proposal.yaml",
        f"{PROPOSAL_ID}/summary.md",
    )
    assert snapshot.total_bytes == sum(item.byte_length for item in snapshot.files)
    assert snapshot.snapshot_id == f"MPS-{snapshot.snapshot_digest[-16:].upper()}"


def test_plan_binds_freeze_evidence_but_snapshot_ignores_v3_object_directories(
    tmp_path: Path,
) -> None:
    root = _legacy_tree(tmp_path)
    definition = root / ".amplai" / "proposals" / PROPOSAL_ID / "definitions" / "sha256-object.yaml"
    definition.parent.mkdir()
    definition.write_text("v3 object\n", encoding="utf-8")
    service = LegacyProposalDryRunService(root, PROJECT)
    first = service.create_plan(
        freeze=_freeze(),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )
    second_freeze = _freeze().model_copy(update={"freeze_id": "MFR-0000000000000002"})
    second = service.create_plan(
        freeze=second_freeze,
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )

    assert first.snapshot_digest == second.snapshot_digest
    assert first.plan_digest != second.plan_digest
    assert all(
        "/definitions/" not in item.relative_path
        for item in service.create_snapshot(_freeze()).files
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
                freeze=_freeze(),
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
            freeze=_freeze(),
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
            freeze=_freeze(),
            base_revision="a13d92f",
            validation_policy_ref="policy/migration/v1",
        )

    proposal_path = root / ".amplai" / "proposals" / PROPOSAL_ID / "proposal.yaml"
    renamed = proposal_path.parent.with_name("PROP-20260730-FFFFFFFF")
    proposal_path.parent.rename(renamed)
    with pytest.raises(LegacyMigrationScanError, match="LEGACY_PROPOSAL_PATH_MISMATCH"):
        LegacyProposalDryRunService(root, PROJECT).create_plan(
            freeze=_freeze(),
            base_revision="a13d92f",
            validation_policy_ref="policy/migration/v1",
        )


def test_same_local_proposal_id_in_different_projects_has_distinct_plan_identity(
    tmp_path: Path,
) -> None:
    cortex = ProjectRef(project_id="cortex", namespace="org/default/project/cortex")
    amplai_root = _legacy_tree(tmp_path / "one", project=PROJECT)
    cortex_root = _legacy_tree(tmp_path / "two", project=cortex)

    amplai = LegacyProposalDryRunService(amplai_root, PROJECT).create_plan(
        freeze=_freeze(),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )
    cortex_plan = LegacyProposalDryRunService(cortex_root, cortex).create_plan(
        freeze=_freeze(),
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
        LegacyProposalDryRunService(root, PROJECT).create_snapshot(_freeze())

    limited_root = _legacy_tree(tmp_path / "limited")
    with pytest.raises(LegacyMigrationScanError, match="LEGACY_SNAPSHOT_FILE_SIZE_LIMIT"):
        LegacyProposalDryRunService(
            limited_root,
            PROJECT,
            config=LegacyMigrationScanConfig(maximum_file_bytes=8),
        ).create_snapshot(_freeze())


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
        service.create_snapshot(_freeze())


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
            freeze=_freeze(),
            base_revision=base_revision,
            validation_policy_ref=policy,
        )
