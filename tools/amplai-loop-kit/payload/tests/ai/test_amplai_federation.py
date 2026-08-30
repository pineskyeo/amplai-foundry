import os
import shutil
import sys
import tempfile
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from amplai_runtime import ConflictError, ProjectStore  # noqa: E402


class FederationLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.mkdtemp(prefix="amplai-federation-")
        self.repo = os.path.join(self.temp, "repo")
        os.makedirs(self.repo)
        self.store = ProjectStore.initialize(
            os.path.join(self.temp, "store"), "platform-test", git_init=False
        )
        self.store.register_app("app", repo_path=self.repo)
        change = self.store.create_change("title", "goal", "app")
        self.cr = change["change_id"]
        self.store.activate_change(self.cr)
        work = self.store.create_work(self.cr, "app", "goal", acceptance=["ok"])
        self.work = work["work_id"]
        question = self.store.create_question(self.work, "which?")
        self.question = question["question_id"]
        self.evidence = self.store.add_evidence(
            self.cr, "test", "pytest passed", "test", "pytest://unit", work_id=self.work
        )
        self.decision = self.store.record_decision(
            self.question,
            "choose A",
            "tests support it",
            [self.evidence["evidence_id"]],
        )

    def tearDown(self):
        shutil.rmtree(self.temp)

    def test_new_objects_start_local(self):
        self.assertEqual(self.evidence["federation"]["promotion_status"], "LOCAL")
        self.assertEqual(self.decision["federation"]["promotion_status"], "LOCAL")

    def test_evidence_promotion_acceptance(self):
        ref = self.evidence["evidence_id"]
        candidate = self.store.mark_promotion_candidate("evidence", ref)
        self.assertEqual(candidate["federation"]["promotion_status"], "CANDIDATE")
        envelope = self.store.promotion_envelope("evidence", ref)
        self.assertEqual(envelope["origin"]["object_ref"], ref)
        self.assertTrue(envelope["idempotency_key"].startswith("kit:evidence:"))
        submitted = self.store.mark_promotion_submitted("evidence", ref, "sub-1")
        self.assertEqual(submitted["federation"]["submission_id"], "sub-1")
        accepted = self.store.resolve_promotion(
            "evidence", ref, True, canonical_ref="evidence:platform-test:0001"
        )
        self.assertEqual(accepted["federation"]["promotion_status"], "ACCEPTED")
        self.assertEqual(
            accepted["federation"]["canonical_ref"], "evidence:platform-test:0001"
        )

    def test_decision_rejection_can_be_reconsidered(self):
        ref = self.decision["decision_id"]
        self.store.mark_promotion_candidate("decision", ref)
        self.store.mark_promotion_submitted("decision", ref, "sub-2")
        rejected = self.store.resolve_promotion(
            "decision", ref, False, reason="not stable knowledge"
        )
        self.assertEqual(rejected["federation"]["promotion_status"], "REJECTED")
        candidate = self.store.mark_promotion_candidate("decision", ref)
        self.assertEqual(candidate["federation"]["promotion_status"], "CANDIDATE")

    def test_state_machine_is_fail_closed(self):
        ref = self.evidence["evidence_id"]
        with self.assertRaises(ConflictError):
            self.store.mark_promotion_submitted("evidence", ref, "too-soon")
        self.store.mark_promotion_candidate("evidence", ref)
        with self.assertRaises(ConflictError):
            self.store.resolve_promotion(
                "evidence", ref, True, canonical_ref="evidence:x"
            )


if __name__ == "__main__":
    unittest.main()
