"""Domain module graph and lock validation for Project Packs."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from amplai_foundry.projects.models import DomainModuleManifest, version_satisfies
from amplai_foundry.projects.repository import ProjectPack


class DomainRegistryError(RuntimeError):
    """Domain manifests do not form a coherent, acyclic registry."""


class DomainRegistry:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def load(self) -> dict[str, DomainModuleManifest]:
        modules: dict[str, DomainModuleManifest] = {}
        if not self.root.exists():
            return modules
        for path in sorted(self.root.glob("*/module.yaml")):
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
                module = DomainModuleManifest.model_validate(data)
            except (OSError, yaml.YAMLError, ValidationError) as error:
                raise DomainRegistryError(f"{path}: {error}") from error
            if module.module in modules:
                raise DomainRegistryError(f"domain module이 중복됩니다: {module.module}")
            modules[module.module] = module
        return modules

    def validate_graph(self) -> list[str]:
        modules = self.load()
        issues: list[str] = []
        for module in modules.values():
            for imported in module.imports:
                target = modules.get(imported.module)
                if target is None:
                    issues.append(
                        f"domain import target missing: {module.module} -> {imported.module}"
                    )
                elif not version_satisfies(target.version, imported.version):
                    issues.append(
                        f"domain import version mismatch: {module.module} -> {imported.module} "
                        f"requested={imported.version} actual={target.version}"
                    )
        issues.extend(self._cycle_issues(modules))
        return sorted(set(issues))

    def validate_pack(self, pack: ProjectPack) -> list[str]:
        modules = self.load()
        issues: list[str] = []
        for entry in pack.domain_lock.imports:
            module = modules.get(entry.module)
            if module is None:
                issues.append(f"locked domain module이 registry에 없습니다: {entry.module}")
            elif module.version != entry.version:
                issues.append(
                    f"locked domain version과 registry가 다릅니다: {entry.module} "
                    f"locked={entry.version} registry={module.version}"
                )
        return issues

    @staticmethod
    def _cycle_issues(modules: dict[str, DomainModuleManifest]) -> list[str]:
        issues: list[str] = []
        visiting: list[str] = []
        visited: set[str] = set()

        def visit(module_id: str) -> None:
            if module_id in visiting:
                start = visiting.index(module_id)
                cycle = [*visiting[start:], module_id]
                issues.append("domain import cycle: " + " -> ".join(cycle))
                return
            if module_id in visited or module_id not in modules:
                return
            visiting.append(module_id)
            for imported in modules[module_id].imports:
                visit(imported.module)
            visiting.pop()
            visited.add(module_id)

        for module_id in sorted(modules):
            visit(module_id)
        return issues
