"""``check.kind_conformance``: every registered module passes its kind's suite (M-2, M-13, M-16).

For each registered module whose kind has a conformance suite (:data:`KIND_SUITES`), the check
builds the module with its test transport (:data:`BUILTIN_CODE` ``conformance_subject``; a check
module is its own subject, its suite is static) and runs the suite through the module registry's
:class:`ConformanceLedger`, the same path ``ModuleRegistry.enable`` reads (``MODULE_UNQUALIFIED``).

- ``CONFORMANCE_FAILED``: a module did not pass (the suite's failure code is in the details).
- ``CONFORMANCE_NO_SUBJECT``: a module of a kind with a suite has no way to be built for it.
- Modules of a kind without a suite (driver, strategy today) are listed as ``no_suite``.

With ``--target kind:<kind>`` only that kind's modules run (the ``enable`` moment of one module).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..modules.builtin import BUILTIN_CODE, KIND_SUITES
from ..modules.conformance import ConformanceLedger, ConformanceSuite
from ..modules.loader import load
from ..modules.registry import ModuleRegistry
from ..modules.spec import Module, ModuleSpec
from .interface import BuiltinCheck, CheckContext, CheckResult, Finding, covers, result


def _subject(spec: ModuleSpec, workdir: Path) -> Module | None:
    code = BUILTIN_CODE.get(spec.module_id)
    if code is None:
        return None
    if spec.kind == "check" and code.entry is not None:
        from .builtin import BUILTIN_CHECKS  # declarations only; no implementation

        (declaration,) = (c for c in BUILTIN_CHECKS if c.check_id == spec.module_id)
        factory: Any = load(code.entry)
        built: Module = factory(spec, declaration)
        return built
    if code.conformance_subject is None:
        return None
    make: Any = load(code.conformance_subject)
    made: Module = make(workdir)
    return made


class KindConformanceCheck(BuiltinCheck):
    def run(self, context: CheckContext) -> CheckResult:
        ledger = ConformanceLedger()
        for ref in KIND_SUITES.values():
            suite = load(ref)
            if not isinstance(suite, ConformanceSuite):
                raise TypeError(f"{ref} is not a ConformanceSuite")
            ledger.register_suite(suite)
        registry = ModuleRegistry(ledger)
        wanted = tuple(t for t in context.targets if t.startswith("kind:"))
        findings: list[Finding] = []
        passed: list[str] = []
        no_suite: list[str] = []
        for spec in registry.modules():
            if wanted and not any(covers((f"kind:{spec.kind}",), t) or t == "kind:*"
                                  for t in wanted):  # fmt: skip
                continue
            if spec.kind not in KIND_SUITES:
                no_suite.append(spec.ref)
                continue
            workdir = context.workdir / spec.module_id
            workdir.mkdir(parents=True, exist_ok=True)
            subject = _subject(spec, workdir)
            if subject is None:
                findings.append(Finding("CONFORMANCE_NO_SUBJECT",
                                        f"{spec.ref} has no conformance subject",
                                        details={"module": spec.ref}))  # fmt: skip
                continue
            outcome = ledger.run(spec, subject)
            if outcome.passed and registry.enable(*spec.key) == spec:
                passed.append(f"{spec.ref} {outcome.suite_id}")
            else:
                findings.append(Finding(
                    "CONFORMANCE_FAILED", f"{spec.ref} did not pass {outcome.suite_id}",
                    details={"module": spec.ref, "failure": outcome.failure},
                ))  # fmt: skip
        return result(findings, passed=passed, no_suite=no_suite)
