import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from amplai_runtime import (  # noqa: E402
    ConflictError, ProjectStore, ValidationError, read_json, seal,
    write_json_atomic,
)
from amplai_supervisor import (  # noqa: E402
    Supervisor, WorkerRunner, session_id_from_output,
)


class AmplaiRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.mkdtemp(prefix="amplai-runtime-test-")
        self.project_home = os.path.join(self.temp, "project")
        self.cortex_repo = os.path.join(self.temp, "cortex")
        self.synapse_repo = os.path.join(self.temp, "synapse")
        os.makedirs(self.cortex_repo)
        os.makedirs(self.synapse_repo)
        self.store = ProjectStore.initialize(
            self.project_home, "test-project", git_init=False,
        )
        self.store.register_app("cortex", repo_path=self.cortex_repo)
        self.store.register_app("synapse", repo_path=self.synapse_repo)
        self.change = self.store.create_change(
            "Cross app change", "Coordinate Cortex and Synapse", "cortex",
            affected_apps=["cortex", "synapse"], change_id="CR-0001",
        )
        self.store.activate_change(self.change["change_id"])

    def tearDown(self):
        shutil.rmtree(self.temp)

    def make_work(self, app="cortex", depends=None, goal="Implement change"):
        work = self.store.create_work(
            self.change["change_id"], app, goal,
            depends_on=depends or [], acceptance=["deterministic verifier passes"],
        )
        return work

    def activate_claim_start(self, work):
        self.store.activate_work(work["work_id"])
        claimed, token = self.store.claim_work(work["work_id"], "test-worker")
        self.store.start_work(work["work_id"], token)
        return claimed, token

    def add_evidence(self, work, evidence_type="test", metadata=None):
        return self.store.add_evidence(
            self.change["change_id"], evidence_type,
            "verified evidence", "test", "fixture://evidence",
            work_id=work["work_id"], app_id=work["target_app"],
            facts=["fixture passed"], metadata=metadata or {},
        )

    def test_content_hash_tampering_is_rejected(self):
        work = self.make_work()
        path = self.store.work_path(self.change["change_id"], work["work_id"])
        value = read_json(path)
        value["goal"] = "tampered without resealing"
        write_json_atomic(path, value)
        with self.assertRaises(ValidationError):
            self.store.get_work(work["work_id"])

    def test_request_ref_creates_one_pinned_work(self):
        base_ref = "a" * 40
        first = self.store.create_work(
            self.change["change_id"], "cortex", "Bridge request",
            acceptance=["one draft work"], controller="work",
            runner_profile="codex", base_ref=base_ref, request_ref="REQ-001",
        )
        second = self.store.create_work(
            self.change["change_id"], "cortex", "Bridge retry",
            acceptance=["one draft work"], controller="work",
            runner_profile="codex", base_ref=base_ref, request_ref="REQ-001",
        )
        self.assertEqual(first["work_id"], second["work_id"])
        self.assertEqual(first["controller"], "work")
        self.assertEqual(first["runner_profile"], "codex")
        self.assertEqual(first["base_ref"], base_ref)

    def test_auto_decision_requires_evidence(self):
        work = self.make_work()
        question = self.store.create_question(
            work["work_id"], "Use an existing parser?",
            decision_class="engineering", reversibility="high", blast_radius="low",
        )
        self.assertEqual(question["required_authority"], "AUTO")
        with self.assertRaises(ValidationError):
            self.store.record_decision(
                question["question_id"], "Use existing parser", "Repository convention", [],
            )
        evidence = self.add_evidence(work, "code")
        decision = self.store.record_decision(
            question["question_id"], "Use existing parser", "Repository convention",
            [evidence["evidence_id"]],
        )
        self.assertEqual(decision["authority"], "AUTO")
        self.assertEqual(self.store.get_question(question["question_id"])["status"], "RESOLVED")

    def test_challenge_requires_independent_accept_review(self):
        work = self.make_work()
        question = self.store.create_question(
            work["work_id"], "Change persistence boundary?",
            decision_class="persistence", reversibility="low", blast_radius="high",
        )
        evidence = self.add_evidence(work, "code")
        ordinary_review = self.add_evidence(
            work, "review", {"independent": False, "verdict": "ACCEPT"},
        )
        with self.assertRaises(ValidationError):
            self.store.record_decision(
                question["question_id"], "Keep current boundary", "Lower migration risk",
                [evidence["evidence_id"]], authority="CHALLENGE",
                review_evidence_refs=[ordinary_review["evidence_id"]],
            )
        independent = self.add_evidence(
            work, "review", {"independent": True, "verdict": "ACCEPT"},
        )
        decision = self.store.record_decision(
            question["question_id"], "Keep current boundary", "Lower migration risk",
            [evidence["evidence_id"]], authority="CHALLENGE",
            review_evidence_refs=[independent["evidence_id"]],
        )
        self.assertEqual(decision["authority"], "CHALLENGE")

    def test_human_decision_requires_approval(self):
        work = self.make_work()
        question = self.store.create_question(
            work["work_id"], "Change product behavior?", decision_class="product",
            reversibility="medium", blast_radius="high",
        )
        evidence = self.add_evidence(work, "documentation")
        with self.assertRaises(ValidationError):
            self.store.record_decision(
                question["question_id"], "Keep behavior", "Owner not recorded",
                [evidence["evidence_id"]], authority="HUMAN",
            )
        approval = self.add_evidence(
            work, "human_approval", {"approved_by": "product-owner"},
        )
        decision = self.store.record_decision(
            question["question_id"], "Keep behavior", "Owner approved compatibility",
            [evidence["evidence_id"]], authority="HUMAN",
            approval_evidence_refs=[approval["evidence_id"]],
        )
        self.assertEqual(decision["authority"], "HUMAN")

    def test_draft_is_not_launchable_and_activation_respects_dependency(self):
        upstream = self.make_work("cortex", goal="Produce contract")
        downstream = self.make_work("synapse", [upstream["work_id"]], "Consume contract")
        self.assertEqual(self.store.get_work(downstream["work_id"])["status"], "DRAFT")
        self.assertIsNone(self.store.next_ready("synapse"))
        activated = self.store.activate_work(downstream["work_id"])
        self.assertEqual(activated["status"], "WAITING")
        _, token = self.activate_claim_start(upstream)
        evidence = self.add_evidence(upstream)
        self.store.complete_work(
            upstream["work_id"], token, "Contract produced", [evidence["evidence_id"]],
        )
        self.assertEqual(self.store.get_work(downstream["work_id"])["status"], "READY")

    def test_dependency_cycle_is_rejected(self):
        first = self.make_work("cortex", goal="First")
        second = self.make_work("synapse", [first["work_id"]], "Second")
        self.store.activate_work(first["work_id"])
        self.store.activate_work(second["work_id"])
        claimed, token = self.store.claim_work(first["work_id"], "cycle-worker")
        self.store.start_work(claimed["work_id"], token)
        with self.assertRaises(ValidationError):
            self.store.wait_work(
                first["work_id"], token, [second["work_id"]], "cycle",
            )

    def test_app_concurrency_is_atomic(self):
        first = self.make_work("cortex", goal="First")
        second = self.make_work("cortex", goal="Second")
        self.store.activate_work(first["work_id"])
        self.store.activate_work(second["work_id"])
        self.store.claim_work(first["work_id"], "worker-one")
        with self.assertRaises(ConflictError):
            self.store.claim_work(second["work_id"], "worker-two")

    def test_expired_lease_recovers_to_ready(self):
        work = self.make_work()
        self.store.activate_work(work["work_id"])
        claimed, _ = self.store.claim_work(work["work_id"], "expired-worker", lease_seconds=10)
        lease_path = self.store._lease_path(claimed["work_id"])
        lease = read_json(lease_path)
        lease["expires_at"] = "2000-01-01T00:00:00Z"
        write_json_atomic(lease_path, seal(lease), mode=0o600)
        self.store.reconcile()
        recovered = self.store.get_work(work["work_id"])
        self.assertEqual(recovered["status"], "READY")
        self.assertIn("lease expired", recovered["last_error"])

    def test_open_question_blocks_done(self):
        work = self.make_work()
        _, token = self.activate_claim_start(work)
        self.store.create_question(work["work_id"], "Unresolved engineering question")
        evidence = self.add_evidence(work)
        with self.assertRaises(ConflictError):
            self.store.complete_work(
                work["work_id"], token, "Should not close", [evidence["evidence_id"]],
            )

    def test_human_required_does_not_block_independent_work(self):
        human_work = self.make_work("cortex", goal="Owner choice")
        independent = self.make_work("synapse", goal="Independent implementation")
        _, token = self.activate_claim_start(human_work)
        question = self.store.create_question(
            human_work["work_id"], "Select product behavior", decision_class="product",
        )
        self.store.require_human(
            human_work["work_id"], token, "product_owner", "Need owner choice",
            question_refs=[question["question_id"]],
        )
        self.store.activate_work(independent["work_id"])
        self.assertEqual(self.store.next_ready("synapse")["work_id"], independent["work_id"])
        with self.assertRaises(ConflictError):
            self.store.activate_work(human_work["work_id"])

    def test_human_question_cannot_be_deferred_or_gate_without_question(self):
        work = self.make_work("cortex", goal="Owner-gated change")
        _, token = self.activate_claim_start(work)
        question = self.store.create_question(
            work["work_id"], "Owner must choose", decision_class="product",
        )
        with self.assertRaises(ConflictError):
            self.store.defer_question(question["question_id"], "later")
        with self.assertRaises(ValidationError):
            self.store.require_human(
                work["work_id"], token, "owner", "No question reference", question_refs=[],
            )

    def test_work_cannot_reference_decision_or_evidence_from_another_change(self):
        first_work = self.make_work()
        evidence = self.add_evidence(first_work)
        question = self.store.create_question(first_work["work_id"], "Local choice")
        decision = self.store.record_decision(
            question["question_id"], "Choose A", "Evidence supports A",
            [evidence["evidence_id"]],
        )
        other = self.store.create_change(
            "Other", "Separate change", "cortex", change_id="CR-0002",
        )
        with self.assertRaises(ValidationError):
            self.store.create_work(
                other["change_id"], "cortex", "Invalid reference",
                acceptance=["must reject"], decision_refs=[decision["decision_id"]],
            )
        with self.assertRaises(ValidationError):
            self.store.create_work(
                other["change_id"], "cortex", "Invalid evidence",
                acceptance=["must reject"], input_evidence_refs=[evidence["evidence_id"]],
            )

    def test_handoff_is_generated_projection(self):
        work = self.make_work("synapse")
        rendered = self.store.render_handoff(work["work_id"])
        self.assertIn(work["work_id"], rendered)
        self.assertIn("Assigned Work", rendered)
        self.assertFalse(os.path.exists(os.path.join(self.store.change_dir("CR-0001"), "handoffs")))

    def _store_bytes(self):
        result = {}
        for parent, _, names in os.walk(self.project_home):
            for name in names:
                path = os.path.join(parent, name)
                with open(path, "rb") as stream:
                    result[os.path.relpath(path, self.project_home)] = stream.read()
        return result

    def test_done_handoff_shows_own_result_without_restarting_work(self):
        work = self.make_work("synapse")
        _, token = self.activate_claim_start(work)
        evidence = self.add_evidence(work)
        question = self.store.create_question(work["work_id"], "Keep existing contract?")
        decision = self.store.record_decision(
            question["question_id"], "Keep existing contract", "Fixture evidence",
            [evidence["evidence_id"]],
        )
        done = self.store.complete_work(
            work["work_id"], token, "Fixture contract delivered", [evidence["evidence_id"]],
            decision_refs=[decision["decision_id"]], outputs=["fixture://contract-v1"],
        )
        before = self._store_bytes()
        context = self.store.build_work_context(work["work_id"])
        rendered = self.store.render_handoff(work["work_id"])
        self.assertEqual(self._store_bytes(), before)
        self.assertTrue(context["instructions"]["read_only"])
        self.assertIsNone(context["instructions"]["entry_point"])
        self.assertEqual(context["work"]["result"], done["result"])
        for value in (
            "Fixture contract delivered", "fixture://contract-v1", evidence["evidence_id"],
            decision["decision_id"], done["result"]["completed_at"], "## Recorded Result",
            "read-only", "Do not claim or restart this Work",
        ):
            self.assertIn(value, rendered)
        self.assertNotIn("## Resume", rendered)
        self.assertNotIn("update the same Work object", rendered)

    def test_cancelled_handoff_is_not_success_or_resume(self):
        work = self.make_work("synapse")
        self.store.cancel_work(work["work_id"], "Fixture request withdrawn")
        before = self._store_bytes()
        context = self.store.build_work_context(work["work_id"])
        rendered = self.store.render_handoff(work["work_id"])
        self.assertEqual(self._store_bytes(), before)
        self.assertTrue(context["instructions"]["read_only"])
        self.assertIsNone(context["instructions"]["entry_point"])
        self.assertIsNone(context["work"]["result"])
        self.assertIn("CANCELLED", rendered)
        self.assertIn("not a successful completion", rendered)
        self.assertNotIn("## Resume", rendered)
        self.assertNotIn("## Recorded Result", rendered)

    def test_nonterminal_work_keeps_existing_controller_entry(self):
        work = self.make_work("synapse")
        self.store.activate_work(work["work_id"])
        for start in (False, True):
            if start:
                _, token = self.store.claim_work(work["work_id"], "fixture-worker")
                self.store.start_work(work["work_id"], token)
            before = self._store_bytes()
            context = self.store.build_work_context(work["work_id"])
            rendered = self.store.render_handoff(work["work_id"])
            self.assertEqual(self._store_bytes(), before)
            self.assertFalse(context["instructions"]["read_only"])
            self.assertEqual(context["instructions"]["entry_point"], "/work")
            self.assertIn("## Resume", rendered)

    def test_handoff_dependency_chat_only_done_and_expired_claim_recovery(self):
        from datetime import timedelta
        from unittest.mock import patch

        from amplai_runtime import parse_time

        upstream = self.make_work("cortex", goal="Produce fixture contract")
        downstream = self.make_work("synapse", [upstream["work_id"]], "Use fixture contract")
        self.store.activate_work(downstream["work_id"])
        self.assertIn("WAITING", self.store.render_handoff(downstream["work_id"]))
        # A rendered/chat artifact is not a second Work store or transition command.
        with open(os.path.join(self.synapse_repo, "handoff.md"), "w") as stream:
            stream.write("# " + upstream["work_id"] + "\nDONE: chat-only claim\n")
        self.store.reconcile()
        self.assertEqual(self.store.get_work(downstream["work_id"])["status"], "WAITING")
        with self.assertRaises(ConflictError):
            self.store.claim_work(downstream["work_id"], "cannot-claim-waiting")
        _, upstream_token = self.activate_claim_start(upstream)
        evidence = self.add_evidence(upstream)
        self.store.complete_work(
            upstream["work_id"], upstream_token, "Contract actually recorded",
            [evidence["evidence_id"]], outputs=["fixture://dependency-contract"],
        )
        self.assertEqual(self.store.get_work(downstream["work_id"])["status"], "READY")
        context = self.store.build_work_context(downstream["work_id"])
        self.assertEqual(context["dependencies"][0]["result"]["summary"], "Contract actually recorded")
        _, expired_token = self.store.claim_work(downstream["work_id"], "expired-worker")
        lease_path = self.store._lease_path(downstream["work_id"])
        lease = read_json(lease_path)
        lease["expires_at"] = "2000-01-01T00:00:00Z"
        write_json_atomic(lease_path, seal(lease), mode=0o600)
        self.store.reconcile()
        self.assertEqual(self.store.get_work(downstream["work_id"])["status"], "READY")
        recovered = self.store.get_work(downstream["work_id"])
        with self.assertRaisesRegex(ConflictError, "retry backoff"):
            self.store.claim_work(downstream["work_id"], "too-early-worker")
        # Advance only the isolated clock; preserve the production backoff policy.
        ready_time = parse_time(recovered["retry_not_before"]) + timedelta(seconds=1)
        with patch("amplai_runtime.utc_naive_now", return_value=ready_time):
            _, token = self.store.claim_work(downstream["work_id"], "replacement-worker")
        self.assertNotEqual(token, expired_token)
        self.store.start_work(downstream["work_id"], token)
        evidence = self.add_evidence(downstream)
        self.store.complete_work(
            downstream["work_id"], token, "Downstream delivered", [evidence["evidence_id"]],
        )
        before = self._store_bytes()
        rendered = self.store.render_handoff(downstream["work_id"])
        self.assertEqual(self._store_bytes(), before)
        self.assertIn("Downstream delivered", rendered)
        self.assertIn("Contract actually recorded", rendered)
        self.assertNotIn("## Resume", rendered)
        self.assertFalse(os.path.exists(os.path.join(self.store.change_dir("CR-0001"), "handoffs")))
        with self.assertRaises(ConflictError):
            self.store.claim_work(downstream["work_id"], "cannot-reclaim-done")
        with self.assertRaises(ConflictError):
            self.store.activate_work(downstream["work_id"])

    def test_heartbeat_cli_redacts_json_and_default_without_changing_auth(self):
        for as_json in (False, True):
            work = self.make_work("synapse")
            _, token = self.activate_claim_start(work)
            lease_path = self.store._lease_path(work["work_id"])
            lease = read_json(lease_path)
            lease["heartbeat_at"] = "2000-01-01T00:00:00Z"
            lease["expires_at"] = "2099-01-01T00:00:00Z"
            write_json_atomic(lease_path, seal(lease), mode=0o600)
            command = [sys.executable, "-B", os.path.join(SCRIPTS, "amplai.py"),
                       "--project-home", self.project_home]
            if as_json:
                command.append("--json")
            command += ["work", "heartbeat", "--id", work["work_id"]]
            env = dict(os.environ, AMPLAI_LEASE_TOKEN=token)
            result = subprocess.run(command, env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, universal_newlines=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn(token, result.stdout + result.stderr)
            view = json.loads(result.stdout)
            self.assertEqual(view["token"], "<redacted>")
            self.assertEqual(view["kind"], "lease_view")
            self.assertTrue(view["view_only"])
            self.assertNotIn("content_hash", view)
            stored = self.store._read_lease(work["work_id"])
            self.assertEqual(stored["token"], token)
            self.assertEqual(stored["kind"], "lease")
            self.assertNotEqual(stored["expires_at"], lease["expires_at"])
            self.assertNotEqual(stored["heartbeat_at"], lease["heartbeat_at"])
            self.assertEqual(view["expires_at"], stored["expires_at"])
            self.assertEqual(os.stat(lease_path).st_mode & 0o777, 0o600)
            evidence = self.add_evidence(work)
            self.assertEqual(self.store.complete_work(
                work["work_id"], token, "Same-token completion", [evidence["evidence_id"]],
            )["status"], "DONE")

    def test_heartbeat_output_is_detached_allowlisted_view(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        import amplai

        lease = {"schema_version": "1.0", "kind": "lease", "token": "fixture-credential",
                 "content_hash": "stale", "work_id": "W-fixture",
                 "future_private_field": "not-output", "expires_at": "2099-01-01T00:00:00Z"}
        before = dict(lease)
        with patch.object(amplai, "store_from_args") as store, patch.object(amplai, "lease_token"):
            store.return_value.heartbeat.return_value = lease
            view = amplai.cmd_work(SimpleNamespace(work_action="heartbeat", id="W-fixture", actor="test"))
        self.assertEqual(lease, before)
        self.assertIsNot(view, lease)
        self.assertNotIn("future_private_field", view)
        self.assertNotIn("content_hash", view)
        self.assertEqual(view["token"], "<redacted>")

    def test_claude_command_masks_prompt_and_can_resume(self):
        work = self.make_work("cortex")
        self.store.activate_work(work["work_id"])
        self.store.register_app(
            "cortex", repo_path=self.cortex_repo, runner_type="claude-code",
            command="claude", runner_args=["--model", "sonnet"], auto_start=True,
        )
        claimed, token = self.store.claim_work(work["work_id"], "runner-test")
        self.store.update_session("cortex", work_id=work["work_id"], session_id="session-123")
        runner = WorkerRunner(self.store, claimed, token, "runner-test")
        command = runner._command()
        self.assertIn("--resume", command)
        self.assertIn("session-123", command)
        self.assertEqual(command[-2], "-p")
        self.assertIn("Work ID", command[-1])

    def test_codex_command_uses_jsonl_resume_and_masks_prompt(self):
        work = self.make_work("cortex")
        self.store.activate_work(work["work_id"])
        self.store.register_app(
            "cortex", repo_path=self.cortex_repo, runner_type="codex",
            runner_args=["--sandbox", "workspace-write"], auto_start=True,
        )
        claimed, token = self.store.claim_work(work["work_id"], "runner-test")
        self.store.update_session(
            "cortex", work_id=work["work_id"], session_id="thread-123",
        )
        runner = WorkerRunner(self.store, claimed, token, "runner-test")
        command = runner._command()
        self.assertEqual(command[:3], ["codex", "exec", "--json"])
        self.assertIn("resume", command)
        self.assertIn("thread-123", command)
        self.assertIn("$work", command[-1])
        self.assertEqual(runner._logged_command(command)[-1], "<prompt>")

    def test_codex_jsonl_thread_id_is_recovered_from_first_event(self):
        path = os.path.join(self.temp, "codex.jsonl")
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write('{"type":"thread.started","thread_id":"thread-jsonl"}\n')
            handle.write('{"type":"turn.started"}\n')
            handle.write('{"type":"turn.completed","usage":{}}\n')
        self.assertEqual(session_id_from_output(path), "thread-jsonl")

    def test_profiled_work_uses_controller_entry_and_managed_worktree(self):
        subprocess.check_call(["git", "init"], cwd=self.cortex_repo, stdout=subprocess.DEVNULL)
        subprocess.check_call(["git", "config", "user.email", "test@example.com"], cwd=self.cortex_repo)
        subprocess.check_call(["git", "config", "user.name", "Test"], cwd=self.cortex_repo)
        with open(os.path.join(self.cortex_repo, "README.md"), "w") as handle:
            handle.write("fixture\n")
        subprocess.check_call(["git", "add", "README.md"], cwd=self.cortex_repo)
        subprocess.check_call(["git", "commit", "-m", "fixture"], cwd=self.cortex_repo, stdout=subprocess.DEVNULL)
        base_ref = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.cortex_repo
        ).decode("utf-8").strip()
        work = self.store.create_work(
            self.change["change_id"], "cortex", "Design a fixture",
            acceptance=["isolated workspace"], controller="design", runner_profile="codex",
            base_ref=base_ref, request_ref="REQ-PROFILED",
        )
        self.store.activate_work(work["work_id"])
        self.store.register_app(
            "cortex", repo_path=self.cortex_repo, runner_type="claude-code",
            runner_profiles={"codex": {"command": "codex", "args": ["--sandbox", "workspace-write"]}},
            default_runner_profile="codex",
        )
        claimed, token = self.store.claim_work(work["work_id"], "runner-test")
        runner = WorkerRunner(self.store, claimed, token, "runner-test")
        runner.workspace_path = runner._prepare_workspace()
        self.assertNotEqual(runner.workspace_path, self.cortex_repo)
        self.assertIn("$design", runner._command()[-1])
        self.assertEqual(
            subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=runner.workspace_path)
            .decode("utf-8").strip(),
            base_ref,
        )
        with open(os.path.join(runner.workspace_path, "README.md"), "a") as handle:
            handle.write("uncommitted\n")
        with self.assertRaisesRegex(ValidationError, "managed workspace is dirty"):
            runner._prepare_workspace()

    def test_new_binding_default_profile_is_used_for_legacy_work(self):
        work = self.make_work("cortex")
        self.store.activate_work(work["work_id"])
        self.store.register_app(
            "cortex", repo_path=self.cortex_repo, runner_type="claude-code",
            runner_profiles={"codex": {"command": "codex", "args": ["--sandbox", "workspace-write"]}},
            default_runner_profile="codex",
        )
        claimed, token = self.store.claim_work(work["work_id"], "runner-test")
        runner = WorkerRunner(self.store, claimed, token, "runner-test")
        self.assertEqual(runner.runner["type"], "codex")


class AmplaiSupervisorGoldenTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.mkdtemp(prefix="amplai-golden-")
        self.home = os.path.join(self.temp, "project")
        self.cortex_repo = os.path.join(self.temp, "cortex")
        self.synapse_repo = os.path.join(self.temp, "synapse")
        os.makedirs(self.cortex_repo)
        os.makedirs(self.synapse_repo)
        self.worker_path = os.path.join(self.temp, "fake_worker.py")
        worker_source = r'''import os, sys
sys.path.insert(0, %r)
from amplai_runtime import ProjectStore
store = ProjectStore(os.environ["AMPLAI_PROJECT_HOME"])
work_id = os.environ["AMPLAI_WORK_ID"]
token = os.environ["AMPLAI_LEASE_TOKEN"]
work = store.get_work(work_id)
cr_id = work["change_id"]
if work["target_app"] == "cortex" and not work.get("depends_on"):
    child = store.create_work(
        cr_id, "synapse", "Confirm consumer contract", source_app="cortex",
        acceptance=["consumer contract evidence recorded"], actor="cortex-agent")
    store.activate_work(child["work_id"], actor="cortex-agent")
    store.wait_work(work_id, token, [child["work_id"]], "Waiting for Synapse contract", actor="cortex-agent")
elif work["target_app"] == "synapse":
    evidence = store.add_evidence(
        cr_id, "contract", "Synapse accepts optional field", "fixture",
        "fixture://synapse-contract", work_id=work_id, app_id="synapse",
        facts=["optional string is backward compatible"], actor="synapse-agent")
    store.complete_work(work_id, token, "Consumer contract confirmed", [evidence["evidence_id"]], actor="synapse-agent")
else:
    evidence = store.add_evidence(
        cr_id, "test", "Cortex resumed after Synapse", "fixture",
        "fixture://cortex-resume", work_id=work_id, app_id="cortex",
        facts=["dependency result consumed"], actor="cortex-agent")
    store.complete_work(work_id, token, "Producer implementation completed after resume", [evidence["evidence_id"]], actor="cortex-agent")
print('{"ok": true}')
''' % SCRIPTS
        with io.open(self.worker_path, "w", encoding="utf-8") as handle:
            handle.write(worker_source)

    def tearDown(self):
        shutil.rmtree(self.temp)

    def test_cortex_synapse_cortex_async_round_trip(self):
        store = ProjectStore.initialize(self.home, "golden", git_init=False)
        for app_id, repo in (("cortex", self.cortex_repo), ("synapse", self.synapse_repo)):
            store.register_app(
                app_id, repo_path=repo, runner_type="command",
                command=sys.executable, runner_args=[self.worker_path], auto_start=True,
            )
        change = store.create_change(
            "Async round trip", "Cortex waits for Synapse and resumes", "cortex",
            affected_apps=["cortex", "synapse"], change_id="CR-0100",
        )
        store.activate_change(change["change_id"])
        initial = store.create_work(
            change["change_id"], "cortex", "Implement producer",
            acceptance=["resume after consumer decision"],
        )
        store.activate_work(initial["work_id"])
        results = Supervisor(store, max_workers=2).run_until_quiescent()
        works = store.list_work(cr_id=change["change_id"])
        self.assertEqual(len(works), 2)
        self.assertTrue(all(item["status"] == "DONE" for item in works))
        self.assertEqual(store.get_work(initial["work_id"])["attempts"], 2)
        self.assertTrue(store.verify()["ok"])
        self.assertGreaterEqual(len(results), 3)


if __name__ == "__main__":
    unittest.main()

class AmplaiHookTest(unittest.TestCase):
    def setUp(self):
        # 이 test 는 임시 Store 를 세우지만 discover_project_home() 은
        # AMPLAI_PROJECT_HOME 을 repo-local binding 보다 먼저 본다
        # (amplai_runtime.py:2381). 그 변수를 심는 것이 이 kit 의 SessionStart
        # hook 자신이므로, kit 이 설치된 저장소의 세션에서 돌리면 실제 Store 를
        # 읽어 버린다. 격리는 test 의 책임이다 — hook 의 동작은 의도된 것이다.
        self._saved_env = {
            name: value for name, value in os.environ.items()
            if name.startswith("AMPLAI_")
        }
        for name in self._saved_env:
            del os.environ[name]
        self.temp = tempfile.mkdtemp(prefix="amplai-hook-")
        self.repo = os.path.join(self.temp, "app")
        self.home = os.path.join(self.temp, "project")
        os.makedirs(os.path.join(self.repo, ".ai-team", "local"))
        self.store = ProjectStore.initialize(self.home, "hook-project", git_init=False)
        self.store.register_app("synapse", repo_path=self.repo)
        from amplai_runtime import write_json_atomic
        write_json_atomic(os.path.join(self.repo, ".ai-team", "app.json"), seal({
            "schema_version": "1.0", "kind": "app_identity",
            "runtime_protocol": "amplai.async-cross-app.v1",
            "project_id": "hook-project", "app_id": "synapse",
        }))
        write_json_atomic(os.path.join(self.repo, ".ai-team", "local", "project.json"), seal({
            "schema_version": "1.0", "kind": "local_project_binding",
            "runtime_protocol": "amplai.async-cross-app.v1",
            "project_id": "hook-project", "app_id": "synapse",
            "project_home": self.home,
        }), mode=0o600)

    def tearDown(self):
        for name in [n for n in os.environ if n.startswith("AMPLAI_")]:
            del os.environ[name]
        os.environ.update(self._saved_env)
        shutil.rmtree(self.temp)

    def test_session_hooks_inject_and_checkpoint_without_owning_work_state(self):
        from amplai_hook import session_end, session_start
        change = self.store.create_change(
            "Hook test", "Expose READY Work", "synapse", change_id="CR-0200",
        )
        self.store.activate_change(change["change_id"])
        work = self.store.create_work(
            change["change_id"], "synapse", "Run from hook context",
            acceptance=["context injected"],
        )
        self.store.activate_work(work["work_id"])
        old = os.environ.copy()
        env_file = os.path.join(self.temp, "claude-env")
        try:
            os.environ["CLAUDE_PROJECT_DIR"] = self.repo
            os.environ["CLAUDE_ENV_FILE"] = env_file
            value = session_start({"cwd": self.repo})
            context = value["hookSpecificOutput"]["additionalContext"]
            self.assertIn("Next READY Work", context)
            self.assertIn(work["work_id"], context)
            session_end({"cwd": self.repo, "session_id": "hook-session", "reason": "clear"})
            session = self.store.get_session("synapse")
            self.assertEqual(session["last_event"], "SessionEnd")
            self.assertEqual(self.store.get_work(work["work_id"])["status"], "READY")
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_codex_session_start_uses_shared_context_without_claude_title(self):
        from amplai_hook import session_start
        change = self.store.create_change(
            "Codex hook test", "Expose READY Work", "synapse", change_id="CR-0201",
        )
        self.store.activate_change(change["change_id"])
        work = self.store.create_work(
            change["change_id"], "synapse", "Run from Codex hook context",
            acceptance=["context injected"],
        )
        self.store.activate_work(work["work_id"])
        value = session_start({"cwd": self.repo}, host="codex")
        output = value["hookSpecificOutput"]
        self.assertIn(work["work_id"], output["additionalContext"])
        self.assertNotIn("sessionTitle", output)
