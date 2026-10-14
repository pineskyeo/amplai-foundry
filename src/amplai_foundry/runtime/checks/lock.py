"""``checks.lock.json`` and loosening detection (Work 034 M-19).

The lock is the committed form of the check registry and of ``ci_external`` (the checks CI may
skip because another step runs them). On main it is the snapshot a change is compared against.
These are loosenings:

- a removed check; ``required`` or ``protected`` lowered; ``when`` or ``targets`` narrowed;
- ``config`` loosened: a removed item of a coverage list, an added item of an allow list
  (``loosens_when_added``), a coverage list turned into an allow list;
- ``version`` lowered (dotted numbers; a version that does not compare as numbers counts as
  lowered);
- a check put in ``ci_external``.

A loosening needs a human approval record (class C); adding or tightening does not. An approval
binds to :func:`bound_digest` of the check before and after: its lock entry with its
``ci_external`` membership. A change to a check's code that loosens it without touching its
declaration is not detectable here; review sees it, and its ``version`` should be raised.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from ..contracts.identity import digest
from ..errors import RuntimeFault
from .interface import CheckSpec, covers

LOCK_FILE = "checks.lock.json"
LOCK_SCHEMA = "amplai.checks-lock.v1"


def lock_document(checks: Iterable[CheckSpec], external: Iterable[str]) -> dict[str, Any]:
    return {
        "schema": LOCK_SCHEMA,
        "checks": {c.check_id: c.entry() for c in sorted(checks, key=lambda c: c.check_id)},
        "ci_external": sorted(external),
    }


def ci_external(document: Mapping[str, Any]) -> frozenset[str]:
    values = document.get("ci_external", [])
    if not isinstance(values, list):
        return frozenset()
    return frozenset(v for v in values if isinstance(v, str))


def lock_bytes(document: Mapping[str, Any]) -> bytes:
    """The file bytes: indented JSON with sorted keys and a final newline (reviewable diffs)."""
    return (json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def parse_lock(raw: bytes, *, source: str) -> dict[str, Any]:
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeFault("CHECK_LOCK", "the check lock is not JSON",
                           details={"source": source, "error": str(exc)}) from exc  # fmt: skip
    if (
        not isinstance(document, dict)
        or document.get("schema") != LOCK_SCHEMA
        or not isinstance(document.get("checks"), dict)
        or not isinstance(document.get("ci_external", []), list)
    ):
        raise RuntimeFault(
            "CHECK_LOCK", "the check lock has another shape", details={"source": source}
        )
    return document


def entry_digest(entry: Mapping[str, Any] | None) -> str | None:
    return None if entry is None else digest(dict(entry))


def bound_digest(entry: Mapping[str, Any] | None, external: bool) -> str | None:
    """What an approval binds to: the check's lock entry and whether CI may skip it."""
    return None if entry is None else digest({**dict(entry), "ci_external": external})


def _numbers(version: object) -> tuple[int, ...] | None:
    if not isinstance(version, str) or not version:
        return None
    parts = version.split(".")
    return tuple(int(p) for p in parts) if all(p.isdigit() for p in parts) else None


def _loosened(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if before.get("version") != after.get("version"):
        old_version, new_version = _numbers(before.get("version")), _numbers(after.get("version"))
        if old_version is None or new_version is None or new_version < old_version:
            out.append({"what": "version_lowered", "before": before.get("version"),
                        "after": after.get("version")})  # fmt: skip
    if before.get("required") and not after.get("required"):
        out.append({"what": "required_lowered"})
    if before.get("protected") and not after.get("protected"):
        out.append({"what": "protected_lowered"})
    dropped = sorted(set(before.get("when", ())) - set(after.get("when", ())))
    if dropped:
        out.append({"what": "when_narrowed", "removed": dropped})
    targets = frozenset(after.get("targets", ()))
    uncovered = sorted(t for t in before.get("targets", ()) if not covers(targets, t))
    if uncovered:
        out.append({"what": "targets_narrowed", "removed": uncovered})
    old_config: Mapping[str, list[str]] = before.get("config", {})
    new_config: Mapping[str, list[str]] = after.get("config", {})
    allow_before = set(before.get("loosens_when_added", ()))
    allow_after = set(after.get("loosens_when_added", ()))
    if allow_after - allow_before:  # a coverage list turned into an allow list
        out.append({"what": "config_semantics", "keys": sorted(allow_after - allow_before)})
    for key in sorted(set(old_config) | set(new_config)):
        old, new = set(old_config.get(key, ())), set(new_config.get(key, ()))
        if key in allow_before or key in allow_after:
            if new - old:
                out.append({"what": "config_allowed", "key": key, "added": sorted(new - old)})
        elif key in old_config and old - new:
            out.append({"what": "config_narrowed", "key": key, "removed": sorted(old - new)})
    return out


def loosenings(base: Mapping[str, Any], current: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every loosening from the ``base`` lock to the ``current`` one, one item per check:
    ``{check_id, before, after, protected, changes}`` with the digests an approval binds to."""
    old: Mapping[str, Any] = base.get("checks", {})
    new: Mapping[str, Any] = current.get("checks", {})
    skip_before, skip_after = ci_external(base), ci_external(current)
    out: list[dict[str, Any]] = []
    for check_id in sorted(old):
        before, after = old[check_id], new.get(check_id)
        changes = [{"what": "removed"}] if after is None else _loosened(before, after)
        if after is not None and check_id in skip_after and check_id not in skip_before:
            changes.append({"what": "ci_external_added"})
        if changes:
            out.append({
                "check_id": check_id,
                "before": bound_digest(before, check_id in skip_before),
                "after": bound_digest(after, check_id in skip_after),
                "protected": bool(before.get("protected")),
                "changes": changes,
            })  # fmt: skip
    return out


def read_lock(root: Path) -> dict[str, Any] | None:
    path = root / LOCK_FILE
    if not path.is_file():
        return None
    return parse_lock(path.read_bytes(), source=LOCK_FILE)
