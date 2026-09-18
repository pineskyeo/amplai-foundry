"""Frozen sampling, independent execution, strict receipts and root budgets."""

import json
from dataclasses import replace

import pytest

from amplai_foundry.evaluation.analysis import analyze_pairs, validate_analysis_plan
from amplai_foundry.evaluation.receipts import read_receipt
from amplai_foundry.meta_harness.reference import MetaReference
from amplai_foundry.runtime.contracts.identity import canonical, digest, new_id
from amplai_foundry.runtime.errors import Hold, RuntimeFault


@pytest.fixture
def meta03(tmp_path):
    m = MetaReference(tmp_path / "meta")
    try:
        yield m
    finally:
        m.close()


def analysis_plan(**kw):
    return {
        "method": "paired_binary_conservative",
        "confidence": 0.95,
        "minimum_tasks": 24,
        "repeats_per_task": 1,
        "noninferiority_margin": 0.25,
        "safety_failure_limit": 0,
        "missing_policy": "inconclusive",
        "purpose": "local_qualification",
        **kw,
    }


def observations(n=24, repeats=1, candidate=True, cost_b=10, cost_c=5):
    return [
        {
            "task_id": str(i),
            "arm": arm,
            "repeat": r,
            "success": True if arm == "baseline" else candidate,
            "safety_failures": 0,
            "unknown_effects": 0,
            "cost_microunits": cost_b if arm == "baseline" else cost_c,
            "elapsed_ms": 2.0,
            "usage_status": "measured",
        }
        for i in range(n)
        for arm in ("baseline", "candidate")
        for r in range(repeats)
    ]


@pytest.mark.parametrize("n,repeats", [(1, 48), (2, 24), (12, 4)])
def test_dev03_repeated_runs_do_not_inflate_independent_sample(n, repeats):
    result = analyze_pairs(
        observations(n, repeats),
        analysis_plan(repeats_per_task=repeats),
        expected_tasks=[str(i) for i in range(n)],
    )
    assert result["verdict"] == "inconclusive" and result["task_count"] == n
    assert "insufficient_distinct_tasks" in result["reasons"]


def test_dev03_noninferiority_is_not_claimed_from_all_pass_zero_width_interval():
    result = analyze_pairs(
        observations(),
        analysis_plan(noninferiority_margin=0),
        expected_tasks=[str(i) for i in range(24)],
    )
    assert result["verdict"] == "inconclusive"
    assert result["confidence_interval"][0] < 0 < result["confidence_interval"][1]


@pytest.mark.parametrize(
    "change",
    [
        {"confidence": True},
        {"confidence": float("nan")},
        {"confidence": 1},
        {"minimum_tasks": True},
        {"minimum_tasks": 1},
        {"repeats_per_task": 0},
        {"repeats_per_task": 1.5},
        {"noninferiority_margin": float("inf")},
        {"noninferiority_margin": -1},
        {"safety_failure_limit": 1},
        {"missing_policy": "ignore"},
        {"purpose": "promote_anyway"},
        {"benefit": 42},
    ],
)
def test_dev03_analysis_refuses_invalid_or_weakened_plans(change):
    with pytest.raises(RuntimeFault):
        validate_analysis_plan(analysis_plan(**change))


def test_dev03_confirmatory_requires_power_rationale_and_stopping_rule():
    with pytest.raises(RuntimeFault):
        validate_analysis_plan(analysis_plan(purpose="confirmatory"))
    validate_analysis_plan(
        analysis_plan(
            purpose="confirmatory",
            sample_rationale="fixed paired 24 tasks for local study",
            variance_basis="independent pilot disclosed",
            sequential_rule="fixed_sample_safety_abort_only",
        )
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("success", 1),
        ("success", "pass"),
        ("safety_failures", True),
        ("unknown_effects", -1),
        ("cost_microunits", float("nan")),
        ("elapsed_ms", float("inf")),
        ("repeat", True),
        ("arm", "other"),
    ],
)
def test_dev03_ambiguous_trial_measurements_rejected(field, value):
    ts = observations()
    ts[0][field] = value
    with pytest.raises(RuntimeFault):
        analyze_pairs(ts, analysis_plan(), expected_tasks=[str(i) for i in range(24)])


def test_dev03_duplicate_task_or_trial_cannot_inflate_score():
    ts = observations()
    with pytest.raises(RuntimeFault):
        analyze_pairs([*ts, ts[0]], analysis_plan(), expected_tasks=[str(i) for i in range(24)])
    with pytest.raises(RuntimeFault):
        analyze_pairs(ts, analysis_plan(), expected_tasks=["0", "0"])


@pytest.mark.parametrize("case", ["missing", "unknown_cost", "estimated", "drift", "contaminated"])
def test_dev03_incomplete_qualification_is_inconclusive(case):
    ts = observations()
    kwargs = {}
    if case == "missing":
        ts.pop()
    if case == "unknown_cost":
        ts[0]["cost_microunits"] = None
    if case == "estimated":
        ts[0]["usage_status"] = "estimated"
    if case == "drift":
        kwargs["environment_drifted"] = True
    if case == "contaminated":
        kwargs["contamination"] = True
    result = analyze_pairs(
        ts, analysis_plan(), expected_tasks=[str(i) for i in range(24)], **kwargs
    )
    assert result["verdict"] == "inconclusive"


def test_dev03_safety_failure_dominates_missing_data_and_better_cost():
    ts = observations()
    ts[0]["safety_failures"] = 1
    ts.pop()
    result = analyze_pairs(ts, analysis_plan(), expected_tasks=[str(i) for i in range(24)])
    assert result["verdict"] == "fail"


@pytest.mark.parametrize("saving,expected", [(5, "pass"), (0, "fail")])
def test_dev03_paired_task_cost_benefit_is_predeclared_and_reproducible(saving, expected):
    p = analysis_plan(
        benefit={
            "endpoint": "cost_mean_microunits",
            "minimum_reduction": 2,
            "bootstrap_samples": 1000,
            "seed": 42,
        }
    )
    ts = observations(repeats=2, cost_c=10 - saving)
    p["repeats_per_task"] = 2
    args = {"expected_tasks": [str(i) for i in range(24)]}
    a = analyze_pairs(ts, p, **args)
    b = analyze_pairs(ts, p, **args)
    assert a == b and a["benefit"]["verdict"] == expected
    assert a["benefit"]["task_count"] == 24


@pytest.mark.parametrize(
    "raw", [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}', b"[]", b"null", b"bad"]
)
def test_dev03_receipts_reject_ambiguous_json(raw):
    with pytest.raises(RuntimeFault):
        read_receipt(raw)


def freeze_case(m, split="holdout", corpus_id="private", artifact=None, limit=1):
    artifact = artifact or m.d.artifacts.admit(
        m.d.scope, b'{"input":42,"expected":84}', "application/json", trust="operator"
    )
    case = {
        "case_id": "case-a",
        "split": split,
        "task_class": "arithmetic",
        "artifact_ref": artifact,
    }
    return m.eval.corpus.freeze(m.reviewer, corpus_id, [case], holdout_use_limit=limit), artifact


def test_dev03_corpus_never_claims_physical_secrecy(meta03):
    ref, _ = freeze_case(meta03)
    v = meta03.d.store.get(meta03.d.scope, "eval-corpus", ref)
    assert v["metadata"]["secrecy_boundary"] == "local_acl"
    assert v["metadata"]["license"] == "unspecified-no-redistribution"
    with pytest.raises(Hold):
        meta03.eval.corpus.freeze(
            meta03.reviewer,
            "bad",
            v["cases"],
            holdout_use_limit=1,
            metadata={"secrecy_boundary": "remote_hidden"},
        )


@pytest.mark.parametrize("split", ["holdout", "validation"])
def test_dev03_proposer_role_cannot_override_split_acl(meta03, split):
    ref, _ = freeze_case(meta03, split)
    actor = replace(meta03.reviewer, permissions=meta03.reviewer.permissions | {"harness.propose"})
    with pytest.raises(Hold):
        meta03.eval.corpus.select(actor, ref, split, purpose="frozen_experiment")
    cases = meta03.d.store.get(meta03.d.scope, "eval-corpus", ref)["cases"]
    with pytest.raises(Hold):
        meta03.eval.corpus.freeze(actor, "new", cases, holdout_use_limit=1)


def test_dev03_duplicate_case_bytes_cannot_become_independent_tasks(meta03):
    _ref, artifact = freeze_case(meta03, split="development")
    cases = [
        {"case_id": str(i), "split": "validation", "task_class": "sum", "artifact_ref": artifact}
        for i in range(2)
    ]
    with pytest.raises(Hold):
        meta03.eval.corpus.freeze(meta03.reviewer, "duplicate", cases, holdout_use_limit=1)


def test_dev03_holdout_relabeling_tracks_prior_exposure(meta03):
    _, artifact = freeze_case(meta03, split="development", corpus_id="exposed")
    ref, _ = freeze_case(meta03, corpus_id="renamed", artifact=artifact)
    assert meta03.eval.corpus.contamination(meta03.d.scope, ref)


def test_dev03_holdout_budget_survives_corpus_copy_and_is_idempotent(meta03):
    m = meta03
    a, artifact = freeze_case(m, corpus_id="hold-a")
    b, _ = freeze_case(m, corpus_id="hold-b", artifact=artifact, limit=10)

    # Private store fixtures pin different experiment IDs to each exact corpus.
    def exp(corpus):
        with m.d.store.tx() as db:
            return m.d.store.put(
                db, m.d.scope, "eval-experiment", new_id("exp-fixture"), 1, {"corpus_ref": corpus}
            )

    ea, eb = exp(a), exp(b)
    m.eval.corpus.consume_holdout(m.reviewer, a, ea)
    m.eval.corpus.consume_holdout(m.reviewer, a, ea)
    with pytest.raises(Hold):
        m.eval.corpus.consume_holdout(m.reviewer, b, eb)
    m.eval.corpus.mark_contaminated(m.reviewer, a, "leaked in a debug trace")
    assert m.eval.corpus.contamination(m.d.scope, b)


def run_eval(m, p, callback=None):
    m.meta.start_offline(m.reviewer, p["proposal_id"])
    return m.eval.run(m.reviewer, p["experiment_ref"], callback or m.execute_case)


def test_dev03_executor_capability_is_required_before_first_dispatch(meta03):
    p = meta03.prepare()
    meta03.eval.executor_policy = None
    with pytest.raises(Hold):
        run_eval(meta03, p)
    assert meta03.meta.budgets.totals(meta03.d.scope, p["proposal_id"])["pending"] == 0


def test_dev03_reservation_precedes_callback_and_blocks_overspend(meta03):
    p = meta03.prepare()
    m = meta03
    calls = []
    m.eval.executor_policy = replace(m.eval.executor_policy, max_trial_tokens=999999)
    with pytest.raises(Hold) as caught:
        run_eval(m, p, lambda *args: calls.append(args))
    assert calls == [] and caught.value.code == "META_TOKEN_BUDGET"
    assert m.d.store.head(m.d.scope, "experiment", p["experiment_ref"]["id"])["state"] == "aborted"


def test_dev03_environment_probe_is_pinned_at_freeze(meta03):
    p = meta03.prepare()
    m = meta03
    calls = []
    m.eval.environment_probe = lambda env: {**env, "changed_after_freeze": True}
    with pytest.raises(Hold) as caught:
        run_eval(m, p, lambda *args: calls.append(args))
    assert calls == [] and caught.value.code == "ENVIRONMENT_DRIFT"
    assert (
        m.d.store.head(m.d.scope, "experiment", p["experiment_ref"]["id"])["state"]
        == "inconclusive"
    )


def test_dev03_callback_error_is_unknown_not_automatically_retried(meta03):
    m = meta03
    p = m.prepare()
    calls = []

    def interrupted(*args):
        calls.append(args)
        (m.root / "side-effect-happened.txt").write_text("written before disconnected response")
        raise ConnectionError("response lost")

    ref = run_eval(m, p, interrupted)
    assert len(calls) == 1
    assert m.d.store.get(m.d.scope, "eval-report", ref)["verdict"] != "pass"
    assert m.meta.budgets.totals(m.d.scope, p["proposal_id"])["unknown"] == 1
    with pytest.raises(Hold):
        m.eval.run(m.reviewer, p["experiment_ref"], interrupted)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "alteration", ["foreign_task", "wrong_composition", "counter_bool", "untrusted_receipt"]
)
def test_dev03_trial_receipt_substitution_fails_closed(meta03, alteration):
    m = meta03
    p = m.prepare()

    def callback(*args):
        obs = m.execute_case(*args)
        receipt = json.loads(m.d.artifacts.read(m.d.scope, obs.artifact_refs[0]))
        if alteration == "foreign_task":
            receipt["task_id"] = "foreign-task"
        if alteration == "wrong_composition":
            receipt["composition_ref"] = p["candidate_ref"]
        if alteration == "counter_bool":
            receipt["cost_microunits"] = False
        ar = m.d.artifacts.admit(
            m.d.scope,
            canonical(receipt),
            "application/json",
            trust="worker" if alteration == "untrusted_receipt" else "verifier",
        )
        return replace(obs, artifact_refs=(ar,))

    ref = run_eval(m, p, callback)
    assert m.d.store.get(m.d.scope, "eval-report", ref)["verdict"] != "pass"
    assert m.meta.budgets.totals(m.d.scope, p["proposal_id"])["unknown"] == 1


def test_dev03_current_approval_rechecked_after_actual_callback(meta03):
    m = meta03
    p = m.prepare()
    plan = m.d.store.get(m.d.scope, "eval-experiment", p["experiment_ref"])
    calls = []

    def callback(*args):
        calls.append(args)
        obs = m.execute_case(*args)
        m.approvals[digest(plan["approval_ref"])]["revoked"] = True
        return obs

    ref = run_eval(m, p, callback)
    assert len(calls) == 1 and m.d.store.get(m.d.scope, "eval-report", ref)["verdict"] != "pass"


def test_dev03_environment_drift_during_trial_cannot_be_qualified(meta03):
    m = meta03
    p = m.prepare()

    def callback(*args):
        obs = m.execute_case(*args)
        m.eval.environment_probe = lambda env: {**env, "drifted": True}
        return obs

    ref = run_eval(m, p, callback)
    report = m.d.store.get(m.d.scope, "eval-report", ref)
    assert report["verdict"] == "inconclusive" and report["environment_drifted"]


def test_dev03_interrupted_experiment_requires_new_owner_and_no_reexecution(meta03):
    m = meta03
    p = m.prepare()

    def crash(*args):
        raise KeyboardInterrupt("simulated process loss")

    with pytest.raises(KeyboardInterrupt):
        run_eval(m, p, crash)
    actor = replace(m.reviewer, permissions=m.reviewer.permissions | {"experiment.reconcile"})
    with pytest.raises(Hold):
        m.eval.recover_interrupted(actor, p["experiment_ref"])
    # Simulate the fencing epoch that a fresh Store owner increments on restart.
    m.d.store.epoch += 1
    result = m.eval.recover_interrupted(actor, p["experiment_ref"])
    assert result["state"] == "interrupted"
    assert m.meta.budgets.totals(m.d.scope, p["proposal_id"])["pending"] == 1
    with pytest.raises(Hold):
        m.eval.run(m.reviewer, p["experiment_ref"], m.execute_case)
