"""The static in-package list of built-in modules (Work 034 S0, spec M-3, M-8).

Declarative only in this PR: nothing in the runtime reads the registry yet, and wrapping each fixed
list behind a built-in module is a separate behavior-neutral PR (plan.md §3.1, AC-M2). The entries
mirror the fixed lists that exist today: the task-class baseline driver order
(``execution/product.py`` ``ROUTER_ORDER``) and the strategy catalogue (``execution/policies.py``
``STRATEGIES``), plus the first ``notifier`` (Work 034 S1c), the first ``git_host``
(Work 034 OD-15) and the built-in ``check`` modules (Work 034 OD-18, M-16).

``permissions`` is empty for every entry until each wrapping PR states what its module needs; the
spec does not list per-module permissions for today's drivers and strategies.

:data:`BUILTIN_CODE` says where a built-in module's code lives, for each module that has its own
file (the ``notifier``, ``git_host`` and ``check`` modules; drivers and strategies are still the
fixed lists above). The checks read it: the boundary lint (M-12) derives its module-to-file map
from it, the conformance check builds each module with its test transport, and the removal test
(M-13) knows which file to hide and which configuration key turns the module off. Code named here
is loaded only through :func:`.loader.load` (no entry point is scanned, M-3).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..execution.policies import STRATEGIES
from ..execution.product import ROUTER_ORDER
from .spec import INTERFACE_VERSIONS, ModuleSpec

BUILTIN_VERSION = "1"


def suite_id(kind: str) -> str:
    """The conformance suite id of a kind at its current interface version."""
    return f"conformance.{kind}.v{INTERFACE_VERSIONS[kind]}"


def _builtin(kind: str, name: str) -> ModuleSpec:
    return ModuleSpec(
        kind=kind,
        module_id=f"{kind}.{name}",
        version=BUILTIN_VERSION,
        interface_version=INTERFACE_VERSIONS[kind],
        conformance_suite=suite_id(kind),
        permissions=frozenset(),
        placement="in_process",
    )


# Work 034 OD-18 (M-16): the built-in checks, each a module of its own (``runtime/checks/``)
BUILTIN_CHECK_NAMES: tuple[str, ...] = (
    "module_boundary", "kind_conformance", "module_removal", "secret_exposure", "policy",
    "tool_ruff", "tool_ruff_format", "tool_mypy", "tool_pytest", "evaluator_qsuite",
)  # fmt: skip

BUILTIN_MODULES: tuple[ModuleSpec, ...] = (
    *(_builtin("driver", driver) for driver in ROUTER_ORDER),
    *(_builtin("strategy", strategy) for strategy in STRATEGIES),
    # Work 034 S1c (F-5, OD-9, Q-10): the operator's direct alert, a Slack Incoming Webhook
    # (``runtime/notifier/slack.py``). Outbound only, so it runs in process.
    _builtin("notifier", "slack_webhook"),
    # Work 034 OD-15 (Q-15): the code host, the operator's ``gh`` and ``git`` against GitHub
    # (``runtime/git_host/github.py``). It runs host commands only, so it runs in process.
    _builtin("git_host", "github"),
    *(_builtin("check", name) for name in BUILTIN_CHECK_NAMES),
)


@dataclass(frozen=True)
class BuiltinCode:
    """Where a built-in module's code lives (Work 034 M-16).

    - ``implementation``: the Python module that is the implementation. Only that module, its
      conformance subject and a composition root may import it (boundary lint, M-12).
    - ``entry``: ``module:attribute`` the loader builds the module from (checks).
    - ``conformance_subject``: ``module:factory``; ``factory(workdir)`` returns the implementation
      wired to its kind's test transport, for the kind conformance check (M-2, M-13).
    - ``config_key``: the ``local.json`` key whose ``null`` turns the module off; the removal test
      (M-13) runs the core tests with that key off and the implementation unimportable.
    """

    implementation: str
    entry: str | None = None
    conformance_subject: str | None = None
    config_key: str | None = None

    def files(self) -> tuple[str, ...]:
        """The Python modules that belong to this module (implementation, conformance subject)."""
        subject = self.conformance_subject
        return (self.implementation,) + ((subject.split(":", 1)[0],) if subject else ())


_CHECKS = "amplai_foundry.runtime.checks"
_CHECK_ENTRIES: dict[str, tuple[str, str]] = {
    "module_boundary": ("boundary", "ModuleBoundaryCheck"),
    "kind_conformance": ("kind_conformance", "KindConformanceCheck"),
    "module_removal": ("removal", "ModuleRemovalCheck"),
    "secret_exposure": ("secret_exposure", "SecretExposureCheck"),
    "policy": ("policy", "CheckPolicyCheck"),
    "tool_ruff": ("tools", "RuffCheck"),
    "tool_ruff_format": ("tools", "RuffFormatCheck"),
    "tool_mypy": ("tools", "MypyCheck"),
    "tool_pytest": ("tools", "PytestCheck"),
    "evaluator_qsuite": ("tools", "EvaluatorQSuiteCheck"),
}

BUILTIN_CODE: dict[str, BuiltinCode] = {
    "notifier.slack_webhook": BuiltinCode(
        implementation="amplai_foundry.runtime.notifier.slack",
        conformance_subject="amplai_foundry.runtime.notifier.slack_conformance:subject",
        config_key="notifier",
    ),
    "git_host.github": BuiltinCode(
        implementation="amplai_foundry.runtime.git_host.github",
        conformance_subject="amplai_foundry.runtime.git_host.github_conformance:subject",
        config_key="git_host",
    ),
    **{
        f"check.{name}": BuiltinCode(
            implementation=f"{_CHECKS}.{file}", entry=f"{_CHECKS}.{file}:{attribute}"
        )
        for name, (file, attribute) in _CHECK_ENTRIES.items()
    },
}

# The package of each kind that has its own code: the kind's interface, with its implementations
# as submodules. Code that is not the module reaches a kind only through this package's interface.
KIND_PACKAGES: dict[str, str] = {
    "notifier": "amplai_foundry.runtime.notifier",
    "git_host": "amplai_foundry.runtime.git_host",
    "check": _CHECKS,
}
# The conformance suite of each kind that has one (``module:attribute``, M-2)
KIND_SUITES: dict[str, str] = {
    "notifier": "amplai_foundry.runtime.notifier.interface:SUITE",
    "git_host": "amplai_foundry.runtime.git_host.interface:SUITE",
    "check": f"{_CHECKS}.interface:SUITE",
}
