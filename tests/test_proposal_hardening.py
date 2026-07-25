from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from amplai_foundry.domain.models import MemoryObject
from amplai_foundry.domain.project import ProjectPathError
from amplai_foundry.ingestion.service import SourceIngestionService
from amplai_foundry.parsing.markdown import parse_markdown_file
from amplai_foundry.proposals.apply import (
    ProposalApplyError,
    ProposalApplyService,
    approve_proposal,
)
from amplai_foundry.proposals.diff import destination_for_create
from amplai_foundry.proposals.models import Proposal
from amplai_foundry.proposals.repository import ProposalRepository
from amplai_foundry.proposals.validation import ProposalValidator

NOW = datetime(2026, 7, 12, 12, 0, tzinfo=ZoneInfo("Asia/Seoul"))


def _setup(tmp_path: Path) -> tuple[Path, ProposalRepository, str]:
    vault = tmp_path / "vault"
    (vault / "projects/amplai/00-sources").mkdir(parents=True)
    source = SourceIngestionService(vault).ingest(
        b"Evidence line one\nEvidence line two\n",
        project="amplai",
        source_type="chatgpt",
        title="Hardening evidence",
        now=NOW,
    )
    repository = ProposalRepository(tmp_path / ".amplai/proposals")
    return vault, repository, source.source_id


def _add_project_source(vault: Path, project: str) -> str:
    (vault / "projects" / project / "00-sources").mkdir(parents=True)
    result = SourceIngestionService(vault).ingest(
        f"{project} evidence line one\n{project} evidence line two\n".encode(),
        project=project,
        source_type="chatgpt",
        title=f"{project} evidence",
        now=NOW,
    )
    return result.source_id


def _proposal_data(
    source_id: str,
    *,
    proposal_id: str,
    operation_type: str,
    kind: str,
    target_id: str | None,
    draft_path: Path | None,
    status: str = "draft",
    expected_revision: int | None = None,
    expected_target_sha256: str | None = None,
    project: str = "amplai",
) -> dict[str, object]:
    operation: dict[str, object] = {
        "operation_id": "OP-001",
        "type": operation_type,
        "kind": kind,
        "title": "Hardening operation",
        "reason": "무결성 경계를 검증한다.",
        "evidence": [
            {
                "source_id": source_id,
                "locator": {"type": "line_range", "start": 1, "end": 2},
            }
        ],
        "confidence": "high",
    }
    if target_id is not None:
        operation["target_id"] = target_id
    if draft_path is not None:
        operation["draft_path"] = str(draft_path)
    if expected_revision is not None:
        operation["expected_revision"] = expected_revision
    if expected_target_sha256 is not None:
        operation["expected_target_sha256"] = expected_target_sha256
    data: dict[str, object] = {
        "proposal_version": 1,
        "proposal_id": proposal_id,
        "project": project,
        "namespace": f"org/default/project/{project}",
        "status": status,
        "source_ids": [source_id],
        "created_at": NOW,
        "created_by": "test",
        "operations": [operation],
        "notes": [],
    }
    if status in {"approved", "applied"}:
        data.update({"approved_at": NOW, "approved_by": "reviewer"})
    if status == "applied":
        data.update({"applied_at": NOW, "applied_by": "reviewer"})
    return data


def _write_concept(
    path: Path,
    source_id: str,
    *,
    revision: int,
    status: str = "active",
    created_at: str = "2026-07-12",
    updated_at: str = "2026-07-12",
    project: str = "amplai",
    namespace: str = "org/default/project/amplai",
    kind: str = "concept",
    body: str = "원본 canonical 내용이다.",
    identifier: str = "CON-9100",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""---
schema_version: 1
id: {identifier}
namespace: {namespace}
project: {project}
kind: {kind}
status: {status}
title: Concurrency Target
summary: Optimistic concurrency와 전이 검증을 위한 지식이다.
created_at: {created_at}
updated_at: {updated_at}
source_refs: [{source_id}]
relations:
  - type: derived_from
    target: {source_id}
revision: {revision}
tags: [hardening]
---
# Concurrency Target

{body}
변경 유실을 막기 위해 revision과 raw Markdown hash를 함께 검사한다.
""",
        encoding="utf-8",
    )


def _update_proposal(
    vault: Path,
    repository: ProposalRepository,
    source_id: str,
    *,
    proposal_id: str,
    body: str,
) -> Proposal:
    target = vault / "projects/amplai/10-concepts/CON-9100-concurrency-target.md"
    proposal_path = repository.path_for(proposal_id)
    draft = proposal_path.parent / "drafts/CON-9100.md"
    _write_concept(draft, source_id, revision=2, updated_at="2026-07-13", body=body)
    return Proposal.model_validate(
        _proposal_data(
            source_id,
            proposal_id=proposal_id,
            operation_type="UPDATE",
            kind="concept",
            target_id="CON-9100",
            draft_path=draft,
            expected_revision=1,
            expected_target_sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
        )
    )


def _vault_snapshot(vault: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(vault): path.read_bytes() for path in vault.rglob("*") if path.is_file()
    }


@pytest.mark.parametrize(
    "project",
    [".", "..", "../..", "/absolute", "a/b", r"a\b", "bad project", "", "bad!project"],
)
def test_proposal_rejects_invalid_project(project: str) -> None:
    with pytest.raises(ValidationError):
        Proposal.model_validate(
            _proposal_data(
                "SRC-20260712-A1B2C3D4",
                proposal_id="PROP-20260712-A1B2C3D4",
                operation_type="IGNORE",
                kind="concept",
                target_id=None,
                draft_path=None,
                project=project,
            )
        )


def test_new_mutating_proposal_requires_concurrency_preconditions(tmp_path: Path) -> None:
    vault, repository, source_id = _setup(tmp_path)
    target = vault / "projects/amplai/10-concepts/CON-9100-concurrency-target.md"
    _write_concept(target, source_id, revision=1)
    draft = repository.path_for("PROP-20260712-A1B2C3D4").parent / "drafts/CON-9100.md"
    _write_concept(draft, source_id, revision=2)

    with pytest.raises(ValidationError):
        Proposal.model_validate(
            _proposal_data(
                source_id,
                proposal_id="PROP-20260712-A1B2C3D4",
                operation_type="UPDATE",
                kind="concept",
                target_id="CON-9100",
                draft_path=draft,
            )
        )


@pytest.mark.parametrize("operation_type", ["CREATE", "CONFLICT", "IGNORE", "MERGE", "SPLIT"])
def test_non_precondition_operations_reject_target_preconditions(
    operation_type: str,
) -> None:
    target_required = operation_type in {"CONFLICT", "MERGE", "SPLIT"}
    with pytest.raises(ValidationError):
        Proposal.model_validate(
            _proposal_data(
                "SRC-20260712-A1B2C3D4",
                proposal_id="PROP-20260712-A1B2C3D4",
                operation_type=operation_type,
                kind="concept",
                target_id="CON-9100" if target_required else None,
                draft_path=Path("drafts/CON-9100.md") if operation_type == "CREATE" else None,
                expected_revision=1,
                expected_target_sha256="a" * 64,
            )
        )


def test_historical_applied_proposal_without_preconditions_still_loads() -> None:
    proposal = Proposal.model_validate(
        _proposal_data(
            "SRC-20260712-A1B2C3D4",
            proposal_id="PROP-20260712-A1B2C3D4",
            operation_type="UPDATE",
            kind="concept",
            target_id="CON-9100",
            draft_path=Path("drafts/CON-9100.md"),
            status="applied",
        )
    )
    assert proposal.status.value == "applied"


@pytest.mark.parametrize(
    "path",
    [
        Path(".amplai/proposals/PROP-20260712-E116EE23/proposal.yaml"),
        Path(".amplai/proposals/PROP-20260714-122C8F66/proposal.yaml"),
    ],
)
def test_committed_applied_v1_proposals_remain_loadable(path: Path) -> None:
    proposal = ProposalRepository.load(path)
    assert proposal.status.value == "applied"


def test_validator_rejects_cross_project_declared_source(tmp_path: Path) -> None:
    vault, repository, source_a = _setup(tmp_path)
    source_b = _add_project_source(vault, "other")
    proposal_path = repository.path_for("PROP-20260712-A1B2C3D4")
    data = _proposal_data(
        source_a,
        proposal_id="PROP-20260712-A1B2C3D4",
        operation_type="IGNORE",
        kind="concept",
        target_id=None,
        draft_path=None,
    )
    data["source_ids"] = [source_b]
    proposal = Proposal.model_validate(data)

    codes = {issue.code for issue in ProposalValidator(vault).validate(proposal, proposal_path)}

    assert "PROPOSAL_SOURCE_PROJECT_MISMATCH" in codes


def test_validator_rejects_cross_project_evidence_source(tmp_path: Path) -> None:
    vault, repository, source_a = _setup(tmp_path)
    source_b = _add_project_source(vault, "other")
    proposal_path = repository.path_for("PROP-20260712-A1B2C3D4")
    data = _proposal_data(
        source_a,
        proposal_id="PROP-20260712-A1B2C3D4",
        operation_type="IGNORE",
        kind="concept",
        target_id=None,
        draft_path=None,
    )
    operations = data["operations"]
    assert isinstance(operations, list)
    operation = operations[0]
    assert isinstance(operation, dict)
    operation["evidence"] = [
        {
            "source_id": source_b,
            "locator": {"type": "line_range", "start": 1, "end": 2},
        }
    ]
    proposal = Proposal.model_validate(data)

    codes = {issue.code for issue in ProposalValidator(vault).validate(proposal, proposal_path)}

    assert "PROPOSAL_EVIDENCE_PROJECT_MISMATCH" in codes


@pytest.mark.parametrize("operation_type", ["UPDATE", "LINK", "SUPERSEDE"])
def test_validator_rejects_cross_project_target(tmp_path: Path, operation_type: str) -> None:
    vault, repository, source_a = _setup(tmp_path)
    source_b = _add_project_source(vault, "other")
    target = vault / "projects/other/10-concepts/CON-9200-cross-project-target.md"
    _write_concept(
        target,
        source_b,
        revision=1,
        project="other",
        namespace="org/default/project/other",
        identifier="CON-9200",
    )
    proposal_path = repository.path_for("PROP-20260712-A1B2C3D4")
    draft = proposal_path.parent / "drafts/CON-9200.md"
    _write_concept(
        draft,
        source_a,
        revision=2,
        updated_at="2026-07-13",
        identifier="CON-9200",
    )
    proposal = Proposal.model_validate(
        _proposal_data(
            source_a,
            proposal_id="PROP-20260712-A1B2C3D4",
            operation_type=operation_type,
            kind="concept",
            target_id="CON-9200",
            draft_path=draft,
            expected_revision=1,
            expected_target_sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
        )
    )

    codes = {issue.code for issue in ProposalValidator(vault).validate(proposal, proposal_path)}

    assert "PROPOSAL_TARGET_PROJECT_MISMATCH" in codes


@pytest.mark.parametrize(
    ("draft_project", "draft_namespace", "expected_code"),
    [
        ("other", "org/default/project/other", "PROPOSAL_DRAFT_PROJECT_MISMATCH"),
        ("amplai", "org/default/project/other", "PROPOSAL_DRAFT_NAMESPACE_MISMATCH"),
    ],
)
@pytest.mark.parametrize("operation_type", ["CREATE", "UPDATE", "LINK", "SUPERSEDE"])
def test_validator_rejects_cross_project_draft(
    tmp_path: Path,
    draft_project: str,
    draft_namespace: str,
    expected_code: str,
    operation_type: str,
) -> None:
    vault, repository, source_a = _setup(tmp_path)
    proposal_path = repository.path_for("PROP-20260712-A1B2C3D4")
    draft = proposal_path.parent / "drafts/CON-9200.md"
    target_id: str | None = None
    expected_revision: int | None = None
    expected_hash: str | None = None
    revision = 1
    if operation_type != "CREATE":
        target = vault / "projects/amplai/10-concepts/CON-9200-scope-target.md"
        _write_concept(target, source_a, revision=1, identifier="CON-9200")
        target_id = "CON-9200"
        expected_revision = 1
        expected_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        revision = 2
    _write_concept(
        draft,
        source_a,
        revision=revision,
        updated_at="2026-07-13",
        project=draft_project,
        namespace=draft_namespace,
        identifier="CON-9200",
    )
    proposal = Proposal.model_validate(
        _proposal_data(
            source_a,
            proposal_id="PROP-20260712-A1B2C3D4",
            operation_type=operation_type,
            kind="concept",
            target_id=target_id,
            draft_path=draft,
            expected_revision=expected_revision,
            expected_target_sha256=expected_hash,
        )
    )

    codes = {issue.code for issue in ProposalValidator(vault).validate(proposal, proposal_path)}

    assert expected_code in codes


@pytest.mark.parametrize("operation_type", ["CREATE", "UPDATE"])
def test_apply_rejects_cross_project_change_when_validator_is_bypassed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation_type: str,
) -> None:
    vault, repository, source_a = _setup(tmp_path)
    source_b = _add_project_source(vault, "other")
    proposal_path = repository.path_for("PROP-20260712-A1B2C3D4")
    draft = proposal_path.parent / "drafts/CON-9200.md"
    target_id: str | None = None
    expected_revision: int | None = None
    expected_hash: str | None = None
    if operation_type == "CREATE":
        _write_concept(
            draft,
            source_b,
            revision=1,
            project="other",
            namespace="org/default/project/other",
            identifier="CON-9200",
        )
        expected_code = "PROPOSAL_DRAFT_PROJECT_MISMATCH"
    else:
        target = vault / "projects/other/10-concepts/CON-9200-cross-project-target.md"
        _write_concept(
            target,
            source_b,
            revision=1,
            project="other",
            namespace="org/default/project/other",
            identifier="CON-9200",
        )
        _write_concept(
            draft,
            source_a,
            revision=2,
            updated_at="2026-07-13",
            identifier="CON-9200",
        )
        target_id = "CON-9200"
        expected_revision = 1
        expected_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        expected_code = "PROPOSAL_TARGET_PROJECT_MISMATCH"
    proposal = Proposal.model_validate(
        _proposal_data(
            source_a,
            proposal_id="PROP-20260712-A1B2C3D4",
            operation_type=operation_type,
            kind="concept",
            target_id=target_id,
            draft_path=draft,
            status="approved",
            expected_revision=expected_revision,
            expected_target_sha256=expected_hash,
        )
    )
    repository.save(proposal)
    before = _vault_snapshot(vault)
    monkeypatch.setattr(ProposalValidator, "validate", lambda *_args, **_kwargs: [])

    with pytest.raises(ProposalApplyError, match=expected_code):
        ProposalApplyService(vault, repository).apply(proposal, proposal_path)

    assert _vault_snapshot(vault) == before
    stored = repository.get(proposal.proposal_id)
    assert stored is not None
    assert stored.status.value == "approved"


def test_create_destination_rejects_project_root_symlink_to_sibling(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    other = vault / "projects/other"
    other.mkdir(parents=True)
    (vault / "projects/amplai").symlink_to(other, target_is_directory=True)
    draft_path = tmp_path / "CON-9200.md"
    _write_concept(draft_path, "SRC-20260712-A1B2C3D4", revision=1, identifier="CON-9200")
    document = parse_markdown_file(draft_path)
    draft = MemoryObject.model_validate({**document.metadata, "content": document.content})

    with pytest.raises(ProjectPathError):
        destination_for_create(
            vault,
            draft,
            project="amplai",
            namespace="org/default/project/amplai",
        )

    assert list(other.iterdir()) == []


@pytest.mark.parametrize("operation_type", ["UPDATE", "LINK", "SUPERSEDE"])
def test_validator_rejects_source_mutation(tmp_path: Path, operation_type: str) -> None:
    vault, repository, source_id = _setup(tmp_path)
    source_path = next((vault / "projects/amplai/00-sources").glob("*.md"))
    proposal_path = repository.path_for("PROP-20260712-A1B2C3D4")
    draft = proposal_path.parent / "drafts/source.md"
    draft.parent.mkdir(parents=True)
    draft.write_bytes(source_path.read_bytes())
    proposal = Proposal.model_validate(
        _proposal_data(
            source_id,
            proposal_id="PROP-20260712-A1B2C3D4",
            operation_type=operation_type,
            kind="source",
            target_id=source_id,
            draft_path=draft,
            expected_revision=1,
            expected_target_sha256=hashlib.sha256(source_path.read_bytes()).hexdigest(),
        )
    )

    codes = {issue.code for issue in ProposalValidator(vault).validate(proposal, proposal_path)}
    assert "PROPOSAL_SOURCE_MUTATION_FORBIDDEN" in codes


def test_validator_rejects_source_create_but_allows_source_evidence(
    tmp_path: Path,
) -> None:
    vault, repository, source_id = _setup(tmp_path)
    source_path = next((vault / "projects/amplai/00-sources").glob("*.md"))
    proposal_path = repository.path_for("PROP-20260712-A1B2C3D4")
    draft = proposal_path.parent / "drafts/source.md"
    draft.parent.mkdir(parents=True)
    draft.write_bytes(source_path.read_bytes())
    proposal = Proposal.model_validate(
        _proposal_data(
            source_id,
            proposal_id="PROP-20260712-A1B2C3D4",
            operation_type="CREATE",
            kind="source",
            target_id=None,
            draft_path=draft,
        )
    )

    codes = {issue.code for issue in ProposalValidator(vault).validate(proposal, proposal_path)}
    assert "PROPOSAL_SOURCE_CREATE_FORBIDDEN" in codes
    assert "EVIDENCE_SOURCE_MISSING" not in codes


def test_source_create_cannot_hide_behind_mismatched_operation_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, repository, source_id = _setup(tmp_path)
    source_path = next((vault / "projects/amplai/00-sources").glob("*.md"))
    proposal_path = repository.path_for("PROP-20260712-A1B2C3D4")
    draft = proposal_path.parent / "drafts/source.md"
    draft.parent.mkdir(parents=True)
    draft.write_bytes(source_path.read_bytes())
    proposal = Proposal.model_validate(
        _proposal_data(
            source_id,
            proposal_id="PROP-20260712-A1B2C3D4",
            operation_type="CREATE",
            kind="concept",
            target_id=None,
            draft_path=draft,
            status="approved",
        )
    )
    codes = {issue.code for issue in ProposalValidator(vault).validate(proposal, proposal_path)}
    assert "PROPOSAL_SOURCE_CREATE_FORBIDDEN" in codes
    monkeypatch.setattr(ProposalValidator, "validate", lambda *_args, **_kwargs: [])

    with pytest.raises(ProposalApplyError, match="PROPOSAL_SOURCE_CREATE_FORBIDDEN"):
        ProposalApplyService(vault, repository).apply(proposal, proposal_path)


@pytest.mark.parametrize("operation_type", ["CREATE", "UPDATE", "LINK", "SUPERSEDE"])
def test_apply_rejects_source_changes_even_if_validator_is_bypassed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation_type: str
) -> None:
    vault, repository, source_id = _setup(tmp_path)
    source_path = next((vault / "projects/amplai/00-sources").glob("*.md"))
    proposal_path = repository.path_for("PROP-20260712-A1B2C3D4")
    draft = proposal_path.parent / "drafts/source.md"
    draft.parent.mkdir(parents=True)
    draft.write_bytes(source_path.read_bytes())
    is_create = operation_type == "CREATE"
    proposal = Proposal.model_validate(
        _proposal_data(
            source_id,
            proposal_id="PROP-20260712-A1B2C3D4",
            operation_type=operation_type,
            kind="source",
            target_id=None if is_create else source_id,
            draft_path=draft,
            status="approved",
            expected_revision=None if is_create else 1,
            expected_target_sha256=(
                None if is_create else hashlib.sha256(source_path.read_bytes()).hexdigest()
            ),
        )
    )
    before = source_path.read_bytes()
    monkeypatch.setattr(ProposalValidator, "validate", lambda *_args, **_kwargs: [])

    expected_code = (
        "PROPOSAL_SOURCE_CREATE_FORBIDDEN" if is_create else "PROPOSAL_SOURCE_MUTATION_FORBIDDEN"
    )
    with pytest.raises(ProposalApplyError, match=expected_code):
        ProposalApplyService(vault, repository).apply(proposal, proposal_path)

    assert source_path.read_bytes() == before


def test_stale_revision_and_hash_leave_vault_and_proposal_unchanged(
    tmp_path: Path,
) -> None:
    vault, repository, source_id = _setup(tmp_path)
    target = vault / "projects/amplai/10-concepts/CON-9100-concurrency-target.md"
    _write_concept(target, source_id, revision=1)
    proposal = _update_proposal(
        vault,
        repository,
        source_id,
        proposal_id="PROP-20260712-A1B2C3D4",
        body="새로운 변경 내용이다.",
    )
    approved = approve_proposal(proposal, approved_by="reviewer", now=NOW)
    repository.save(approved)
    target.write_text(target.read_text(encoding="utf-8") + "\n외부 변경\n", encoding="utf-8")
    before = {path.relative_to(vault): path.read_bytes() for path in vault.rglob("*.md")}

    with pytest.raises(ProposalApplyError, match="PROPOSAL_STALE_TARGET_HASH"):
        ProposalApplyService(vault, repository).apply(
            approved, repository.path_for(approved.proposal_id)
        )

    after = {path.relative_to(vault): path.read_bytes() for path in vault.rglob("*.md")}
    assert after == before
    assert repository.get(approved.proposal_id).status.value == "approved"  # type: ignore[union-attr]


def test_two_proposals_based_on_revision_one_cannot_lose_update(tmp_path: Path) -> None:
    vault, repository, source_id = _setup(tmp_path)
    target = vault / "projects/amplai/10-concepts/CON-9100-concurrency-target.md"
    _write_concept(target, source_id, revision=1)
    proposal_a = _update_proposal(
        vault,
        repository,
        source_id,
        proposal_id="PROP-20260712-AAAAAAAA",
        body="Proposal A의 변경이다.",
    )
    proposal_b = _update_proposal(
        vault,
        repository,
        source_id,
        proposal_id="PROP-20260712-BBBBBBBB",
        body="Proposal B의 변경이다.",
    )
    approved_a = approve_proposal(proposal_a, approved_by="reviewer", now=NOW)
    approved_b = approve_proposal(proposal_b, approved_by="reviewer", now=NOW)
    repository.save(approved_a)
    repository.save(approved_b)

    ProposalApplyService(vault, repository).apply(
        approved_a, repository.path_for(approved_a.proposal_id)
    )
    revision_two = target.read_bytes()

    with pytest.raises(ProposalApplyError, match="PROPOSAL_STALE_REVISION"):
        ProposalApplyService(vault, repository).apply(
            approved_b, repository.path_for(approved_b.proposal_id)
        )

    assert target.read_bytes() == revision_two
    assert repository.get(approved_b.proposal_id).status.value == "approved"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    [
        ("revision", 1, "PROPOSAL_TRANSITION_REVISION"),
        ("created_at", "2026-07-11", "PROPOSAL_TRANSITION_CREATED_AT"),
        ("project", "other", "PROPOSAL_TRANSITION_PROJECT"),
        ("namespace", "org/default/project/other", "PROPOSAL_TRANSITION_NAMESPACE"),
        ("kind", "principle", "PROPOSAL_TRANSITION_KIND"),
        ("updated_at", "2026-07-11", "PROPOSAL_TRANSITION_UPDATED_AT"),
    ],
)
def test_validator_rejects_invalid_update_transition(
    tmp_path: Path, field: str, value: object, expected_code: str
) -> None:
    vault, repository, source_id = _setup(tmp_path)
    target = vault / "projects/amplai/10-concepts/CON-9100-concurrency-target.md"
    _write_concept(target, source_id, revision=1)
    proposal_path = repository.path_for("PROP-20260712-A1B2C3D4")
    draft = proposal_path.parent / "drafts/CON-9100.md"
    kwargs: dict[str, object] = {
        "revision": 2,
        "created_at": "2026-07-12",
        "updated_at": "2026-07-13",
        "project": "amplai",
        "namespace": "org/default/project/amplai",
        "kind": "concept",
    }
    kwargs[field] = value
    _write_concept(draft, source_id, body="변경 draft다.", **kwargs)  # type: ignore[arg-type]
    proposal = Proposal.model_validate(
        _proposal_data(
            source_id,
            proposal_id="PROP-20260712-A1B2C3D4",
            operation_type="UPDATE",
            kind=str(kwargs["kind"]),
            target_id="CON-9100",
            draft_path=draft,
            expected_revision=1,
            expected_target_sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
        )
    )

    codes = {issue.code for issue in ProposalValidator(vault).validate(proposal, proposal_path)}
    assert expected_code in codes


def test_validator_rejects_decreased_revision(tmp_path: Path) -> None:
    vault, repository, source_id = _setup(tmp_path)
    target = vault / "projects/amplai/10-concepts/CON-9100-concurrency-target.md"
    _write_concept(target, source_id, revision=2)
    proposal_path = repository.path_for("PROP-20260712-A1B2C3D4")
    draft = proposal_path.parent / "drafts/CON-9100.md"
    _write_concept(draft, source_id, revision=1, updated_at="2026-07-13")
    proposal = Proposal.model_validate(
        _proposal_data(
            source_id,
            proposal_id="PROP-20260712-A1B2C3D4",
            operation_type="UPDATE",
            kind="concept",
            target_id="CON-9100",
            draft_path=draft,
            expected_revision=2,
            expected_target_sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
        )
    )

    codes = {issue.code for issue in ProposalValidator(vault).validate(proposal, proposal_path)}
    assert "PROPOSAL_TRANSITION_REVISION" in codes


def test_validator_rejects_terminal_status_reactivation(tmp_path: Path) -> None:
    vault, repository, source_id = _setup(tmp_path)
    target = vault / "projects/amplai/10-concepts/CON-9100-concurrency-target.md"
    _write_concept(target, source_id, revision=1, status="archived")
    proposal_path = repository.path_for("PROP-20260712-A1B2C3D4")
    draft = proposal_path.parent / "drafts/CON-9100.md"
    _write_concept(draft, source_id, revision=2, status="active", updated_at="2026-07-13")
    proposal = Proposal.model_validate(
        _proposal_data(
            source_id,
            proposal_id="PROP-20260712-A1B2C3D4",
            operation_type="UPDATE",
            kind="concept",
            target_id="CON-9100",
            draft_path=draft,
            expected_revision=1,
            expected_target_sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
        )
    )

    codes = {issue.code for issue in ProposalValidator(vault).validate(proposal, proposal_path)}
    assert "PROPOSAL_TRANSITION_STATUS" in codes


@pytest.mark.parametrize(
    ("before_status", "after_status"),
    [
        ("candidate", "active"),
        ("active", "deprecated"),
        ("deprecated", "active"),
    ],
)
def test_validator_allows_documented_status_transitions(
    tmp_path: Path, before_status: str, after_status: str
) -> None:
    vault, repository, source_id = _setup(tmp_path)
    target = vault / "projects/amplai/10-concepts/CON-9100-concurrency-target.md"
    _write_concept(target, source_id, revision=1, status=before_status)
    proposal_path = repository.path_for("PROP-20260712-A1B2C3D4")
    draft = proposal_path.parent / "drafts/CON-9100.md"
    _write_concept(
        draft,
        source_id,
        revision=2,
        status=after_status,
        updated_at="2026-07-13",
    )
    proposal = Proposal.model_validate(
        _proposal_data(
            source_id,
            proposal_id="PROP-20260712-A1B2C3D4",
            operation_type="UPDATE",
            kind="concept",
            target_id="CON-9100",
            draft_path=draft,
            expected_revision=1,
            expected_target_sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
        )
    )

    codes = {issue.code for issue in ProposalValidator(vault).validate(proposal, proposal_path)}
    assert "PROPOSAL_TRANSITION_STATUS" not in codes


def test_link_operation_applies_with_matching_preconditions(tmp_path: Path) -> None:
    vault, repository, source_id = _setup(tmp_path)
    target = vault / "projects/amplai/10-concepts/CON-9100-concurrency-target.md"
    _write_concept(target, source_id, revision=1)
    proposal_path = repository.path_for("PROP-20260712-A1B2C3D4")
    draft = proposal_path.parent / "drafts/CON-9100.md"
    _write_concept(
        draft,
        source_id,
        revision=2,
        updated_at="2026-07-13",
        body="LINK operation으로 구조화된 관계를 갱신한다.",
    )
    proposal = Proposal.model_validate(
        _proposal_data(
            source_id,
            proposal_id="PROP-20260712-A1B2C3D4",
            operation_type="LINK",
            kind="concept",
            target_id="CON-9100",
            draft_path=draft,
            status="approved",
            expected_revision=1,
            expected_target_sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
        )
    )

    ProposalApplyService(vault, repository).apply(proposal, proposal_path)

    assert "LINK operation" in target.read_text(encoding="utf-8")


def test_supersede_operation_applies_with_reciprocal_create(tmp_path: Path) -> None:
    vault, repository, source_id = _setup(tmp_path)
    target = vault / "projects/amplai/10-concepts/CON-9100-concurrency-target.md"
    _write_concept(target, source_id, revision=1)
    proposal_path = repository.path_for("PROP-20260712-A1B2C3D4")
    old_draft = proposal_path.parent / "drafts/CON-9100.md"
    _write_concept(
        old_draft,
        source_id,
        revision=2,
        status="superseded",
        updated_at="2026-07-13",
        body="새 지식 CON-9101로 대체되었다.",
    )
    old_text = old_draft.read_text(encoding="utf-8").replace(
        "revision: 2", "superseded_by: CON-9101\nrevision: 2"
    )
    old_draft.write_text(old_text, encoding="utf-8")
    new_draft = proposal_path.parent / "drafts/CON-9101.md"
    _write_concept(
        new_draft,
        source_id,
        revision=1,
        updated_at="2026-07-13",
        body="이 지식은 CON-9100을 명시적으로 대체한다.",
    )
    new_text = (
        new_draft.read_text(encoding="utf-8")
        .replace("id: CON-9100", "id: CON-9101")
        .replace(
            "  - type: derived_from\n    target: " + source_id,
            "  - type: derived_from\n    target: "
            + source_id
            + "\n  - type: supersedes\n    target: CON-9100",
        )
    )
    new_draft.write_text(new_text, encoding="utf-8")
    data = _proposal_data(
        source_id,
        proposal_id="PROP-20260712-A1B2C3D4",
        operation_type="SUPERSEDE",
        kind="concept",
        target_id="CON-9100",
        draft_path=old_draft,
        status="approved",
        expected_revision=1,
        expected_target_sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
    )
    operations = data["operations"]
    assert isinstance(operations, list)
    operations.append(
        {
            "operation_id": "OP-002",
            "type": "CREATE",
            "kind": "concept",
            "title": "Replacement concept",
            "reason": "이전 지식을 명시적으로 대체한다.",
            "evidence": [
                {
                    "source_id": source_id,
                    "locator": {"type": "line_range", "start": 1, "end": 2},
                }
            ],
            "confidence": "high",
            "draft_path": str(new_draft),
        }
    )
    proposal = Proposal.model_validate(data)

    ProposalApplyService(vault, repository).apply(proposal, proposal_path)

    assert "status: superseded" in target.read_text(encoding="utf-8")
    assert (vault / "projects/amplai/10-concepts/CON-9101-concurrency-target.md").exists()
