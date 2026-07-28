"""Knowledge candidate, anchor, and comparison result contracts."""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from amplai_foundry.domain.enums import MemoryKind
from amplai_foundry.domain.identity import MemoryRef, ProjectRef
from amplai_foundry.domain.semantic import (
    NonEmptyString,
    SemanticClaim,
    SemanticDescriptor,
    SemanticScope,
    semantic_signature,
)

CandidateId = Annotated[str, StringConstraints(pattern=r"^CAND-[A-F0-9]{12}$")]
SemanticId = Annotated[str, StringConstraints(pattern=r"^SEM-[A-F0-9]{12}$")]


class CandidateEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: MemoryRef
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_range(self) -> CandidateEvidence:
        if self.end_line < self.start_line:
            raise ValueError("candidate evidence end_line은 start_line 이상이어야 합니다.")
        return self


class KnowledgeCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: CandidateId
    project: ProjectRef
    scope: SemanticScope
    claim: SemanticClaim
    constraints: list[NonEmptyString] = Field(default_factory=list)
    canonical_statement: NonEmptyString
    aliases: list[NonEmptyString] = Field(default_factory=list)
    suggested_kinds: list[MemoryKind] = Field(min_length=1)
    evidence: list[CandidateEvidence] = Field(min_length=1)
    signature: str

    @model_validator(mode="after")
    def validate_candidate(self) -> KnowledgeCandidate:
        expected = semantic_signature(self.scope, self.claim, list(self.constraints))
        if self.signature != expected:
            raise ValueError("candidate signature가 구조화된 의미와 일치하지 않습니다.")
        forbidden = {MemoryKind.SOURCE, MemoryKind.MAP}
        if any(kind in forbidden for kind in self.suggested_kinds):
            raise ValueError("Source와 Map은 semantic candidate kind가 될 수 없습니다.")
        if any(item.source.namespace != self.project.namespace for item in self.evidence):
            raise ValueError("candidate evidence는 같은 project namespace에 속해야 합니다.")
        return self

    @classmethod
    def create(
        cls,
        *,
        project: ProjectRef,
        scope: SemanticScope,
        claim: SemanticClaim,
        constraints: list[str],
        canonical_statement: str,
        aliases: list[str],
        suggested_kinds: list[MemoryKind],
        evidence: list[CandidateEvidence],
    ) -> KnowledgeCandidate:
        signature = semantic_signature(scope, claim, constraints)
        seed = hashlib.sha256(
            f"{project.namespace}\0{signature}\0{evidence[0].source.qualified}".encode()
        ).hexdigest()
        return cls(
            candidate_id=f"CAND-{seed[:12].upper()}",
            project=project,
            scope=scope,
            claim=claim,
            constraints=constraints,
            canonical_statement=canonical_statement,
            aliases=aliases,
            suggested_kinds=suggested_kinds,
            evidence=evidence,
            signature=signature,
        )

    def descriptor(self) -> SemanticDescriptor:
        return SemanticDescriptor(
            signature=self.signature,
            scope=self.scope,
            claim=self.claim,
            constraints=self.constraints,
            canonical_statement=self.canonical_statement,
            aliases=self.aliases,
        )


class SemanticAnchor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    semantic_id: SemanticId
    project: ProjectRef
    descriptor: SemanticDescriptor
    target_refs: list[MemoryRef] = Field(default_factory=list)
    source_refs: list[MemoryRef] = Field(default_factory=list)
    provisional: bool = False

    @model_validator(mode="after")
    def validate_refs(self) -> SemanticAnchor:
        references = [*self.target_refs, *self.source_refs]
        if any(item.namespace != self.project.namespace for item in references):
            raise ValueError("SemanticAnchor reference는 같은 project namespace여야 합니다.")
        return self

    @classmethod
    def from_candidate(
        cls,
        candidate: KnowledgeCandidate,
        *,
        target_refs: list[MemoryRef] | None = None,
        provisional: bool = True,
    ) -> SemanticAnchor:
        seed = hashlib.sha256(
            f"{candidate.project.namespace}\0{candidate.signature}".encode()
        ).hexdigest()
        return cls(
            semantic_id=f"SEM-{seed[:12].upper()}",
            project=candidate.project,
            descriptor=candidate.descriptor(),
            target_refs=target_refs or [],
            source_refs=[item.source for item in candidate.evidence],
            provisional=provisional,
        )


class ComparisonRelation(StrEnum):
    EXACT_DUPLICATE = "EXACT_DUPLICATE"
    SEMANTIC_DUPLICATE = "SEMANTIC_DUPLICATE"
    REFINES = "REFINES"
    CONFLICTS = "CONFLICTS"
    NEW = "NEW"
    UNCERTAIN = "UNCERTAIN"


class ComparisonSignals(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    signature_match: bool = False
    subject_match: bool = False
    constraint_relation: str = "none"
    lexical_score: float = Field(ge=0, le=1)
    alias_match: bool = False


class ComparisonResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: CandidateId
    relation: ComparisonRelation
    matched_semantic_id: SemanticId | None = None
    matched_target_refs: list[MemoryRef] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    signals: ComparisonSignals
    reason_codes: list[NonEmptyString] = Field(min_length=1)
    recommended_operation: str
    requires_review: bool

    @model_validator(mode="after")
    def validate_match(self) -> ComparisonResult:
        matched_relations = {
            ComparisonRelation.EXACT_DUPLICATE,
            ComparisonRelation.SEMANTIC_DUPLICATE,
            ComparisonRelation.REFINES,
            ComparisonRelation.CONFLICTS,
        }
        if self.relation in matched_relations and self.matched_semantic_id is None:
            raise ValueError(f"{self.relation.value}에는 matched_semantic_id가 필요합니다.")
        if self.relation in {ComparisonRelation.NEW, ComparisonRelation.UNCERTAIN} and (
            self.matched_semantic_id is not None
        ):
            raise ValueError(f"{self.relation.value}에는 matched_semantic_id를 지정하지 않습니다.")
        return self
