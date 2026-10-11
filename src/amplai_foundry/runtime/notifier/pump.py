"""Operator alerts from the product store outbox (Work 034 S1c; spec H-3, F-5, AC-F3).

Every store event gets an outbox row in the same transaction (``Store.event``), and
``OutboxPump`` delivers pending rows at least once with stable event ids, backing off on failure.
This module is the outbox's consumer when a notifier is configured:

- ``intake.stop_requested`` (a front agent paused or cancelled a goal, H-3) becomes a
  :class:`~.interface.Projection` and is sent through the notifier.
- A transient failure (``NOTIFY_FAILED``: HTTP 429 or 5xx, a timeout, a connection error, a
  missing webhook file) is not acknowledged, so the outbox retries it with backoff.
- A permanent refusal (``NOTIFY_REJECTED``: any other HTTP 4xx, e.g. a revoked webhook) is
  recorded and acknowledged: retrying would only repeat it. The operator sees it in
  ``amplai status`` (:meth:`NotifierPump.health`). A stop event that cannot be projected (a
  malformed record) is recorded and acknowledged the same way (``NOTIFY_INPUT``).
- A send that succeeded but whose acknowledgment was lost finds its receipt on the next delivery
  and is acknowledged without a second message, even if the goal's status changed in between.
- Every other event is acknowledged without a send: it is not an operator alert in v1.
- Stop events at or below the watermark (the newest event when a notifier was first enabled on
  this store) are acknowledged without a send: enabling a notifier does not flush old alerts.

Delivery is at least once. A crash after the webhook POST and before the receipt is written
sends that alert again on the next delivery; a Slack Incoming Webhook takes no idempotency key.

The alert reads only the store, never the intake configuration, so it is sent with the front agent
turned off or its adapter removed (AC-F3).

The outbox has one status per event, so acknowledging here acknowledges for every consumer: this
is the only consumer the code has today (``OutboxPump`` has no other caller in ``src``).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from typing import Any

from ..contracts.identity import canonical, now
from ..contracts.intake import STOP_EVENT
from ..errors import RuntimeFault
from ..recovery.service import OutboxPump
from ..storage.store import Scope, Store
from .interface import Notifier, Projection, ReceiptLog

REJECTED = "notify-rejected"
WATERMARK = "notify-watermark"


def http_status(fault: RuntimeFault) -> int | None:
    details = fault.details
    value = details.get("http_status") if isinstance(details, dict) else None
    return value if isinstance(value, int) else None


class NotifierPump:
    def __init__(
        self,
        store: Store,
        scope: Scope,
        notifier: Notifier,
        status: Callable[[str], str],
        receipts: ReceiptLog,
        *,
        interval: float = 5.0,
    ) -> None:
        self.store, self.scope, self.notifier, self.status = store, scope, notifier, status
        self.receipts = receipts
        self.interval = interval
        # keyed by module id, not version: an upgrade keeps receipts and the watermark (review P3-8)
        self.actor = "notifier:" + notifier.spec.module_id
        self.last_error: dict[str, Any] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.watermark = self._watermark()

    # -- records in the store idempotency table (``commands``) ----------------------------------
    def _row(self, db: Any, operation: str, key: str, value: dict[str, Any]) -> None:
        db.execute(
            "INSERT OR IGNORE INTO commands VALUES(?,?,?,?,?,?,?,?,?)",
            (
                *self.scope.keys(),
                self.actor,
                operation,
                key,
                "-",
                canonical(value),
                "effect",
                now(),
            ),
        )

    def _watermark(self) -> int:
        """The newest event seq when a notifier was first enabled on this store; kept forever."""
        with self.store.tx() as db:
            newest = db.execute(
                "SELECT COALESCE(MAX(seq),0) FROM events WHERE tenant=? AND project=?",
                self.scope.keys(),
            ).fetchone()[0]
            self._row(db, WATERMARK, "first-enable", {"seq": int(newest)})
            row = db.execute(
                "SELECT result FROM commands WHERE tenant=? AND project=? AND actor=? "
                "AND operation=? AND key='first-enable'",
                (*self.scope.keys(), self.actor, WATERMARK),
            ).fetchone()
        seq: int = json.loads(row[0])["seq"]
        return seq

    def _reject(self, event: dict[str, Any], fault: RuntimeFault) -> None:
        record = {
            "event_id": event["event_id"],
            "code": fault.code,
            "http_status": http_status(fault),
            "at": now(),
        }
        with self.store.tx() as db:
            self._row(db, REJECTED, event["event_id"], record)

    # -- delivery ----------------------------------------------------------------------------------
    def projection(self, event: dict[str, Any]) -> Projection | None:
        """The alert of a store event, or None when the event is not an operator alert."""
        if (event["tenant"], event["project"]) != self.scope.keys():
            return None
        if event["aggregate_type"] != "goal" or event["event_type"] != STOP_EVENT:
            return None
        data = event["data"]
        goal_id = event["aggregate_id"]
        return Projection(
            event=STOP_EVENT,
            goal_id=goal_id,
            action=data["kind"],
            by=data["actor"]["authn_context_ref"],
            status=self.status(goal_id),
            at=event["created_at"],
        )

    def deliver(self, event: dict[str, Any]) -> bool:
        """An event that already has a receipt was sent: acknowledge it without projecting it
        again. The projection reads the goal's status now, so a redelivery after a lost
        acknowledgment would otherwise differ from what was sent and never pass replay."""
        if event["seq"] <= self.watermark:
            return True  # from before the notifier was first enabled: not flushed
        if self.receipts.get(event["event_id"]) is not None:
            return True
        try:
            projection = self.projection(event)
        except (KeyError, TypeError, AttributeError, RuntimeFault):
            # a malformed stop record never becomes valid: record it, do not retry (review P3-7)
            malformed = RuntimeFault("NOTIFY_INPUT", "The stop event cannot be projected")
            self.last_error = {"code": malformed.code, "http_status": None, "at": now()}
            self._reject(event, malformed)
            return True
        if projection is None:
            return True
        try:
            self.notifier.send(event["event_id"], projection)
        except RuntimeFault as fault:
            self.last_error = {
                "code": fault.code,
                "http_status": http_status(fault),
                "at": now(),
            }
            if fault.code == "NOTIFY_REJECTED":
                self._reject(event, fault)
                return True  # permanent: recorded, not retried
            raise  # transient: retried by the outbox
        except Exception as exc:
            self.last_error = {"code": type(exc).__name__, "http_status": None, "at": now()}
            raise
        return True

    def run_once(self, limit: int = 100) -> list[dict[str, Any]]:
        return OutboxPump(self.store).pump(self.deliver, limit=limit)

    def health(self) -> dict[str, Any]:
        """For the operator only: alerts not delivered yet, alerts refused for good, the last
        error. No goal id, no text."""
        with self.store._lock:
            pending = self.store.conn.execute(
                "SELECT COUNT(*), MIN(e.created_at) FROM outbox o JOIN events e "
                "ON e.seq=o.event_seq WHERE o.status='pending' AND e.tenant=? AND e.project=? "
                "AND e.event_type=? AND e.seq>?",
                (*self.scope.keys(), STOP_EVENT, self.watermark),
            ).fetchone()
            rejected = self.store.conn.execute(
                "SELECT COUNT(*) FROM commands WHERE tenant=? AND project=? AND actor=? "
                "AND operation=?",
                (*self.scope.keys(), self.actor, REJECTED),
            ).fetchone()[0]
        return {
            "undelivered": int(pending[0]),
            "undelivered_since": pending[1],
            "rejected": int(rejected),
            "last_error": self.last_error,
        }

    # -- background ------------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="amplai-notifier", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=30)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                while self.run_once() and not self._stop.is_set():
                    pass  # drain a backlog before waiting
            except Exception:  # a store fault waits for the next round
                pass
            self._stop.wait(self.interval)
