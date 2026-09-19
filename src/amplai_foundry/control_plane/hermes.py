"""Hermes intake/status/approval adapter (design/17 §5-§6, V3-049).

Hermes, Slack and Telegram are IntakeAdapter + read projection. An external user is
mapped to an AMPLAI actor only through an operator-verified binding; repo IDs, roles or
approval tokens inside conversation text are data, never authority. Webhook redelivery
is controlled by scoped idempotency, and approval is forwarded to the authority
service unchanged — the adapter has no approval power of its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from amplai_foundry.runtime.contracts.authority import Actor, Authority
from amplai_foundry.runtime.contracts.identity import now
from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.runtime.goals.service import GoalService
from amplai_foundry.runtime.storage.store import Scope, Store

# design/17 §6 — user-facing states; internal leased/pending/outbox words never surface.
STATUS_LABELS: dict[str, str] = {
    "draft": "의도 확인 중",
    "discovering": "의도 확인 중",
    "awaiting_decision": "확인 필요",
    "ready": "실행 대기",
    "active": "작업 중",
    "verifying": "검증 중",
    "verified": "완료",
    "blocked": "확인 필요",
    "failed": "실패",
    "cancelled": "중단",
}
PROJECTION_FIELDS = frozenset(
    {
        "goal_id",
        "state",
        "label",
        "row_version",
        "contract_ref",
        "graph_ref",
        "next_required_approval",
        "evidence",
    }
)
INTERNAL_WORDS = ("lease", "outbox", "inbox", "fence", "epoch", "heartbeat")


@dataclass(frozen=True)
class IdentityBinding:
    channel: str
    external_user_id: str
    actor: Actor
    verified_by: str
    bound_at: str


class HermesIdentityMap:
    """Operator-verified mapping external identity → AMPLAI actor (stored per scope)."""

    KIND = "hermes-identity"

    def __init__(self, store: Store) -> None:
        self.store = store

    @staticmethod
    def _key(channel: str, external_user_id: str) -> str:
        if not channel.strip() or not external_user_id.strip() or ":" in channel:
            raise RuntimeFault(
                "IDENTITY_KEY", "Channel and external user id must be plain, bounded ids"
            )
        return f"{channel}:{external_user_id}"

    def bind(
        self, operator: Actor, channel: str, external_user_id: str, actor: Actor
    ) -> dict[str, Any]:
        operator.require("runtime.admin")
        if actor.scope != operator.scope:
            raise Hold(
                "IDENTITY_SCOPE", "A binding may not cross the operator's authenticated scope"
            )
        if not actor.permissions <= operator.permissions:
            raise Hold(
                "IDENTITY_PERMISSION_SUBSET",
                "An operator cannot bind more permissions than it holds",
                details={"excess": sorted(actor.permissions - operator.permissions)},
            )
        key = self._key(channel, external_user_id)
        record = {
            "channel": channel,
            "external_user_id": external_user_id,
            "subject_id": actor.subject_id,
            "kind": actor.kind,
            "permissions": sorted(actor.permissions),
            "authn_context_ref": actor.authn_context_ref,
            "verified_by": operator.subject_id,
            "bound_at": now(),
        }
        with self.store.tx() as db:
            existing = [
                r for r, _ in self.store.list_objects(operator.scope, self.KIND) if r["id"] == key
            ]
            revision = max([r["revision"] for r in existing] + [0]) + 1
            ref = self.store.put(db, operator.scope, self.KIND, key, revision, record)
            self.store.event(
                db,
                operator.scope,
                "release",
                key,
                "hermes.identity_bound",
                {"ref": ref, "by": operator.subject_id},
            )
        return ref

    def resolve(self, scope: Scope, channel: str, external_user_id: str) -> IdentityBinding:
        key = self._key(channel, external_user_id)
        latest: tuple[dict[str, Any], dict[str, Any]] | None = None
        for ref, value in self.store.list_objects(scope, self.KIND):
            if ref["id"] == key and (latest is None or ref["revision"] > latest[0]["revision"]):
                latest = (ref, value)
        if latest is None:
            raise Hold(
                "IDENTITY_UNBOUND",
                "External identity is not bound to an AMPLAI actor; "
                "an operator must verify it first",
                details={"channel": channel},
            )
        value = latest[1]
        actor = Actor(
            value["subject_id"],
            scope,
            frozenset(value["permissions"]),
            value["kind"],
            value["authn_context_ref"],
        )
        return IdentityBinding(
            channel, external_user_id, actor, value["verified_by"], value["bound_at"]
        )


class HermesAdapter:
    def __init__(
        self, store: Store, goals: GoalService, authority: Authority, identities: HermesIdentityMap
    ) -> None:
        self.store, self.goals, self.authority, self.identities = (
            store,
            goals,
            authority,
            identities,
        )

    # -- intake -------------------------------------------------------------------
    def intake(
        self,
        scope: Scope,
        channel: str,
        external_user_id: str,
        external_message_id: str,
        text: str,
        *,
        mode: str = "work",
        target_hints: list[str] | None = None,
        classification: str = "internal",
    ) -> dict[str, Any]:
        if not external_message_id.strip() or len(external_message_id) > 200:
            raise RuntimeFault("MESSAGE_ID", "A bounded external message id is required for dedupe")
        binding = self.identities.resolve(scope, channel, external_user_id)
        # The message text is the intent. Nothing in it is parsed as role, grant or repo authority.
        result = self.goals.submit(
            binding.actor,
            text=text,
            mode=mode,
            target_hints=list(target_hints or []),
            channel="hermes",
            external_message_id=f"{channel}:{external_message_id}",
            classification=classification,
            key=f"hermes:{channel}:{external_message_id}",
        )
        return {**result, "status": self._project(scope, result["goal_id"])}

    # -- projection ---------------------------------------------------------------
    def _project(self, scope: Scope, goal_id: str) -> dict[str, Any]:
        head = self.store.head(scope, "goal", goal_id)
        data = head["data"]
        projection: dict[str, Any] = {
            "goal_id": goal_id,
            "state": head["state"],
            "label": STATUS_LABELS.get(head["state"], "확인 필요"),
            "row_version": head["row_version"],
            "contract_ref": data.get("active_contract_ref"),
            "graph_ref": data.get("active_graph_ref"),
            "next_required_approval": (
                "human decision" if head["state"] in {"awaiting_decision", "blocked"} else None
            ),
            "evidence": {"intent_ref": data.get("intent_ref")},
        }
        leaked = [k for k in projection if any(w in k for w in INTERNAL_WORDS)]
        if leaked or set(projection) - PROJECTION_FIELDS:
            raise RuntimeFault(
                "PROJECTION_FIELDS", "Status projection exposes internal runtime fields"
            )
        return projection

    def status(
        self, scope: Scope, channel: str, external_user_id: str, goal_id: str
    ) -> dict[str, Any]:
        binding = self.identities.resolve(scope, channel, external_user_id)
        binding.actor.require("runtime.read")
        return self._project(scope, goal_id)

    # -- approval: authority only ---------------------------------------------------
    def approve(
        self, scope: Scope, channel: str, external_user_id: str, grant: dict[str, Any]
    ) -> dict[str, Any]:
        binding = self.identities.resolve(scope, channel, external_user_id)
        # No local approval logic: the bound actor's own permission and the existing
        # Decision/authority checks decide. Conversation context adds nothing.
        return self.authority.issue(binding.actor, grant)
