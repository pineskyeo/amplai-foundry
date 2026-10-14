"""The Slack notifier wired to a test transport, for its conformance suite (Work 034 M-2, M-16).

The kind conformance check (``runtime/checks/kind_conformance.py``) builds the module with
:func:`subject` and runs ``conformance.notifier.v1`` against it. Nothing leaves the process: the
sender records each POST and answers 200, and the webhook URL file is a 0600 file in the check's
own work directory with a URL that names no real workspace.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..keys import private_bytes
from .interface import MemoryReceipts
from .slack import SlackWebhookNotifier

CONFORMANCE_WEBHOOK = "https://hooks.slack.com/services/T000/B000/conformance-only"


class RecordingPost:
    """The test sender: records every ``(url, body)`` and answers with ``status``."""

    def __init__(self, status: int = 200) -> None:
        self.calls: list[tuple[str, bytes]] = []
        self.status = status

    def __call__(self, url: str, body: bytes) -> int:
        self.calls.append((url, body))
        return self.status


def subject(workdir: Path) -> SlackWebhookNotifier:
    """The Slack notifier with a recording sender and a 0600 webhook URL file under ``workdir``."""
    url_file = workdir.resolve() / "notifier.url"
    url_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(url_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(CONFORMANCE_WEBHOOK + "\n")
    os.chmod(url_file, 0o600)
    return SlackWebhookNotifier(
        url_file, read=private_bytes, receipts=MemoryReceipts(), post=RecordingPost()
    )
