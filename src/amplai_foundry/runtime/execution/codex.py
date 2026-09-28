"""The production Codex CLI execution profile (D-067, D-072).

Two pieces:

- ``SeededCodexPort`` wraps ``CliPort`` so each dispatch gets its own native home seeded with the
  operator's *scoped* ChatGPT credential copy (``scripts/sandbox_up.sh --codex-home``), never the
  real ``~/.codex``. A refreshed token is written back to the scoped copy after the run and the
  per-dispatch credential is removed; the session files stay for exact resume.
- ``install_codex_profile`` writes the runtime records the execution gate checks
  (``runtime/execution/service.py:_profile``) from *measured* inputs only: the pinned container
  profile, the qualified egress profile, and a passing ``scripts/container_qualify.py`` report
  for exactly that image and driver version. Anything else refuses to register.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...agent_drivers.cli import ClaudeCodeDriver, CodexCliDriver
from ...agent_drivers.ports import CliPort
from ...agent_drivers.protocol import SessionJournal
from ...sandbox.container import ContainerProfile, ContainerSandbox
from ...sandbox.egress import EgressProfile, load_qualification
from ..contracts.identity import now
from ..errors import Hold
from ..storage.store import Scope, Store

DRIVER_ID = "codex-cli"
# provider -> driver id / account kind (Work 019 E: Claude is a second qualified driver)
DRIVER_IDS = {"codex": "codex-cli", "claude": "claude-cli"}
ACCOUNTS = {"codex": "openai-chatgpt-account", "claude": "anthropic-subscription-oauth"}
AUTH = Path(".codex") / "auth.json"


class ScopedCredential:
    """The operator's scoped ChatGPT credential copy, leased into per-run homes.

    ``seed`` puts the current token into a run home; ``release`` writes a refreshed token back
    atomically (ChatGPT refresh tokens rotate, so a stale copy would stop working) and removes
    the run's copy. The real ``~/.codex`` is never accepted.
    """

    def __init__(self, credential_home: Path) -> None:
        home = Path(credential_home).absolute()
        if home.resolve() != home or not (home / AUTH).is_file():
            raise Hold("CODEX_CREDENTIAL", "Scoped credential copy (--codex-home) is required")
        real = (Path.home() / ".codex").resolve()
        if (home / ".codex").resolve() == real or home == Path.home().resolve():
            # design 10_AUTHORITY_SECURITY: no home credential mounts; only a scoped copy
            raise Hold("CODEX_CREDENTIAL", "Use a scoped copy, never the real ~/.codex")
        self.home = home
        self._lock = threading.Lock()

    def seed(self, run_home: Path) -> None:
        (run_home / ".codex").mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._lock:
            data = (self.home / AUTH).read_bytes()
        target = run_home / AUTH
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        # colima maps the bind owner for the container uid; the 0700 parent keeps others out.
        target.chmod(0o644)

    def release(self, run_home: Path) -> None:
        target = run_home / AUTH
        if not target.is_file() or target.is_symlink():
            return
        data = target.read_bytes()
        with self._lock:
            current = (self.home / AUTH).read_bytes()
            if data != current:
                try:
                    json.loads(data)
                except ValueError:
                    data = b""
                if data:
                    tmp = self.home / ".codex" / ".auth.json.writeback"
                    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(fd, "wb") as out:
                        out.write(data)
                        out.flush()
                        os.fsync(out.fileno())
                    os.replace(tmp, self.home / AUTH)
        target.unlink()


class SeededCodexPort(CliPort):
    def __init__(self, driver: CodexCliDriver, credential_home: Path | ScopedCredential) -> None:
        super().__init__(driver)
        self.credential = (
            credential_home
            if isinstance(credential_home, ScopedCredential)
            else ScopedCredential(credential_home)
        )

    def _dispatch_home(self, dispatch_id: str) -> Path:
        self.driver.journal._path(dispatch_id)  # validate the id before joining a path
        return self.driver.native_root / dispatch_id

    def prepare(self, dispatch: dict[str, Any], prompt: str, workspace: Path) -> dict[str, Any]:
        home = self._dispatch_home(dispatch["dispatch_id"])
        self.credential.seed(home)
        return self.driver.prepare(dispatch, prompt, workspace, native_home=home)

    def _release(self, handle: str) -> None:
        self.credential.release(self._dispatch_home(handle))

    def collect(self, handle: str) -> dict[str, Any]:
        receipt = self.driver.collect(handle)
        self._release(handle)
        return receipt

    def cancel(self, handle: str) -> dict[str, Any]:
        result = self.driver.cancel(handle)
        if result.get("process_stopped") is True:
            self._release(handle)
        return result

    def destroy(self, handle: str) -> None:
        self.driver.destroy(handle)
        self._release(handle)


@dataclass(frozen=True)
class CodexProfileInputs:
    container_profile: Path  # deployment/local-container-app-<app>.json
    egress_profile: Path  # deployment/local-egress.json
    egress_qualification: Path  # deployment/local-egress-qualification.json
    qualification_report: Path  # container_qualify.py --container-profile ... output
    model: str = "gpt-5.6-sol"
    data_classes: tuple[str, ...] = ("internal",)
    provider: str = "codex"  # "codex" | "claude"
    enabled: bool = True  # operator switch: a disabled model is filtered out of selection

    @property
    def driver_id(self) -> str:
        return DRIVER_IDS[self.provider]


# The same inputs describe either driver; the historical name stays for Work 018 callers.
DriverProfileInputs = CodexProfileInputs


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def container_profile(inputs: CodexProfileInputs) -> ContainerProfile:
    c = json.loads(inputs.container_profile.read_text())
    egress = EgressProfile.load(inputs.egress_profile)
    return ContainerProfile(
        c["image"],
        uid=c["uid"],
        gid=c["gid"],
        memory=c["memory"],
        cpus=c["cpus"],
        pids=c["pids"],
        network=egress.network,
        network_qualification_ref=load_qualification(inputs.egress_qualification),
        egress=egress,
    )


def measured_qualification(inputs: CodexProfileInputs) -> dict[str, Any]:
    """The passing report for exactly this image and driver version, or a Hold."""
    container = json.loads(inputs.container_profile.read_text())
    doc = json.loads(inputs.qualification_report.read_text())
    driver_id = inputs.driver_id
    report = doc.get("reports", {}).get(driver_id)
    pinned = re.search(r"\d+\.\d+\.\d+", str(container.get("tools", {}).get(inputs.provider, "")))
    version = [pinned.group(0)] if pinned else []
    if (
        not report
        or report.get("status") != "pass"
        or doc.get("container_image") != container["image"]
        or [report.get("driver_version")] != version
        or report.get("model") != inputs.model
        # The nine design probes passed on PONG turns while every tool call failed in the
        # container; real work also needs a measured tool turn (2026-09-28).
        or (report.get("tool_use") or {}).get("outcome") != "pass"
    ):
        raise Hold(
            "DRIVER_UNQUALIFIED",
            f"No passing {driver_id} qualification for this exact image, version and model",
            details={"image": container["image"], "report": str(inputs.qualification_report)},
        )
    return {
        "qualification_id": report["qualification_id"],
        "driver_id": driver_id,
        "driver_version": report["driver_version"],
        "status": "pass",
        "image": container["image"],
        "model": report["model"],
        "checks": {c["name"]: c["outcome"] for c in report["checks"]},
        "tool_use": report["tool_use"],
        "source": {
            "path": str(inputs.qualification_report),
            "sha256": _sha256(inputs.qualification_report),
        },
        "checked_at": doc.get("checked_at"),
    }


def put_record(
    store: Store, scope: Scope, contracts: Any, kind: str, object_id: str, value: dict[str, Any]
) -> dict[str, Any]:
    """Idempotent install: reuse the latest revision if identical, else append a revision."""
    from ..contracts.identity import digest

    if kind in contracts.definitions:
        contracts.validate(kind, value)
    existing = [r for r, _ in store.list_objects(scope, kind) if r["id"] == object_id]
    latest = max(existing, key=lambda r: r["revision"]) if existing else None
    if latest and latest["digest"] == digest(value):
        return latest
    with store.tx() as db:
        return store.put(db, scope, kind, object_id, latest["revision"] + 1 if latest else 1, value)


def install_codex_profile(
    store: Store,
    scope: Scope,
    inputs: CodexProfileInputs,
    capabilities: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Write environment → qualification → driver-capabilities → model-profile records."""
    from ..contracts.registry import Contracts

    contracts = Contracts()
    measured = measured_qualification(inputs)
    profile = container_profile(inputs)

    def put(kind: str, object_id: str, value: dict[str, Any]) -> dict[str, Any]:
        return put_record(store, scope, contracts, kind, object_id, value)

    # One record for the container both drivers run in: execution and verification must name
    # the same pinned environment (the suite verifier checks it), whichever driver was chosen.
    env_id = "sandbox-" + profile.image.rsplit("@sha256:", 1)[-1][:12]
    environment = {
        "environment_id": env_id,
        "scope": scope.wire(),
        "status": "qualified",
        "containment_enforced": True,
        "boundary": "container: pinned image, egress allowlist, uid 65534, read-only root",
        "image": profile.image,
        "egress_profile": profile.egress.wire() if profile.egress else None,
        "capabilities": capabilities,
    }
    env_ref = put("environment", env_id, environment)
    qual_ref = put(
        "qualification", measured["qualification_id"], {**measured, "environment_ref": env_ref}
    )
    driver = {
        "schema_version": "3.0.0",
        "driver_id": inputs.driver_id,
        "driver_version": measured["driver_version"],
        "transport": "cli",
        "environment_ref": env_ref,
        "declared": sorted({c["action"] for c in capabilities}),
        "observed": sorted({c["action"] for c in capabilities}),
        "qualified": sorted({c["action"] for c in capabilities}),
        "qualification_report_ref": qual_ref,
        "maturity": "qualified",
        "probed_at": measured["checked_at"] or now(),
    }
    driver_ref = put("driver-capabilities", inputs.driver_id, driver)
    model = {
        "schema_version": "3.0.0",
        "profile_id": f"{inputs.provider}-{inputs.model}",
        "provider": ACCOUNTS[inputs.provider],
        "provider_model_id": inputs.model,
        "model_version_policy": "pinned",
        "driver_profile_ref": driver_ref,
        "reasoning_profile": "provider-default",
        "data_classes_allowed": list(inputs.data_classes),
        "required_capabilities": [],
        "context_limit_tokens": 200000,
        "price_snapshot_ref": None,
        "qualification_ref": qual_ref,
        "enabled": inputs.enabled,
    }
    model_ref = put("model-profile", f"{inputs.provider}-{inputs.model}", model)
    return {
        "environment": env_ref,
        "qualification": qual_ref,
        "driver": driver_ref,
        "model": model_ref,
    }


install_driver_profile = install_codex_profile


def build_claude_port(inputs: CodexProfileInputs, journal_root: Path, token: str) -> CliPort:
    """Claude CLI in the same qualified container; the OAuth token is passed by env name."""
    measured = measured_qualification(inputs)
    sandbox = ContainerSandbox(container_profile(inputs))
    driver = ClaudeCodeDriver(
        measured["driver_version"],
        sandbox,
        SessionJournal(journal_root),
        model=inputs.model,
        qualified=True,
        environment={"CLAUDE_CODE_OAUTH_TOKEN": token},
        auth="oauth_token",
    )
    return CliPort(driver)


def build_codex_port(
    inputs: CodexProfileInputs, journal_root: Path, credential_home: Path | ScopedCredential
) -> SeededCodexPort:
    measured = measured_qualification(inputs)
    sandbox = ContainerSandbox(container_profile(inputs))
    driver = CodexCliDriver(
        measured["driver_version"],
        sandbox,
        SessionJournal(journal_root),
        model=inputs.model,
        qualified=True,
    )
    return SeededCodexPort(driver, credential_home)
