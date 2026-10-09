"""Work 033 S2: evaluator service (interfaces.md 3.8, 7, IC-09, IC-15, IC-16 B).

Covers Q-06 (reference arm, with a fake `reference_validator`), Q-08 exploratory part, Q-09
(parallel run, safety stop, concurrency cap), Q-10 (`MetaHarness._report` recomputes a new-method
passing report), Q-13 (stage subsets), Q-15 (per-experiment `max_trial_tokens`), the always-valid
e-process accumulation of 7.6 (including continuation into a new proposal with the same arms,
IC-16 B) and the `evaluator-version` record `eval-2` of 2.15. Legacy plans (no
`evaluator_version_ref`) keep full-split equality and the summed safety rule.

The world builder (`make_world`, `World`, `Scripted`, `policy`, `confirm`, `subset`) is shared
with `test_033_s2_calibration.py` and `test_033_s2_requalify.py`; `hold` and `fault` assert the
public code of a refusal.
"""

import contextlib
import dataclasses
import hashlib
import threading
import time
from pathlib import Path
from statistics import mean
from types import SimpleNamespace

import pytest
from meta_world import MetaReference

from amplai_foundry.evaluation import calibration, versions
from amplai_foundry.evaluation.analysis import analyze_pairs
from amplai_foundry.evaluation.corpus import CorpusService
from amplai_foundry.evaluation.receipts import read_receipt
from amplai_foundry.evaluation.sequential import (
    ALTERNATIVES,
    e_process_update,
    e_value,
    mde,
    min_wins_for_superiority,
)
from amplai_foundry.evaluation.service import (
    NOT_RUN_EVIDENCE,
    EvaluationService,
    TrialObservation,
    arm_order,
    e_process_key,
    effective_parallel,
    run_bounded,
)
from amplai_foundry.runtime.contracts.identity import canonical, digest, new_id, now
from amplai_foundry.runtime.errors import Hold, RuntimeFault

BUDGET = {
    "max_wall_seconds": 600,
    "max_attempts": 10,
    "max_tokens": 1_000_000,
    "max_cost_microunits": 1_000_000,
    "currency": "USD",
    "max_parallel_works": 4,
    "max_delegation_depth": 0,
}
ARMS3 = ["baseline", "candidate", "reference"]
DOMAINS = ("alpha", "beta", "gamma")


def hold(code, fn, *args, **kwargs):
    """`fn` is refused with a Hold of this public code."""
    with pytest.raises(Hold) as caught:
        fn(*args, **kwargs)
    assert caught.value.code == code
    return caught.value


def fault(code, fn, *args, **kwargs):
    """`fn` is refused with a RuntimeFault (not a Hold) of this public code."""
    with pytest.raises(RuntimeFault) as caught:
        fn(*args, **kwargs)
    assert not isinstance(caught.value, Hold)
    assert caught.value.code == code
    return caught.value


def ref_key(ref):
    return f"{ref['id']}@{ref['revision']}"


def policy(ev=None, **kw):
    """An analysis policy; with `ev` it is a versioned (Work 033) plan."""
    value = {
        "method": "paired_binary_conservative",
        "confidence": 0.95,
        "minimum_tasks": 2,
        "repeats_per_task": 1,
        "noninferiority_margin": 0.25,
        "safety_failure_limit": 0,
        "missing_policy": "inconclusive",
        "purpose": "exploratory",
    }
    if ev is not None:
        value["evaluator_version_ref"] = ev
    value.update(kw)
    return value


def confirm(ev, n, **kw):
    """A confirmatory policy over n tasks with the rationale lines of 7.1."""
    value = {
        "purpose": "confirmatory",
        "sample_rationale": f"Fixed paired sample of {n} tasks.",
        "variance_basis": "A/A discordance 0.5 assumed (no calibration data)",
        "sequential_rule": "fixed_sample_safety_abort_only",
    }
    if ev is not None:
        bound = mde(n, 0.5)
        value["mde"] = bound
        value["sample_rationale"] += f"\nMDE: {bound!r} at n={n}"
    value.update(kw)
    return policy(ev, **value)


def subset(rule="all_v1", max_tasks=16, *, cell="cell-a", summary=None, stage="focused", arms=None):
    """The IC-15 fields of a sampling plan."""
    return {
        "stage": stage,
        "arms": arms or ["baseline", "candidate"],
        "order_rule": "alternate_by_repeat_v1",
        "case_rule": rule,
        "max_tasks": max_tasks,
        "cell_id": cell,
        "calibration_summary_ref": summary,
    }


class Scripted:
    """A deterministic executor with scripted outcomes and receipts bound as the service requires.

    `roles` maps a role name ("baseline", "candidate", "reference") to its composition ref;
    `outcome(role, case_id, repeat)` gives `success` or `(success, safety_failures)`. Receipts are
    cached, so `prime` lets parallel runs call it without touching the store from worker threads.
    """

    def __init__(self, world, roles, outcome=None, *, tokens=(0, 0), delay=0.0, raises=None):
        self.world = world
        self.roles = {ref_key(ref): role for role, ref in roles.items()}
        self.compositions = dict(roles)
        self.outcome = outcome or (lambda role, case_id, repeat: True)
        self.tokens, self.delay, self.raises = tokens, delay, raises
        self.calls, self.cache = [], {}
        self.lock = threading.Lock()
        self.active = self.peak = 0

    def _observe(self, composition, case, repeat, mode):
        key = (ref_key(composition), case["case_id"], repeat)
        if key not in self.cache:
            d = self.world.m.d
            role = self.roles[ref_key(composition)]
            result = self.outcome(role, case["case_id"], repeat)
            success, safety = result if isinstance(result, tuple) else (result, 0)
            tin, tout = self.tokens
            receipt = {
                "success": success,
                "safety_failures": safety,
                "unknown_effects": 0,
                "cost_microunits": 0,
                "input_tokens": tin,
                "output_tokens": tout,
                "usage_status": "measured",
                "mode": mode,
                "composition_ref": composition,
                "task_id": case["case_id"],
                "repeat": repeat,
                "scope": d.scope.wire(),
            }
            artifact = d.artifacts.admit(
                d.scope, canonical(receipt), "application/json", trust="verifier"
            )
            self.cache[key] = self.world.observation(
                success, artifact, safety=safety, tokens=self.tokens
            )
        return self.cache[key]

    def prime(self, cases, repeats, mode="sandbox_rerun"):
        for composition in self.compositions.values():
            for case in cases:
                for repeat in range(repeats):
                    self._observe(composition, case, repeat, mode)

    def __call__(self, composition, case, repeat, mode):
        role = self.roles[ref_key(composition)]
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.calls.append((role, case["case_id"], repeat))
        try:
            if self.delay:
                time.sleep(self.delay)
            if self.raises and self.raises(role, case["case_id"], repeat):
                raise RuntimeError("response lost")
            with self.lock:
                return self._observe(composition, case, repeat, mode)
        finally:
            with self.lock:
                self.active -= 1


class FakeValidator:
    """A fake `reference_validator(scope, plan, policy)`; raises the given hold when set."""

    def __init__(self, refuse=None):
        self.refuse, self.calls = refuse, []

    def __call__(self, scope, plan, pol):
        self.calls.append((scope, plan, pol))
        if self.refuse:
            raise Hold("REFERENCE_ARM", self.refuse)


class World:
    """A MetaReference store plus builders for corpora, proposals and frozen experiments."""

    def __init__(self, meta):
        self.m = meta
        self.scope = meta.d.scope
        self.ev_ref = None

    def observation(self, success, artifact, *, safety=0, tokens=(0, 0)):
        return TrialObservation(
            success,
            (artifact,),
            safety_failures=safety,
            cost_microunits=0,
            input_tokens=tokens[0],
            output_tokens=tokens[1],
        )

    def composition(self, name, algorithm="builtin_sum"):
        m = self.m
        base = m.d.store.get(
            self.scope, "harness-composition", m.prepared["execution_profile"]["composition_ref"]
        )
        prompt = m.put("prompt-bundle", {"prompt_id": new_id("prompt"), "algorithm": algorithm})
        value = {
            **base,
            "composition_id": new_id(f"{name}-composition"),
            "prompt_bundle_ref": prompt,
        }
        return m.meta.compositions.register(m.proposer, value)

    def proposal(self, baseline_ref, candidate_ref):
        """A screened proposal (evolution head `screened`) for the two compositions."""
        m, d = self.m, self.m.d
        draft = m.put(
            "experiment-draft", {"draft_id": new_id("exp-draft"), "comparison": "scripted outcomes"}
        )
        rollback = m.put("rollback-plan", {"rollback_id": new_id("rollback")})
        issue = d.artifacts.admit(
            d.scope,
            b'{"issue":"Exercise the evaluator service on scripted outcomes"}',
            "application/json",
            trust="verifier",
        )
        observation = m.put(
            "harness-observation", {"observation_id": new_id("observation"), "artifact": issue}
        )
        change = d.artifacts.admit(
            d.scope,
            canonical(
                {
                    "changed_paths": ["packs/software/prompts/arithmetic.txt"],
                    "baseline_ref": baseline_ref,
                    "candidate_ref": candidate_ref,
                }
            ),
            "application/json",
            trust="operator",
        )
        proposal = {
            "schema_version": "3.0.0",
            "proposal_id": new_id("harness-proposal"),
            "scope": d.scope.wire(),
            "baseline_ref": baseline_ref,
            "candidate_ref": candidate_ref,
            "surface_class": "A",
            "hypothesis": "Scripted outcomes exercise the evaluator; not evidence of any LLM.",
            "observation_refs": [observation],
            "change_artifact": change,
            "expected_benefit": "Exercise the evaluator service without changing boundaries",
            "risks": ["Scripted outcomes do not estimate model quality"],
            "protected_surface_findings": [],
            "experiment_plan_ref": draft,
            "rollback_plan_ref": rollback,
            "proposer": m.proposer.wire(),
            "status": "draft",
        }
        proposal_ref = m.meta.submit(m.proposer, proposal)
        m.meta.screen(m.reviewer, proposal["proposal_id"])
        return {
            "proposal_id": proposal["proposal_id"],
            "proposal_ref": proposal_ref,
            "baseline_ref": baseline_ref,
            "candidate_ref": candidate_ref,
        }

    def corpus(self, dev=0, val=0, hold_n=0, limit=10):
        """A frozen corpus: development, validation, then holdout cases, each in id order."""
        m, d = self.m, self.m.d
        corpus_id = new_id("corpus")
        cases = []
        for split, count in (("development", dev), ("validation", val), ("holdout", hold_n)):
            for i in range(count):
                payload = {"numbers": [i, i + 1], "expected": 2 * i + 1, "salt": corpus_id}
                artifact = d.artifacts.admit(
                    d.scope,
                    canonical({**payload, "split": split}),
                    "application/json",
                    trust="operator",
                )
                cases.append(
                    {
                        "case_id": f"{split[:3]}-{i:02d}",
                        "split": split,
                        "task_class": DOMAINS[i % len(DOMAINS)],
                        "artifact_ref": artifact,
                    }
                )
        ref = CorpusService(d.store, d.artifacts).freeze(
            m.reviewer, corpus_id, cases, holdout_use_limit=limit
        )
        return ref, cases

    def evaluator(self, corpus_ref):
        """The `evaluator-version` eval-2 of the running code (written once per store)."""
        if self.ev_ref is None:
            requal = self.m.d.put(
                "evaluator-requalification",
                new_id("requal"),
                {
                    "schema": "amplai.evaluator-requalification.v1",
                    "scope": self.scope.wire(),
                    "evaluator_version": "eval-2",
                    "store": "test",
                    "reports": [],
                    "all_equal": True,
                },
            )
            self.ev_ref = versions.write_version(
                self.m.d.store, self.m.reviewer, corpus_ref=corpus_ref, requalification_ref=requal
            )
        return self.ev_ref

    def setup(
        self,
        dev=0,
        val=0,
        hold_n=0,
        baseline="loop_sum",
        candidate="builtin_sum",
        reference=False,
    ):
        corpus_ref, cases = self.corpus(dev=dev, val=val, hold_n=hold_n)
        base = self.composition("baseline", baseline)
        cand = self.composition("candidate", candidate)
        s = SimpleNamespace(
            corpus_ref=corpus_ref,
            cases=cases,
            ev=self.evaluator(corpus_ref),
            base=base,
            cand=cand,
            ref=self.composition("reference", "builtin_sum") if reference else None,
        )
        s.prop = self.proposal(base, cand)
        return s

    def ids(self, s, split="validation", n=None):
        ids = [c["case_id"] for c in s.cases if c["split"] == split]
        return ids if n is None else ids[:n]

    def experiment(
        self,
        s,
        pol,
        *,
        prop=None,
        split="validation",
        ids=None,
        sampling=None,
        budget=None,
        mode="sandbox_rerun",
    ):
        """An approved, not yet frozen experiment plan over the split (or the given ids)."""
        m, d = self.m, self.m.d
        prop = prop or s.prop
        analysis = m.put(
            "analysis-plan",
            {"analysis_id": new_id("analysis"), "policy": pol, "scope_note": "scripted S2 plan"},
        )
        sampling_ref = m.put(
            "sampling-plan",
            {
                "sampling_id": new_id("sampling"),
                "split": split,
                "case_ids": self.ids(s, split) if ids is None else ids,
                **(sampling or {}),
            },
        )
        holdout = m.put(
            "holdout-policy", {"policy_id": new_id("holdout"), "sealed": True, "max_uses": 1}
        )
        plan = {
            "schema_version": "3.0.0",
            "experiment_id": new_id("experiment"),
            "scope": d.scope.wire(),
            "proposal_ref": prop["proposal_ref"],
            "baseline_ref": prop["baseline_ref"],
            "candidate_ref": prop["candidate_ref"],
            "corpus_ref": s.corpus_ref,
            "verifier_ref": m.prepared["verifier_ref"],
            "environment_ref": m.prepared["execution_profile"]["environment_ref"],
            "mode": mode,
            "primary_endpoint": "task_all_repeats_pass",
            "analysis_plan_ref": analysis,
            "sampling_plan_ref": sampling_ref,
            "safety_stop_rules": ["Any safety failure or unknown effect stops the run"],
            "holdout_policy_ref": holdout,
            "budget": budget or BUDGET,
            "frozen_at": now(),
        }
        plan["approval_ref"] = m.approve("experiment.execute", digest(plan))
        return plan

    def freeze(self, plan, actor=None):
        return self.m.eval.freeze(actor or self.m.reviewer, plan)

    def run(self, experiment_ref, executor, *, split="validation", parallel=1, actor=None):
        return self.m.eval.run(
            actor or self.m.reviewer, experiment_ref, executor, split=split, parallel=parallel
        )

    def report(self, report_ref):
        return self.m.d.store.get(self.scope, "eval-report", report_ref)

    def trials(self, report_ref):
        store = self.m.d.store
        return [store.get(self.scope, "eval-trial", r) for r in self.report(report_ref)["run_refs"]]

    def analysis(self, report_ref):
        raw = self.m.d.artifacts.read(self.scope, self.report(report_ref)["analysis_artifact"])
        return read_receipt(raw)

    def state(self, plan):
        return self.m.d.store.head(self.scope, "experiment", plan["experiment_id"])["state"]

    def budget(self, proposal_id):
        return self.m.d.store.head(self.scope, "meta-budget", proposal_id)

    def roles(self, s):
        roles = {"baseline": s.base, "candidate": s.cand}
        if s.ref is not None:
            roles["reference"] = s.ref
        return roles


@contextlib.contextmanager
def make_world(tmp_path):
    """A `World` over a fresh MetaReference store under `tmp_path`, closed on exit. Plain factory
    so other S2 test modules define their own `w` fixture instead of importing this one."""
    meta = MetaReference(tmp_path / "meta")
    meta.eval.max_parallel = 4
    try:
        yield World(meta)
    finally:
        meta.close()


@pytest.fixture
def w(tmp_path):
    with make_world(tmp_path) as world:
        yield world


# --- evaluator-version eval-2 (2.15, 7.8) ---------------------------------------------------


def requalification(w, *, all_equal=True, version="eval-2"):
    value = {
        "schema": "amplai.evaluator-requalification.v1",
        "scope": w.scope.wire(),
        "evaluator_version": version,
        "store": "test",
        "reports": [],
        "all_equal": all_equal,
    }
    return w.m.d.put("evaluator-requalification", new_id("requal"), value)


def test_eval2_is_written_with_the_running_code_digests(w):
    corpus_ref, _ = w.corpus(val=16)
    store = w.m.d.store
    assert versions.current_version_ref(store, w.scope) is None
    ref = w.evaluator(corpus_ref)
    assert ref["id"] == "evaluator-2"
    assert ref["revision"] == 1
    value = versions.read_version(store, w.scope, ref)
    assert value["version"] == "eval-2"
    assert value["corpus_ref"] == corpus_ref
    assert value["approved_by"]["kind"] == "human"
    assert versions.current_version_ref(store, w.scope) == ref
    assert versions.check_current(value) is None


PYROOT = Path(versions.__file__).resolve().parent


def sha(names):
    data = b""
    for name in names:
        data += (PYROOT / name).read_bytes()
    return "sha256:" + hashlib.sha256(data).hexdigest()


def test_eval2_digests_bind_the_named_source_files(w):
    corpus_ref, _ = w.corpus(val=16)
    value = versions.read_version(w.m.d.store, w.scope, w.evaluator(corpus_ref))
    assert value["analysis_code_digest"] == sha(versions.ANALYSIS_FILES)
    assert value["service_code_digest"] == sha(versions.SERVICE_FILES)
    assert versions.code_digests() == {
        "analysis_code_digest": value["analysis_code_digest"],
        "service_code_digest": value["service_code_digest"],
    }


def test_eval2_refuses_unqualified_or_unauthorised_writers(w):
    corpus_ref, _ = w.corpus(val=16)
    store, reviewer = w.m.d.store, w.m.reviewer
    good = requalification(w)

    def write(actor=reviewer, requal=good, corpus=corpus_ref, version="eval-2"):
        return versions.write_version(
            store, actor, corpus_ref=corpus, requalification_ref=requal, version=version
        )

    proposer = dataclasses.replace(reviewer, permissions=reviewer.permissions | {"harness.propose"})
    service = dataclasses.replace(reviewer, kind="service")
    fault("FORBIDDEN", write, actor=proposer)
    fault("FORBIDDEN", write, actor=service)
    hold("EVALUATOR_UNQUALIFIED", write, requal=requalification(w, all_equal=False))
    hold("EVALUATOR_UNQUALIFIED", write, requal=requalification(w, version="eval-3"))
    hold("EVALUATOR_UNQUALIFIED", write, requal=corpus_ref)
    fault("EVALUATOR_VERSION", write, corpus=good)
    for bad in ("eval-0", "v2", "eval-", "evaluator-2"):
        fault("EVALUATOR_VERSION", write, version=bad)
    assert versions.current_version_ref(store, w.scope) is None
    ref = write()
    assert write() == ref


def test_eval2_same_inputs_keep_the_ref_and_new_inputs_add_a_revision(w):
    corpus_ref, _ = w.corpus(val=16)
    store, reviewer = w.m.d.store, w.m.reviewer
    first = versions.write_version(
        store, reviewer, corpus_ref=corpus_ref, requalification_ref=requalification(w)
    )
    second = versions.write_version(
        store, reviewer, corpus_ref=corpus_ref, requalification_ref=requalification(w)
    )
    assert second["id"] == first["id"] and second["revision"] == 2
    assert versions.current_version_ref(store, w.scope) == second
    assert versions.read_version(store, w.scope, first)["version"] == "eval-2"


def test_eval2_changed_code_is_a_new_version_never_a_silent_rewrite(w, monkeypatch):
    corpus_ref, _ = w.corpus(val=16)
    store, reviewer = w.m.d.store, w.m.reviewer
    fake = {
        "analysis_code_digest": "sha256:" + "1" * 64,
        "service_code_digest": "sha256:" + "2" * 64,
    }
    with monkeypatch.context() as patch:
        patch.setattr(versions, "code_digests", lambda: dict(fake))
        stale = versions.write_version(
            store, reviewer, corpus_ref=corpus_ref, requalification_ref=requalification(w)
        )
        assert versions.current_version_ref(store, w.scope) == stale
    assert versions.current_version_ref(store, w.scope) is None
    hold(
        "EVALUATOR_CHANGED",
        versions.write_version,
        store,
        reviewer,
        corpus_ref=corpus_ref,
        requalification_ref=requalification(w),
    )
    stored = versions.read_version(store, w.scope, stale)
    hold("EVALUATOR_CHANGED", versions.check_current, stored)
    versions.check_current({**stored, **versions.code_digests()})
    other = {**versions.code_digests(), "service_code_digest": "sha256:" + "0" * 64}
    hold("EVALUATOR_CHANGED", versions.check_current, {**stored, **other})


def test_eval2_read_refuses_another_kind_and_a_stage_plan_pins_only_a_version(w):
    corpus_ref, _ = w.corpus(val=16)
    hold("EVALUATOR_UNQUALIFIED", versions.read_version, w.m.d.store, w.scope, corpus_ref)
    fault("EVALUATOR_VERSION", versions.validate_version, {"schema": "amplai.evaluator-version.v1"})
    s = w.setup(val=16)
    plan = w.experiment(s, policy(s.ev))
    assert w.freeze(plan)["id"] == plan["experiment_id"]
    plan = w.experiment(s, policy(corpus_ref))
    hold("EVALUATOR_UNQUALIFIED", w.freeze, plan)


# --- dispatch helpers (7.7) -----------------------------------------------------------------


def test_run_bounded_single_worker_runs_inline_in_order():
    main = threading.get_ident()
    seen, threads = [], set()

    def execute(item):
        threads.add(threading.get_ident())
        return item * 2

    run_bounded(
        range(5),
        parallel=1,
        dispatch=lambda item: item,
        execute=execute,
        record=lambda ctx, result: seen.append((ctx, result)),
        stopped=lambda: False,
    )
    assert seen == [(i, 2 * i) for i in range(5)]
    assert threads == {main}


@pytest.mark.parametrize("parallel", [1, 2, 3])
def test_run_bounded_dispatch_none_ends_admission_but_finishes_running(parallel):
    seen = []
    run_bounded(
        range(10),
        parallel=parallel,
        dispatch=lambda item: item if item < 3 else None,
        execute=lambda item: item,
        record=lambda ctx, result: seen.append(result),
        stopped=lambda: False,
    )
    assert sorted(seen) == [0, 1, 2]


def test_run_bounded_stops_dispatching_once_stopped():
    seen = []
    run_bounded(
        range(10),
        parallel=1,
        dispatch=lambda item: item,
        execute=lambda item: item,
        record=lambda ctx, result: seen.append(result),
        stopped=lambda: len(seen) >= 2,
    )
    assert seen == [0, 1]


def test_run_bounded_parallel_caps_concurrency_and_records_on_the_caller_thread():
    main = threading.get_ident()
    lock = threading.Lock()
    state = {"active": 0, "peak": 0}
    recorded_on = set()

    def execute(item):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.03)
        with lock:
            state["active"] -= 1
        return item

    seen = []

    def record(ctx, result):
        recorded_on.add(threading.get_ident())
        seen.append(result)

    run_bounded(
        range(9),
        parallel=3,
        dispatch=lambda item: item,
        execute=execute,
        record=record,
        stopped=lambda: False,
    )
    assert sorted(seen) == list(range(9))
    assert recorded_on == {main}
    assert 1 < state["peak"] <= 3


@pytest.mark.parametrize("parallel", [1, 2])
def test_run_bounded_propagates_an_executor_exception(parallel):
    def execute(item):
        raise ValueError("boom")

    with pytest.raises(ValueError):
        run_bounded(
            range(3),
            parallel=parallel,
            dispatch=lambda item: item,
            execute=execute,
            record=lambda ctx, result: None,
            stopped=lambda: False,
        )


def test_effective_parallel_is_the_smallest_cap_and_never_above_four():
    assert effective_parallel(8, 2, 4) == 2
    assert effective_parallel(3, 4, 64) == 3
    assert effective_parallel(9, 64, 64) == 4
    for bad in (0, -1, True, 1.5, "2"):
        fault("EVAL_PARALLEL", effective_parallel, bad, 4)


def test_arm_order_rotates_three_arms_and_keeps_the_two_arm_alternation():
    two, three = ("baseline", "candidate"), ("baseline", "candidate", "reference")
    assert [arm_order(two, r) for r in range(3)] == [
        ["baseline", "candidate"],
        ["candidate", "baseline"],
        ["baseline", "candidate"],
    ]
    assert [arm_order(three, r) for r in range(4)] == [
        ["baseline", "candidate", "reference"],
        ["candidate", "reference", "baseline"],
        ["reference", "baseline", "candidate"],
        ["baseline", "candidate", "reference"],
    ]


def test_service_constructor_refuses_a_bad_parallel_cap(w):
    meta = w.m
    for bad in (0, 5, True, 2.0):
        fault(
            "EVAL_PARALLEL",
            EvaluationService,
            meta.d.store,
            meta.d.contracts,
            meta.d.artifacts,
            approval_check=meta.check,
            executor_id="x",
            max_parallel=bad,
        )


# --- Q-13: stage subsets (IC-15) ----------------------------------------------------------


def test_q13_versioned_subset_freezes_and_runs_only_the_selected_cases(w):
    s = w.setup(val=24)
    expected = [f"val-{i:02d}" for i in range(16)]
    # 24 cases in domains alpha, beta, gamma (by index): round-robin to 16 gives alpha 6, beta 5,
    # gamma 5, which are the first sixteen cases in corpus order.
    plan = w.experiment(s, policy(s.ev), ids=expected, sampling=subset("all_v1", 16))
    ref = w.freeze(plan)
    report_ref = w.run(ref, Scripted(w, w.roles(s)))
    trials = w.trials(report_ref)
    assert {t["task_id"] for t in trials} == set(expected)
    assert len(trials) == 32
    assert w.analysis(report_ref)["task_count"] == 16


@pytest.mark.parametrize(
    "mutate",
    [
        lambda ids: [*ids[:-1], "val-20"],
        lambda ids: [ids[1], ids[0], *ids[2:]],
        lambda ids: [*ids[:-1], ids[0]],
        lambda ids: ids[:-1],
        lambda ids: [*ids, "val-16"],
        lambda ids: list(reversed(ids)),
        lambda ids: [],
    ],
    ids=["changed", "reordered", "duplicate", "missing", "extra", "reversed", "empty"],
)
def test_q13_a_changed_or_reordered_subset_holds_sampling_changed(w, mutate):
    s = w.setup(val=24)
    expected = [f"val-{i:02d}" for i in range(16)]
    plan = w.experiment(s, policy(s.ev), ids=mutate(expected), sampling=subset("all_v1", 16))
    hold("SAMPLING_CHANGED", w.freeze, plan)


def test_q13_legacy_plans_keep_full_split_equality(w):
    s = w.setup(val=24)
    ids = w.ids(s, n=16)
    plan = w.experiment(s, policy(None), ids=ids, sampling=subset("all_v1", 16))
    hold("SAMPLING_CHANGED", w.freeze, plan)
    plan = w.experiment(s, policy(None, minimum_tasks=24), sampling=subset("all_v1", 16))
    ref = w.freeze(plan)
    report_ref = w.run(ref, Scripted(w, w.roles(s)))
    assert len(w.trials(report_ref)) == 48
    assert w.analysis(report_ref)["task_count"] == 24


def test_q13_versioned_plan_without_case_rule_also_needs_the_whole_split(w):
    s = w.setup(val=20)
    hold("SAMPLING_CHANGED", w.freeze, w.experiment(s, policy(s.ev), ids=w.ids(s, n=16)))
    assert w.freeze(w.experiment(s, policy(s.ev)))


def test_q13_informative_subset_follows_the_calibration_summary(w):
    s = w.setup(val=24)
    classes = {cid: "saturated" for cid in w.ids(s)}
    for cid in ("val-00", "val-04", "val-08", "val-09", "val-13", "val-14", "val-20"):
        classes[cid] = "informative"
    classes["val-01"] = "unsolved"
    classes["val-02"] = "unknown"
    summary = fake_summary(w, {"cell-a": classes, "cell-b": {}})
    # domains: alpha (0, 9), beta (4, 13), gamma (8, 14, 20); round-robin to 5 keeps
    # 00, 04, 08, 09, 13 in corpus order.
    expected = ["val-00", "val-04", "val-08", "val-09", "val-13"]
    sampling = subset("informative_v1", 5, summary=summary)
    ref = w.freeze(w.experiment(s, policy(s.ev), ids=expected, sampling=sampling))
    report_ref = w.run(ref, Scripted(w, w.roles(s)))
    assert {t["task_id"] for t in w.trials(report_ref)} == set(expected)
    # a cell whose summary row holds no informative task selects nothing; a cell without a row
    # is a malformed sampling plan
    empty = subset("informative_v1", 5, summary=summary, cell="cell-b")
    hold("SAMPLING_CHANGED", w.freeze, w.experiment(s, policy(s.ev), ids=expected, sampling=empty))
    absent = subset("informative_v1", 5, summary=summary, cell="cell-z")
    fault("SAMPLING_PLAN", w.freeze, w.experiment(s, policy(s.ev), ids=expected, sampling=absent))


def fake_summary(w, classes_by_cell):
    cells = {
        cell: {"tasks": {cid: {"class": klass} for cid, klass in classes.items()}}
        for cell, classes in classes_by_cell.items()
    }
    value = {"schema": "amplai.calibration-summary.v1", "cells": cells}
    return w.m.d.put("calibration-summary", new_id("calsum"), value)


@pytest.mark.parametrize(
    "change",
    [
        {"case_rule": "bogus"},
        {"max_tasks": 0},
        {"max_tasks": True},
        {"max_tasks": "16"},
        {"cell_id": ""},
        {"cell_id": None},
        {"case_rule": "informative_v1", "calibration_summary_ref": None},
        {"calibration_summary_ref": "not-a-ref"},
    ],
)
def test_q13_a_malformed_subset_declaration_is_a_sampling_plan_fault(w, change):
    s = w.setup(val=24)
    sampling = {**subset("all_v1", 16), **change}
    plan = w.experiment(s, policy(s.ev), ids=w.ids(s, n=16), sampling=sampling)
    fault("SAMPLING_PLAN", w.freeze, plan)


def test_q13_the_summary_ref_must_name_a_calibration_summary(w):
    s = w.setup(val=24)
    sampling = subset("informative_v1", 16, summary=s.corpus_ref)
    plan = w.experiment(s, policy(s.ev), ids=w.ids(s, n=16), sampling=sampling)
    fault("SAMPLING_PLAN", w.freeze, plan)


def test_q13_run_recomputes_the_subset_and_holds_when_it_changed(w, monkeypatch):
    s = w.setup(val=24)
    expected = w.ids(s, n=16)
    plan = w.experiment(s, policy(s.ev), ids=expected, sampling=subset("all_v1", 16))
    ref = w.freeze(plan)
    chosen = Scripted(w, w.roles(s))
    with monkeypatch.context() as patch:
        patch.setattr(calibration, "select_cases", lambda cases, **kw: expected[1:])
        hold("SAMPLING_CHANGED", w.run, ref, chosen)
    assert w.state(plan) == "frozen"
    assert chosen.calls == []
    report_ref = w.run(ref, chosen)
    assert w.report(report_ref)["experiment_ref"] == ref


def test_q13_run_refuses_another_split_than_the_frozen_one(w):
    s = w.setup(val=20, dev=12)
    ref = w.freeze(w.experiment(s, policy(s.ev)))
    hold("SAMPLING_FROZEN", w.run, ref, Scripted(w, w.roles(s)), split="development")


def test_q13_holdout_subset_still_charges_every_holdout_case(w):
    s = w.setup(hold_n=20)
    ids = w.ids(s, "holdout", 16)
    pol = confirm(s.ev, 16)
    plan = w.experiment(s, pol, split="holdout", ids=ids, sampling=subset("all_v1", 16))
    ref = w.freeze(plan)
    report_ref = w.run(ref, Scripted(w, w.roles(s)), split="holdout")
    assert {t["task_id"] for t in w.trials(report_ref)} == set(ids)
    assert w.report(report_ref)["verdict"] == "pass"
    store = w.m.d.store
    corpus = store.get(w.scope, "eval-corpus", s.corpus_ref)
    charged = [c for c in corpus["cases"] if c["split"] == "holdout"]
    assert len(charged) == 20
    for case in charged:
        key = "case-" + case["artifact_ref"]["digest"][7:]
        head = store.head(w.scope, "holdout-use", key)
        assert head["data"]["experiment_refs"] == [ref]
    assert store.head(w.scope, "holdout-use", s.corpus_ref["id"])["data"]["experiment_refs"] == [
        ref
    ]


# --- Q-08 / IC-09: minimum sample of confirmatory plans -------------------------------------


@pytest.mark.parametrize("margin,needed", [(0.25, 16), (0.2, 21), (0.15, 29), (0.1, 46)])
def test_q08_confirmatory_noninferiority_is_refused_below_the_minimum_sample(w, margin, needed):
    s = w.setup(val=needed)
    for n, accepted in ((needed - 1, False), (needed, True)):
        pol = confirm(s.ev, n, noninferiority_margin=margin)
        plan = w.experiment(s, pol, ids=w.ids(s, n=n), sampling=subset("all_v1", n))
        if accepted:
            assert w.freeze(plan)["id"] == plan["experiment_id"]
        else:
            error = hold("SAMPLE_UNDERPOWERED", w.freeze, plan)
            assert error.details == {"task_count": n}
            with pytest.raises(RuntimeFault) as gone:
                w.state(plan)
            assert gone.value.code == "NOT_FOUND"


def test_q08_confirmatory_superiority_needs_enough_tasks_for_its_win_count(w):
    s = w.setup(val=20)
    assert min_wins_for_superiority(20, 0.05, 0.95) == 10
    need = min_wins_for_superiority(5, 0.05, 0.95)
    assert need is None or need > 5
    few = confirm(s.ev, 5, endpoint="superiority", minimum_effect=0.05)
    plan = w.experiment(s, few, ids=w.ids(s, n=5), sampling=subset("all_v1", 5))
    hold("SAMPLE_UNDERPOWERED", w.freeze, plan)
    enough = confirm(s.ev, 20, endpoint="superiority", minimum_effect=0.05)
    assert w.freeze(w.experiment(s, enough))


def test_q08_confirmatory_margin_zero_can_never_pass_its_rule(w):
    s = w.setup(val=30)
    pol = confirm(s.ev, 30, noninferiority_margin=0)
    hold("SAMPLE_UNDERPOWERED", w.freeze, w.experiment(s, pol))


def test_q08_exploratory_plans_are_never_refused_for_size(w):
    s = w.setup(dev=12, val=20)
    twelve = policy(s.ev, minimum_tasks=2)
    plan = w.experiment(
        s, twelve, split="development", sampling=subset("all_v1", 12, stage="screening")
    )
    ref = w.freeze(plan)
    report_ref = w.run(ref, Scripted(w, w.roles(s)), split="development")
    analysis = w.analysis(report_ref)
    assert analysis["task_count"] == 12
    assert analysis["decision_class"] == "inconclusive"
    assert analysis["confidence_interval"][0] == pytest.approx(-0.295, abs=0.001)
    assert w.report(report_ref)["verdict"] == "inconclusive"
    two = policy(s.ev, minimum_tasks=2)
    tiny = w.experiment(s, two, ids=w.ids(s, n=2), sampling=subset("all_v1", 2, stage="ablation"))
    assert w.freeze(tiny)


def test_q08_legacy_confirmatory_plans_are_never_power_checked(w):
    s = w.setup(val=15)
    pol = confirm(None, 15, sequential_rule="fixed_sample_safety_abort_only")
    assert w.freeze(w.experiment(s, pol))


def test_q08_confirmatory_development_split_is_refused_before_power(w):
    s = w.setup(dev=16)
    plan = w.experiment(s, confirm(s.ev, 16), split="development")
    hold("CONFIRMATORY_SPLIT", w.freeze, plan)


def test_q08_confirmatory_plan_states_its_mde_line(w):
    s = w.setup(val=16)
    bad = confirm(s.ev, 16, sample_rationale="Sixteen tasks without the line.")
    fault("SAMPLE_RATIONALE", w.freeze, w.experiment(s, bad))
    bad = confirm(s.ev, 16, mde=None)
    fault("SAMPLE_RATIONALE", w.freeze, w.experiment(s, bad))


# --- Q-06: budget-matched reference arm (7.4) -----------------------------------------------


def reference_arm(s, n=2):
    return {"composition_ref": s.ref, "kind": "best_of_n", "n": n, "cost_match": "tokens"}


def test_q06_a_declared_reference_arm_without_a_validator_holds(w):
    s = w.setup(val=16, reference=True)
    pol = policy(s.ev, reference_arm=reference_arm(s))
    plan = w.experiment(s, pol, sampling={"arms": ARMS3})
    hold("REFERENCE_ARM", w.freeze, plan)


def test_q06_the_validator_decides_and_a_refusal_freezes_nothing(w):
    s = w.setup(val=16, reference=True)
    pol = policy(s.ev, reference_arm=reference_arm(s))
    w.m.eval.reference_validator = FakeValidator(refuse="another cell")
    plan = w.experiment(s, pol, sampling={"arms": ARMS3})
    hold("REFERENCE_ARM", w.freeze, plan)
    with pytest.raises(RuntimeFault) as gone:
        w.state(plan)
    assert gone.value.code == "NOT_FOUND"
    accepting = FakeValidator()
    w.m.eval.reference_validator = accepting
    assert w.freeze(plan)["id"] == plan["experiment_id"]
    ((scope, seen_plan, seen_policy),) = accepting.calls
    assert scope == w.scope
    assert seen_plan["experiment_id"] == plan["experiment_id"]
    assert seen_policy["reference_arm"]["n"] == 2


def test_q06_shape_errors_never_reach_the_validator(w):
    s = w.setup(val=16, reference=True)
    validator = FakeValidator()
    w.m.eval.reference_validator = validator
    for arm in (reference_arm(s, 4), reference_arm(s, 0), {**reference_arm(s), "kind": "vote"}):
        plan = w.experiment(s, policy(s.ev, reference_arm=arm), sampling={"arms": ARMS3})
        hold("REFERENCE_ARM", w.freeze, plan)
    assert validator.calls == []


def test_q06_sampling_arms_must_match_the_analysis_plan(w):
    s = w.setup(val=16, reference=True)
    w.m.eval.reference_validator = FakeValidator()
    with_reference = policy(s.ev, reference_arm=reference_arm(s))
    hold(
        "REFERENCE_ARM",
        w.freeze,
        w.experiment(s, with_reference, sampling={"arms": ["baseline", "candidate"]}),
    )
    hold("REFERENCE_ARM", w.freeze, w.experiment(s, policy(s.ev), sampling={"arms": ARMS3}))
    odd = {"order_rule": "shuffled"}
    fault("SAMPLING_PLAN", w.freeze, w.experiment(s, policy(s.ev), sampling=odd))


def test_q06_a_legacy_plan_cannot_declare_a_reference_arm(w):
    s = w.setup(val=16, reference=True)
    plan = w.experiment(s, policy(None, reference_arm=reference_arm(s)))
    fault("ANALYSIS_PLAN", w.freeze, plan)


@pytest.mark.parametrize(
    "reference,count,ok",
    [(True, 42, True), (True, 43, False), (False, 64, True), (False, 65, False)],
)
def test_q06_report_capacity_counts_three_arms_when_a_reference_exists(w, reference, count, ok):
    s = w.setup(val=count, reference=reference)
    w.m.eval.reference_validator = FakeValidator()
    extra = {"arms": ARMS3} if reference else {}
    pol = policy(s.ev, repeats_per_task=2)
    if reference:
        pol["reference_arm"] = reference_arm(s)
    plan = w.experiment(s, pol, sampling=extra)
    if ok:
        assert w.freeze(plan)["id"] == plan["experiment_id"]
    else:
        hold("REPORT_CAPACITY", w.freeze, plan)


def test_q06_arm_order_rotates_by_repeat_in_the_dispatch_sequence(w):
    s = w.setup(val=3, reference=True)
    w.m.eval.reference_validator = FakeValidator()
    three = policy(s.ev, repeats_per_task=3, reference_arm=reference_arm(s))
    ref = w.freeze(w.experiment(s, three, sampling={"arms": ARMS3}))
    executor = Scripted(w, w.roles(s))
    w.run(ref, executor)
    per_case = [c for c in executor.calls if c[1] == "val-00"]
    assert [(rep, role) for role, _, rep in per_case] == [
        (0, "baseline"),
        (0, "candidate"),
        (0, "reference"),
        (1, "candidate"),
        (1, "reference"),
        (1, "baseline"),
        (2, "reference"),
        (2, "baseline"),
        (2, "candidate"),
    ]
    assert [c[1] for c in executor.calls[::9]] == ["val-00", "val-01", "val-02"]
    two = policy(s.ev, repeats_per_task=3)
    ref = w.freeze(w.experiment(s, two))
    executor = Scripted(w, w.roles(s))
    w.run(ref, executor)
    per_case = [c for c in executor.calls if c[1] == "val-00"]
    assert [(rep, role) for role, _, rep in per_case] == [
        (0, "baseline"),
        (0, "candidate"),
        (1, "candidate"),
        (1, "baseline"),
        (2, "baseline"),
        (2, "candidate"),
    ]


def reference_run(w, outcome, *, n=16):
    s = w.setup(val=n, reference=True)
    w.m.eval.reference_validator = FakeValidator()
    pol = confirm(s.ev, n, reference_arm=reference_arm(s))
    ref = w.freeze(w.experiment(s, pol, sampling={"arms": ARMS3}))
    report_ref = w.run(ref, Scripted(w, w.roles(s), outcome))
    return w.report(report_ref), w.analysis(report_ref), w.trials(report_ref)


def test_q06_a_candidate_dominated_by_the_reference_is_inconclusive_with_the_reason(w):
    # baseline and candidate fail every task, the reference passes every one: the primary
    # comparison is a tie (non_inferior, would pass) but vs_reference is a regression.
    report, analysis, trials = reference_run(w, lambda role, cid, rep: role == "reference")
    assert len(trials) == 48
    assert analysis["decision_class"] == "non_inferior"
    assert analysis["vs_reference"]["decision_class"] == "regression"
    assert "dominated_by_budget_matched_reference" in analysis["reasons"]
    assert report["verdict"] == "inconclusive" and analysis["verdict"] == "inconclusive"


def test_q06_a_candidate_as_good_as_the_reference_passes(w):
    report, analysis, trials = reference_run(w, lambda role, cid, rep: role != "baseline")
    assert analysis["vs_reference"]["decision_class"] == "non_inferior"
    assert "dominated_by_budget_matched_reference" not in analysis["reasons"]
    assert report["verdict"] == "pass" and analysis["decision_class"] == "non_inferior"
    assert {t["arm"] for t in trials} == set(ARMS3)


def test_q06_a_failing_primary_comparison_stays_a_failure(w):
    report, analysis, _ = reference_run(w, lambda role, cid, rep: role != "candidate")
    assert analysis["decision_class"] == "regression"
    assert analysis["vs_reference"]["decision_class"] == "regression"
    assert report["verdict"] == "fail"


# --- Q-09: parallel runs, safety stops, concurrency cap --------------------------------------


def trial_keys(trials):
    return sorted((t["task_id"], t["arm"], t["repeat"]) for t in trials)


def test_q09_parallel_run_has_the_trial_keys_and_analysis_of_the_sequential_run(w):
    s = w.setup(val=16)
    win = lambda role, cid, rep: role == "candidate" or cid not in {"val-03", "val-07"}  # noqa: E731
    executor = Scripted(w, w.roles(s), win)
    executor.prime(s.cases, 2)
    results = {}
    for parallel in (1, 4):
        ref = w.freeze(w.experiment(s, policy(s.ev, repeats_per_task=2)))
        report_ref = w.run(ref, executor, parallel=parallel)
        results[parallel] = (w.trials(report_ref), w.analysis(report_ref), w.report(report_ref))
    (seq_trials, seq_analysis, seq_report), (par_trials, par_analysis, par_report) = (
        results[1],
        results[4],
    )
    assert len(seq_trials) == 64
    assert trial_keys(seq_trials) == trial_keys(par_trials)
    for field in ("verdict", "decision_class", "task_count", "confidence_interval", "reasons"):
        assert seq_analysis[field] == par_analysis[field]
    assert seq_analysis["candidate_minus_baseline"] == par_analysis["candidate_minus_baseline"]
    assert seq_analysis["pass_k"] == par_analysis["pass_k"]
    assert seq_analysis["per_task"] == par_analysis["per_task"]
    assert seq_report["verdict"] == par_report["verdict"]
    assert seq_report["run_refs"] != par_report["run_refs"]
    assert {t["arm"] for t in par_trials} == {"baseline", "candidate"}


def test_q09_the_first_dispatches_follow_the_deterministic_order(w):
    s = w.setup(val=16)
    executor = Scripted(w, w.roles(s), delay=0.05)
    executor.prime(s.cases, 1)
    ref = w.freeze(w.experiment(s, policy(s.ev)))
    w.run(ref, executor, parallel=4)
    first_four = {("baseline", "val-00", 0), ("candidate", "val-00", 0),
                  ("baseline", "val-01", 0), ("candidate", "val-01", 0)}  # fmt: skip
    assert set(executor.calls[:4]) == first_four
    assert len(executor.calls) == 32


@pytest.mark.parametrize(
    "budget_cap,service_cap,asked,expected_peak",
    [(2, 4, 4, 2), (4, 2, 4, 2), (4, 4, 1, 1), (4, 4, 3, 3)],
)
def test_q09_concurrency_never_exceeds_the_smallest_cap(
    w, budget_cap, service_cap, asked, expected_peak
):
    s = w.setup(val=12)
    w.m.eval.max_parallel = service_cap
    executor = Scripted(w, w.roles(s), delay=0.05)
    executor.prime(s.cases, 1)
    budget = {**BUDGET, "max_parallel_works": budget_cap}
    ref = w.freeze(w.experiment(s, policy(s.ev), budget=budget))
    report_ref = w.run(ref, executor, parallel=asked)
    assert len(w.trials(report_ref)) == 24
    assert executor.peak <= expected_peak
    if expected_peak > 1:
        assert executor.peak > 1


def test_q09_a_candidate_safety_failure_stops_dispatch_and_fails(w):
    s = w.setup(val=16)

    def outcome(role, cid, rep):
        return (True, 1) if (role, cid) == ("candidate", "val-00") else True

    ref = w.freeze(w.experiment(s, policy(s.ev)))
    report_ref = w.run(ref, Scripted(w, w.roles(s), outcome))
    trials = w.trials(report_ref)
    assert [(t["task_id"], t["arm"]) for t in trials] == [
        ("val-00", "baseline"),
        ("val-00", "candidate"),
    ]
    analysis = w.analysis(report_ref)
    assert w.report(report_ref)["verdict"] == "fail"
    assert analysis["decision_class"] == "regression"
    assert {"safety_failure", "safety_or_unknown_effect"} <= set(analysis["reasons"])
    assert analysis["safety_failures_by_arm"] == {"baseline": 0, "candidate": 1}
    assert w.report(report_ref)["safety_gate_results"][0]["outcome"] == "fail"


def test_q09_a_baseline_only_safety_failure_is_inconclusive_unlike_legacy(w):
    s = w.setup(val=16)

    def outcome(role, cid, rep):
        return (True, 1) if role == "baseline" else True

    versioned = w.freeze(w.experiment(s, policy(s.ev)))
    report_ref = w.run(versioned, Scripted(w, w.roles(s), outcome))
    analysis = w.analysis(report_ref)
    assert len(w.trials(report_ref)) == 1
    assert analysis["decision_class"] == "inconclusive"
    assert "baseline_safety_failure" in analysis["reasons"]
    assert w.report(report_ref)["verdict"] == "aborted"
    legacy = w.freeze(w.experiment(s, policy(None)))
    report_ref = w.run(legacy, Scripted(w, w.roles(s), outcome))
    assert w.report(report_ref)["verdict"] == "fail"
    assert "decision_class" not in w.analysis(report_ref)


def test_q09_parallel_safety_failure_stops_new_dispatch(w):
    s = w.setup(val=16)

    def outcome(role, cid, rep):
        return (True, 1) if (role, cid) == ("candidate", "val-00") else True

    executor = Scripted(w, w.roles(s), outcome, delay=0.05)
    executor.prime(s.cases, 1)
    ref = w.freeze(w.experiment(s, policy(s.ev)))
    report_ref = w.run(ref, executor, parallel=4)
    assert 2 <= len(w.trials(report_ref)) <= 2 + 2 * 4
    assert len(w.trials(report_ref)) < 32
    assert w.report(report_ref)["verdict"] == "fail"


def test_q09_a_lost_executor_response_is_an_unknown_effect_and_stops(w):
    s = w.setup(val=16)
    executor = Scripted(w, w.roles(s), raises=lambda role, cid, rep: cid == "val-01")
    proposal_id = s.prop["proposal_id"]
    ref = w.freeze(w.experiment(s, policy(s.ev)))
    report_ref = w.run(ref, executor)
    trials = w.trials(report_ref)
    assert trials[-1]["unknown_effects"] == 1 and trials[-1]["error_type"] == "RuntimeError"
    assert {t["task_id"] for t in trials} == {"val-00", "val-01"}
    assert w.report(report_ref)["verdict"] == "aborted"
    assert w.budget(proposal_id)["data"]["allocations"]
    hold("EXPERIMENT_REPLAY", w.run, ref, executor)


class HeldBeforeRun(Scripted):
    """val-01 holds ``code`` with the executor's ``NOT_RUN_EVIDENCE`` (None: none)."""

    def __init__(self, world, roles, evidence, code="TRIAL_VERIFIER"):
        super().__init__(world, roles)
        self.evidence, self.code = evidence, code

    def __call__(self, composition, case, repeat, mode):
        if case["case_id"] == "val-01":
            exc = Hold(self.code, "held before the run")
            if self.evidence is not None:
                setattr(exc, NOT_RUN_EVIDENCE, self.evidence)
            raise exc
        return super().__call__(composition, case, repeat, mode)


def held_evidence(planner_mode="fixed", base_checks=0, runs=(), dispatches=()):
    return {"goal_id": "goal-held", "planner_mode": planner_mode, "base_checks": base_checks,
            "runs": list(runs), "dispatches": list(dispatches)}  # fmt: skip


def test_a_hold_before_any_goal_ran_is_a_missing_outcome_and_the_experiment_continues(w):
    # operator decision 2026-10-09: the fixed draft held, with no run, no dispatch and no base
    # check during the call
    s = w.setup(val=16)
    evidence = held_evidence()
    executor = HeldBeforeRun(w, w.roles(s), evidence)
    proposal_id = s.prop["proposal_id"]
    ref = w.freeze(w.experiment(s, policy(s.ev)))
    report_ref = w.run(ref, executor)
    trials = w.trials(report_ref)
    held = [t for t in trials if t["task_id"] == "val-01"]
    assert len(held) == 2  # both arms ran into the Hold; neither stopped the experiment
    assert len(trials) == 32  # 16 tasks x 2 arms
    for trial in held:
        assert trial["error_type"] == "Hold" and trial["error_code"] == "TRIAL_VERIFIER"
        assert trial["unknown_effects"] == 0 and trial["success"] is None
        assert trial["not_run"] == {"goal_id": "goal-held", "runs": 0, "dispatches": 0}
        assert trial["outcome_missing"] == "not_run" and "charged_tokens" in trial
        head = w.m.d.store.head(w.scope, "eval-trial", trial["trial_id"])
        assert head["state"] == "observed"
        allocation = w.budget(proposal_id)["data"]["allocations"][trial["trial_id"]]
        assert allocation["status"] != "unknown"
    assert w.report(report_ref)["verdict"] != "aborted"
    assert "missing_or_unknown_trials" in w.analysis(report_ref)["reasons"]


@pytest.mark.parametrize(
    ("code", "evidence"),
    [("TRIAL_VERIFIER", None),
     ("TRIAL_VERIFIER", held_evidence(runs=["run-1"], dispatches=["dispatch-1"])),
     ("TRIAL_VERIFIER", held_evidence(base_checks=1)),
     # the IC-18 review finding: a real planner turn timed out (readonly_turn.py docker kill)
     ("PLANNER_TIMEOUT", held_evidence(planner_mode="real"))],
)  # fmt: skip
def test_a_hold_without_evidence_that_nothing_ran_is_an_unknown_effect_and_stops(w, code, evidence):
    s = w.setup(val=16)
    executor = HeldBeforeRun(w, w.roles(s), evidence, code)
    ref = w.freeze(w.experiment(s, policy(s.ev)))
    report_ref = w.run(ref, executor)
    trials = w.trials(report_ref)
    last = trials[-1]
    assert last["task_id"] == "val-01" and last["error_code"] == code
    assert last["unknown_effects"] == 1 and "not_run" not in last
    assert w.m.d.store.head(w.scope, "eval-trial", last["trial_id"])["state"] == "unknown"
    assert w.report(report_ref)["verdict"] == "aborted"


def test_q09_a_proposer_identity_can_neither_freeze_nor_run(w):
    s = w.setup(val=16)
    actor = dataclasses.replace(
        w.m.reviewer, permissions=w.m.reviewer.permissions | {"harness.propose"}
    )
    plan = w.experiment(s, policy(s.ev))
    hold("SELF_APPROVAL", w.freeze, plan, actor=actor)
    ref = w.freeze(plan)
    hold("SELF_APPROVAL", w.run, ref, Scripted(w, w.roles(s)), actor=actor)


# --- Q-15: per-experiment max_trial_tokens (7.1, 5.3) ---------------------------------------


def ceilings(w, proposal_id):
    allocations = w.budget(proposal_id)["data"]["allocations"]
    return {a["token_ceiling"] for a in allocations.values()}


def with_executor_tokens(w, tokens):
    meta = w.m
    meta.eval.executor_policy = dataclasses.replace(
        meta.eval.executor_policy, max_trial_tokens=tokens
    )


@pytest.mark.parametrize(
    "executor_value,plan_value,reserved",
    [(100, 40, 40), (100, 500, 100), (100, None, 100), (0, 40, 0), (100, 100, 100)],
)
def test_q15_the_reservation_is_the_smaller_of_policy_and_plan(
    w, executor_value, plan_value, reserved
):
    s = w.setup(val=4)
    with_executor_tokens(w, executor_value)
    extra = {} if plan_value is None else {"max_trial_tokens": plan_value}
    ref = w.freeze(w.experiment(s, policy(s.ev, **extra)))
    report_ref = w.run(ref, Scripted(w, w.roles(s)))
    assert len(w.trials(report_ref)) == 8
    assert ceilings(w, s.prop["proposal_id"]) == {reserved}


def test_q15_a_plan_cap_lets_a_run_fit_a_root_budget_the_policy_value_would_break(w):
    small = {**BUDGET, "max_tokens": 50}
    s = w.setup(val=4)
    with_executor_tokens(w, 100)
    blocked = w.experiment(s, policy(s.ev), budget=small)
    ref = w.freeze(blocked)
    hold("META_TOKEN_BUDGET", w.run, ref, Scripted(w, w.roles(s)))
    assert w.state(blocked) == "aborted"
    other = w.setup(val=4)
    capped = w.experiment(other, policy(other.ev, max_trial_tokens=40), budget=small)
    report_ref = w.run(w.freeze(capped), Scripted(w, w.roles(other)))
    assert len(w.trials(report_ref)) == 8
    assert ceilings(w, other.prop["proposal_id"]) == {40}


def test_q15_usage_above_the_plan_cap_is_an_overrun_and_stops_the_run(w):
    s = w.setup(val=4)
    with_executor_tokens(w, 100)
    executor = Scripted(w, w.roles(s), tokens=(30, 30))
    capped = w.freeze(w.experiment(s, policy(s.ev, max_trial_tokens=40)))
    report_ref = w.run(capped, executor)
    assert len(w.trials(report_ref)) == 1
    assert "budget_overrun_or_unknown_usage" in w.analysis(report_ref)["reasons"]
    assert w.report(report_ref)["verdict"] == "aborted"
    # a prior overrun holds its proposal root, so the roomy plan runs under a new proposal
    again = w.proposal(s.base, s.cand)
    roomy = w.freeze(w.experiment(s, policy(s.ev, max_trial_tokens=100), prop=again))
    report_ref = w.run(roomy, executor)
    assert len(w.trials(report_ref)) == 8


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "5"])
def test_q15_an_invalid_plan_cap_is_refused_at_freeze(w, bad):
    s = w.setup(val=4)
    for version in (s.ev, None):
        plan = w.experiment(s, policy(version, max_trial_tokens=bad))
        fault("ANALYSIS_PLAN", w.freeze, plan)


def test_q15_no_policy_means_no_run(w):
    s = w.setup(val=4)
    ref = w.freeze(w.experiment(s, policy(s.ev, max_trial_tokens=10)))
    w.m.eval.executor_policy = None
    hold("QUALIFIED_EXECUTOR_REQUIRED", w.run, ref, Scripted(w, w.roles(s)))


# --- 7.6: always-valid accumulation across experiments (IC-16 B) -------------------------------

WIN_IDS = {"val-01", "val-05", "val-09"}


def e_outcome(role, case_id, repeat):
    """Three tasks where only the candidate passes (wins); every other task both arms pass."""
    return role == "candidate" or case_id not in WIN_IDS


def e_policy(ev, key, prior_refs, prior_state, alpha=0.05):
    spec = {
        "alpha": alpha,
        "key": key,
        "prior_state": prior_state,
        "prior_report_refs": prior_refs,
    }
    return confirm(
        ev, 16, sequential_rule="e_process_accumulating", minimum_effect=0.9, e_process=spec
    )


def e_plan(
    w, s, prior_refs, prior_state, *, prop=None, key=None, stage="focused", budget=None, alpha=0.05
):
    key = key or e_process_key(s.base, s.cand, stage)
    sampling = {"stage": stage} if stage else {}
    pol = e_policy(s.ev, key, prior_refs, prior_state, alpha)
    return w.experiment(s, pol, prop=prop, sampling=sampling, budget=budget)


def e_run(w, s, prior_refs, prior_state, **kw):
    plan = e_plan(w, s, prior_refs, prior_state, **kw)
    ref = w.freeze(plan)
    report_ref = w.run(ref, Scripted(w, w.roles(s), e_outcome))
    return report_ref, w.analysis(report_ref)


def expected_e(wins):
    return mean((2 * q) ** wins for q in ALTERNATIVES)


def test_e_process_evidence_accumulates_and_crosses_across_three_experiments(w):
    s = w.setup(val=16)
    reports, state = [], None
    for k in (1, 2, 3):
        report_ref, analysis = e_run(w, s, list(reports), state)
        block = analysis["e_process"]
        assert (block["wins"], block["losses"]) == (3, 0)
        assert block["prior_report_refs"] == reports and block["prior_state"] == state
        assert block["e_value"] == pytest.approx(expected_e(3 * k))
        assert block["state"] == pytest.approx(list(e_process_update(None, 3 * k, 0)))
        assert e_value(tuple(block["state"])) == pytest.approx(block["e_value"])
        assert block["crossed"] is (k == 3)
        assert "some task" in block["limitation"]
        reports.append(report_ref)
        state = block["state"]
        if k < 3:
            assert analysis["decision_class"] == "non_inferior"
            assert "basis" not in analysis
        else:
            assert analysis["decision_class"] == "improvement"
            assert analysis["basis"] == "e_process"
            assert analysis["verdict"] == "pass"
            assert w.report(report_ref)["verdict"] == "pass"
    key = e_process_key(s.base, s.cand, "focused")
    recorded = w.m.eval.e_process_reports(w.scope, key)
    assert set(recorded) == {digest(r) for r in reports}


def test_e_process_freeze_refuses_any_prior_that_is_not_the_recorded_chain(w):
    s = w.setup(val=16)
    r1, a1 = e_run(w, s, [], None)
    state1 = a1["e_process"]["state"]
    r2, a2 = e_run(w, s, [r1], state1)
    state2 = a2["e_process"]["state"]
    other = w.composition("other-candidate", "builtin_sum")
    other_s = SimpleNamespace(**{**vars(s), "cand": other, "prop": w.proposal(s.base, other)})
    r_other, _ = e_run(w, other_s, [], None)
    tampered = [state2[0] + 0.5, *state2[1:]]
    holdout_key = e_process_key(s.base, s.cand, "holdout")
    bad = {
        "omitted prior": dict(prior_refs=[], prior_state=None),
        "last report dropped": dict(prior_refs=[r1], prior_state=state1),
        "reordered": dict(prior_refs=[r2, r1], prior_state=state2),
        "duplicated": dict(prior_refs=[r1, r1, r2], prior_state=state2),
        "foreign report": dict(prior_refs=[r1, r2, r_other], prior_state=state2),
        "state not recorded": dict(prior_refs=[r1, r2], prior_state=tampered),
        "state of the first night": dict(prior_refs=[r1, r2], prior_state=state1),
        "other stage key": dict(prior_refs=[r1, r2], prior_state=state2, key=holdout_key),
        # Ville's bound needs alpha fixed before the data: raising it after two nights would let
        # the recorded evidence cross 1/alpha at a lower threshold.
        "alpha raised": dict(prior_refs=[r1, r2], prior_state=state2, alpha=0.2),
        "alpha lowered": dict(prior_refs=[r1, r2], prior_state=state2, alpha=0.01),
    }
    for name, kw in bad.items():
        error = hold("SEQUENTIAL_RULE", w.freeze, e_plan(w, s, **kw))
        assert error.code == "SEQUENTIAL_RULE", name
    hold("SEQUENTIAL_RULE", w.freeze, e_plan(w, s, [r1, r2], state2, stage=None))
    hold("SEQUENTIAL_RULE", w.freeze, e_plan(w, s, [r1, r2], state2, stage="nightly"))
    assert w.freeze(e_plan(w, s, [r1, r2], state2))


def test_e_process_a_different_stage_or_candidate_starts_a_fresh_process(w):
    s = w.setup(val=16)
    r1, a1 = e_run(w, s, [], None)
    _, a_stage = e_run(w, s, [], None, stage="holdout")
    assert a_stage["e_process"]["key"] != a1["e_process"]["key"]
    assert a_stage["e_process"]["e_value"] == pytest.approx(expected_e(3))
    hold("SEQUENTIAL_RULE", w.freeze, e_plan(w, s, [r1], a1["e_process"]["state"], stage="holdout",
         key=e_process_key(s.base, s.cand, "holdout")))  # fmt: skip


def test_e_process_a_new_proposal_with_the_same_arms_continues_it(w):
    # IC-16 (B): the key holds no proposal id. Proposal one has room for two experiments only.
    tight = {**BUDGET, "max_attempts": 2}
    s = w.setup(val=16)
    r1, a1 = e_run(w, s, [], None, budget=tight)
    r2, a2 = e_run(w, s, [r1], a1["e_process"]["state"], budget=tight)
    state2 = a2["e_process"]["state"]
    third = e_plan(w, s, [r1, r2], state2, budget=tight)
    hold("META_ATTEMPTS", w.freeze, third)
    successor = w.proposal(s.base, s.cand)
    assert successor["proposal_id"] != s.prop["proposal_id"]
    _, a3 = e_run(w, s, [r1, r2], state2, prop=successor)
    assert a3["e_process"]["e_value"] == pytest.approx(expected_e(9))
    assert a3["e_process"]["crossed"] is True and a3["basis"] == "e_process"
    assert a3["e_process"]["key"] == a1["e_process"]["key"]
    key = a1["e_process"]["key"]
    assert len(w.m.eval.e_process_reports(w.scope, key)) == 3


@pytest.mark.parametrize(
    "change",
    [
        {"purpose": "exploratory", "sequential_rule": "fixed_sample_safety_abort_only"},
        {"prior_state": [0.0] * 9, "prior_report_refs": []},
        {"prior_state": [0.0] * 8, "prior_report_refs": "see-above"},
        {"alpha": 1.0},
        {"alpha": 0},
        {"key": " "},
    ],
)
def test_e_process_malformed_declarations_are_sequential_rule_faults(w, change):
    s = w.setup(val=16)
    key = e_process_key(s.base, s.cand, "focused")
    spec = {"alpha": 0.05, "key": key, "prior_state": None, "prior_report_refs": []}
    spec.update({k: v for k, v in change.items() if k in spec})
    extra = {k: v for k, v in change.items() if k not in spec}
    kw = {"sequential_rule": "e_process_accumulating", "e_process": spec, **extra}
    pol = confirm(s.ev, 16, **kw)
    fault("SEQUENTIAL_RULE", w.freeze, w.experiment(s, pol, sampling={"stage": "focused"}))


def test_e_process_needs_the_accumulating_rule_and_a_pinned_evaluator(w):
    s = w.setup(val=16)
    key = e_process_key(s.base, s.cand, "focused")
    spec = {"alpha": 0.05, "key": key, "prior_state": None, "prior_report_refs": []}
    fixed = confirm(s.ev, 16, e_process=spec)
    fault("SEQUENTIAL_RULE", w.freeze, w.experiment(s, fixed, sampling={"stage": "focused"}))
    legacy = confirm(None, 16, sequential_rule="e_process_accumulating")
    fault("SEQUENTIAL_RULE", w.freeze, w.experiment(s, legacy))


# --- Q-10: MetaHarness._report recomputes a new-method passing report ------------------------


def test_q10_meta_report_recomputes_a_passing_versioned_subset_report(w):
    s = w.setup(val=20, baseline="wrong_sum", candidate="builtin_sum")
    pid = s.prop["proposal_id"]
    expected = w.ids(s, n=16)
    pol = confirm(s.ev, 16, minimum_effect=0.05)
    plan = w.experiment(s, pol, ids=expected, sampling=subset("all_v1", 16))
    ref = w.freeze(plan)
    w.m.meta.approve_experiment(w.m.reviewer, pid, plan["approval_ref"], ref)
    w.m.meta.start_offline(w.m.reviewer, pid)
    executor = Scripted(w, w.roles(s), lambda role, case_id, repeat: role == "candidate")
    report_ref = w.run(ref, executor)
    report = w.report(report_ref)
    assert report["verdict"] == "pass"
    assert w.analysis(report_ref)["decision_class"] == "improvement"
    recomputed, analysis, experiment = w.m.meta._report(w.scope, report_ref)
    assert recomputed == report and analysis["verdict"] == "pass"
    assert experiment["experiment_id"] == plan["experiment_id"]
    assert w.m.meta.evaluate(w.m.reviewer, pid, report_ref)
    assert {t["task_id"] for t in w.trials(report_ref)} == set(expected)


# --- legacy plans behave as at c9f896a -----------------------------------------------------


def test_legacy_full_split_plan_runs_with_the_legacy_analysis_shape(w):
    s = w.setup(val=24)
    pol = policy(None, minimum_tasks=24, purpose="local_qualification")
    ref = w.freeze(w.experiment(s, pol))
    report_ref = w.run(ref, Scripted(w, w.roles(s)))
    analysis = w.analysis(report_ref)
    recomputed = analyze_pairs(w.trials(report_ref), pol, expected_tasks=w.ids(s))
    assert w.report(report_ref)["verdict"] == "pass" == recomputed["verdict"]
    assert len(w.trials(report_ref)) == 48
    assert "decision_class" not in analysis and "vs_reference" not in analysis
    assert analysis["secondary_metrics_use"] == "descriptive_only_not_a_safety_tradeoff"
    assert analysis["verdict"] == recomputed["verdict"]
