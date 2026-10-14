"""``amplai ops upgrade plan``: the read-only upgrade plan (Work 034 S3; spec U-1..U-9, D-110).

It reads what an upgrade would start from and prints the ordered upgrade of ``plan.md`` §3.4
(verify release → freeze → preview → approve → backup → apply → evaluator → rebaseline →
unfreeze) and the rollback order of U-6, each step marked with what exists in the code today.
It changes nothing:

- stores are opened read-only (``Store(readonly=True)``: no owner lock, no DDL, no migration, no
  owner epoch increase), so it runs while the server or a night owns them;
- no command runs: the service manager's freeze and unfreeze commands are printed only (C-8);
- package receipts are read as files.

What it reads (``current``):

- the package version, the distribution release (none exists: U-1), the built-in modules and the
  kinds' interface versions;
- per store (product, meta): ``schema_major``, ``owner_epoch``, ``incarnation``, the kill switch
  (``runtime-control/kill``), row counts, and what the next writable open would change today
  (``at_open``: the ``DDL`` tables and indexes it would create and the inline ``commands``
  rebuild, ``storage/store.py``). U-3 replaces that with numbered ``migrate`` steps; their format
  is not decided, so none is listed as one;
- the evaluator: the running code digests, the configured ``meta.evaluator_version`` and the
  stored version matching the running code (``evaluation/versions.py`` ``current_version_ref``);
- each app's kit receipts (``<repo>/.ai-team/install-v3/<pack>.json``).

``plan_digest`` is the sha256 of the canonical report without itself. It names this reading; it is
not the AC-U1 preview digest an approval binds to (that needs a target release, U-1, and the
migration format, U-3).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..contracts.identity import digest
from ..errors import RuntimeFault
from ..modules.builtin import BUILTIN_MODULES
from ..modules.spec import INTERFACE_VERSIONS
from ..storage.store import DDL, Scope, Store

REPORT = "upgrade-plan (read-only; Work 034 S3 slice 1, provisional)"

_CREATE = re.compile(r"CREATE (TABLE|INDEX) IF NOT EXISTS (\w+)")
_RECEIPT_SKIP = frozenset({"pending.json"})

# plan.md §3.4 steps 1-9, with what the code has today (file:line at origin/main 89d9e20)
STEPS: tuple[dict[str, str], ...] = (
    {"step": "verify_release", "ref": "plan §3.4 step 1, U-1, G-07, D-121",
     "does": "check the release signature, protocol major 3 and dependencies; on failure change "
             "nothing",
     "state": "missing",
     "today": "no distribution-release kind (U-1); the signed bundle is S4b (D-121)"},
    {"step": "freeze", "ref": "plan §3.4 step 2, U-2, C-8, D-118, AC-U4",
     "does": "kill switch on -> service manager stops the clock, then the server, gateway and "
             "listener -> owner lock on every store (product, meta) -> drain check; held until "
             "verification ends",
     "state": "partial",
     "today": "service manager renders units and prints commands (service_manager kind); kill "
              "switch exists only as restore's runtime-control/kill (recovery/service.py:149-165); "
              "no freeze marker, no freeze command, no drain check"},
    {"step": "preview", "ref": "plan §3.4 step 3, U-3, AC-U1",
     "does": "owned-path migration-plan (KitInstaller), numbered store migrate steps, "
             "irreversible changes, rollback; report digest",
     "state": "missing",
     "today": "no migration registry; every writable open runs DDL and the inline commands "
              "rebuild (storage/store.py:185-189)"},
    {"step": "approve", "ref": "plan §3.4 step 4, G-20",
     "does": "the operator approves the preview report digest; the freeze holds while waiting",
     "state": "missing", "today": "no upgrade approval record"},
    {"step": "backup", "ref": "plan §3.4 step 5, U-4",
     "does": "manifest-scoped backup (product and meta store, CAS, keys, token files, config, "
             "installer receipts, knowledge HEAD) after approval, before apply",
     "state": "partial",
     "today": "RecoveryService.backup copies one store's DB and CAS (recovery/service.py:37-105); "
              "no ops backup command"},
    {"step": "apply", "ref": "plan §3.4 step 6, U-3",
     "does": "install the wheel, amplai ops migrate (stores never migrate at open), KitInstaller, "
             "verify, receipt",
     "state": "partial",
     "today": "KitInstaller plan/apply/recover exist (distribution/installer.py); no ops migrate"},
    {"step": "evaluator", "ref": "plan §3.4 step 7, U-5, AC-U5",
     "does": "approve the new evaluator version on site with the release's Q-suite asset; no "
             "experiment or calibration before",
     "state": "partial",
     "today": "fail-closed version check exists (evaluation/versions.py:229-258); the Q-suite is "
              "read from the checkout's tests/v3 (evaluation/quality.py:113-120)"},
    {"step": "rebaseline", "ref": "plan §3.4 step 8, U-7, AC-U7",
     "does": "compare each promoted part's baseline digest with the new default; on a change mark "
             "it stale and propose a re-compare; pointers unchanged until approval",
     "state": "missing",
     "today": "no baseline digest; class-A match applies the candidate "
              "(execution/releases.py:230-254)"},
    {"step": "unfreeze", "ref": "plan §3.4 step 9, U-2",
     "does": "service manager restarts the services and the clock; kill switch off",
     "state": "partial", "today": "service manager prints the commands only (C-8)"},
)  # fmt: skip

ROLLBACK: tuple[dict[str, str], ...] = (
    {"step": "restore_new_dir", "ref": "U-6",
     "does": "restore the backup into a new directory",
     "today": "RecoveryService.restore into an empty dir, kill switch on, new incarnation "
              "(recovery/service.py:107-186)"},
    {"step": "reapply_revocations", "ref": "U-6, AC-U6, AC-H7",
     "does": "reapply the out-of-store revocation journal", "today": "no revocation journal"},
    {"step": "reconcile", "ref": "U-6", "does": "reconcile processes and effects",
     "today": "restore policy text only (recovery/service.py:96-98)"},
    {"step": "resume_admission", "ref": "U-6",
     "does": "resume admission; data rollback is allowed only before this step",
     "today": "kill switch head runtime-control/kill"},
    {"step": "reapprove_authority", "ref": "U-6", "does": "re-approve authority",
     "today": "restore marks authority_restored false (recovery/service.py:178)"},
)  # fmt: skip
ROLLBACK_NOTES = (
    "knowledge repository: governed revert commit, not a backup restore (U-4, AC-U6)",
    "packs: KitInstaller rollback (U-6, distribution/installer.py:236-364)",
)


def _ddl_objects() -> tuple[tuple[str, str], ...]:
    return tuple((kind.lower(), name) for kind, name in _CREATE.findall(DDL))


def store_state(root: Path, scope: Scope) -> dict[str, Any]:
    """One store, read-only. ``present: false`` when it has no database yet."""
    database = root / "runtime.sqlite3"
    if not database.is_file():
        return {"runtime_root": str(root), "present": False}
    with Store(root, readonly=True) as store:
        conn = store.conn
        meta = {row[0]: row[1] for row in conn.execute("SELECT key,value FROM meta")}
        have = {(row[0], row[1]) for row in conn.execute("SELECT type,name FROM sqlite_master")}
        create = [f"{kind} {name}" for kind, name in _ddl_objects() if (kind, name) not in have]
        rebuild = []
        if ("table", "commands") in have:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(commands)")}
            if "operation" not in columns:
                rebuild.append("commands: add key column operation (store.py "
                               "_migrate_commands_operation)")  # fmt: skip
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("objects", "heads", "events", "artifacts")
            if ("table", table) in have
        }
        try:
            kill = store.head(scope, "runtime-control", "kill")
            kill_switch: dict[str, Any] = {"state": kill["state"],
                                           "row_version": kill["row_version"]}  # fmt: skip
        except RuntimeFault as exc:
            if exc.code != "NOT_FOUND":
                raise
            kill_switch = {"state": "absent"}
        return {
            "runtime_root": str(root),
            "present": True,
            "schema_major": meta.get("schema_major"),
            "owner_epoch": store.epoch,
            "incarnation": store.incarnation,
            "scope": scope.wire(),
            "kill_switch": kill_switch,
            "rows": counts,
            "at_open": {"create": create, "rebuild": rebuild},
        }


def evaluator_state(meta_root: Path | None, scope: Scope | None, configured: str | None
                    ) -> dict[str, Any]:  # fmt: skip
    from ...evaluation import versions

    out: dict[str, Any] = {"code_digests": versions.code_digests(), "configured": configured,
                           "current_version_ref": None}  # fmt: skip
    if meta_root is not None and scope is not None and (meta_root / "runtime.sqlite3").is_file():
        with Store(meta_root, readonly=True) as store:
            out["current_version_ref"] = versions.current_version_ref(store, scope)
    return out


def kit_receipts(repo: Path) -> list[dict[str, Any]]:
    meta = repo / ".ai-team" / "install-v3"
    if not meta.is_dir() or meta.is_symlink():
        return []
    out = []
    for path in sorted(meta.glob("*.json")):
        if path.name in _RECEIPT_SKIP or path.is_symlink():
            continue
        value = json.loads(path.read_bytes())
        out.append({key: value.get(key) for key in ("pack_id", "version", "bundle_digest",
                                                     "plan_digest", "installed_at")})  # fmt: skip
    return out


def upgrade_plan(
    config: Any,
    meta_config: Any | None,
    *,
    service: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The report. ``config``/``meta_config`` are parsed ``LocalConfig`` values; ``service`` gives
    the freeze/unfreeze commands of the selected service manager (printed, never run)."""
    from amplai_foundry import __version__

    scope = Scope.parse(config.scope)
    stores: dict[str, Any] = {"product": store_state(Path(config.runtime_root).expanduser(), scope)}
    meta_root = meta_scope = None
    configured = None
    if meta_config is not None:
        meta_root = Path(meta_config.runtime_root).expanduser()
        meta_scope = Scope.parse(meta_config.scope)
        stores["meta"] = store_state(meta_root, meta_scope)
        configured = meta_config.meta.evaluator_version if meta_config.meta else None
    modules = sorted(spec.ref for spec in BUILTIN_MODULES)
    report: dict[str, Any] = {
        "report": REPORT,
        "read_only": True,
        "current": {
            "package_version": __version__,
            "distribution_release": None,
            "distribution_release_note": "no distribution-release kind exists yet (U-1)",
            "modules": {"builtin": modules, "builtin_digest": digest(modules),
                        "interface_versions": dict(INTERFACE_VERSIONS),
                        "extensions": "none: local.json names no pinned manifest"},
            "stores": stores,
            "evaluator": evaluator_state(meta_root, meta_scope, configured),
            "packs": {app.app_id: kit_receipts(Path(app.repo).expanduser())
                      for app in config.apps},
        },
        "steps": [dict(step, order=i + 1) for i, step in enumerate(STEPS)],
        "rollback": [dict(step, order=i + 1) for i, step in enumerate(ROLLBACK)],
        "rollback_notes": list(ROLLBACK_NOTES),
        "service_manager": service() if service is not None else None,
    }  # fmt: skip
    report["plan_digest"] = digest(report)
    return report
