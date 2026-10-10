"""Code-module identity: kinds, interface versions, placement (Work 034 S0, spec M-1/M-2/M-5).

A code module is the implementation a ``harness-component`` record points at (``module_id@version``,
M-1). The component record stays the source of truth for on/off and version; this module only says
what a code module *is* and where it may run.

``intake_adapter`` and ``model_gateway`` handle outside input, so they never run in the Control
Plane process that holds the authority key (M-5, design/02 §4, AC-M6): their specs must say
``out_of_process`` and the registry refuses to admit them in process.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Protocol, get_args

from ..errors import RuntimeFault

Placement = Literal["in_process", "out_of_process"]
PLACEMENTS: tuple[str, ...] = get_args(Placement)

# The closed set of first kinds (plan.md §3.1). A new kind is a new row here and a new suite.
KINDS: tuple[str, ...] = (
    "driver", "strategy", "decider", "judge", "knowledge", "doc_freshness", "intake_adapter",
    "model_gateway",
)  # fmt: skip
# The interface version a module of each kind must implement (M-2). A module declaring another
# version is refused; raising a row is an interface change and needs a new conformance suite.
INTERFACE_VERSIONS: dict[str, int] = {kind: 1 for kind in KINDS}
# Kinds that take outside input: separate process or container, API-only to the Control Plane (M-5).
OUT_OF_PROCESS_KINDS: frozenset[str] = frozenset({"intake_adapter", "model_gateway"})

_ID = re.compile(r"[a-z][a-z0-9_.-]{0,127}")
_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z._+-]{0,63}")
_PERMISSION = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+")


def _bad(why: str, details: object = None) -> RuntimeFault:
    return RuntimeFault("MODULE_SPEC", why, details=details)


@dataclass(frozen=True)
class ModuleSpec:
    """What a code module is. Immutable; ``(module_id, version)`` names exactly one spec."""

    kind: str
    module_id: str
    version: str
    interface_version: int
    conformance_suite: str
    permissions: frozenset[str]
    placement: Placement

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise _bad("unknown module kind", {"kind": self.kind, "kinds": list(KINDS)})
        for name, value, pattern in (
            ("module_id", self.module_id, _ID),
            ("version", self.version, _VERSION),
            ("conformance_suite", self.conformance_suite, _ID),
        ):
            if not isinstance(value, str) or not pattern.fullmatch(value):
                raise _bad(f"{name} is malformed", {name: value})
        if type(self.interface_version) is not int:
            raise _bad("interface_version is an integer")
        if self.interface_version != INTERFACE_VERSIONS[self.kind]:
            raise RuntimeFault(
                "MODULE_INTERFACE",
                f"{self.kind} modules implement interface version {INTERFACE_VERSIONS[self.kind]}",
                details={"module_id": self.module_id, "declared": self.interface_version},
            )
        if not isinstance(self.permissions, frozenset) or not all(
            isinstance(p, str) and _PERMISSION.fullmatch(p) for p in self.permissions
        ):
            raise _bad("permissions are a frozenset of dotted permission names")
        if self.placement not in PLACEMENTS:
            raise _bad("unknown placement", {"placement": self.placement})
        if self.kind in OUT_OF_PROCESS_KINDS and self.placement != "out_of_process":
            raise RuntimeFault(
                "MODULE_PLACEMENT",
                f"{self.kind} modules run out of process (M-5)",
                details={"module_id": self.module_id},
            )

    @property
    def key(self) -> tuple[str, str]:
        return (self.module_id, self.version)

    @property
    def ref(self) -> str:
        """The ``module_id@version`` form a component record carries (plan.md §3.1)."""
        return f"{self.module_id}@{self.version}"


class Module(Protocol):
    """The minimal base every module implementation exposes; per-kind Protocols come later.

    ``spec`` ties the implementation object to the spec its conformance pass was recorded for.
    """

    @property
    def spec(self) -> ModuleSpec: ...
