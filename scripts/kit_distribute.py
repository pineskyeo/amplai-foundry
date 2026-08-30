#!/usr/bin/env python3
"""Distribute the AMPLAI Loop Kit from this repository to its target apps.

``install.py`` takes one ``--target`` at a time.  This wrapper reads the target
list, resolves each one to a real path, and drives the installer across all of
them under a few rules that exist because a mistake here writes into somebody
else's repository:

* Paths are never guessed.  A target whose path cannot be resolved stops the
  run instead of being approximated from a hint.
* Nothing is installed until every target's dry run succeeds.
* Installation is sequential and stops at the first failure, reporting what was
  already done rather than pretending the run was atomic.

See ``D-053`` and ``specs/007-kit-source-and-distribution/plan.md``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
KIT_ROOT = REPO_ROOT / "tools" / "amplai-loop-kit"
TARGETS_FILE = KIT_ROOT / "distribution" / "targets.json"
INSTALLER = KIT_ROOT / "install.py"
SELFTEST = KIT_ROOT / "selftest.py"
INSTALL_RECORD = Path(".ai-team") / "install" / "amplai-loop-kit.json"

EXIT_OK = 0
EXIT_ERROR = 2
EXIT_UNRESOLVED = 3


class DistributeError(Exception):
    """A condition that must stop the run before anything is written."""


def read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def kit_version() -> str:
    return str(read_json(KIT_ROOT / "manifest.json")["version"])


def load_targets() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not TARGETS_FILE.is_file():
        raise DistributeError(f"target list is missing: {TARGETS_FILE}")
    config = read_json(TARGETS_FILE)
    targets = config.get("targets") or []
    if not targets:
        raise DistributeError("target list is empty")
    return config, targets


def load_path_map(config: dict[str, Any]) -> dict[str, Any]:
    rel = config.get("path_map") or str(Path(".ai-team") / "local" / "kit-targets.json")
    path = REPO_ROOT / rel
    if not path.is_file():
        return {}
    return read_json(path)


def expand(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value)))


def resolve_paths(
    config: dict[str, Any], targets: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Attach a real path to each target, or record why it has none.

    A hint is only ever reported as a candidate.  Installing into a guessed
    directory is the failure mode this refuses to have.
    """
    path_map = (load_path_map(config).get("paths") or {}) if config else {}
    resolved: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for target in targets:
        app_id = target["app_id"]
        entry = dict(target)
        configured = path_map.get(app_id)
        if configured:
            path = expand(str(configured))
            if not path.is_absolute():
                unresolved.append(
                    {
                        **entry,
                        "reason": "configured path is not absolute",
                        "configured": str(configured),
                    }
                )
                continue
            if not path.is_dir():
                unresolved.append(
                    {**entry, "reason": "configured path does not exist", "configured": str(path)}
                )
                continue
            entry["path"] = str(path)
            resolved.append(entry)
            continue
        hint = target.get("path_hint")
        candidate = str((REPO_ROOT / hint).resolve()) if hint else None
        unresolved.append(
            {
                **entry,
                "reason": "no host-local path configured",
                "candidate_from_hint": candidate,
            }
        )
    return resolved, unresolved


def git_is_dirty(path: Path) -> bool | None:
    """True/False for a git repo, None when the path is not one."""
    result = subprocess.run(
        ["git", "-C", str(path), "status", "--porcelain"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return bool(result.stdout.strip())


def installed_version(path: Path) -> str | None:
    """The kit version recorded in a target's install record.

    The field is ``package_version``; reading ``version`` here silently
    reported every target as mismatched.
    """
    record = path / INSTALL_RECORD
    if not record.is_file():
        return None
    try:
        value = read_json(record).get("package_version")
    except (ValueError, OSError):
        return None
    return str(value) if value is not None else None


def _git(path: Path, *args: str) -> str | None:
    """Read-only git query against a target checkout.  None when it cannot answer."""
    try:
        done = subprocess.run(
            ["git", "-C", str(path), *args],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return None
    if done.returncode != 0:
        return None
    value = done.stdout.strip()
    return value or None


def checkout_state(path: Path) -> dict[str, Any]:
    """Which branch this checkout is on, and whether that is the default branch.

    `--verify` reads the filesystem, so a checkout parked on a feature branch
    looks exactly like a target that never received the kit.  Knowing the branch
    is what lets the two be told apart.
    """
    branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    head = _git(path, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    default = head.rsplit("/", 1)[-1] if head else "main"
    return {"branch": branch, "on_default": branch == default, "default_branch": default}


def default_branch_version(path: Path) -> str | None:
    """The kit version recorded on the target's `origin/<default>`.

    Read from the remote-tracking ref, not the working tree, so it answers
    "what did we actually ship" independently of what is checked out.
    """
    head = _git(path, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    ref = head if head else "refs/remotes/origin/main"
    raw = _git(path, "show", f"{ref}:{INSTALL_RECORD.as_posix()}")
    if not raw:
        return None
    try:
        value = json.loads(raw).get("package_version")
    except ValueError:
        return None
    return str(value) if value is not None else None


def run_installer(
    target: dict[str, Any],
    config: dict[str, Any],
    *,
    dry_run: bool,
    uninstall: bool = False,
    project_home: str | None = None,
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(INSTALLER),
        "--target",
        target["path"],
        "--app-id",
        target["app_id"],
        "--project-id",
        str(config.get("project_id", "")),
    ]
    if uninstall:
        command.append("--uninstall")
    if project_home:
        command += ["--project-home", project_home]
    if dry_run:
        command.append("--dry-run")
    result = subprocess.run(command, capture_output=True, text=True)
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        payload = None
    return {
        "app_id": target["app_id"],
        "path": target["path"],
        "returncode": result.returncode,
        "ok": result.returncode == 0 and bool(payload and payload.get("ok")),
        "report": payload,
        "stderr": result.stderr.strip() or None,
    }


def preflight(strict_clean: bool) -> dict[str, Any]:
    """Package integrity plus a look at each target before touching anything."""
    checks: dict[str, Any] = {}
    result = subprocess.run(
        [sys.executable, str(SELFTEST)],
        capture_output=True,
        text=True,
    )
    checks["selftest"] = {
        "ok": result.returncode == 0,
        "detail": (result.stderr or result.stdout).strip().splitlines()[-1:] or None,
    }
    checks["kit_version"] = kit_version()
    checks["strict_clean"] = strict_clean
    return checks


def order_targets(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Source first: it owns the kit, so a failure there is easiest to read,
    and the Store's supervisor entry point points back at it."""
    return sorted(targets, key=lambda t: 0 if t.get("role") == "source" else 1)


def select(targets: list[dict[str, Any]], app_id: str | None) -> list[dict[str, Any]]:
    if not app_id:
        return targets
    chosen = [t for t in targets if t["app_id"] == app_id]
    if not chosen:
        known = ", ".join(t["app_id"] for t in targets)
        raise DistributeError(f"unknown app: {app_id} (known: {known})")
    return chosen


def cmd_verify(config: dict[str, Any], resolved: list[dict[str, Any]]) -> int:
    """Compare installed versions with the kit.

    A target may be listed and deliberately not installed — cortex was, while
    its `.ai-team` convention still forbade the kit's directories (`D-054`).
    Without a way to say so, `--verify` reported that as a mismatch and failed
    on a fleet that was in its intended state, which trains people to ignore it.
    `expect_installed: false` records the intent; an unexpected *presence* is
    still a mismatch, because that means someone installed what we said we
    would not.
    """
    expected = kit_version()
    rows = []
    mismatched = []
    off_main = []
    for target in resolved:
        path = Path(target["path"])
        found = installed_version(path)
        wanted = target.get("expect_installed", True)
        agree = (found == expected) if wanted else (found is None)
        row: dict[str, Any] = {
            "app_id": target["app_id"],
            "installed": found,
            "matches": agree,
            "state": "MATCH" if agree else "MISMATCH",
        }
        if not wanted:
            row["expect_installed"] = False
        if not agree and wanted:
            state = checkout_state(path)
            shipped = default_branch_version(path)
            row["branch"] = state["branch"]
            # Only the combination excuses the disagreement: the checkout is
            # parked elsewhere *and* the default branch really carries this kit.
            # An unreadable default branch stays a mismatch — a check that
            # assumes innocence when it cannot look is worse than none.
            if not state["on_default"] and shipped == expected:
                row["state"] = "LOCAL_NOT_ON_MAIN"
                row["matches"] = True
                row["default_branch_installed"] = shipped
                row["default_branch"] = state.get("default_branch")
            elif shipped is not None:
                row["default_branch_installed"] = shipped
        rows.append(row)
        if row["state"] == "MISMATCH":
            mismatched.append(target["app_id"])
        elif row["state"] == "LOCAL_NOT_ON_MAIN":
            off_main.append(target["app_id"])
    emit(
        {
            "ok": not mismatched,
            "action": "verify",
            "kit_version": expected,
            "targets": rows,
            "mismatched": mismatched,
            "local_not_on_main": off_main,
        }
    )
    return EXIT_OK if not mismatched else EXIT_ERROR


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Distribute the AMPLAI Loop Kit")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Plan every target; change nothing")
    mode.add_argument("--all", action="store_true", help="Install into every target")
    mode.add_argument(
        "--verify", action="store_true", help="Compare installed versions with the kit"
    )
    parser.add_argument("--app", help="Restrict to one app_id")
    parser.add_argument("--uninstall", action="store_true", help="Remove instead of install")
    parser.add_argument("--project-home", help="Project Store path passed to the installer")
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Proceed even if a target has uncommitted changes",
    )
    args = parser.parse_args(argv)

    try:
        config, targets = load_targets()
        targets = select(targets, args.app)
        resolved, unresolved = resolve_paths(config, targets)
    except DistributeError as exc:
        emit({"ok": False, "error": str(exc)})
        return EXIT_ERROR

    if unresolved:
        # Stop rather than approximate.  A hint is a suggestion for a human to
        # confirm, not an address to write into.
        emit(
            {
                "ok": False,
                "error": "some targets have no resolved path",
                "unresolved": unresolved,
                "hint": f"add them to {config.get('path_map')} and run again",
            }
        )
        return EXIT_UNRESOLVED

    if args.verify:
        return cmd_verify(config, resolved)

    ordered = order_targets(resolved)
    checks = preflight(not args.allow_dirty)
    if not checks["selftest"]["ok"]:
        emit({"ok": False, "error": "kit selftest failed", "preflight": checks})
        return EXIT_ERROR

    dirty = []
    for target in ordered:
        state = git_is_dirty(Path(target["path"]))
        target["git_dirty"] = state
        if state:
            dirty.append(target["app_id"])
    # A plan writes nothing, so a dirty tree is reported there but only blocks
    # an actual install, where uncommitted work would be hard to separate from
    # what the installer did.
    if dirty and not args.dry_run and not args.allow_dirty:
        emit(
            {
                "ok": False,
                "error": "targets have uncommitted changes",
                "dirty": dirty,
                "hint": "commit or stash them, or pass --allow-dirty",
            }
        )
        return EXIT_ERROR

    plans = [
        run_installer(
            t, config, dry_run=True, uninstall=args.uninstall, project_home=args.project_home
        )
        for t in ordered
    ]
    failed_plans = [p["app_id"] for p in plans if not p["ok"]]

    if args.dry_run:
        emit(
            {
                "ok": not failed_plans,
                "action": "dry-run",
                "kit_version": checks["kit_version"],
                "preflight": checks,
                "targets": [
                    {
                        "app_id": p["app_id"],
                        "path": p["path"],
                        "ok": p["ok"],
                        "actions": len((p["report"] or {}).get("actions") or []),
                        "notes": (p["report"] or {}).get("notes"),
                        "stderr": p["stderr"],
                    }
                    for p in plans
                ],
                "dirty": dirty,
                "failed": failed_plans,
            }
        )
        return EXIT_OK if not failed_plans else EXIT_ERROR

    if failed_plans:
        # One bad plan means nothing is installed anywhere.  Half a fleet on a
        # new version is harder to reason about than none of it.
        emit(
            {
                "ok": False,
                "action": "install",
                "error": "dry run failed; nothing was installed",
                "failed": failed_plans,
                "targets": [
                    {"app_id": p["app_id"], "ok": p["ok"], "stderr": p["stderr"]} for p in plans
                ],
            }
        )
        return EXIT_ERROR

    completed: list[dict[str, Any]] = []
    stopped_at = None
    for target in ordered:
        outcome = run_installer(
            target, config, dry_run=False, uninstall=args.uninstall, project_home=args.project_home
        )
        completed.append(
            {"app_id": outcome["app_id"], "ok": outcome["ok"], "stderr": outcome["stderr"]}
        )
        if not outcome["ok"]:
            stopped_at = outcome["app_id"]
            break

    emit(
        {
            "ok": stopped_at is None,
            "action": "uninstall" if args.uninstall else "install",
            "kit_version": checks["kit_version"],
            "completed": completed,
            "stopped_at": stopped_at,
            "not_attempted": [
                t["app_id"] for t in ordered if t["app_id"] not in {c["app_id"] for c in completed}
            ],
        }
    )
    return EXIT_OK if stopped_at is None else EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
