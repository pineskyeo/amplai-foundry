"""Intake routes for a messenger front agent (Work 034 S1a; spec H-1, H-5..H-8, plan.md §3.2).

The front agent (Hermes) holds its own bearer token, which no operator route accepts, and names the
messenger user who sent each message. The server looks that user up in the operator's identity map
and builds the intake actor (``runtime/contracts/intake.py``): the front agent's permissions, the
linked operator's attribution, never human. The token file and the identity map live outside the
store and are read on every request, so removing or rotating the token revokes it at once and a
store restore cannot bring it back (H-6, AC-H7). No request field supplies an actor, a role, a
repository hint or a data classification (H-5, design/17 §5): unknown fields are refused.

Routes under ``/api/v3/intake/hermes``:

    POST ""                      submit a goal from a message; its contract is drafted next
    GET  /goals                  the linked operator's goals (fixed-schema projection)
    GET  /goals/{goal_id}        one of them
    POST /goals/{goal_id}/steer  guidance for the running attempt (``amplai steer``)
    POST /goals/{goal_id}/replan change a running goal's plan; the operator approves the revision
    POST /goals/{goal_id}/cancel stop the goal, saying why

A message is accepted once: the server derives the idempotency key from the message's transport
identity (``hermes:<digest>``, H-7). A goal of another subject is not found (AC-H3).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Header
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from amplai_foundry.runtime.contracts.authority import Actor
from amplai_foundry.runtime.contracts.identity import digest
from amplai_foundry.runtime.contracts.intake import (
    HERMES,
    check_goal_access,
    goal_owner,
    intake_actor,
)
from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.runtime.storage.store import Scope, Store

from .server import ApiCommands

PREFIX = "/api/v3/intake/" + HERMES
IDENTITY_MAP_VERSION = "intake-identity-1"
# a messenger's own identifiers: bounded, no whitespace (provider-neutral, H-11)
_TOKEN = r"^[^\s]{1,256}$"


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class IdentityEntry(_Body):
    provider: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    user_id: str = Field(pattern=_TOKEN)
    subject_id: str = Field(min_length=1, max_length=128)


class IdentityMap(_Body):
    """Messenger user -> AMPLAI subject. This scope links one operator (H-5)."""

    schema_version: Literal["intake-identity-1"]
    entries: list[IdentityEntry] = Field(min_length=1, max_length=64)


class Message(_Body):
    """Who sent the message and its transport identity (the idempotency source)."""

    provider: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    user_id: str = Field(pattern=_TOKEN)
    workspace_id: str = Field(pattern=_TOKEN)
    channel_id: str = Field(pattern=_TOKEN)
    message_id: str = Field(pattern=_TOKEN)

    def key(self) -> str:
        identity = {
            "provider": self.provider,
            "workspace": self.workspace_id,
            "channel": self.channel_id,
            "message": self.message_id,
        }
        return "hermes:" + digest(identity)[7:]


class SubmitBody(Message):
    text: str = Field(min_length=1, max_length=32768)
    mode: Literal["work", "design"] = "work"


class SteerBody(Message):
    text: str = Field(min_length=1, max_length=4000)


class ReplanBody(Message):
    reason: str = Field(min_length=1, max_length=4000)


class CancelBody(Message):
    reason: str = Field(min_length=1, max_length=4096)


class IntakeGate:
    """Authenticates the front agent and resolves the messenger user, on every request.

    ``read`` returns a credential file's bytes (``keys.private_bytes``: owner-only, no symlink).
    """

    def __init__(
        self,
        token_file: Path,
        identity_map_file: Path,
        *,
        scope: Scope,
        operator_subject: str,
        operator_token: Callable[[], str],
        read: Callable[[Path], bytes],
    ) -> None:
        self.token_file, self.identity_map_file = token_file, identity_map_file
        self.scope, self.operator_subject = scope, operator_subject
        self.operator_token, self.read = operator_token, read

    def _authenticate(self, authorization: str) -> None:
        """Check the presented credential before saying anything about the configuration: an
        unauthenticated caller gets the same 401 whether the token file is missing (revoked),
        short or shared with the operator, and a bare 503 when the file cannot be trusted."""
        try:
            token = self.read(self.token_file).decode().strip()
        except OSError as exc:  # removed: revoked
            raise _denied() from exc
        except (RuntimeFault, ValueError) as exc:  # not owner-only, a symlink, not text
            raise _unavailable() from exc
        presented = authorization[7:] if authorization.startswith("Bearer ") else ""
        if not token or not hmac.compare_digest(
            hashlib.sha256(presented.encode()).digest(), hashlib.sha256(token.encode()).digest()
        ):
            raise _denied()
        # the caller holds the configured token: configuration faults may be named now
        if len(token) < 32:
            raise Hold("INTAKE_TOKEN", "The front agent token file needs a token of 32+ characters")
        try:
            operator = self.operator_token()
        except (OSError, RuntimeFault, ValueError) as exc:
            raise _unavailable() from exc
        if hmac.compare_digest(token, operator):
            raise Hold(
                "INTAKE_TOKEN_REUSED", "The front agent token must differ from the operator's"
            )

    def identities(self) -> list[IdentityEntry]:
        try:
            parsed = IdentityMap.model_validate(json.loads(self.read(self.identity_map_file)))
        except (OSError, ValueError, ValidationError) as exc:
            raise Hold("INTAKE_IDENTITY_MAP", "The identity map is missing or malformed") from exc
        seen = [(e.provider, e.user_id) for e in parsed.entries]
        if len(seen) != len(set(seen)):
            raise Hold("INTAKE_IDENTITY_MAP", "A messenger user appears twice in the identity map")
        if any(e.subject_id != self.operator_subject for e in parsed.entries):
            raise Hold("INTAKE_IDENTITY_MAP", "The identity map links only the operator (H-5)")
        return parsed.entries

    def actor(self, authorization: str, provider: str, user_id: str) -> Actor:
        self._authenticate(authorization)
        for entry in self.identities():
            if entry.provider == provider and entry.user_id == user_id:
                return intake_actor(entry.subject_id, self.scope)
        raise RuntimeFault("FORBIDDEN", "The messenger user is not linked to an operator")


def _denied() -> RuntimeFault:
    return RuntimeFault("UNAUTHENTICATED", "A valid front agent credential is required")


def _unavailable() -> RuntimeFault:
    # 503, no detail: the caller is not yet known to be the front agent
    return RuntimeFault("INTAKE_UNAVAILABLE", "The front agent entry is unavailable")


@dataclass
class IntakeServices:
    gate: IntakeGate
    store: Store
    goals: Any  # GoalService
    plan: Callable[[str], dict[str, Any]]  # draft the contract (background)
    plan_record: Callable[[str], dict[str, Any]]
    steer: Callable[[Actor, str, str], dict[str, Any]]
    replan: Callable[[Actor, str, str], dict[str, Any]]
    cancel: Callable[..., dict[str, Any]]


# The local product's plan statuses (``execution/product.py``, ``execution/loop.py``). The
# projection names only these; anything else is ``unknown``.
PLAN_STATUSES = frozenset(
    {
        "planning", "plan_failed", "needs_answers", "awaiting_approval", "approved", "running",
        "replanning", "replan_failed", "escalation_pending", "cancelling", "verified", "published",
        "failed", "cancelled", "timed_out", "held",
    }
)  # fmt: skip


def last_event_at(store: Store, scope: Scope, goal_id: str) -> str | None:
    with store._lock:
        row = store.conn.execute(
            "SELECT created_at FROM events WHERE tenant=? AND project=? "
            "AND aggregate_type='goal' AND aggregate_id=? ORDER BY seq DESC LIMIT 1",
            (*scope.keys(), goal_id),
        ).fetchone()
    return str(row["created_at"]) if row else None


def project(store: Store, scope: Scope, goal_id: str, record: dict[str, Any]) -> dict[str, Any]:
    """The fixed-schema view the front agent gets of a goal (H-8, AC-H10): ids, a status from a
    closed set and a time. No planner text, PR title, log or steering text."""
    status = record.get("status")
    return {
        "goal_id": goal_id,
        "status": status if status in PLAN_STATUSES else "unknown",
        "at": last_event_at(store, scope, goal_id),
    }


def intake_router(services: IntakeServices) -> APIRouter:
    router = APIRouter(prefix=PREFIX)
    gate, store = services.gate, services.store
    commands = ApiCommands(store)

    def owned(actor: Actor, goal_id: str) -> dict[str, Any]:
        check_goal_access(store, actor, goal_id)
        try:
            record = services.plan_record(goal_id)
        except RuntimeFault as exc:
            raise RuntimeFault("NOT_FOUND", "Goal not found in this scope") from exc
        return project(store, actor.scope, goal_id, record)

    @router.post("", status_code=202)
    def submit(body: SubmitBody, authorization: str = Header(default="")) -> Any:
        actor = gate.actor(authorization, body.provider, body.user_id)
        submitted = services.goals.submit(
            actor,
            text=body.text,
            mode=body.mode,
            channel="hermes",
            external_message_id=body.message_id,
            key=body.key(),
        )
        goal_id: str = submitted["goal_id"]
        services.plan(goal_id)  # a resent message finds the same goal and its plan
        return owned(actor, goal_id)

    @router.get("/goals")
    def goals(provider: str, user_id: str, authorization: str = Header(default="")) -> Any:
        actor = gate.actor(authorization, provider, user_id)
        with store._lock:
            rows = store.conn.execute(
                "SELECT id FROM heads WHERE tenant=? AND project=? AND kind='execution-plan' "
                "ORDER BY rowid DESC",
                actor.scope.keys(),
            ).fetchall()
        items = []
        for row in rows:
            try:
                mine = goal_owner(store, actor.scope, row["id"]) == actor.subject_id
            except RuntimeFault:
                continue
            if mine:
                items.append(owned(actor, row["id"]))
            if len(items) == 20:
                break
        return {"items": items}

    @router.get("/goals/{goal_id}")
    def goal(
        goal_id: str, provider: str, user_id: str, authorization: str = Header(default="")
    ) -> Any:
        return owned(gate.actor(authorization, provider, user_id), goal_id)

    def relay(
        authorization: str,
        body: Message,
        goal_id: str,
        route: str,
        operation: Callable[[Actor], dict[str, Any]],
    ) -> Any:
        actor = gate.actor(authorization, body.provider, body.user_id)
        owned(actor, goal_id)

        def execute() -> Any:
            operation(actor)
            return owned(actor, goal_id)

        return commands.run(
            actor, body.key(), "intake." + route + ":" + goal_id, body.model_dump(), execute
        )

    @router.post("/goals/{goal_id}/steer", status_code=202)
    def steer(goal_id: str, body: SteerBody, authorization: str = Header(default="")) -> Any:
        return relay(
            authorization, body, goal_id, "steer", lambda a: services.steer(a, goal_id, body.text)
        )

    @router.post("/goals/{goal_id}/replan", status_code=202)
    def replan(goal_id: str, body: ReplanBody, authorization: str = Header(default="")) -> Any:
        return relay(
            authorization,
            body,
            goal_id,
            "replan",
            lambda a: services.replan(a, goal_id, body.reason),
        )

    @router.post("/goals/{goal_id}/cancel")
    def cancel(goal_id: str, body: CancelBody, authorization: str = Header(default="")) -> Any:
        return relay(
            authorization,
            body,
            goal_id,
            "cancel",
            lambda a: services.cancel(a, goal_id, reason=body.reason),
        )

    return router
