"""Qualified project and memory identities."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator

from amplai_foundry.domain.project import ProjectId

MemoryId = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9]*-[A-Z0-9-]+$")]
MemoryNamespace = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]
Namespace = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        pattern=r"^[a-z0-9][a-z0-9_-]*(/[a-z0-9][a-z0-9_-]*)+$",
    ),
]


class ProjectRef(BaseModel):
    """Stable identity for one project independent of its clone path."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: ProjectId
    namespace: Namespace

    @model_validator(mode="after")
    def validate_namespace(self) -> ProjectRef:
        if not self.namespace.endswith(f"/project/{self.project_id}"):
            raise ValueError("project namespace는 /project/{project_id}로 끝나야 합니다.")
        return self


class MemoryRef(BaseModel):
    """Qualified memory identity; local IDs are only unique inside a namespace."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    namespace: MemoryNamespace
    local_id: MemoryId

    @property
    def qualified(self) -> str:
        return f"{self.namespace}#{self.local_id}"

    @classmethod
    def parse(cls, value: str, *, default_namespace: str | None = None) -> MemoryRef:
        """Parse ``namespace#local_id`` or a local ID with an explicit default."""
        if "#" in value:
            namespace, local_id = value.rsplit("#", 1)
            return cls(namespace=namespace, local_id=local_id)
        if default_namespace is None:
            raise ValueError("local memory ID를 해석하려면 default namespace가 필요합니다.")
        return cls(namespace=default_namespace, local_id=value)

    def __str__(self) -> str:
        return self.qualified
