"""``check.policy``: a check loosens only with a human approval (Work 034 M-19; protected).

1. ``checks.lock.json`` must equal the registry's lock form (``CHECK_LOCK_MISSING``,
   ``CHECK_LOCK_STALE``; ``amplai checks lock`` writes it). A change to a check changes the lock in
   the same diff, so the lock on main is always the registry on main.
2. The registry is compared with the main-branch snapshot, the lock at ``--base-ref`` (CI passes
   the PR's base commit, or the previous main commit on a push). Every loosening
   (``lock.loosenings``) needs an approval record (``CHECK_LOOSENING_UNAPPROVED``). Adding or
   tightening needs none. Without ``--base-ref`` the committed lock is the base, so a loosening
   against main is not checked; the result says so as a warning.
3. With ``--base-ref`` the decision (:func:`evaluate`: what a loosening is, which approval counts)
   is made by the base ref's copy of this file: the check extracts ``src/amplai_foundry`` at that
   ref (``git archive``) into its work directory and runs that copy's :func:`evaluate_json` in a
   subprocess with the registry's lock as input. That protects against a change that edits
   :func:`evaluate` or ``lock.loosenings``. It does not protect against a change that edits the
   caller (this class, the runner, ``checks_cli.py``) or the CI workflow: that needs repository
   protection (CODEOWNERS and required review on ``runtime/checks/**``, ``runtime/checks_cli.py``,
   ``.github/workflows/ci.yml``, ``checks.lock.json``, the approvals directory,
   ``runtime/evidence/cas.py``, and the check as a required status), an open operator decision.
   Only when the base ref has no ``runtime/checks/policy.py`` (``git cat-file -e``, confirmed by
   an empty ``git ls-tree``) does this tree's code decide (bootstrap, labelled in
   ``policy_code``); any other failure to read, extract or run the base code is
   ``CHECK_POLICY_BASE`` (fail closed).
4. Lock schema change: a lock whose ``schema`` this code does not know can not be compared check by
   check, so the whole lock change is one loosening, ``check_id`` ``checks.lock.json`` with the
   digests of the base lock and the new lock, and needs an approval record. Without one, migrate in
   two steps: first a change that teaches :func:`evaluate` and ``lock.py`` the new schema while the
   lock keeps the old one; then a change that writes the new lock, judged by the base code that now
   knows it.

An approval record is a JSON file in :data:`APPROVALS_DIR`::

    {"schema": "amplai.check-approval.v1", "check_id": "<id>",
     "before": "<bound digest at base>", "after": "<bound digest now>" | null,
     "approver": "<who>", "reason": "<why>", "approved_at": "<RFC 3339 UTC, not in the future>"}

It binds to the exact before and after declarations (``lock.bound_digest``). It is single use: a
record counts only in the change that adds it (a record already in the base ref is
``CHECK_APPROVAL_REUSED``), and two records for the same ``(check_id, before, after)`` are
``CHECK_APPROVAL_DUPLICATE``. This check does not establish that a human wrote the record: that
needs the repository protection above or a signature, an open operator decision.
"""

from __future__ import annotations

import io
import json
import re
import subprocess
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from ..contracts.identity import digest
from ..errors import RuntimeFault
from .builtin import BUILTIN_CHECKS, CI_EXTERNAL
from .interface import BuiltinCheck, CheckContext, CheckResult, Finding
from .lock import LOCK_FILE, LOCK_SCHEMA, entry_digest, lock_document, loosenings, parse_lock

POLICY_API = 1  # what the PR side calls in the base ref's copy: evaluate_json(root, ref, current)
POLICY_PATH = "src/amplai_foundry/runtime/checks/policy.py"
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


def _base_fault(
    why: str, ref: str, done: subprocess.CompletedProcess[bytes] | None = None
) -> RuntimeFault:
    stderr = done.stderr.decode(errors="replace")[-1000:] if done is not None else None
    return RuntimeFault("CHECK_POLICY_BASE", why, details={"ref": ref, "stderr_tail": stderr})


def _verify_commit(root: Path, ref: str) -> None:
    if _git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode != 0:
        raise RuntimeFault(
            "CHECK_BASE_REF", "the base ref is not a commit here", details={"ref": ref}
        )


def path_absent(root: Path, ref: str, path: str) -> bool:
    """True only when ``path`` is certainly not in ``ref``: ``git cat-file -e`` fails and
    ``git ls-tree`` succeeds with no entry. Any other answer is CHECK_POLICY_BASE."""
    if _git(root, "cat-file", "-e", f"{ref}:{path}").returncode == 0:
        return False
    listed = _git(root, "ls-tree", "--name-only", ref, "--", path)
    if listed.returncode != 0:
        raise _base_fault(f"can not tell whether {path} is in the base ref", ref, listed)
    if listed.stdout.strip():
        raise _base_fault(f"{path} is in the base ref but can not be read", ref, listed)
    return True


def base_lock(root: Path, ref: str) -> tuple[dict[str, Any], str]:
    """The lock at ``ref`` and where it came from; an empty lock when ``ref`` has no lock file."""
    _verify_commit(root, ref)
    if path_absent(root, ref, LOCK_FILE):
        return {"schema": None, "checks": {}, "ci_external": []}, f"{ref} (no {LOCK_FILE})"
    shown = _git(root, "show", f"{ref}:{LOCK_FILE}")
    if shown.returncode != 0:
        raise _base_fault(f"the base ref's {LOCK_FILE} can not be read", ref, shown)
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
    """The approval records this change adds; findings for invalid, reused or duplicate ones.
    A record is ``reused`` when the base ref already has a record with the same
    ``(check_id, before, after)``, under any path (a copy under a new name is not a new approval).
    A base record that can not be read is ``CHECK_POLICY_BASE`` (fail closed)."""
    found: list[dict[str, Any]] = []
    bad: list[Finding] = []
    in_base: set[str] = set()
    base_keys: set[tuple[Any, Any, Any]] = set()
    if ref is not None:
        listed = _git(root, "ls-tree", "-r", "--name-only", ref, "--", APPROVALS_DIR)
        if listed.returncode != 0:
            raise _base_fault("the base ref's approval records can not be listed", ref, listed)
        in_base = {line for line in listed.stdout.decode().splitlines() if line}
        for rel in sorted(in_base):
            shown = _git(root, "show", f"{ref}:{rel}")
            if shown.returncode != 0:
                raise _base_fault(f"the base ref's approval record {rel} can not be read", ref,
                                  shown)  # fmt: skip
            try:
                old = json.loads(shown.stdout)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue  # not a record: it can not match a key
            if isinstance(old, dict):
                base_keys.add((old.get("check_id"), old.get("before"), old.get("after")))
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
        key = (record["check_id"], record["before"], record["after"])
        if rel in in_base or key in base_keys:
            record["reused"] = True  # an earlier change used it (this file or a copy of it)
        else:
            if key in seen:
                bad.append(Finding("CHECK_APPROVAL_DUPLICATE", "two approval records for one "
                                   "change", rel, {"other": seen[key]}))  # fmt: skip
                continue
            seen[key] = rel
        found.append({**record, "path": rel})
    return found, bad


def _read_committed(root: Path) -> dict[str, Any] | None:
    """The committed lock as JSON, whatever its schema; ``CHECK_LOCK`` when it is not JSON."""
    path = root / LOCK_FILE
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeFault("CHECK_LOCK", "the check lock is not JSON",
                           details={"source": LOCK_FILE, "error": str(exc)}) from exc  # fmt: skip
    if not isinstance(value, dict):
        raise RuntimeFault("CHECK_LOCK", "the check lock is not an object")
    return value


def _schema_change(base: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    return {
        "check_id": LOCK_FILE,
        "before": digest(base),
        "after": digest(current),
        "protected": True,
        "changes": [{"what": "lock_schema_unknown", "schema": current.get("schema")}],
    }


def evaluate(root: Path, base_ref: str | None, current: dict[str, Any]) -> CheckResult:
    """The policy decision for the tree at ``root`` whose registry lock is ``current``."""
    committed = _read_committed(root)
    findings: list[Finding] = []
    warnings: list[str] = []
    if committed is None:
        findings.append(Finding("CHECK_LOCK_MISSING", f"{LOCK_FILE} is missing; run "
                                "`amplai checks lock`", LOCK_FILE))  # fmt: skip
    elif committed != current:
        keys = set(committed) | set(current)
        checks_old = (
            committed.get("checks", {}) if isinstance(committed.get("checks"), dict) else {}
        )
        checks_new = current.get("checks", {}) if isinstance(current.get("checks"), dict) else {}
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
        known = committed is not None and committed.get("schema") == LOCK_SCHEMA
        base = committed if known and committed is not None else {"checks": {}}
        source = f"working tree {LOCK_FILE}"
        warnings.append(NO_BASE_WARNING)
    records, invalid = approvals(root, base_ref)
    findings.extend(invalid)
    used: list[str] = []
    if current.get("schema") == LOCK_SCHEMA:
        loosened = loosenings(base, current)
    else:  # a schema this code can not read check by check: the whole lock is one loosening
        loosened = [_schema_change(base, current)]
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


def _safe_extract(tar: tarfile.TarFile, into: Path) -> None:
    """Extract regular files and directories only, inside ``into``. Uses the ``data`` filter when
    this Python has it (3.11.4+); otherwise refuses links, devices, absolute names and ``..``
    members itself."""
    if hasattr(tarfile, "data_filter"):
        tar.extractall(into, filter="data")
        return
    members = []
    for member in tar.getmembers():
        name = PurePosixPath(member.name)
        if name.is_absolute() or ".." in name.parts or not (member.isfile() or member.isdir()):
            raise RuntimeFault("CHECK_POLICY_BASE", "the base archive has an unsafe member",
                               details={"member": member.name})  # fmt: skip
        members.append(member)
    tar.extractall(into, members=members)  # members checked above


def base_source(root: Path, ref: str, into: Path) -> Path | None:
    """``src`` of ``ref`` extracted under ``into``. ``None`` only when ``ref`` certainly has no
    ``runtime/checks/policy.py`` (bootstrap); any other failure is ``CHECK_POLICY_BASE``."""
    _verify_commit(root, ref)
    if path_absent(root, ref, POLICY_PATH):
        return None
    archive = _git(root, "archive", "--format=tar", ref, "src/amplai_foundry")
    if archive.returncode != 0:
        raise _base_fault("the base ref's source can not be archived", ref, archive)
    try:
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
            _safe_extract(tar, into)
    except (tarfile.TarError, OSError) as exc:
        raise RuntimeFault("CHECK_POLICY_BASE", "the base ref's source can not be extracted",
                           details={"ref": ref, "error": str(exc)}) from exc  # fmt: skip
    policy = into / POLICY_PATH
    if not policy.is_file() or not re.search(rb"^POLICY_API = 1\b", policy.read_bytes(), re.M):
        raise RuntimeFault("CHECK_POLICY_BASE", "the base ref's check policy has another API",
                           details={"ref": ref, "api": POLICY_API})  # fmt: skip
    return into / "src"


class CheckPolicyCheck(BuiltinCheck):
    def run(self, context: CheckContext) -> CheckResult:
        current = lock_document(BUILTIN_CHECKS, CI_EXTERNAL)
        if not context.base_ref:
            own = evaluate(context.root, None, current)
            details = {**own.details, "policy_code": "this tree (no --base-ref)"}
            return CheckResult(own.status, own.findings, details)
        src = base_source(context.root, context.base_ref, context.workdir / "base")
        if src is None:
            own = evaluate(context.root, context.base_ref, current)
            label = f"this tree (bootstrap: {context.base_ref} has no {POLICY_PATH})"
            return CheckResult(own.status, own.findings, {**own.details, "policy_code": label})
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
        try:
            decided = json.loads(done.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError) as exc:
            raise RuntimeFault("CHECK_POLICY_BASE", "the base ref's check policy gave no result",
                               details={"stdout_tail": done.stdout[-2000:]}) from exc  # fmt: skip
        findings = tuple(
            Finding(f["code"], f["message"], f.get("path"), f.get("details"))
            for f in decided["findings"]
        )
        details = {**decided["details"], "policy_code": f"{context.base_ref} (base ref code)"}
        return CheckResult(decided["status"], findings, details)
