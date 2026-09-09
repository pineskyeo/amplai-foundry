"""Tests for the host-edge Project Store bridge adapter."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from amplai_foundry.control_plane.models import OrchestrationRequest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from amplai_orchestration_bridge import (  # noqa: E402
    ProjectStoreOrchestrationGateway,
    ProjectStoreWorkActivationGateway,
    ProjectStoreWorkReader,
)
from amplai_runtime import ProjectStore  # noqa: E402


def request() -> OrchestrationRequest:
    return OrchestrationRequest(
        request_id="REQ-001",
        tenant_id="tenant-a",
        project_id="project-a",
        controller="work",
        goal="Implement one verified change",
        project_hint="project-a",
        target_app_hint="target",
        runner_hint="codex",
        artifact_refs=(),
        reply_route=None,
        correlation_id="cor-001",
        status="REQUESTED",
        hold_code=None,
        work_ref=None,
        created_at="2026-09-04T00:00:00Z",
        updated_at="2026-09-04T00:00:00Z",
    )


def git(command: list[str], cwd: Path) -> None:
    subprocess.run(["git", *command], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def project_store(tmp_path: Path) -> ProjectStore:
    repo = tmp_path / "target-repo"
    repo.mkdir()
    git(["init"], repo)
    git(["config", "user.email", "test@example.com"], repo)
    git(["config", "user.name", "Test"], repo)
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    git(["add", "README.md"], repo)
    git(["commit", "-m", "fixture"], repo)

    store = ProjectStore.initialize(str(tmp_path / "project"), "project-a", git_init=False)
    store.register_app("source", repo_path=str(repo))
    store.register_app(
        "target",
        repo_path=str(repo),
        runner_profiles={
            "claude-code": {"command": "claude", "args": []},
            "codex": {"command": "codex", "args": []},
        },
        default_runner_profile="claude-code",
    )
    store.register_contract(
        "hermes-slack-orchestration",
        "1",
        "orchestration_contract",
        ["source"],
        ["target"],
        "fixture://contract",
    )
    return store


def test_gateway_creates_one_draft_work_pinned_to_request(project_store: ProjectStore) -> None:
    gateway = ProjectStoreOrchestrationGateway(
        project_store,
        source_app="source",
        contract_ref="hermes-slack-orchestration@1",
    )
    first = gateway.ensure_draft_work(request())
    second = gateway.ensure_draft_work(request())

    assert first == second
    work = project_store.get_work(first)
    assert work["status"] == "DRAFT"
    assert work["request_ref"] == "REQ-001"
    assert work["base_ref"]
    assert project_store.get_change(work["change_id"])["status"] == "ACTIVE"


def test_gateway_rejects_a_control_plane_project_that_is_not_its_store(
    project_store: ProjectStore,
) -> None:
    from amplai_foundry.control_plane.orchestration import ProjectStoreResolutionHold

    mismatched = replace(request(), project_id="project-b")
    gateway = ProjectStoreOrchestrationGateway(
        project_store,
        source_app="source",
        contract_ref="hermes-slack-orchestration@1",
    )

    with pytest.raises(ProjectStoreResolutionHold, match="PROJECT_STORE_PROJECT_MISMATCH"):
        gateway.ensure_draft_work(mismatched)


def test_activation_gateway_compares_card_revision_and_digest_inside_store_lock(
    project_store: ProjectStore,
) -> None:
    work_id = ProjectStoreOrchestrationGateway(
        project_store,
        source_app="source",
        contract_ref="hermes-slack-orchestration@1",
    ).ensure_draft_work(request())
    gateway = ProjectStoreWorkActivationGateway(project_store)
    card = gateway.get(work_id)
    # A state transition after card rendering gives the activation a different
    # sealed digest and revision before it reaches the Store CAS mutation.
    project_store.activate_work(work_id, actor="other-human")
    with pytest.raises(Exception, match="WORK_STALE"):
        gateway.activate(
            work_id,
            actor_id="ACT-1",
            expected_revision=card.revision,
            expected_digest=card.digest,
        )


def test_work_reader_returns_terminal_result_evidence(project_store: ProjectStore) -> None:
    work_id = ProjectStoreOrchestrationGateway(
        project_store,
        source_app="source",
        contract_ref="hermes-slack-orchestration@1",
    ).ensure_draft_work(request())
    project_store.activate_work(work_id)
    claimed, token = project_store.claim_work(work_id, "reader-test")
    project_store.start_work(claimed["work_id"], token)
    evidence = project_store.add_evidence(
        claimed["change_id"], "test", "terminal evidence", "test", "fixture", work_id=work_id
    )
    project_store.complete_work(work_id, token, "done", [evidence["evidence_id"]])

    assert ProjectStoreWorkReader(project_store).get_work(work_id)["evidence_refs"] == [
        evidence["evidence_id"]
    ]
