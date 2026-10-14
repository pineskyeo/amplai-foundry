"""Run the checks of one moment and record the run (Work 034 OD-18; spec M-17, M-19, M-20).

``amplai checks run --when <moment> [--target T ...]`` runs every registered check that declares
that moment and covers a requested target, in registry order, one at a time. A required check
that fails or errors stops the action: Hold ``CHECK_FAILED`` whose details carry each failed
check's id, version, status and findings (M-17). An optional check's failure is a warning.

- Protected checks (:data:`.builtin.BASE_PROTECTED` and every check declared ``protected``) must
  be registered (Hold ``CHECK_PROTECTED_MISSING``), run at each moment they declare whatever the
  targets, and can not be skipped (Hold ``CHECK_PROTECTED``) (M-19).
- ``--skip`` leaves out only a check in the committed lock's ``ci_external`` (a check another CI
  step runs: pytest, ruff, mypy); any other skip is Hold ``CHECK_SKIP_REFUSED``. A run with a skip
  ends ``pass_with_skips``, never ``pass``.
- A run that selects no check is Hold ``CHECK_NONE_SELECTED``.
- Every run writes one record under the record directory, pass or Hold, a Hold before any check
  ran included (M-20): ``check-run-<id>.json`` with the commit, the registry digest (the lock),
  the modules, and each check's id, version, status, findings and duration. When the record can
  not be written, a Hold keeps its details and adds ``record_error``; a pass becomes
  ``CHECK_RECORD``. The record stays a local file: where promotion and upgrade screens read it
  from (the product store) is not decided yet.

Checks are loaded through the module loader from :data:`..modules.builtin.BUILTIN_CODE`; this file
imports no check implementation.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from ..contracts.identity import new_id, now
from ..errors import Hold, RuntimeFault
from ..modules.builtin import BUILTIN_CODE, BUILTIN_MODULES
from ..modules.loader import load
from .builtin import BASE_PROTECTED, BUILTIN_CHECKS, CI_EXTERNAL
from .interface import WHEN, Check, CheckContext, CheckResult, Finding, covers, target_ok
from .lock import ci_external, entry_digest, lock_document, read_lock

RECORD_SCHEMA = "amplai.check-run.v1"
MAX_FINDINGS_IN_HOLD = 20


def builtin_checks() -> tuple[Check, ...]:
    """The built-in checks, built from their declarations and their registered code."""
    specs = {s.module_id: s for s in BUILTIN_MODULES if s.kind == "check"}
    declared = {c.check_id: c for c in BUILTIN_CHECKS}
    if set(specs) != set(declared):
        raise RuntimeFault(
            "CHECK_REGISTRY", "every check module has one declaration and the reverse",
            details={"modules_only": sorted(set(specs) - set(declared)),
                     "declarations_only": sorted(set(declared) - set(specs))},
        )  # fmt: skip
    out: list[Check] = []
    for check_id, declaration in declared.items():
        code = BUILTIN_CODE.get(check_id)
        if code is None or code.entry is None:
            raise RuntimeFault("CHECK_REGISTRY", "a check has no code", details={"check": check_id})
        factory = load(code.entry)
        if not callable(factory):
            raise RuntimeFault("CHECK_REGISTRY", "a check entry is not callable",
                               details={"check": check_id})  # fmt: skip
        check = factory(specs[check_id], declaration)
        out.append(check)
    return tuple(out)


def is_protected(check: Check) -> bool:
    return check.check.protected or check.check.check_id in BASE_PROTECTED


def _matches(declared: tuple[str, ...], target: str) -> bool:
    if target == "kind:*":
        return any(t.startswith("kind:") for t in declared)
    return covers(declared, target)


def select(checks: Iterable[Check], when: str, targets: Sequence[str]) -> list[Check]:
    """The checks of ``when`` that cover one of ``targets`` (every one when none is given); a
    protected check of ``when`` always."""
    return [
        c
        for c in checks
        if when in c.check.when
        and (not targets or is_protected(c) or any(_matches(c.check.targets, t) for t in targets))
    ]


def _commit(root: Path) -> str | None:
    try:
        done = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                              text=True, timeout=30, check=False)  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = done.stdout.strip()
    return value if done.returncode == 0 and value else None


def _run_one(check: Check, context: CheckContext) -> tuple[CheckResult, float]:
    started = time.monotonic()
    try:
        outcome = check.run(context)
        if not isinstance(outcome, CheckResult):
            raise RuntimeFault("CHECK_RESULT", "a check returns a CheckResult")
    except RuntimeFault as fault:
        finding = Finding(fault.code, fault.message, details={"details": fault.details})
        outcome = CheckResult("error", (finding,))
    except Exception as exc:  # any crash of a check is an error, never a pass
        outcome = CheckResult("error", (Finding("CHECK_CRASHED", f"{type(exc).__name__}: {exc}"),))
    return outcome, round(time.monotonic() - started, 3)


def _allowed_skips(root: Path) -> frozenset[str]:
    """``ci_external`` of the committed lock; none without a readable lock."""
    try:
        committed = read_lock(root)
    except RuntimeFault:
        return frozenset()
    return frozenset() if committed is None else ci_external(committed)


def run_checks(
    when: str,
    *,
    root: Path,
    record_dir: Path,
    targets: Sequence[str] = (),
    skip: Sequence[str] = (),
    base_ref: str | None = None,
    checks: Sequence[Check] | None = None,
    notes: Sequence[str] = (),
) -> dict[str, Any]:
    """Run the checks of ``when``; return the run summary, or Hold ``CHECK_FAILED`` after the
    record is written when a required check did not pass. ``notes`` are the caller's remarks
    (CI: why there is no base ref); they go into the record and the warnings."""
    if when not in WHEN:
        raise RuntimeFault("CHECK_WHEN", "unknown moment", details={"when": when, "known": WHEN})
    bad = [t for t in targets if not target_ok(t)]
    if bad:
        raise RuntimeFault("CHECK_TARGET", "targets are core, kind:<kind>, kind:* or pack:<id>",
                           details={"targets": bad})  # fmt: skip
    root = root.resolve()
    run_id = new_id("checkrun")
    started_at = now()
    registry = tuple(builtin_checks() if checks is None else checks)

    def record(outcome: str, results: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
        return {
            "schema": RECORD_SCHEMA,
            "run_id": run_id,
            "when": when,
            "targets": list(targets),
            "skipped": sorted(r["check_id"] for r in results if r["status"] == "skipped"),
            "requested_skips": sorted(skip),
            "base_ref": base_ref,
            "notes": list(notes),
            "started_at": started_at,
            "finished_at": now(),
            "commit": _commit(root),
            "registry_digest": entry_digest(
                lock_document((c.check for c in registry), CI_EXTERNAL)
            ),
            "modules": [s.ref for s in BUILTIN_MODULES],
            "outcome": outcome,
            "results": results,
            **extra,
        }

    def stored(document: dict[str, Any]) -> dict[str, str]:
        """Where the record went, or why it did not; never hides the run's own outcome."""
        try:
            return {"record": str(write_record(record_dir, document))}
        except OSError as exc:
            return {"record_error": f"{type(exc).__name__}: {exc}"}

    def hold(code: str, message: str, details: dict[str, Any]) -> Hold:
        where = stored(record("hold", [], hold={"code": code, "details": details}))
        return Hold(code, message, details={**details, **where})

    by_id = {c.check.check_id: c for c in registry}
    missing = sorted(BASE_PROTECTED - set(by_id))
    if missing:
        raise hold("CHECK_PROTECTED_MISSING", "a base protection check is not registered",
                   {"missing": missing})  # fmt: skip
    unknown = sorted(set(skip) - set(by_id))
    if unknown:
        raise RuntimeFault("CHECK_UNKNOWN", "no such check", details={"skip": unknown})
    protected = sorted(s for s in set(skip) if is_protected(by_id[s]))
    if protected:
        raise hold("CHECK_PROTECTED", "a protected check can not be skipped",
                   {"skip": protected})  # fmt: skip
    allowed = _allowed_skips(root)
    refused = sorted(set(skip) - allowed)
    if refused:
        raise hold("CHECK_SKIP_REFUSED", "only a check in the lock's ci_external can be skipped",
                   {"skip": refused, "ci_external": sorted(allowed)})  # fmt: skip
    selected = select(registry, when, targets)
    if not selected:
        raise hold("CHECK_NONE_SELECTED", "no registered check runs at this moment and target",
                   {"when": when, "targets": list(targets)})  # fmt: skip
    results: list[dict[str, Any]] = []
    for check in selected:
        declaration = check.check
        row: dict[str, Any] = {
            "check_id": declaration.check_id,
            "version": declaration.version,
            "module": check.spec.ref,
            "required": declaration.required,
            "protected": is_protected(check),
            "declaration_digest": entry_digest(declaration.entry()),
        }
        if declaration.check_id in skip:
            results.append({**row, "status": "skipped", "reason": "ci_external",
                            "findings": [], "details": {}, "seconds": 0.0})  # fmt: skip
            continue
        with tempfile.TemporaryDirectory(prefix="amplai-check-") as scratch:
            context = CheckContext(root, when, Path(scratch).resolve(), tuple(targets), base_ref)
            outcome, seconds = _run_one(check, context)
        results.append({
            **row,
            "status": outcome.status,
            "findings": [f.wire() for f in outcome.findings],
            "details": dict(outcome.details),
            "seconds": seconds,
        })  # fmt: skip
    failed = [r for r in results if r["required"] and r["status"] in ("fail", "error")]
    warnings = [*notes] + [f"{r['check_id']}: {r['status']}" for r in results
                if not r["required"] and r["status"] in ("fail", "error")]  # fmt: skip
    for r in results:
        warnings.extend(f"{r['check_id']}: {w}" for w in r["details"].get("warnings", ()))
    skipped = sorted(r["check_id"] for r in results if r["status"] == "skipped")
    outcome_word = "hold" if failed else "pass_with_skips" if skipped else "pass"
    where = stored(record(outcome_word, results, warnings=warnings))
    if failed:
        raise Hold(
            "CHECK_FAILED",
            "a required check did not pass",
            details={
                "when": when,
                **where,
                "failed": [
                    {"check_id": r["check_id"], "version": r["version"], "status": r["status"],
                     "findings": r["findings"][:MAX_FINDINGS_IN_HOLD],
                     "findings_total": len(r["findings"])}
                    for r in failed
                ],
            },
        )  # fmt: skip
    if "record" not in where:  # a pass without its record is not evidence (M-20)
        raise RuntimeFault("CHECK_RECORD", "the run passed but its record was not written",
                           details=where)  # fmt: skip
    return {
        "outcome": outcome_word,
        "run_id": run_id,
        "when": when,
        "record": where["record"],
        "skipped": skipped,
        "warnings": warnings,
        "results": [
            {"check_id": r["check_id"], "version": r["version"], "status": r["status"],
             "seconds": r["seconds"]}
            for r in results
        ],
    }  # fmt: skip


def write_record(record_dir: Path, record: dict[str, Any]) -> Path:
    record_dir.mkdir(parents=True, exist_ok=True)
    path = record_dir / f"check-run-{record['run_id']}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    return path
