"""Conformance suites and their recorded results (Work 034 S0, spec M-2).

A module may be enabled only after its kind's suite passed for that exact ``(module_id, version)``
(``MODULE_UNQUALIFIED`` otherwise). This PR keeps results in memory behind
:class:`ConformanceLedger`; a store-backed ledger replaces it without changing callers.

Store record shape a store-backed ledger would write (not a store kind yet; one record per run,
append-only, the latest record for a key decides)::

    {
      "schema_version": "3.0.0",
      "conformance_result_id": "<id>",
      "module_id": "<module_id>",
      "version": "<version>",
      "kind": "<kind>",
      "interface_version": <int>,
      "suite_id": "<suite id>",
      "outcome": "pass" | "fail",
      "failure": {"code": "<RuntimeFault code>", "message": "<text>"} | null,
      "recorded_at": "<RFC 3339 UTC>"
    }
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..errors import RuntimeFault
from .spec import INTERFACE_VERSIONS, Module, ModuleSpec

# A suite check raises RuntimeFault (or any exception) on a non-conforming implementation.
Check = Callable[[Module], None]


@dataclass(frozen=True)
class ConformanceSuite:
    suite_id: str
    kind: str
    interface_version: int
    check: Check


@dataclass(frozen=True)
class ConformanceResult:
    module_id: str
    version: str
    kind: str
    interface_version: int
    suite_id: str
    passed: bool
    failure: dict[str, str] | None = None


class ConformanceLedger:
    """Registered suites per kind and the latest result per ``(module_id, version)``."""

    def __init__(self) -> None:
        self._suites: dict[str, ConformanceSuite] = {}
        self._results: dict[tuple[str, str], ConformanceResult] = {}

    def register_suite(self, suite: ConformanceSuite) -> None:
        if suite.kind not in INTERFACE_VERSIONS:
            raise RuntimeFault("MODULE_SPEC", "unknown module kind", details={"kind": suite.kind})
        if suite.interface_version != INTERFACE_VERSIONS[suite.kind]:
            raise RuntimeFault(
                "MODULE_INTERFACE",
                "suite targets another interface version",
                details={"suite_id": suite.suite_id, "declared": suite.interface_version},
            )
        if suite.suite_id in self._suites:
            raise RuntimeFault(
                "CONFORMANCE_SUITE_DUPLICATE",
                "suite id already registered",
                details={"suite_id": suite.suite_id},
            )
        self._suites[suite.suite_id] = suite

    def suite(self, suite_id: str) -> ConformanceSuite:
        try:
            return self._suites[suite_id]
        except KeyError:
            raise RuntimeFault(
                "CONFORMANCE_SUITE_UNKNOWN", "no such suite", details={"suite_id": suite_id}
            ) from None

    def run(self, spec: ModuleSpec, implementation: Module) -> ConformanceResult:
        """Run the spec's suite against ``implementation`` and record the outcome (pass or fail)."""
        suite = self.suite(spec.conformance_suite)
        if suite.kind != spec.kind:
            raise RuntimeFault(
                "CONFORMANCE_SUITE_KIND",
                "suite is for another kind",
                details={"suite_id": suite.suite_id, "suite_kind": suite.kind, "kind": spec.kind},
            )
        failure: dict[str, str] | None = None
        try:
            if implementation.spec != spec:
                raise RuntimeFault("CONFORMANCE_SPEC", "implementation reports another spec")
            suite.check(implementation)
        except RuntimeFault as fault:
            failure = {"code": fault.code, "message": fault.message}
        except Exception as exc:  # a suite failure of any shape is a fail, never a pass
            failure = {"code": "CONFORMANCE_FAILED", "message": type(exc).__name__}
        result = ConformanceResult(
            module_id=spec.module_id,
            version=spec.version,
            kind=spec.kind,
            interface_version=spec.interface_version,
            suite_id=suite.suite_id,
            passed=failure is None,
            failure=failure,
        )
        self._results[spec.key] = result
        return result

    def latest(self, module_id: str, version: str) -> ConformanceResult | None:
        return self._results.get((module_id, version))

    def passed(self, spec: ModuleSpec) -> bool:
        """True only if the latest result for this exact spec is a pass of its declared suite."""
        result = self._results.get(spec.key)
        return (
            result is not None
            and result.passed
            and result.kind == spec.kind
            and result.suite_id == spec.conformance_suite
            and result.interface_version == spec.interface_version
        )
