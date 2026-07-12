"""Storage-independent memory object models."""

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from amplai_foundry.domain.enums import MemoryKind, MemoryStatus, RelationType
from amplai_foundry.domain.source import SourceMetadata

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
MemoryId = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9]*-[A-Z0-9-]+$")]


class MemoryRelation(BaseModel):
    """A typed, directed link to another memory object."""

    model_config = ConfigDict(extra="forbid")

    type: RelationType
    target: MemoryId


class MemoryMetadata(BaseModel):
    """Canonical Front Matter contract independent of a storage adapter."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    id: MemoryId
    namespace: NonEmptyString
    project: NonEmptyString
    kind: MemoryKind
    status: MemoryStatus
    title: NonEmptyString
    summary: NonEmptyString
    created_at: date
    updated_at: date
    source_refs: list[MemoryId] = Field(default_factory=list)
    relations: list[MemoryRelation] = Field(default_factory=list)
    superseded_by: MemoryId | None = None
    merged_into: MemoryId | None = None
    revision: int = Field(ge=1)
    tags: list[NonEmptyString] = Field(default_factory=list)
    source_metadata: SourceMetadata | None = None

    @model_validator(mode="after")
    def validate_source_metadata(self) -> "MemoryMetadata":
        """Require typed source metadata only on source notes."""
        if self.kind is MemoryKind.SOURCE and self.source_metadata is None:
            raise ValueError("kind=source에는 source_metadata가 필요합니다.")
        if self.kind is not MemoryKind.SOURCE and self.source_metadata is not None:
            raise ValueError("source_metadata는 kind=source에서만 허용됩니다.")
        return self


class MemoryObject(MemoryMetadata):
    """Canonical metadata plus Markdown body content."""

    content: str = ""

    def referenced_ids(self) -> set[str]:
        """Return every structured memory reference made by this object."""
        references = {relation.target for relation in self.relations}
        references.update(self.source_refs)
        if self.superseded_by:
            references.add(self.superseded_by)
        if self.merged_into:
            references.add(self.merged_into)
        return references
