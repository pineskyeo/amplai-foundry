"""Risk and review policy derived from semantic comparison results."""

from __future__ import annotations

from amplai_foundry.domain.enums import MemoryKind
from amplai_foundry.intake.models import (
    PolicyDecision,
    PolicyDisposition,
    RiskLevel,
)
from amplai_foundry.semantics.models import ComparisonRelation, ComparisonResult, KnowledgeCandidate


class IntakePolicy:
    def decide(
        self,
        candidate: KnowledgeCandidate,
        comparison: ComparisonResult,
        *,
        allow_auto_apply: bool = False,
    ) -> PolicyDecision:
        if comparison.relation is ComparisonRelation.UNCERTAIN:
            return PolicyDecision(
                candidate_id=candidate.candidate_id,
                risk=RiskLevel.HIGH,
                disposition=PolicyDisposition.HOLD,
                reason_codes=["UNCERTAIN_NEVER_CREATES"],
            )
        if comparison.relation is ComparisonRelation.CONFLICTS:
            return PolicyDecision(
                candidate_id=candidate.candidate_id,
                risk=RiskLevel.HIGH,
                disposition=PolicyDisposition.HOLD,
                reason_codes=["CONFLICT_REQUIRES_GOVERNOR"],
            )
        if comparison.relation is ComparisonRelation.EXACT_DUPLICATE:
            if comparison.matched_target_refs:
                return PolicyDecision(
                    candidate_id=candidate.candidate_id,
                    risk=RiskLevel.LOW,
                    disposition=(
                        PolicyDisposition.AUTO_APPLY
                        if allow_auto_apply
                        else PolicyDisposition.REVIEW_REQUIRED
                    ),
                    reason_codes=[
                        "EXACT_DUPLICATE_LINK_INDEPENDENT_EVIDENCE",
                        (
                            "LOW_RISK_AUTO_APPLY_ALLOWED"
                            if allow_auto_apply
                            else "REVIEW_REQUIRED_BY_AUTHORITY"
                        ),
                    ],
                )
            return PolicyDecision(
                candidate_id=candidate.candidate_id,
                risk=RiskLevel.LOW,
                disposition=PolicyDisposition.IGNORE,
                reason_codes=["EXACT_DUPLICATE_NO_CHANGE"],
            )
        if comparison.relation is ComparisonRelation.SEMANTIC_DUPLICATE:
            if not comparison.matched_target_refs:
                return PolicyDecision(
                    candidate_id=candidate.candidate_id,
                    risk=RiskLevel.LOW,
                    disposition=PolicyDisposition.IGNORE,
                    reason_codes=["SEMANTIC_DUPLICATE_WITHOUT_CANONICAL_TARGET"],
                )
            return PolicyDecision(
                candidate_id=candidate.candidate_id,
                risk=RiskLevel.LOW,
                disposition=(
                    PolicyDisposition.AUTO_APPLY
                    if allow_auto_apply
                    else PolicyDisposition.REVIEW_REQUIRED
                ),
                reason_codes=[
                    "SEMANTIC_DUPLICATE_LINK_EVIDENCE",
                    (
                        "LOW_RISK_AUTO_APPLY_ALLOWED"
                        if allow_auto_apply
                        else "REVIEW_REQUIRED_BY_AUTHORITY"
                    ),
                ],
            )
        high_kinds = {
            MemoryKind.PRINCIPLE,
            MemoryKind.DECISION,
            MemoryKind.ARCHITECTURE,
        }
        risk = RiskLevel.HIGH if candidate.suggested_kinds[0] in high_kinds else RiskLevel.MEDIUM
        return PolicyDecision(
            candidate_id=candidate.candidate_id,
            risk=risk,
            disposition=PolicyDisposition.REVIEW_REQUIRED,
            reason_codes=[f"{comparison.relation.value}_PROPOSAL_ONLY"],
        )
