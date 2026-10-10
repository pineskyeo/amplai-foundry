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
    GET  /events                 notifications after a cursor (fixed schema, H-8, AC-H10)

A message is accepted once: the server derives the idempotency key from the message's transport
identity (``hermes:<digest>``, H-7). A goal of another subject is not found (AC-H3). Submissions and
controls are rate limited per actor and operation with the operator's configured limits (H-3, H-7).

The notification cursor is ``<store incarnation>:<seq>`` (plan.md §3.2 item 8). A cursor of another
incarnation (the store was restored) or past the newest event is ``CURSOR_EXPIRED`` and carries a
snapshot of the goals and a fresh cursor (design/18 §4, AC-H6).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse
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


OPERATIONS = ("submit", "steer", "replan", "cancel")


class RateLimiter:
    """At most ``count`` requests per ``window`` seconds for each (actor, operation) (H-3, H-7).

    Every authenticated new message counts; a resent message (its command or receipt exists) is
    a replay and does not. The windows live in this process on a monotonic clock: a server
    restart starts them again.
    """

    def __init__(self, limits: dict[str, tuple[int, float]], clock: Callable[[], float]) -> None:
        if set(limits) != set(OPERATIONS):
            raise Hold("INTAKE_RATE_LIMITS", "Every intake operation needs a configured limit")
        self.limits, self.clock = dict(limits), clock
        self._seen: dict[tuple[str, str, str], deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, actor: Actor, operation: str) -> None:
        count, window = self.limits[operation]
        key = (actor.subject_id, actor.authn_context_ref, operation)
        with self._lock:
            moment = self.clock()
            seen = self._seen.setdefault(key, deque())
            while seen and seen[0] <= moment - window:
                seen.popleft()
            if len(seen) >= count:
                raise RuntimeFault(
                    "RATE_LIMITED",
                    f"At most {count} {operation} requests per {window:g} seconds",
                    outcome="hold",
                )
            seen.append(moment)


@dataclass
class IntakeServices:
    gate: IntakeGate
    limits: RateLimiter
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


_EVENT_TYPE = re.compile(r"[a-z][a-z0-9_]*(\.[a-z0-9_]+)*")
_SEQ = re.compile(r"[0-9]{1,18}")  # ASCII digits only: str.isdigit() takes other scripts too


def submitted_before(store: Store, actor: Actor, key: str) -> bool:
    """A submission with this key was accepted already (``GoalService.submit``'s command)."""
    with store._lock:
        row = store.conn.execute(
            "SELECT 1 FROM commands WHERE tenant=? AND project=? AND actor=? "
            "AND operation='command' AND key=?",
            (*actor.scope.keys(), actor.subject_id, key),
        ).fetchone()
    return row is not None


def newest_seq(store: Store, scope: Scope) -> int:
    with store._lock:
        row = store.conn.execute(
            "SELECT COALESCE(MAX(seq),0) FROM events WHERE tenant=? AND project=?", scope.keys()
        ).fetchone()
    return int(row[0])


def intake_router(services: IntakeServices) -> APIRouter:
    router = APIRouter(prefix=PREFIX)
    gate, store, limits = services.gate, services.store, services.limits
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
        if not submitted_before(store, actor, body.key()):  # a resend replays, never limited
            limits.check(actor, "submit")
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

    def listing(actor: Actor) -> list[dict[str, Any]]:
        """The linked operator's 20 most recent goals."""
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
        return items

    @router.get("/goals")
    def goals(provider: str, user_id: str, authorization: str = Header(default="")) -> Any:
        return {"items": listing(gate.actor(authorization, provider, user_id))}

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
        if commands.receipt(actor, body.key()) is None:  # a resend replays, never limited
            limits.check(actor, route)

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

    @router.get("/events")
    def events(
        provider: str,
        user_id: str,
        cursor: str | None = None,
        limit: int = 100,
        authorization: str = Header(default=""),
    ) -> Any:
        """Notifications for the linked operator's goals after ``cursor``. Each names the goal, the
        event type (an AMPLAI identifier), the goal's current status and the event time; never an
        event payload. Without a cursor: a snapshot and the cursor to follow from."""
        actor = gate.actor(authorization, provider, user_id)
        if not 1 <= limit <= 1000:
            raise RuntimeFault("EVENT_BOUNDS", "Invalid event limit")
        newest = newest_seq(store, actor.scope)
        fresh = f"{store.incarnation}:{newest}"
        if cursor is None:
            return {"items": [], "snapshot": listing(actor), "cursor": fresh}
        incarnation, _, seq = cursor.rpartition(":")
        if not incarnation or not _SEQ.fullmatch(seq):
            raise RuntimeFault("EVENT_CURSOR", "A cursor is <incarnation>:<seq>")
        if incarnation != store.incarnation or int(seq) > newest:
            return JSONResponse(
                {
                    "code": "CURSOR_EXPIRED",
                    "message": "The cursor is from another store generation; start again from "
                    "the snapshot",
                    "outcome": "rejected",
                    "snapshot": listing(actor),
                    "cursor": fresh,
                },
                status_code=409,
            )
        owners: dict[str, bool] = {}
        items, last = [], int(seq)
        for event in store.events(actor.scope, after=int(seq), limit=limit):
            last = event["seq"]
            goal_id = event["aggregate_id"]
            if event["aggregate_type"] != "goal":
                continue
            if goal_id not in owners:
                try:
                    owners[goal_id] = goal_owner(store, actor.scope, goal_id) == actor.subject_id
                except RuntimeFault:
                    owners[goal_id] = False
            if not owners[goal_id]:
                continue
            try:
                status = project(store, actor.scope, goal_id, services.plan_record(goal_id))
            except RuntimeFault:
                status = {"status": "unknown"}
            kind = event["event_type"]
            items.append(
                {
                    "seq": event["seq"],
                    "goal_id": goal_id,
                    "event": kind if _EVENT_TYPE.fullmatch(kind) else "other",
                    "status": status["status"],
                    "at": event["created_at"],
                }
            )
        return {"items": items, "cursor": f"{store.incarnation}:{last}"}

    return router
