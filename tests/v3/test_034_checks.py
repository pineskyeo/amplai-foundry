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
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from amplai_foundry.runtime import cli
from amplai_foundry.runtime.checks import policy as policy_module
from amplai_foundry.runtime.checks.boundary import ModuleBoundaryCheck
from amplai_foundry.runtime.checks.builtin import BASE_PROTECTED, BUILTIN_CHECKS, CI_EXTERNAL
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


def with_lock(root: Path, external: tuple[str, ...] = ()) -> Path:
    document = {"schema": "amplai.checks-lock.v1", "checks": {}, "ci_external": list(external)}
    (root / LOCK_FILE).write_bytes(lock_bytes(document))
    return root


def test_an_optional_failure_is_a_warning_and_a_crash_is_an_error(tmp_path: Path) -> None:
    bad = CheckResult("fail", (Finding("SOFT", "soft"),))
    soft = fixed("check.soft", bad, required=False)
    summary = run_checks("ci", root=tmp_path, record_dir=tmp_path,
                         checks=[*protected_set(), soft])  # fmt: skip
    assert summary["outcome"] == "pass" and summary["warnings"] == ["check.soft: fail"]

    class Crashes(Fixed):
        def run(self, context: CheckContext) -> CheckResult:
            raise ValueError("boom")

    crash = Crashes(fixed("check.crash").spec, fixed("check.crash").check)
    with pytest.raises(Hold) as held:
        run_checks("ci", root=tmp_path, record_dir=tmp_path, checks=[*protected_set(), crash])
    details: Any = held.value.details
    assert details["failed"][0]["status"] == "error"
    assert details["failed"][0]["findings"][0]["code"] == "CHECK_CRASHED"


def test_moment_and_target_select_checks_and_protected_checks_always_run(tmp_path: Path) -> None:
    checks = [
        *protected_set(),
        fixed("check.at_enable", when=frozenset({"enable"}), targets=("kind:git_host",)),
        fixed("check.every_kind", when=frozenset({"enable"}), targets=("kind:*",)),
        fixed("check.core_only", when=frozenset({"enable"})),
        fixed("check.own_protected", when=frozenset({"enable"}), protected=True),
    ]
    summary = run_checks("enable", root=tmp_path, record_dir=tmp_path, checks=checks,
                         targets=("kind:git_host",))  # fmt: skip
    assert [r["check_id"] for r in summary["results"]] == [
        "check.at_enable", "check.every_kind", "check.own_protected",
    ]  # fmt: skip
    ci = run_checks("ci", root=tmp_path, record_dir=tmp_path, checks=checks,
                    targets=("pack:none",))  # fmt: skip
    assert [r["check_id"] for r in ci["results"]] == sorted(BASE_PROTECTED)
    with pytest.raises(Hold) as held:
        run_checks("upgrade", root=tmp_path, record_dir=tmp_path, checks=checks)
    assert held.value.code == "CHECK_NONE_SELECTED"


def test_only_a_locked_ci_external_check_is_skipped_and_the_run_says_so(tmp_path: Path) -> None:
    checks = [*protected_set(), fixed("check.ci_tool"), fixed("check.other")]
    with pytest.raises(Hold) as held:  # no lock: nothing can be skipped
        run_checks("ci", root=tmp_path, record_dir=tmp_path, checks=checks,
                   skip=("check.ci_tool",))  # fmt: skip
    assert held.value.code == "CHECK_SKIP_REFUSED"
    with_lock(tmp_path, ("check.ci_tool",))
    with pytest.raises(Hold) as held:
        run_checks("ci", root=tmp_path, record_dir=tmp_path, checks=checks,
                   skip=("check.other",))  # fmt: skip
    assert held.value.code == "CHECK_SKIP_REFUSED"
    summary = run_checks("ci", root=tmp_path, record_dir=tmp_path, checks=checks,
                         skip=("check.ci_tool",))  # fmt: skip
    assert summary["outcome"] == "pass_with_skips" and summary["skipped"] == ["check.ci_tool"]
    record = json.loads(Path(summary["record"]).read_text())
    assert record["outcome"] == "pass_with_skips" and record["skipped"] == ["check.ci_tool"]


def test_protected_checks_can_not_be_skipped_or_missing(tmp_path: Path) -> None:
    flagged = fixed("check.flagged", protected=True)
    with_lock(tmp_path, ("check.secret_exposure", "check.flagged"))  # even if the lock says so
    for skipped in ("check.secret_exposure", "check.flagged"):
        with pytest.raises(Hold) as held:
            run_checks("ci", root=tmp_path, record_dir=tmp_path,
                       checks=[*protected_set(), flagged], skip=(skipped,))  # fmt: skip
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
    assert [c["check_id"] for c in json.loads(listed.stdout)] == [
        "check.module_boundary", "check.secret_exposure", "check.policy",
        "check.evaluator_qsuite",
    ]  # fmt: skip
    argv = ["checks", "run", "--when", "ci", "--target", "pack:none",
            "--record-dir", str(tmp_path), "--root", str(REPO)]  # fmt: skip
    ran = CliRunner().invoke(cli.app, argv)
    assert ran.exit_code == 0, ran.output
    summary = json.loads(ran.stdout)
    assert [r["check_id"] for r in summary["results"]] == [
        "check.module_boundary", "check.secret_exposure", "check.policy",
    ]  # fmt: skip
    assert "warning: check.policy: no --base-ref" in ran.stderr
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

# Built here, so this file carries no pattern itself.
PEM = "-----BEGIN " + "PRIVATE KEY-----\n"
CHECK_ONLY = {
    "slack-webhook": "https://hooks.slack.com/services/" + "T0ABCDEF1/B0ABCDEF1/" + "a1" * 12,
    "github-token": "ghp" + "_" + "A1b2" * 9,
    "github-pat": "github" + "_pat_" + "11ABCDEFG" * 6,
    "aws-access-key-id": "AKIA" + "ABCDEFGH12345678",
    "pem-encrypted-or-dsa": "-----BEGIN " + "ENCRYPTED PRIVATE KEY-----",
}


def tracked(root: Path, files: dict[str, str]) -> Path:
    git(root, "init", "-q")
    for rel, text in files.items():
        write(root, rel, text)
        git(root, "add", rel)
    return root


def test_a_tracked_secret_pattern_fails_unless_that_occurrence_is_allowed(tmp_path: Path) -> None:
    tracked(tmp_path, {"keys/id.pem": PEM})
    write(tmp_path, "untracked.pem", PEM)
    found = build(SecretExposureCheck, "check.secret_exposure").run(context(tmp_path, tmp_path))
    assert [(f.code, f.path, (f.details or {})["pattern"]) for f in found.findings] == [
        ("SECRET_PATTERN_TRACKED", "keys/id.pem", "secret-pattern-1")
    ]
    assert PEM.strip() not in json.dumps([f.wire() for f in found.findings])
    key = (found.findings[0].details or {})["allow_key"]
    allowed = build(SecretExposureCheck, "check.secret_exposure", config={"allow": (key,)})
    assert allowed.run(context(tmp_path, tmp_path)).status == "pass"
    # the allow entry is that one line: a second occurrence in the same file is a finding
    write(tmp_path, "keys/id.pem", PEM + "x\n" + PEM.replace("\n", " # another\n"))
    again = allowed.run(context(tmp_path, tmp_path))
    assert [(f.details or {})["line"] for f in again.findings] == [3]


@pytest.mark.parametrize("pattern", sorted(CHECK_ONLY))
def test_the_check_adds_patterns_beyond_the_artifact_scanner(tmp_path: Path, pattern: str) -> None:
    tracked(tmp_path, {"config.txt": f"value = {CHECK_ONLY[pattern]}\n"})
    found = build(SecretExposureCheck, "check.secret_exposure").run(context(tmp_path, tmp_path))
    assert pattern in [(f.details or {})["pattern"] for f in found.findings]


def test_this_repository_tracks_no_secret_pattern_outside_the_allow_list(tmp_path: Path) -> None:
    found = build(SecretExposureCheck, "check.secret_exposure").run(context(REPO, tmp_path))
    assert found.status == "pass", [f.wire() for f in found.findings]
    assert found.details["allow_unused"] == []


# -- check policy (M-19) ---------------------------------------------------------------------------


def lock_of(checks: tuple[CheckSpec, ...], external: tuple[str, ...] = CI_EXTERNAL) -> Any:
    return lock_document(checks, external)


def loosen(check_id: str, **override: Any) -> tuple[CheckSpec, ...]:
    return tuple(dataclasses.replace(c, **override) if c.check_id == check_id else c
                 for c in BUILTIN_CHECKS)  # fmt: skip


def changes(
    base: tuple[CheckSpec, ...], now: tuple[CheckSpec, ...], external: tuple[str, ...] = CI_EXTERNAL
) -> dict[str, list[str]]:
    found = loosenings(lock_of(base), lock_of(now, external))
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
    more = loosen("check.secret_exposure", config={"allow": (*allow, "a.py::secret-pattern-1::0")})
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


def test_a_lower_version_and_a_new_ci_skip_are_loosenings() -> None:
    raised = loosen("check.tool_ruff", version="2")
    assert changes(raised, BUILTIN_CHECKS) == {"check.tool_ruff": ["version_lowered"]}
    assert changes(BUILTIN_CHECKS, raised) == {}
    named = loosen("check.tool_ruff", version="1-beta")  # not numbers: counted as lowered
    assert changes(BUILTIN_CHECKS, named) == {"check.tool_ruff": ["version_lowered"]}
    skippable = (*CI_EXTERNAL, "check.kind_conformance")
    assert changes(BUILTIN_CHECKS, BUILTIN_CHECKS, skippable) == {
        "check.kind_conformance": ["ci_external_added"]
    }


def repo_with_lock(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-q")
    (path / LOCK_FILE).write_bytes(lock_bytes(lock_of(BUILTIN_CHECKS)))
    git(path, "add", LOCK_FILE)
    git(path, "commit", "-q", "-m", "base")
    return path


def policy(tmp_path: Path, root: Path, base_ref: str | None = "HEAD") -> CheckResult:
    check = build(CheckPolicyCheck, "check.policy")
    work = tmp_path / "ctx" / str(len(list((tmp_path / "ctx").glob("*"))))
    work.mkdir(parents=True)
    return check.run(CheckContext(root=root, when="ci", workdir=work, base_ref=base_ref))


def approval(item: dict[str, Any], **override: Any) -> dict[str, Any]:
    return {
        "schema": "amplai.check-approval.v1", "check_id": item["check_id"],
        "before": item["before"], "after": item["after"], "approver": "operator",
        "reason": "test", "approved_at": "2026-10-14T00:00:00Z", **override,
    }  # fmt: skip


def test_a_loosening_needs_an_approval_bound_to_the_exact_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = repo_with_lock(tmp_path / "repo")
    clean = policy(tmp_path, root)
    assert clean.status == "pass" and clean.details["policy_code"].startswith("this tree")
    loosened = loosen("check.tool_mypy", required=False)
    monkeypatch.setattr(policy_module, "BUILTIN_CHECKS", loosened)
    stale = policy(tmp_path, root)
    assert {f.code for f in stale.findings} == {"CHECK_LOCK_STALE", "CHECK_LOOSENING_UNAPPROVED"}
    (root / LOCK_FILE).write_bytes(lock_bytes(lock_of(loosened)))  # the change updates the lock
    found = policy(tmp_path, root)
    assert [f.code for f in found.findings] == ["CHECK_LOOSENING_UNAPPROVED"]
    item: Any = found.findings[0].details
    assert item["check_id"] == "check.tool_mypy" and item["after"] is not None
    target = f"{APPROVALS_DIR}/mypy.json"
    write(root, target, json.dumps(approval(item, after="sha256:" + "0" * 64)))
    assert [f.code for f in policy(tmp_path, root).findings] == ["CHECK_LOOSENING_UNAPPROVED"]
    for bad_at in ("2026-10-14", "2999-01-01T00:00:00Z"):  # not RFC 3339 UTC; in the future
        write(root, target, json.dumps(approval(item, approved_at=bad_at)))
        assert "CHECK_APPROVAL_INVALID" in [f.code for f in policy(tmp_path, root).findings]
    write(root, target, json.dumps(approval(item)))
    approved = policy(tmp_path, root)
    assert approved.status == "pass", [f.wire() for f in approved.findings]
    assert approved.details["approvals_used"] == [target]
    write(root, f"{APPROVALS_DIR}/mypy-again.json", json.dumps(approval(item)))
    assert [f.code for f in policy(tmp_path, root).findings] == ["CHECK_APPROVAL_DUPLICATE"]
    (root / APPROVALS_DIR / "mypy-again.json").unlink()
    write(root, f"{APPROVALS_DIR}/broken.json", "{}")
    assert [f.code for f in policy(tmp_path, root).findings] == ["CHECK_APPROVAL_INVALID"]


def test_an_approval_is_single_use(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = repo_with_lock(tmp_path / "repo")
    loosened = loosen("check.tool_mypy", required=False)
    monkeypatch.setattr(policy_module, "BUILTIN_CHECKS", loosened)
    (root / LOCK_FILE).write_bytes(lock_bytes(lock_of(loosened)))
    item: Any = policy(tmp_path, root).findings[0].details
    write(root, f"{APPROVALS_DIR}/mypy.json", json.dumps(approval(item)))
    assert policy(tmp_path, root).status == "pass"
    # merged: then a later change restores the old lock on its base and loosens again the same way
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "loosened with approval")
    (root / LOCK_FILE).write_bytes(lock_bytes(lock_of(BUILTIN_CHECKS)))
    git(root, "commit", "-q", "-am", "restored")
    (root / LOCK_FILE).write_bytes(lock_bytes(lock_of(loosened)))
    assert [f.code for f in policy(tmp_path, root).findings] == ["CHECK_APPROVAL_REUSED"]
    # the same record under a new name is the same approval: still reused
    old = root / APPROVALS_DIR / "mypy.json"
    (root / APPROVALS_DIR / "mypy-copy.json").write_bytes(old.read_bytes())
    old.unlink()
    assert [f.code for f in policy(tmp_path, root).findings] == ["CHECK_APPROVAL_REUSED"]


def test_without_a_base_ref_the_result_warns_and_a_base_without_a_lock_is_empty(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    write(root, "README", "x")
    git(root, "add", "README")
    git(root, "commit", "-q", "-m", "first")
    missing = policy(tmp_path, root)
    assert [f.code for f in missing.findings] == ["CHECK_LOCK_MISSING"]
    (root / LOCK_FILE).write_bytes(lock_bytes(lock_of(BUILTIN_CHECKS)))
    assert policy(tmp_path, root).status == "pass"  # the change that adds the lock
    local = policy(tmp_path, root, base_ref=None)
    assert local.status == "pass" and local.details["warnings"] == [policy_module.NO_BASE_WARNING]
    with pytest.raises(RuntimeFault) as excinfo:
        policy(tmp_path, root, base_ref="no-such-ref")
    assert excinfo.value.code == "CHECK_BASE_REF"


def test_with_a_base_ref_the_base_refs_policy_code_decides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    shutil.copytree(REPO / "src" / "amplai_foundry", root / "src" / "amplai_foundry",
                    ignore=shutil.ignore_patterns("__pycache__"))  # fmt: skip
    base_policy = root / "src/amplai_foundry/runtime/checks/policy.py"
    code = base_policy.read_text()
    marked = code.replace(
        '"details": dict(outcome.details)}',
        '"details": {**dict(outcome.details), "marker": "base"}}',
    )
    assert marked != code
    base_policy.write_text(marked)
    (root / LOCK_FILE).write_bytes(lock_bytes(lock_of(BUILTIN_CHECKS)))
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    decided = policy(tmp_path, root)
    assert decided.status == "pass", [f.wire() for f in decided.findings]
    assert decided.details["marker"] == "base"
    assert decided.details["policy_code"] == "HEAD (base ref code)"
    loosened = loosen("check.policy", when=frozenset({"ci"}))
    monkeypatch.setattr(policy_module, "BUILTIN_CHECKS", loosened)
    (root / LOCK_FILE).write_bytes(lock_bytes(lock_of(loosened)))
    held = policy(tmp_path, root)
    assert [f.code for f in held.findings] == ["CHECK_LOOSENING_UNAPPROVED"]
    assert held.details["marker"] == "base"


def test_the_committed_lock_is_the_registry() -> None:
    assert (REPO / LOCK_FILE).read_bytes() == lock_bytes(lock_of(BUILTIN_CHECKS))


# -- failure review (PR #94, c9d338f) --------------------------------------------------------------


def test_the_base_policy_falls_back_only_when_the_file_is_certainly_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = repo_with_lock(tmp_path / "repo")
    write(root, policy_module.POLICY_PATH, "# an older policy without the API\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "policy without API")
    with pytest.raises(RuntimeFault) as excinfo:  # present but not usable: fail closed
        policy_module.base_source(root, "HEAD", tmp_path / "x")
    assert excinfo.value.code == "CHECK_POLICY_BASE"
    real = policy_module._git

    def broken(where: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
        if args[0] in ("cat-file", "ls-tree"):
            return subprocess.CompletedProcess(list(args), 128, b"", b"fatal: broken object")
        return real(where, *args)

    monkeypatch.setattr(policy_module, "_git", broken)
    with pytest.raises(RuntimeFault) as excinfo:  # can not tell: fail closed, no bootstrap
        policy_module.base_source(root, "HEAD", tmp_path / "y")
    assert excinfo.value.code == "CHECK_POLICY_BASE"


def test_an_unknown_lock_schema_is_one_loosening_that_needs_an_approval(tmp_path: Path) -> None:
    root = repo_with_lock(tmp_path / "repo")
    newer = {**lock_of(BUILTIN_CHECKS), "schema": "amplai.checks-lock.v2"}
    (root / LOCK_FILE).write_bytes(lock_bytes(newer))
    found = policy_module.evaluate(root, "HEAD", newer)
    assert [f.code for f in found.findings] == ["CHECK_LOOSENING_UNAPPROVED"]
    item: Any = found.findings[0].details
    assert item["check_id"] == LOCK_FILE
    assert item["changes"] == [{"what": "lock_schema_unknown", "schema": "amplai.checks-lock.v2"}]
    write(root, f"{APPROVALS_DIR}/schema.json", json.dumps(approval(item)))
    assert policy_module.evaluate(root, "HEAD", newer).status == "pass"


def test_a_check_that_read_nothing_does_not_pass(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    boundary = build(ModuleBoundaryCheck, "check.module_boundary").run(context(empty, tmp_path))
    assert [f.code for f in boundary.findings] == ["BOUNDARY_NO_SOURCE"]
    secrets = build(SecretExposureCheck, "check.secret_exposure")
    assert [f.code for f in secrets.run(context(empty, tmp_path)).findings] == [
        "SECRET_SCAN_NOT_GIT"
    ]
    git(empty, "init", "-q")
    assert [f.code for f in secrets.run(context(empty, tmp_path)).findings] == ["SECRET_SCAN_EMPTY"]


def test_a_crlf_line_has_the_same_allow_key() -> None:
    from amplai_foundry.runtime.checks.secret_exposure import occurrences

    lf = list(occurrences(("x\n" + PEM).encode()))
    crlf = list(occurrences(("x\r\n" + PEM.replace("\n", "\r\n")).encode()))
    assert lf == crlf and lf[0][1] == 2


def test_early_holds_are_recorded_and_a_failed_record_keeps_the_hold(tmp_path: Path) -> None:
    records = tmp_path / "records"
    with pytest.raises(Hold) as held:  # the protected fixtures declare only `ci`
        run_checks("upgrade", root=tmp_path, record_dir=records, checks=protected_set())
    assert held.value.code == "CHECK_NONE_SELECTED"
    details: Any = held.value.details
    stored = json.loads(Path(details["record"]).read_text())
    assert stored["outcome"] == "hold" and stored["hold"]["code"] == "CHECK_NONE_SELECTED"
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("x")
    bad = CheckResult("fail", (Finding("X", "x"),))
    with pytest.raises(Hold) as held:
        run_checks("ci", root=tmp_path, record_dir=blocked,
                   checks=[*protected_set(), fixed("check.lint", bad)])  # fmt: skip
    details = held.value.details
    assert held.value.code == "CHECK_FAILED" and "record_error" in details
    assert details["failed"][0]["check_id"] == "check.lint"
    with pytest.raises(RuntimeFault) as excinfo:
        run_checks("ci", root=tmp_path, record_dir=blocked, checks=protected_set())
    assert excinfo.value.code == "CHECK_RECORD"


def test_notes_go_into_the_record_and_the_warnings(tmp_path: Path) -> None:
    summary = run_checks("ci", root=tmp_path, record_dir=tmp_path, checks=protected_set(),
                         notes=("base abc can not be fetched (push)",))  # fmt: skip
    assert summary["warnings"][0] == "base abc can not be fetched (push)"
    assert json.loads(Path(summary["record"]).read_text())["notes"] == [
        "base abc can not be fetched (push)"
    ]


def test_the_safe_extract_fallback_refuses_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io
    import tarfile

    def archive(link: bool) -> tarfile.TarFile:
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w") as tar:
            body = b"x = 1\n"
            info = tarfile.TarInfo("src/a.py")
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
            if link:
                sym = tarfile.TarInfo("src/evil")
                sym.type, sym.linkname = tarfile.SYMTYPE, "/etc/passwd"
                tar.addfile(sym)
        data.seek(0)
        return tarfile.open(fileobj=data)

    monkeypatch.delattr(tarfile, "data_filter")
    policy_module._safe_extract(archive(False), tmp_path / "ok")
    assert (tmp_path / "ok" / "src" / "a.py").read_text() == "x = 1\n"
    with pytest.raises(RuntimeFault) as excinfo:
        policy_module._safe_extract(archive(True), tmp_path / "bad")
    assert excinfo.value.code == "CHECK_POLICY_BASE"


def test_the_removal_run_drops_pytest_variables_and_kills_its_group_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys
    import time

    from amplai_foundry.runtime.checks.removal import clean_env, run_group

    monkeypatch.setenv("PYTEST_ADDOPTS", "-k nothing")
    assert not [k for k in clean_env() if k.startswith("PYTEST_")]
    started = time.monotonic()
    child = (
        "import subprocess, sys, time; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); time.sleep(60)"
    )
    assert run_group([sys.executable, "-c", child], tmp_path, clean_env(), 1) is None
    assert time.monotonic() - started < 30


# -- regression review (PR #94) --------------------------------------------------------------------


def test_protected_lowered_and_a_coverage_list_turned_allow_list_are_loosenings() -> None:
    assert changes(BUILTIN_CHECKS, loosen("check.policy", protected=False)) == {
        "check.policy": ["protected_lowered"]
    }
    turned = loosen("check.module_removal", loosens_when_added=frozenset({"core_tests"}))
    assert changes(BUILTIN_CHECKS, turned) == {"check.module_removal": ["config_semantics"]}


def test_the_bound_digest_includes_ci_external() -> None:
    from amplai_foundry.runtime.checks.lock import bound_digest

    entry = DECLARED["check.kind_conformance"].entry()
    assert bound_digest(entry, True) != bound_digest(entry, False)
    skippable = (*CI_EXTERNAL, "check.kind_conformance")
    (item,) = loosenings(lock_of(BUILTIN_CHECKS), lock_of(BUILTIN_CHECKS, skippable))
    assert item["before"] == bound_digest(entry, False)
    assert item["after"] == bound_digest(entry, True)


def test_a_listed_but_unreadable_base_path_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = repo_with_lock(tmp_path / "repo")
    real = policy_module._git

    def unreadable(where: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
        if args[0] == "cat-file":
            return subprocess.CompletedProcess(list(args), 128, b"", b"fatal: bad object")
        return real(where, *args)

    monkeypatch.setattr(policy_module, "_git", unreadable)
    with pytest.raises(RuntimeFault) as excinfo:  # ls-tree lists it, cat-file can not read it
        policy_module.path_absent(root, "HEAD", LOCK_FILE)
    assert excinfo.value.code == "CHECK_POLICY_BASE"
    assert policy_module.path_absent(root, "HEAD", "no/such/file") is True


def test_the_secret_scan_size_bound_and_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from amplai_foundry.runtime.checks import secret_exposure

    outside = tmp_path / "outside.pem"
    outside.write_text(PEM)
    (tmp_path / "repo").mkdir()
    root = tracked(tmp_path / "repo", {"ok.txt": "nothing here\n"})
    (root / "link.pem").symlink_to(outside)
    git(root, "add", "link.pem")
    check = build(SecretExposureCheck, "check.secret_exposure")
    found = check.run(context(root, tmp_path))
    assert found.status == "pass" and found.details["scanned"] == 1  # the link is not followed
    monkeypatch.setattr(secret_exposure, "MAX_FILE_BYTES", 4)
    large = check.run(context(root, tmp_path))
    assert [(f.code, f.path) for f in large.findings] == [
        ("SECRET_SCAN_TOO_LARGE", "ok.txt"),
        ("SECRET_SCAN_EMPTY", None),  # the only regular file was not read
    ]


SLOW_CORE = """
import time

def test_slow():
    time.sleep(60)
"""


def test_a_removal_run_that_does_not_finish_is_a_timeout_finding(tmp_path: Path) -> None:
    write(tmp_path, "tests/test_core.py", SLOW_CORE)
    check = removal(core_tests=("tests/test_core.py",))
    check.timeout = 5
    found = check.run(context(tmp_path, tmp_path))
    assert [f.code for f in found.findings] == ["MODULE_REMOVAL_TIMEOUT"]
    assert found.details["runs"]["git_host.github"]["exit_code"] is None


def test_the_removal_plugin_refuses_a_module_that_is_already_imported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    from amplai_foundry.runtime.checks import removal_plugin

    monkeypatch.setenv(removal_plugin.ENV_FILES, "json")  # imported long before
    before = list(sys.meta_path)
    with pytest.raises(RuntimeError, match="already imported"):
        removal_plugin._install()
    assert sys.meta_path == before


def test_a_module_without_a_conformance_subject_fails_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from amplai_foundry.runtime.checks import kind_conformance

    monkeypatch.setattr(kind_conformance, "_subject", lambda spec, workdir: None)
    found = build(KindConformanceCheck, "check.kind_conformance").run(
        context(REPO, tmp_path, targets=("kind:git_host",))
    )
    assert [f.code for f in found.findings] == ["CONFORMANCE_NO_SUBJECT"]
