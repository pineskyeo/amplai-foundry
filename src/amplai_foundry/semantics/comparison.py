"""Deterministic, fail-closed semantic comparison kernel."""

from __future__ import annotations

import re

from amplai_foundry.domain.semantic import normalize_semantic_text
from amplai_foundry.semantics.models import (
    ComparisonRelation,
    ComparisonResult,
    ComparisonSignals,
    KnowledgeCandidate,
    SemanticAnchor,
)


def _tokens(value: str) -> set[str]:
    return set(re.findall(r"\w+", normalize_semantic_text(value), flags=re.UNICODE))


def _lexical_similarity(candidate: KnowledgeCandidate, anchor: SemanticAnchor) -> float:
    left = _tokens(" ".join([candidate.canonical_statement, *candidate.aliases]))
    right = _tokens(
        " ".join(
            [
                anchor.descriptor.canonical_statement,
                *anchor.descriptor.aliases,
            ]
        )
    )
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def _subject(candidate: KnowledgeCandidate) -> tuple[str, ...]:
    scope = candidate.scope.normalized()
    claim = candidate.claim.normalized()
    return (
        scope["domain"],
        scope["target"],
        scope["actor"],
        str(claim["action"]),
        str(claim["object"]),
    )


def _anchor_subject(anchor: SemanticAnchor) -> tuple[str, ...]:
    scope = anchor.descriptor.scope.normalized()
    claim = anchor.descriptor.claim.normalized()
    return (
        scope["domain"],
        scope["target"],
        scope["actor"],
        str(claim["action"]),
        str(claim["object"]),
    )


class SemanticComparator:
    """Classify deterministic signals; ambiguity never defaults to ``NEW``."""

    def compare(
        self,
        candidate: KnowledgeCandidate,
        anchors: list[SemanticAnchor],
    ) -> ComparisonResult:
        scoped = [anchor for anchor in anchors if anchor.project == candidate.project]
        if not scoped:
            return self._new(candidate)

        ranked = sorted(
            (
                (
                    self._relation(candidate, anchor),
                    _lexical_similarity(candidate, anchor),
                    anchor,
                )
                for anchor in scoped
            ),
            key=lambda item: (
                self._priority(item[0]),
                -item[1],
                item[2].semantic_id,
            ),
        )
        relation, lexical, anchor = ranked[0]
        equally_strong = [
            item for item in ranked[1:] if item[0] == relation and abs(item[1] - lexical) < 0.05
        ]
        if relation is not ComparisonRelation.NEW and equally_strong:
            return self._uncertain(candidate, lexical, "MULTIPLE_PLAUSIBLE_MATCHES")
        return self._result(candidate, anchor, relation, lexical)

    @staticmethod
    def _priority(relation: ComparisonRelation) -> int:
        return {
            ComparisonRelation.EXACT_DUPLICATE: 0,
            ComparisonRelation.CONFLICTS: 1,
            ComparisonRelation.REFINES: 2,
            ComparisonRelation.SEMANTIC_DUPLICATE: 3,
            ComparisonRelation.UNCERTAIN: 4,
            ComparisonRelation.NEW: 5,
        }[relation]

    @staticmethod
    def _relation(
        candidate: KnowledgeCandidate,
        anchor: SemanticAnchor,
    ) -> ComparisonRelation:
        if candidate.signature == anchor.descriptor.signature:
            if normalize_semantic_text(candidate.canonical_statement) == normalize_semantic_text(
                anchor.descriptor.canonical_statement
            ):
                return ComparisonRelation.EXACT_DUPLICATE
            return ComparisonRelation.SEMANTIC_DUPLICATE
        subject_match = _subject(candidate) == _anchor_subject(anchor)
        lexical = _lexical_similarity(candidate, anchor)
        if subject_match:
            if candidate.claim.modality.polarity != anchor.descriptor.claim.modality.polarity:
                return ComparisonRelation.CONFLICTS
            existing_constraints = {
                normalize_semantic_text(item) for item in anchor.descriptor.constraints
            }
            candidate_constraints = {
                normalize_semantic_text(item) for item in candidate.constraints
            }
            if candidate_constraints > existing_constraints:
                return ComparisonRelation.REFINES
            if candidate_constraints == existing_constraints:
                return ComparisonRelation.SEMANTIC_DUPLICATE
            return ComparisonRelation.UNCERTAIN
        if lexical >= 0.2:
            return ComparisonRelation.UNCERTAIN
        return ComparisonRelation.NEW

    def _result(
        self,
        candidate: KnowledgeCandidate,
        anchor: SemanticAnchor,
        relation: ComparisonRelation,
        lexical: float,
    ) -> ComparisonResult:
        subject_match = _subject(candidate) == _anchor_subject(anchor)
        signature_match = candidate.signature == anchor.descriptor.signature
        aliases = {
            normalize_semantic_text(item)
            for item in [candidate.canonical_statement, *candidate.aliases]
        } & {
            normalize_semantic_text(item)
            for item in [
                anchor.descriptor.canonical_statement,
                *anchor.descriptor.aliases,
            ]
        }
        existing_constraints = {
            normalize_semantic_text(item) for item in anchor.descriptor.constraints
        }
        candidate_constraints = {normalize_semantic_text(item) for item in candidate.constraints}
        constraint_relation = (
            "equal"
            if candidate_constraints == existing_constraints
            else "superset"
            if candidate_constraints > existing_constraints
            else "subset"
            if candidate_constraints < existing_constraints
            else "overlap"
        )
        policy = {
            ComparisonRelation.EXACT_DUPLICATE: ("IGNORE", False, 1.0),
            ComparisonRelation.SEMANTIC_DUPLICATE: ("LINK", False, 0.98),
            ComparisonRelation.REFINES: ("UPDATE", True, 0.92),
            ComparisonRelation.CONFLICTS: ("CONFLICT", True, 0.99),
            ComparisonRelation.NEW: ("CREATE", True, 0.85),
            ComparisonRelation.UNCERTAIN: ("HOLD", True, 0.5),
        }
        operation, review, confidence = policy[relation]
        if relation is ComparisonRelation.NEW:
            return self._new(candidate)
        if relation is ComparisonRelation.UNCERTAIN:
            return self._uncertain(candidate, lexical, "INSUFFICIENT_SEMANTIC_EVIDENCE")
        return ComparisonResult(
            candidate_id=candidate.candidate_id,
            relation=relation,
            matched_semantic_id=anchor.semantic_id,
            matched_target_refs=anchor.target_refs,
            confidence=confidence,
            signals=ComparisonSignals(
                signature_match=signature_match,
                subject_match=subject_match,
                constraint_relation=constraint_relation,
                lexical_score=round(lexical, 4),
                alias_match=bool(aliases),
            ),
            reason_codes=[relation.value],
            recommended_operation=operation,
            requires_review=review,
        )

    @staticmethod
    def _new(candidate: KnowledgeCandidate) -> ComparisonResult:
        return ComparisonResult(
            candidate_id=candidate.candidate_id,
            relation=ComparisonRelation.NEW,
            confidence=0.85,
            signals=ComparisonSignals(lexical_score=0.0),
            reason_codes=["NO_PROJECT_SCOPED_MATCH"],
            recommended_operation="CREATE",
            requires_review=True,
        )

    @staticmethod
    def _uncertain(
        candidate: KnowledgeCandidate,
        lexical: float,
        reason: str,
    ) -> ComparisonResult:
        return ComparisonResult(
            candidate_id=candidate.candidate_id,
            relation=ComparisonRelation.UNCERTAIN,
            confidence=0.5,
            signals=ComparisonSignals(lexical_score=round(lexical, 4)),
            reason_codes=[reason],
            recommended_operation="HOLD",
            requires_review=True,
        )
