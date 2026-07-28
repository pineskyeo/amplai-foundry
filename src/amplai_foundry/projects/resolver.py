"""Fail-closed project resolution for intent-driven intake."""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from amplai_foundry.domain.identity import ProjectRef
from amplai_foundry.projects.repository import ProjectPack, ProjectPackRepository


class ProjectResolutionStatus(StrEnum):
    RESOLVED = "resolved"
    HOLD = "hold"


class ProjectResolution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: ProjectResolutionStatus
    project: ProjectRef | None = None
    reason: str
    candidates: list[ProjectRef] = Field(default_factory=list)


class ProjectResolver:
    """Resolve only when explicit, path-contained, named, or uniquely default."""

    def __init__(self, repository: ProjectPackRepository) -> None:
        self.repository = repository

    def resolve(
        self,
        *,
        instruction: str,
        project_hint: str | None = None,
        artifact_paths: list[Path] | None = None,
    ) -> ProjectResolution:
        packs = self.repository.list()
        if project_hint is not None:
            matched = [pack for pack in packs if self._matches_hint(pack, project_hint)]
            return self._single_or_hold(matched, f"explicit project hint: {project_hint}")

        path_matches: list[ProjectPack] = []
        for artifact in artifact_paths or []:
            resolved = artifact.resolve()
            containing = [pack for pack in packs if self._is_relative_to(resolved, pack.root)]
            if containing:
                deepest = max(len(pack.root.parts) for pack in containing)
                path_matches.extend(pack for pack in containing if len(pack.root.parts) == deepest)
        unique_paths = self._unique(path_matches)
        named = []
        for pack in packs:
            names = {
                pack.manifest.project_id.casefold(),
                pack.manifest.name.casefold(),
                *(alias.casefold() for alias in pack.manifest.aliases),
            }
            if any(self._mentions(instruction, name) for name in names):
                named.append(pack)
        unique_named = self._unique(named)
        if unique_paths and unique_named:
            path_ids = {pack.manifest.project_id for pack in unique_paths}
            named_ids = {pack.manifest.project_id for pack in unique_named}
            agreed = [
                pack for pack in unique_paths if pack.manifest.project_id in path_ids & named_ids
            ]
            if len(agreed) == 1 and len(path_ids | named_ids) == 1:
                return self._single_or_hold(agreed, "artifact path and instruction agree")
            return ProjectResolution(
                status=ProjectResolutionStatus.HOLD,
                reason="artifact path와 instruction의 project 신호가 충돌합니다.",
                candidates=[pack.ref for pack in self._unique([*unique_paths, *unique_named])],
            )
        if unique_paths:
            return self._single_or_hold(unique_paths, "artifact path containment")
        if unique_named:
            return self._single_or_hold(unique_named, "project name in instruction")

        defaults = [pack for pack in packs if pack.manifest.default]
        if len(defaults) == 1:
            return ProjectResolution(
                status=ProjectResolutionStatus.RESOLVED,
                project=defaults[0].ref,
                reason="unique default project",
                candidates=[defaults[0].ref],
            )
        return ProjectResolution(
            status=ProjectResolutionStatus.HOLD,
            reason="project를 안전하게 확정할 근거가 없습니다.",
            candidates=[pack.ref for pack in packs],
        )

    @staticmethod
    def _matches_hint(pack: ProjectPack, hint: str) -> bool:
        normalized = hint.casefold()
        return normalized in {
            pack.manifest.project_id.casefold(),
            pack.manifest.namespace.casefold(),
            *(alias.casefold() for alias in pack.manifest.aliases),
        }

    @staticmethod
    def _mentions(instruction: str, name: str) -> bool:
        return (
            re.search(
                rf"(?<![a-z0-9_-]){re.escape(name.casefold())}(?![a-z0-9_-])",
                instruction.casefold(),
                flags=re.UNICODE,
            )
            is not None
        )

    @staticmethod
    def _unique(packs: list[ProjectPack]) -> list[ProjectPack]:
        return list({pack.manifest.project_id: pack for pack in packs}.values())

    @staticmethod
    def _is_relative_to(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root.resolve())
        except ValueError:
            return False
        return True

    def _single_or_hold(self, packs: list[ProjectPack], reason: str) -> ProjectResolution:
        unique = self._unique(packs)
        if len(unique) == 1:
            return ProjectResolution(
                status=ProjectResolutionStatus.RESOLVED,
                project=unique[0].ref,
                reason=reason,
                candidates=[unique[0].ref],
            )
        suffix = "일치하는 project가 없습니다." if not unique else "project 판별이 모호합니다."
        return ProjectResolution(
            status=ProjectResolutionStatus.HOLD,
            reason=f"{reason}: {suffix}",
            candidates=[pack.ref for pack in unique],
        )
