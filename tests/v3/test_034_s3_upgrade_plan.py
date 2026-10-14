"""Work 034 S3 — ``amplai ops upgrade plan``: the read-only upgrade plan (U-1..U-9, D-110).

- It reports the package version, the missing distribution release (U-1), the built-in modules,
  each store's schema_major, owner epoch, incarnation, kill switch and row counts, what the next
  writable open would change today (U-3), the evaluator code digests and each app's kit receipts.
- It prints the nine steps of plan §3.4 in order and the U-6 rollback order, and the service
  manager's freeze and unfreeze commands (printed only).
- It changes nothing: no owner lock (it runs while another process owns the store), no DDL, no
  inline migration, no owner epoch increase; the database bytes are the same afterwards.
- ``plan_digest`` is the digest of the report without itself.
"""

from __future__ import annotations

import hashlib
import json
import pwd
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_034_s1a_intake_route import init_home
from typer.testing import CliRunner

from amplai_foundry import __version__
from amplai_foundry.evaluation import versions
from amplai_foundry.runtime import cli
from amplai_foundry.runtime.contracts.identity import digest
from amplai_foundry.runtime.local_deployment import LocalConfig, MetaEntry
from amplai_foundry.runtime.storage.store import Scope, Store
from amplai_foundry.runtime.upgrade.plan import ROLLBACK, STEPS, store_state, upgrade_plan

EXE = "/opt/amplai/bin/amplai"
AGENTS = "/srv/amplai/Library/LaunchAgents"
STEP_ORDER = ["verify_release", "freeze", "preview", "approve", "backup", "apply", "evaluator",
              "rebaseline", "unfreeze"]  # fmt: skip
ROLLBACK_ORDER = ["restore_new_dir", "reapply_revocations", "reconcile", "resume_admission",
                  "reapprove_authority"]  # fmt: skip


def load(home: Path) -> LocalConfig:
    return LocalConfig.model_validate_json((home / "local.json").read_bytes())


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def home(tmp_path: Path) -> Path:
    home = init_home(tmp_path)
    cfg = load(home)
    Store(Path(cfg.runtime_root)).close()  # a store the product opened once
    return home


def test_the_plan_reads_the_current_state(home: Path) -> None:
    cfg = load(home)
    report = upgrade_plan(cfg, None)
    current = report["current"]
    assert report["read_only"] is True
    assert current["package_version"] == __version__
    assert current["distribution_release"] is None  # U-1 not built
    assert "service_manager.launchd@1" in current["modules"]["builtin"]
    assert current["modules"]["builtin_digest"] == digest(current["modules"]["builtin"])
    product = current["stores"]["product"]
    assert product["present"] is True and product["schema_major"] == "3"
    assert product["owner_epoch"] == 1 and product["incarnation"].startswith("incarnation")
    assert product["kill_switch"] == {"state": "absent"}
    assert product["readable"] is True
    assert product["at_open"] == {"create": [], "rebuild": [], "meta": ["owner_epoch 1 -> 2"]}
    assert "meta" not in current["stores"]
    assert current["evaluator"] == {"code_digests": versions.code_digests(), "configured": None,
                                    "current_version_ref": None}  # fmt: skip
    assert current["packs"] == {"app": []}


def test_the_steps_and_rollback_are_in_the_spec_order(home: Path) -> None:
    report = upgrade_plan(load(home), None)
    assert [s["step"] for s in report["steps"]] == STEP_ORDER == [s["step"] for s in STEPS]
    assert [s["order"] for s in report["steps"]] == list(range(1, 10))
    assert (
        [s["step"] for s in report["rollback"]] == ROLLBACK_ORDER == [s["step"] for s in ROLLBACK]
    )
    assert all(s["state"] in ("missing", "partial") for s in report["steps"])  # nothing complete
    assert report["service_manager"] is None


def test_plan_digest_names_the_report(home: Path) -> None:
    report = upgrade_plan(load(home), None)
    value = dict(report)
    assert value.pop("plan_digest") == digest(value)


def test_the_plan_changes_nothing_and_runs_while_another_process_owns_the_store(
    home: Path,
) -> None:
    root = Path(load(home).runtime_root)
    owner = Store(root)  # the running server: holds the owner lock
    try:
        before = owner.epoch
        report = upgrade_plan(load(home), None)
        assert report["current"]["stores"]["product"]["owner_epoch"] == before
    finally:
        owner.close()
    database = root / "runtime.sqlite3"
    sha = file_sha(database)
    upgrade_plan(load(home), None)
    assert file_sha(database) == sha
    with Store(root, readonly=True) as store:
        assert store.epoch == before  # no open in between raised it


def test_an_absent_store_is_reported_not_created(tmp_path: Path) -> None:
    root = tmp_path / "nothing"
    assert store_state(root, Scope("local", "app")) == {"runtime_root": str(root),
                                                        "present": False}  # fmt: skip
    assert not root.exists()


def test_what_the_next_open_would_change_is_listed_and_not_done(tmp_path: Path) -> None:
    root = tmp_path / "old"
    root.mkdir()
    database = root / "runtime.sqlite3"
    with sqlite3.connect(database) as db:  # a store from before commands.operation (pre dev4)
        db.executescript(
            "CREATE TABLE meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);"
            "INSERT INTO meta VALUES('schema_major','3');"
            "CREATE TABLE heads (tenant TEXT,project TEXT,kind TEXT,id TEXT,state TEXT,"
            " row_version INTEGER,data BLOB,PRIMARY KEY(tenant,project,kind,id));"
            "CREATE TABLE commands (tenant TEXT,project TEXT,actor TEXT,key TEXT,"
            " request_digest TEXT,result BLOB,created_at TEXT);"
        )
    db.close()
    sha = file_sha(database)
    state = store_state(root, Scope("local", "app"))
    assert "table objects" in state["at_open"]["create"]
    assert "index scoped_events" in state["at_open"]["create"]
    assert "table heads" not in state["at_open"]["create"]
    assert len(state["at_open"]["rebuild"]) == 1 and "operation" in state["at_open"]["rebuild"][0]
    assert state["rows"] == {"heads": 0} and state["owner_epoch"] == 0
    assert state["at_open"]["meta"] == ["owner_epoch 0 -> 1", "insert incarnation"]
    assert file_sha(database) == sha
    assert not (root / "owner.lock").exists()


@pytest.mark.parametrize("shape", ["not_sqlite", "no_meta_table", "other_schema"])
def test_an_unreadable_store_is_reported_and_the_report_goes_on(
    home: Path, tmp_path: Path, shape: str
) -> None:
    root = tmp_path / "bad"
    root.mkdir()
    database = root / "runtime.sqlite3"
    if shape == "not_sqlite":
        database.write_bytes(b"this is not a database" * 100)
    else:
        with sqlite3.connect(database) as db:
            if shape == "no_meta_table":
                db.execute("CREATE TABLE other (x)")
            else:
                db.executescript("CREATE TABLE meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);"
                                 "INSERT INTO meta VALUES('schema_major','4');")  # fmt: skip
        db.close()
    state = store_state(root, Scope("local", "app"))
    assert state["present"] is True and state["readable"] is False
    expected = {"not_sqlite": "DatabaseError", "no_meta_table": "OperationalError",
                "other_schema": "SCHEMA_VERSION"}  # fmt: skip
    assert state["error"]["code"] == expected[shape]
    cfg = load(home)
    meta = cfg.model_copy(update={"runtime_root": str(root), "meta": MetaEntry()})
    report = upgrade_plan(cfg, meta)  # the product store and the rest are still read
    assert report["current"]["stores"]["product"]["readable"] is True
    assert report["current"]["stores"]["meta"]["readable"] is False
    assert "error" in report["current"]["evaluator"]


def test_an_unreadable_kit_receipt_is_listed_not_raised(home: Path) -> None:
    cfg = load(home)
    meta = Path(cfg.apps[0].repo) / ".ai-team" / "install-v3"
    meta.mkdir(parents=True)
    (meta / "a-list.json").write_text("[1, 2]")
    (meta / "b-broken.json").write_text("{not json")
    assert upgrade_plan(cfg, None)["current"]["packs"] == {"app": [
        {"file": "a-list.json", "readable": False, "error": "not an object"},
        {"file": "b-broken.json", "readable": False, "error": "JSONDecodeError"},
    ]}  # fmt: skip


def test_the_kill_switch_and_kit_receipts_are_reported(home: Path) -> None:
    cfg = load(home)
    scope = Scope.parse(cfg.scope)
    with Store(Path(cfg.runtime_root)) as store, store.tx() as db:
        store.cas(db, scope, "runtime-control", "kill", 0, "enabled",
                  {"enabled": True, "reason": "test", "actor": "t"})  # fmt: skip
    meta = Path(cfg.apps[0].repo) / ".ai-team" / "install-v3"
    meta.mkdir(parents=True)
    receipt = {
        "pack_id": "kit",
        "version": "1.2.0",
        "bundle_digest": "sha256:" + "a" * 64,
        "plan_digest": "sha256:" + "b" * 64,
        "installed_at": "t",
        "owned_files": {},
    }
    (meta / "kit.json").write_text(json.dumps(receipt))
    (meta / "pending.json").write_text("{}")
    current = upgrade_plan(cfg, None)["current"]
    kill = current["stores"]["product"]["kill_switch"]
    assert kill["state"] == "enabled"
    assert current["packs"] == {"app": [{k: receipt[k] for k in (
        "pack_id", "version", "bundle_digest", "plan_digest", "installed_at")}]}  # fmt: skip


def test_a_meta_deployment_adds_its_store_and_evaluator_setting(home: Path, tmp_path: Path) -> None:
    cfg = load(home)
    meta_root = tmp_path / "meta-runtime"
    meta = cfg.model_copy(update={"runtime_root": str(meta_root), "meta": MetaEntry()})
    Store(meta_root).close()
    report = upgrade_plan(cfg, meta)
    assert report["current"]["stores"]["meta"]["present"] is True
    assert report["current"]["evaluator"]["configured"] == "eval-2"
    assert report["current"]["evaluator"]["current_version_ref"] is None


# -- CLI -------------------------------------------------------------------------------------------


@pytest.fixture
def no_local_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    def getpwnam(name: str) -> Any:
        raise KeyError(name)

    monkeypatch.setattr(pwd, "getpwnam", getpwnam)


def test_ops_upgrade_plan_prints_the_plan_with_freeze_commands(
    home: Path, tmp_path: Path, no_local_accounts: None
) -> None:
    result = CliRunner().invoke(cli.app, [
        "ops", "upgrade", "plan", "--config", str(home / "local.json"),
        "--meta-config", str(tmp_path / "no-meta.json"), "--manager", "systemd",
        "--account", "remoteacct", "--home", "/home/remoteacct", "--amplai", EXE,
    ])  # fmt: skip
    assert result.exit_code == 0, result.output
    body: dict[str, Any] = json.loads(result.output)
    assert [s["step"] for s in body["steps"]] == STEP_ORDER
    service = body["service_manager"]
    assert service["manager"] == "service_manager.systemd@1" and service["runs_nothing"] is True
    assert service["freeze"] == ["systemctl stop amplai-server.service"]  # no meta: no clock
    assert service["unfreeze"] == ["systemctl start amplai-server.service"]


def meta_config(home: Path, tmp_path: Path) -> Path:
    cfg = load(home)
    meta = cfg.model_copy(update={"runtime_root": str(tmp_path / "meta-rt"), "meta": MetaEntry()})
    path = tmp_path / "meta" / "local.json"
    path.parent.mkdir()
    path.write_text(meta.model_dump_json())
    return path


@pytest.mark.parametrize(
    ("manager", "freeze", "unfreeze"),
    [
        ("launchd",
         ["launchctl bootout gui/1500/ai.amplai.meta-nightly",
          "launchctl bootout gui/1500/ai.amplai.local-serve"],
         [f"launchctl bootstrap gui/1500 {AGENTS}/ai.amplai.local-serve.plist",
          f"launchctl bootstrap gui/1500 {AGENTS}/ai.amplai.meta-nightly.plist"]),
        ("systemd",
         ["systemctl stop amplai-meta-tick.timer amplai-meta-tick.service",
          "systemctl stop amplai-server.service"],
         ["systemctl start amplai-server.service", "systemctl start amplai-meta-tick.timer"]),
    ],
)  # fmt: skip
def test_freeze_and_unfreeze_with_a_meta_deployment_name_each_unit_once(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, manager: str,
    freeze: list[str], unfreeze: list[str],
) -> None:  # fmt: skip
    account = SimpleNamespace(pw_uid=1500, pw_dir="/srv/amplai")
    monkeypatch.setattr(pwd, "getpwnam", lambda name: account)
    result = CliRunner().invoke(cli.app, [
        "ops", "upgrade", "plan", "--config", str(home / "local.json"),
        "--meta-config", str(meta_config(home, tmp_path)), "--manager", manager,
        "--account", "amplai", "--amplai", EXE,
    ])  # fmt: skip
    assert result.exit_code == 0, result.output
    service = json.loads(result.output)["service_manager"]
    assert service["freeze"] == freeze and service["unfreeze"] == unfreeze


def test_ops_upgrade_plan_reports_a_service_hold_instead_of_failing(
    home: Path, tmp_path: Path
) -> None:
    result = CliRunner().invoke(cli.app, [
        "ops", "upgrade", "plan", "--config", str(home / "local.json"), "--manager", "systemd",
        "--meta-config", str(tmp_path / "no-meta.json"), "--amplai", EXE,
    ])  # fmt: skip
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["service_manager"]["hold"] == "SERVICE_ACCOUNT"


def test_ops_upgrade_plan_needs_the_product_config(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli.app, [
        "ops", "upgrade", "plan", "--config", str(tmp_path / "none.json"),
        "--meta-config", str(tmp_path / "no-meta.json"),
    ])  # fmt: skip
    assert result.exit_code == 3 and json.loads(result.output)["code"] == "CONFIG_MISSING"
