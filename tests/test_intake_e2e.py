import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from typer.testing import CliRunner

from amplai_foundry.cli import app
from amplai_foundry.domain.identity import MemoryRef, ProjectRef
from amplai_foundry.domain.semantic import (
    ClaimModality,
    SemanticDescriptor,
    semantic_signature,
)
from amplai_foundry.ingestion.service import SourceIngestionService
from amplai_foundry.intake.models import (
    ArtifactRef,
    AuthorityContext,
    AuthorityKind,
    IntentRequest,
)
from amplai_foundry.intake.service import KnowledgeIntakeService
from amplai_foundry.projects.repository import ProjectPackRepository
from amplai_foundry.proposals.apply import ProposalApplyService, approve_proposal
from amplai_foundry.proposals.repository import ProposalRepository
from amplai_foundry.proposals.validation import ProposalValidator
from amplai_foundry.repositories.markdown import MarkdownMemoryRepository
from amplai_foundry.roadmaps.models import RoadmapPhaseStatus, RoadmapProposalStatus
from amplai_foundry.roadmaps.proposals import RoadmapProposalRepository
from amplai_foundry.roadmaps.repository import RoadmapRepository
from amplai_foundry.semantics.models import ComparisonRelation, SemanticAnchor
from amplai_foundry.semantics.repository import SemanticAnchorRepository

NOW = datetime(2026, 7, 28, 9, 30, tzinfo=ZoneInfo("Asia/Seoul"))
RUNNER = CliRunner()


def _pack(
    root: Path,
    *,
    project_id: str = "amplai",
    default: bool = True,
) -> None:
    (root / ".amplai").mkdir(parents=True)
    (root / "memory").mkdir()
    (root / ".amplai/project.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "project_id": project_id,
                "namespace": f"org/pinesky/project/{project_id}",
                "name": project_id.upper(),
                "aliases": [project_id],
                "default": default,
                "imports": [],
                "canonical": {"memory": "memory"},
                "runtime": {"root": ".amplai/runtime"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (root / ".amplai/domain.lock.yaml").write_text(
        "schema_version: 1\nimports: []\n",
        encoding="utf-8",
    )


def _roadmap_state(root: Path) -> None:
    plans = root / "plans"
    plans.mkdir()
    (plans / "amplai-master-roadmap.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "roadmap_id": "amplai-master-roadmap",
                "version": 1,
                "status": "approved",
                "updated_at": "2026-07-28",
                "current_focus": "phase-1a-identity",
                "phases": [
                    {
                        "id": "phase-1a-identity",
                        "order": 10,
                        "status": "planned",
                        "depends_on": [],
                        "definition_of_done": ["qualified identity"],
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    manifest_path = root / ".amplai/project.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["canonical"]["roadmap"] = "plans/amplai-master-roadmap.yaml"
    manifest_path.write_text(
        yaml.safe_dump(manifest, sort_keys=False),
        encoding="utf-8",
    )


def _request(
    path: str,
    *,
    instruction: str = "이 로드맵을 AMPLAI에 반영해줘",
    title: str = "AMPLAI Phase 1 Roadmap",
) -> IntentRequest:
    return IntentRequest(
        instruction=instruction,
        artifacts=[
            ArtifactRef(
                path=path,
                source_type="roadmap",
                title=title,
            )
        ],
        authority=AuthorityContext(
            kind=AuthorityKind.USER,
            actor="tester",
            can_approve_authoritative=False,
        ),
    )


def _service(root: Path) -> KnowledgeIntakeService:
    return KnowledgeIntakeService(
        root,
        now=lambda: NOW,
        monotonic_ns=lambda: 0,
    )


def test_roadmap_intake_is_review_only_reproducible_and_idempotent(tmp_path: Path) -> None:
    _pack(tmp_path)
    _roadmap_state(tmp_path)
    artifact = tmp_path / "incoming-roadmap.md"
    artifact.write_text(
        "# Roadmap\n\n## Phase 1A\n\n"
        "Tracker 진행 상태를 동기화하고 add, update, delete와 revision, impact를 처리한다.\n",
        encoding="utf-8",
    )
    service = _service(tmp_path)

    first = service.process(_request(artifact.name))

    assert first.status == "prepared"
    assert first.source is not None and first.source.status == "created"
    assert first.classification is not None
    assert [item.relation for item in first.comparisons] == [ComparisonRelation.NEW]
    assert first.proposal_id is not None
    assert first.intake_run_path is not None
    assert first.roadmap_update_path is not None
    assert first.validation.evaluation.fixture_id == "roadmap-intake-v1"
    assert not first.validation.evaluation.unsafe_change_applied
    assert first.validation.evaluation.token_usage == 0
    assert not list((tmp_path / "memory").glob("10-concepts/*.md"))
    assert not list((tmp_path / "memory").glob("40-architecture/*.md"))
    assert len(list((tmp_path / "memory/00-sources").glob("*.md"))) == 1
    assert len(list((tmp_path / ".amplai/proposals").glob("*/proposal.yaml"))) == 1

    second = service.process(_request(artifact.name))

    assert second.proposal_id == first.proposal_id
    assert second.intake_run_path == first.intake_run_path
    assert len(list((tmp_path / "memory/00-sources").glob("*.md"))) == 1
    assert len(list((tmp_path / ".amplai/intake/runs").glob("*.yaml"))) == 1
    assert len(list((tmp_path / ".amplai/proposals").glob("*/proposal.yaml"))) == 1


def test_concurrent_identical_intake_is_one_reproducible_run(tmp_path: Path) -> None:
    _pack(tmp_path)
    artifact = tmp_path / "roadmap.md"
    artifact.write_text(
        "# Roadmap\n\n## Phase 1A\n\nRoadmap tracker add revision.\n",
        encoding="utf-8",
    )
    request = _request(artifact.name)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _index: _service(tmp_path).process(request),
                range(2),
            )
        )

    assert [result.status for result in results] == ["prepared", "prepared"]
    assert results[0].proposal_id == results[1].proposal_id
    assert results[0].intake_run_path == results[1].intake_run_path
    assert len(list((tmp_path / "memory/00-sources").glob("*.md"))) == 1
    assert len(list((tmp_path / ".amplai/proposals").glob("*/proposal.yaml"))) == 1
    assert len(list((tmp_path / ".amplai/intake/runs").glob("*.yaml"))) == 1


def test_normalized_duplicate_uses_stored_source_for_evidence_validation(
    tmp_path: Path,
) -> None:
    _pack(tmp_path)
    original = b"# Roadmap\n\n## Phase 1A\n\nRoadmap tracker add revision.\n"
    project = ProjectRef(
        project_id="amplai",
        namespace="org/pinesky/project/amplai",
    )
    SourceIngestionService().ingest_project(
        original,
        project=project,
        project_root_path=tmp_path / "memory",
        source_type="roadmap",
        title="AMPLAI Phase 1 Roadmap",
        now=NOW,
    )
    artifact = tmp_path / "normalized-duplicate.md"
    artifact.write_bytes(original + b"\n\n\n")

    result = _service(tmp_path).process(_request(artifact.name))

    assert result.source is not None and result.source.status == "duplicate"
    assert result.proposal_id is not None
    assert result.validation.passed
    proposal = ProposalRepository(tmp_path / ".amplai/proposals").get(result.proposal_id)
    assert proposal is not None
    proposal_path = ProposalRepository(tmp_path / ".amplai/proposals").path_for(result.proposal_id)
    assert not ProposalValidator(tmp_path / "memory").validate(proposal, proposal_path)


def test_paraphrased_roadmap_reuses_semantic_anchor_without_new_proposal(
    tmp_path: Path,
) -> None:
    _pack(tmp_path)
    _roadmap_state(tmp_path)
    first_artifact = tmp_path / "roadmap-a.md"
    second_artifact = tmp_path / "roadmap-b.md"
    first_artifact.write_text(
        "# Roadmap\n\n## Phase 1A\n\n로드맵 tracker의 add와 revision을 동기화한다.\n",
        encoding="utf-8",
    )
    second_artifact.write_text(
        "# 로드맵\n\n## Phase 1A\n\n진행 현황에 추가 사항과 version 이력을 맞춘다.\n",
        encoding="utf-8",
    )
    service = _service(tmp_path)

    first = service.process(_request(first_artifact.name))
    second = service.process(_request(second_artifact.name))

    assert first.proposal_id is not None
    assert [item.relation for item in second.comparisons] == [ComparisonRelation.EXACT_DUPLICATE]
    assert second.proposal_id is None
    assert second.validation.evaluation.proposal_reproducible
    assert len(list((tmp_path / ".amplai/proposals").glob("*/proposal.yaml"))) == 1
    assert len(list((tmp_path / "memory/00-sources").glob("*.md"))) == 2


def test_unresolved_project_holds_before_source_registration(tmp_path: Path) -> None:
    _pack(tmp_path / "amplai", project_id="amplai", default=False)
    _pack(tmp_path / "cortex", project_id="cortex", default=False)
    artifact = tmp_path / "unscoped-roadmap.md"
    artifact.write_text("# Roadmap\n\n## Phase 1\n\nMilestone.\n", encoding="utf-8")

    result = _service(tmp_path).process(_request(artifact.name, instruction="이 로드맵을 반영해줘"))

    assert result.status == "hold"
    assert result.project is None
    assert result.source is None
    assert result.validation.evaluation.project_resolution == "hold"
    assert result.intake_run_path is not None
    hold_path = tmp_path / result.intake_run_path
    assert hold_path.is_file()
    hold = yaml.safe_load(hold_path.read_text(encoding="utf-8"))
    assert hold["validation"]["evaluation"]["failure_category"] == "PROJECT_UNRESOLVED"
    assert "artifacts" not in hold
    assert not list(tmp_path.glob("**/00-sources/*.md"))


def test_ambiguous_artifact_preserves_source_but_creates_no_proposal(tmp_path: Path) -> None:
    _pack(tmp_path)
    artifact = tmp_path / "notes.md"
    artifact.write_text("# 메모\n\n짧은 자유 형식 메모다.\n", encoding="utf-8")

    result = _service(tmp_path).process(
        _request(
            artifact.name,
            instruction="이 파일을 반영해줘",
            title="자유 형식 메모",
        )
    )

    assert result.status == "hold"
    assert result.source is not None and result.source.status == "created"
    assert result.classification is not None and result.classification.held
    assert not result.candidates
    assert result.proposal_id is None
    assert result.validation.evaluation.classification == "hold"
    assert result.validation.evaluation.failure_category == "AMBIGUOUS_CLASSIFICATION"
    assert len(list((tmp_path / "memory/00-sources").glob("*.md"))) == 1
    assert not list((tmp_path / ".amplai/proposals").glob("*/proposal.yaml"))


def test_invalid_project_pack_holds_before_source_or_canonical_change(
    tmp_path: Path,
) -> None:
    _pack(tmp_path)
    (tmp_path / "memory/broken.md").write_text(
        "not a canonical memory document\n",
        encoding="utf-8",
    )
    artifact = tmp_path / "roadmap.md"
    artifact.write_text(
        "# Roadmap\n\n## Phase 1A\n\nRoadmap tracker add revision.\n",
        encoding="utf-8",
    )

    result = _service(tmp_path).process(_request(artifact.name))

    assert result.status == "hold"
    assert result.source is None
    assert result.validation.evaluation.failure_category == "PROJECT_PACK_INVALID"
    assert not list((tmp_path / "memory/00-sources").glob("*.md"))


def test_classifier_requires_artifact_content_grounding(tmp_path: Path) -> None:
    _pack(tmp_path)
    artifact = tmp_path / "generic.md"
    artifact.write_text("hello world\n", encoding="utf-8")

    result = _service(tmp_path).process(
        _request(
            artifact.name,
            instruction="please adopt roadmap phase one",
            title="Generic note",
        )
    )

    assert result.status == "hold"
    assert result.classification is not None
    assert result.classification.reason_codes == ["NO_CONTENT_GROUNDING"]
    assert not result.candidates


def test_non_roadmap_contradictory_statements_never_collapse_as_duplicates(
    tmp_path: Path,
) -> None:
    _pack(tmp_path)
    first_artifact = tmp_path / "architecture-a.md"
    second_artifact = tmp_path / "architecture-b.md"
    first_artifact.write_text(
        "# Architecture Proposal\n\nThe architecture must use PostgreSQL.\n",
        encoding="utf-8",
    )
    second_artifact.write_text(
        "# Architecture Proposal\n\nThe architecture must use DynamoDB.\n",
        encoding="utf-8",
    )

    def request(path: Path) -> IntentRequest:
        return IntentRequest(
            instruction="이 architecture proposal을 AMPLAI에 반영해줘",
            artifacts=[
                ArtifactRef(
                    path=path.name,
                    source_type="architecture_proposal",
                    title="Storage Architecture Proposal",
                )
            ],
            authority=AuthorityContext(kind=AuthorityKind.USER, actor="tester"),
        )

    service = _service(tmp_path)
    first = service.process(request(first_artifact))
    second = service.process(request(second_artifact))

    assert first.comparisons[0].relation is ComparisonRelation.NEW
    assert second.comparisons[0].relation is ComparisonRelation.UNCERTAIN
    assert second.status == "hold"
    assert second.validation.evaluation.failure_category == "POLICY_HOLD"


def test_conflicting_semantic_candidate_is_held_by_policy(tmp_path: Path) -> None:
    _pack(tmp_path)
    _roadmap_state(tmp_path)
    artifact = tmp_path / "roadmap.md"
    content = "# Roadmap\n\n## Phase 1A\n\nStatus: completed\n\nRoadmap tracker add revision.\n"
    instruction = "이 로드맵을 AMPLAI에 반영해줘"
    title = "AMPLAI Phase 1 Roadmap"
    artifact.write_text(content, encoding="utf-8")
    service = _service(tmp_path)
    project = ProjectRef(
        project_id="amplai",
        namespace="org/pinesky/project/amplai",
    )
    classification = service.classifier.classify(
        content,
        title=title,
        instruction=instruction,
    )
    candidate = service.extractor.extract(
        project=project,
        source=MemoryRef(
            namespace=project.namespace,
            local_id="SRC-20260728-AAAAAAAA",
        ),
        instruction=instruction,
        content=content,
        title=title,
        classification=classification,
    )[0]
    conflicting_claim = candidate.claim.model_copy(update={"modality": ClaimModality.MUST_NOT})
    descriptor = SemanticDescriptor(
        signature=semantic_signature(
            candidate.scope,
            conflicting_claim,
            candidate.constraints,
        ),
        scope=candidate.scope,
        claim=conflicting_claim,
        constraints=candidate.constraints,
        canonical_statement="Roadmap tracker synchronization must not happen.",
    )
    SemanticAnchorRepository(tmp_path / ".amplai/semantic/anchors.yaml").save(
        [
            SemanticAnchor(
                semantic_id="SEM-AAAAAAAAAAAA",
                project=project,
                descriptor=descriptor,
            )
        ]
    )

    request = _request(artifact.name).model_copy(
        update={
            "authority": AuthorityContext(
                kind=AuthorityKind.USER,
                actor="tester",
                can_auto_apply_low_risk=True,
            )
        }
    )
    result = service.process(request)

    assert result.status == "hold"
    assert [item.relation for item in result.comparisons] == [ComparisonRelation.CONFLICTS]
    assert result.validation.evaluation.failure_category == "POLICY_HOLD"
    assert result.validation.evaluation.proposal_reproducible
    assert not result.validation.evaluation.safe_change_applied
    roadmap = RoadmapRepository(tmp_path / "plans/amplai-master-roadmap.yaml").load()
    assert roadmap.version == 1
    assert roadmap.phases[0].status is RoadmapPhaseStatus.PLANNED


def test_nested_pack_proposal_remains_portable_and_applies_only_after_approval(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    pack_root = workspace / "products/amplai"
    _pack(pack_root)
    artifact = pack_root / "architecture.md"
    artifact.write_text(
        "# Architecture Proposal\n\n"
        "The architecture proposal defines a portable review boundary.\n",
        encoding="utf-8",
    )
    request = IntentRequest(
        instruction="이 architecture proposal을 AMPLAI에 반영해줘",
        artifacts=[ArtifactRef(path=str(artifact), title="Architecture Proposal")],
        authority=AuthorityContext(kind=AuthorityKind.USER, actor="tester"),
    )

    result = _service(workspace).process(request)

    assert result.proposal_id is not None
    pack = ProjectPackRepository(workspace).get("amplai")
    assert pack is not None
    proposals = ProposalRepository(pack.proposal_root)
    proposal_path = proposals.path_for(result.proposal_id)
    proposal = proposals.get(result.proposal_id)
    assert proposal is not None
    assert proposal.operations[0].draft_path is not None
    assert proposal.operations[0].draft_path.startswith("drafts/")
    assert not ProposalValidator(pack.memory_root).validate(proposal, proposal_path)

    approved = approve_proposal(proposal, approved_by="reviewer", now=NOW)
    proposals.save(approved)
    applied = ProposalApplyService(pack.memory_root, proposals).apply(
        approved,
        proposal_path,
    )

    assert applied.touched_paths
    assert len(list(pack.memory_root.glob("40-architecture/*.md"))) == 1


def test_roadmap_intake_proposal_can_be_shown_approved_and_applied_by_cli(
    tmp_path: Path,
) -> None:
    _pack(tmp_path)
    _roadmap_state(tmp_path)
    artifact = tmp_path / "phase-status.md"
    artifact.write_text(
        "# Roadmap\n\n## Phase 1A\n\n**Status:** completed\n",
        encoding="utf-8",
    )

    result = _service(tmp_path).process(_request(artifact.name))

    proposals = RoadmapProposalRepository(tmp_path / ".amplai/intake/roadmap-proposals")
    stored = proposals.list()
    assert result.roadmap_update_path is not None
    assert len(stored) == 1
    assert stored[0].status is RoadmapProposalStatus.DRAFT
    common = [
        stored[0].proposal_id,
        "--proposal-root",
        str(proposals.root),
    ]
    shown = RUNNER.invoke(app, ["roadmap", "show", *common])
    approved = RUNNER.invoke(
        app,
        ["roadmap", "approve", *common, "--approved-by", "reviewer"],
    )
    applied = RUNNER.invoke(
        app,
        [
            "roadmap",
            "apply",
            *common,
            "--roadmap",
            str(tmp_path / "plans/amplai-master-roadmap.yaml"),
        ],
    )

    assert shown.exit_code == 0
    assert approved.exit_code == 0
    assert applied.exit_code == 0
    roadmap = RoadmapRepository(tmp_path / "plans/amplai-master-roadmap.yaml").load()
    assert roadmap.version == 2
    assert roadmap.applied_proposal_id == stored[0].proposal_id
    assert roadmap.phases[0].status is RoadmapPhaseStatus.COMPLETED
    applied_proposal = proposals.get(stored[0].proposal_id)
    assert applied_proposal is not None
    assert applied_proposal.status is RoadmapProposalStatus.APPLIED


def test_policy_can_auto_apply_only_safe_tracker_status(tmp_path: Path) -> None:
    _pack(tmp_path)
    _roadmap_state(tmp_path)
    artifact = tmp_path / "safe-status.md"
    artifact.write_text(
        "# Roadmap\n\n## Phase 1A\n\n상태: 완료\n",
        encoding="utf-8",
    )
    request = _request(artifact.name).model_copy(
        update={
            "authority": AuthorityContext(
                kind=AuthorityKind.USER,
                actor="tester",
                can_auto_apply_low_risk=True,
            )
        }
    )

    result = _service(tmp_path).process(request)

    assert result.status == "prepared"
    assert result.validation.evaluation.safe_change_applied
    roadmap = RoadmapRepository(tmp_path / "plans/amplai-master-roadmap.yaml").load()
    assert roadmap.version == 2
    proposal = RoadmapProposalRepository(tmp_path / ".amplai/intake/roadmap-proposals").list()[0]
    assert proposal.status is RoadmapProposalStatus.APPLIED
    assert proposal.approved_by == "policy:safe-tracker:tester"


def test_policy_auto_links_independent_duplicate_evidence_without_meaning_change(
    tmp_path: Path,
) -> None:
    _pack(tmp_path)
    project = ProjectRef(
        project_id="amplai",
        namespace="org/pinesky/project/amplai",
    )
    base_content = b"# Roadmap\n\n## Phase 1A\n\nRoadmap tracker add revision.\n"
    base_source = SourceIngestionService().ingest_project(
        base_content,
        project=project,
        project_root_path=tmp_path / "memory",
        source_type="roadmap",
        title="Baseline roadmap evidence",
        now=NOW,
    )
    service = _service(tmp_path)
    instruction = "이 로드맵을 AMPLAI에 반영해줘"
    classification = service.classifier.classify(
        base_content.decode(),
        title="AMPLAI Roadmap",
        instruction=instruction,
    )
    candidate = service.extractor.extract(
        project=project,
        source=MemoryRef(
            namespace=project.namespace,
            local_id=base_source.source_id,
        ),
        instruction=instruction,
        content=base_content.decode(),
        title="AMPLAI Roadmap",
        classification=classification,
    )[0]
    target_path = tmp_path / "memory/40-architecture/ARC-0001.md"
    target_path.parent.mkdir(parents=True)
    target_metadata = {
        "schema_version": 1,
        "id": "ARC-0001",
        "namespace": project.namespace,
        "project": project.project_id,
        "kind": "architecture",
        "status": "active",
        "title": "Governed roadmap synchronization",
        "summary": candidate.canonical_statement,
        "created_at": "2026-07-28",
        "updated_at": "2026-07-28",
        "source_refs": [base_source.source_id],
        "relations": [],
        "revision": 1,
        "tags": ["roadmap"],
        "semantic": candidate.descriptor().model_dump(mode="json"),
    }
    target_path.write_text(
        "---\n"
        + yaml.safe_dump(target_metadata, sort_keys=False)
        + "---\n# Governed roadmap synchronization\n\n"
        + candidate.canonical_statement
        + "\n",
        encoding="utf-8",
    )
    SemanticAnchorRepository(tmp_path / ".amplai/semantic/anchors.yaml").save(
        [
            SemanticAnchor(
                semantic_id="SEM-BBBBBBBBBBBB",
                project=project,
                descriptor=candidate.descriptor(),
                target_refs=[MemoryRef(namespace=project.namespace, local_id="ARC-0001")],
                source_refs=[
                    MemoryRef(
                        namespace=project.namespace,
                        local_id=base_source.source_id,
                    )
                ],
            )
        ]
    )
    artifact = tmp_path / "independent-roadmap.md"
    artifact.write_text(
        "# Roadmap\n\n## Phase 1A\n\nTracker roadmap must add and preserve version history.\n",
        encoding="utf-8",
    )
    request = _request(artifact.name).model_copy(
        update={
            "authority": AuthorityContext(
                kind=AuthorityKind.USER,
                actor="tester",
                can_auto_apply_low_risk=True,
            )
        }
    )

    result = service.process(request)

    assert result.status == "prepared"
    assert result.comparisons[0].relation is ComparisonRelation.EXACT_DUPLICATE
    assert result.validation.evaluation.safe_change_applied
    updated = MarkdownMemoryRepository(tmp_path / "memory").get(
        "ARC-0001",
        namespace=project.namespace,
    )
    assert updated is not None
    assert updated.revision == 2
    assert updated.semantic == candidate.descriptor()
    assert len(updated.source_refs) == 2
    assert result.proposal_id is not None
    proposal = ProposalRepository(tmp_path / ".amplai/proposals").get(result.proposal_id)
    assert proposal is not None
    assert proposal.status.value == "applied"


def test_phase_one_cli_exposes_project_intake_and_roadmap_commands(tmp_path: Path) -> None:
    _pack(tmp_path)
    _roadmap_state(tmp_path)
    artifact = tmp_path / "roadmap.md"
    artifact.write_text(
        "# Roadmap\n\n## Phase 1A\n\nRoadmap tracker add revision.\n",
        encoding="utf-8",
    )

    projects = RUNNER.invoke(
        app,
        ["project", "list", "--workspace", str(tmp_path), "--json"],
    )
    intake = RUNNER.invoke(
        app,
        [
            "intake",
            "process",
            str(artifact),
            "--instruction",
            "이 로드맵을 AMPLAI에 반영해줘",
            "--workspace",
            str(tmp_path),
            "--json",
        ],
    )
    next_phase = RUNNER.invoke(
        app,
        [
            "roadmap",
            "next",
            "--path",
            str(tmp_path / "plans/amplai-master-roadmap.yaml"),
        ],
    )

    assert projects.exit_code == 0
    assert json.loads(projects.stdout)[0]["project_id"] == "amplai"
    assert intake.exit_code == 0
    assert json.loads(intake.stdout)[0]["status"] == "prepared"
    assert next_phase.exit_code == 0
    assert json.loads(next_phase.stdout)["id"] == "phase-1a-identity"
