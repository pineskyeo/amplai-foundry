"""Deployment composition root. Production authority remains in the existing Foundry.

The loader accepts local operator configuration only. It never executes a class,
module, shell string or URL supplied by an API request. Qualification is not inferred
from credential presence. A missing subsystem is visible and held at its boundary.
"""

from __future__ import annotations

import json
import os
import platform
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import BaseModel, ConfigDict, Field

from amplai_foundry.agent_drivers.http import ResponsesDriver
from amplai_foundry.agent_drivers.protocol import SessionJournal
from amplai_foundry.control_plane.api_v3.server import ApiServices, BearerAuthenticator, create_app
from amplai_foundry.domain.identity import ProjectRef
from amplai_foundry.governance.authority import DirectAuthorityRequest
from amplai_foundry.governance.object_store import ImmutableDefinitionObjectStore
from amplai_foundry.governance.store import GovernanceStore
from amplai_foundry.knowledge_runtime.service import KnowledgeService
from amplai_foundry.meta_harness.service import MetaHarness
from amplai_foundry.runtime.contracts.authority import Actor
from amplai_foundry.verification.runtime.service import JsonVerifier, VerificationService

from .contracts.authority import Authority
from .contracts.foundry_authority import FoundryAuthorityBridge, RuntimeRoleBinding
from .contracts.identity import new_id
from .contracts.registry import Contracts
from .errors import Hold
from .evidence.cas import ArtifactStore
from .execution.service import Runtime
from .goals.planner import PlanningService
from .goals.service import GoalService
from .storage.store import Scope, Store


class ProjectBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    scope: dict[str, str]
    foundry_project_ref: dict[str, str]
    project_root: str


class RoleBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    subject_id: str
    scope: dict[str, str]
    required_foundry_permissions: list[str] = Field(min_length=1)
    runtime_permissions: list[str]


class DeploymentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: str = "3.0.0"
    runtime_root: str
    governance_database: str
    credential_bindings_file: str
    signing_key_file: str
    signing_key_id: str
    verifier_key_file: str
    verifier_key_id: str
    project_bindings: list[ProjectBinding] = Field(min_length=1)
    role_bindings: list[RoleBinding] = Field(min_length=1)
    registry_snapshot_file: str | None = None
    verification_bindings_file: str | None = None
    planning: dict[str, Any] | None = None
    qualification_report_file: str | None = None
    release_trust_files: dict[str, str] = Field(default_factory=dict)


def private_bytes(path: Path, *, maximum: int = 4 * 1024 * 1024) -> bytes:
    path = path.expanduser().absolute()
    if path.resolve(strict=True) != path or path.is_symlink():
        raise Hold("SECRET_PATH", "Credential paths and their ancestors must not be symlinks")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise Hold("SECRET_PERMISSIONS", "Credential files require owner-only permissions (0600)")
    if info.st_uid != os.getuid():
        raise Hold("SECRET_OWNER", "Credential files must belong to the service identity")
    if info.st_size > maximum:
        raise Hold("SECRET_SIZE", "Credential file exceeds its configured bound")
    return path.read_bytes()


def read_key(path: Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(private_bytes(path), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise Hold("SIGNING_ALGORITHM", "An Ed25519 signing key is required")
    return key


def generate_key(path: Path) -> dict[str, Any]:
    path = path.expanduser().absolute()
    if path.exists() or path.is_symlink():
        raise Hold("KEY_EXISTS", "An existing trust key will not be overwritten")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.resolve() != path.parent:
        raise Hold("SECRET_PATH", "Key parent path may not contain symlinks")
    key = Ed25519PrivateKey.generate()
    payload = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        .hex()
    )
    return {"key_file": str(path), "public_key_hex": public, "authority_granted": False}


class ObjectStoreRouter:
    def __init__(self, projects: dict[tuple[str, str], Any]) -> None:
        self.projects = projects

    def get_definition_object(self, proposal_ref: Any, expected_digest: str) -> Any:
        key = (proposal_ref.project_ref.namespace, proposal_ref.project_ref.project_id)
        if key not in self.projects:
            raise Hold("PROJECT_BINDING", "Unknown Foundry object namespace")
        return self.projects[key].get_definition_object(proposal_ref, expected_digest)


class RuntimeDeployment:
    def __init__(self, config_path: Path) -> None:
        self.path = Path(config_path).expanduser().absolute()
        self.config = DeploymentConfig.model_validate_json(self.path.read_bytes())
        if self.config.schema_version != "3.0.0":
            raise Hold("CONFIG_VERSION", "Configuration is not V3")
        self.base = self.path.parent
        self.contracts = Contracts()
        self.store = Store(self.local_path(self.config.runtime_root))
        self.artifacts = ArtifactStore(self.store)
        self.http_clients: list[Any] = []
        try:
            self._configure()
        except BaseException:
            self.close()
            raise

    def local_path(self, value: str | Path) -> Path:
        p = Path(value).expanduser()
        return p if p.is_absolute() else self.base / p

    def _configure(self) -> None:
        cfg = self.config
        governance_path = self.local_path(cfg.governance_database)
        if not governance_path.is_file():
            raise Hold(
                "FOUNDRY_NOT_INITIALIZED",
                "Initialize/enroll the existing Foundry authority explicitly; "
                "V3 will not mint replacement authority",
            )
        self.governance = GovernanceStore(governance_path)
        projects, object_stores = {}, {}
        for binding in cfg.project_bindings:
            scope = Scope.parse(binding.scope)
            project = ProjectRef.model_validate(binding.foundry_project_ref)
            if scope in projects:
                raise Hold("DUPLICATE_PROJECT", "Runtime scope has two Foundry bindings")
            projects[scope] = project
            object_stores[(project.namespace, project.project_id)] = ImmutableDefinitionObjectStore(
                project, self.local_path(binding.project_root)
            )
        roles = tuple(
            RuntimeRoleBinding(
                r.subject_id,
                Scope.parse(r.scope),
                frozenset(r.required_foundry_permissions),
                frozenset(r.runtime_permissions),
            )
            for r in cfg.role_bindings
        )
        self.bridge = FoundryAuthorityBridge(
            self.store,
            self.governance,
            ObjectStoreRouter(object_stores),
            project_bindings=projects,
            roles=roles,
        )
        self.signer = read_key(self.local_path(cfg.signing_key_file))
        verifier_signer = read_key(self.local_path(cfg.verifier_key_file))
        if (
            cfg.signing_key_id == cfg.verifier_key_id
            or self.signer.public_key().public_bytes_raw()
            == verifier_signer.public_key().public_bytes_raw()
        ):
            raise Hold(
                "KEY_SEPARATION",
                "Execution authority and independent verification require distinct signing keys",
            )
        authority = Authority(
            self.store,
            self.contracts,
            {cfg.signing_key_id: self.signer.public_key()},
            self.bridge.resolve_decision,
            signer=self.signer,
            key_id=cfg.signing_key_id,
        )
        self.runtime = Runtime(self.store, self.contracts, authority, self.artifacts)
        self.goals = GoalService(self.store, self.contracts)
        self.verification = VerificationService(
            self.runtime,
            signer=verifier_signer,
            key_id=cfg.verifier_key_id,
            trusted_keys={cfg.verifier_key_id: verifier_signer.public_key()},
        )
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        trust = {
            key: Ed25519PublicKey.from_public_bytes(
                bytes.fromhex(self.local_path(path).read_text().strip())
            )
            for key, path in cfg.release_trust_files.items()
        }
        self.meta = MetaHarness(
            self.store,
            self.contracts,
            self.artifacts,
            approval_check=self.bridge.check_approval,
            trusted_release_keys=trust,
        )

        # The credential directory is read again per request; rotation/revocation is live.
        def authenticate(authorization: str | None) -> Any:
            directory = json.loads(private_bytes(self.local_path(cfg.credential_bindings_file)))

            def resolve(binding: dict[str, Any]) -> Any:
                scope = Scope.parse(binding["scope"])
                request = DirectAuthorityRequest.model_validate(
                    {**binding["foundry_identity"], "request_id": new_id("http-auth")}
                )
                return self.bridge.authenticate(scope, request)

            return BearerAuthenticator(directory, resolve)(authorization or "")

        self.authenticate = authenticate
        self._load_registry()
        self._load_verifiers()
        self.planning: PlanningService | None = None
        self.context_provider: Callable[[Actor, str], Any] | None = None
        if cfg.planning:
            self._load_planning(cfg.planning)
        self.services = ApiServices(
            self.runtime,
            self.goals,
            self.authenticate,
            verification=self.verification,
            meta=self.meta,
            planning=self.planning,
            planning_context=self.context_provider,
            authority_bridge=self.bridge,
            doctor=self.doctor,
        )
        self.app = create_app(self.services)

    def _load_registry(self) -> None:
        if not self.config.registry_snapshot_file:
            return
        data = json.loads(self.local_path(self.config.registry_snapshot_file).read_bytes())
        allowed = {
            "policy",
            "knowledge-observation",
            "verification-profile",
            "driver-capabilities",
            "model-profile",
            "environment",
            "qualification-report",
            "invariant-registry",
            "app-binding",
            "context-bundle",
            "global-verifier",
            "harness-composition",
            "price-snapshot",
            "qualification-set",
        }
        for item in data["objects"]:
            if (
                set(item) != {"kind", "id", "revision", "scope", "value"}
                or item["kind"] not in allowed
            ):
                raise Hold(
                    "REGISTRY_IMPORT_KIND",
                    "Startup registry import cannot create grants, approvals, verdicts, "
                    "active runs or release pointers",
                )
            scope = Scope.parse(item["scope"])
            if scope not in self.bridge.projects:
                raise Hold("REGISTRY_SCOPE", "Registry object is outside configured projects")
            if item["kind"] in self.contracts.definitions:
                self.contracts.validate(item["kind"], item["value"])
            from .contracts.semantics import check_refs

            check_refs(self.store, scope, item["value"])
            with self.store.tx() as db:
                self.store.put(db, scope, item["kind"], item["id"], item["revision"], item["value"])
        # Import is an operator action, not a claim that a profile's live probes passed.

    def _load_verifiers(self) -> None:
        if not self.config.verification_bindings_file:
            return
        bindings = json.loads(self.local_path(self.config.verification_bindings_file).read_bytes())
        for entry in bindings:
            if entry["kind"] != "json":
                raise Hold(
                    "VERIFIER_PROFILE",
                    "Use an explicitly composed command/browser verifier for non-JSON profiles",
                )
            self.verification.register(
                entry["profile_ref"], JsonVerifier(entry["schema"], equals=entry.get("equals"))
            )

    def _load_planning(self, config: dict[str, Any]) -> None:
        from .contracts.semantics import resolve_ref

        scope = Scope.parse(config["scope"])
        clients = []
        for name in ("planner", "reviewer"):
            entry = config[name]
            _, profile = resolve_ref(self.store, scope, entry["profile_ref"])
            _, qualification = resolve_ref(self.store, scope, profile["qualification_ref"])
            if (
                qualification.get("status") != "pass"
                or qualification.get("model_id") != profile["provider_model_id"]
            ):
                raise Hold(
                    "PLANNER_QUALIFICATION",
                    "Planner transport needs exact-model, environment-bound qualification",
                )
            client = ResponsesDriver(
                private_bytes(self.local_path(entry["api_key_file"])).decode().strip(),
                SessionJournal(self.store.root / "sessions" / name),
                model=profile["provider_model_id"],
                base_url=entry["base_url"],
                qualified=True,
            )
            self.http_clients.append(client.http)
            clients.append(client)
        self.planning = PlanningService(
            self.goals,
            self.runtime,
            planner=clients[0],
            reviewer=clients[1],
            planner_profile_ref=config["planner"]["profile_ref"],
            reviewer_profile_ref=config["reviewer"]["profile_ref"],
            planning_policy=config["policy"],
        )

        def context(actor: Actor, goal_id: str) -> Any:
            if actor.scope != scope:
                raise Hold("PLANNING_SCOPE", "Planner context is not bound to this project")
            knowledge = KnowledgeService(self.store, self.contracts)
            readiness = knowledge.readiness(scope, config["readiness_sources_by_area"])
            return {
                "readiness": readiness,
                "context_bundle_ref": config["context_bundle_ref"],
                "policy_ref": config["policy_ref"],
                "global_verifier_ref": config["global_verifier_ref"],
            }

        self.context_provider = context

    def doctor(self) -> dict[str, Any]:
        checks = [
            {
                "check": "python",
                "passed": sys.version_info >= (3, 11),
                "version": platform.python_version(),
            },
            {
                "check": "sqlite_integrity",
                "passed": self.store.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
            },
            {
                "check": "single_owner",
                "passed": self.store._owner is not None,
                "owner_epoch": self.store.epoch,
            },
            {"check": "distinct_verifier_key", "passed": True},
        ]
        qualification = None
        if self.config.qualification_report_file:
            qualification = json.loads(
                self.local_path(self.config.qualification_report_file).read_bytes()
            )
            checks.append(
                {
                    "check": "deployment_qualification",
                    "passed": qualification.get("status") == "pass"
                    and qualification.get("protocol_major") == 3,
                }
            )
        else:
            checks.append(
                {
                    "check": "deployment_qualification",
                    "passed": False,
                    "reason": "Live provider/sandbox qualification has not been supplied",
                }
            )
        return {
            "status": "pass" if all(c["passed"] for c in checks) else "hold",
            "checks": checks,
            "runtime_available": True,
            "qualification_is_not_inferred_from_credentials": True,
        }

    def close(self) -> None:
        for client in getattr(self, "http_clients", []):
            client.close()
        if getattr(self, "store", None):
            self.store.close()
