"""The ``notifier`` module kind (Work 034 S1c, spec F-5).

Only the interface and the store outbox pump are exported here. The first implementation, Slack
(``notifier/slack.py``), is imported by the composition root that selects it from configuration
(``runtime/local_deployment.py``), like ``git_host`` (M-12, M-13).
"""

from __future__ import annotations

from .interface import (
    NOTIFY_EVENTS,
    SUITE,
    MemoryReceipts,
    Notifier,
    Projection,
    Receipt,
    ReceiptLog,
    StoreReceipts,
)
from .pump import NotifierPump

__all__ = [
    "NOTIFY_EVENTS",
    "SUITE",
    "MemoryReceipts",
    "Notifier",
    "NotifierPump",
    "Projection",
    "Receipt",
    "ReceiptLog",
    "StoreReceipts",
]
