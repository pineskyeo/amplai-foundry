"""Tests for the kit distribution wrapper.

The wrapper writes into other repositories, so these tests pin the rules that
keep a mistake from doing that: never guess a path, never install after a
failed plan, and stop at the first failure instead of continuing.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import kit_distribute as kd  # noqa: E402


@pytest.fixture
def config() -> dict:
    return {
        "schema_version": "1.0",
        "project_id": "test-project",
        "path_map": ".ai-team/local/kit-targets.json",
        "targets": [
            {"app_id": "alpha", "role": "source", "path_hint": "."},
            {"app_id": "beta", "role": "target", "path_hint": "../beta"},
        ],
    }


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class TestPathResolution:
    def test_a_target_without_a_configured_path_is_unresolved(self, config, monkeypatch):
        monkeypatch.setattr(kd, "load_path_map", lambda _cfg: {})
        resolved, unresolved = kd.resolve_paths(config, config["targets"])
        assert resolved == []
        assert {u["app_id"] for u in unresolved} == {"alpha", "beta"}
        assert all(u["reason"] == "no host-local path configured" for u in unresolved)

    def test_the_hint_is_offered_as_a_candidate_never_used(self, config, monkeypatch):
        monkeypatch.setattr(kd, "load_path_map", lambda _cfg: {})
        _, unresolved = kd.resolve_paths(config, config["targets"])
        candidate = next(u for u in unresolved if u["app_id"] == "alpha")["candidate_from_hint"]
        # It is reported so a human can confirm it, but it never becomes "path".
        assert candidate is not None
        assert "path" not in next(u for u in unresolved if u["app_id"] == "alpha")

    def test_a_relative_configured_path_is_refused(self, config, monkeypatch):
        monkeypatch.setattr(kd, "load_path_map", lambda _cfg: {"paths": {"alpha": "relative/dir"}})
        _, unresolved = kd.resolve_paths(config, config["targets"][:1])
        assert unresolved[0]["reason"] == "configured path is not absolute"

    def test_a_missing_configured_path_is_refused(self, config, monkeypatch, tmp_path):
        missing = tmp_path / "not-there"
        monkeypatch.setattr(kd, "load_path_map", lambda _cfg: {"paths": {"alpha": str(missing)}})
        _, unresolved = kd.resolve_paths(config, config["targets"][:1])
        assert unresolved[0]["reason"] == "configured path does not exist"

    def test_a_configured_path_resolves(self, config, monkeypatch, tmp_path):
        monkeypatch.setattr(kd, "load_path_map", lambda _cfg: {"paths": {"alpha": str(tmp_path)}})
        resolved, unresolved = kd.resolve_paths(config, config["targets"][:1])
        assert unresolved == []
        assert resolved[0]["path"] == str(tmp_path)

    def test_user_and_variable_expansion(self, config, monkeypatch, tmp_path):
        monkeypatch.setenv("KD_TEST_ROOT", str(tmp_path))
        monkeypatch.setattr(kd, "load_path_map", lambda _cfg: {"paths": {"alpha": "$KD_TEST_ROOT"}})
        resolved, _ = kd.resolve_paths(config, config["targets"][:1])
        assert resolved[0]["path"] == str(tmp_path)


class TestOrdering:
    def test_the_source_app_goes_first(self, config):
        ordered = kd.order_targets(list(reversed(config["targets"])))
        assert [t["app_id"] for t in ordered] == ["alpha", "beta"]

    def test_selecting_an_unknown_app_is_an_error(self, config):
        with pytest.raises(kd.DistributeError) as excinfo:
            kd.select(config["targets"], "gamma")
        assert "gamma" in str(excinfo.value)

    def test_selecting_a_known_app_narrows_the_run(self, config):
        assert [t["app_id"] for t in kd.select(config["targets"], "beta")] == ["beta"]


class TestVerify:
    def test_a_version_mismatch_names_the_app(self, config, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(kd, "kit_version", lambda: "9.9.9")
        _write(
            tmp_path / ".ai-team" / "install" / "amplai-loop-kit.json",
            {"package_version": "1.0.0"},
        )
        rc = kd.cmd_verify(config, [{"app_id": "alpha", "path": str(tmp_path)}])
        payload = json.loads(capsys.readouterr().out)
        assert rc == kd.EXIT_ERROR
        assert payload["mismatched"] == ["alpha"]
        assert payload["targets"][0]["installed"] == "1.0.0"

    def test_matching_versions_pass(self, config, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(kd, "kit_version", lambda: "9.9.9")
        _write(
            tmp_path / ".ai-team" / "install" / "amplai-loop-kit.json",
            {"package_version": "9.9.9"},
        )
        rc = kd.cmd_verify(config, [{"app_id": "alpha", "path": str(tmp_path)}])
        payload = json.loads(capsys.readouterr().out)
        assert rc == kd.EXIT_OK
        assert payload["ok"] is True

    def test_a_target_with_no_install_record_counts_as_mismatched(
        self, config, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setattr(kd, "kit_version", lambda: "9.9.9")
        rc = kd.cmd_verify(config, [{"app_id": "alpha", "path": str(tmp_path)}])
        payload = json.loads(capsys.readouterr().out)
        assert rc == kd.EXIT_ERROR
        assert payload["targets"][0]["installed"] is None

    def test_a_target_we_meant_to_skip_is_not_a_mismatch(
        self, config, tmp_path, monkeypatch, capsys
    ):
        """expect_installed: false 인 target 의 부재는 정상이다.

        그것이 없으면 의도적으로 비워 둔 fleet 에서 --verify 가 항상 rc=2 를 내고,
        늘 실패하는 check 는 아무도 안 읽는다.
        """
        monkeypatch.setattr(kd, "kit_version", lambda: "9.9.9")
        rc = kd.cmd_verify(
            config,
            [{"app_id": "alpha", "path": str(tmp_path), "expect_installed": False}],
        )
        payload = json.loads(capsys.readouterr().out)
        assert rc == kd.EXIT_OK
        assert payload["mismatched"] == []
        assert payload["targets"][0]["expect_installed"] is False
        assert payload["targets"][0]["installed"] is None

    def test_a_target_we_meant_to_skip_but_found_installed_is_a_mismatch(
        self, config, tmp_path, monkeypatch, capsys
    ):
        """반대 방향도 잡는다 — 안 넣기로 한 곳에 누가 넣었으면 그것도 어긋남이다."""
        monkeypatch.setattr(kd, "kit_version", lambda: "9.9.9")
        _write(
            tmp_path / ".ai-team" / "install" / "amplai-loop-kit.json",
            {"package_version": "9.9.9"},
        )
        rc = kd.cmd_verify(
            config,
            [{"app_id": "alpha", "path": str(tmp_path), "expect_installed": False}],
        )
        payload = json.loads(capsys.readouterr().out)
        assert rc == kd.EXIT_ERROR
        assert payload["mismatched"] == ["alpha"]
        assert payload["targets"][0]["installed"] == "9.9.9"


class TestRunGates:
    """The wrapper must refuse before it writes, not apologise afterwards."""

    def _resolved(self, tmp_path, monkeypatch, config, names=("alpha", "beta")):
        paths = {}
        for name in names:
            d = tmp_path / name
            d.mkdir()
            paths[name] = str(d)
        monkeypatch.setattr(kd, "load_path_map", lambda _cfg: {"paths": paths})
        monkeypatch.setattr(kd, "load_targets", lambda: (config, config["targets"]))
        monkeypatch.setattr(kd, "git_is_dirty", lambda _p: False)
        monkeypatch.setattr(
            kd,
            "preflight",
            lambda strict: {
                "selftest": {"ok": True, "detail": None},
                "kit_version": "9.9.9",
                "strict_clean": strict,
            },
        )
        return paths

    def test_an_unresolved_target_stops_the_run(self, config, monkeypatch, capsys):
        monkeypatch.setattr(kd, "load_path_map", lambda _cfg: {})
        monkeypatch.setattr(kd, "load_targets", lambda: (config, config["targets"]))
        rc = kd.main(["--all"])
        payload = json.loads(capsys.readouterr().out)
        assert rc == kd.EXIT_UNRESOLVED
        assert payload["ok"] is False
        assert len(payload["unresolved"]) == 2

    def test_a_failed_plan_installs_nothing(self, config, tmp_path, monkeypatch, capsys):
        self._resolved(tmp_path, monkeypatch, config)
        installs = []

        def fake(target, cfg, *, dry_run, uninstall=False, project_home=None):
            if not dry_run:
                installs.append(target["app_id"])
            ok = dry_run and target["app_id"] == "alpha"
            return {
                "app_id": target["app_id"],
                "path": target["path"],
                "returncode": 0 if ok else 1,
                "ok": ok,
                "report": {"ok": ok, "actions": []},
                "stderr": None,
            }

        monkeypatch.setattr(kd, "run_installer", fake)
        rc = kd.main(["--all"])
        payload = json.loads(capsys.readouterr().out)
        assert rc == kd.EXIT_ERROR
        assert payload["failed"] == ["beta"]
        # The whole point: a bad plan means nothing is written anywhere.
        assert installs == []

    def test_an_install_failure_stops_before_the_targets_after_it(
        self, config, tmp_path, monkeypatch, capsys
    ):
        """With only two targets a failure is always last, so `break` is untested.

        The real configuration has three apps and the wrapper writes into other
        repositories, so "stops at the first failure" has to be pinned with a
        target that comes after the one that fails.
        """
        config["targets"].append({"app_id": "gamma", "role": "target", "path_hint": "../gamma"})
        self._resolved(tmp_path, monkeypatch, config, names=("alpha", "beta", "gamma"))
        attempted = []

        def fake(target, cfg, *, dry_run, uninstall=False, project_home=None):
            if not dry_run:
                attempted.append(target["app_id"])
            ok = dry_run or target["app_id"] != "beta"
            return {
                "app_id": target["app_id"],
                "path": target["path"],
                "returncode": 0 if ok else 1,
                "ok": ok,
                "report": {"ok": ok, "actions": []},
                "stderr": None,
            }

        monkeypatch.setattr(kd, "run_installer", fake)
        rc = kd.main(["--all"])
        payload = json.loads(capsys.readouterr().out)
        assert rc == kd.EXIT_ERROR
        assert payload["stopped_at"] == "beta"
        # gamma comes after the failure and must never be touched.
        assert attempted == ["alpha", "beta"]
        assert payload["not_attempted"] == ["gamma"]

    def test_an_install_failure_stops_and_reports_what_was_done(
        self, config, tmp_path, monkeypatch, capsys
    ):
        self._resolved(tmp_path, monkeypatch, config)

        def fake(target, cfg, *, dry_run, uninstall=False, project_home=None):
            ok = dry_run or target["app_id"] == "alpha"
            return {
                "app_id": target["app_id"],
                "path": target["path"],
                "returncode": 0 if ok else 1,
                "ok": ok,
                "report": {"ok": ok, "actions": []},
                "stderr": None,
            }

        monkeypatch.setattr(kd, "run_installer", fake)
        rc = kd.main(["--all"])
        payload = json.loads(capsys.readouterr().out)
        assert rc == kd.EXIT_ERROR
        assert payload["stopped_at"] == "beta"
        # alpha stays installed; the run does not pretend to be atomic.
        assert [c["app_id"] for c in payload["completed"]] == ["alpha", "beta"]
        assert payload["completed"][0]["ok"] is True

    def test_a_dirty_target_blocks_install_but_not_a_plan(
        self, config, tmp_path, monkeypatch, capsys
    ):
        self._resolved(tmp_path, monkeypatch, config)
        monkeypatch.setattr(kd, "git_is_dirty", lambda _p: True)
        monkeypatch.setattr(
            kd,
            "run_installer",
            lambda target, cfg, **kw: {
                "app_id": target["app_id"],
                "path": target["path"],
                "returncode": 0,
                "ok": True,
                "report": {"ok": True, "actions": []},
                "stderr": None,
            },
        )

        assert kd.main(["--all"]) == kd.EXIT_ERROR
        blocked = json.loads(capsys.readouterr().out)
        assert blocked["error"] == "targets have uncommitted changes"

        # A plan changes nothing, so it still runs and reports the dirt.
        assert kd.main(["--dry-run"]) == kd.EXIT_OK
        planned = json.loads(capsys.readouterr().out)
        assert planned["ok"] is True
        assert sorted(planned["dirty"]) == ["alpha", "beta"]

    def test_a_failed_selftest_stops_before_any_plan(self, config, tmp_path, monkeypatch, capsys):
        self._resolved(tmp_path, monkeypatch, config)
        monkeypatch.setattr(
            kd,
            "preflight",
            lambda strict: {
                "selftest": {"ok": False, "detail": ["boom"]},
                "kit_version": "9.9.9",
                "strict_clean": strict,
            },
        )
        called = []
        monkeypatch.setattr(kd, "run_installer", lambda *a, **k: called.append(1) or {})
        rc = kd.main(["--all"])
        payload = json.loads(capsys.readouterr().out)
        assert rc == kd.EXIT_ERROR
        assert payload["error"] == "kit selftest failed"
        assert called == []


class TestRealPackage:
    """Facts about the checked-in configuration, not a mocked one."""

    def test_the_committed_target_list_holds_no_absolute_paths(self):
        raw = kd.TARGETS_FILE.read_text(encoding="utf-8")
        config = json.loads(raw)

        found: list[str] = []

        def walk(value):
            if isinstance(value, dict):
                for item in value.values():
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)
            elif isinstance(value, str) and (value.startswith("/") or value.startswith("~")):
                found.append(value)

        walk(config)
        assert found == []

    def test_the_host_local_path_map_is_git_ignored(self):
        rel = json.loads(kd.TARGETS_FILE.read_text(encoding="utf-8"))["path_map"]
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "check-ignore", rel],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{rel} is not git-ignored"

    def test_installed_version_reads_the_field_the_installer_writes(self):
        """The install record names it package_version, not version.

        A mock that invents the field name let this pass while every target
        reported as mismatched, so this reads a record the installer produced.
        """
        record = REPO_ROOT / kd.INSTALL_RECORD
        if not record.is_file():
            pytest.skip("the kit is not installed in this repository")
        raw = json.loads(record.read_text(encoding="utf-8"))
        assert "package_version" in raw
        assert kd.installed_version(REPO_ROOT) == raw["package_version"]

    def test_seal_verify_reports_a_broken_package(self, tmp_path):
        """The seal check must fail on a mismatch, not just on a clean tree.

        Nothing read CHECKSUMS.sha256 until this check existed, and the seal
        sat broken across two commits.  A green-only test would not have
        noticed either.
        """
        seal = kd.KIT_ROOT / "seal.py"
        clean = subprocess.run(
            [sys.executable, str(seal), "--verify"],
            capture_output=True,
            text=True,
        )
        assert clean.returncode == 0, clean.stdout
        assert json.loads(clean.stdout)["ok"] is True

        target = kd.KIT_ROOT / "distribution" / "targets.json"
        original = target.read_bytes()
        try:
            target.write_bytes(original + b"\n")
            broken = subprocess.run(
                [sys.executable, str(seal), "--verify"],
                capture_output=True,
                text=True,
            )
            payload = json.loads(broken.stdout)
            assert broken.returncode == 1
            assert payload["ok"] is False
            assert "distribution/targets.json" in payload["checksum_mismatched"]
            assert payload["hint"]
        finally:
            target.write_bytes(original)

    def test_seal_verify_notices_a_file_missing_from_checksums(self, tmp_path):
        """A new package file that was never sealed must not pass silently."""
        seal = kd.KIT_ROOT / "seal.py"
        extra = kd.KIT_ROOT / "distribution" / "__seal_probe__.json"
        extra.write_text("{}\n", encoding="utf-8")
        try:
            result = subprocess.run(
                [sys.executable, str(seal), "--verify"],
                capture_output=True,
                text=True,
            )
            payload = json.loads(result.stdout)
            assert result.returncode == 1
            assert "distribution/__seal_probe__.json" in payload["not_in_checksums"]
        finally:
            extra.unlink()

    def test_kit_version_matches_the_manifest(self):
        manifest = json.loads((kd.KIT_ROOT / "manifest.json").read_text(encoding="utf-8"))
        assert kd.kit_version() == manifest["version"]
