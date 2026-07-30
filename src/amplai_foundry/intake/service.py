"""Governed Source → Candidate → Compare → Proposal intake orchestration."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import yaml

from amplai_foundry.domain.identity import MemoryRef, ProjectRef
from amplai_foundry.governance.authority import AuthorityResolutionError, AuthorityService
from amplai_foundry.governance.models import AuthorityPermission
from amplai_foundry.ingestion.hashing import content_sha256
from amplai_foundry.ingestion.service import (
    IngestionError,
    SourceIngestionService,
    extract_original_content,
)
from amplai_foundry.intake.classifier import ArtifactClassifier
from amplai_foundry.intake.extractor import CandidateExtractor
from amplai_foundry.intake.models import (
    ArtifactClassification,
    ArtifactKind,
    IntakeResult,
    IntakeRun,
    IntentRequest,
    MinimalEvaluationRecord,
    PolicyDecision,
    PolicyDisposition,
    ResolutionHoldRecord,
    SourceRecord,
    ValidationCheck,
    ValidationReport,
)
from amplai_foundry.intake.policy import IntakePolicy
from amplai_foundry.intake.proposal_builder import IntakeProposalBuilder
from amplai_foundry.intake.store import IntakeRunStore, ResolutionHoldStore
from amplai_foundry.projects.repository import ProjectPack, ProjectPackRepository
from amplai_foundry.projects.resolver import ProjectResolutionStatus, ProjectResolver
from amplai_foundry.projects.service import ProjectPackService
from amplai_foundry.proposals.validation import ProposalValidator
from amplai_foundry.repositories.markdown import MarkdownMemoryRepository
from amplai_foundry.roadmaps.proposals import RoadmapProposalRepository
from amplai_foundry.roadmaps.repository import RoadmapRepository
from amplai_foundry.roadmaps.service import RoadmapService
from amplai_foundry.semantics.comparison import SemanticComparator
from amplai_foundry.semantics.models import (
    ComparisonResult,
    KnowledgeCandidate,
    SemanticAnchor,
)
from amplai_foundry.semantics.repository import SemanticAnchorRepository


class KnowledgeIntakeError(RuntimeError):
    pass


class KnowledgeIntakeService:
    def __init__(
        self,
        workspace_root: Path,
        authority_service: AuthorityService,
        *,
        now: Callable[[], datetime] | None = None,
        monotonic_ns: Callable[[], int] | None = None,
    ) -> None:
        self.workspace_root = workspace_root.resolve()
        self.authority_service = authority_service
        self.pack_repository = ProjectPackRepository(self.workspace_root)
        self.now = now or (lambda: datetime.now(ZoneInfo("Asia/Seoul")))
        self.monotonic_ns = monotonic_ns or time.monotonic_ns
        self.classifier = ArtifactClassifier()
        self.extractor = CandidateExtractor()
        self.comparator = SemanticComparator()
        self.policy = IntakePolicy()
        self.proposal_builder = IntakeProposalBuilder()

    def process_batch(self, request: IntentRequest) -> list[IntakeResult]:
        artifact_paths = [
            (self.workspace_root / artifact.path).resolve()
            if not Path(artifact.path).is_absolute()
            else Path(artifact.path).resolve()
            for artifact in request.artifacts
        ]
        resolution = ProjectResolver(self.pack_repository).resolve(
            instruction=request.instruction,
            project_hint=request.project_hint,
            artifact_paths=artifact_paths,
        )
        if resolution.status is ProjectResolutionStatus.HOLD or resolution.project is None:
            return [
                self._resolution_hold(
                    request.model_copy(update={"artifacts": [artifact]}),
                    resolution.reason,
                )
                for artifact in request.artifacts
            ]
        pack = self.pack_repository.get(resolution.project.project_id)
        if pack is None:
            raise KnowledgeIntakeError("resolved Project Pack을 다시 찾을 수 없습니다.")
        pack_validation = ProjectPackService(self.pack_repository).validate(pack)
        if not pack_validation.valid:
            reason = "Project Pack validation failed: " + "; ".join(pack_validation.issues)
            return [
                self._pack_hold(
                    request.model_copy(update={"artifacts": [artifact]}),
                    pack.ref,
                    reason,
                )
                for artifact in request.artifacts
            ]
        try:
            authority = self.authority_service.authenticate(request.identity.for_project(pack.ref))
        except AuthorityResolutionError as error:
            raise KnowledgeIntakeError(error.code) from error
        if AuthorityPermission.PROPOSAL_SUBMIT_REVIEW not in authority.permissions:
            raise KnowledgeIntakeError("AUTHORITY_DENIED")
        results: list[IntakeResult] = []
        for artifact, path in zip(request.artifacts, artifact_paths, strict=True):
            single_request = request.model_copy(update={"artifacts": [artifact]})
            results.append(
                self._process_one(
                    single_request,
                    pack=pack,
                    artifact_path=path,
                    resolution_reason=resolution.reason,
                    actor_id=authority.actor_ref.actor_id,
                )
            )
        return results

    def process(self, request: IntentRequest) -> IntakeResult:
        if len(request.artifacts) != 1:
            raise KnowledgeIntakeError(
                "process는 artifact 한 건만 받습니다. process_batch를 사용하세요."
            )
        return self.process_batch(request)[0]

    def _process_one(
        self,
        request: IntentRequest,
        *,
        pack: ProjectPack,
        artifact_path: Path,
        resolution_reason: str,
        actor_id: str,
    ) -> IntakeResult:
        with self._intake_lock(pack):
            return self._process_one_locked(
                request,
                pack=pack,
                artifact_path=artifact_path,
                resolution_reason=resolution_reason,
                actor_id=actor_id,
            )

    def _process_one_locked(
        self,
        request: IntentRequest,
        *,
        pack: ProjectPack,
        artifact_path: Path,
        resolution_reason: str,
        actor_id: str,
    ) -> IntakeResult:
        started = self.monotonic_ns()
        artifact = request.artifacts[0]
        try:
            content_bytes = artifact_path.read_bytes()
            content = content_bytes.decode("utf-8")
        except (OSError, UnicodeError) as error:
            raise KnowledgeIntakeError(f"artifact를 읽을 수 없습니다: {error}") from error
        timestamp = self.now()
        ingestion = SourceIngestionService().ingest_project(
            content_bytes,
            project=pack.ref,
            project_root_path=pack.memory_root,
            source_type=artifact.source_type,
            title=artifact.title or artifact_path.stem,
            original_filename=artifact_path.name,
            media_type=artifact.media_type,
            created_by=actor_id,
            now=timestamp,
        )
        source_path = Path(ingestion.path)
        try:
            stored_content_bytes = extract_original_content(source_path)
            content = stored_content_bytes.decode("utf-8")
        except (IngestionError, UnicodeError) as error:
            raise KnowledgeIntakeError(
                f"등록된 Source 원문을 분석할 수 없습니다: {error}"
            ) from error
        source_ref = MemoryRef(namespace=pack.ref.namespace, local_id=ingestion.source_id)
        source_status: Literal["created", "duplicate"] = (
            "created" if ingestion.status == "created" else "duplicate"
        )
        source_record = SourceRecord(
            status=source_status,
            ref=source_ref,
            path=self._portable(pack, source_path),
            content_sha256=content_sha256(stored_content_bytes),
            duplicate_of=ingestion.duplicate_of,
        )
        run_id = self._run_id(pack.ref, request.instruction, source_record.content_sha256)
        store = IntakeRunStore(pack.intake_root)
        previous = store.get(run_id)
        if previous is not None:
            return self._result_from_run(
                previous,
                store.path_for(run_id),
                resolution_reason,
                pack,
            )

        classification = self.classifier.classify(
            content,
            title=artifact.title or artifact_path.stem,
            instruction=request.instruction,
        )
        candidates = self.extractor.extract(
            project=pack.ref,
            source=source_ref,
            instruction=request.instruction,
            content=content,
            title=artifact.title or artifact_path.stem,
            classification=classification,
        )
        anchors = self._anchors(pack, store)
        comparisons = []
        for candidate in candidates:
            comparison = self.comparator.compare(candidate, anchors)
            comparisons.append(comparison)
            if comparison.relation.value == "NEW":
                anchors.append(SemanticAnchor.from_candidate(candidate, provisional=True))
        decisions = [
            self.policy.decide(
                candidate,
                comparison,
                allow_auto_apply=False,
            )
            for candidate, comparison in zip(candidates, comparisons, strict=True)
        ]
        proposal, proposal_path = self.proposal_builder.build(
            pack=pack,
            candidates=candidates,
            comparisons=comparisons,
            created_at=timestamp,
            created_by=actor_id,
        )
        proposal_issues = (
            ProposalValidator(pack.memory_root).validate(proposal, proposal_path)
            if proposal is not None and proposal_path is not None
            else []
        )
        semantic_safe_change_applied = False
        roadmap_update_path, safe_change_applied = self._roadmap_report(
            pack=pack,
            run_id=run_id,
            classification=classification,
            content=content,
            created_at=timestamp,
            actor_id=actor_id,
        )
        pack_issues = list(ProjectPackService(self.pack_repository).validate(pack).issues)
        latency_ms = max(0, (self.monotonic_ns() - started) // 1_000_000)
        validation = self._validation(
            classification=classification,
            candidates=candidates,
            comparisons=comparisons,
            decisions=decisions,
            proposal_id=proposal.proposal_id if proposal else None,
            proposal_issues=[f"{issue.code}: {issue.message}" for issue in proposal_issues],
            pack_issues=pack_issues,
            safe_change_applied=(safe_change_applied or semantic_safe_change_applied),
            latency_ms=latency_ms,
        )
        persisted_artifact = artifact.model_copy(
            update={"path": self._portable_artifact(pack, artifact_path)}
        )
        persisted_request = request.model_copy(update={"artifacts": [persisted_artifact]})
        run = IntakeRun(
            run_id=run_id,
            created_at=timestamp,
            project=pack.ref,
            request=persisted_request,
            source=source_record,
            classification=classification,
            candidates=candidates,
            comparisons=comparisons,
            policy_decisions=decisions,
            proposal_id=proposal.proposal_id if proposal else None,
            roadmap_update_path=roadmap_update_path,
            validation=validation,
        )
        run_path = store.save(run)
        status: Literal["prepared", "hold"] = (
            "hold" if classification.held or not validation.passed else "prepared"
        )
        return IntakeResult(
            status=status,
            project=pack.ref,
            resolution_reason=resolution_reason,
            source=source_record,
            classification=classification,
            candidates=candidates,
            comparisons=comparisons,
            policy_decisions=decisions,
            proposal_id=run.proposal_id,
            intake_run_path=self._portable(pack, run_path),
            roadmap_update_path=roadmap_update_path,
            validation=validation,
        )

    @staticmethod
    @contextlib.contextmanager
    def _intake_lock(pack: ProjectPack) -> Iterator[None]:
        lock_path = pack.runtime_root / "locks/intake.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with lock_path.open("a+b") as handle:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    if handle.read(1) == b"":
                        handle.write(b"\0")
                        handle.flush()
                    handle.seek(0)
                    locking = vars(msvcrt)["locking"]
                    lock_mode = vars(msvcrt)["LK_LOCK"]
                    unlock_mode = vars(msvcrt)["LK_UNLCK"]
                    locking(handle.fileno(), lock_mode, 1)
                    try:
                        yield
                    finally:
                        handle.seek(0)
                        locking(handle.fileno(), unlock_mode, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    try:
                        yield
                    finally:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError as error:
            raise KnowledgeIntakeError(
                f"Project Pack intake lock을 사용할 수 없습니다: {error}"
            ) from error

    def _anchors(
        self,
        pack: ProjectPack,
        store: IntakeRunStore,
    ) -> list[SemanticAnchor]:
        path = pack.resolve_canonical("semantic") / "anchors.yaml"
        canonical = SemanticAnchorRepository(path).list(pack.ref)
        from_memory = SemanticAnchorRepository.from_memory(
            MarkdownMemoryRepository(pack.memory_root),
            pack.ref,
        )
        provisional = store.provisional_anchors(
            pack.ref,
            proposal_root=pack.proposal_root,
        )
        by_signature: dict[str, SemanticAnchor] = {}
        for anchor in [*provisional, *from_memory, *canonical]:
            existing = by_signature.get(anchor.descriptor.signature)
            if existing is not None:
                anchor = anchor.model_copy(
                    update={
                        "target_refs": sorted(
                            {*existing.target_refs, *anchor.target_refs},
                            key=lambda item: item.qualified,
                        ),
                        "source_refs": sorted(
                            {*existing.source_refs, *anchor.source_refs},
                            key=lambda item: item.qualified,
                        ),
                        "provisional": existing.provisional and anchor.provisional,
                    }
                )
            by_signature[anchor.descriptor.signature] = anchor
        return sorted(by_signature.values(), key=lambda item: item.semantic_id)

    def _roadmap_report(
        self,
        *,
        pack: ProjectPack,
        run_id: str,
        classification: ArtifactClassification,
        content: str,
        created_at: datetime,
        actor_id: str,
    ) -> tuple[str | None, bool]:
        if classification.kind is not ArtifactKind.ROADMAP:
            return None, False
        roadmap_path = pack.roadmap_path
        if roadmap_path is None or not roadmap_path.exists():
            return None, False
        roadmap = RoadmapRepository(roadmap_path).load()
        report = RoadmapService().propose_from_markdown(
            roadmap,
            content,
            created_at=created_at,
            created_by=actor_id,
        )
        if report.change_proposal is not None:
            proposal_repository = RoadmapProposalRepository(pack.roadmap_proposal_root)
            proposal_repository.save(report.change_proposal)
        path = pack.intake_root / "roadmap-updates" / f"{run_id}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(
                report.model_dump(mode="json"),
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        return (
            self._portable(pack, path),
            "SAFE_TRACKER_AUTO_APPLIED" in report.reason_codes,
        )

    @staticmethod
    def _validation(
        *,
        classification: ArtifactClassification,
        candidates: list[KnowledgeCandidate],
        comparisons: list[ComparisonResult],
        decisions: list[PolicyDecision],
        proposal_id: str | None,
        proposal_issues: list[str],
        pack_issues: list[str],
        safe_change_applied: bool,
        latency_ms: int,
    ) -> ValidationReport:
        classification_passed = not classification.held
        policy_passed = not any(
            decision.disposition is PolicyDisposition.HOLD for decision in decisions
        )
        unsafe = False
        proposal_required = any(
            decision.disposition
            in {
                PolicyDisposition.REVIEW_REQUIRED,
                PolicyDisposition.AUTO_APPLY,
            }
            for decision in decisions
        )
        proposal_reproducible = not proposal_issues and (
            proposal_id is not None or not proposal_required
        )
        checks = [
            ValidationCheck(
                name="project_resolution",
                passed=True,
                detail="ProjectRef was resolved before Source registration.",
            ),
            ValidationCheck(
                name="artifact_classification",
                passed=classification_passed,
                detail=(
                    classification.kind.value
                    if classification_passed
                    else "ambiguous classification held"
                ),
            ),
            ValidationCheck(
                name="semantic_comparison",
                passed=all(
                    comparison.recommended_operation != "CREATE"
                    or comparison.relation.value == "NEW"
                    for comparison in comparisons
                ),
                detail=f"{len(candidates)} candidate(s) compared before proposal.",
            ),
            ValidationCheck(
                name="authoritative_apply",
                passed=not unsafe,
                detail=(
                    "Only a policy-approved safe tracker status was auto-applied."
                    if safe_change_applied
                    else "No authoritative canonical meaning was auto-applied."
                ),
            ),
            ValidationCheck(
                name="policy_gate",
                passed=policy_passed,
                detail=(
                    "No candidate is held by policy."
                    if policy_passed
                    else "At least one candidate requires a governor hold."
                ),
            ),
            ValidationCheck(
                name="proposal_generation",
                passed=proposal_reproducible,
                detail=(
                    "Proposal or deterministic no-proposal disposition was produced."
                    if proposal_reproducible
                    else "An actionable candidate did not produce a Proposal."
                ),
            ),
            ValidationCheck(
                name="proposal_integrity",
                passed=not proposal_issues,
                detail=(
                    "Generated Proposal passed the full ProposalValidator."
                    if not proposal_issues
                    else "; ".join(proposal_issues)
                ),
            ),
            ValidationCheck(
                name="project_pack_contract",
                passed=not pack_issues,
                detail=(
                    "Project Pack, Vault lint, and typed canonical artifacts are valid."
                    if not pack_issues
                    else "; ".join(pack_issues)
                ),
            ),
        ]
        failure = (
            None
            if all(item.passed for item in checks)
            else "AMBIGUOUS_CLASSIFICATION"
            if not classification_passed
            else "POLICY_HOLD"
            if not policy_passed
            else "PROPOSAL_INVALID"
            if proposal_issues
            else "PROJECT_PACK_INVALID"
            if pack_issues
            else "PROPOSAL_NOT_CREATED"
        )
        return ValidationReport(
            checks=checks,
            evaluation=MinimalEvaluationRecord(
                fixture_id=(
                    "roadmap-intake-v1"
                    if classification.kind is ArtifactKind.ROADMAP
                    else f"{classification.kind.value}-intake-v1"
                ),
                project_resolution="pass",
                classification="pass" if classification_passed else "hold",
                duplicate_created=False,
                unsafe_change_applied=unsafe,
                safe_change_applied=safe_change_applied,
                proposal_reproducible=proposal_reproducible,
                user_corrections=0,
                latency_ms=latency_ms,
                token_usage=0,
                failure_category=failure,
            ),
        )

    def _resolution_hold(self, request: IntentRequest, reason: str) -> IntakeResult:
        validation = ValidationReport(
            checks=[
                ValidationCheck(
                    name="project_resolution",
                    passed=False,
                    detail=reason,
                )
            ],
            evaluation=MinimalEvaluationRecord(
                fixture_id="roadmap-intake-v1",
                project_resolution="hold",
                classification="hold",
                duplicate_created=False,
                unsafe_change_applied=False,
                proposal_reproducible=True,
                user_corrections=0,
                latency_ms=0,
                token_usage=0,
                failure_category="PROJECT_UNRESOLVED",
            ),
        )
        path = self._persist_hold(request, reason, validation, project=None)
        return IntakeResult(
            status="hold",
            resolution_reason=reason,
            intake_run_path=str(path.relative_to(self.workspace_root)).replace("\\", "/"),
            validation=validation,
        )

    def _pack_hold(
        self,
        request: IntentRequest,
        project: ProjectRef,
        reason: str,
    ) -> IntakeResult:
        validation = ValidationReport(
            checks=[
                ValidationCheck(
                    name="project_pack_contract",
                    passed=False,
                    detail=reason,
                )
            ],
            evaluation=MinimalEvaluationRecord(
                fixture_id="project-pack-intake-v1",
                project_resolution="pass",
                classification="hold",
                duplicate_created=False,
                unsafe_change_applied=False,
                proposal_reproducible=False,
                user_corrections=0,
                latency_ms=0,
                token_usage=0,
                failure_category="PROJECT_PACK_INVALID",
            ),
        )
        path = self._persist_hold(request, reason, validation, project=project)
        return IntakeResult(
            status="hold",
            project=project,
            resolution_reason=reason,
            intake_run_path=str(path.relative_to(self.workspace_root)).replace("\\", "/"),
            validation=validation,
        )

    def _persist_hold(
        self,
        request: IntentRequest,
        reason: str,
        validation: ValidationReport,
        *,
        project: ProjectRef | None,
    ) -> Path:
        request_payload = json.dumps(
            request.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        fingerprint = hashlib.sha256(request_payload.encode()).hexdigest()
        record = ResolutionHoldRecord(
            hold_id=f"HOLD-{fingerprint[:16].upper()}",
            created_at=self.now(),
            request_fingerprint=fingerprint,
            artifact_count=len(request.artifacts),
            project_hint=request.project_hint,
            project=project,
            reason=reason,
            validation=validation,
        )
        return ResolutionHoldStore(self.workspace_root / ".amplai/intake-holds").save(record)

    @staticmethod
    def _run_id(project: ProjectRef, instruction: str, source_hash: str) -> str:
        seed = hashlib.sha256(
            f"{project.namespace}\0{instruction.strip()}\0{source_hash}".encode()
        ).hexdigest()
        return f"INT-{seed[:16].upper()}"

    @staticmethod
    def _portable(pack: ProjectPack, path: Path) -> str:
        return str(path.resolve().relative_to(pack.root.resolve())).replace("\\", "/")

    @staticmethod
    def _portable_artifact(pack: ProjectPack, path: Path) -> str:
        try:
            return KnowledgeIntakeService._portable(pack, path)
        except ValueError:
            return f"external/{path.name}"

    @staticmethod
    def _result_from_run(
        run: IntakeRun,
        run_path: Path,
        resolution_reason: str,
        pack: ProjectPack,
    ) -> IntakeResult:
        return IntakeResult(
            status="hold" if run.classification.held or not run.validation.passed else "prepared",
            project=run.project,
            resolution_reason=resolution_reason,
            source=run.source,
            classification=run.classification,
            candidates=run.candidates,
            comparisons=run.comparisons,
            policy_decisions=run.policy_decisions,
            proposal_id=run.proposal_id,
            intake_run_path=KnowledgeIntakeService._portable(pack, run_path),
            roadmap_update_path=run.roadmap_update_path,
            validation=run.validation,
        )
