"""Structured semantic identity contracts shared by candidates and memory notes."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

_CANONICAL_TERMS = {
    "계획": "roadmap",
    "로드맵": "roadmap",
    "road-map": "roadmap",
    "진행현황": "tracker",
    "진행상태": "tracker",
    "진행 상태": "tracker",
    "추적기": "tracker",
    "갱신": "update",
    "업데이트": "update",
    "반영": "update",
    "동기화": "synchronize",
    "맞춘다": "synchronize",
    "일치": "consistent",
    "삭제": "delete",
    "제거": "delete",
    "추가": "add",
    "수정": "modify",
    "자동": "automatic",
    "승인": "approval",
}


def normalize_semantic_text(value: str) -> str:
    """Normalize text for deterministic signatures without pretending to understand it."""
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    for source, target in sorted(_CANONICAL_TERMS.items(), key=lambda item: -len(item[0])):
        normalized = normalized.replace(source, target)
    tokens = re.findall(r"[\w-]+", normalized, flags=re.UNICODE)
    return "-".join(tokens)


class ClaimModality(StrEnum):
    MUST = "must"
    MUST_NOT = "must_not"
    SHOULD = "should"
    SHOULD_NOT = "should_not"
    MAY = "may"
    IS = "is"
    IS_NOT = "is_not"

    @property
    def polarity(self) -> bool:
        return self not in {ClaimModality.MUST_NOT, ClaimModality.SHOULD_NOT, ClaimModality.IS_NOT}


class SemanticScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    domain: NonEmptyString
    target: NonEmptyString
    actor: NonEmptyString

    def normalized(self) -> dict[str, str]:
        return {
            "domain": normalize_semantic_text(self.domain),
            "target": normalize_semantic_text(self.target),
            "actor": normalize_semantic_text(self.actor),
        }


class SemanticClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    modality: ClaimModality
    action: NonEmptyString
    object: NonEmptyString
    outcome: NonEmptyString | None = None

    def normalized(self) -> dict[str, str | None]:
        return {
            "modality": self.modality.value,
            "action": normalize_semantic_text(self.action),
            "object": normalize_semantic_text(self.object),
            "outcome": (
                normalize_semantic_text(self.outcome) if self.outcome is not None else None
            ),
        }


def semantic_signature(
    scope: SemanticScope,
    claim: SemanticClaim,
    constraints: list[str],
) -> str:
    payload = {
        "scope": scope.normalized(),
        "claim": claim.normalized(),
        "constraints": sorted({normalize_semantic_text(constraint) for constraint in constraints}),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class SemanticDescriptor(BaseModel):
    """Canonical structured meaning attached to an approved MemoryObject."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    signature: Sha256
    scope: SemanticScope
    claim: SemanticClaim
    constraints: list[NonEmptyString] = Field(default_factory=list)
    canonical_statement: NonEmptyString
    aliases: list[NonEmptyString] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_signature(self) -> SemanticDescriptor:
        expected = semantic_signature(self.scope, self.claim, list(self.constraints))
        if self.signature != expected:
            raise ValueError("semantic signature가 scope/claim/constraints와 일치하지 않습니다.")
        return self
