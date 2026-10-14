"""``check.policy``: a check loosens only with a human approval (Work 034 M-19; protected).

1. ``checks.lock.json`` must equal the registry's lock form (``CHECK_LOCK_MISSING``,
   ``CHECK_LOCK_STALE``; ``amplai checks lock`` writes it). A change to a check changes the lock in
   the same diff, so the lock on main is always the registry on main.
2. The registry is compared with the main-branch snapshot, the lock at ``--base-ref`` (CI passes
   the PR's base branch, or the previous main commit on a push). Every loosening
   (``lock.loosenings``) needs an approval record (``CHECK_LOOSENING_UNAPPROVED``). Adding or
   tightening needs none. Without ``--base-ref`` the committed lock is the base, so a loosening
   against main is not checked; the result says so as a warning.
3. With ``--base-ref`` the decision is made by the base ref's policy code, not this tree's: the
   check extracts ``src/amplai_foundry`` at that ref (``git archive``) into its work directory and
   runs that copy's :func:`evaluate_json` in a subprocess with the registry's lock as input. A
   change can then not loosen the policy that judges it. When the base ref has no policy code of
   this API (:data:`POLICY_API`), this tree's code decides and the result says so (bootstrap).

An approval record is a JSON file in :data:`APPROVALS_DIR`::

    {"schema": "amplai.check-approval.v1", "check_id": "<id>",
     "before": "<bound digest at base>", "after": "<bound digest now>" | null,
     "approver": "<who>", "reason": "<why>", "approved_at": "<RFC 3339 UTC, not in the future>"}

It binds to the exact before and after declarations (``lock.bound_digest``). It is single use: a
record counts only in the change that adds it (a record already in the base ref is
``CHECK_APPROVAL_REUSED``), and two records for the same ``(check_id, before, after)`` are
``CHECK_APPROVAL_DUPLICATE``. This check does not establish that a human wrote the record: that
needs a repository rule (CODEOWNERS and a required status check) or a signature, an open operator
decision.
"""

from __future__ import annotations

import io
import json
import re
import subprocess
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..errors import RuntimeFault
from .builtin import BUILTIN_CHECKS, CI_EXTERNAL
from .interface import BuiltinCheck, CheckContext, CheckResult, Finding
from .lock import LOCK_FILE, entry_digest, lock_document, loosenings, parse_lock, read_lock

POLICY_API = 1  # what the PR side calls in the base ref's copy: evaluate_json(root, ref, current)
APPROVALS_DIR = "docs/workstreams/v3-real-execution/check-approvals"
APPROVAL_SCHEMA = "amplai.check-approval.v1"
_FIELDS = {"schema", "check_id", "before", "after", "approver", "reason", "approved_at"}
_AT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?Z")
NO_BASE_WARNING = "no --base-ref: loosening against main is not checked"
BASE_TIMEOUT_SECONDS = 300

# Run in a subprocess with the base ref's source first on the path. The editable install's own
# finder (if any) is dropped so that ``amplai_foundry`` is the extracted copy, and that is asserted.
_BOOTSTRAP = """
import json, sys
src, root, ref, current = sys.argv[1:5]
sys.meta_path[:] = [f for f in sys.meta_path if "editable" not in repr(type(f)).lower()]
sys.path.insert(0, src)
import amplai_foundry
if not amplai_foundry.__file__.startswith(src):
    raise SystemExit("the base policy did not load from the base ref")
from amplai_foundry.runtime.checks import policy
with open(current, encoding="utf-8") as handle:
    print(json.dumps(policy.evaluate_json(root, ref, json.load(handle))))
"""


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, timeout=120, check=False)


def base_lock(root: Path, ref: str) -> tuple[dict[str, Any], str]:
    """The lock at ``ref`` and where it came from; an empty lock when ``ref`` has no lock file."""
    if _git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode != 0:
        raise RuntimeFault(
            "CHECK_BASE_REF", "the base ref is not a commit here", details={"ref": ref}
        )
    shown = _git(root, "show", f"{ref}:{LOCK_FILE}")
    if shown.returncode != 0:
        return {"schema": None, "checks": {}, "ci_external": []}, f"{ref} (no {LOCK_FILE})"
    return parse_lock(shown.stdout, source=f"{ref}:{LOCK_FILE}"), f"{ref}:{LOCK_FILE}"


def _approved_at_ok(value: str) -> bool:
    if not _AT.fullmatch(value):
        return False
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return at <= datetime.now(UTC)


def approvals(root: Path, ref: str | None) -> tuple[list[dict[str, Any]], list[Finding]]:
    """The approval records this change adds; findings for invalid, reused or duplicate ones."""
    found: list[dict[str, Any]] = []
    bad: list[Finding] = []
    in_base: set[str] = set()
    if ref is not None:
        listed = _git(root, "ls-tree", "-r", "--name-only", ref, "--", APPROVALS_DIR)
        in_base = {line for line in listed.stdout.decode().splitlines() if line}
    folder = root / APPROVALS_DIR
    seen: dict[tuple[str, str, str | None], str] = {}
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
            or not _approved_at_ok(record["approved_at"])
        ):  # fmt: skip
            bad.append(Finding("CHECK_APPROVAL_INVALID", "an approval record has another shape "
                               "or approved_at is not a past RFC 3339 UTC time", rel))  # fmt: skip
            continue
        if rel in in_base:
            continue  # an earlier change used it: matched again, it is CHECK_APPROVAL_REUSED
        key = (record["check_id"], record["before"], record["after"])
        if key in seen:
            bad.append(Finding("CHECK_APPROVAL_DUPLICATE", "two approval records for one change",
                               rel, {"other": seen[key]}))  # fmt: skip
            continue
        seen[key] = rel
        found.append({**record, "path": rel})
    reused: list[dict[str, Any]] = []
    for rel in sorted(in_base):
        path = root / rel
        if path.is_file():
            try:
                record = json.loads(path.read_bytes())
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(record, dict):
                reused.append({**record, "path": rel})
    return found + [{**r, "reused": True} for r in reused], bad


def evaluate(root: Path, base_ref: str | None, current: dict[str, Any]) -> CheckResult:
    """The policy decision for the tree at ``root`` whose registry lock is ``current``."""
    committed = read_lock(root)
    findings: list[Finding] = []
    warnings: list[str] = []
    if committed is None:
        findings.append(Finding("CHECK_LOCK_MISSING", f"{LOCK_FILE} is missing; run "
                                "`amplai checks lock`", LOCK_FILE))  # fmt: skip
    elif committed != current:
        keys = set(committed) | set(current)
        checks_old, checks_new = committed.get("checks", {}), current.get("checks", {})
        ids = sorted(
            k for k in set(checks_old) | set(checks_new) if checks_old.get(k) != checks_new.get(k)
        )
        fields = sorted(k for k in keys if committed.get(k) != current.get(k))
        findings.append(Finding("CHECK_LOCK_STALE", f"{LOCK_FILE} differs from the registry; "
                                "run `amplai checks lock`", LOCK_FILE,
                                {"checks": ids, "fields": fields}))  # fmt: skip
    if base_ref:
        base, source = base_lock(root, base_ref)
    else:
        base, source = committed or {"checks": {}}, f"working tree {LOCK_FILE}"
        warnings.append(NO_BASE_WARNING)
    records, invalid = approvals(root, base_ref)
    findings.extend(invalid)
    used: list[str] = []
    loosened = loosenings(base, current)
    for item in loosened:
        key = (item["check_id"], item["before"], item["after"])
        match = [r for r in records if (r["check_id"], r["before"], r["after"]) == key]
        fresh = [r for r in match if not r.get("reused")]
        if fresh:
            used.extend(r["path"] for r in fresh)
            continue
        code = "CHECK_APPROVAL_REUSED" if match else "CHECK_LOOSENING_UNAPPROVED"
        message = (
            f"{item['check_id']}: the approval record is already on the base ref (single use)"
            if match
            else f"{item['check_id']} is loosened without an approval record (class C, M-19)"
        )
        findings.append(Finding(code, message, details={**item, "approvals_dir": APPROVALS_DIR}))
    details: dict[str, Any] = {
        "base": source,
        "loosenings": loosened,
        "approvals_used": used,
        "registry_digest": entry_digest(current),
    }
    if warnings:
        details["warnings"] = warnings
    return CheckResult("fail" if findings else "pass", tuple(findings), details)


def evaluate_json(root: str, base_ref: str | None, current: dict[str, Any]) -> dict[str, Any]:
    """:func:`evaluate` as JSON, the entry the PR side calls in the base ref's copy (API 1)."""
    outcome = evaluate(Path(root), base_ref, current)
    return {"status": outcome.status, "findings": [f.wire() for f in outcome.findings],
            "details": dict(outcome.details)}  # fmt: skip


def _base_source(root: Path, ref: str, into: Path) -> Path | None:
    """``src`` of ``ref`` extracted under ``into``; ``None`` when that ref has no policy API."""
    if _git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode != 0:
        raise RuntimeFault(
            "CHECK_BASE_REF", "the base ref is not a commit here", details={"ref": ref}
        )
    archive = _git(root, "archive", "--format=tar", ref, "src/amplai_foundry")
    if archive.returncode != 0:
        return None
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(into, filter="data")
    policy = into / "src/amplai_foundry/runtime/checks/policy.py"
    if not policy.is_file() or not re.search(rb"^POLICY_API = 1\b", policy.read_bytes(), re.M):
        return None
    return into / "src"


class CheckPolicyCheck(BuiltinCheck):
    def run(self, context: CheckContext) -> CheckResult:
        current = lock_document(BUILTIN_CHECKS, CI_EXTERNAL)
        if not context.base_ref:
            return evaluate(context.root, None, current)
        src = _base_source(context.root, context.base_ref, context.workdir / "base")
        if src is None:
            own = evaluate(context.root, context.base_ref, current)
            details = {**own.details, "policy_code": "this tree (the base ref has no check policy)"}
            return CheckResult(own.status, own.findings, details)
        current_file = context.workdir / "current-lock.json"
        current_file.write_text(json.dumps(current))
        done = subprocess.run(
            [sys.executable, "-c", _BOOTSTRAP, str(src.resolve()), str(context.root),
             context.base_ref, str(current_file)],
            cwd=context.workdir, capture_output=True, text=True, timeout=BASE_TIMEOUT_SECONDS,
            check=False,
        )  # fmt: skip
        if done.returncode != 0:
            raise RuntimeFault("CHECK_POLICY_BASE", "the base ref's check policy did not run",
                               details={"stderr_tail": done.stderr[-2000:]})  # fmt: skip
        decided = json.loads(done.stdout.strip().splitlines()[-1])
        findings = tuple(
            Finding(f["code"], f["message"], f.get("path"), f.get("details"))
            for f in decided["findings"]
        )
        details = {**decided["details"], "policy_code": f"{context.base_ref} (base ref)"}
        return CheckResult(decided["status"], findings, details)
