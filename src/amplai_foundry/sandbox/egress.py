"""Egress profile: deny-by-default network with a host:port allowlist enforced by a sidecar.

The agent container is attached only to an *internal* container network (no route, no DNS
to the outside). Its HTTPS_PROXY points at an allowlist CONNECT proxy on that network; the
proxy alone has a route out and refuses every target that is not on the allowlist. A profile
is usable only after real probes show all three edges: direct egress fails, an allowed target
tunnels, a denied target is refused. That measured record is the ``network_qualification_ref``
``ContainerSandbox`` demands (design 10_AUTHORITY_SECURITY: egress host+port, deny wins).

This is containment for local qualification. It is not the production credential broker.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from amplai_foundry.runtime.errors import Hold, RuntimeFault

_LABEL = r"[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?"
_HOST_PORT = re.compile(rf"^(?=.{{1,253}}:){_LABEL}(\.{_LABEL})*:([1-9][0-9]{{0,4}})$")
_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")
REQUIRED_PROBES = ("direct_egress_denied", "allowed_target_tunnels", "denied_target_refused")
PROXY_ENV = ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy")


def _valid_target(value: str) -> bool:
    if not _HOST_PORT.match(value):
        return False
    return int(value.rsplit(":", 1)[1]) <= 65535


@dataclass(frozen=True)
class EgressProfile:
    """Static shape of an egress-controlled network; carries no measurement."""

    name: str
    network: str
    proxy: str
    allow: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.name) or not _NAME.fullmatch(self.network):
            raise RuntimeFault("EGRESS_PROFILE", "Invalid egress profile or network name")
        if not _valid_target(self.proxy):
            raise RuntimeFault("EGRESS_PROXY", "Proxy must be host:port")
        if not self.allow:
            raise RuntimeFault(
                "EGRESS_ALLOWLIST", "An empty allowlist is network=none, not a profile"
            )
        for target in self.allow:
            if not _valid_target(target) or "*" in target:
                raise RuntimeFault("EGRESS_ALLOWLIST", "Allowlist entries are exact host:port")
        if len(set(self.allow)) != len(self.allow):
            raise RuntimeFault("EGRESS_ALLOWLIST", "Duplicate allowlist entry")
        object.__setattr__(self, "allow", tuple(sorted(self.allow)))

    def digest(self) -> str:
        body = {
            "name": self.name,
            "network": self.network,
            "proxy": self.proxy,
            "allow": list(self.allow),
        }
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()

    def env(self) -> dict[str, str]:
        url = f"http://{self.proxy}"
        return {"HTTPS_PROXY": url, "HTTP_PROXY": url, "NO_PROXY": "localhost,127.0.0.1"}

    def wire(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "name": self.name,
            "network": self.network,
            "proxy": self.proxy,
            "allow": list(self.allow),
            "digest": self.digest(),
        }

    @classmethod
    def load(cls, path: Path) -> EgressProfile:
        try:
            raw = json.loads(Path(path).read_text())
        except (OSError, ValueError) as exc:
            raise Hold(
                "EGRESS_PROFILE_UNREADABLE", "Egress profile file is missing or malformed"
            ) from exc
        try:
            return cls(raw["name"], raw["network"], raw["proxy"], tuple(raw["allow"]))
        except (KeyError, TypeError) as exc:
            raise RuntimeFault("EGRESS_PROFILE", "Egress profile fields missing") from exc


def qualification_ref(
    profile: EgressProfile, probes: dict[str, dict[str, Any]], *, checked_at: str, evidence: str
) -> dict[str, Any]:
    """Assemble the ref from real probe results. Anything short of three passes is ``fail``."""

    checks = []
    for name in REQUIRED_PROBES:
        result = probes.get(name) or {}
        outcome = result.get("outcome")
        observation = result.get("observation")
        if outcome not in {"pass", "fail"} or not isinstance(observation, str) or not observation:
            outcome = "inconclusive"
        checks.append(
            {
                "name": name,
                "outcome": outcome,
                "observation": result.get("observation", ""),
                "command": result.get("command", ""),
            }
        )
    return {
        "schema_version": "1.0",
        "egress_profile": profile.name,
        "egress_profile_digest": profile.digest(),
        "outcome": "pass" if all(c["outcome"] == "pass" for c in checks) else "fail",
        "checks": checks,
        "checked_at": checked_at,
        "evidence": evidence,
    }


def require_qualified(profile: EgressProfile, ref: dict[str, Any] | None) -> None:
    """The gate ``ContainerSandbox`` applies before it will attach any network."""

    if not ref or ref.get("outcome") != "pass":
        raise Hold(
            "EGRESS_UNQUALIFIED", "Network access requires a qualified enforced egress profile"
        )
    if ref.get("egress_profile_digest") != profile.digest():
        raise Hold("EGRESS_UNQUALIFIED", "Qualification refers to a different egress profile")
    checks = [c for c in ref.get("checks", []) if isinstance(c, dict)]
    passed = [c for c in checks if c.get("outcome") == "pass"]
    names = [str(c.get("name")) for c in passed]
    if sorted(names) != sorted(REQUIRED_PROBES) or len(names) != len(set(names)):
        raise Hold("EGRESS_UNQUALIFIED", "Qualification lacks one of the required egress probes")
    # A pass without a recorded observation is an assertion, not a measurement (R002).
    if any(not str(c.get("observation") or "").strip() for c in passed):
        raise Hold("EGRESS_UNQUALIFIED", "Qualification probes must carry their observations")


def load_qualification(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise Hold(
            "EGRESS_UNQUALIFIED", "Egress qualification file is missing or malformed"
        ) from exc
    if not isinstance(value, dict):
        raise Hold("EGRESS_UNQUALIFIED", "Egress qualification must be an object")
    return value
