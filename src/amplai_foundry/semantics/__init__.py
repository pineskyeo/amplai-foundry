"""Semantic candidate retrieval and deterministic comparison."""

from amplai_foundry.semantics.comparison import SemanticComparator
from amplai_foundry.semantics.models import (
    ComparisonRelation,
    ComparisonResult,
    KnowledgeCandidate,
    SemanticAnchor,
)

__all__ = [
    "ComparisonRelation",
    "ComparisonResult",
    "KnowledgeCandidate",
    "SemanticAnchor",
    "SemanticComparator",
]
