#!/usr/bin/env python3
"""Self-test the package manifest and conservative installer behavior."""
from __future__ import print_function

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.abspath(os.path.dirname(__file__))
INSTALLER = os.path.join(ROOT, "install.py")


def read_json(path):
    with io.open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def sha(path):
    digest = hashlib.sha256()
    with io.open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def write(path, text):
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    with io.open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def make_target(root):
    write(os.path.join(root, ".agents/skills/work/SKILL.md"), "# Existing /work\n\nKeep this app rule.\n")
    write(os.path.join(root, ".agents/skills/design/SKILL.md"), "# Existing /design\n")
    write(os.path.join(root, ".agents/skills/handoff/SKILL.md"), "# Existing handoff\n")
    write(os.path.join(root, ".ai-team/skills/handoff/SKILL.md"), "# Existing handoff source\n")
    write(os.path.join(root, ".ai-team/AUTONOMY_POLICY.md"), "# Existing autonomy\n")
    write(os.path.join(root, ".ai-team/runtime/WORKFLOW.md"), "# Existing workflow\n")
    write(os.path.join(root, ".ai-team/runtime/policy.json"), json.dumps({
        "schema_version": "2.0", "runtime": "amplai-loop-v2",
        "public_commands": ["work", "design"],
        "path_rules": [], "forbidden_automatic_actions": [],
    }, indent=2) + "\n")
    settings = {
        "permissions": {"defaultMode": "default", "deny": ["Bash(sudo *)"]},
        "hooks": {
            "SessionStart": [{
                "hooks": [{"type": "command", "command": "echo existing-hook"}]
            }]
        },
        "custom": {"preserve": True},
    }
    write(os.path.join(root, ".claude/settings.json"), json.dumps(settings, indent=2) + "\n")
    write(os.path.join(root, ".gitignore"), "build/\n")


def run_install(target, extra=None, expect=0):
    command = [
        sys.executable, INSTALLER,
        "--target", target,
        "--app-id", "fixture-app",
        "--project-id", "fixture-project",
    ] + list(extra or [])
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    stdout, stderr = proc.communicate()
    if proc.returncode != expect:
        raise AssertionError(
            "installer returncode %s != %s\nstdout=%s\nstderr=%s" %
            (proc.returncode, expect, stdout, stderr)
        )
    return stdout, stderr


def main():
    manifest = read_json(os.path.join(ROOT, "manifest.json"))
    for item in manifest["owned_files"]:
        path = os.path.join(ROOT, "payload", item["path"])
        assert os.path.isfile(path), item["path"]
        assert sha(path) == item["sha256"], item["path"]
    for item in manifest["markers"]:
        path = os.path.join(ROOT, "fragments", item["fragment"])
        assert sha(path) == item["sha256"], item["fragment"]
    for item in manifest.get("json_merges", []):
        path = os.path.join(ROOT, "fragments", item["fragment"])
        assert sha(path) == item["sha256"], item["fragment"]

    temp = tempfile.mkdtemp(prefix="amplai-kit-selftest-")
    try:
        target = os.path.join(temp, "app")
        os.makedirs(target)
        make_target(target)

        first, _ = run_install(target)
        first_report = json.loads(first)
        assert first_report["actions"], "first install had no actions"
        settings = read_json(os.path.join(target, ".claude/settings.json"))
        assert settings["permissions"]["defaultMode"] == "default"
        assert settings["custom"]["preserve"] is True
        commands = []
        for wrappers in settings.get("hooks", {}).values():
            for wrapper in wrappers:
                commands += [hook.get("command") for hook in wrapper.get("hooks", [])]
        assert "echo existing-hook" in commands
        assert any("amplai_hook.py" in (command or "") for command in commands)
        assert "Keep this app rule." in io.open(
            os.path.join(target, ".agents/skills/work/SKILL.md"), encoding="utf-8"
        ).read()

        second, _ = run_install(target)
        second_report = json.loads(second)
        assert second_report["actions"] == [], second_report["actions"]

        runtime_path = os.path.join(target, "scripts/amplai_runtime.py")
        with io.open(runtime_path, "a", encoding="utf-8") as handle:
            handle.write("\n# local modification\n")
        _, error = run_install(target, expect=2)
        assert "local modifications" in error

        forced, _ = run_install(target, extra=["--force"])
        forced_report = json.loads(forced)
        assert forced_report.get("backup_dir")
        assert os.path.isdir(forced_report["backup_dir"])
        assert sha(runtime_path) == next(
            item["sha256"] for item in manifest["owned_files"]
            if item["path"] == "scripts/amplai_runtime.py"
        )

        project_target = os.path.join(temp, "project-app")
        os.makedirs(project_target)
        make_target(project_target)
        project_home = os.path.join(temp, "project-store")
        output, _ = run_install(
            project_target,
            extra=[
                "--project-home", project_home,
                "--runner", "command",
                "--runner-command", sys.executable,
                "--runner-arg=-c",
                "--runner-arg=print('fixture')",
                "--no-git",
            ],
        )
        report = json.loads(output)
        store_report = report["project_store"]
        assert store_report["status"]["project_id"] == "fixture-project"
        assert store_report["policy_drift"] == [], store_report["policy_drift"]
        verify = subprocess.check_output([
            sys.executable, os.path.join(project_target, "scripts/amplai.py"),
            "--project-home", project_home, "project", "verify", "--include-local",
        ], universal_newlines=True)
        verify_report = json.loads(verify)
        assert verify_report["ok"] is True
        enforcement = dict(
            (item["prohibition"], item["machine_enforced"])
            for item in verify_report["policy_enforcement"]
        )
        assert any(enforcement.values()), "no prohibition reported as enforced"
        assert not all(enforcement.values()), "prompt-only prohibitions were mislabelled"

        # B1: a Loop V2 app without AUTONOMY_POLICY.md is still a valid target.
        bare_target = os.path.join(temp, "bare-app")
        os.makedirs(bare_target)
        make_target(bare_target)
        os.unlink(os.path.join(bare_target, ".ai-team/AUTONOMY_POLICY.md"))
        run_install(bare_target)
        created = io.open(
            os.path.join(bare_target, ".ai-team/AUTONOMY_POLICY.md"), encoding="utf-8"
        ).read()
        assert "AMPLAI-ASYNC-BEGIN" in created
        assert "# Autonomy Policy" in created

        # Uninstall must give the app back what it had.
        uninstall_target = os.path.join(temp, "uninstall-app")
        os.makedirs(uninstall_target)
        make_target(uninstall_target)
        run_install(uninstall_target)
        proc = subprocess.Popen(
            [sys.executable, INSTALLER, "--target", uninstall_target, "--uninstall"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
        )
        out, err = proc.communicate()
        assert proc.returncode == 0, err
        assert not os.path.exists(os.path.join(uninstall_target, "scripts/amplai_runtime.py"))
        skill = io.open(
            os.path.join(uninstall_target, ".agents/skills/work/SKILL.md"), encoding="utf-8"
        ).read()
        assert "AMPLAI-ASYNC-BEGIN" not in skill
        assert "Keep this app rule." in skill
        settings = read_json(os.path.join(uninstall_target, ".claude/settings.json"))
        remaining = []
        for wrappers in settings.get("hooks", {}).values():
            for wrapper in wrappers:
                remaining += [hook.get("command") for hook in wrapper.get("hooks", [])]
        assert remaining == ["echo existing-hook"], remaining
        assert settings["custom"]["preserve"] is True

        print(json.dumps({
            "ok": True,
            "version": manifest["version"],
            "checks": [
                "manifest hashes",
                "fresh install",
                "idempotent reinstall",
                "settings/hook preservation",
                "local modification conflict",
                "force backup and replacement",
                "Project Store init and app registration",
                "policy enforcement reporting",
                "install without AUTONOMY_POLICY.md",
                "uninstall restores the app",
            ],
        }, ensure_ascii=False, indent=2))
        return 0
    finally:
        shutil.rmtree(temp)


if __name__ == "__main__":
    sys.exit(main())
