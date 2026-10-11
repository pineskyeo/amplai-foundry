"""The ``notifier`` module kind, interface v1 (Work 034 S1c; spec F-5, H-3, H-8; plan.md §3.7).

A notifier tells the operator directly, on a channel the front agent does not hold, so a stopped
or compromised front agent cannot hide a warning (F-5, AC-F3). Interface v1::

    send(event_id, projection) -> receipt

- Input is only an event id and a :class:`Projection`: a closed set of fields, each a bounded
  identifier, a closed value or a timestamp. No free text reaches a notifier: not a reason the
  front agent wrote, not a PR title, not a log (H-8, AC-F3). The notifier renders its own message
  from these fields.
- The same event id is sent once. A second ``send`` returns the first receipt, marked
  ``replayed``, without sending (receipt replay). The same event id with another projection is
  ``NOTIFY_CONFLICT``.
- The destination is the one operator connection in the notifier's configuration; nothing in the
  input names a destination.
- A failed send raises; nothing is recorded, so a later ``send`` of the same event tries again. The
  product store outbox drives those retries (``notifier/pump.py``). ``NOTIFY_FAILED`` is
  transient (retried); ``NOTIFY_REJECTED`` is a permanent refusal (recorded, not retried).
- Delivery is at least once: a crash after the destination accepted a message and before its
  receipt is written sends that message again.

``conformance_suite`` is the v1 suite (``conformance.notifier.v1``). It needs an implementation
built with a test transport: it sends synthetic events.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, Literal, Protocol, get_args

from ..contracts.identity import canonical, digest, new_id
from ..errors import Conflict, RuntimeFault
from ..modules.builtin import suite_id
from ..modules.conformance import ConformanceSuite
from ..modules.spec import INTERFACE_VERSIONS, Module
from ..storage.store import Scope, Store

KIND = "notifier"
PROJECTION_VERSION = "notify-projection-1"

# The events a notifier carries in v1: a front agent paused or cancelled a goal (H-3).
NotifyEvent = Literal["intake.stop_requested"]
NOTIFY_EVENTS: tuple[str, ...] = get_args(NotifyEvent)
StopAction = Literal["pause", "cancel"]
STOP_ACTIONS: tuple[str, ...] = get_args(StopAction)

_EVENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_GOAL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_BY = re.compile(r"intake:[a-z][a-z0-9_-]{0,31}")
_STATUS = re.compile(r"[a-z][a-z_]{0,31}")
# RFC 3339 UTC as ``contracts.identity.now`` writes it
_AT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?Z")


def _bad(why: str, details: object = None) -> RuntimeFault:
    return RuntimeFault("NOTIFY_INPUT", why, details=details)


@dataclass(frozen=True)
class Projection:
    """The fixed-schema view a notifier gets of one event (H-8). Every field is bounded and none
    is free text: ``by`` is the actor mark (``intake:<adapter_id>``), ``status`` the goal's status
    from a closed set (``unknown`` otherwise), ``at`` the event time."""

    event: str
    goal_id: str
    action: str
    by: str
    status: str
    at: str
    schema_version: str = PROJECTION_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != PROJECTION_VERSION:
            raise _bad("unknown projection version", {"schema_version": self.schema_version})
        if self.event not in NOTIFY_EVENTS:
            raise _bad("event is outside the notifier's closed set", {"event": self.event})
        if self.action not in STOP_ACTIONS:
            raise _bad("action is outside the closed set", {"action": self.action})
        for name, pattern in (
            ("goal_id", _GOAL_ID),
            ("by", _BY),
            ("status", _STATUS),
            ("at", _AT),
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not pattern.fullmatch(value):
                raise _bad(f"{name} is not a bounded identifier", {"field": name})

    def wire(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class Receipt:
    """What a notifier records for one sent event. ``replayed`` is true on every later ``send``."""

    event_id: str
    module: str  # module_id@version
    projection_digest: str
    sent_at: str
    replayed: bool = False

    def wire(self) -> dict[str, Any]:
        return asdict(self)


class ReceiptLog(Protocol):
    """Durable receipts by event id. ``put`` keeps the first receipt of an event."""

    def get(self, event_id: str) -> Receipt | None: ...

    def put(self, receipt: Receipt) -> None: ...


class MemoryReceipts:
    """Receipts in memory: conformance runs and tests."""

    def __init__(self) -> None:
        self._items: dict[str, Receipt] = {}

    def get(self, event_id: str) -> Receipt | None:
        return self._items.get(event_id)

    def put(self, receipt: Receipt) -> None:
        self._items.setdefault(receipt.event_id, receipt)


class StoreReceipts:
    """Receipts in the product store's idempotency table (``commands``), one row per event:
    actor ``notifier:<module_id>`` (not the version, so an upgrade keeps them), operation
    ``notify``, key the event id, request digest the projection digest, retention ``effect``.
    Rows are never deleted (``Store.command``)."""

    OPERATION = "notify"

    def __init__(self, store: Store, scope: Scope, module: str) -> None:
        self.store, self.scope, self.actor = store, scope, "notifier:" + module

    def get(self, event_id: str) -> Receipt | None:
        with self.store._lock:
            row = self.store.conn.execute(
                "SELECT result FROM commands WHERE tenant=? AND project=? AND actor=? "
                "AND operation=? AND key=?",
                (*self.scope.keys(), self.actor, self.OPERATION, event_id),
            ).fetchone()
        if row is None:
            return None
        return Receipt(**json.loads(row["result"]))

    def put(self, receipt: Receipt) -> None:
        with self.store.tx() as db:
            db.execute(
                "INSERT OR IGNORE INTO commands VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    *self.scope.keys(),
                    self.actor,
                    self.OPERATION,
                    receipt.event_id,
                    receipt.projection_digest,
                    canonical(receipt.wire()),
                    "effect",
                    receipt.sent_at,
                ),
            )


class Notifier(Module, Protocol):
    def send(self, event_id: str, projection: Projection) -> Receipt: ...


def check_input(event_id: object, projection: object) -> tuple[str, Projection]:
    """The v1 input rule every notifier applies first: an event id and a :class:`Projection`,
    nothing else (no dict, no text)."""
    if not isinstance(event_id, str) or not _EVENT_ID.fullmatch(event_id):
        raise _bad("event_id is not a bounded identifier")
    if type(projection) is not Projection:
        raise _bad("a notifier takes only the fixed-schema projection (H-8)")
    return event_id, projection


def replay(log: ReceiptLog, event_id: str, projection: Projection) -> Receipt | None:
    """The earlier receipt of this event, marked replayed; ``NOTIFY_CONFLICT`` if that event was
    sent with another projection."""
    earlier = log.get(event_id)
    if earlier is None:
        return None
    if earlier.projection_digest != digest(projection.wire()):
        raise Conflict("NOTIFY_CONFLICT", "The event id was sent with another projection")
    return Receipt(**{**earlier.wire(), "replayed": True})


# -- conformance (M-2) ----------------------------------------------------------------------------


def sample(**override: str) -> Projection:
    fields = {
        "event": "intake.stop_requested",
        "goal_id": "goal-conformance",
        "action": "pause",
        "by": "intake:conformance",
        "status": "running",
        "at": "2026-10-11T00:00:00Z",
        **override,
    }
    return Projection(**fields)


def _expect(code: str, call: Callable[[], object]) -> None:
    try:
        call()
    except RuntimeFault as fault:
        if fault.code != code:
            raise RuntimeFault("CONFORMANCE_FAILED", f"expected {code}, got {fault.code}") from None
        return
    raise RuntimeFault("CONFORMANCE_FAILED", f"expected {code}, nothing was refused")


def _check(module: Module) -> None:
    send = getattr(module, "send", None)
    if not callable(send):
        raise RuntimeFault("CONFORMANCE_FAILED", "a notifier has send(event_id, projection)")
    event_id = new_id("evt-conformance")
    first = send(event_id, sample())
    if not isinstance(first, Receipt) or first.event_id != event_id or first.replayed:
        raise RuntimeFault("CONFORMANCE_FAILED", "the first send returns a fresh receipt")
    if first.projection_digest != digest(sample().wire()):
        raise RuntimeFault("CONFORMANCE_FAILED", "the receipt binds the projection digest")
    again = send(event_id, sample())
    if not isinstance(again, Receipt) or not again.replayed:
        raise RuntimeFault("CONFORMANCE_FAILED", "the same event id is a receipt replay")
    if {**again.wire(), "replayed": False} != first.wire():
        raise RuntimeFault("CONFORMANCE_FAILED", "a replay returns the first receipt")
    _expect("NOTIFY_CONFLICT", lambda: send(event_id, sample(action="cancel")))
    other = new_id("evt-conformance")
    _expect("NOTIFY_INPUT", lambda: send(other, sample().wire()))
    _expect("NOTIFY_INPUT", lambda: send(other, "free text"))
    _expect("NOTIFY_INPUT", lambda: send("bad id with spaces", sample()))


SUITE = ConformanceSuite(
    suite_id=suite_id(KIND), kind=KIND, interface_version=INTERFACE_VERSIONS[KIND], check=_check
)
