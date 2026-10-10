"""Code modules: specs, conformance, pinned extensions, registry (Work 034 S0)."""

from __future__ import annotations

from .builtin import BUILTIN_MODULES, suite_id
from .conformance import ConformanceLedger, ConformanceResult, ConformanceSuite
from .pinned import PinnedManifest, PinnedModule, PinnedPackage, verify_extensions
from .registry import ModuleRegistry
from .spec import (
    INTERFACE_VERSIONS,
    KINDS,
    OUT_OF_PROCESS_KINDS,
    Module,
    ModuleSpec,
    Placement,
)

__all__ = [
    "BUILTIN_MODULES",
    "INTERFACE_VERSIONS",
    "KINDS",
    "OUT_OF_PROCESS_KINDS",
    "ConformanceLedger",
    "ConformanceResult",
    "ConformanceSuite",
    "Module",
    "ModuleRegistry",
    "ModuleSpec",
    "PinnedManifest",
    "PinnedModule",
    "PinnedPackage",
    "Placement",
    "suite_id",
    "verify_extensions",
]
