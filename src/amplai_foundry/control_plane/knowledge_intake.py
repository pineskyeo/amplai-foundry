"""Governed completion handler for explicit Hermes knowledge-intake requests."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from amplai_foundry.control_plane.models import DurableJob
from amplai_foundry.curation.context_builder import CurateContextBuilder
from amplai_foundry.ingestion.service import SourceIngestionService
from amplai_foundry.proposals.models import (
    Confidence,
    EvidenceLocator,
    OperationType,
    Proposal,
    ProposalEvidence,
    ProposalOperation,
    ProposalStatus,
    proposal_id,
)
from amplai_foundry.proposals.repository import ProposalRepository
from amplai_foundry.proposals.validation import ProposalValidator


class GovernedKnowledgeIntakeHandler:
    """Preserve an explicit source, then prepare—not apply—curation work."""

    def __init__(
        self,
        *,
        vault: Path,
        knowledge_project: str,
        workspace: Path,
        repository_root: Path,
    ) -> None:
        self.vault = vault.expanduser().resolve(strict=False)
        self.knowledge_project = knowledge_project
        self.workspace = workspace.expanduser().resolve(strict=False)
        self.repository_root = repository_root.expanduser().resolve(strict=False)

    def __call__(self, job: DurableJob) -> dict[str, Any]:
        if job.kind != "knowledge.intake":
            raise ValueError("KNOWLEDGE_INTAKE_JOB_KIND_INVALID")
        source_text = job.payload.get("source_text")
        if not isinstance(source_text, str) or not source_text:
            raise ValueError("KNOWLEDGE_INTAKE_SOURCE_INVALID")
        result = SourceIngestionService(self.vault).ingest(
            source_text.encode("utf-8"),
            project=self.knowledge_project,
            source_type="hermes",
            title=f"Hermes knowledge intake {job.job_id}",
            original_filename=f"{job.job_id}.txt",
            media_type="text/plain",
            created_by="hermes-request",
        )
        output = self.workspace / ".amplai" / "jobs" / f"CURATE-{result.source_id}.md"
        context_path = CurateContextBuilder(self.vault, repository_root=self.repository_root).write(
            result.source_id, project=self.knowledge_project, output=output
        )
        proposal = self._prepare_proposal(result.source_id)
        repository = ProposalRepository(self.workspace / ".amplai" / "proposals")
        proposal_path = repository.path_for(proposal.proposal_id)
        issues = ProposalValidator(self.vault).validate(proposal, proposal_path)
        if issues:
            raise ValueError("KNOWLEDGE_INTAKE_PROPOSAL_INVALID")
        repository.save(proposal)
        return {
            "source_id": result.source_id,
            "source_status": result.status,
            "source_path": result.path,
            "curation_context": str(context_path),
            "proposal_id": proposal.proposal_id,
            "proposal_path": str(proposal_path),
            "proposal_status": proposal.status.value,
            "canonical_mutation": "not_attempted",
            "next_action": "governed_human_review_required",
        }

    def _prepare_proposal(self, source_id: str) -> Proposal:
        """Create the minimal reviewable disposition without inferring knowledge facts.

        Hermes provides free-form source text, not a trusted semantic operation.
        A draft ``IGNORE`` operation preserves the source and explicitly asks a
        human curator to classify it; no canonical Memory is written or changed.
        """
        created_at = datetime.now(UTC)
        digest = hashlib.sha256(source_id.encode("utf-8")).hexdigest()
        return Proposal(
            proposal_id=proposal_id(created_at.date(), digest),
            project=self.knowledge_project,
            namespace=f"org/default/project/{self.knowledge_project}",
            status=ProposalStatus.DRAFT,
            source_ids=[source_id],
            created_at=created_at,
            created_by="hermes-intake-worker",
            operations=[
                ProposalOperation(
                    operation_id="OP-001",
                    type=OperationType.IGNORE,
                    kind="source",
                    title="Hermes intake awaits governed curation",
                    reason="Free-form Hermes text has no automatic canonical knowledge action.",
                    evidence=[
                        ProposalEvidence(
                            source_id=source_id, locator=EvidenceLocator(start=1, end=1)
                        )
                    ],
                    confidence=Confidence.HIGH,
                )
            ],
            notes=["Canonical apply is intentionally deferred to governed human review."],
        )
