from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from amplai_foundry.roadmaps.models import (
    RoadmapChange,
    RoadmapChangeProposal,
    RoadmapChangeType,
    RoadmapDefinition,
    RoadmapImpactAnalysis,
    RoadmapPhase,
    RoadmapPhaseStatus,
    RoadmapProposalStatus,
)
from amplai_foundry.roadmaps.proposals import (
    RoadmapProposalRepository,
    RoadmapProposalRepositoryError,
)
from amplai_foundry.roadmaps.repository import RoadmapRepository
from amplai_foundry.roadmaps.service import RoadmapService

NOW = datetime(2026, 7, 28, 10, 0, tzinfo=ZoneInfo("Asia/Seoul"))


def _phase(
    identifier: str,
    order: int,
    *,
    status: str = "planned",
    depends_on: list[str] | None = None,
) -> RoadmapPhase:
    return RoadmapPhase(
        id=identifier,
        order=order,
        status=status,
        depends_on=depends_on or [],
        definition_of_done=[f"{identifier} done"],
    )


def _current() -> RoadmapDefinition:
    return RoadmapDefinition(
        roadmap_id="amplai-roadmap",
        version=3,
        status="approved",
        updated_at=date(2026, 7, 27),
        current_focus="phase-1",
        phases=[
            _phase("phase-0", 0, status="completed"),
            _phase("phase-1", 10, depends_on=["phase-0"]),
            _phase("phase-2", 20, depends_on=["phase-1"]),
        ],
    )


def _save(path: Path, roadmap: RoadmapDefinition) -> RoadmapRepository:
    repository = RoadmapRepository(path)
    repository._save_legacy_fixture(roadmap)
    return repository


def test_roadmap_diff_preserves_cancelled_items_and_rebuilds_next_plan(
    tmp_path: Path,
) -> None:
    current = _current()
    desired = RoadmapDefinition(
        roadmap_id=current.roadmap_id,
        version=4,
        status="approved",
        updated_at=date(2026, 7, 28),
        current_focus="phase-3",
        phases=[
            _phase("phase-0", 0, status="completed"),
            _phase("phase-1", 30, status="completed", depends_on=["phase-0"]),
            _phase("phase-3", 40, depends_on=["phase-1"]),
        ],
    )
    service = RoadmapService()

    proposal = service.propose_change(
        current,
        desired,
        created_at=NOW,
        created_by="tester",
    )

    change_types = {(item.item_id, item.type) for item in proposal.changes}
    assert ("phase-3", RoadmapChangeType.ADD) in change_types
    assert ("phase-2", RoadmapChangeType.CANCEL) in change_types
    assert ("phase-1", RoadmapChangeType.MOVE) in change_types
    assert ("phase-1", RoadmapChangeType.UPDATE) in change_types
    assert proposal.impact.requires_replan
    assert proposal.impact.current_focus_affected

    approved = service._approve_legacy_proposal(proposal, approved_by="reviewer", approved_at=NOW)
    repository = _save(tmp_path / "roadmap.yaml", current)
    updated = service._apply_legacy_proposal(repository, approved, applied_on=date(2026, 7, 28))

    by_id = {phase.id: phase for phase in updated.phases}
    assert updated.version == 4
    assert by_id["phase-2"].status == "cancelled"
    assert by_id["phase-1"].order == 30
    assert updated.current_focus == "phase-3"
    assert updated.approved_at == NOW.date()
    assert updated.approved_by == "reviewer"
    assert approved.status is RoadmapProposalStatus.APPLIED
    assert repository.load() == updated


def test_roadmap_apply_requires_approval_and_rejects_stale_revision(tmp_path: Path) -> None:
    current = _current()
    desired = current.model_copy(
        update={
            "version": 4,
            "phases": [
                current.phases[0],
                current.phases[1].model_copy(update={"status": RoadmapPhaseStatus.COMPLETED}),
                current.phases[2],
            ],
        }
    )
    service = RoadmapService()
    proposal = service.propose_change(current, desired, created_at=NOW, created_by="tester")
    repository = _save(tmp_path / "roadmap.yaml", current)

    with pytest.raises(ValueError, match="approved"):
        service._apply_legacy_proposal(repository, proposal, applied_on=date(2026, 7, 28))

    approved = service._approve_legacy_proposal(proposal, approved_by="reviewer", approved_at=NOW)
    repository._save_legacy_fixture(current.model_copy(update={"version": 4}))
    with pytest.raises(ValueError, match="stale roadmap revision"):
        service._apply_legacy_proposal(repository, approved, applied_on=date(2026, 7, 28))


def test_concurrent_roadmap_apply_has_one_winner_and_one_stale_revision(
    tmp_path: Path,
) -> None:
    current = _current()
    service = RoadmapService()
    desired_a = current.model_copy(
        update={
            "version": 4,
            "phases": [
                current.phases[0],
                current.phases[1].model_copy(update={"status": RoadmapPhaseStatus.IN_PROGRESS}),
                current.phases[2],
            ],
        }
    )
    desired_b = current.model_copy(
        update={
            "version": 4,
            "phases": [
                current.phases[0],
                current.phases[1].model_copy(update={"status": RoadmapPhaseStatus.COMPLETED}),
                current.phases[2],
            ],
        }
    )
    proposals = [
        service._approve_legacy_proposal(
            service.propose_change(current, desired, created_at=NOW, created_by="tester"),
            approved_by="reviewer",
            approved_at=NOW,
        )
        for desired in (desired_a, desired_b)
    ]
    repository = _save(tmp_path / "roadmap.yaml", current)

    def apply(proposal: RoadmapChangeProposal) -> str:
        try:
            service._apply_legacy_proposal(repository, proposal, applied_on=NOW.date())
        except ValueError as error:
            return str(error)
        return "applied"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(apply, proposals))

    assert outcomes.count("applied") == 1
    assert sum("stale roadmap revision" in outcome for outcome in outcomes) == 1
    assert repository.load().version == 4


def test_roadmap_apply_rolls_back_if_proposal_audit_cannot_be_saved(
    tmp_path: Path,
) -> None:
    current = _current()
    desired = current.model_copy(
        update={
            "version": 4,
            "phases": [
                current.phases[0],
                current.phases[1].model_copy(update={"status": RoadmapPhaseStatus.COMPLETED}),
                current.phases[2],
            ],
        }
    )
    service = RoadmapService()
    approved = service._approve_legacy_proposal(
        service.propose_change(current, desired, created_at=NOW, created_by="tester"),
        approved_by="reviewer",
        approved_at=NOW,
    )
    roadmap_repository = _save(tmp_path / "roadmap.yaml", current)

    class FailingRepository(RoadmapProposalRepository):
        def _save_legacy_fixture(self, proposal: RoadmapChangeProposal) -> Path:
            raise RoadmapProposalRepositoryError("simulated audit failure")

    with pytest.raises(RoadmapProposalRepositoryError, match="simulated"):
        service._apply_legacy_proposal(
            roadmap_repository,
            approved,
            applied_on=NOW.date(),
            proposal_repository=FailingRepository(tmp_path / "proposals"),
        )

    assert roadmap_repository.load() == current
    assert approved.status is RoadmapProposalStatus.APPROVED


def test_roadmap_rejects_dependency_cycles_and_cross_roadmap_apply(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="dependency cycle"):
        RoadmapDefinition(
            roadmap_id="cyclic-roadmap",
            version=1,
            status="draft",
            updated_at=date(2026, 7, 28),
            phases=[
                _phase("phase-a", 10, depends_on=["phase-b"]),
                _phase("phase-b", 20, depends_on=["phase-a"]),
            ],
        )

    current = _current()
    desired = current.model_copy(
        update={
            "version": 4,
            "phases": [
                current.phases[0],
                current.phases[1].model_copy(update={"status": RoadmapPhaseStatus.COMPLETED}),
                current.phases[2],
            ],
        }
    )
    service = RoadmapService()
    proposal = service.propose_change(current, desired, created_at=NOW, created_by="tester")
    approved = service._approve_legacy_proposal(proposal, approved_by="reviewer", approved_at=NOW)
    wrong_roadmap = approved.model_copy(update={"roadmap_id": "another-roadmap"})
    repository = _save(tmp_path / "roadmap.yaml", current)

    with pytest.raises(ValueError, match="identity mismatch"):
        service._apply_legacy_proposal(repository, wrong_roadmap, applied_on=date(2026, 7, 28))


def test_roadmap_rejects_unknown_phase_status() -> None:
    with pytest.raises(ValueError):
        _phase("phase-typo", 99, status="compeleted")


def test_roadmap_supersede_is_versioned_and_reviewed(tmp_path: Path) -> None:
    current = _current()
    replacement = {
        **current.phases[1].model_dump(mode="json"),
        "status": "superseded",
    }
    proposal = RoadmapChangeProposal(
        proposal_id="RMAP-20260728-AABBCCDD",
        roadmap_id=current.roadmap_id,
        base_version=3,
        proposed_version=4,
        created_at=NOW,
        created_by="tester",
        changes=[
            RoadmapChange(
                item_id="phase-1",
                type=RoadmapChangeType.SUPERSEDE,
                before=current.phases[1].model_dump(mode="json"),
                after=replacement,
                reason="A replacement phase owns the revised contract.",
            )
        ],
        impact=RoadmapImpactAnalysis(
            changed_items=["phase-1"],
            directly_dependent_items=["phase-2"],
            transitive_dependent_items=[],
            current_focus_affected=True,
            requires_replan=True,
        ),
    )
    service = RoadmapService()
    approved = service._approve_legacy_proposal(proposal, approved_by="reviewer", approved_at=NOW)
    repository = _save(tmp_path / "roadmap.yaml", current)

    updated = service._apply_legacy_proposal(repository, approved, applied_on=date(2026, 7, 28))

    assert next(item for item in updated.phases if item.id == "phase-1").status == "superseded"
    assert updated.current_focus is None


def test_roadmap_markdown_analysis_holds_unknown_structure() -> None:
    report = RoadmapService.analyze_markdown(
        _current(),
        "# Roadmap\n\n## Phase 99Z — Surprise\n\nUnknown work.",
    )

    assert report.disposition == "review_required"
    assert report.unmatched_phase_labels == ["99Z"]


def test_empty_approvers_are_rejected() -> None:
    current = _current()
    desired = current.model_copy(
        update={
            "version": 4,
            "phases": [
                current.phases[0],
                current.phases[1].model_copy(update={"status": RoadmapPhaseStatus.COMPLETED}),
                current.phases[2],
            ],
        }
    )
    proposal = RoadmapService().propose_change(
        current,
        desired,
        created_at=NOW,
        created_by="tester",
    )

    with pytest.raises(ValueError):
        RoadmapService._approve_legacy_proposal(proposal, approved_by="", approved_at=NOW)


def test_roadmap_proposal_repository_rejects_path_traversal(tmp_path: Path) -> None:
    repository = RoadmapProposalRepository(tmp_path)

    with pytest.raises(RoadmapProposalRepositoryError, match="ID 형식"):
        repository.get("../../outside")


def test_roadmap_proposal_repository_rejects_direct_lifecycle_write(tmp_path: Path) -> None:
    current = _current()
    desired = current.model_copy(
        update={
            "version": current.version + 1,
            "phases": [
                current.phases[0],
                current.phases[1].model_copy(update={"status": RoadmapPhaseStatus.COMPLETED}),
                *current.phases[2:],
            ],
        }
    )
    proposal = RoadmapService().propose_change(
        current,
        desired,
        created_at=NOW,
        created_by="tester",
    )
    repository = RoadmapProposalRepository(tmp_path)
    repository.save(proposal)
    approved = RoadmapService._approve_legacy_proposal(
        proposal,
        approved_by="caller",
        approved_at=NOW,
    )

    with pytest.raises(RoadmapProposalRepositoryError, match="DIRECT_MUTATION_DISABLED"):
        repository.save(approved)

    stored = repository.get(proposal.proposal_id)
    assert stored is not None and stored.status is RoadmapProposalStatus.DRAFT


def test_markdown_adapter_derives_explicit_changes_without_mutating_state() -> None:
    current = _current()
    service = RoadmapService()

    status_report = service.propose_from_markdown(
        current,
        "## Phase 1\n\n**상태:** 완료\n",
        created_at=NOW,
        created_by="tester",
    )
    add_report = service.propose_from_markdown(
        current,
        "## Phase 99\n\nStatus: planned\nOrder: 99\n",
        created_at=NOW,
        created_by="tester",
    )
    negative_report = service.propose_from_markdown(
        current,
        "## Phase 1\n\n상태: 미완료\n",
        created_at=NOW,
        created_by="tester",
    )

    assert status_report.change_proposal is not None
    assert status_report.change_proposal.changes[0].type is RoadmapChangeType.UPDATE
    assert status_report.change_proposal.changes[0].after is not None
    assert status_report.change_proposal.changes[0].after["status"] == "completed"
    assert add_report.change_proposal is not None
    assert add_report.change_proposal.changes[0].type is RoadmapChangeType.ADD
    assert negative_report.change_proposal is None
    assert negative_report.reason_codes == ["STRUCTURAL_DIFF_NOT_DERIVED"]
    assert current == _current()
