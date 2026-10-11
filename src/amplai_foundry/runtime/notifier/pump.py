"""Operator alerts from the product store outbox (Work 034 S1c; spec H-3, F-5, AC-F3).

Every store event gets an outbox row in the same transaction (``Store.event``), and
``OutboxPump`` delivers pending rows at least once with stable event ids, backing off on failure.
This module is the outbox's consumer when a notifier is configured:

- ``intake.stop_requested`` (a front agent paused or cancelled a goal, H-3) becomes a
  :class:`~.interface.Projection` and is sent through the notifier. A failed send is not
  acknowledged, so the outbox retries it later; a send that succeeded but whose acknowledgment was
  lost is a receipt replay on the next delivery, not a second message (F-5).
- Every other event is acknowledged without a send: it is not an operator alert in v1.

The alert reads only the store, never the intake configuration, so it is sent with the front agent
turned off or its adapter removed (AC-F3).

The outbox has one status per event, so acknowledging here acknowledges for every consumer: this
is the only consumer the code has today (``OutboxPump`` has no other caller in ``src``).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from ..contracts.intake import STOP_EVENT
from ..recovery.service import OutboxPump
from ..storage.store import Scope, Store
from .interface import Notifier, Projection


class NotifierPump:
    def __init__(
        self,
        store: Store,
        scope: Scope,
        notifier: Notifier,
        status: Callable[[str], str],
        *,
        interval: float = 5.0,
    ) -> None:
        self.store, self.scope, self.notifier, self.status = store, scope, notifier, status
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

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
        projection = self.projection(event)
        if projection is not None:
            self.notifier.send(event["event_id"], projection)  # raises: retried by the outbox
        return True

    def run_once(self, limit: int = 100) -> list[dict[str, Any]]:
        return OutboxPump(self.store).pump(self.deliver, limit=limit)

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
