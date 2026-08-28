"""Installer behaviour tests for the vendored AMPLAI Loop Kit.

These run against `tools/amplai-loop-kit/install.py` in this repository and are
skipped when the kit source is not vendored alongside the installed payload.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
KIT_ROOT = os.path.join(REPO_ROOT, "tools", "amplai-loop-kit")
INSTALLER = os.path.join(KIT_ROOT, "install.py")


def write(path, text):
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    with io.open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def read(path):
    with io.open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def read_json(path):
    with io.open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


@unittest.skipUnless(os.path.isfile(INSTALLER), "kit source is not vendored")
class InstallerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.mkdtemp(prefix="amplai-installer-test-")
        self.target = os.path.join(self.temp, "app")
        os.makedirs(self.target)
        self.make_target(with_autonomy=True)

    def tearDown(self):
        shutil.rmtree(self.temp)

    def make_target(self, with_autonomy=True):
        write(os.path.join(self.target, ".agents/skills/work/SKILL.md"),
              "# Existing /work\n\nKeep this app rule.\n")
        write(os.path.join(self.target, ".agents/skills/design/SKILL.md"),
              "# Existing /design\n")
        write(os.path.join(self.target, ".ai-team/runtime/WORKFLOW.md"),
              "# Existing workflow\n")
        write(os.path.join(self.target, ".ai-team/runtime/policy.json"), json.dumps({
            "schema_version": "2.0",
            "runtime": "amplai-loop-v2",
            "public_commands": ["work", "design"],
            "path_rules": [{"id": "app-owned", "patterns": ["src/**"], "risk": "low"}],
            "forbidden_automatic_actions": ["app owned prohibition"],
        }, indent=2) + "\n")
        write(os.path.join(self.target, ".claude/settings.json"), json.dumps({
            "permissions": {"defaultMode": "default", "deny": ["Bash(sudo *)"]},
            "hooks": {"SessionStart": [
                {"hooks": [{"type": "command", "command": "echo existing-hook"}]}
            ]},
            "custom": {"preserve": True},
        }, indent=2) + "\n")
        if with_autonomy:
            write(os.path.join(self.target, ".ai-team/AUTONOMY_POLICY.md"),
                  "# Existing autonomy\n")

    def run_install(self, extra=None, expect=0):
        command = [sys.executable, INSTALLER, "--target", self.target]
        if not (extra and "--uninstall" in extra):
            command += ["--app-id", "fixture-app", "--project-id", "fixture-project"]
        command += list(extra or [])
        proc = subprocess.Popen(command, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, universal_newlines=True)
        stdout, stderr = proc.communicate()
        self.assertEqual(
            proc.returncode, expect,
            "returncode %s != %s\nstdout=%s\nstderr=%s" %
            (proc.returncode, expect, stdout, stderr),
        )
        return stdout, stderr

    # ------------------------------------------------------------------
    def test_fresh_install_then_idempotent_reinstall(self):
        self.run_install()
        self.assertTrue(os.path.isfile(os.path.join(self.target, "scripts/amplai_runtime.py")))
        self.assertIn("AMPLAI-ASYNC-BEGIN",
                      read(os.path.join(self.target, ".agents/skills/work/SKILL.md")))
        stdout, _stderr = self.run_install()
        self.assertEqual(json.loads(stdout)["actions"], [])

    def test_autonomy_policy_is_created_when_missing(self):
        """B1: a Loop V2 app without AUTONOMY_POLICY.md is still a valid target."""
        os.unlink(os.path.join(self.target, ".ai-team/AUTONOMY_POLICY.md"))
        self.run_install()
        created = read(os.path.join(self.target, ".ai-team/AUTONOMY_POLICY.md"))
        self.assertIn("# Autonomy Policy", created)
        self.assertIn("AMPLAI-ASYNC-BEGIN", created)

    def test_existing_content_hooks_and_permissions_are_preserved(self):
        self.run_install()
        skill = read(os.path.join(self.target, ".agents/skills/work/SKILL.md"))
        self.assertIn("Keep this app rule.", skill)

        settings = read_json(os.path.join(self.target, ".claude/settings.json"))
        self.assertEqual(settings["permissions"]["defaultMode"], "default")
        self.assertEqual(settings["custom"], {"preserve": True})
        commands = [
            hook["command"]
            for wrapper in settings["hooks"]["SessionStart"]
            for hook in wrapper["hooks"]
        ]
        self.assertIn("echo existing-hook", commands)
        self.assertTrue(any("amplai_hook.py" in command for command in commands))

        policy = read_json(os.path.join(self.target, ".ai-team/runtime/policy.json"))
        rule_ids = [rule["id"] for rule in policy["path_rules"]]
        self.assertIn("app-owned", rule_ids)
        self.assertIn("amplai-decision-async-runtime", rule_ids)
        self.assertIn("app owned prohibition", policy["forbidden_automatic_actions"])

    def test_policy_merge_preserves_hand_written_formatting(self):
        """The merge appends rules instead of re-dumping the whole document."""
        policy_path = os.path.join(self.target, ".ai-team/runtime/policy.json")
        hand_written = (
            '{\n'
            '  "schema_version": "2.0",\n'
            '  "runtime": "amplai-loop-v2",\n'
            '  "public_commands": ["work", "design"],\n'
            '  "path_rules": [\n'
            '    {\n'
            '      "id": "app-owned",\n'
            '      "patterns": ["src/**", "docs/**"],\n'
            '      "risk": "low"\n'
            '    }\n'
            '  ],\n'
            '  "forbidden_automatic_actions": ["app owned prohibition"]\n'
            '}\n'
        )
        write(policy_path, hand_written)
        self.run_install()

        merged = read(policy_path)
        self.assertIn('"public_commands": ["work", "design"],', merged)
        self.assertIn('"patterns": ["src/**", "docs/**"],', merged)
        self.assertIn("amplai-decision-async-runtime", merged)

        parsed = json.loads(merged)
        self.assertEqual([rule["id"] for rule in parsed["path_rules"]],
                         ["app-owned", "amplai-decision-async-runtime"])
        self.assertEqual(parsed["forbidden_automatic_actions"][0],
                         "app owned prohibition")

    def test_local_modification_of_an_owned_file_conflicts(self):
        self.run_install()
        write(os.path.join(self.target, "scripts/amplai_runtime.py"), "# locally edited\n")
        _stdout, stderr = self.run_install(expect=2)
        self.assertIn("local modifications", stderr)

    def test_force_replaces_and_backs_up(self):
        self.run_install()
        write(os.path.join(self.target, "scripts/amplai_runtime.py"), "# locally edited\n")
        stdout, _stderr = self.run_install(extra=["--force"])
        report = json.loads(stdout)
        backup = os.path.join(report["backup_dir"], "scripts/amplai_runtime.py")
        self.assertEqual(read(backup), "# locally edited\n")
        self.assertIn("AMPLAI", read(os.path.join(self.target, "scripts/amplai_runtime.py")))

    def test_downgrade_is_refused_without_force(self):
        self.run_install()
        state_path = os.path.join(self.target, ".ai-team/install/amplai-loop-kit.json")
        state = read_json(state_path)
        state["package_version"] = "99.0.0"
        state.pop("content_hash", None)
        sys.path.insert(0, os.path.join(self.target, "scripts"))
        from amplai_runtime import seal  # noqa: E402
        write(state_path, json.dumps(seal(state), indent=2, sort_keys=True) + "\n")
        _stdout, stderr = self.run_install(expect=2)
        self.assertIn("newer than this package", stderr)

    def test_uninstall_restores_the_app_and_keeps_foreign_content(self):
        self.run_install()
        self.run_install(extra=["--uninstall"])

        self.assertFalse(os.path.exists(os.path.join(self.target, "scripts/amplai_runtime.py")))
        self.assertFalse(os.path.exists(os.path.join(self.target, ".ai-team/app.json")))
        self.assertFalse(os.path.exists(
            os.path.join(self.target, ".ai-team/install/amplai-loop-kit.json")))

        skill = read(os.path.join(self.target, ".agents/skills/work/SKILL.md"))
        self.assertNotIn("AMPLAI-ASYNC-BEGIN", skill)
        self.assertIn("Keep this app rule.", skill)

        settings = read_json(os.path.join(self.target, ".claude/settings.json"))
        commands = [
            hook["command"]
            for wrapper in settings.get("hooks", {}).get("SessionStart", [])
            for hook in wrapper["hooks"]
        ]
        self.assertEqual(commands, ["echo existing-hook"])
        self.assertEqual(settings["custom"], {"preserve": True})

        policy = read_json(os.path.join(self.target, ".ai-team/runtime/policy.json"))
        self.assertEqual([rule["id"] for rule in policy["path_rules"]], ["app-owned"])
        self.assertEqual(policy["forbidden_automatic_actions"], ["app owned prohibition"])

    def test_uninstall_keeps_a_created_file_the_app_wrote_into(self):
        """Creating a file does not make its later content ours to delete.

        `setUp` pre-creates AUTONOMY_POLICY.md, so the created-marker branch is
        never exercised by the other tests.  This removes it first so the kit
        genuinely creates it, then has the application write below our header.
        """
        policy = os.path.join(self.target, ".ai-team/AUTONOMY_POLICY.md")
        if os.path.exists(policy):
            os.unlink(policy)
        self.run_install()
        self.assertTrue(os.path.exists(policy))

        body = read(policy) + "\n## Application section\n\nowned by the app\n"
        write(policy, body)

        stdout, _stderr = self.run_install(extra=["--uninstall"])
        report = json.loads(stdout)

        self.assertTrue(
            os.path.exists(policy),
            "a file the application wrote into must survive the uninstall",
        )
        remaining = read(policy)
        self.assertIn("owned by the app", remaining)
        self.assertNotIn("AMPLAI-ASYNC-BEGIN", remaining)
        self.assertTrue(
            any("content outside the managed section" in note for note in report["notes"]),
            report["notes"],
        )

    def test_uninstall_removes_a_created_file_that_is_only_our_header(self):
        """The other side of the same judgement: nothing of theirs, so remove it."""
        policy = os.path.join(self.target, ".ai-team/AUTONOMY_POLICY.md")
        if os.path.exists(policy):
            os.unlink(policy)
        self.run_install()
        self.run_install(extra=["--uninstall"])
        self.assertFalse(os.path.exists(policy))

    def test_uninstall_keeps_a_pre_existing_file_it_did_not_create(self):
        """A file the app already had is never removed, only stripped."""
        policy = os.path.join(self.target, ".ai-team/AUTONOMY_POLICY.md")
        write(policy, "# Existing autonomy\n")
        self.run_install()
        self.run_install(extra=["--uninstall"])
        self.assertTrue(os.path.exists(policy))
        self.assertIn("Existing autonomy", read(policy))

    def test_uninstall_leaves_no_directory_it_created(self):
        """An app that counts its own top-level directories must see none of ours.

        Removing only files left `.ai-team/install`, `.ai-team/local` and
        friends behind as empty directories.  A target whose convention pins
        the set of `.ai-team` subdirectories still failed after a "successful"
        uninstall, and `git status` did not show it because git does not track
        empty directories.
        """
        before = sorted(os.listdir(os.path.join(self.target, ".ai-team")))
        self.run_install()
        stdout, _stderr = self.run_install(extra=["--uninstall"])
        report = json.loads(stdout)

        after = sorted(os.listdir(os.path.join(self.target, ".ai-team")))
        created_and_left = [name for name in after if name not in before]
        # backups is deliberate: it holds the copies taken before removal.
        self.assertEqual(created_and_left, ["backups"])

        for rel in (".ai-team/install", ".ai-team/local", ".ai-team/runtime/schemas"):
            self.assertFalse(
                os.path.isdir(os.path.join(self.target, rel)),
                "%s survived the uninstall" % rel,
            )
        self.assertIn(".ai-team/install", report["removed_directories"])

    def test_uninstall_keeps_a_directory_the_app_still_uses(self):
        """Pruning walks upward but must stop at anything not empty."""
        keep = os.path.join(self.target, "scripts", "app_tool.py")
        write(keep, "# owned by the application\n")
        self.run_install()
        self.run_install(extra=["--uninstall"])
        # scripts/ held our files and one of theirs; theirs keeps it alive.
        self.assertTrue(os.path.isdir(os.path.join(self.target, "scripts")))
        self.assertTrue(os.path.exists(keep))

    def test_project_store_is_created_and_registered(self):
        home = os.path.join(self.temp, "store")
        stdout, _stderr = self.run_install(
            extra=["--project-home", home, "--no-git"],
        )
        report = json.loads(stdout)
        self.assertTrue(os.path.isfile(os.path.join(home, "project.json")))
        self.assertEqual(report["project_store"]["policy_drift"], [])
        self.assertIn("fixture-app", report["project_store"]["status"]["next_ready"])

    def test_failed_registration_rolls_back_both_planes(self):
        """M2: a Project Store this run created must not survive a failure."""
        home = os.path.join(self.temp, "store")
        _stdout, stderr = self.run_install(
            extra=["--project-home", home, "--no-git",
                   "--max-concurrency", "0"],
            expect=2,
        )
        self.assertIn("max_concurrency", stderr)
        self.assertFalse(os.path.exists(home), "partial Project Store was left behind")
        self.assertFalse(os.path.exists(os.path.join(self.target, "scripts/amplai_runtime.py")))
        self.assertFalse(os.path.exists(
            os.path.join(self.target, ".ai-team/install/amplai-loop-kit.json")))


if __name__ == "__main__":
    unittest.main()


class StoreSupervisorEntryPointTest(unittest.TestCase):
    """The Store's `run` had no tests at all.

    It decides which application copy of the supervisor to execute and refuses
    a version it cannot vouch for.  Both gates worked when exercised by hand
    and neither was pinned, so a change to either would have passed silently.
    """

    def setUp(self):
        if not os.path.isdir(KIT_ROOT):
            self.skipTest("kit source is not vendored beside the installed payload")
        self.temp = tempfile.mkdtemp(prefix="amplai-store-run-")
        self.home = os.path.join(self.temp, "store")
        self.repo = os.path.join(self.temp, "app")
        os.makedirs(os.path.join(self.home, "supervisor"))
        os.makedirs(os.path.join(self.home, ".amplai", "local", "apps"))
        os.makedirs(os.path.join(self.repo, "scripts"))
        os.makedirs(os.path.join(self.repo, ".ai-team", "install"))

        shutil.copy2(
            os.path.join(KIT_ROOT, "payload", "store", "supervisor", "run"),
            os.path.join(self.home, "supervisor", "run"),
        )
        # A stand-in supervisor: the entry point's job is choosing and checking,
        # not running the real scheduler.
        write(os.path.join(self.repo, "scripts", "amplai_supervisor.py"),
              "import sys\nprint('supervisor ran', sys.argv[1:])\n")
        self.write_json(os.path.join(self.home, "supervisor", "source.json"),
                        {"app_id": "app", "kit_version": "9.9.9"})
        self.write_json(os.path.join(self.home, ".amplai", "local", "apps", "app.json"),
                        {"repo_path": self.repo})
        write(os.path.join(self.home, "supervisor", "VERSION"), "9.9.9\n")
        self.write_json(os.path.join(self.repo, ".ai-team", "install", "amplai-loop-kit.json"),
                        {"package_version": "9.9.9"})

    def tearDown(self):
        shutil.rmtree(self.temp, ignore_errors=True)

    @staticmethod
    def write_json(path, value):
        write(path, json.dumps(value, indent=2) + "\n")

    def run_entry(self):
        env = dict(os.environ, AMPLAI_PROJECT_HOME=self.home)
        return subprocess.run(
            [sys.executable, os.path.join(self.home, "supervisor", "run"), "--dry-run"],
            capture_output=True, text=True, env=env,
        )

    def test_matching_versions_hand_over_to_the_application_copy(self):
        result = self.run_entry()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("supervisor ran", result.stdout)

    def test_a_version_mismatch_is_refused(self):
        self.write_json(os.path.join(self.repo, ".ai-team", "install", "amplai-loop-kit.json"),
                        {"package_version": "0.0.1"})
        result = self.run_entry()
        self.assertEqual(result.returncode, 4)
        self.assertIn("mismatch", result.stderr)

    def test_an_unknown_version_is_refused_rather_than_skipped(self):
        """Not knowing the versions is when running is least safe."""
        for break_it in (
            lambda: write(os.path.join(self.home, "supervisor", "VERSION"), ""),
            lambda: os.unlink(os.path.join(self.home, "supervisor", "VERSION")),
            lambda: os.unlink(
                os.path.join(self.repo, ".ai-team", "install", "amplai-loop-kit.json")),
            lambda: self.write_json(
                os.path.join(self.repo, ".ai-team", "install", "amplai-loop-kit.json"), {}),
        ):
            self.setUp()
            break_it()
            result = self.run_entry()
            self.assertEqual(result.returncode, 4, result.stdout)
            self.assertIn("cannot compare", result.stderr)

    def test_a_missing_source_application_is_refused(self):
        os.unlink(os.path.join(self.home, "supervisor", "source.json"))
        result = self.run_entry()
        self.assertEqual(result.returncode, 2)
        self.assertIn("no source application", result.stderr)

    def test_a_missing_supervisor_script_is_refused(self):
        os.unlink(os.path.join(self.repo, "scripts", "amplai_supervisor.py"))
        result = self.run_entry()
        self.assertEqual(result.returncode, 2)
        self.assertIn("no supervisor script", result.stderr)

    def test_source_app_can_be_overridden_on_the_command_line(self):
        other = os.path.join(self.temp, "other")
        os.makedirs(os.path.join(other, "scripts"))
        os.makedirs(os.path.join(other, ".ai-team", "install"))
        write(os.path.join(other, "scripts", "amplai_supervisor.py"),
              "print('other supervisor')\n")
        self.write_json(os.path.join(other, ".ai-team", "install", "amplai-loop-kit.json"),
                        {"package_version": "9.9.9"})
        self.write_json(os.path.join(self.home, ".amplai", "local", "apps", "other.json"),
                        {"repo_path": other})
        env = dict(os.environ, AMPLAI_PROJECT_HOME=self.home)
        result = subprocess.run(
            [sys.executable, os.path.join(self.home, "supervisor", "run"),
             "--source-app", "other", "--dry-run"],
            capture_output=True, text=True, env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("other supervisor", result.stdout)
