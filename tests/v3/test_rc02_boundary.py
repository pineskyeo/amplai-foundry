"""V3 test-catalog boundary cases: T058 (EXTERNAL: real browser) (visual, local part), T-076 (security),
T-108/T-109 (release conformance). See specs/014-v3-catalog-closure/external-boundary.json
for the EXTERNAL/GREEN classification and evidence for each id.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from amplai_foundry.meta_harness.reference import MetaReference
from amplai_foundry.runtime.contracts.gates import GateEngine, Observation
from amplai_foundry.runtime.contracts.identity import digest, new_id
from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.tool_broker.service import SecretBroker
from amplai_foundry.verification.runtime.visual import VisualVerifier


class FakeRenderer:
    """Stands in for the real qualified headless browser (external boundary).

    T058 (EXTERNAL: real browser) needs actual page layout/geometry (a real Chromium via Playwright) to prove
    an SVG clips text; that layer is EXTERNAL (see external-boundary.json). What is
    tested here, with real code, is the local part: VisualVerifier's own aggregation
    of a renderer's geometry findings into pass/fail with page/region detail, across a
    bounded repair-then-rerender cycle.
    """

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def render(self, html, destination, *, width, height, required_selectors, steps):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        shot = Path(destination) / "shot.png"
        shot.parent.mkdir(parents=True, exist_ok=True)
        shot.write_bytes(b"fake-png")
        clipped = ["headline"] if outcome == "fail" else []
        return {
            "source_digest": "x",
            "viewport": {"width": width, "height": height},
            "browser_version": "fake-fixture",
            "details": {
                "horizontal_overflow": False,
                "clipped_critical": clipped,
                "outside_critical": [],
                "critical_overlaps": [],
                "broken_images": [],
                "font_failures": [],
                "invalid_buttons": 0,
                "unlabelled_inputs": 0,
            },
            "page_errors": [],
            "blocked_requests_count": 0,
            "missing_selectors": [],
            "screenshot_path": str(shot),
            "execution_boundary": "fake fixture (external browser boundary, see T058 (EXTERNAL: real browser) EXTERNAL)",
            "production_qualification": False,
            "aesthetic_acceptance": "not_assessed",
            "outcome": outcome,
        }


def test_visual_clip_finding_local_part_browser_layer_external():
    # given: source SVG syntactically valid but final embedding clips a critical text
    # element on first render (the real layout engine is faked as the external side)
    renderer = FakeRenderer(["fail", "pass"])
    verifier = VisualVerifier(renderer, viewports=((1280, 900),))
    html = b"<html><body><svg data-amplai-critical id='headline'></svg></body></html>"
    # when: render the final output
    first = verifier(html)
    # expected: visual finding with page/region (viewport + named clipped element)
    assert first.outcome == "fail"
    render = first.details["renders"][0]
    assert render["viewport"] == {"width": 1280, "height": 900}
    assert render["details"]["clipped_critical"] == ["headline"]
    # when: a bounded repair is applied and the page is rerendered
    second = verifier(html)
    # expected: rerender clears the finding
    assert second.outcome == "pass"
    assert renderer.calls == 2


def test_t076_credential_isolation_broker_handle_only_no_raw_secret_leak():
    # given: a worker prompt/tool asks for a production token
    broker = SecretBroker(
        {"PROD_TOKEN": "sk-production-real-secret"},
        allowed_hosts={"PROD_TOKEN": {"api.prod.invalid"}},
    )
    # when: secret access is requested
    handle = broker.issue_handle("PROD_TOKEN", "api.prod.invalid")
    # expected: broker returns a handle only, never the raw long-lived secret
    assert handle.startswith("secret-handle-") and "sk-production-real-secret" not in handle
    assert "sk-production-real-secret" not in repr(broker)
    # a handle used against a non-allow-listed / non-HTTPS endpoint is denied
    with pytest.raises(Hold):
        broker.with_secret(handle, "https://attacker.invalid/x", lambda v: v)
    with pytest.raises(RuntimeFault):
        broker.with_secret(handle, "http://api.prod.invalid/x", lambda v: v)
    # the correct scoped endpoint is the only place the raw value is ever delivered,
    # and only inside the broker-owned callback — never returned as a bare value
    captured = []
    result = broker.with_secret(handle, "https://api.prod.invalid/x", captured.append)
    assert captured == ["sk-production-real-secret"] and result is None
    # revocation removes the handle; it can never be replayed
    broker.revoke(handle)
    with pytest.raises(Hold):
        broker.with_secret(handle, "https://api.prod.invalid/x", lambda v: v)


@pytest.mark.parametrize("profile", ["direct", "discovery", "deliberative", "bounded_loop"])
def test_t108_gate_applicability_cannot_bypass_under_fast_tiny_flag(deployment, profile):
    # given: direct/discovery/deliberative/bounded profiles all reach the same mandatory
    # boundary (GateEngine.evaluate takes no strategy/profile input — a "fast"/"tiny" flag
    # on the caller cannot change which gates are mandatory)
    engine = GateEngine(deployment.runtime.contracts)
    protected = "G-03"  # cannot be waived on an execution transition (gates.py)
    # when: profile=<profile> reaches the boundary and claims the protected gate is N/A
    # under a "fast"/"tiny" flag, with an (illegitimate, for a hard-protected gate) rule
    with pytest.raises(Hold) as exc:
        engine.evaluate(
            [protected],
            {
                protected: Observation(
                    "not_applicable",
                    f"{profile} fast/tiny flag claims this is not needed",
                    applicability_rule="policy://fast-path/v1",
                )
            },
        )
    # expected: same invariant enforcement regardless of profile; only a legitimate N/A
    # rule is ever accepted, and never for a hard-protected gate
    assert exc.value.code == "GATE_BLOCKED"
    assert exc.value.details[0]["outcome"] == "hold"

    # an unversioned/unreasoned N/A claim on a non-protected gate is also rejected
    with pytest.raises(Hold):
        engine.evaluate(["G-20"], {"G-20": Observation("not_applicable", "")})

    # only a fully legitimate, versioned applicability rule with a reason is accepted,
    # and only for a gate that is not hard-protected
    result = engine.evaluate(
        ["G-20"],
        {
            "G-20": Observation(
                "not_applicable",
                "This deployment has no external distribution channel",
                applicability_rule="policy://gate-20-na/v1",
            )
        },
    )
    assert result[0]["outcome"] == "not_applicable"


def meta(tmp_path):
    return MetaReference(tmp_path / "meta")


def test_t109_meta_rejection_and_rollback_path_complete(tmp_path):
    # given: an eligible experiment with a bad candidate (fails offline evaluation)
    m1 = meta(tmp_path / "m1")
    try:
        rejected = m1.prepare(bad_candidate=True)
        # when: exercised through the offline/fail branch (no canary, no promotion)
        fail_result = m1.execute(rejected, rollback=False)
        # expected: fail/inconclusive verdict never reaches promotion
        assert fail_result["verdict"] != "pass" and fail_result["promoted"] is False
    finally:
        m1.close()

    # given: a second eligible experiment and a controlled nonproduction canary,
    # this time with a good candidate
    m2 = meta(tmp_path / "m2")
    try:
        accepted = m2.prepare(bad_candidate=False)
        # independent grants: the reviewer, never the proposer, issues every approval —
        # no self-promote
        assert m2.proposer.subject_id != m2.reviewer.subject_id
        # when: pass -> canary -> promote -> rollback branches are all exercised
        pass_result = m2.execute(accepted, rollback=True)
        # expected: frozen report evidence, real promotion and real rollback recorded
        assert pass_result["verdict"] == "pass"
        assert pass_result["promotion"] is not None
        assert pass_result["rollback"] is not None
        report_ref = pass_result["report_ref"]
        # the report artifact is content-addressed / frozen: refetching it is stable
        frozen_again = m2.d.store.get(m2.d.scope, "eval-report", report_ref)
        assert digest(frozen_again) == digest(m2.d.store.get(m2.d.scope, "eval-report", report_ref))
    finally:
        m2.close()

    # given: a third eligible experiment reaching promotion while an old Run is
    # still active/in-flight on the prior composition
    m = meta(tmp_path / "m3")
    try:
        third = m.prepare(bad_candidate=False)
        pid = third["proposal_id"]
        m.meta.start_offline(m.reviewer, pid)
        report_ref_3 = m.eval.run(m.reviewer, third["experiment_ref"], m.execute_case)
        m.meta.evaluate(m.reviewer, pid, report_ref_3)
        task_bindings = {c["case_id"]: m.canary_target(c["case_id"]) for c in third["cases"][:2]}
        targets = list({digest(r): r for r in task_bindings.values()}.values())
        policy_ref = m.put(
            "canary-policy",
            {
                "policy_id": new_id("canary-policy"),
                "eligible_task_ids": list(task_bindings),
                "max_runs": 2,
                "max_wall_seconds": 120,
                "max_cost_microunits": 100,
                "max_trial_cost_microunits": 10,
                "max_trial_tokens": 0,
                "max_concurrent": 1,
                "project_opt_in": True,
                "eligible_risk_classes": ["low"],
                "target_binding_refs": targets,
                "task_binding_refs": task_bindings,
                "abort_on_safety_failure": True,
                "abort_on_unknown_effect": True,
                "fallback_release_ref": third["baseline_release_ref"],
            },
        )
        approval = m.approve(
            "canary.execute",
            digest(
                {
                    "proposal_ref": third["proposal_ref"],
                    "report_ref": report_ref_3,
                    "policy_ref": policy_ref,
                }
            ),
        )
        m.meta.approve_canary(m.reviewer, pid, policy_ref, approval)
        m.meta.start_canary(m.reviewer, pid)
        for case in third["cases"][:2]:

            def run_canary(_: str, case=case) -> dict:
                obs = m.execute_case(third["candidate_ref"], case, 0, "canary")
                return {
                    "success": obs.success,
                    "safety_failures": obs.safety_failures,
                    "unknown_effects": obs.unknown_effects,
                    "cost_microunits": obs.cost_microunits,
                    "artifact_ref": obs.artifact_refs[0],
                    "input_tokens": obs.input_tokens,
                    "output_tokens": obs.output_tokens,
                }

            m.meta.canary_trial(m.reviewer, pid, case["case_id"], run_canary)
        m.meta.request_promotion(m.reviewer, pid)
        with m.d.store.tx() as db:
            m.d.store.cas(
                db,
                m.d.scope,
                "run",
                "in-flight-run",
                0,
                "running",
                {"composition_ref": third["baseline_ref"], "process_stopped": False},
            )
        plan = {
            "schema_version": "3.0.0",
            "promotion_id": new_id("promotion"),
            "scope": m.d.scope.wire(),
            "expected_active_release_ref": third["baseline_release_ref"],
            "candidate_release_ref": third["candidate_release_ref"],
            "eval_report_ref": report_ref_3,
            "target_binding_refs": targets,
            "canary_policy_ref": policy_ref,
            "abort_rules": ["No unknown effects", "No security regression"],
            "rollback_release_ref": third["baseline_release_ref"],
            "expires_at": (datetime.fromtimestamp(m.d.store.clock(), UTC) + timedelta(minutes=5))
            .isoformat()
            .replace("+00:00", "Z"),
            "active_run_policy": "drain",
        }
        plan["grant_ref"] = m.approve("release.promote", digest(plan))
        # when: promotion is attempted while the old Run is still reading/continuing
        with pytest.raises(Hold) as exc:
            m.meta.promote(m.reviewer, pid, plan)
        # expected: old in-flight work is pinned — promotion is blocked, not silently
        # racing the still-active composition binding
        assert exc.value.code == "DRAIN_REQUIRED"
    finally:
        m.close()
