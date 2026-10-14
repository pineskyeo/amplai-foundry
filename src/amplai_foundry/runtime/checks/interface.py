"""The ``check`` module kind, interface v1 (Work 034 OD-18; spec M-16, M-17, M-19, M-20).

A check is a module that judges other code: a lint, a test run, a scan. Each check declares
(:class:`CheckSpec`):

- ``check_id`` — its module id (``check.<name>``), and ``version`` — its module version;
- ``targets`` — what it covers: ``core``, ``kind:<kind>`` (``kind:*`` for every kind) or
  ``pack:<pack id>`` (M-16);
- ``when`` — the moments it runs at: ``ci`` (a PR's CI), ``enable`` (turning a module on),
  ``pack_install`` (installing or updating a pack), ``upgrade`` (S3) (M-17);
- ``required`` — a failure at one of those moments stops the action (Hold ``CHECK_FAILED`` with
  the check id and result, M-17); an optional check's failure is reported only;
- ``protected`` — a base protection check (permissions, module isolation, secrets): a pack can not
  turn it off and a caller can not skip it (M-19);
- ``config`` — the check's settings as lists of strings. Keys in ``loosens_when_added`` are allow
  lists (an added item loosens the check); for every other key a removed item loosens it. The
  check policy check uses this to tell tightening from loosening (M-19).

Interface v1::

    run(context) -> CheckResult

``run`` returns ``pass`` or ``fail`` with findings; an exception is the runner's ``error``.
``conformance.check.v1`` (:data:`SUITE`) is static: the module has ``run`` and its ``check``
declaration names the module's own id and version. It never runs the check.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, get_args

from ..errors import RuntimeFault
from ..modules.builtin import suite_id
from ..modules.conformance import ConformanceSuite
from ..modules.spec import INTERFACE_VERSIONS, KINDS, Module, ModuleSpec

KIND = "check"

When = Literal["ci", "enable", "pack_install", "upgrade"]
WHEN: tuple[str, ...] = get_args(When)
Status = Literal["pass", "fail", "error", "skipped"]
STATUSES: tuple[str, ...] = get_args(Status)

_TARGET = re.compile(r"core|kind:(\*|[a-z][a-z_]*)|pack:[a-z][a-z0-9_.-]{0,127}")
_KEY = re.compile(r"[a-z][a-z0-9_]{0,63}")


def _bad(why: str, details: object = None) -> RuntimeFault:
    return RuntimeFault("CHECK_SPEC", why, details=details)


def target_ok(target: str) -> bool:
    if not _TARGET.fullmatch(target):
        return False
    return not target.startswith("kind:") or target in ("kind:*", *(f"kind:{k}" for k in KINDS))


def covers(targets: frozenset[str] | tuple[str, ...], target: str) -> bool:
    """True when ``targets`` covers ``target`` (``kind:*`` covers every ``kind:<kind>``)."""
    return target in targets or (target.startswith("kind:") and "kind:*" in targets)


@dataclass(frozen=True)
class CheckSpec:
    """What a check declares (M-16, M-17, M-19). Immutable; ``check_id`` is its module id."""

    check_id: str
    version: str
    targets: tuple[str, ...]
    when: frozenset[str]
    required: bool
    protected: bool
    description: str
    config: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    loosens_when_added: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not isinstance(self.check_id, str) or not self.check_id.startswith(f"{KIND}."):
            raise _bad("a check id is its module id, check.<name>", {"check_id": self.check_id})
        if not self.targets or not all(isinstance(t, str) and target_ok(t) for t in self.targets):
            raise _bad("targets are core, kind:<kind>, kind:* or pack:<id>",
                       {"check_id": self.check_id, "targets": list(self.targets)})  # fmt: skip
        if not self.when or not set(self.when) <= set(WHEN):
            raise _bad("when is a non-empty subset of " + ", ".join(WHEN),
                       {"check_id": self.check_id, "when": sorted(self.when)})  # fmt: skip
        if type(self.required) is not bool or type(self.protected) is not bool:
            raise _bad("required and protected are booleans", {"check_id": self.check_id})
        if self.protected and not self.required:
            raise _bad("a protected check is required", {"check_id": self.check_id})
        for key, values in self.config.items():
            if not _KEY.fullmatch(key) or not isinstance(values, tuple):
                raise _bad("config is key -> tuple of strings", {"key": key})
            if not all(isinstance(v, str) for v in values) or len(set(values)) != len(values):
                raise _bad("config values are distinct strings", {"key": key})
        if not self.loosens_when_added <= set(self.config):
            raise _bad("loosens_when_added names config keys", {"check_id": self.check_id})

    def entry(self) -> dict[str, Any]:
        """The lock form (``checks.lock.json``): sorted, so equal declarations are equal bytes."""
        return {
            "version": self.version,
            "targets": sorted(self.targets),
            "when": sorted(self.when),
            "required": self.required,
            "protected": self.protected,
            "config": {k: sorted(v) for k, v in sorted(self.config.items())},
            "loosens_when_added": sorted(self.loosens_when_added),
        }


@dataclass(frozen=True)
class Finding:
    """One problem a check found. ``path`` is repository-relative when the finding has a place."""

    code: str
    message: str
    path: str | None = None
    details: Mapping[str, Any] | None = None

    def wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.path is not None:
            out["path"] = self.path
        if self.details is not None:
            out["details"] = dict(self.details)
        return out


@dataclass(frozen=True)
class CheckResult:
    status: Status
    findings: tuple[Finding, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise RuntimeFault("CHECK_RESULT", "unknown check status", details=self.status)
        if self.status == "pass" and self.findings:
            raise RuntimeFault("CHECK_RESULT", "a passing check has no findings")


def result(findings: list[Finding], **details: Any) -> CheckResult:
    """``pass`` without findings, ``fail`` with them."""
    return CheckResult("fail" if findings else "pass", tuple(findings), details)


@dataclass(frozen=True)
class CheckContext:
    """What a check run gets. ``root`` is the repository checkout; ``workdir`` is the check's own
    empty scratch directory; ``targets`` narrows the run (empty: everything the check covers);
    ``base_ref`` is the git ref of the main-branch snapshot (the check policy check, M-19)."""

    root: Path
    when: str
    workdir: Path
    targets: tuple[str, ...] = ()
    base_ref: str | None = None


class Check(Protocol):
    @property
    def spec(self) -> ModuleSpec: ...

    @property
    def check(self) -> CheckSpec: ...

    def run(self, context: CheckContext) -> CheckResult: ...


class BuiltinCheck:
    """The base of the built-in checks: holds its module spec and its declaration."""

    def __init__(self, spec: ModuleSpec, check: CheckSpec) -> None:
        self._spec, self._check = spec, check

    @property
    def spec(self) -> ModuleSpec:
        return self._spec

    @property
    def check(self) -> CheckSpec:
        return self._check

    def run(self, context: CheckContext) -> CheckResult:
        raise NotImplementedError


def _check(module: Module) -> None:
    declared = getattr(module, "check", None)
    if not isinstance(declared, CheckSpec):
        raise RuntimeFault("CONFORMANCE_FAILED", "a check declares a CheckSpec as `check`")
    if (declared.check_id, declared.version) != module.spec.key:
        raise RuntimeFault("CONFORMANCE_FAILED", "a check declares its own module id and version")
    if not callable(getattr(module, "run", None)):
        raise RuntimeFault("CONFORMANCE_FAILED", "a check has run(context)")


SUITE = ConformanceSuite(
    suite_id=suite_id(KIND), kind=KIND, interface_version=INTERFACE_VERSIONS[KIND], check=_check
)
