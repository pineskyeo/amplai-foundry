"""Canonical immutable Proposal definition contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints, model_validator

from amplai_foundry.domain.identity import MemoryRef
from amplai_foundry.governance.models import Digest, ProposalRef

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
GitRevision = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{7,64}$")]


class DefinitionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_ref: MemoryRef
    source_digest: Digest


class ApplyInputDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    logical_name: NonEmptyString
    object_digest: Digest
    media_type: NonEmptyString


class ProposalDefinitionManifest(BaseModel):
    """Definition envelope whose digest excludes only its self-referential field."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[3] = 3
    proposal_ref: ProposalRef
    definition_digest: Digest | None = None
    canonicalization_version: Literal[1] = 1
    operations: tuple[dict[str, JsonValue], ...] = Field(min_length=1)
    evidence: tuple[DefinitionEvidence, ...] = Field(default_factory=tuple)
    apply_inputs: tuple[ApplyInputDescriptor, ...] = Field(default_factory=tuple)
    preconditions: tuple[dict[str, JsonValue], ...] = Field(default_factory=tuple)
    base_revision: GitRevision
    validation_policy_ref: NonEmptyString

    @model_validator(mode="after")
    def validate_unique_inputs(self) -> ProposalDefinitionManifest:
        logical_names = tuple(item.logical_name for item in self.apply_inputs)
        if len(logical_names) != len(set(logical_names)):
            raise ValueError("apply input logical_name은 definition 안에서 고유해야 합니다.")
        return self


@dataclass(frozen=True, slots=True)
class CanonicalDefinition:
    manifest: ProposalDefinitionManifest
    digest: str
    canonical_bytes: bytes


def _canonical_json_bytes(payload: object) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def canonicalize_definition(manifest: ProposalDefinitionManifest) -> CanonicalDefinition:
    """Return version-1 canonical bytes and the semantic definition digest."""

    preimage = manifest.model_dump(mode="json", exclude={"definition_digest"})
    digest = f"sha256:{hashlib.sha256(_canonical_json_bytes(preimage)).hexdigest()}"
    if manifest.definition_digest is not None and manifest.definition_digest != digest:
        raise ValueError(
            "definition_digest가 canonical manifest와 일치하지 않습니다: "
            f"expected={digest} actual={manifest.definition_digest}"
        )
    canonical_manifest = manifest.model_copy(update={"definition_digest": digest})
    return CanonicalDefinition(
        manifest=canonical_manifest,
        digest=digest,
        canonical_bytes=_canonical_json_bytes(canonical_manifest.model_dump(mode="json")),
    )
