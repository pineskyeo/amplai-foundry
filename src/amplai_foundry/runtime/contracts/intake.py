"""The intake actor: a messenger front agent acting for one linked operator (Work 034 S1a).

Spec H-2..H-5, D-106, D-112 (``specs/034-hermes-modular-platform``). The server builds this actor
after it authenticated the front agent (Hermes) and found the messenger user in the operator's
identity map; no request field supplies it. Its permissions are the front agent's, its attribution
(``subject_id``) is the linked operator's, and it is never human, so it cannot approve
(``execution/product.py`` ``approve``).

On the wire an actor is ``human`` or ``service`` (frozen 3.0.0 ``common.schema.json``
``$defs/actor``), so an intake actor is written as ``service`` and marked by its
``authn_context_ref`` (``intake:<adapter>``); persisted records are checked by that mark.

Rules every path applies, whatever route reached the service:

- steering kinds by actor kind (H-3): an intake actor may add constraints, change priority, add
  evidence, change acceptance (a new contract revision that the operator approves again), pause
  and cancel; it may resume only a goal whose latest applied pause was its own (D-112);
- question kinds (H-4): an intake actor answers or refines only non-authority kinds;
- goals (AC-H2, AC-H3): an intake actor reaches only goals its linked operator submitted.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..errors import RuntimeFault
from ..storage.store import Scope, Store
from .authority import INTAKE_KIND, Actor

HERMES = "hermes"
INTAKE_CONTEXT_PREFIX = "intake:"
HERMES_CONTEXT = INTAKE_CONTEXT_PREFIX + HERMES

# H-2. Not here: execution.approve, grant.issue (no approval, no grant through the front agent).
INTAKE_PERMISSIONS = frozenset(
    {
        "goal.submit",
        "runtime.read",
        "goal.steer",
        "question.answer",
        "knowledge.propose",
        "meta.read",
        "goal.cancel",
    }
)

# steering-event.schema.json ``kind``
STEERING_KINDS = frozenset(
    {
        "pause",
        "resume",
        "cancel",
        "priority_change",
        "constraint_add",
        "acceptance_change",
        "new_evidence",
    }
)
# H-3, D-112: ``resume`` is allowed separately, only for a goal the same intake adapter paused.
INTAKE_STEERING_KINDS = frozenset(
    {"constraint_add", "priority_change", "new_evidence", "acceptance_change", "pause", "cancel"}
)
STEERING_KINDS_BY_ACTOR: dict[str, frozenset[str]] = {
    "human": STEERING_KINDS,
    "service": STEERING_KINDS,
    INTAKE_KIND: INTAKE_STEERING_KINDS,
}

# H-4. ``authority`` and ``destructive_change`` stay on the operator's direct path.
INTAKE_QUESTION_KINDS = frozenset(
    {"ambiguous_target", "subjective_direction", "conflicting_constraint", "business_intent"}
)


def intake_actor(subject_id: str, scope: Scope, adapter: str = HERMES) -> Actor:
    """The actor for a request the front agent relays from the linked operator ``subject_id``."""
    context = INTAKE_CONTEXT_PREFIX + adapter
    return Actor(subject_id, scope, INTAKE_PERMISSIONS, INTAKE_KIND, context)


def is_intake(actor: Actor) -> bool:
    return actor.kind == INTAKE_KIND


def is_intake_wire(actor: dict[str, Any]) -> bool:
    """A persisted actor (``Actor.wire()``) written by an intake actor."""
    return str(actor.get("authn_context_ref", "")).startswith(INTAKE_CONTEXT_PREFIX)


def check_steering_kind(actor: Actor, kind: str, *, paused_by: dict[str, Any] | None) -> None:
    """Refuse a steering kind this actor kind may not send (H-3). ``paused_by`` is the wire actor
    of the goal's latest applied pause, or None when the goal has none."""
    allowed = STEERING_KINDS_BY_ACTOR.get(actor.kind)
    if allowed is None:
        raise RuntimeFault("FORBIDDEN", "Steering is not available to this actor kind")
    if kind in allowed:
        return
    if (
        kind == "resume"
        and is_intake(actor)
        and paused_by is not None
        and paused_by.get("authn_context_ref") == actor.authn_context_ref
    ):
        return
    if kind == "resume" and is_intake(actor):
        raise RuntimeFault(
            "FORBIDDEN", "The front agent resumes only a goal it paused; the operator resumes this"
        )
    raise RuntimeFault("FORBIDDEN", "This steering kind is not allowed for this actor: " + kind)


def check_question_kind(actor: Actor, kind: str) -> None:
    """Refuse an answer or intent refinement through the front agent for an authority kind (H-4)."""
    if is_intake(actor) and kind not in INTAKE_QUESTION_KINDS:
        raise RuntimeFault(
            "FORBIDDEN",
            "Answer this question directly as the operator, not through the front agent",
        )


def goal_owner(store: Store, scope: Scope, goal_id: str) -> str:
    """The subject that submitted the goal: the actor of its intent's first revision (a later
    refinement keeps the intent id and records the answering actor)."""
    goal = store.head(scope, "goal", goal_id)
    intent_id = goal["data"]["intent_ref"]["id"]
    with store._lock:
        row = store.conn.execute(
            "SELECT data FROM objects WHERE tenant=? AND project=? AND kind='intent-envelope' "
            "AND id=? ORDER BY revision LIMIT 1",
            (*scope.keys(), intent_id),
        ).fetchone()
    if row is None:
        raise RuntimeFault("NOT_FOUND", "Goal not found in this scope")
    subject: str = json.loads(row["data"])["actor"]["subject_id"]
    return subject


def check_goal_access(store: Store, actor: Actor, goal_id: str) -> None:
    """An intake actor reaches only its linked operator's goals; another goal is not found."""
    if is_intake(actor) and goal_owner(store, actor.scope, goal_id) != actor.subject_id:
        raise RuntimeFault("NOT_FOUND", "Goal not found in this scope")


STOP_EVENT = "intake.stop_requested"


def stop_requested(
    store: Store,
    db: sqlite3.Connection,
    actor: Actor,
    goal_id: str,
    kind: str,
    reason: str,
    *,
    steering_ref: dict[str, Any] | None = None,
) -> None:
    """Record, in the caller's transaction, that the front agent paused or cancelled a goal: who
    (the intake actor) and why (its reason). H-3: every such stop leaves an operator record."""
    store.event(
        db,
        actor.scope,
        "goal",
        goal_id,
        STOP_EVENT,
        {"kind": kind, "actor": actor.wire(), "reason": reason, "steering_ref": steering_ref},
    )


def latest_applied_pause(
    db: sqlite3.Connection, scope: Scope, goal_id: str
) -> dict[str, Any] | None:
    """The wire actor of the goal's most recently received pause that took effect, or None."""
    rows = db.execute(
        "SELECT data FROM heads WHERE tenant=? AND project=? AND kind='steering' "
        "AND state='applied'",
        scope.keys(),
    ).fetchall()
    pauses = [
        event
        for event in (json.loads(r["data"])["event"] for r in rows)
        if event["goal_id"] == goal_id and event["kind"] == "pause"
    ]
    if not pauses:
        return None
    actor: dict[str, Any] = max(pauses, key=lambda e: e["received_at"])["actor"]
    return actor
