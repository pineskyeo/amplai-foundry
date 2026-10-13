"""Generation event cursors shared by the operator stream and the intake feed (Work 034 H-16).

A cursor is ``<store incarnation>:<seq>``. The incarnation is the store's generation
(``Store.incarnation``): a restart keeps it and a restore replaces it, so a cursor taken before a
restore cannot be read against another history. A cursor of another incarnation, or with a seq
past the newest event of the scope, is ``CURSOR_EXPIRED`` (409): the caller starts again from the
snapshot route named by ``snapshot_ref``, which returns the projection and the cursor to resume
from, read together (design/18 §4, AC-H6).
"""

from __future__ import annotations

import re
from typing import Any

from fastapi.responses import JSONResponse

from amplai_foundry.runtime.errors import RuntimeFault
from amplai_foundry.runtime.storage.store import Scope, Store

OPERATOR_SNAPSHOT = "/api/v3/snapshot"
_SEQ = re.compile(r"[0-9]{1,18}")  # ASCII digits only: str.isdigit() takes other scripts too


def newest_seq(store: Store, scope: Scope) -> int:
    with store._lock:
        row = store.conn.execute(
            "SELECT COALESCE(MAX(seq),0) FROM events WHERE tenant=? AND project=?", scope.keys()
        ).fetchone()
    return int(row[0])


def current(store: Store, scope: Scope) -> str:
    """The cursor after the scope's newest event."""
    return f"{store.incarnation}:{newest_seq(store, scope)}"


def parse(cursor: str) -> tuple[str, int]:
    """``<incarnation>:<seq>`` -> (incarnation, seq); anything else is ``EVENT_CURSOR`` (400)."""
    incarnation, _, seq = cursor.rpartition(":")
    if not incarnation or not _SEQ.fullmatch(seq):
        raise RuntimeFault("EVENT_CURSOR", "A cursor is <incarnation>:<seq>")
    return incarnation, int(seq)


def is_bare_seq(cursor: str) -> bool:
    """A bare number: the operator stream's cursor before H-16."""
    return _SEQ.fullmatch(cursor) is not None


def expired(store: Store, scope: Scope, incarnation: str, seq: int) -> bool:
    return incarnation != store.incarnation or seq > newest_seq(store, scope)


def expired_response(snapshot_ref: str) -> JSONResponse:
    """409 ``CURSOR_EXPIRED``. It carries no cursor: the snapshot's own cursor is the one to
    resume from, so no event between the projection and the cursor is skipped."""
    body: dict[str, Any] = {
        "code": "CURSOR_EXPIRED",
        "message": "The cursor is not of this store generation; start again from the snapshot",
        "outcome": "rejected",
        "snapshot_ref": snapshot_ref,
    }
    return JSONResponse(body, status_code=409)
