"""Storage-independent memory object models."""

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from amplai_foundry.domain.enums import MemoryKind, MemoryStatus, RelationType

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
MemoryId = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9]*-[A-Z0-9-]+$")]


class MemoryRelation(BaseModel):
    """A typed, directed link to another memory object."""

    model_config = ConfigDict(extra="forbid")

    type: RelationType
    target: MemoryId


class MemoryObject(BaseModel):
    """Canonical memory contract independent of a storage adapter."""

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
