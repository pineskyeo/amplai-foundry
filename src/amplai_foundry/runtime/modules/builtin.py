"""The static in-package list of built-in modules (Work 034 S0, spec M-3, M-8).

Declarative only in this PR: nothing in the runtime reads the registry yet, and wrapping each fixed
list behind a built-in module is a separate behavior-neutral PR (plan.md §3.1, AC-M2). The entries
mirror the fixed lists that exist today: the task-class baseline driver order
(``execution/product.py`` ``ROUTER_ORDER``) and the strategy catalogue (``execution/policies.py``
``STRATEGIES``).

``permissions`` is empty for every entry until each wrapping PR states what its module needs; the
spec does not list per-module permissions for today's drivers and strategies.
"""

from __future__ import annotations

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


BUILTIN_MODULES: tuple[ModuleSpec, ...] = (
    *(_builtin("driver", driver) for driver in ROUTER_ORDER),
    *(_builtin("strategy", strategy) for strategy in STRATEGIES),
)
