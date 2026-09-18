"""Live adapter to the EXISTING Foundry actor/decision authority.

V3 does not manufacture approval from imported Markdown or from a JWT permission
claim. A decision link resolves to the active approved definition and its exact,
explicit V3 precondition. Legacy definition canonicalization remains unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from amplai_foundry.domain.identity import ProjectRef
from amplai_foundry.governance.authority import AuthorityService, DirectAuthorityRequest
from amplai_foundry.governance.definitions import (
    ProposalDefinitionManifest,
    canonicalize_definition,
)
from amplai_foundry.governance.models import ProposalRef
from amplai_foundry.governance.review_cards import DefinitionObjectReader
from amplai_foundry.governance.store import GovernanceStore

from ..errors import Hold
from ..storage.store import Scope, Store
from .authority import Actor
from .identity import new_id


@dataclass(frozen=True)
class RuntimeRoleBinding:
    subject_id: str
    scope: Scope
    required_foundry_permissions: frozenset[str]
    runtime_permissions: frozenset[str]


class FoundryAuthorityBridge:
    def __init__(
        self,
        runtime_store: Store,
        governance_store: GovernanceStore,
        object_store: DefinitionObjectReader,
        *,
        project_bindings: Mapping[Scope, ProjectRef],
        roles: tuple[RuntimeRoleBinding, ...],
    ) -> None:
        self.runtime_store = runtime_store
        self.governance_store = governance_store
        self.object_store = object_store
        self.projects = dict(project_bindings)
        self.roles = tuple(roles)
        self.authority = AuthorityService(governance_store)

    def authenticate(self, scope: Scope, request: DirectAuthorityRequest) -> Actor:
        project = self.projects.get(scope)
        if project is None or project != request.project_ref:
            raise Hold("PROJECT_AUTHORITY", "No exact server-side Foundry project binding")
        try:
            context = self.authority.authenticate(request)
        except Exception as exc:
            raise Hold(
                "FOUNDRY_AUTHORITY",
                "Existing Foundry actor binding/permissions denied this request",
            ) from exc
        present = frozenset(p.value for p in context.permissions)
        roles = [
            r
            for r in self.roles
            if r.subject_id == context.actor_ref.actor_id
            and r.scope == scope
            and r.required_foundry_permissions <= present
        ]
        if not roles:
            raise Hold(
                "RUNTIME_ROLE", "Authenticated Foundry actor has no explicit V3 role binding"
            )
        permissions = frozenset(p for role in roles for p in role.runtime_permissions)
        return Actor(
            context.actor_ref.actor_id,
            scope,
            permissions,
            context.actor_ref.actor_type.value,
            "foundry:" + context.source.request_id,
        )

    def link(
        self,
        actor: Actor,
        proposal_ref: ProposalRef,
        definition_digest: str,
        authorization_name: str,
    ) -> dict[str, Any]:
        actor.require("runtime.admin")
        if self.projects.get(actor.scope) != proposal_ref.project_ref:
            raise Hold("PROJECT_AUTHORITY", "Proposal is outside the exact runtime project")
        value: dict[str, Any] = {
            "link_id": new_id("foundry-decision"),
            "scope": actor.scope.wire(),
            "proposal_ref": proposal_ref.model_dump(mode="json"),
            "definition_digest": definition_digest,
            "authorization_name": authorization_name,
        }
        # Resolve from live governance before publishing even a reference.
        self._resolve_link(actor.scope, value)
        with self.runtime_store.tx() as db:
            return self.runtime_store.put(
                db, actor.scope, "foundry-decision-link", value["link_id"], 1, value
            )

    def _resolve_link(self, scope: Scope, value: dict[str, Any]) -> dict[str, Any]:
        project = self.projects.get(scope)
        ref = ProposalRef.model_validate(value["proposal_ref"])
        if value.get("scope") != scope.wire() or project is None or ref.project_ref != project:
            raise Hold("PROJECT_AUTHORITY", "Decision link is outside the bound project")
        self.runtime_store.assert_outside_tx()
        with self.governance_store.connect() as db:
            row = db.execute(
                "SELECT active_definition_digest,decision_epoch,status "
                "FROM governance_active_proposals "
                "WHERE project_namespace=? AND project_id=? AND proposal_id=?",
                (project.namespace, project.project_id, ref.proposal_id),
            ).fetchone()
            if (
                row is None
                or row[0] != value["definition_digest"]
                or row[2] not in {"approved", "applied"}
            ):
                raise Hold(
                    "DECISION_NOT_ACTIVE",
                    "Definition changed or proposal is not currently approved",
                )
            decision = db.execute(
                "SELECT actor_id,decision_epoch,action FROM governance_decision_results "
                "WHERE project_namespace=? AND project_id=? AND proposal_id=? "
                "AND active_definition_digest=? AND decision_epoch=? AND action='approve' "
                "ORDER BY processed_at DESC LIMIT 1",
                (project.namespace, project.project_id, ref.proposal_id, row[0], row[1]),
            ).fetchone()
            if decision is None:
                raise Hold(
                    "DECISION_RECEIPT",
                    "An approved active definition requires its actual decision receipt",
                )
            actor = db.execute(
                "SELECT actor_type,status FROM governance_actors WHERE actor_id=?", (decision[0],)
            ).fetchone()
            permission = db.execute(
                "SELECT 1 FROM governance_actor_permissions "
                "WHERE actor_id=? AND project_namespace=? AND project_id=? "
                "AND permission='proposal.decide'",
                (decision[0], project.namespace, project.project_id),
            ).fetchone()
            if actor is None or actor[0] != "human" or actor[1] != "active" or permission is None:
                raise Hold(
                    "APPROVER_REVOKED", "The independent human approver is no longer authorized"
                )
            generation = row[1]
            approver = decision[0]
        raw = self.object_store.get_definition_object(ref, value["definition_digest"])
        definition = ProposalDefinitionManifest.model_validate_json(raw)
        if canonicalize_definition(definition).digest != value["definition_digest"]:
            raise Hold(
                "DEFINITION_INTEGRITY",
                "Existing Foundry definition bytes no longer match their digest",
            )
        entries = [
            p
            for p in definition.preconditions
            if p.get("kind") == "amplai_v3_authorization"
            and p.get("name") == value["authorization_name"]
        ]
        if len(entries) != 1:
            raise Hold(
                "EXPLICIT_V3_AUTHORIZATION",
                "Approved definition lacks exactly one named V3 authorization precondition",
            )
        entry = entries[0]
        authorization = entry.get("authorization")
        if entry.get("scope") != scope.wire() or not isinstance(authorization, dict):
            raise Hold("AUTHORIZATION_SCOPE", "Approved precondition is malformed or outside scope")
        return {
            **authorization,
            "scope": scope.wire(),
            "status": "approved",
            "revoked": False,
            "generation": generation,
            "approved_by": approver,
            "source_definition_digest": value["definition_digest"],
            "source_proposal_ref": value["proposal_ref"],
        }

    def resolve_decision(self, scope: Scope, decision_ref: dict[str, Any]) -> dict[str, Any]:
        link = self.runtime_store.get(scope, "foundry-decision-link", decision_ref)
        return self._resolve_link(scope, link)

    def check_approval(
        self, scope: Scope, approval_ref: dict[str, Any], action: str, subject_digest: str
    ) -> dict[str, Any]:
        decision = self.resolve_decision(scope, approval_ref)
        if decision.get("action") != action or decision.get("subject_digest") != subject_digest:
            raise Hold(
                "APPROVAL_BINDING",
                "Governed approval does not authorize this exact action and frozen subject",
            )
        return decision
