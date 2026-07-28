"""Small deterministic candidate extractor for the Phase 1 vertical slice."""

from __future__ import annotations

import re

from amplai_foundry.domain.enums import MemoryKind
from amplai_foundry.domain.identity import MemoryRef, ProjectRef
from amplai_foundry.domain.semantic import ClaimModality, SemanticClaim, SemanticScope
from amplai_foundry.intake.models import ArtifactClassification, ArtifactKind
from amplai_foundry.semantics.models import CandidateEvidence, KnowledgeCandidate

_KIND_SUGGESTIONS = {
    ArtifactKind.ROADMAP: [MemoryKind.ARCHITECTURE, MemoryKind.DECISION],
    ArtifactKind.ARCHITECTURE_PROPOSAL: [MemoryKind.ARCHITECTURE],
    ArtifactKind.MEETING_NOTE: [MemoryKind.QUESTION, MemoryKind.CONCEPT],
    ArtifactKind.INCIDENT: [MemoryKind.EXPERIMENT, MemoryKind.CONCEPT],
    ArtifactKind.SOP: [MemoryKind.PRINCIPLE, MemoryKind.ARCHITECTURE],
    ArtifactKind.IMPLEMENTATION_RESULT: [MemoryKind.EXPERIMENT],
}


class CandidateExtractor:
    """Extract one auditable candidate; unknown artifacts stay held."""

    def extract(
        self,
        *,
        project: ProjectRef,
        source: MemoryRef,
        instruction: str,
        content: str,
        title: str,
        classification: ArtifactClassification,
    ) -> list[KnowledgeCandidate]:
        if classification.held or classification.kind is ArtifactKind.UNKNOWN:
            return []
        lines = content.splitlines() or [content]
        if classification.kind is ArtifactKind.ROADMAP:
            return [
                self._roadmap_candidate(
                    project=project,
                    source=source,
                    instruction=instruction,
                    content=content,
                    end_line=max(1, len(lines)),
                )
            ]
        statement = self._first_statement(content) or instruction
        return [
            KnowledgeCandidate.create(
                project=project,
                scope=SemanticScope(
                    domain=classification.kind.value,
                    target=title,
                    actor=project.project_id,
                ),
                claim=SemanticClaim(
                    modality=ClaimModality.MUST,
                    action="record",
                    object=statement,
                    outcome="reviewable-project-knowledge",
                ),
                constraints=[],
                canonical_statement=statement,
                aliases=[title],
                suggested_kinds=_KIND_SUGGESTIONS[classification.kind],
                evidence=[
                    CandidateEvidence(
                        source=source,
                        start_line=1,
                        end_line=max(1, len(lines)),
                    )
                ],
            )
        ]

    @staticmethod
    def _roadmap_candidate(
        *,
        project: ProjectRef,
        source: MemoryRef,
        instruction: str,
        content: str,
        end_line: int,
    ) -> KnowledgeCandidate:
        combined = f"{instruction}\n{content}".casefold()
        synchronize = any(
            token in combined
            for token in ("tracker", "진행 현황", "진행현황", "진행 상태", "동기화")
        )
        constraints = [
            canonical
            for canonical, variants in (
                ("handle-add", ("추가", "add")),
                ("handle-modify", ("수정", "update", "modify")),
                ("handle-delete", ("삭제", "delete", "cancel")),
                ("preserve-revision", ("revision", "version", "버전", "이력")),
                ("analyze-impact", ("impact", "영향")),
            )
            if any(variant in combined for variant in variants)
        ]
        action = "synchronize" if synchronize else "adopt"
        target = "roadmap-tracker" if synchronize else "project-roadmap"
        statement = (
            "Roadmap structural changes must remain synchronized with tracker state."
            if synchronize
            else "The project roadmap must be adopted through a governed, versioned change."
        )
        return KnowledgeCandidate.create(
            project=project,
            scope=SemanticScope(
                domain="project-planning",
                target=target,
                actor=project.project_id,
            ),
            claim=SemanticClaim(
                modality=ClaimModality.MUST,
                action=action,
                object=target,
                outcome="consistent-versioned-plan-state",
            ),
            constraints=constraints,
            canonical_statement=statement,
            aliases=[instruction],
            suggested_kinds=_KIND_SUGGESTIONS[ArtifactKind.ROADMAP],
            evidence=[CandidateEvidence(source=source, start_line=1, end_line=end_line)],
        )

    @staticmethod
    def _first_statement(content: str) -> str:
        for paragraph in re.split(r"\n\s*\n", content):
            cleaned = " ".join(
                line.strip()
                for line in paragraph.splitlines()
                if line.strip() and not line.lstrip().startswith(("#", "```"))
            )
            if cleaned:
                return cleaned[:500]
        return ""
