"""Atomic filesystem ledger for idempotent Proposal actions and audit events."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from amplai_foundry.governance.models import StoredProposalAction


class ProposalActionLedgerError(RuntimeError):
    """The action ledger cannot be read or updated safely."""


class ProposalActionLedger(Protocol):
    def get(self, idempotency_key: str) -> StoredProposalAction | None: ...

    def record(self, action: StoredProposalAction) -> Path: ...


class FileProposalActionLedger:
    """One immutable transaction file per idempotency key."""

    def __init__(self, root: Path = Path(".amplai/audit/proposal-actions")) -> None:
        self.root = root

    def path_for(self, idempotency_key: str) -> Path:
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.json"

    def get(self, idempotency_key: str) -> StoredProposalAction | None:
        path = self.path_for(idempotency_key)
        if not path.exists():
            return None
        try:
            return StoredProposalAction.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as error:
            raise ProposalActionLedgerError(f"action ledger를 읽을 수 없습니다: {path}") from error

    def record(self, action: StoredProposalAction) -> Path:
        canonical_path = self.path_for(action.audit.idempotency_key)
        if canonical_path.exists():
            raise ProposalActionLedgerError("idempotency record가 이미 존재합니다.")
        self.root.mkdir(parents=True, exist_ok=True)
        payload = (
            json.dumps(
                action.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        descriptor = -1
        temporary: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{canonical_path.stem}.",
                suffix=".tmp",
                dir=self.root,
            )
            temporary = Path(temporary_name)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.link(temporary, canonical_path)
            temporary.unlink()
            return canonical_path
        except OSError as error:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise ProposalActionLedgerError(
                "action ledger를 원자적으로 기록할 수 없습니다."
            ) from error
