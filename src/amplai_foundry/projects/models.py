"""Versioned Project Pack and domain-lock models."""

from __future__ import annotations

import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from amplai_foundry.domain.identity import Namespace, ProjectRef
from amplai_foundry.domain.project import ProjectId

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
ModuleId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]
VersionSpec = Annotated[str, StringConstraints(pattern=r"^[0-9]+(?:\.[0-9]+)*(?:\.x)?$")]
ExactVersion = Annotated[str, StringConstraints(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")]


def validate_relative_path(value: str) -> str:
    """Reject absolute and parent-traversing manifest paths."""
    path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    if (
        "\\" in value
        or path.is_absolute()
        or windows_path.is_absolute()
        or bool(windows_path.drive)
        or ".." in path.parts
        or ".." in windows_path.parts
        or value.strip() in {"", "."}
    ):
        raise ValueError("Project Pack path는 pack root 아래의 상대 경로여야 합니다.")
    return value


class DomainImport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    module: ModuleId
    version: VersionSpec


class CanonicalPaths(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    memory: str
    proposals: str = ".amplai/proposals"
    intake: str = ".amplai/intake"
    semantic: str = ".amplai/semantic"
    ontology: str = ".amplai/ontology"
    skills: str = ".amplai/skills"
    policies: str = ".amplai/policies"
    agents: str = ".amplai/agents"
    evals: str = ".amplai/evals"
    roadmap: str | None = None

    @model_validator(mode="after")
    def validate_paths(self) -> CanonicalPaths:
        for field_name in type(self).model_fields:
            value = getattr(self, field_name)
            if value is not None:
                validate_relative_path(str(value))
        return self


class RuntimePaths(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    root: str = ".amplai/runtime"

    @model_validator(mode="after")
    def validate_path(self) -> RuntimePaths:
        validate_relative_path(self.root)
        return self


class ProjectManifest(BaseModel):
    """Clone-path-independent Project Pack manifest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    project_id: ProjectId
    namespace: Namespace
    name: NonEmptyString
    aliases: list[NonEmptyString] = Field(default_factory=list)
    default: bool = False
    imports: list[DomainImport] = Field(default_factory=list)
    profiles: list[NonEmptyString] = Field(default_factory=list)
    canonical: CanonicalPaths
    runtime: RuntimePaths = Field(default_factory=RuntimePaths)

    @model_validator(mode="after")
    def validate_identity_and_imports(self) -> ProjectManifest:
        ProjectRef(project_id=self.project_id, namespace=self.namespace)
        module_ids = [item.module for item in self.imports]
        if len(module_ids) != len(set(module_ids)):
            raise ValueError("Project import module은 중복될 수 없습니다.")
        aliases = [alias.casefold() for alias in self.aliases]
        if len(aliases) != len(set(aliases)):
            raise ValueError("Project alias는 대소문자와 무관하게 고유해야 합니다.")
        return self

    @property
    def ref(self) -> ProjectRef:
        return ProjectRef(project_id=self.project_id, namespace=self.namespace)


class DomainLockEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    module: ModuleId
    version: ExactVersion


class DomainLock(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    imports: list[DomainLockEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_modules(self) -> DomainLock:
        modules = [item.module for item in self.imports]
        if len(modules) != len(set(modules)):
            raise ValueError("domain lock module은 중복될 수 없습니다.")
        return self


class DomainModuleManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    module: ModuleId
    version: ExactVersion
    imports: list[DomainImport] = Field(default_factory=list)


def version_satisfies(version: str, requested: str) -> bool:
    """Return whether an exact semantic version satisfies the minimal ``x`` syntax."""
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        return False
    if requested.endswith(".x"):
        return version.startswith(requested[:-1])
    requested_parts = requested.split(".")
    version_parts = version.split(".")
    return version_parts[: len(requested_parts)] == requested_parts
