"""Work 034 OD-18 (spec M-16..M-20, M-12, M-13): reliability checks are ``check`` modules.

- The kind: ``check`` is a module kind with interface v1 and a static suite; every built-in check
  is a registered module with one declaration (id, version, targets, when, required, protected).
- The runner: runs the checks of a moment, Holds ``CHECK_FAILED`` with the check id and result
  when a required one fails, never skips a protected one, writes the run record (M-20).
- The checks: module boundary lint, kind conformance, removal test, secret exposure, check policy
  (M-19 loosening against the main-branch lock), on synthetic trees and on this repository.

Real: the check implementations, ``ast``, git in ``tmp_path`` repositories, a pytest subprocess
for the removal test. The tool checks (ruff, mypy, pytest) are the CI commands and run in CI.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from amplai_foundry.runtime import cli
from amplai_foundry.runtime.checks import policy as policy_module
from amplai_foundry.runtime.checks.boundary import ModuleBoundaryCheck
from amplai_foundry.runtime.checks.builtin import BASE_PROTECTED, BUILTIN_CHECKS
from amplai_foundry.runtime.checks.interface import (
    SUITE,
    BuiltinCheck,
    CheckContext,
    CheckResult,
    CheckSpec,
    Finding,
)
from amplai_foundry.runtime.checks.kind_conformance import KindConformanceCheck
from amplai_foundry.runtime.checks.lock import LOCK_FILE, lock_bytes, lock_document, loosenings
from amplai_foundry.runtime.checks.policy import APPROVALS_DIR, CheckPolicyCheck
from amplai_foundry.runtime.checks.removal import ModuleRemovalCheck
from amplai_foundry.runtime.checks.runner import builtin_checks, run_checks
from amplai_foundry.runtime.checks.secret_exposure import SecretExposureCheck
from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.runtime.modules import (
    BUILTIN_CODE,
    BUILTIN_MODULES,
    KINDS,
    ConformanceLedger,
    ModuleRegistry,
    ModuleSpec,
    suite_id,
)

REPO = Path(__file__).resolve().parents[2]
DECLARED = {c.check_id: c for c in BUILTIN_CHECKS}
SPECS = {s.module_id: s for s in BUILTIN_MODULES}


def git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}  # fmt: skip
    return subprocess.run(
        ["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=True
    ).stdout


def context(root: Path, tmp: Path, **kw: Any) -> CheckContext:
    work = tmp / "work"
    work.mkdir(exist_ok=True)
    return CheckContext(root=root, when=kw.pop("when", "ci"), workdir=work, **kw)


def build(cls: type[BuiltinCheck], check_id: str, **override: Any) -> BuiltinCheck:
    return cls(SPECS[check_id], dataclasses.replace(DECLARED[check_id], **override))


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# -- the kind --------------------------------------------------------------------------------------


def test_check_is_a_kind_and_every_builtin_check_is_a_module_with_one_declaration() -> None:
    assert "check" in KINDS
    modules = {s.module_id for s in BUILTIN_MODULES if s.kind == "check"}
    assert modules == set(DECLARED)
    loaded = builtin_checks()
    assert [c.check.check_id for c in loaded] == [c.check_id for c in BUILTIN_CHECKS]
    assert {c.check_id for c in BUILTIN_CHECKS if c.protected} >= BASE_PROTECTED
    for check in loaded:
        assert check.spec.conformance_suite == SUITE.suite_id == "conformance.check.v1"
        assert BUILTIN_CODE[check.check.check_id].entry is not None


def test_the_check_suite_passes_builtins_and_fails_a_check_that_names_another_module() -> None:
    ledger = ConformanceLedger()
    ledger.register_suite(SUITE)
    registry = ModuleRegistry(ledger)
    good = build(ModuleBoundaryCheck, "check.module_boundary")
    with pytest.raises(Hold):
        registry.enable(*good.spec.key)
    assert ledger.run(good.spec, good).passed
    assert registry.enable(*good.spec.key) == good.spec
    other = ModuleBoundaryCheck(SPECS["check.policy"], DECLARED["check.module_boundary"])
    assert not ledger.run(other.spec, other).passed


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"targets": ("kind:nope",)}, "CHECK_SPEC"),
        ({"targets": ()}, "CHECK_SPEC"),
        ({"when": frozenset({"nightly"})}, "CHECK_SPEC"),
        ({"required": False}, "CHECK_SPEC"),  # protected but optional
        ({"check_id": "lint.x"}, "CHECK_SPEC"),
        ({"loosens_when_added": frozenset({"missing"})}, "CHECK_SPEC"),
    ],
)
def test_a_check_declaration_is_validated(override: dict[str, Any], code: str) -> None:
    with pytest.raises(RuntimeFault) as excinfo:
        dataclasses.replace(DECLARED["check.module_boundary"], **override)
    assert excinfo.value.code == code


# -- the runner ------------------------------------------------------------------------------------


class Fixed(BuiltinCheck):
    outcome = CheckResult("pass")

    def run(self, context: CheckContext) -> CheckResult:
        return self.outcome


def fixed(check_id: str, outcome: CheckResult | None = None, **decl: Any) -> Fixed:
    spec = ModuleSpec("check", check_id, "7", 1, suite_id("check"), frozenset(), "in_process")
    fields: dict[str, Any] = {
        "targets": ("core",),
        "when": frozenset({"ci"}),
        "required": True,
        "protected": check_id in BASE_PROTECTED,
        "description": "t",
    }
    fields.update(decl)
    check = Fixed(spec, CheckSpec(check_id, "7", **fields))
    if outcome is not None:
        check.outcome = outcome
    return check


def protected_set() -> list[Fixed]:
    return [fixed(check_id) for check_id in sorted(BASE_PROTECTED)]


def test_a_required_failure_holds_with_the_check_id_and_result_after_the_record(
    tmp_path: Path,
) -> None:
    bad = CheckResult("fail", (Finding("X_BROKEN", "broken", "src/x.py"),))
    checks = [*protected_set(), fixed("check.lint", bad), fixed("check.soft", bad, required=False)]
    records = tmp_path / "records"
    with pytest.raises(Hold) as held:
        run_checks("ci", root=tmp_path, record_dir=records, checks=checks)
    assert held.value.code == "CHECK_FAILED"
    details: Any = held.value.details
    assert [f["check_id"] for f in details["failed"]] == ["check.lint"]
    assert details["failed"][0]["version"] == "7"
    assert details["failed"][0]["findings"] == [
        {"code": "X_BROKEN", "message": "broken", "path": "src/x.py"}
    ]
    record = json.loads(Path(details["record"]).read_text())  # M-20
    assert record["schema"] == "amplai.check-run.v1" and record["outcome"] == "hold"
    rows = {r["check_id"]: r for r in record["results"]}
    assert rows["check.soft"]["status"] == "fail" and not rows["check.soft"]["required"]
    assert {r["version"] for r in record["results"]} == {"7"}
    assert all(r["module"].endswith("@7") for r in record["results"])
    assert record["registry_digest"].startswith("sha256:")


def test_an_optional_failure_is_a_warning_and_a_crash_is_an_error(tmp_path: Path) -> None:
    bad = CheckResult("fail", (Finding("SOFT", "soft"),))
    soft = fixed("check.soft", bad, required=False)
    summary = run_checks("ci", root=tmp_path, record_dir=tmp_path,
                         checks=[*protected_set(), soft])  # fmt: skip
    assert summary["outcome"] == "pass" and summary["warnings"] == ["check.soft"]

    class Crashes(Fixed):
        def run(self, context: CheckContext) -> CheckResult:
            raise ValueError("boom")

    crash = Crashes(fixed("check.crash").spec, fixed("check.crash").check)
    with pytest.raises(Hold) as held:
        run_checks("ci", root=tmp_path, record_dir=tmp_path, checks=[*protected_set(), crash])
    details: Any = held.value.details
    assert details["failed"][0]["status"] == "error"
    assert details["failed"][0]["findings"][0]["code"] == "CHECK_CRASHED"


def test_moment_and_target_select_checks_and_skips_are_recorded(tmp_path: Path) -> None:
    checks = [
        *protected_set(),
        fixed("check.at_enable", when=frozenset({"enable"}), targets=("kind:git_host",)),
        fixed("check.every_kind", when=frozenset({"enable"}), targets=("kind:*",)),
        fixed("check.core_only", when=frozenset({"enable"})),
        fixed("check.ci_tool"),
    ]
    summary = run_checks("enable", root=tmp_path, record_dir=tmp_path, checks=checks,
                         targets=("kind:git_host",))  # fmt: skip
    assert [r["check_id"] for r in summary["results"]] == ["check.at_enable", "check.every_kind"]
    summary = run_checks("ci", root=tmp_path, record_dir=tmp_path, checks=checks,
                         skip=("check.ci_tool",))  # fmt: skip
    rows = {r["check_id"]: r["status"] for r in summary["results"]}
    assert rows["check.ci_tool"] == "skipped"
    record = json.loads(Path(summary["record"]).read_text())
    assert record["skipped"] == ["check.ci_tool"]


def test_protected_checks_can_not_be_skipped_or_missing(tmp_path: Path) -> None:
    with pytest.raises(Hold) as held:
        run_checks("ci", root=tmp_path, record_dir=tmp_path, checks=protected_set(),
                   skip=("check.secret_exposure",))  # fmt: skip
    assert held.value.code == "CHECK_PROTECTED"
    with pytest.raises(Hold) as held:
        run_checks("ci", root=tmp_path, record_dir=tmp_path, checks=protected_set()[1:])
    assert held.value.code == "CHECK_PROTECTED_MISSING"
    with pytest.raises(RuntimeFault) as excinfo:
        run_checks("ci", root=tmp_path, record_dir=tmp_path, checks=protected_set(),
                   skip=("check.nope",))  # fmt: skip
    assert excinfo.value.code == "CHECK_UNKNOWN"
    with pytest.raises(RuntimeFault) as excinfo:
        run_checks("nightly", root=tmp_path, record_dir=tmp_path, checks=protected_set())
    assert excinfo.value.code == "CHECK_WHEN"


def test_the_cli_runs_the_registered_checks_and_lists_them(tmp_path: Path) -> None:
    listed = CliRunner().invoke(cli.app, ["checks", "list", "--when", "upgrade"])
    assert listed.exit_code == 0, listed.output
    assert [c["check_id"] for c in json.loads(listed.output)] == ["check.evaluator_qsuite"]
    ran = CliRunner().invoke(
        cli.app,
        [
            "checks",
            "run",
            "--when",
            "ci",
            "--target",
            "pack:none",
            "--record-dir",
            str(tmp_path),
            "--root",
            str(REPO),
        ],
    )
    assert ran.exit_code == 0, ran.output
    assert json.loads(ran.output)["results"] == []
    assert len(list(tmp_path.glob("check-run-*.json"))) == 1


# -- module boundary lint (M-12) -------------------------------------------------------------------


def test_the_boundary_lint_finds_implementation_and_cross_kind_imports(tmp_path: Path) -> None:
    pkg = "src/amplai_foundry/runtime"
    write(tmp_path, f"{pkg}/execution/publish.py", "from ..git_host import GitHost\n")  # interface
    write(tmp_path, f"{pkg}/execution/outbox.py",
          "def f():\n    from ..notifier.slack import SlackWebhookNotifier\n")  # fmt: skip
    write(tmp_path, f"{pkg}/local_deployment.py", "from .notifier.slack import spec\n")  # root
    write(tmp_path, f"{pkg}/notifier/__init__.py", "from . import slack\n")
    write(tmp_path, f"{pkg}/notifier/slack.py", "from .interface import Projection\n")
    write(tmp_path, f"{pkg}/notifier/slack_conformance.py", "from .slack import X\n")  # own
    write(tmp_path, f"{pkg}/git_host/github.py", "import amplai_foundry.runtime.notifier\n")
    check = build(ModuleBoundaryCheck, "check.module_boundary")
    found = check.run(context(tmp_path, tmp_path))
    assert found.status == "fail"
    got = sorted((f.code, f.path) for f in found.findings)
    assert got == [
        ("MODULE_IMPORT_IMPLEMENTATION", f"{pkg}/execution/outbox.py"),
        ("MODULE_IMPORT_IMPLEMENTATION", f"{pkg}/notifier/__init__.py"),
        ("MODULE_IMPORT_OTHER_KIND", f"{pkg}/git_host/github.py"),
    ]
    roots = check.check.config["composition_roots"]
    no_root = build(ModuleBoundaryCheck, "check.module_boundary",
                    config={"composition_roots": ()})  # fmt: skip
    assert roots == ("amplai_foundry.runtime.local_deployment",)
    assert len(no_root.run(context(tmp_path, tmp_path)).findings) == 4


def test_this_repository_keeps_its_module_boundaries(tmp_path: Path) -> None:
    found = build(ModuleBoundaryCheck, "check.module_boundary").run(context(REPO, tmp_path))
    assert found.status == "pass", [f.wire() for f in found.findings]
    assert {"git_host.github", "notifier.slack_webhook"} <= set(found.details["modules_covered"])
    assert "driver.codex-cli" in found.details["modules_not_covered"]


# -- kind conformance (M-2, M-13) ------------------------------------------------------------------


def test_every_registered_module_with_a_suite_passes_it(tmp_path: Path) -> None:
    found = build(KindConformanceCheck, "check.kind_conformance").run(context(REPO, tmp_path))
    assert found.status == "pass", [f.wire() for f in found.findings]
    passed = found.details["passed"]
    assert "notifier.slack_webhook@1 conformance.notifier.v1" in passed
    assert "git_host.github@1 conformance.git_host.v1" in passed
    assert "driver.codex-cli@1" in found.details["no_suite"]
    one = build(KindConformanceCheck, "check.kind_conformance")
    only = one.run(context(REPO, tmp_path, targets=("kind:git_host",)))
    assert only.details["passed"] == ["git_host.github@1 conformance.git_host.v1"]


def test_a_module_that_fails_its_suite_fails_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from amplai_foundry.runtime.git_host.github import GitHubHost
    from amplai_foundry.runtime.git_host.github_conformance import ConformanceHost

    class OpensTwice(GitHubHost):
        def open_draft_pr(self, *args: Any) -> str:
            super().open_draft_pr(*args)
            return "https://github.com/o/r/pull/999"

    from amplai_foundry.runtime.checks import kind_conformance

    real = kind_conformance._subject

    def subject(spec: ModuleSpec, workdir: Path) -> Any:
        return OpensTwice(run=ConformanceHost()) if spec.kind == "git_host" else real(spec, workdir)

    monkeypatch.setattr(kind_conformance, "_subject", subject)
    found = build(KindConformanceCheck, "check.kind_conformance").run(
        context(REPO, tmp_path, targets=("kind:git_host",))
    )
    assert [(f.code, (f.details or {})["module"]) for f in found.findings] == [
        ("CONFORMANCE_FAILED", "git_host.github@1")
    ]


# -- removal test (M-13) ---------------------------------------------------------------------------

CORE_OK = """
import pytest

def test_the_module_is_gone():
    with pytest.raises(ModuleNotFoundError):
        import amplai_foundry.runtime.git_host.github  # noqa: F401

def test_the_module_is_off_by_default():
    from amplai_foundry.runtime.local_deployment import LocalConfig
    assert LocalConfig.model_fields["git_host"].default is None
"""
CORE_DEPENDS = """
def test_needs_github():
    from amplai_foundry.runtime.git_host.github import GitHubHost
    assert GitHubHost
"""


def removal(**config: tuple[str, ...]) -> ModuleRemovalCheck:
    check = build(ModuleRemovalCheck, "check.module_removal",
                  config={"modules": ("git_host.github",), **config})  # fmt: skip
    assert isinstance(check, ModuleRemovalCheck)
    check.workers = None
    return check


def test_the_removal_test_hides_the_module_and_turns_it_off(tmp_path: Path) -> None:
    write(tmp_path, "tests/test_core.py", CORE_OK)
    found = removal(core_tests=("tests/test_core.py",)).run(context(tmp_path, tmp_path))
    assert found.status == "pass", [f.wire() for f in found.findings]
    run = found.details["runs"]["git_host.github"]
    assert run["off_key"] == "git_host" and "2 passed" in run["summary"]


def test_a_core_test_that_needs_the_module_is_an_isolation_failure(tmp_path: Path) -> None:
    write(tmp_path, "tests/test_core.py", CORE_DEPENDS)
    check = removal(core_tests=("tests/test_core.py", "tests/missing.py"))
    found = check.run(context(tmp_path, tmp_path))
    codes = [f.code for f in found.findings]
    assert codes == ["REMOVAL_TEST_MISSING", "MODULE_REMOVAL_BROKE_CORE"]
    assert found.findings[1].details is not None
    assert found.findings[1].details["module"] == "git_host.github"


def test_a_module_without_an_off_key_is_not_removable(tmp_path: Path) -> None:
    write(tmp_path, "tests/test_core.py", CORE_OK)
    check = removal(modules=("driver.codex-cli",), core_tests=("tests/test_core.py",))
    assert [f.code for f in check.run(context(tmp_path, tmp_path)).findings] == [
        "REMOVAL_NOT_OPTIONAL"
    ]
    empty = removal(core_tests=()).run(context(tmp_path, tmp_path))
    assert [f.code for f in empty.findings] == ["REMOVAL_NO_CORE_TESTS"]


# -- secret exposure (protected) -------------------------------------------------------------------

PEM = "-----BEGIN " + "PRIVATE KEY-----\n"  # built here so this file carries no pattern


def test_a_tracked_secret_pattern_fails_unless_allowed(tmp_path: Path) -> None:
    git(tmp_path, "init", "-q")
    write(tmp_path, "keys/id.pem", PEM)
    write(tmp_path, "untracked.pem", PEM)
    git(tmp_path, "add", "keys/id.pem")
    found = build(SecretExposureCheck, "check.secret_exposure").run(context(tmp_path, tmp_path))
    assert [(f.code, f.path, (f.details or {})["pattern"]) for f in found.findings] == [
        ("SECRET_PATTERN_TRACKED", "keys/id.pem", "secret-pattern-1")
    ]
    assert PEM.strip() not in json.dumps([f.wire() for f in found.findings])
    allowed = build(SecretExposureCheck, "check.secret_exposure",
                    config={"allow": ("keys/id.pem::secret-pattern-1",)})  # fmt: skip
    assert allowed.run(context(tmp_path, tmp_path)).status == "pass"


def test_this_repository_tracks_no_secret_pattern_outside_the_allow_list(tmp_path: Path) -> None:
    found = build(SecretExposureCheck, "check.secret_exposure").run(context(REPO, tmp_path))
    assert found.status == "pass", [f.wire() for f in found.findings]


# -- check policy (M-19) ---------------------------------------------------------------------------


def loosen(check_id: str, **override: Any) -> tuple[CheckSpec, ...]:
    return tuple(dataclasses.replace(c, **override) if c.check_id == check_id else c
                 for c in BUILTIN_CHECKS)  # fmt: skip


def changes(base: tuple[CheckSpec, ...], now: tuple[CheckSpec, ...]) -> dict[str, list[str]]:
    found = loosenings(lock_document(base), lock_document(now))
    return {i["check_id"]: [c["what"] for c in i["changes"]] for i in found}


def test_loosening_is_told_from_tightening() -> None:
    base = BUILTIN_CHECKS
    assert changes(base, loosen("check.tool_mypy", required=False)) == {
        "check.tool_mypy": ["required_lowered"]
    }
    assert changes(base, loosen("check.kind_conformance", when=frozenset({"ci"}))) == {
        "check.kind_conformance": ["when_narrowed"]
    }
    narrowed = loosen("check.module_removal", targets=("kind:git_host",))
    assert changes(base, narrowed) == {"check.module_removal": ["targets_narrowed"]}
    widened = loosen("check.module_removal", targets=("kind:*",))  # kind:* covers both kinds
    assert changes(base, widened) == {}
    allow = DECLARED["check.secret_exposure"].config["allow"]
    more = loosen("check.secret_exposure", config={"allow": (*allow, "a.py::secret-pattern-1")})
    assert changes(base, more) == {"check.secret_exposure": ["config_allowed"]}
    fewer = loosen("check.secret_exposure", config={"allow": allow[1:]})
    assert changes(base, fewer) == {}  # a smaller allow list is stricter
    core = DECLARED["check.module_removal"].config
    smaller = loosen("check.module_removal",
                     config={**core, "core_tests": core["core_tests"][1:]})  # fmt: skip
    assert changes(base, smaller) == {"check.module_removal": ["config_narrowed"]}
    tighter = loosen("check.tool_ruff", when=frozenset({"ci", "enable"}))
    assert changes(base, tighter) == {}
    gone = tuple(c for c in base if c.check_id != "check.tool_pytest")
    assert changes(base, gone) == {"check.tool_pytest": ["removed"]}


def repo_with_lock(tmp_path: Path) -> Path:
    tmp_path.mkdir()
    git(tmp_path, "init", "-q")
    (tmp_path / LOCK_FILE).write_bytes(lock_bytes(lock_document(BUILTIN_CHECKS)))
    git(tmp_path, "add", LOCK_FILE)
    git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def policy(tmp_path: Path, root: Path, base_ref: str | None = "HEAD") -> CheckResult:
    check = build(CheckPolicyCheck, "check.policy")
    return check.run(context(root, tmp_path / "ctx", base_ref=base_ref))


def test_a_loosening_needs_an_approval_bound_to_the_exact_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = repo_with_lock(tmp_path / "repo")
    (tmp_path / "ctx").mkdir()
    assert policy(tmp_path, root).status == "pass"
    loosened = loosen("check.tool_mypy", required=False)
    monkeypatch.setattr(policy_module, "BUILTIN_CHECKS", loosened)
    stale = policy(tmp_path, root)
    assert {f.code for f in stale.findings} == {"CHECK_LOCK_STALE", "CHECK_LOOSENING_UNAPPROVED"}
    (root / LOCK_FILE).write_bytes(lock_bytes(lock_document(loosened)))  # the PR updates the lock
    found = policy(tmp_path, root)
    assert [f.code for f in found.findings] == ["CHECK_LOOSENING_UNAPPROVED"]
    item: Any = found.findings[0].details
    assert item["check_id"] == "check.tool_mypy" and item["after"] is not None
    approval = {
        "schema": "amplai.check-approval.v1", "check_id": "check.tool_mypy",
        "before": item["before"], "after": "sha256:" + "0" * 64,
        "approver": "operator", "reason": "test", "approved_at": "2026-10-14T00:00:00Z",
    }  # fmt: skip
    write(root, f"{APPROVALS_DIR}/mypy.json", json.dumps(approval))
    assert [f.code for f in policy(tmp_path, root).findings] == ["CHECK_LOOSENING_UNAPPROVED"]
    write(root, f"{APPROVALS_DIR}/mypy.json", json.dumps({**approval, "after": item["after"]}))
    approved = policy(tmp_path, root)
    assert approved.status == "pass", [f.wire() for f in approved.findings]
    assert approved.details["approvals_used"] == [f"{APPROVALS_DIR}/mypy.json"]
    write(root, f"{APPROVALS_DIR}/broken.json", "{}")
    assert [f.code for f in policy(tmp_path, root).findings] == ["CHECK_APPROVAL_INVALID"]


def test_the_lock_must_exist_and_a_base_without_a_lock_is_empty(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (tmp_path / "ctx").mkdir()
    git(root, "init", "-q")
    write(root, "README", "x")
    git(root, "add", "README")
    git(root, "commit", "-q", "-m", "first")
    missing = policy(tmp_path, root)
    assert [f.code for f in missing.findings] == ["CHECK_LOCK_MISSING"]
    (root / LOCK_FILE).write_bytes(lock_bytes(lock_document(BUILTIN_CHECKS)))
    assert policy(tmp_path, root).status == "pass"  # the PR that adds the lock
    with pytest.raises(RuntimeFault) as excinfo:
        policy(tmp_path, root, base_ref="no-such-ref")
    assert excinfo.value.code == "CHECK_BASE_REF"


def test_the_committed_lock_is_the_registry() -> None:
    assert (REPO / LOCK_FILE).read_bytes() == lock_bytes(lock_document(BUILTIN_CHECKS))
