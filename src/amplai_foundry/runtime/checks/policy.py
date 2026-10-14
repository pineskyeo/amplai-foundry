"""``check.policy``: a check loosens only with a human approval (Work 034 M-19; protected).

1. ``checks.lock.json`` must equal the registry's lock form (``CHECK_LOCK_MISSING``,
   ``CHECK_LOCK_STALE``; ``amplai checks lock`` writes it). A PR that changes a check changes the
   lock in the same diff, so the lock on main is always the registry on main.
2. The registry is compared with the main-branch snapshot: the lock at ``--base-ref`` (CI passes
   the PR's base branch; without it, the committed lock). Every loosening (``lock.loosenings``:
   a removed check, ``required``/``protected`` lowered, ``when``/``targets`` narrowed, config
   loosened) needs an approval record (``CHECK_LOOSENING_UNAPPROVED``). Adding or tightening needs
   none.

An approval record is a JSON file in :data:`APPROVALS_DIR`::

    {"schema": "amplai.check-approval.v1", "check_id": "<id>",
     "before": "<entry digest at base>", "after": "<entry digest now>" | null,
     "approver": "<who>", "reason": "<why>", "approved_at": "<RFC 3339>"}

It binds to the exact before and after declarations, so it does not carry over to another change.
This check does not establish who wrote the file: that a human approver did is left to review of
the PR (an operator decision on CODEOWNERS or a signature is open).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from ..errors import RuntimeFault
from .builtin import BUILTIN_CHECKS
from .interface import BuiltinCheck, CheckContext, CheckResult, Finding, result
from .lock import LOCK_FILE, entry_digest, lock_document, loosenings, parse_lock, read_lock

APPROVALS_DIR = "docs/workstreams/v3-real-execution/check-approvals"
APPROVAL_SCHEMA = "amplai.check-approval.v1"
_FIELDS = {"schema", "check_id", "before", "after", "approver", "reason", "approved_at"}


def base_lock(root: Path, ref: str) -> tuple[dict[str, Any], str]:
    """The lock at ``ref`` and where it came from; an empty lock when ``ref`` has no lock file."""
    verify = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                            cwd=root, capture_output=True, timeout=60, check=False)  # fmt: skip
    if verify.returncode != 0:
        raise RuntimeFault(
            "CHECK_BASE_REF", "the base ref is not a commit here", details={"ref": ref}
        )
    shown = subprocess.run(["git", "show", f"{ref}:{LOCK_FILE}"], cwd=root, capture_output=True,
                           timeout=60, check=False)  # fmt: skip
    if shown.returncode != 0:
        return {"schema": None, "checks": {}}, f"{ref} (no {LOCK_FILE})"
    return parse_lock(shown.stdout, source=f"{ref}:{LOCK_FILE}"), f"{ref}:{LOCK_FILE}"


def approvals(root: Path) -> tuple[list[dict[str, Any]], list[Finding]]:
    found: list[dict[str, Any]] = []
    bad: list[Finding] = []
    folder = root / APPROVALS_DIR
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        rel = path.relative_to(root).as_posix()
        try:
            record = json.loads(path.read_bytes())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            bad.append(Finding("CHECK_APPROVAL_INVALID", "an approval record is not JSON", rel))
            continue
        if (
            not isinstance(record, dict)
            or set(record) != _FIELDS
            or record["schema"] != APPROVAL_SCHEMA
            or not all(isinstance(record[k], str) and record[k].strip()
                       for k in ("check_id", "before", "approver", "reason", "approved_at"))
            or not (record["after"] is None or isinstance(record["after"], str))
        ):  # fmt: skip
            bad.append(Finding("CHECK_APPROVAL_INVALID", "an approval record has another shape",
                               rel))  # fmt: skip
            continue
        found.append({**record, "path": rel})
    return found, bad


class CheckPolicyCheck(BuiltinCheck):
    def run(self, context: CheckContext) -> CheckResult:
        current = lock_document(BUILTIN_CHECKS)
        committed = read_lock(context.root)
        findings: list[Finding] = []
        if committed is None:
            findings.append(Finding("CHECK_LOCK_MISSING", f"{LOCK_FILE} is missing; run "
                                    "`amplai checks lock`", LOCK_FILE))  # fmt: skip
        elif committed != current:
            ids = sorted(
                k for k in set(committed["checks"]) | set(current["checks"])
                if committed["checks"].get(k) != current["checks"].get(k)
            )  # fmt: skip
            findings.append(Finding("CHECK_LOCK_STALE", f"{LOCK_FILE} differs from the registry; "
                                    "run `amplai checks lock`", LOCK_FILE,
                                    {"checks": ids}))  # fmt: skip
        if context.base_ref:
            base, source = base_lock(context.root, context.base_ref)
        else:
            base, source = committed or {"checks": {}}, f"working tree {LOCK_FILE}"
        records, invalid = approvals(context.root)
        findings.extend(invalid)
        used: list[str] = []
        loosened = loosenings(base, current)
        for item in loosened:
            match = [
                r for r in records
                if (r["check_id"], r["before"], r["after"])
                == (item["check_id"], item["before"], item["after"])
            ]  # fmt: skip
            if match:
                used.extend(r["path"] for r in match)
                continue
            findings.append(Finding(
                "CHECK_LOOSENING_UNAPPROVED",
                f"{item['check_id']} is loosened without an approval record (class C, M-19)",
                details={**item, "approvals_dir": APPROVALS_DIR},
            ))  # fmt: skip
        return result(findings, base=source, loosenings=loosened, approvals_used=used,
                      registry_digest=entry_digest(current))  # fmt: skip
