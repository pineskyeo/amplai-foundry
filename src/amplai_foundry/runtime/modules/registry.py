"""The module registry: built-ins plus verified pinned extensions (Work 034 S0, M-2/M-3/M-5).

The registry knows which code modules exist and gates their use; it does not hold on/off state.
That stays in the ``harness-component`` record (M-1), which a later PR points at
``module_id@version``.

- Sources: :data:`BUILTIN_MODULES` and, only with a :class:`PinnedManifest`, the extensions that
  :func:`verify_extensions` accepts. Installed entry points are never scanned (AC-M3).
- :meth:`ModuleRegistry.enable` is the gate a component change calls: the latest recorded
  conformance result for that exact spec must be a pass, else Hold ``MODULE_UNQUALIFIED`` (M-2).
- :meth:`ModuleRegistry.admit_in_process` additionally refuses out-of-process placements, so an
  ``intake_adapter`` or ``model_gateway`` never loads into the Control Plane process (AC-M6).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path

from ..errors import Hold, RuntimeFault
from .builtin import BUILTIN_MODULES
from .conformance import ConformanceLedger
from .pinned import PinnedManifest, PinnedModule, verify_extensions
from .spec import ModuleSpec


class ModuleRegistry:
    def __init__(
        self,
        conformance: ConformanceLedger,
        *,
        builtins: Iterable[ModuleSpec] = BUILTIN_MODULES,
        manifest: PinnedManifest | None = None,
        extension_roots: Sequence[Path] = (),
    ) -> None:
        if manifest is None and extension_roots:
            raise RuntimeFault("MODULE_MANIFEST", "extension roots need a pinned manifest")
        self.conformance = conformance
        self._specs: dict[tuple[str, str], ModuleSpec] = {}
        self._builtin: set[tuple[str, str]] = set()
        self._extensions: dict[tuple[str, str], PinnedModule] = {}
        for spec in builtins:
            self._add(spec)
            self._builtin.add(spec.key)
        builtin_ids = {module_id for module_id, _ in self._builtin}
        extensions = verify_extensions(manifest, extension_roots) if manifest else ()
        for module in extensions:
            if module.spec.module_id in builtin_ids:
                raise RuntimeFault(
                    "MODULE_DUPLICATE",
                    "an extension may not reuse a built-in module id",
                    details={"module_id": module.spec.module_id},
                )
            self._add(module.spec)
            self._extensions[module.spec.key] = module

    def _add(self, spec: ModuleSpec) -> None:
        if not isinstance(spec, ModuleSpec):
            raise RuntimeFault("MODULE_SPEC", "registry entries are ModuleSpec")
        if spec.key in self._specs:
            raise RuntimeFault(
                "MODULE_DUPLICATE", "module registered twice", details={"module": spec.ref}
            )
        self._specs[spec.key] = spec

    def modules(self, kind: str | None = None) -> tuple[ModuleSpec, ...]:
        return tuple(s for s in self._specs.values() if kind is None or s.kind == kind)

    def is_builtin(self, module_id: str, version: str) -> bool:
        return (module_id, version) in self._builtin

    def get(self, module_id: str, version: str) -> ModuleSpec:
        try:
            return self._specs[(module_id, version)]
        except KeyError:
            raise RuntimeFault(
                "MODULE_UNKNOWN",
                "no such module",
                details={"module": f"{module_id}@{version}"},
            ) from None

    def extension(self, module_id: str, version: str) -> PinnedModule | None:
        """The pinned package and entry of an extension module; ``None`` for a built-in."""
        self.get(module_id, version)
        return self._extensions.get((module_id, version))

    def enable(self, module_id: str, version: str) -> ModuleSpec:
        spec = self.get(module_id, version)
        if not self.conformance.passed(spec):
            latest = self.conformance.latest(module_id, version)
            raise Hold(
                "MODULE_UNQUALIFIED",
                "module has no passing conformance result",
                details={
                    "module": spec.ref,
                    "suite_id": spec.conformance_suite,
                    "latest": None
                    if latest is None
                    else {
                        "passed": latest.passed,
                        "suite_id": latest.suite_id,
                        "failure": latest.failure,
                    },
                },
            )
        return spec

    def admit_in_process(self, module_id: str, version: str) -> ModuleSpec:
        spec = self.get(module_id, version)
        if spec.placement != "in_process":
            raise RuntimeFault(
                "MODULE_PLACEMENT",
                f"{spec.kind} modules run out of process (M-5)",
                details={"module": spec.ref},
            )
        return self.enable(module_id, version)
