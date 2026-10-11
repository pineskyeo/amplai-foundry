"""Slack notifier: one Incoming Webhook to one operator channel (Work 034 S1c; OD-9, Q-10).

The webhook belongs to a Slack app separate from the front agent's (Q-10): it can only post to the
one channel it was created for, and the front agent never holds it. Its URL is a secret: it lives
in a store-external file that is read on every send with the same rules as the intake token file
(``keys.private_bytes``: a regular file, owner-only 0600, owned by the service user, no symlink in
the path). Replacing the file changes the destination at the next send; removing it stops sends
(each fails and the outbox retries later).

The message is rendered here from the projection's fixed fields only (H-8). No input field is
free text, so nothing the front agent wrote reaches the operator's channel through this path.

Only an ``https://hooks.slack.com/`` URL is used: the documented Incoming Webhook address. The
HTTP sender is injected (``post``): tests never reach the network.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from ..contracts.identity import digest, now
from ..errors import RuntimeFault
from ..modules.builtin import BUILTIN_MODULES
from ..modules.spec import ModuleSpec
from .interface import Projection, Receipt, ReceiptLog, check_input, replay

MODULE_ID = "notifier.slack_webhook"
WEBHOOK_HOST = "hooks.slack.com"

# (url, JSON body) -> HTTP status
Post = Callable[[str, bytes], int]

_ACTION_TEXT = {"pause": "paused", "cancel": "asked to cancel"}


def http_post(url: str, body: bytes) -> int:
    """The production sender: one POST, no redirect, bounded time."""
    response = httpx.post(
        url,
        content=body,
        headers={"Content-Type": "application/json"},
        timeout=10.0,
        follow_redirects=False,
    )
    return response.status_code


def render(projection: Projection) -> str:
    """The operator's message, from fixed fields only."""
    action = _ACTION_TEXT[projection.action]
    return (
        f"AMPLAI: the front agent ({projection.by}) {action} goal {projection.goal_id}. "
        f"Status: {projection.status}. At: {projection.at}. "
        f"Check with `amplai status {projection.goal_id}`."
    )


def spec() -> ModuleSpec:
    (found,) = (s for s in BUILTIN_MODULES if s.module_id == MODULE_ID)
    return found


class SlackWebhookNotifier:
    def __init__(
        self,
        url_file: Path,
        *,
        read: Callable[[Path], bytes],
        receipts: ReceiptLog,
        post: Post = http_post,
        clock: Callable[[], str] = now,
    ) -> None:
        self.url_file, self.read, self.receipts = url_file, read, receipts
        self.post, self.clock = post, clock
        self._spec = spec()

    @property
    def spec(self) -> ModuleSpec:
        return self._spec

    def _url(self) -> str:
        try:
            url = self.read(self.url_file).decode().strip()
        except OSError as exc:
            raise RuntimeFault("NOTIFY_UNAVAILABLE", "The webhook URL file is missing") from exc
        except (RuntimeFault, ValueError) as exc:  # not owner-only, a symlink, not text
            raise RuntimeFault(
                "NOTIFY_UNAVAILABLE", "The webhook URL file cannot be trusted"
            ) from exc
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname != WEBHOOK_HOST or parts.port is not None:
            raise RuntimeFault("NOTIFY_UNAVAILABLE", "The webhook URL is not a Slack webhook")
        return url

    def send(self, event_id: str, projection: Projection) -> Receipt:
        event_id, projection = check_input(event_id, projection)
        earlier = replay(self.receipts, event_id, projection)
        if earlier is not None:
            return earlier
        body = json.dumps({"text": render(projection)}).encode()
        status = self.post(self._url(), body)
        if not 200 <= status < 300:
            # the status only: Slack's response body is not carried anywhere
            raise RuntimeFault("NOTIFY_FAILED", f"The webhook answered HTTP {status}")
        receipt = Receipt(
            event_id=event_id,
            module=self._spec.ref,
            projection_digest=digest(projection.wire()),
            sent_at=self.clock(),
        )
        self.receipts.put(receipt)
        return receipt
