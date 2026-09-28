"""Regression tests for the defects fixed in AMPLAI Loop Kit 2.2.0.

Each test names the 2.1.0 behaviour it prevents from coming back.
"""

import io
import importlib.util
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)
# 이 디렉토리를 직접 넣는다. 앱마다 tests/ai 가 package 이기도 하고
# (cortex 는 __init__.py 가 있다) 아니기도 해서(amplai-foundry) sibling
# import 가 runner 에 따라 갈린다.
HERE = os.path.abspath(os.path.dirname(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from amplai_runtime import (  # noqa: E402
    AtomicDirectoryLock,
    ConflictError,
    LockError,
    ProjectStore,
    ValidationError,
    read_json,
    reseal_object,
    scan_unsealed,
    utc_after,
    utc_now,
    write_json_atomic,
)
from amplai_supervisor import Supervisor  # noqa: E402


def _load_sealed_payload_runtime():
    path = os.path.join(
        REPO_ROOT, "tools", "amplai-loop-kit", "payload", "scripts", "amplai_runtime.py"
    )
    spec = importlib.util.spec_from_file_location("amplai_loop_kit_payload_runtime", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PAYLOAD_RUNTIME = _load_sealed_payload_runtime()


class LockTest(unittest.TestCase):
    """C1: a holder must never delete a lock it no longer owns."""

    def setUp(self):
        self.temp = tempfile.mkdtemp(prefix="amplai-lock-test-")
        self.lock_dir = os.path.join(self.temp, "locks", "project.lock")

    def tearDown(self):
        shutil.rmtree(self.temp)

    def test_release_after_stale_reclaim_keeps_new_owner(self):
        first = AtomicDirectoryLock(self.lock_dir, wait_seconds=1, stale_seconds=0.0)
        first.acquire()
        second = AtomicDirectoryLock(self.lock_dir, wait_seconds=1, stale_seconds=0.0)
        second.acquire()

        with self.assertRaises(LockError):
            first.release()

        self.assertTrue(
            os.path.isdir(self.lock_dir),
            "releasing a lost lock destroyed the new owner's lock",
        )
        third = AtomicDirectoryLock(self.lock_dir, wait_seconds=0.2, stale_seconds=999)
        with self.assertRaises(LockError):
            third.acquire()
        second.release()
        self.assertFalse(os.path.isdir(self.lock_dir))

    def test_context_manager_does_not_mask_body_exception(self):
        lock = AtomicDirectoryLock(self.lock_dir, wait_seconds=1, stale_seconds=0.0)
        with self.assertRaises(ValueError):
            with lock:
                thief = AtomicDirectoryLock(self.lock_dir, wait_seconds=1, stale_seconds=0.0)
                thief.acquire()
                raise ValueError("body failed")
        thief.release()

    def test_normal_acquire_release_cycle(self):
        lock = AtomicDirectoryLock(self.lock_dir, wait_seconds=1, stale_seconds=120)
        with lock:
            self.assertTrue(os.path.isdir(self.lock_dir))
        self.assertFalse(os.path.isdir(self.lock_dir))


class StoreTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.mkdtemp(prefix="amplai-regression-test-")
        self.home = os.path.join(self.temp, "project")
        self.repo = os.path.join(self.temp, "repo")
        os.makedirs(self.repo)
        self.store = ProjectStore.initialize(self.home, "test-project", git_init=False)
        self.store.register_app("cortex", repo_path=self.repo)
        self.store.register_app("synapse", repo_path=self.repo)

    def tearDown(self):
        shutil.rmtree(self.temp)

    def make_change(self, change_id=None):
        change = self.store.create_change(
            "t",
            "g",
            "cortex",
            affected_apps=["cortex", "synapse"],
            change_id=change_id,
        )
        self.store.activate_change(change["change_id"])
        return change["change_id"]

    def make_work(self, cr_id, app="cortex", depends=None, max_attempts=None):
        return self.store.create_work(
            cr_id,
            app,
            "goal",
            depends_on=depends or [],
            acceptance=["verifier passes"],
            max_attempts=max_attempts,
        )


class ChildIdTest(StoreTestBase):
    """C2: a change_id containing -W/-Q/-D/-E addressed the wrong directory."""

    def test_change_id_with_marker_resolves(self):
        cr_id = self.make_change("CR-WIRE-01")
        work = self.make_work(cr_id)
        self.assertEqual(self.store.get_work(work["work_id"])["change_id"], cr_id)

        question = self.store.create_question(work["work_id"], "결정이 필요한가?")
        self.assertEqual(
            self.store.get_question(question["question_id"])["change_id"],
            cr_id,
        )
        evidence = self.store.add_evidence(cr_id, "code", "s", "file", "a.c")
        self.assertEqual(
            self.store.get_evidence(evidence["evidence_id"])["change_id"],
            cr_id,
        )
        self.store.activate_work(work["work_id"])
        self.assertEqual(self.store.get_work(work["work_id"])["status"], "READY")

    def test_longest_matching_prefix_wins(self):
        short = self.make_change("CR-A")
        long_id = self.make_change("CR-A-Wing")
        short_work = self.make_work(short)
        long_work = self.make_work(long_id)
        self.assertEqual(self.store.get_work(short_work["work_id"])["change_id"], "CR-A")
        self.assertEqual(
            self.store.get_work(long_work["work_id"])["change_id"],
            "CR-A-Wing",
        )

    def test_change_id_shaped_like_a_child_id_is_rejected(self):
        for bad in ("CR-0009-W001", "CR-1-Q07", "CR-1-D3", "CR-1-E12"):
            with self.assertRaises(ValidationError):
                self.store.create_change("t", "g", "cortex", change_id=bad)


class EscapeHatchTest(StoreTestBase):
    """C3: an exhausted upstream deadlocked its dependents with no recovery."""

    def _exhaust(self, work):
        self.store.activate_work(work["work_id"])
        _claimed, token = self.store.claim_work(work["work_id"], "w")
        self.store.start_work(work["work_id"], token)
        self.store.fail_work(work["work_id"], token, "boom", retryable=False)

    def test_cancel_requires_cascade_when_dependents_exist(self):
        cr_id = self.make_change()
        upstream = self.make_work(cr_id, max_attempts=1)
        downstream = self.make_work(cr_id, app="synapse", depends=[upstream["work_id"]])
        self.store.activate_work(downstream["work_id"])
        self._exhaust(upstream)

        with self.assertRaises(ConflictError):
            self.store.cancel_work(upstream["work_id"], "abandon")

        result = self.store.cancel_work(upstream["work_id"], "abandon", cascade=True)
        self.assertEqual(
            sorted(result["cancelled"]), sorted([upstream["work_id"], downstream["work_id"]])
        )
        self.assertEqual(self.store.get_work(upstream["work_id"])["status"], "CANCELLED")
        self.assertEqual(self.store.get_work(downstream["work_id"])["status"], "CANCELLED")

    def test_retarget_releases_a_dependent_from_a_dead_upstream(self):
        cr_id = self.make_change()
        dead = self.make_work(cr_id, max_attempts=1)
        alive = self.make_work(cr_id)
        downstream = self.make_work(cr_id, app="synapse", depends=[dead["work_id"]])
        self.store.activate_work(downstream["work_id"])
        self._exhaust(dead)
        self.assertEqual(self.store.get_work(downstream["work_id"])["status"], "WAITING")

        self.store.retarget_work(downstream["work_id"], [alive["work_id"]])
        self.assertEqual(
            self.store.get_work(downstream["work_id"])["depends_on"],
            [alive["work_id"]],
        )
        self.store.cancel_work(dead["work_id"], "superseded")
        self.assertEqual(self.store.get_work(dead["work_id"])["status"], "CANCELLED")

    def test_retarget_still_refuses_a_cycle(self):
        cr_id = self.make_change()
        first = self.make_work(cr_id)
        second = self.make_work(cr_id, depends=[first["work_id"]])
        with self.assertRaises(ValidationError):
            self.store.retarget_work(first["work_id"], [second["work_id"]])

    def test_reset_attempts_reactivates_an_exhausted_work(self):
        cr_id = self.make_change()
        work = self.make_work(cr_id, max_attempts=1)
        self._exhaust(work)
        with self.assertRaises(ConflictError):
            self.store.activate_work(work["work_id"])

        self.store.reset_work_attempts(work["work_id"], max_attempts=3)
        refreshed = self.store.activate_work(work["work_id"])
        self.assertEqual(refreshed["status"], "READY")
        self.assertEqual(refreshed["attempts"], 0)

    def test_cancel_change_cancels_its_open_work(self):
        cr_id = self.make_change()
        first = self.make_work(cr_id)
        second = self.make_work(cr_id, app="synapse", depends=[first["work_id"]])
        self.store.activate_work(first["work_id"])
        change = self.store.cancel_change(cr_id, "descoped")
        self.assertEqual(change["status"], "CANCELLED")
        self.assertEqual(self.store.get_work(first["work_id"])["status"], "CANCELLED")
        self.assertEqual(self.store.get_work(second["work_id"])["status"], "CANCELLED")

    def test_cannot_cancel_completed_work(self):
        cr_id = self.make_change()
        work = self.make_work(cr_id)
        self.store.activate_work(work["work_id"])
        _claimed, token = self.store.claim_work(work["work_id"], "w")
        self.store.start_work(work["work_id"], token)
        evidence = self.store.add_evidence(
            cr_id,
            "test",
            "ran",
            "test",
            "pytest://x",
            work_id=work["work_id"],
        )
        self.store.complete_work(
            work["work_id"],
            token,
            "done",
            [evidence["evidence_id"]],
        )
        with self.assertRaises(ConflictError):
            self.store.cancel_work(work["work_id"], "too late")


class RetryBackoffTest(StoreTestBase):
    """H3: a failing worker was relaunched immediately, three times in a row."""

    def test_retryable_failure_defers_the_next_claim(self):
        cr_id = self.make_change()
        work = self.make_work(cr_id, max_attempts=3)
        self.store.activate_work(work["work_id"])
        _claimed, token = self.store.claim_work(work["work_id"], "w")
        self.store.start_work(work["work_id"], token)
        self.store.fail_work(work["work_id"], token, "boom", retryable=True)

        refreshed = self.store.get_work(work["work_id"])
        self.assertEqual(refreshed["status"], "READY")
        self.assertTrue(refreshed["retry_not_before"])
        self.assertFalse(self.store.retry_ready(refreshed))
        with self.assertRaises(ConflictError):
            self.store.claim_work(work["work_id"], "w")
        self.assertIsNone(self.store.next_ready("cortex"))

    def test_backoff_grows_with_attempts_and_is_capped(self):
        self.assertEqual(self.store._retry_delay_seconds(1), 30)
        self.assertEqual(self.store._retry_delay_seconds(2), 60)
        self.assertEqual(self.store._retry_delay_seconds(3), 120)
        self.assertEqual(self.store._retry_delay_seconds(99), 900)

    def test_expired_backoff_becomes_claimable_again(self):
        cr_id = self.make_change()
        work = self.make_work(cr_id, max_attempts=3)
        self.store.activate_work(work["work_id"])
        _claimed, token = self.store.claim_work(work["work_id"], "w")
        self.store.start_work(work["work_id"], token)
        self.store.fail_work(work["work_id"], token, "boom", retryable=True)

        stored = self.store.get_work(work["work_id"])
        stored["retry_not_before"] = utc_after(-60)
        self.store._write_sealed(self.store.work_path(cr_id, work["work_id"]), stored)
        self.assertTrue(self.store.retry_ready(self.store.get_work(work["work_id"])))
        claimed, _token = self.store.claim_work(work["work_id"], "w")
        self.assertEqual(claimed["status"], "CLAIMED")
        self.assertIsNone(claimed["retry_not_before"])


class ForeignHostTest(StoreTestBase):
    """M3: a missing host-local lease must not recover another host's Work."""

    def test_work_claimed_elsewhere_is_not_recovered(self):
        cr_id = self.make_change()
        work = self.make_work(cr_id)
        self.store.activate_work(work["work_id"])
        _claimed, _token = self.store.claim_work(work["work_id"], "remote-worker")

        stored = self.store.get_work(work["work_id"])
        stored["claimed_host"] = "some-other-host"
        self.store._write_sealed(self.store.work_path(cr_id, work["work_id"]), stored)
        os.unlink(self.store._lease_path(work["work_id"]))

        self.store.reconcile()
        self.assertEqual(self.store.get_work(work["work_id"])["status"], "CLAIMED")

        report = self.store.verify()
        messages = [f["message"] for f in report["findings"]]
        self.assertTrue(any("another host" in m for m in messages), messages)

    def test_local_missing_lease_is_still_recovered(self):
        cr_id = self.make_change()
        work = self.make_work(cr_id)
        self.store.activate_work(work["work_id"])
        self.store.claim_work(work["work_id"], "local-worker")
        self.assertEqual(
            self.store.get_work(work["work_id"])["claimed_host"],
            socket.gethostname(),
        )
        os.unlink(self.store._lease_path(work["work_id"]))
        self.store.reconcile()
        self.assertIn(self.store.get_work(work["work_id"])["status"], ("READY", "WAITING"))


class EventChainTest(StoreTestBase):
    """H4: every append re-parsed the whole event file."""

    def test_chain_stays_valid_and_sequential(self):
        cr_id = self.make_change()
        for index in range(25):
            self.store.add_evidence(cr_id, "code", "e%d" % index, "file", "a.c")
        self.assertTrue(self.store.verify_event_chain())
        last = self.store._last_event()
        with open(self.store.events_path, encoding="utf-8") as handle:
            total = len([line for line in handle if line.strip()])
        self.assertEqual(last["sequence"], total)

    def test_append_cost_does_not_grow_with_history(self):
        cr_id = self.make_change()

        def batch():
            start = time.time()
            for index in range(60):
                self.store.add_evidence(cr_id, "code", "x%d" % index, "file", "a.c")
            return time.time() - start

        # min of two runs each: one scheduler stall on a shared CI runner must not read as
        # growth (main CI run 35563398959 failed 0.146s -> 1.170s once, then passed on rerun).
        first = min(batch(), batch())
        for _ in range(3):
            batch()
        last = min(batch(), batch())
        # 2.1.0 grew roughly linearly per batch; allow generous slack for a
        # noisy machine but catch a return to quadratic behaviour.
        self.assertLess(
            last,
            max(first * 3.0, 0.5),
            "append cost grew with history: %.3fs -> %.3fs" % (first, last),
        )


class ResealTest(StoreTestBase):
    """C3/protocol: the documented repair command must exist and work."""

    def test_reseal_repairs_a_hand_edited_object(self):
        cr_id = self.make_change()
        path = self.store.change_path(cr_id)
        value = read_json(path)
        value["goal"] = "hand edited"
        write_json_atomic(path, value)

        with self.assertRaises(ValidationError):
            self.store.get_change(cr_id)

        self.assertIn("changes/%s/change.json" % cr_id, scan_unsealed(self.home))
        report = reseal_object(self.home, "changes/%s/change.json" % cr_id)
        self.assertTrue(report["changed"])
        self.assertEqual(self.store.get_change(cr_id)["goal"], "hand edited")

    def test_reseal_works_without_a_readable_store(self):
        value = read_json(self.store.policy_path)
        value["supervisor"]["poll_seconds"] = 9
        write_json_atomic(self.store.policy_path, value)
        with self.assertRaises(ValidationError):
            ProjectStore(self.home)
        reseal_object(self.home, "policy.json")
        self.assertEqual(ProjectStore(self.home).policy["supervisor"]["poll_seconds"], 9)

    def test_reseal_refuses_a_foreign_file(self):
        stray = os.path.join(self.home, "contracts", "stray.json")
        write_json_atomic(stray, {"kind": "not-a-store-object"})
        with self.assertRaises(ValidationError):
            reseal_object(self.home, "contracts/stray.json")
        with self.assertRaises(ValidationError):
            reseal_object(self.home, "../escape.json")


class PolicyReportTest(StoreTestBase):
    """M1/M4/M5: policy drift, enforcement honesty, unattended bypass."""

    def test_policy_drift_lists_changed_keys(self):
        desired = dict(self.store.policy)
        desired["work"] = dict(desired["work"], default_max_attempts=9)
        self.assertEqual(self.store.policy_drift(desired), ["work"])
        self.store.set_policy(desired)
        self.assertEqual(ProjectStore(self.home).policy_drift(desired), [])

    def test_enforcement_report_separates_enforced_from_prompt_only(self):
        report = self.store.policy_enforcement_report()
        by_text = dict((item["prohibition"], item["machine_enforced"]) for item in report)
        self.assertTrue(
            by_text["launch work in DRAFT, BLOCKED, HUMAN_REQUIRED, FAILED, DONE, or CANCELLED"]
        )
        self.assertFalse(by_text["invent cross-app goals or affected apps"])
        self.assertFalse(by_text["treat rendered handoff text as the source of truth"])

    def test_registration_rejects_unattended_permission_bypass(self):
        with self.assertRaisesRegex(ValidationError, "unattended permission bypass"):
            self.store.register_app(
                "cortex",
                repo_path=self.repo,
                auto_start=True,
                runner_args=["--dangerously-skip-permissions"],
            )

    def test_registration_rejects_codex_danger_full_access(self):
        with self.assertRaisesRegex(ValidationError, "unattended permission bypass"):
            self.store.register_app(
                "cortex",
                repo_path=self.repo,
                auto_start=True,
                runner_type="codex",
                runner_args=["--sandbox", "danger-full-access"],
            )

    def test_payload_registration_rejects_profile_permission_bypass(self):
        store = PAYLOAD_RUNTIME.ProjectStore.initialize(
            os.path.join(self.temp, "payload-project"), "payload-project", git_init=False
        )
        with self.assertRaisesRegex(
            PAYLOAD_RUNTIME.ValidationError, "runner profile has unattended permission bypass"
        ):
            store.register_app(
                "cortex",
                repo_path=self.repo,
                auto_start=True,
                runner_profiles={
                    "codex": {
                        "command": "codex",
                        "args": ["--dangerously-bypass-approvals-and-sandbox"],
                    }
                },
                default_runner_profile="codex",
            )

    def test_payload_verify_warns_for_resealed_profile_permission_bypass(self):
        store = PAYLOAD_RUNTIME.ProjectStore.initialize(
            os.path.join(self.temp, "payload-verify-project"), "payload-verify-project", git_init=False
        )
        store.register_app(
            "cortex",
            repo_path=self.repo,
            auto_start=True,
            runner_profiles={"codex": {"command": "codex", "args": []}},
            default_runner_profile="codex",
        )
        local_path = store.local_app_path("cortex")
        local = store._read_sealed(local_path, "local_app")
        local["runner_profiles"]["codex"]["args"] = [
            "--dangerously-bypass-approvals-and-sandbox"
        ]
        PAYLOAD_RUNTIME.write_json_atomic(local_path, PAYLOAD_RUNTIME.seal(local), mode=0o600)

        findings = store.verify(include_local=True)["findings"]
        assert any(item["severity"] == "WARNING" for item in findings)


class SupervisorPollingTest(StoreTestBase):
    """H2: the poll loop reconciled the whole store several times a second."""

    def test_worker_run_does_not_spin_on_reconcile(self):
        self.store.register_app(
            "cortex",
            repo_path=self.repo,
            runner_type="command",
            command="/bin/sh",
            runner_args=["-c", "sleep 2"],
            auto_start=True,
        )
        cr_id = self.make_change()
        work = self.make_work(cr_id, max_attempts=1)
        self.store.activate_work(work["work_id"])

        calls = {"reconcile": 0}
        original = ProjectStore.reconcile

        def counting(store_self, *args, **kwargs):
            calls["reconcile"] += 1
            return original(store_self, *args, **kwargs)

        ProjectStore.reconcile = counting
        try:
            Supervisor(self.store).run_until_quiescent(persistent=False)
        finally:
            ProjectStore.reconcile = original

        # 2.1.0 rescanned every 0.25s, so a ~2s worker produced ~10 reconciles.
        self.assertLessEqual(calls["reconcile"], 4, calls)

    def test_dry_run_reports_work_held_by_backoff(self):
        self.store.register_app(
            "cortex",
            repo_path=self.repo,
            runner_type="command",
            command="/bin/sh",
            runner_args=["-c", "true"],
            auto_start=True,
        )
        cr_id = self.make_change()
        work = self.make_work(cr_id, max_attempts=3)
        self.store.activate_work(work["work_id"])
        _claimed, token = self.store.claim_work(work["work_id"], "w")
        self.store.start_work(work["work_id"], token)
        self.store.fail_work(work["work_id"], token, "boom", retryable=True)

        supervisor = Supervisor(self.store)
        self.assertEqual(supervisor.dry_run(), [])
        deferred = supervisor.deferred()
        self.assertEqual([item["work_id"] for item in deferred], [work["work_id"]])

    def test_resealed_unsafe_profile_is_not_claimed_by_supervisor(self):
        self.store.register_app(
            "cortex",
            repo_path=self.repo,
            auto_start=True,
            runner_profiles={"codex": {"command": "codex", "args": []}},
            default_runner_profile="codex",
        )
        local_path = self.store.local_app_path("cortex")
        local = self.store._read_sealed(local_path, "local_app")
        local["runner_profiles"]["codex"]["args"] = [
            "--dangerously-bypass-approvals-and-sandbox"
        ]
        self.store._write_sealed(local_path, local)
        cr_id = self.make_change()
        work = self.make_work(cr_id)
        self.store.activate_work(work["work_id"])

        supervisor = Supervisor(self.store)
        self.assertEqual(supervisor.launchable(), [])
        with self.assertRaisesRegex(ValidationError, "unattended permission bypass"):
            supervisor._claim(self.store.get_work(work["work_id"]))
        self.assertEqual(self.store.get_work(work["work_id"])["status"], "READY")


class HookQuietTest(StoreTestBase):
    """The hook must stay silent in a repository that never joined a store.

    2.1.0 printed "AMPLAI hook skipped: ..." to stderr on every session start,
    which Claude Code surfaces as a failing hook.
    """

    HOOK = os.path.join(SCRIPTS, "amplai_hook.py")

    def _run(self, event, repo, env=None):
        import subprocess

        environment = dict(os.environ)
        environment.pop("AMPLAI_PROJECT_HOME", None)
        environment["CLAUDE_PROJECT_DIR"] = repo
        environment.update(env or {})
        proc = subprocess.Popen(
            [sys.executable, self.HOOK, event],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            env=environment,
        )
        out, err = proc.communicate("{}")
        return proc.returncode, out, err

    def _write_identity(self, repo):
        from amplai_runtime import seal, write_json_atomic

        write_json_atomic(
            os.path.join(repo, ".ai-team", "app.json"),
            seal(
                {
                    "schema_version": "1.0",
                    "kind": "app_identity",
                    "runtime_protocol": "amplai.async-cross-app.v1",
                    "project_id": "test-project",
                    "app_id": "cortex",
                }
            ),
        )

    def test_silent_without_app_identity(self):
        for event in ("session-start", "session-end"):
            code, out, err = self._run(event, self.repo)
            self.assertEqual(code, 0, err)
            self.assertEqual(err.strip(), "", "hook wrote to stderr: %r" % err)
            self.assertEqual(out.strip(), "")

    def test_silent_with_identity_but_no_project_home(self):
        self._write_identity(self.repo)
        for event in ("session-start", "session-end"):
            code, out, err = self._run(event, self.repo)
            self.assertEqual(code, 0, err)
            self.assertEqual(err.strip(), "", "hook wrote to stderr: %r" % err)

    def test_injects_context_when_the_store_is_configured(self):
        self._write_identity(self.repo)
        cr_id = self.make_change()
        work = self.make_work(cr_id, app="cortex")
        self.store.activate_work(work["work_id"])

        code, out, err = self._run(
            "session-start",
            self.repo,
            {"AMPLAI_PROJECT_HOME": self.home},
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(err.strip(), "")
        payload = json.loads(out)["hookSpecificOutput"]
        self.assertIn(work["work_id"], payload["additionalContext"])
        self.assertEqual(payload["hookEventName"], "SessionStart")

    def test_reports_a_store_it_cannot_read(self):
        self._write_identity(self.repo)
        from amplai_runtime import read_json, write_json_atomic

        broken = read_json(self.store.project_path)
        broken["project_id"] = "tampered"
        write_json_atomic(self.store.project_path, broken)

        code, _out, err = self._run(
            "session-start",
            self.repo,
            {"AMPLAI_PROJECT_HOME": self.home},
        )
        self.assertEqual(code, 0)
        self.assertIn("AMPLAI hook skipped", err)


if __name__ == "__main__":
    unittest.main()


class SupervisorSingleInstanceTest(unittest.TestCase):
    """2.3.0: the Store owns the right to run a supervisor.

    Before this, nothing stopped a second supervisor from starting.  The lock
    lives in the Store rather than in an application repository, so it holds
    whichever copy of the script is started -- that is what closes the bypass
    the design had previously accepted as a limitation.
    """

    def setUp(self):
        self.temp = tempfile.mkdtemp(prefix="amplai-supervisor-lock-")
        self.store = ProjectStore.initialize(
            os.path.join(self.temp, "store"),
            "lock-lab",
            git_init=False,
        )

    def tearDown(self):
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_lock_lives_in_the_store_not_the_application(self):
        lock = self.store.supervisor_lock()
        self.assertTrue(lock.lock_dir.startswith(self.store.home))
        self.assertTrue(lock.lock_dir.endswith(os.path.join(".amplai", "locks", "supervisor.lock")))

    def test_second_holder_is_refused_without_waiting(self):
        first = self.store.supervisor_lock()
        first.acquire()
        try:
            second = self.store.supervisor_lock()
            started = time.time()
            with self.assertRaises(LockError):
                second.acquire()
            # A refusal, not a queue: the caller must learn immediately that a
            # supervisor is already running.
            self.assertLess(time.time() - started, 1.0)
        finally:
            first.release()

    def test_refusal_reports_who_holds_it(self):
        first = self.store.supervisor_lock()
        first.acquire()
        try:
            owner = self.store.supervisor_lock().describe_owner()
            self.assertEqual(owner["pid"], os.getpid())
            self.assertEqual(owner["host"], socket.gethostname())
            self.assertIsNotNone(owner["heartbeat_at"])
        finally:
            first.release()

    def test_heartbeat_keeps_a_long_run_from_being_reclaimed(self):
        lock = self.store.supervisor_lock()
        lock.acquire()
        try:
            # Age the marker instead of sleeping: utc_now() has one-second
            # resolution, so a sub-second stale window cannot be tested by
            # waiting without making the test slow and flaky.
            aged = read_json(lock.owner_path)
            aged["heartbeat_at"] = "2000-01-01T00:00:00Z"
            aged["created_at"] = "2000-01-01T00:00:00Z"
            write_json_atomic(lock.owner_path, aged)
            self.assertTrue(lock._stale())

            # The state a running supervisor is actually in: started long ago,
            # heartbeat recent.  Ageing both fields cannot tell whether
            # heartbeat_at is preferred, so pin that separately.
            live = read_json(lock.owner_path)
            live["created_at"] = "2000-01-01T00:00:00Z"
            live["heartbeat_at"] = utc_now()
            write_json_atomic(lock.owner_path, live)
            self.assertFalse(
                lock._stale(),
                "a long-running holder with a fresh heartbeat must not look stale",
            )

            # And the reverse: a fresh start with no heartbeat since is stale
            # once the window passes, so created_at is still the fallback.
            fallback = read_json(lock.owner_path)
            fallback["created_at"] = "2000-01-01T00:00:00Z"
            del fallback["heartbeat_at"]
            write_json_atomic(lock.owner_path, fallback)
            self.assertTrue(lock._stale())

            write_json_atomic(lock.owner_path, aged)
            self.assertTrue(lock._stale())

            self.assertTrue(lock.heartbeat())
            # After a heartbeat the same lock is live again, so a contender
            # that would have reclaimed it now loses.
            self.assertFalse(lock._stale())
            refreshed = read_json(lock.owner_path)
            self.assertNotEqual(refreshed["heartbeat_at"], "2000-01-01T00:00:00Z")
            # created_at is preserved so the record still shows when the run
            # actually started.
            self.assertEqual(refreshed["created_at"], lock.created_at)
        finally:
            lock.release()

    def test_heartbeat_refuses_after_the_lock_was_reclaimed(self):
        first = AtomicDirectoryLock(
            os.path.join(self.temp, "locks", "supervisor.lock"),
            wait_seconds=0,
            stale_seconds=0.0,
        )
        first.acquire()
        second = AtomicDirectoryLock(
            os.path.join(self.temp, "locks", "supervisor.lock"),
            wait_seconds=1,
            stale_seconds=0.0,
        )
        second.acquire()
        try:
            # The first holder lost the lock to a stale reclaim.  Writing a
            # heartbeat here would resurrect a lock it no longer owns.
            self.assertFalse(first.heartbeat())
            self.assertEqual(second._owner_token(), second.token)
        finally:
            second.release()

    def test_stale_lock_is_reclaimed_by_the_next_supervisor(self):
        first = self.store.supervisor_lock()
        first.acquire()
        owner_path = first.owner_path
        stale = read_json(owner_path)
        stale["heartbeat_at"] = "2000-01-01T00:00:00Z"
        stale["created_at"] = "2000-01-01T00:00:00Z"
        write_json_atomic(owner_path, stale)
        second = self.store.supervisor_lock()
        second.acquire()
        try:
            self.assertEqual(second._owner_token(), second.token)
        finally:
            second.release()

    def test_supervisor_passes_its_heartbeat_into_the_scan_loop(self):
        calls = []
        supervisor = Supervisor(self.store, heartbeat=lambda: calls.append(1))
        supervisor.run_until_quiescent(persistent=False)
        # An empty store still completes one scan, and that scan must have
        # refreshed the lock.
        self.assertGreaterEqual(len(calls), 1)


class SupervisorLockRaceTest(unittest.TestCase):
    """2.3.0 review: the lock must not let two supervisors live.

    A first pass made the single-instance lock hold for the simple cases and
    still lose under contention -- a heartbeat whose failure was discarded, a
    stale reclaim that could take a lock created moments earlier, and a raw
    OSError escaping acquire().
    """

    def setUp(self):
        self.temp = tempfile.mkdtemp(prefix="amplai-lock-race-")
        self.lock_dir = os.path.join(self.temp, "locks", "supervisor.lock")

    def tearDown(self):
        shutil.rmtree(self.temp, ignore_errors=True)

    def _lock(self, wait=0, stale=300):
        return AtomicDirectoryLock(self.lock_dir, wait_seconds=wait, stale_seconds=stale)

    def test_reclaim_does_not_steal_a_lock_created_after_the_judgement(self):
        """The window between "this is stale" and the rename must be closed."""
        victim = self._lock(stale=0.0)
        victim.acquire()
        stale, token = victim._stale_owner()
        self.assertTrue(stale)

        # The judged holder releases and a fresh one takes the slot before the
        # reclaim lands.  Reclaiming on the old judgement would evict a live
        # owner.
        victim.release()
        fresh = self._lock()
        fresh.acquire()
        try:
            contender = self._lock()
            self.assertFalse(contender._reclaim_stale(token))
            self.assertTrue(os.path.isdir(self.lock_dir))
            self.assertEqual(fresh._owner_token(), fresh.token)
        finally:
            fresh.release()

    def test_reclaim_without_a_token_still_works(self):
        """A lock with no readable owner file has no token to pin."""
        holder = self._lock(stale=0.0)
        holder.acquire()
        os.unlink(holder.owner_path)
        self.assertTrue(self._lock()._reclaim_stale(None))
        self.assertFalse(os.path.isdir(self.lock_dir))

    def test_contention_raises_lock_error_not_a_bare_os_error(self):
        """Callers separate "someone else has it" from "the Store is broken"."""
        blocker = self._lock()
        blocker.acquire()
        try:
            with self.assertRaises(LockError) as caught:
                self._lock().acquire()
            # Reaching the deadline is the ordinary path; this test exists for
            # the other one, so name which branch answered.
            self.assertIn("timed out", str(caught.exception))
        finally:
            blocker.release()

    def test_an_os_error_while_contending_becomes_a_lock_error(self):
        """The reclaim path must not leak a raw OSError to the caller.

        A contender removing the directory underneath us is a lost race, not a
        broken Store.  Before this, FileExistsError escaped acquire() and the
        caller could not tell the two apart.
        """
        blocker = self._lock(stale=0.0)
        blocker.acquire()
        try:
            # The contender must judge the holder stale, or reclaim is never
            # reached and the deadline answers instead.
            contender = self._lock(wait=1, stale=0.0)

            def explode(_expected_token=None):
                raise OSError(17, "File exists")

            contender._reclaim_stale = explode
            with self.assertRaises(LockError) as caught:
                contender.acquire()
            self.assertIn("failed while contending", str(caught.exception))
        finally:
            try:
                blocker.release()
            except LockError:
                pass

    def test_concurrent_acquire_yields_exactly_one_holder(self):
        held = []
        errors = []

        def attempt():
            # The real stale window is 300s.  Setting it below one second makes
            # every holder look stale to every other -- utc_now() has one-second
            # resolution -- so the test would be measuring reclaim, not mutual
            # exclusion.
            lock = self._lock(wait=0.5, stale=300)
            try:
                lock.acquire()
            except LockError:
                return
            except Exception as exc:  # noqa: BLE001 - the point of the test
                errors.append(exc)
                return
            held.append(lock)

        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        try:
            # A bare OSError escaping acquire() is a failure even if only one
            # thread ended up holding the lock.
            self.assertEqual(errors, [])
            self.assertEqual(len(held), 1)
        finally:
            for lock in held:
                try:
                    lock.release()
                except LockError:
                    pass


class SupervisorStopsWhenItLosesTheLockTest(unittest.TestCase):
    """A supervisor whose heartbeat fails must stop, not keep scanning."""

    def setUp(self):
        self.temp = tempfile.mkdtemp(prefix="amplai-lost-lock-")
        self.store = ProjectStore.initialize(
            os.path.join(self.temp, "store"),
            "lost-lock",
            git_init=False,
        )

    def tearDown(self):
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_a_failed_heartbeat_stops_the_scan_loop(self):
        calls = []

        def heartbeat():
            calls.append(1)
            return False

        supervisor = Supervisor(self.store, heartbeat=heartbeat)
        # persistent=False so that a regression here fails the assertion
        # instead of spinning forever: ignoring the heartbeat result under
        # persistent=True is an endless loop, and a hanging suite is a worse
        # signal than a failing one.
        with self.assertRaises(LockError):
            supervisor.run_until_quiescent(persistent=False)
        self.assertEqual(len(calls), 1)

    def test_a_healthy_heartbeat_does_not_stop_it(self):
        supervisor = Supervisor(self.store, heartbeat=lambda: True)
        supervisor.run_until_quiescent(persistent=False)


class HookTestIsolationTest(unittest.TestCase):
    """2.3.1: kit 이 설치된 세션에서 자기 test 가 실제 Store 를 읽던 것.

    SessionStart hook 이 AMPLAI_PROJECT_HOME 을 세션에 export 하고
    (amplai_hook.py append_env), discover_project_home() 은 그 변수를
    repo-local binding 보다 먼저 본다 (amplai_runtime.py). 그래서 kit 이
    설치된 저장소의 Claude 세션에서 test 를 돌리면 AmplaiHookTest 가 임시
    Store 대신 실제 Store 를 읽어 실패했다. verifier 의 block check 하나가
    통째로 FAIL 이 되는 경로였고, 사람이 맨 터미널에서 돌리면 통과해서
    한동안 안 보였다.

    hook 쪽 동작은 의도된 것이므로 고치지 않는다. 격리는 test 의 책임이다.
    """

    def setUp(self):
        self.saved = {
            name: value for name, value in os.environ.items() if name.startswith("AMPLAI_")
        }

    def tearDown(self):
        for name in [n for n in os.environ if n.startswith("AMPLAI_")]:
            del os.environ[name]
        os.environ.update(self.saved)

    def test_hook_test_passes_while_a_real_store_is_named_in_the_environment(self):
        """AMPLAI_PROJECT_HOME 이 다른 Store 를 가리켜도 hook test 가 통과한다."""
        from test_amplai_async_runtime import AmplaiHookTest

        decoy_root = tempfile.mkdtemp(prefix="amplai-decoy-store-")
        self.addCleanup(shutil.rmtree, decoy_root, True)
        decoy = ProjectStore.initialize(
            os.path.join(decoy_root, "store"),
            "decoy-project",
            git_init=False,
        )
        os.environ["AMPLAI_PROJECT_HOME"] = decoy.home
        os.environ["AMPLAI_APP_ID"] = "decoy-app"

        name = "test_session_hooks_inject_and_checkpoint_without_owning_work_state"
        result = unittest.TextTestRunner(
            stream=io.StringIO(),
            verbosity=0,
        ).run(unittest.TestSuite([AmplaiHookTest(name)]))

        self.assertEqual([], result.errors, result.errors)
        self.assertEqual([], result.failures, result.failures)

    def test_the_decoy_store_is_what_would_have_broken_it(self):
        """격리가 없으면 그 변수가 실제로 Store 결정을 이긴다는 것을 고정한다."""
        from amplai_runtime import discover_project_home

        decoy_root = tempfile.mkdtemp(prefix="amplai-decoy-precedence-")
        self.addCleanup(shutil.rmtree, decoy_root, True)
        repo = os.path.join(decoy_root, "app")
        os.makedirs(repo)
        os.environ["AMPLAI_PROJECT_HOME"] = os.path.join(decoy_root, "elsewhere")

        self.assertEqual(
            os.path.join(decoy_root, "elsewhere"),
            discover_project_home(repo),
        )
