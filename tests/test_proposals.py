from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from amplai_foundry.cli import app
from amplai_foundry.ingestion.service import SourceIngestionService
from amplai_foundry.lint.engine import KnowledgeLinter
from amplai_foundry.proposals.apply import (
    ProposalApplyError,
    ProposalApplyService,
    approve_proposal,
)
from amplai_foundry.proposals.models import Proposal
from amplai_foundry.proposals.repository import ProposalRepository
from amplai_foundry.proposals.validation import ProposalValidator

runner = CliRunner()
NOW = datetime(2026, 7, 12, 12, 0, tzinfo=ZoneInfo("Asia/Seoul"))


def setup_vault(tmp_path: Path) -> tuple[Path, str]:
    vault = tmp_path / "vault"
    (vault / "projects/amplai/00-sources").mkdir(parents=True)
    source = SourceIngestionService(vault).ingest(
        b"Evidence line one\nEvidence line two\n",
        project="amplai",
        source_type="chatgpt",
        title="Evidence",
        now=NOW,
    )
    return vault, source.source_id


def write_draft(
    proposal_dir: Path,
    source_id: str,
    *,
    identifier: str = "CON-9001",
    relation_target: str | None = None,
) -> Path:
    drafts = proposal_dir / "drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    target = relation_target or source_id
    path = drafts / f"{identifier}.md"
    path.write_text(
        f"""---
schema_version: 1
id: {identifier}
namespace: org/default/project/amplai
project: amplai
kind: concept
status: active
title: Curated Memory Concept
summary: Source evidence를 추적하는 apply test concept다.
created_at: 2026-07-12
updated_at: 2026-07-12
source_refs: [{source_id}]
relations:
  - type: derived_from
    target: {target}
revision: 1
tags: [memory, fixture]
---
# Curated Memory Concept

이 draft는 immutable Source evidence에서 파생한 원자 지식이다.
Proposal 승인 전에는 canonical Vault에 없고 safe apply 성공 후에만 등록된다.
""",
        encoding="utf-8",
    )
    return path


def proposal_data(
    source_id: str,
    draft_path: Path | None,
    *,
    operation_type: str = "CREATE",
    target_id: str | None = None,
    status: str = "draft",
) -> dict[str, object]:
    operation: dict[str, object] = {
        "operation_id": "OP-001",
        "type": operation_type,
        "kind": "concept",
        "title": "Curated Memory Concept",
        "reason": "Source가 새 concept를 뒷받침한다.",
        "evidence": [
            {
                "source_id": source_id,
                "locator": {"type": "line_range", "start": 1, "end": 2},
            }
        ],
        "confidence": "medium",
    }
    if draft_path is not None:
        operation["draft_path"] = str(draft_path)
    if target_id is not None:
        operation["target_id"] = target_id
    data: dict[str, object] = {
        "proposal_version": 1,
        "proposal_id": "PROP-20260712-A1B2C3D4",
        "project": "amplai",
        "namespace": "org/default/project/amplai",
        "status": status,
        "source_ids": [source_id],
        "created_at": NOW,
        "created_by": "codex",
        "operations": [operation],
        "notes": [],
    }
    if status in {"approved", "applied"}:
        data.update({"approved_at": NOW, "approved_by": "test-user"})
    if status == "applied":
        data.update({"applied_at": NOW, "applied_by": "test-user"})
    return data


def saved_proposal(tmp_path: Path) -> tuple[Path, ProposalRepository, Proposal, str]:
    vault, source_id = setup_vault(tmp_path)
    repository = ProposalRepository(tmp_path / ".amplai/proposals")
    proposal_dir = repository.path_for("PROP-20260712-A1B2C3D4").parent
    draft = write_draft(proposal_dir, source_id)
    proposal = Proposal.model_validate(proposal_data(source_id, draft))
    repository.save(proposal)
    return vault, repository, proposal, source_id


def test_proposal_schema_and_invalid_operation() -> None:
    with pytest.raises(ValidationError):
        Proposal.model_validate(
            proposal_data("SRC-20260712-A1B2C3D4", None, operation_type="DELETE")
        )
    with pytest.raises(ValidationError):
        Proposal.model_validate(proposal_data("SRC-20260712-A1B2C3D4", None, status="applied"))


def test_validator_rejects_missing_source_and_target(tmp_path: Path) -> None:
    vault, source_id = setup_vault(tmp_path)
    repository = ProposalRepository(tmp_path / ".amplai/proposals")
    proposal_path = repository.path_for("PROP-20260712-A1B2C3D4")
    proposal_path.parent.mkdir(parents=True)
    proposal = Proposal.model_validate(
        proposal_data(
            "SRC-20260712-FFFFFFFF",
            None,
            operation_type="CONFLICT",
            target_id="CON-DOES-NOT-EXIST",
        )
    )

    codes = {issue.code for issue in ProposalValidator(vault).validate(proposal, proposal_path)}

    assert {"SOURCE_MISSING", "EVIDENCE_SOURCE_MISSING", "TARGET_MISSING"} <= codes
    assert source_id != "SRC-20260712-FFFFFFFF"


def test_draft_cannot_apply_and_only_approved_can_apply(tmp_path: Path) -> None:
    vault, repository, proposal, _source_id = saved_proposal(tmp_path)

    with pytest.raises(ProposalApplyError, match="approved"):
        ProposalApplyService(vault, repository).apply(
            proposal, repository.path_for(proposal.proposal_id)
        )

    approved = approve_proposal(proposal, approved_by="test-user", now=NOW)
    result = ProposalApplyService(vault, repository).apply(
        approved, repository.path_for(proposal.proposal_id)
    )

    assert result.touched_paths == (
        "projects/amplai/10-concepts/CON-9001-curated-memory-concept.md",
    )
    assert (vault / result.touched_paths[0]).exists()
    assert KnowledgeLinter().lint(vault).error_count == 0
    applied = repository.get(proposal.proposal_id)
    assert applied is not None
    assert applied.status.value == "applied"
    assert (
        ProposalValidator(vault).validate(applied, repository.path_for(applied.proposal_id)) == []
    )


def test_apply_accepts_relative_vault_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault, repository, proposal, _source_id = saved_proposal(tmp_path)
    approved = approve_proposal(proposal, approved_by="test-user", now=NOW)
    monkeypatch.chdir(tmp_path)

    result = ProposalApplyService(Path("vault"), repository).apply(
        approved, repository.path_for(proposal.proposal_id)
    )

    assert (vault / result.touched_paths[0]).exists()
    assert repository.get(proposal.proposal_id).status.value == "applied"  # type: ignore[union-attr]


def test_pre_apply_lint_failure_leaves_vault_unchanged(tmp_path: Path) -> None:
    vault, repository, proposal, source_id = saved_proposal(tmp_path)
    invalid = vault / "projects/amplai/10-concepts/invalid.md"
    invalid.parent.mkdir(parents=True)
    invalid.write_text(
        """---
schema_version: 1
id: CON-INVALID
namespace: org/default/project/amplai
project: amplai
kind: concept
status: active
title: Invalid existing note
summary: Missing provenance creates a pre-apply lint error.
created_at: 2026-07-12
updated_at: 2026-07-12
source_refs: []
relations: []
revision: 1
tags: []
---
# Invalid
""",
        encoding="utf-8",
    )
    before = {path.relative_to(vault): path.read_bytes() for path in vault.rglob("*.md")}
    approved = approve_proposal(proposal, approved_by="test-user", now=NOW)

    with pytest.raises(ProposalApplyError, match="변경 전"):
        ProposalApplyService(vault, repository).apply(
            approved, repository.path_for(proposal.proposal_id)
        )

    after = {path.relative_to(vault): path.read_bytes() for path in vault.rglob("*.md")}
    assert after == before
    assert any(source_id in payload.decode("utf-8") for payload in before.values())


def test_post_apply_lint_failure_rolls_back_without_vault_changes(tmp_path: Path) -> None:
    vault, source_id = setup_vault(tmp_path)
    repository = ProposalRepository(tmp_path / ".amplai/proposals")
    proposal_path = repository.path_for("PROP-20260712-A1B2C3D4")
    draft = write_draft(proposal_path.parent, source_id, relation_target="CON-MISSING")
    proposal = Proposal.model_validate(proposal_data(source_id, draft))
    repository.save(proposal)
    before = {path.relative_to(vault): path.read_bytes() for path in vault.rglob("*.md")}
    approved = approve_proposal(proposal, approved_by="test-user", now=NOW)

    with pytest.raises(ProposalApplyError, match="임시 결과"):
        ProposalApplyService(vault, repository).apply(approved, proposal_path)

    after = {path.relative_to(vault): path.read_bytes() for path in vault.rglob("*.md")}
    assert after == before
    assert not (vault / "projects/amplai/10-concepts").exists()


def test_conflict_operation_is_never_auto_applied(tmp_path: Path) -> None:
    vault, source_id = setup_vault(tmp_path)
    repository = ProposalRepository(tmp_path / ".amplai/proposals")
    proposal_path = repository.path_for("PROP-20260712-A1B2C3D4")
    proposal_path.parent.mkdir(parents=True)
    conflict = Proposal.model_validate(
        proposal_data(
            source_id,
            None,
            operation_type="CONFLICT",
            target_id=source_id,
            status="approved",
        )
    )

    with pytest.raises(ProposalApplyError, match="CONFLICT"):
        ProposalApplyService(vault, repository).apply(conflict, proposal_path)


def test_cli_validate_show_diff_approve_and_apply(tmp_path: Path) -> None:
    vault, repository, proposal, _source_id = saved_proposal(tmp_path)
    root = repository.root
    path = repository.path_for(proposal.proposal_id)

    validated = runner.invoke(app, ["proposal", "validate", str(path), "--vault", str(vault)])
    shown = runner.invoke(
        app,
        ["proposal", "show", proposal.proposal_id, "--proposal-root", str(root)],
    )
    diffed = runner.invoke(
        app,
        [
            "proposal",
            "diff",
            proposal.proposal_id,
            "--proposal-root",
            str(root),
            "--vault",
            str(vault),
        ],
    )
    approved = runner.invoke(
        app,
        [
            "proposal",
            "approve",
            proposal.proposal_id,
            "--approved-by",
            "test-user",
            "--proposal-root",
            str(root),
        ],
    )
    applied = runner.invoke(
        app,
        [
            "proposal",
            "apply",
            proposal.proposal_id,
            "--proposal-root",
            str(root),
            "--vault",
            str(vault),
        ],
    )

    assert validated.exit_code == shown.exit_code == diffed.exit_code == 0
    assert approved.exit_code == applied.exit_code == 0
    assert "CON-9001" in diffed.stdout
    assert "APPLIED" in applied.stdout
