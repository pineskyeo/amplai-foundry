"""``check.module_boundary``: the import boundary lint (Work 034 M-12, M-16; protected).

A static scan of every ``import`` (module level and inside functions) under
``src/amplai_foundry``. The module-to-file map comes from the registry: :data:`BUILTIN_CODE`
names each module's implementation file (and its conformance subject), :data:`KIND_PACKAGES`
names each kind's package. Two rules:

- ``MODULE_IMPORT_IMPLEMENTATION``: only the module itself and a composition root (config
  ``composition_roots``, today ``runtime/local_deployment.py``) import a module's implementation.
  Everything else, the kind's own package included, reaches it through the kind's interface.
- ``MODULE_IMPORT_OTHER_KIND``: a module's implementation imports no other kind's package. A
  module spec declares no dependencies in v1 (M-12 "declared dependency"): none is allowed.

Modules without their own file (drivers, strategies: still fixed lists) are listed as not covered.
A dynamic import (``importlib``) is not seen; the module loader is the only one in this package.
"""

from __future__ import annotations

import ast
from pathlib import Path

from ..modules.builtin import BUILTIN_CODE, BUILTIN_MODULES, KIND_PACKAGES
from .interface import BuiltinCheck, CheckContext, CheckResult, Finding, result

PACKAGE = "amplai_foundry"


def module_name(path: Path, src: Path) -> tuple[str, bool]:
    """The dotted name of a source file and whether it is a package ``__init__``."""
    parts = list(path.relative_to(src).with_suffix("").parts)
    if parts[-1] == "__init__":
        return ".".join(parts[:-1]), True
    return ".".join(parts), False


def imported_names(tree: ast.AST, name: str, is_package: bool) -> list[tuple[str, int]]:
    """Every absolute module name an import may load, with its line: ``import a.b`` gives
    ``a.b``; ``from a import b`` gives ``a`` and ``a.b`` (``b`` may be a submodule)."""
    out: list[tuple[str, int]] = []
    package = name if is_package else name.rpartition(".")[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                if node.level - 1 >= len(parts):
                    continue
                base = ".".join(parts[: len(parts) - (node.level - 1)])
                base = f"{base}.{node.module}" if node.module else base
            else:
                base = node.module or ""
            out.append((base, node.lineno))
            out.extend((f"{base}.{alias.name}", node.lineno) for alias in node.names)
    return [(n, line) for n, line in out if n == PACKAGE or n.startswith(PACKAGE + ".")]


def _prefix_hit(name: str, table: dict[str, frozenset[str]]) -> frozenset[str]:
    parts = name.split(".")
    for end in range(len(parts), 0, -1):
        hit = table.get(".".join(parts[:end]))
        if hit is not None:
            return hit
    return frozenset()


def kind_of(name: str) -> str | None:
    for kind, package in KIND_PACKAGES.items():
        if name == package or name.startswith(package + "."):
            return kind
    return None


class ModuleBoundaryCheck(BuiltinCheck):
    def run(self, context: CheckContext) -> CheckResult:
        src = context.root / "src"
        kinds = {s.module_id: s.kind for s in BUILTIN_MODULES}
        owners: dict[str, set[str]] = {}
        for module_id, code in BUILTIN_CODE.items():
            if module_id in kinds:
                for file in code.files():
                    owners.setdefault(file, set()).add(module_id)
        table = {k: frozenset(v) for k, v in owners.items()}
        roots = set(self.check.config.get("composition_roots", ()))
        findings: list[Finding] = []
        seen: set[tuple[str, str, str]] = set()
        files = sorted((src / PACKAGE).rglob("*.py"))
        if not files:  # a wrong root would otherwise pass with nothing read
            findings.append(Finding("BOUNDARY_NO_SOURCE", f"no source under src/{PACKAGE}",
                                    details={"root": str(context.root)}))  # fmt: skip
        for path in files:
            name, is_package = module_name(path, src)
            own = table.get(name, frozenset())
            own_kinds = {kinds[m] for m in own}
            rel = path.relative_to(context.root).as_posix()
            try:
                tree = ast.parse(path.read_bytes(), filename=rel)
            except SyntaxError as exc:
                findings.append(Finding("BOUNDARY_UNPARSABLE", str(exc), rel))
                continue
            for target, line in imported_names(tree, name, is_package):
                hit = _prefix_hit(target, table)
                if hit and not hit & own and name not in roots:
                    key = (rel, ",".join(sorted(hit)), "impl")
                    if key not in seen:
                        seen.add(key)
                        findings.append(Finding(
                            "MODULE_IMPORT_IMPLEMENTATION",
                            f"{name} imports the implementation of {', '.join(sorted(hit))}; "
                            "go through the kind's interface or the composition root",
                            rel, {"line": line, "imports": target, "modules": sorted(hit)},
                        ))  # fmt: skip
                target_kind = kind_of(target)
                if own and target_kind is not None and target_kind not in own_kinds:
                    key = (rel, target_kind, "kind")
                    if key not in seen:
                        seen.add(key)
                        findings.append(Finding(
                            "MODULE_IMPORT_OTHER_KIND",
                            f"{name} (module {', '.join(sorted(own))}) imports the {target_kind} "
                            "kind; a module declares no dependency in v1",
                            rel, {"line": line, "imports": target, "kind": target_kind},
                        ))  # fmt: skip
        covered = sorted(m for m in kinds if m in BUILTIN_CODE)
        return result(
            findings,
            files=len(files),
            modules_covered=covered,
            modules_not_covered=sorted(m for m in kinds if m not in BUILTIN_CODE),
            composition_roots=sorted(roots),
        )
