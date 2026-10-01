"""Work 033 S11: stage runner, meta operations, leak-gate hook, reference validator.

Contract: specs/033-harness-taxonomy/interfaces.md §2.9, §3.9, §7, §8.1, §8.5, §10.4, IC-01, IC-02,
IC-09, IC-16, IC-19 (A, provisional), IC-20 (provisional), and the Clarifications sections.

Real: the rc06 local product rig (store, runtime, goals, compositions, releases), `LocalMeta` (the
real `MetaHarness` with the leak-gate hook and the real `reference_validator`), the real
`EvaluationService`, `CorpusV2` frozen with its task and leak index, `LocalMetaOps`, `StageRunner`
and `TrialMetrics`. Stand-in: the trial executor. It answers a scripted outcome per (role, task,
repeat) with a verifier-trust receipt shaped like the receipt v2 of `LocalTrialExecutor`
(interfaces.md §2.10), so the baseline-reuse cache key (§8.5) is the real one. The calibration
summary is written directly (the real `CalibrationService` needs two disagreeing cells to produce an
informative class); `LocalMetaOps.calibrate` itself is covered in
`tests/e2e/test_033_s11_search.py`.

The ablation never gates (§8.1): an unbuildable leave-one-out variant (MANIFEST_COMBINATION), any
other exception of one variant and a variant run that ends aborted are findings, an ablation left
failed/aborted/inconclusive is closed with findings, and an ablation interrupted by a process stop
(`Crash`, before a freeze, before a run, inside a trial, after a submit) resumes without re-running
a finished variant. One runner per proposal: a second runner, search, gate or reconcile gets
`SEARCH_BUSY` (a lock of an ended owner epoch is taken over). IC-18 (provisional): `reconcile` is
the human operator's only (APPROVAL_HUMAN, SELF_RECONCILE, FORBIDDEN), ends an interrupted stage
experiment of an earlier owner (EXPERIMENT_OWNER otherwise) and settles a reserved or unknown
allocation with exact stopped-process evidence. A new owner process is simulated by raising
`store.epoch`.

`hold` and `fault` assert the public code of a refusal; `World` and `build_world` are shared with
the e2e module of this slice.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from amplai_foundry.evaluation import versions
from amplai_foundry.evaluation.calibration import select_cases
from amplai_foundry.evaluation.corpus import CorpusService
from amplai_foundry.evaluation.receipts import read_receipt
from amplai_foundry.evaluation.service import TrialObservation
from amplai_foundry.meta_harness import corpus_v2, stages
from amplai_foundry.meta_harness.leak_gate import LeakGate
from amplai_foundry.meta_harness.local_executor import CARRIER_FIELDS
from amplai_foundry.meta_harness.stages import StageRunner
from amplai_foundry.meta_harness.trial_metrics import TrialMetrics
from amplai_foundry.runtime.contracts.identity import canonical, digest, new_id, now
from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.runtime.execution import meta_ops, policies, releases
from amplai_foundry.runtime.execution.meta_local import (
    META_OPERATOR_PERMISSIONS,
    PROPOSER_ID,
    PROPOSER_PERMISSIONS,
    LocalMeta,
)
from amplai_foundry.runtime.execution.meta_ops import LocalMetaOps
from amplai_foundry.runtime.reference import ReferenceDeployment
from rc06_rig import build_rig

KEY = Ed25519PrivateKey.generate()
SHA = "9e574bcd0270ad8ef6e27aa6f6b757d7c032c06b"
CELL = "codex-cli"  # IC-07: the legacy cell id is the driver id
HARNESS_SHA = "1" * 40
SNAPSHOT = {"provider_model_id": "model-x", "driver_version": "0.155.1", "image": "img:1"}
TRIAL_TOKENS = 10  # the qualified per-trial ceiling; scripted trials use at most this
BUDGET = {
    "max_wall_seconds": 604800,
    "max_attempts": 10,
    "max_tokens": 1_000_000,
    "max_cost_microunits": 0,
    "currency": "USD",
    "max_parallel_works": 1,
    "max_delegation_depth": 0,
}
SPLIT_COUNTS = {"development": 14, "validation": 18, "holdout": 16}
DOMAIN_CYCLE = ("bug", "feature", "refactor", "data", "cli_ops")
BASE = {
    "app/__init__.py": "",
    "app/calc.py": "def add(a, b):\n    return a - b\n",
    "tests/test_visible.py": "from app.calc import add\n\n\ndef test_import():\n    assert add\n",
}
# reference-only identifiers (neither in the base tree nor in the task text), as the S5 fixtures
REFERENCE = {
    "app/calc.py": (
        "def add(a, b):\n"
        "    total = NovelAccumulator().zqx_carry_fold(a, b)\n"
        "    return total\n\n\n"
        "class NovelAccumulator:\n"
        "    def zqx_carry_fold(self, a, b):\n"
        "        return a + b\n"
    )
}
GOOD = [
    "You are the IMPLEMENTER for {app_id} at commit {base_commit}.",
    "Make the change below and run the acceptance commands yourself. Do not commit.",
]


def hold(code: str, fn: Any, *args: Any, **kwargs: Any) -> Hold:
    """`fn` is refused with a Hold of this public code."""
    with pytest.raises(Hold) as caught:
        fn(*args, **kwargs)
    assert caught.value.code == code
    return caught.value


def fault(code: str, fn: Any, *args: Any, **kwargs: Any) -> RuntimeFault:
    """`fn` is refused with a RuntimeFault (not a Hold) of this public code."""
    with pytest.raises(RuntimeFault) as caught:
        fn(*args, **kwargs)
    assert not isinstance(caught.value, Hold)
    assert caught.value.code == code
    return caught.value


# -- the corpus v2 --------------------------------------------------------------------------------
def task_ids(split: str) -> list[str]:
    return [f"{DOMAIN_CYCLE[i % 5]}-{split[:3]}-{i:02d}" for i in range(SPLIT_COUNTS[split])]


def split_of(task_id: str) -> str:
    return {"dev": "development", "val": "validation", "hol": "holdout"}[task_id.split("-")[1]]


def hidden_source(task_id: str) -> str:
    name = "test_" + task_id.replace("-", "_") + "_ok"
    return f"from app.calc import add\n\n\ndef {name}():\n    assert add(1, 2) == 3\n"


def write_corpus(root: Path, counts: dict[str, int] | None = None) -> corpus_v2.CorpusV2:
    """A frozen-ready corpus v2: own tasks of five domains with the splits of `SPLIT_COUNTS`."""
    old = dict(SPLIT_COUNTS)
    SPLIT_COUNTS.update(counts or {})
    try:
        ids = [t for split in SPLIT_COUNTS for t in task_ids(split)]
    finally:
        SPLIT_COUNTS.clear()
        SPLIT_COUNTS.update(old)
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "corpus_id": "s11-corpus",
                "version": "1.0.0",
                "split_seed": 7,
                "bases": {"bench": {"dir": "bases/bench", "base_commit": SHA, "app_id": "app"}},
            }
        )
    )
    for name, text in BASE.items():
        target = root / "bases" / "bench" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    for task_id in ids:
        function = "test_" + task_id.replace("-", "_") + "_ok"
        meta = {
            "task_id": task_id,
            "version": 2,
            "domain": task_id.split("-")[0],
            "subdomain": None,
            "set": "main",
            "split": None,
            "source": {"kind": "own", "ref": "r", "author": "a", "created": "2026-09-30"},
            "license": "LicenseRef-amplai-internal",
            "base": "bench",
            "environment": "app",
            "grading": "pytest_hidden",
            "objective": "Make add return the sum of both arguments.",
            "acceptance": ["add(1, 2) is 3"],
            "hidden_map": {function: 0},
            "ambiguity": None,
            "difficulty_declared": None,
        }
        folder = root / "tasks" / task_id
        (folder / "hidden").mkdir(parents=True)
        (folder / "reference").mkdir(parents=True)
        (folder / "task.json").write_text(json.dumps(meta))
        (folder / "hidden" / f"test_{task_id.replace('-', '_')}.py").write_text(
            hidden_source(task_id)
        )
        for name, text in REFERENCE.items():
            target = folder / "reference" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
    (root / "splits.json").write_text(
        json.dumps(
            {
                "seed": 7,
                "method": corpus_v2.SPLIT_METHOD,
                "assignments": {t: split_of(t) for t in ids},
            }
        )
    )
    corpus = corpus_v2.load(root)
    assert {t.task_id: t.split for t in corpus.tasks} == {t: split_of(t) for t in ids}
    return corpus


# -- the stand-in trial executor ------------------------------------------------------------------
def default_outcome(role: str, case_id: str, repeat: int) -> Any:
    return True


class Fake:
    """Scripted outcomes with receipts of the receipt v2 shape (§2.10).

    `outcome(role, case_id, repeat)` gives `success` (True, False or None) or a dict with
    `success` and optional receipt facts (`goal_status`, `hidden_passed`, `safety_failures`).
    `role` is "baseline", "candidate" or "other" (reference arms, ablation variants).
    `tokens[role]` is (input, output) per trial, at most `TRIAL_TOKENS` in total.
    """

    def __init__(self, world: Any) -> None:
        self.world = world
        self.outcome = default_outcome
        self.tokens: dict[str, tuple[int, int]] = {}
        self.calls: list[tuple[str, str, int]] = []
        self.harness_sha: str | None = HARNESS_SHA
        self.proposal: dict[str, Any] | None = None  # the proposal whose roles are scripted
        self.snapshot: dict[str, Any] | None = dict(SNAPSHOT)
        self.corpus_version = "1.0.0"
        self.raises: Any = None

    def role_of(self, composition: dict[str, Any]) -> str:
        if self.proposal is None:
            return "other"
        if composition == self.proposal["baseline_ref"]:
            return "baseline"
        if composition == self.proposal["candidate_ref"]:
            return "candidate"
        return "other"

    def cache_key(self, composition_ref: dict[str, Any], case: dict[str, Any], repeat: int) -> str:
        """§8.5, as `LocalTrialExecutor._cache_key`."""
        store, scope = self.world.store, self.world.scope
        composition = store.get(scope, "harness-composition", composition_ref)
        model = store.get(scope, "model-profile", composition["model_profile_ref"])
        snapshot = self.snapshot or {}
        return digest(
            {
                "manifest_digest": digest({k: composition.get(k) for k in CARRIER_FIELDS}),
                "cell": {
                    "provider_model_id": snapshot.get("provider_model_id"),
                    "reasoning_profile": model.get("reasoning_profile"),
                    "driver_version": snapshot.get("driver_version"),
                    "image": snapshot.get("image"),
                },
                "task_artifact_digest": case["artifact_ref"]["digest"],
                "corpus_version": self.corpus_version,
                "harness_sha": self.harness_sha,
                "strategy": "repair_loop",
                "repeat_slot": repeat,
            }
        )

    def __call__(
        self, composition: dict[str, Any], case: dict[str, Any], repeat: int, mode: str
    ) -> TrialObservation:
        role = self.role_of(composition)
        self.calls.append((role, case["case_id"], repeat))
        if self.raises is not None and self.raises(role, case["case_id"], repeat):
            raise RuntimeError("response lost")
        result = self.outcome(role, case["case_id"], repeat)
        facts = result if isinstance(result, dict) else {"success": result}
        success = facts["success"]
        safety = facts.get("safety_failures", 0)
        unknown = facts.get("unknown_effects", 0)
        tin, tout = self.tokens.get(role, (2, 2))
        d = self.world
        receipt = {
            "success": success,
            "safety_failures": safety,
            "unknown_effects": unknown,
            "cost_microunits": 0,
            "input_tokens": tin,
            "output_tokens": tout,
            "usage_status": "measured",
            "mode": mode,
            "composition_ref": composition,
            "task_id": case["case_id"],
            "repeat": repeat,
            "scope": d.scope.wire(),
            # receipt v2 (§2.10)
            "goal_id": None,
            "goal_status": facts.get("goal_status"),
            "hidden_passed": facts.get("hidden_passed"),
            "cell_id": CELL,
            "strategy": "repair_loop",
            "corpus_version": self.corpus_version,
            "split": case["split"],
            "harness_sha": self.harness_sha,
            "model_snapshot": self.snapshot,
            "cache_key": self.cache_key(composition, case, repeat),
        }
        artifact = d.artifacts.admit(
            d.scope, canonical(receipt), "application/json", trust="verifier"
        )
        return TrialObservation(
            success,
            (artifact,),
            safety_failures=safety,
            unknown_effects=unknown,
            cost_microunits=0,
            input_tokens=tin,
            output_tokens=tout,
        )


# -- the world ------------------------------------------------------------------------------------
class World:
    """The rc06 local product with `LocalMeta`, a frozen corpus v2 and an evaluator version."""

    def __init__(self, tmp_path: Path, deployment: Any, counts: dict[str, int] | None = None):
        rig = build_rig(deployment, tmp_path)
        d = rig.d
        self.rig, self.d = rig, d
        self.store, self.scope = d.store, d.scope
        installed = [ref for app in rig.service.apps.values() for ref in app.compositions.values()]
        releases.bootstrap(
            d.store, d.scope, d.contracts, installed, signer=KEY, key_id="local-authority"
        )
        self.local = LocalMeta(d.store, d.contracts, d.artifacts, d.scope, KEY.public_key())
        self.operator = replace(
            rig.operator, permissions=rig.operator.permissions | META_OPERATOR_PERMISSIONS
        )
        self.dep = SimpleNamespace(
            service=rig.service,
            store=d.store,
            scope=d.scope,
            artifacts=d.artifacts,
            contracts=d.contracts,
            meta_local=self.local,
            meta_operator=lambda: self.operator,
            _release_signer=KEY,
        )
        self.artifacts = d.artifacts
        self.corpus = write_corpus(tmp_path / "corpus", counts)
        self.executor = Fake(self)
        self.ops = LocalMetaOps(self.dep, self.corpus, self.executor, driver=CELL)  # type: ignore
        self.ops.qualify_executor("scripted S11 trials", ["tests/v3/test_033_s11_stages.py"],
                                  TRIAL_TOKENS)  # fmt: skip
        self.refs = corpus_v2.freeze(
            self.operator, self.store, self.artifacts, self.corpus, holdout_use_limit=50
        )
        self.version_ref = self.evaluator()
        self.summary_ref = self.summary()
        self.counter = 0

    # -- records written directly -----------------------------------------------------------------
    def put(self, kind: str, object_id: str, value: dict[str, Any], revision: int = 1) -> Any:
        with self.store.tx() as db:
            return self.store.put(db, self.scope, kind, object_id, revision, value)

    def evaluator(self, *, all_equal: bool = True) -> dict[str, Any]:
        requal = self.put(
            "evaluator-requalification",
            new_id("requal"),
            {
                "schema": "amplai.evaluator-requalification.v1",
                "scope": self.scope.wire(),
                "evaluator_version": "eval-2",
                "store": "test",
                "reports": [],
                "all_equal": all_equal,
            },
        )
        return versions.write_version(
            self.store, self.operator, corpus_ref=self.refs["corpus_ref"],
            requalification_ref=requal,
        )  # fmt: skip

    def summary(
        self,
        informative: dict[str, int] | None = None,
        *,
        cell: str = CELL,
        discordance: float | None = 0.2,
        extra: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """A calibration summary: the first `n` tasks of each split informative (default: all of
        development and validation), the rest saturated; holdout tasks are never calibrated.
        `extra` task ids (tasks added to the corpus by a test) are informative too."""
        limit = {"development": 14, "validation": 18, **(informative or {})}
        tasks: dict[str, Any] = {}
        for split in ("development", "validation"):
            for i, task_id in enumerate(task_ids(split)):
                klass = "informative" if i < limit[split] else "saturated"
                tasks[task_id] = {"class": klass}
        tasks.update({task_id: {"class": "informative"} for task_id in extra})
        plan = self.put(
            "calibration-plan",
            new_id("calplan"),
            {"schema": "amplai.calibration-plan.v1", "scope": self.scope.wire(),
             "corpus_ref": self.refs["corpus_ref"]},
        )  # fmt: skip
        row: dict[str, Any] = {"tasks": tasks}
        if discordance is not None:
            row["aa_discordance"] = discordance
        return self.put(
            "calibration-summary",
            new_id("calsum"),
            {
                "schema": "amplai.calibration-summary.v1",
                "scope": self.scope.wire(),
                "plan_ref": plan,
                "cells": {cell: row},
                "saturated_everywhere": [],
                "informative_any": sorted(
                    t for t, v in tasks.items() if v["class"] == "informative"
                ),
                "evaluator_version_ref": self.version_ref,
                "summarized_at": now(),
            },
        )

    # -- components and proposals -----------------------------------------------------------------
    def component(self, kind: str, name: str, **fields: Any) -> dict[str, Any]:
        content = json.loads(json.dumps(policies.V1[kind]))
        content.update(fields)
        return self.ops.components.register(
            self.local.proposer, component_id=f"{kind}.{name}", kind=kind, content=content,
            source="proposer", rationale="S11 test component",
        )  # fmt: skip

    def role_prompt(self, name: str, extra: str = "Be careful.") -> dict[str, Any]:
        return self.ops.components.register(
            self.local.proposer, component_id=f"role_prompt.{name}", kind="role_prompt",
            content={"implementer": [*GOOD, extra]}, source="proposer",
            rationale="S11 test component",
        )  # fmt: skip

    def propose(self, kind: str = "a2", suffix: str | None = None, **over: Any) -> str:
        """`a2`: role_prompt + env_bootstrap (class A, two components); `a1`: role_prompt only;
        `b2`: attempt_policy + env_bootstrap (class B, needs the human review)."""
        self.counter += 1
        suffix = suffix or f"{kind}{self.counter}"
        changes: dict[str, Any] = {}
        if kind in ("a2", "a1"):
            changes["role_prompt"] = self.role_prompt(suffix)
        if kind in ("a2", "b2"):
            changes["env_bootstrap"] = self.component("env_bootstrap", suffix, enabled=True)
        if kind == "b2":
            changes["attempt_policy"] = self.component("attempt_policy", suffix, max_attempts=2)
        args = {
            "cell_id": CELL,
            "changes": changes,
            "suffix": suffix,
            "hypothesis": "Scripted candidate " + suffix,
            "expected_benefit": "Exercise the stage runner",
            "risks": ["Scripted outcomes say nothing about a real model"],
            "observation_refs": [self.ops._observation("the stages need a candidate")],
            "prediction": None,
        }
        args.update(over)
        return self.ops.propose_components(**args)

    def script(self, proposal_id: str, outcome: Any = None, **tokens: tuple[int, int]) -> Fake:
        """Point the executor's roles at this proposal's baseline and candidate."""
        head = self.store.head(self.scope, "evolution", proposal_id)
        proposal = self.store.get(
            self.scope, "harness-change-proposal", head["data"]["proposal_ref"]
        )
        self.executor.proposal = proposal
        self.executor.outcome = outcome or default_outcome
        self.executor.tokens = dict(tokens)
        return self.executor

    # -- the stage runner -------------------------------------------------------------------------
    def runner(
        self,
        *,
        summary_ref: dict[str, Any] | None = None,
        version_ref: dict[str, Any] | None = None,
        parallel: int = 1,
    ) -> StageRunner:
        return StageRunner(
            self.ops,
            self.refs,
            calibration_summary_ref=summary_ref or self.summary_ref,
            evaluator_version_ref=version_ref or self.version_ref,
            metrics=TrialMetrics(self.dep.service),
            leak_gate=LeakGate(self.operator, self.store, self.refs["leak_index_ref"]),
            parallel=parallel,
        )

    def head(self, proposal_id: str) -> dict[str, Any]:
        return dict(self.store.head(self.scope, "evolution", proposal_id))

    def state(self, proposal_id: str) -> str:
        return str(self.head(proposal_id)["state"])

    def stage_run(self, proposal_id: str) -> dict[str, Any]:
        return dict(self.store.head(self.scope, "stage-run", "stagerun-" + proposal_id))

    def budget(self, proposal_id: str) -> dict[str, Any]:
        return dict(self.store.head(self.scope, "meta-budget", proposal_id))

    def proposal(self, proposal_id: str) -> dict[str, Any]:
        value: dict[str, Any] = self.store.get(
            self.scope, "harness-change-proposal", self.head(proposal_id)["data"]["proposal_ref"]
        )
        return value

    def experiment(self, ref: dict[str, Any]) -> dict[str, Any]:
        value: dict[str, Any] = self.store.get(self.scope, "eval-experiment", ref)
        return value

    def sampling(self, experiment_ref: dict[str, Any]) -> dict[str, Any]:
        exp = self.experiment(experiment_ref)
        return dict(self.store.get(self.scope, "sampling-plan", exp["sampling_plan_ref"]))

    def analysis(self, report_ref: dict[str, Any]) -> dict[str, Any]:
        report = self.store.get(self.scope, "eval-report", report_ref)
        return dict(read_receipt(self.artifacts.read(self.scope, report["analysis_artifact"],
                                                     trusted=True)))  # fmt: skip

    def trials(self, report_ref: dict[str, Any]) -> list[dict[str, Any]]:
        report = self.store.get(self.scope, "eval-report", report_ref)
        return [self.store.get(self.scope, "eval-trial", r) for r in report["run_refs"]]

    def step(self, steps: list[Any], stage: str) -> Any:
        return next(s for s in steps if s.stage == stage)

    def heads(self, kind: str) -> list[tuple[str, str, dict[str, Any]]]:
        with self.store._lock:
            rows = self.store.conn.execute(
                "SELECT id,state,data FROM heads WHERE tenant=? AND project=? AND kind=? "
                "ORDER BY rowid",
                (*self.scope.keys(), kind),
            ).fetchall()
        return [(r["id"], r["state"], json.loads(r["data"])) for r in rows]

    def objects(self, kind: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        return list(self.store.list_objects(self.scope, kind))


@contextlib.contextmanager
def build_world(tmp_path: Path, counts: dict[str, int] | None = None):  # type: ignore[no-untyped-def]
    with ReferenceDeployment(tmp_path / "deployment") as deployment:
        yield World(tmp_path, deployment, counts)


@pytest.fixture
def w(tmp_path: Path):  # type: ignore[no-untyped-def]
    with build_world(tmp_path) as world:
        yield world


# ==================================================================================================
# pure parts of the stage template and the stage records (§2.9, §8.1, IC-09)
# ==================================================================================================
def test_ic09_a_confirmatory_stage_needs_16_tasks_at_margin_025() -> None:
    from amplai_foundry.evaluation.sequential import min_tasks_for_margin

    assert min_tasks_for_margin(stages.MARGIN, stages.CONFIDENCE) == 16
    assert stages.confirmatory_tasks() == 16  # max(16, n_min)
    assert stages.EXPLORATORY_TASKS == 12 and stages.ABLATION_VARIANTS == 3


def test_default_v1_template_is_the_table_of_section_8_1() -> None:
    template = stages.template_stages("default_v1", holdout_tasks=22)
    assert [s["stage"] for s in template] == list(stages.STAGE_ORDER)
    by = {s["stage"]: s for s in template}
    screening, focused, ablation, holdout = (by[n] for n in stages.STAGE_ORDER)
    assert (screening["split"], screening["case_rule"], screening["max_tasks"]) == (
        "development", "informative_v1", 12,
    )  # fmt: skip
    assert (screening["repeats"], screening["arms"], screening["reference"]) == (
        1, ["baseline", "candidate"], None,
    )  # fmt: skip
    assert (screening["purpose"], screening["gate"], screening["margin"]) == (
        "exploratory", "auto", 0.25,
    )  # fmt: skip
    assert (focused["split"], focused["case_rule"], focused["max_tasks"], focused["repeats"]) == (
        "validation", "informative_v1", 16, 2,
    )  # fmt: skip
    assert focused["arms"] == ["baseline", "candidate", "reference"]
    assert focused["reference"] == {"kind": "best_of_n", "n": 3, "cost_match": "tokens"}
    assert (focused["purpose"], focused["gate"], focused["endpoint"]) == (
        "confirmatory", "operator", "noninferiority",
    )  # fmt: skip
    assert (ablation["split"], ablation["max_tasks"], ablation["repeats"]) == ("development", 12, 1)
    assert (ablation["purpose"], ablation["gate"]) == ("exploratory", "auto")
    # holdout: every holdout task (not calibrated, §8.2), one run per arm
    assert (holdout["split"], holdout["case_rule"], holdout["max_tasks"]) == (
        "holdout", "all_v1", 22,
    )  # fmt: skip
    assert (holdout["repeats"], holdout["purpose"], holdout["gate"]) == (
        1,
        "confirmatory",
        "operator",
    )
    assert stages.OPERATOR_GATES == ("focused", "holdout")
    for entry in template:
        # a test edit is a safety failure, not a guard (IC-18 c)
        assert set(entry["hack_guards"]) == {
            "verified_hidden_fail", "ask_back_rate", "broken_tool_calls_rate", "edit_rate_ratio",
        }  # fmt: skip


def test_an_unknown_stage_template_is_a_stage_plan_fault() -> None:
    fault("STAGE_PLAN", stages.template_stages, "nightly_v9", holdout_tasks=16)


@pytest.mark.parametrize(
    "change",
    [
        {"max_attempts": 11},  # $defs.budget maximum is 10
        {"max_attempts": 0},
        {"max_wall_seconds": 604801},  # maximum 604800
        {"max_wall_seconds": 0},
        {"max_tokens": 0},
        {"max_tokens": True},  # a bool is not an integer
        {"currency": "usd"},
        {"currency": "USDX"},
        {"max_parallel_works": 65},
        {"max_delegation_depth": 9},
        {"max_cost_microunits": -1},
        {"surprise": 1},  # no unknown field
    ],
)
def test_the_root_budget_must_match_the_common_budget_schema(change: dict[str, Any]) -> None:
    stages.validate_budget(BUDGET)
    stages.validate_budget({**BUDGET, "max_cost_microunits": None})
    fault("STAGE_PLAN", stages.validate_budget, {**BUDGET, **change})


def test_a_budget_without_a_field_or_not_an_object_is_refused() -> None:
    fault(
        "STAGE_PLAN", stages.validate_budget, {k: v for k, v in BUDGET.items() if k != "currency"}
    )
    fault("STAGE_PLAN", stages.validate_budget, [BUDGET])
    fault("STAGE_PLAN", stages.validate_budget, None)


def test_slots_and_component_names_round_trip() -> None:
    assert stages.component_name("prompt_bundle_ref") == "role_prompt"
    assert stages.component_name("L2") == "decider.L2"
    assert stages.component_name("env_bootstrap") == "env_bootstrap"
    for slot in stages.ALL_SLOTS:
        assert stages.slot_of(stages.component_name(slot)) == slot
    assert stages.slot_of("role_prompt") == "prompt_bundle_ref"
    assert stages.slot_of("decider.L5") == "L5"


# ==================================================================================================
# plan (§3.9): the stage plan, IC-09, IC-16, IC-20 and the holds
# ==================================================================================================
def planned(w: World, kind: str = "a2", **tokens: tuple[int, int]) -> tuple[str, StageRunner]:
    """A proposal with its stage plan and a runner, executor scripted to pass everything."""
    pid = w.propose(kind)
    w.script(pid, **tokens)
    runner = w.runner()
    runner.plan(pid, cell_id=CELL, root_budget=BUDGET)
    return pid, runner


def plan_value(w: World, pid: str) -> dict[str, Any]:
    ref = max(
        (r for r, _ in w.objects("stage-plan") if r["id"] == "stageplan-" + pid),
        key=lambda r: r["revision"],
    )
    value: dict[str, Any] = w.store.get(w.scope, "stage-plan", ref)
    return value


def test_plan_writes_the_stage_plan_and_the_stage_run_head(w: World) -> None:
    pid = w.propose("a2")
    ref = w.runner().plan(pid, cell_id=CELL, root_budget=BUDGET)
    assert ref["id"] == "stageplan-" + pid and ref["revision"] == 1
    plan = w.store.get(w.scope, "stage-plan", ref)
    assert plan["schema"] == "amplai.stage-plan.v1" and plan["scope"] == w.scope.wire()
    assert (plan["proposal_id"], plan["cell_id"]) == (pid, CELL)
    assert plan["corpus_ref"] == w.refs["corpus_ref"]
    assert plan["calibration_summary_ref"] == w.summary_ref
    assert plan["evaluator_version_ref"] == w.version_ref
    assert plan["root_budget"] == BUDGET  # IC-16: one root budget per proposal
    assert plan["environment_digests"] == {}  # IC-12 pins arrive with S7b
    assert [s["stage"] for s in plan["stages"]] == list(stages.STAGE_ORDER)
    holdout = plan["stages"][3]
    assert holdout["max_tasks"] == SPLIT_COUNTS["holdout"] and holdout["case_rule"] == "all_v1"
    assert plan["ablation_components"] == ["role_prompt", "env_bootstrap"]
    stages.validate_stage_plan(plan)
    run = w.stage_run(pid)
    assert run["data"]["plan_ref"] == ref and run["data"]["current"] is None
    assert run["data"]["bound_to_evolution"] == "holdout"  # IC-01
    assert {n: s["state"] for n, s in run["data"]["stages"].items()} == dict.fromkeys(
        stages.STAGE_ORDER, "pending"
    )
    assert w.state(pid) == "draft"  # planning screens nothing


def test_plan_is_idempotent_and_one_root_budget_per_proposal(w: World) -> None:
    pid = w.propose("a2")
    runner = w.runner()
    ref = runner.plan(pid, cell_id=CELL, root_budget=BUDGET)
    assert runner.plan(pid, cell_id=CELL, root_budget=BUDGET) == ref  # no second revision
    assert len([r for r, _ in w.objects("stage-plan") if r["id"] == "stageplan-" + pid]) == 1
    # another root budget or cell is a new root: META_BUDGET_CHANGE (IC-16, budget.py:48-52)
    error = hold("META_BUDGET_CHANGE", runner.plan, pid, cell_id=CELL,
                 root_budget={**BUDGET, "max_tokens": 5})  # fmt: skip
    assert error.details["root_budget"] == BUDGET
    hold("META_BUDGET_CHANGE", runner.plan, pid, cell_id="claude-cli", root_budget=BUDGET)


def test_ablation_covers_at_most_three_changed_components_and_none_for_a_single_one(
    w: World,
) -> None:
    single = w.propose("a1")
    w.runner().plan(single, cell_id=CELL, root_budget=BUDGET)
    # leave-one-out of the only changed component is the baseline itself: nothing to ablate
    assert plan_value(w, single)["ablation_components"] == []
    assert w.stage_run(single)["data"]["stages"]["ablation"]["state"] == "skipped"
    four = w.propose(
        "a1",
        suffix="four",
        changes={
            "role_prompt": w.role_prompt("four"),
            "env_bootstrap": w.component("env_bootstrap", "four", enabled=True),
            "attempt_policy": w.component("attempt_policy", "four", max_attempts=2),
            "limits": w.component("limits", "four", max_wall_seconds=1700),
        },
    )
    w.runner().plan(four, cell_id=CELL, root_budget=BUDGET)
    names = plan_value(w, four)["ablation_components"]
    assert len(names) == stages.ABLATION_VARIANTS == 3
    assert set(names) <= {"role_prompt", "env_bootstrap", "attempt_policy", "limits"}


def test_ic20_focused_holds_no_informative_tasks_below_16(w: World) -> None:
    pid = w.propose("a2")
    short = w.summary({"validation": 15})
    error = hold("NO_INFORMATIVE_TASKS", w.runner(summary_ref=short).plan, pid, cell_id=CELL,
                 root_budget=BUDGET)  # fmt: skip
    assert error.details == {"stage": "focused", "available": 15, "need": 16}
    assert w.objects("stage-plan") == []  # nothing was planned
    # 16 = max(16, n_min): the boundary passes, and a surplus is cut to 16 by the stage rule
    exact = w.summary({"validation": 16})
    assert w.runner(summary_ref=exact).plan(pid, cell_id=CELL, root_budget=BUDGET)


def test_ic20_there_is_no_all_v1_fallback_for_focused(w: World) -> None:
    # saturated tasks are concordant pairs: filling the sample with them would pass
    # non-inferiority without measuring a difference. 30 validation tasks, 3 informative: hold.
    pid = w.propose("a2")
    few = w.summary({"validation": 3})
    error = hold("NO_INFORMATIVE_TASKS", w.runner(summary_ref=few).plan, pid, cell_id=CELL,
                 root_budget=BUDGET)  # fmt: skip
    assert error.details["available"] == 3
    # the saturated validation tasks are still tasks of the corpus: all_v1 is not used
    assert len(task_ids("validation")) == SPLIT_COUNTS["validation"] > 3


def test_screening_with_no_informative_development_task_has_nothing_to_run(w: World) -> None:
    pid = w.propose("a2")
    none = w.summary({"development": 0})
    error = hold("NO_INFORMATIVE_TASKS", w.runner(summary_ref=none).plan, pid, cell_id=CELL,
                 root_budget=BUDGET)  # fmt: skip
    assert error.details == {"stage": "screening", "available": 0, "need": 1}


def test_a_summary_without_a_row_for_the_cell_holds_no_informative_tasks(w: World) -> None:
    pid = w.propose("a2")
    other = w.summary(cell="claude-cli")
    hold("NO_INFORMATIVE_TASKS", w.runner(summary_ref=other).plan, pid, cell_id=CELL,
         root_budget=BUDGET)  # fmt: skip


def test_ic09_a_holdout_that_cannot_pass_its_rule_is_underpowered(tmp_path: Path) -> None:
    with build_world(tmp_path, {"holdout": 15}) as small:
        pid = small.propose("a2")
        error = hold("SAMPLE_UNDERPOWERED", small.runner().plan, pid, cell_id=CELL,
                     root_budget=BUDGET)  # fmt: skip
        assert error.details == {"stage": "holdout", "task_count": 15, "need": 16}
        assert small.objects("stage-plan") == []


def test_ic09_exploratory_stages_are_never_refused_for_size(w: World) -> None:
    # 1 informative development task: screening runs on it (no SAMPLE_UNDERPOWERED), and the
    # class of a 1-task sample is simply not conclusive
    pid = w.propose("a2")
    one = w.summary({"development": 1})
    runner = w.runner(summary_ref=one)
    runner.plan(pid, cell_id=CELL, root_budget=BUDGET)
    w.script(pid)
    steps = runner.advance(pid)
    assert w.step(steps, "screening").state == "passed"
    assert len({t["task_id"] for t in w.trials(w.stage_run(pid)["data"]["stages"]["screening"]
                                                 ["report_ref"])}) == 1  # fmt: skip


def test_plan_holds_cell_unknown_when_the_baseline_is_another_cell(w: World) -> None:
    pid = w.propose("a2")
    hold("CELL_UNKNOWN", w.runner().plan, pid, cell_id="claude-cli", root_budget=BUDGET)
    assert w.objects("stage-plan") == []


def test_plan_refuses_a_rejected_candidate(w: World) -> None:
    pid = w.propose("a2")
    w.ops.reject(pid, "not worth an experiment")
    hold("META_STATE", w.runner().plan, pid, cell_id=CELL, root_budget=BUDGET)


def test_plan_refuses_an_unknown_template_and_a_bad_budget(w: World) -> None:
    pid = w.propose("a2")
    fault("STAGE_PLAN", w.runner().plan, pid, cell_id=CELL, root_budget=BUDGET, template="x_v2")
    fault(
        "STAGE_PLAN", w.runner().plan, pid, cell_id=CELL, root_budget={**BUDGET, "max_attempts": 11}
    )
    assert w.objects("stage-plan") == []


def test_plan_holds_evaluator_unqualified_for_a_ref_that_is_no_version(w: World) -> None:
    pid = w.propose("a2")
    runner = w.runner(version_ref=w.refs["corpus_ref"])
    hold("EVALUATOR_UNQUALIFIED", runner.plan, pid, cell_id=CELL, root_budget=BUDGET)


def test_plan_holds_evaluator_unqualified_without_a_passing_requalification(w: World) -> None:
    pid = w.propose("a2")
    failing = w.put(
        "evaluator-requalification",
        new_id("requal"),
        {"schema": "amplai.evaluator-requalification.v1", "scope": w.scope.wire(),
         "evaluator_version": "eval-9", "store": "test", "reports": [], "all_equal": False},
    )  # fmt: skip
    version = w.put(
        "evaluator-version",
        "evaluator-9",
        {
            "schema": versions.SCHEMA,
            "scope": w.scope.wire(),
            "version": "eval-9",
            **versions.code_digests(),
            "corpus_ref": w.refs["corpus_ref"],
            "stage_templates": {},
            "requalification_ref": failing,
            "approved_by": w.operator.wire(),
            "at": now(),
        },
    )
    hold("EVALUATOR_UNQUALIFIED", w.runner(version_ref=version).plan, pid, cell_id=CELL,
         root_budget=BUDGET)  # fmt: skip


def test_plan_holds_evaluator_changed_when_the_running_code_differs(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = w.propose("a2")
    runner = w.runner()
    other = {k: "sha256:" + "0" * 64 for k in versions.DIGEST_FIELDS}
    monkeypatch.setattr(versions, "code_digests", lambda **_: other)
    hold("EVALUATOR_CHANGED", runner.plan, pid, cell_id=CELL, root_budget=BUDGET)
    assert w.objects("stage-plan") == []


def test_plan_holds_evaluator_changed_when_the_summary_pins_another_version(w: World) -> None:
    pid = w.propose("a2")
    second = versions.write_version(
        w.store, w.operator, corpus_ref=w.refs["corpus_ref"],
        requalification_ref=versions.read_version(w.store, w.scope, w.version_ref)[
            "requalification_ref"],
        stage_templates={"note": "a second revision"},
    )  # fmt: skip
    assert second != w.version_ref
    hold("EVALUATOR_CHANGED", w.runner(version_ref=second).plan, pid, cell_id=CELL,
         root_budget=BUDGET)  # fmt: skip


def test_plan_holds_derived_proposal_for_a_derived_one(w: World) -> None:
    pid, runner = planned(w)
    runner.advance(pid)
    runner.approve_stage(pid, "focused")
    runner.advance(pid)
    derived = w.stage_run(pid)["data"]["stages"]["ablation"]["ablation_proposals"]
    assert len(derived) == 2
    for derived_id in derived:
        hold("DERIVED_PROPOSAL", runner.plan, derived_id, cell_id=CELL, root_budget=BUDGET)


@pytest.mark.parametrize("parallel", [0, 5, -1, True, "2", 1.0])
def test_the_runner_takes_one_to_four_parallel_trials(w: World, parallel: Any) -> None:
    fault("EVAL_PARALLEL", w.runner, parallel=parallel)


def test_the_runner_sets_the_trial_cap_of_the_evaluator(w: World) -> None:
    w.runner(parallel=3)
    assert w.local.evaluation.max_parallel == 3
    w.runner(parallel=1)
    assert w.local.evaluation.max_parallel == 1


def test_the_runner_needs_a_frozen_corpus_ref(w: World) -> None:
    fault("STAGE_PLAN", StageRunner, w.ops, {"leak_index_ref": w.refs["leak_index_ref"]},
          calibration_summary_ref=w.summary_ref, evaluator_version_ref=w.version_ref,
          metrics=TrialMetrics(w.dep.service),
          leak_gate=LeakGate(w.operator, w.store, w.refs["leak_index_ref"]),
          parallel=1)  # fmt: skip


# ==================================================================================================
# advance: the automatic stages (AC-08 unit part) and the screening gate (§8.1)
# ==================================================================================================
def states(steps: list[Any]) -> dict[str, str]:
    return {s.stage: s.state for s in steps}


def test_ac08_screening_of_12_tasks_is_accepted_and_the_runner_stops_at_the_focused_gate(
    w: World,
) -> None:
    pid, runner = planned(w)
    steps = runner.advance(pid)
    assert states(steps) == {
        "screening": "passed", "focused": "waiting_approval", "ablation": "pending",
        "holdout": "pending",
    }  # fmt: skip
    screening, focused = w.step(steps, "screening"), w.step(steps, "focused")
    assert screening.waiting_for is None and screening.report_ref is not None
    assert focused.waiting_for == "approve-stage focused"  # the operator's gate, never chained
    assert focused.experiment_ref is None  # nothing of focused is frozen before the operator acts
    assert runner.last_stop is None
    # 14 informative development tasks, the stage takes 12 (no refusal for size, IC-09): the
    # report is a verdict about 12 tasks x 2 arms x 1 repeat
    trials = w.trials(screening.report_ref)
    assert len(trials) == 24 and len({t["task_id"] for t in trials}) == 12
    assert {t["arm"] for t in trials} == {"baseline", "candidate"}
    # a fully concordant 12-task sample cannot reach non_inferior (lower bound -0.295): the stage
    # gates on "not a regression" only, and the class says so
    assert screening.decision_class == "inconclusive"
    report = w.store.get(w.scope, "eval-report", screening.report_ref)
    assert report["verdict"] == "inconclusive"  # an exploratory pass is never a pass verdict
    assert w.state(pid) == "screened"  # the screen gate ran (MetaHarness.screen)


def test_the_screening_experiment_is_a_frozen_stage_experiment_of_the_proposal(w: World) -> None:
    pid, runner = planned(w)
    steps = runner.advance(pid)
    experiment_ref = w.step(steps, "screening").experiment_ref
    experiment = w.experiment(experiment_ref)
    proposal = w.proposal(pid)
    assert experiment["baseline_ref"] == proposal["baseline_ref"]
    assert experiment["candidate_ref"] == proposal["candidate_ref"]
    assert experiment["corpus_ref"] == w.refs["corpus_ref"]
    assert experiment["budget"] == BUDGET  # the plan's root budget (IC-16)
    assert experiment["mode"] == "sandbox_rerun"
    sampling = w.sampling(experiment_ref)
    assert (sampling["stage"], sampling["split"], sampling["arms"]) == (
        "screening", "development", ["baseline", "candidate"],
    )  # fmt: skip
    assert sampling["case_rule"] == "informative_v1" and sampling["max_tasks"] == 12
    assert sampling["cell_id"] == CELL and sampling["calibration_summary_ref"] == w.summary_ref
    assert sampling["order_rule"] == "alternate_by_repeat_v1"
    # IC-15: the frozen ids are the pure rule over the split and the summary
    summary = w.store.get(w.scope, "calibration-summary", w.summary_ref)
    cases = CorpusService(w.store, w.artifacts).select(
        w.operator, w.refs["corpus_ref"], "development", purpose="frozen_experiment"
    )
    assert sampling["case_ids"] == select_cases(
        cases, rule="informative_v1", summary=summary, cell_id=CELL, max_tasks=12,
        domain_of=lambda case: str(case["task_class"]),
    )  # fmt: skip
    assert len(sampling["case_ids"]) == 12
    # the operator's approval is bound to the exact frozen plan (digest) and is human
    approved = {k: v for k, v in experiment.items() if k != "approval_ref"}
    approval = w.local.approvals.check(
        w.scope, experiment["approval_ref"], "experiment.execute", digest(approved)
    )
    assert approval["approved_by"]["kind"] == "human"


def test_the_stage_policy_is_exploratory_and_declares_the_evaluator_version(w: World) -> None:
    pid, runner = planned(w)
    experiment = w.experiment(w.step(runner.advance(pid), "screening").experiment_ref)
    policy = w.store.get(w.scope, "analysis-plan", experiment["analysis_plan_ref"])["policy"]
    assert policy["purpose"] == "exploratory" and policy["endpoint"] == "noninferiority"
    assert policy["evaluator_version_ref"] == w.version_ref
    assert (policy["noninferiority_margin"], policy["repeats_per_task"]) == (0.25, 1)
    assert (policy["minimum_tasks"], policy["cost_basis"]) == (12, "not_compared")
    assert policy["safety_failure_limit"] == 0 and policy["missing_policy"] == "inconclusive"
    assert policy["mde"] > 0 and "MDE:" in policy["sample_rationale"]
    assert policy["noise_band"] > 0  # the cell has a measured A/A discordance
    assert "0.2" in policy["variance_basis"] and "reference_arm" not in policy


def test_without_a_measured_discordance_the_mde_assumes_half_and_says_so(w: World) -> None:
    pid = w.propose("a2")
    w.script(pid)
    runner = w.runner(summary_ref=w.summary(discordance=None))
    runner.plan(pid, cell_id=CELL, root_budget=BUDGET)
    experiment = w.experiment(w.step(runner.advance(pid), "screening").experiment_ref)
    policy = w.store.get(w.scope, "analysis-plan", experiment["analysis_plan_ref"])["policy"]
    assert "noise_band" not in policy and "0.5 assumed" in policy["variance_basis"]
    assert policy["mde"] > 0


def test_trial_metrics_are_recorded_for_every_stage_trial(w: World) -> None:
    pid, runner = planned(w)
    screening = w.step(runner.advance(pid), "screening")
    records = w.objects("trial-metrics")
    assert len(records) == 24
    sample = records[0][1]
    assert (sample["stage"], sample["split"], sample["phase"]) == (
        "screening",
        "development",
        "stage",
    )
    assert sample["source"] == "experiment" and sample["experiment_ref"] == screening.experiment_ref


def test_advance_is_idempotent_and_never_reruns_a_finished_stage(w: World) -> None:
    pid, runner = planned(w)
    first = runner.advance(pid)
    calls = len(w.executor.calls)
    experiments = len(w.objects("eval-experiment"))
    again = runner.advance(pid)
    assert again == first
    assert len(w.executor.calls) == calls and len(w.objects("eval-experiment")) == experiments
    assert states(runner.status(pid)) == states(first)


def test_advance_without_a_stage_plan_holds_meta_state(w: World) -> None:
    pid = w.propose("a2")
    hold("META_STATE", w.runner().advance, pid)
    assert w.state(pid) == "draft"


def test_status_of_a_proposal_without_a_plan_shows_every_stage_pending(w: World) -> None:
    pid = w.propose("a2")
    steps = w.runner().status(pid)
    assert [s.stage for s in steps] == list(stages.STAGE_ORDER)
    assert {s.state for s in steps} == {"pending"} and {s.waiting_for for s in steps} == {None}
    # class B: the screening step waits for the operator's review
    behavior = w.propose("b2")
    first = w.runner().status(behavior)[0]
    assert (first.stage, first.waiting_for) == ("screening", "review")


def test_class_b_waits_for_the_operator_review_before_screening(w: World) -> None:
    pid, runner = planned(w, "b2")
    steps = runner.advance(pid)
    assert w.step(steps, "screening").waiting_for == "review"
    assert states(steps)["screening"] == "pending" and w.state(pid) == "draft"
    assert w.objects("eval-experiment") == [] and w.executor.calls == []
    # the machine itself refuses to screen it (existing gate)
    hold("CODE_REVIEW_REQUIRED", w.local.meta.screen, w.operator, pid)
    w.ops.review(pid, outcome="pass", note="read the diff of both components")
    steps = runner.advance(pid)
    assert states(steps)["screening"] == "passed" and w.state(pid) == "screened"
    assert w.step(steps, "focused").waiting_for == "approve-stage focused"


def test_screening_rejects_a_regression_with_the_operator_identity(w: World) -> None:
    pid = w.propose("a2")
    w.script(pid, lambda role, case, repeat: role != "candidate")
    runner = w.runner()
    runner.plan(pid, cell_id=CELL, root_budget=BUDGET)
    steps = runner.advance(pid)
    screening = w.step(steps, "screening")
    assert (screening.state, screening.decision_class) == ("failed", "regression")
    assert w.state(pid) == "rejected"
    rejection = w.head(pid)["data"]["rejection"]
    assert rejection["reason"] == "screening: decision class regression"
    assert rejection["by"]["kind"] == "human" and rejection["from_state"] == "screened"
    assert states(steps)["focused"] == "pending"  # a rejected candidate reaches no gate
    assert w.step(steps, "focused").waiting_for is None
    # nothing moves it again
    assert states(runner.advance(pid)) == states(steps)
    hold("META_STATE", runner.approve_stage, pid, "focused")


def test_screening_rejects_a_candidate_safety_failure(w: World) -> None:
    pid = w.propose("a2")

    def outcome(role: str, case: str, repeat: int) -> Any:
        unsafe = role == "candidate" and case == "bug-dev-05"
        return {"success": True, "safety_failures": 1} if unsafe else True

    w.script(pid, outcome)
    runner = w.runner()
    runner.plan(pid, cell_id=CELL, root_budget=BUDGET)
    screening = w.step(runner.advance(pid), "screening")
    assert screening.state == "failed" and w.state(pid) == "rejected"
    assert "candidate safety failure" in w.head(pid)["data"]["rejection"]["reason"]


def test_a_baseline_safety_failure_stops_screening_without_judging_the_candidate(
    w: World,
) -> None:
    pid = w.propose("a2")

    def outcome(role: str, case: str, repeat: int) -> Any:
        unsafe = role == "baseline" and case == "bug-dev-05"
        return {"success": True, "safety_failures": 1} if unsafe else True

    w.script(pid, outcome)
    runner = w.runner()
    runner.plan(pid, cell_id=CELL, root_budget=BUDGET)
    steps = runner.advance(pid)
    screening = w.step(steps, "screening")
    # IC-18 (a): inconclusive with reason baseline_safety_failure, never a candidate regression
    assert screening.state == "aborted" and screening.decision_class == "inconclusive"
    assert w.state(pid) == "screened"  # not rejected: no verdict about the candidate
    assert "baseline_safety_failure" in runner.last_stop["reasons"][0]
    assert w.step(steps, "focused").waiting_for is None  # no gate is reached


def test_screening_holds_the_hack_guard_when_verified_hidden_fails_shift(w: World) -> None:
    pid = w.propose("a2")

    def outcome(role: str, case: str, repeat: int) -> Any:
        if role == "candidate" and case in ("bug-dev-00", "bug-dev-05"):
            return {"success": True, "goal_status": "verified", "hidden_passed": False}
        return True

    w.script(pid, outcome)
    runner = w.runner()
    runner.plan(pid, cell_id=CELL, root_budget=BUDGET)
    steps = runner.advance(pid)
    state = w.stage_run(pid)["data"]["stages"]["screening"]
    assert state["state"] == "failed" and w.state(pid) == "rejected"
    assert len(state["guard_findings"]) == 1
    assert state["guard_findings"][0].startswith("HACK_GUARD: verified_hidden_fail: +2 tasks")
    assert w.step(steps, "screening").state == "failed"
    assert "HACK_GUARD" in w.head(pid)["data"]["rejection"]["reason"]


def test_one_verified_hidden_fail_is_below_the_guard_threshold(w: World) -> None:
    pid = w.propose("a2")

    def outcome(role: str, case: str, repeat: int) -> Any:
        if role == "candidate" and case == "bug-dev-00":
            return {"success": True, "goal_status": "verified", "hidden_passed": False}
        return True

    w.script(pid, outcome)
    runner = w.runner()
    runner.plan(pid, cell_id=CELL, root_budget=BUDGET)
    assert w.step(runner.advance(pid), "screening").state == "passed"


def test_an_unknown_trial_outcome_does_not_reject_screening(w: World) -> None:
    pid = w.propose("a2")
    w.script(pid, lambda role, case, repeat: {"success": None} if case == "bug-dev-05" else True)
    runner = w.runner()
    runner.plan(pid, cell_id=CELL, root_budget=BUDGET)
    steps = runner.advance(pid)
    assert w.step(steps, "screening").state == "passed" and w.state(pid) == "screened"
    # an unknown trial is never a cache entry (§8.5): 11 tasks x 2 arms
    assert len(w.heads("observation-cache")) == 22


def test_an_unknown_effect_aborts_screening_and_blocks_the_root(w: World) -> None:
    pid = w.propose("a2")
    w.script(pid, lambda role, case, repeat: {"success": True, "unknown_effects": 1})
    runner = w.runner()
    runner.plan(pid, cell_id=CELL, root_budget=BUDGET)
    steps = runner.advance(pid)
    assert w.step(steps, "screening").state == "aborted" and w.state(pid) == "screened"
    assert len(w.executor.calls) < 24  # dispatch stopped at the first unknown effect
    # every later reserve of that root holds META_USAGE_UNKNOWN (budget.py:88-92); the runner
    # reports it before freezing anything
    before = len(w.objects("eval-experiment"))
    assert runner._covers(pid, plan_value(w, pid), 1)["code"] == "META_USAGE_UNKNOWN"
    assert len(w.objects("eval-experiment")) == before


# -- the root budget (IC-16) ----------------------------------------------------------------------
def test_the_root_token_budget_must_cover_the_next_stage_before_anything_is_frozen(
    w: World,
) -> None:
    pid = w.propose("a2")
    w.script(pid)
    runner = w.runner()
    runner.plan(pid, cell_id=CELL, root_budget={**BUDGET, "max_tokens": 100})
    steps = runner.advance(pid)
    # screening needs 24 trials x the qualified 10-token ceiling = 240 > 100
    assert runner.last_stop == {
        "stage": "screening", "code": "META_TOKEN_BUDGET", "root": pid, "need_tokens": 240,
        "remaining_tokens": 100,
    }  # fmt: skip
    assert states(steps)["screening"] == "pending" and w.executor.calls == []
    assert w.objects("eval-experiment") == [] and w.state(pid) == "screened"


def test_the_attempt_cap_of_the_root_stops_the_operator_gate(w: World) -> None:
    pid = w.propose("a2")
    w.script(pid)
    runner = w.runner()
    runner.plan(pid, cell_id=CELL, root_budget={**BUDGET, "max_attempts": 1})
    runner.advance(pid)  # screening took the one attempt of the root
    error = hold("META_ATTEMPTS", runner.approve_stage, pid, "focused")
    assert error.details["attempts"] == 1
    assert w.stage_run(pid)["data"]["stages"]["focused"]["state"] == "waiting_approval"
    assert len(w.objects("eval-experiment")) == 1


def test_the_root_wall_clock_runs_from_the_first_freeze_operator_waits_included(
    w: World,
) -> None:
    pid = w.propose("a2")
    w.script(pid)
    runner = w.runner()
    runner.plan(pid, cell_id=CELL, root_budget={**BUDGET, "max_wall_seconds": 100})
    runner.advance(pid)
    start = w.budget(pid)["data"]["started_epoch"]
    w.store.clock = lambda: start + 101  # the operator took more than the root allows
    hold("META_WALL_BUDGET", runner.approve_stage, pid, "focused")


def test_every_stage_experiment_of_a_proposal_shares_one_root(w: World) -> None:
    pid, runner = planned(w)
    runner.advance(pid)
    runner.approve_stage(pid, "focused")
    runner.advance(pid)
    runner.approve_stage(pid, "holdout")
    budget = w.budget(pid)["data"]
    run = w.stage_run(pid)["data"]["stages"]
    # IC-01: screening, focused and holdout are stage experiments under one proposal and root
    assert budget["experiment_refs"] == [
        run["screening"]["experiment_ref"], run["focused"]["experiment_ref"],
        run["holdout"]["experiment_ref"],
    ]  # fmt: skip
    for stage in ("screening", "focused", "holdout"):
        assert w.experiment(run[stage]["experiment_ref"])["budget"] == BUDGET
    assert budget["limits"] == BUDGET


# ==================================================================================================
# the operator's gates (approve_stage) and the evolution machine (IC-01)
# ==================================================================================================
def test_approve_stage_serves_focused_and_holdout_only(w: World) -> None:
    pid, runner = planned(w)
    runner.advance(pid)
    for stage in ("screening", "ablation", "bogus", ""):
        hold("META_STATE", runner.approve_stage, pid, stage)
    assert len(w.objects("eval-experiment")) == 1  # only screening ran


def test_approve_stage_needs_the_gate_to_be_waiting(w: World) -> None:
    pid, runner = planned(w)
    # nothing has run: focused is pending, so is holdout; the gates cannot be skipped
    hold("META_STATE", runner.approve_stage, pid, "focused")
    runner.advance(pid)
    error = hold("META_STATE", runner.approve_stage, pid, "holdout")
    assert error.details == {"stage": "holdout", "state": "pending"}
    runner.approve_stage(pid, "focused")
    # a passed stage is never run twice
    hold("META_STATE", runner.approve_stage, pid, "focused")


def test_approve_stage_without_a_plan_holds_meta_state(w: World) -> None:
    pid = w.propose("a2")
    hold("META_STATE", w.runner().approve_stage, pid, "focused")


def test_holdout_cannot_be_approved_before_ablation_ran(w: World) -> None:
    pid, runner = planned(w)
    runner.advance(pid)
    runner.approve_stage(pid, "focused")
    # ablation is still pending: the holdout gate is not waiting yet
    hold("META_STATE", runner.approve_stage, pid, "holdout")
    steps = runner.advance(pid)
    assert w.step(steps, "holdout").waiting_for == "approve-stage holdout"


def test_ic01_only_the_holdout_experiment_is_bound_to_the_evolution_machine(w: World) -> None:
    pid, runner = planned(w)
    runner.advance(pid)
    focused = runner.approve_stage(pid, "focused")
    assert (focused.state, focused.decision_class) == ("passed", "non_inferior")
    runner.advance(pid)
    # screening, focused and ablation ran while the evolution head stayed `screened`, bound to
    # no experiment
    head = w.head(pid)
    assert head["state"] == "screened" and "experiment_ref" not in head["data"]
    holdout = runner.approve_stage(pid, "holdout")
    assert (holdout.state, holdout.decision_class) == ("passed", "non_inferior")
    head = w.head(pid)
    assert head["state"] == "offline_evaluated"  # approve_experiment -> start_offline -> evaluate
    assert head["data"]["experiment_ref"] == holdout.experiment_ref
    assert head["data"]["report_ref"] == holdout.report_ref and head["data"]["verdict"] == "pass"
    assert head["data"]["experiment_approval_ref"] == w.experiment(
        holdout.experiment_ref
    )["approval_ref"]  # fmt: skip
    sampling = w.sampling(holdout.experiment_ref)
    assert (sampling["stage"], sampling["split"], sampling["case_rule"]) == (
        "holdout", "holdout", "all_v1",
    )  # fmt: skip
    assert sorted(sampling["case_ids"]) == sorted(task_ids("holdout"))  # every holdout task
    trials = w.trials(holdout.report_ref)
    assert len(trials) == 2 * SPLIT_COUNTS["holdout"] and {t["repeat"] for t in trials} == {0}
    policy = w.store.get(w.scope, "analysis-plan", w.experiment(holdout.experiment_ref)[
        "analysis_plan_ref"])["policy"]  # fmt: skip
    assert policy["purpose"] == "confirmatory" and policy["minimum_tasks"] == 16


def test_a_failing_holdout_is_recorded_and_the_candidate_does_not_reach_a_canary(
    w: World,
) -> None:
    pid = w.propose("a2")
    w.script(pid, lambda role, case, repeat: not (role == "candidate" and "-hol-" in case))
    runner = w.runner()
    runner.plan(pid, cell_id=CELL, root_budget=BUDGET)
    runner.advance(pid)
    runner.approve_stage(pid, "focused")
    runner.advance(pid)
    holdout = runner.approve_stage(pid, "holdout")
    assert (holdout.state, holdout.decision_class) == ("failed", "regression")
    head = w.head(pid)
    assert head["state"] == "offline_evaluated" and head["data"]["verdict"] == "fail"
    hold("EVAL_NOT_PASSING", w.ops.approve_canary, pid, ["t0"], max_trial_tokens=10)


def test_focused_uses_the_validation_split_with_two_repeats_and_the_confirmatory_rule(
    w: World,
) -> None:
    pid, runner = planned(w)
    runner.advance(pid)
    focused = runner.approve_stage(pid, "focused")
    assert focused.waiting_for is None and focused.report_ref is not None
    sampling = w.sampling(focused.experiment_ref)
    assert (sampling["stage"], sampling["split"], sampling["max_tasks"]) == (
        "focused", "validation", 16,
    )  # fmt: skip
    assert len(sampling["case_ids"]) == 16 and all("-val-" in c for c in sampling["case_ids"])
    policy = w.store.get(w.scope, "analysis-plan", w.experiment(focused.experiment_ref)[
        "analysis_plan_ref"])["policy"]  # fmt: skip
    assert policy["purpose"] == "confirmatory" and policy["repeats_per_task"] == 2
    assert policy["minimum_tasks"] == 16 and policy["noninferiority_margin"] == 0.25
    assert "MDE:" in policy["sample_rationale"]
    # equal token use gives n = 1, the baseline arm itself: no reference arm is added
    assert sampling["arms"] == ["baseline", "candidate"] and "reference_arm" not in policy
    assert len(w.trials(focused.report_ref)) == 16 * 2 * 2
    report = w.store.get(w.scope, "eval-report", focused.report_ref)
    assert report["verdict"] == "pass"  # confirmatory non-inferiority at 16 concordant tasks


def test_focused_adds_a_budget_matched_reference_arm_the_real_validator_accepts(
    w: World,
) -> None:
    pid, runner = planned(w, baseline=(2, 2), candidate=(4, 4))  # the candidate spends 2x tokens
    runner.advance(pid)
    focused = runner.approve_stage(pid, "focused")
    sampling = w.sampling(focused.experiment_ref)
    assert sampling["arms"] == ["baseline", "candidate", "reference"]
    policy = w.store.get(w.scope, "analysis-plan", w.experiment(focused.experiment_ref)[
        "analysis_plan_ref"])["policy"]  # fmt: skip
    arm = policy["reference_arm"]
    assert (arm["kind"], arm["n"], arm["cost_match"]) == ("best_of_n", 2, "tokens")
    assert "n = 2" in policy["sample_rationale"]
    trials = w.trials(focused.report_ref)
    assert {t["arm"] for t in trials} == {"baseline", "candidate", "reference"}
    assert len(trials) == 16 * 3 * 2
    # the declared reference composition is the baseline with a best_of_n(2) strategy only
    assert w.local.reference_validator(w.scope, {"baseline_ref": w.proposal(pid)["baseline_ref"]},
                                       policy) is None  # fmt: skip
    assert focused.state == "passed"


def test_a_failing_focused_stage_stops_the_search_before_ablation(w: World) -> None:
    pid = w.propose("a2")
    w.script(pid, lambda role, case, repeat: not (role == "candidate" and "-val-" in case))
    runner = w.runner()
    runner.plan(pid, cell_id=CELL, root_budget=BUDGET)
    runner.advance(pid)
    focused = runner.approve_stage(pid, "focused")
    assert (focused.state, focused.decision_class) == ("failed", "regression")
    steps = runner.advance(pid)
    assert states(steps)["ablation"] == "pending" and states(steps)["holdout"] == "pending"
    assert w.step(steps, "holdout").waiting_for is None
    assert w.state(pid) == "screened"  # a failed confirmatory stage does not move the machine
    hold("META_STATE", runner.approve_stage, pid, "holdout")
    hold("META_STATE", runner.approve_stage, pid, "focused")


def test_approve_stage_holds_when_the_candidate_is_no_longer_screened(w: World) -> None:
    pid, runner = planned(w)
    runner.advance(pid)
    w.ops.reject(pid, "changed my mind before the focused gate")
    hold("META_STATE", runner.approve_stage, pid, "focused")
    assert len(w.objects("eval-experiment")) == 1


# ==================================================================================================
# ablation: derived proposals (IC-02, IC-19 A, DERIVED_PROPOSAL)
# ==================================================================================================
def through_ablation(w: World, kind: str = "a2") -> tuple[str, StageRunner, list[str]]:
    pid, runner = planned(w, kind)
    if kind == "b2":
        w.ops.review(pid, outcome="pass", note="read the diff of both components")
    runner.advance(pid)
    runner.approve_stage(pid, "focused")
    runner.advance(pid)
    derived = w.stage_run(pid)["data"]["stages"]["ablation"]["ablation_proposals"]
    return pid, runner, derived


def test_ic02_ablation_variants_are_separate_derived_proposals_with_their_own_roots(
    w: World,
) -> None:
    pid, _runner, derived = through_ablation(w)
    parent = w.proposal(pid)
    assert len(derived) == 2 and len(set(derived)) == 2
    assert w.stage_run(pid)["data"]["stages"]["ablation"]["state"] == "passed"
    plan_ref = w.stage_run(pid)["data"]["plan_ref"]
    manifests = w.ops.manifests
    base_flat = manifests.of_composition(parent["baseline_ref"]).flat()
    cand_flat = manifests.of_composition(parent["candidate_ref"]).flat()
    seen = set()
    for derived_id in derived:
        variant = w.proposal(derived_id)
        assert stages.is_derived(w.store, w.scope, variant)
        assert not stages.is_derived(w.store, w.scope, parent)
        # baseline = the parent candidate, candidate = the leave-one-out variant
        assert variant["baseline_ref"] == parent["candidate_ref"]
        assert variant["experiment_plan_ref"] == plan_ref  # the parent's stage plan marks it
        assert variant["hypothesis"].startswith("Leave-one-out ablation of")
        flat = manifests.of_composition(variant["candidate_ref"]).flat()
        back = [name for name in flat if flat[name] != cand_flat[name]]
        assert len(back) == 1 and flat[back[0]] == base_flat[back[0]]  # that slot back at baseline
        seen.add(back[0])
        # IC-19 (A): never screened, the head stays draft, and a derived root of its own
        assert w.state(derived_id) == "draft"
        assert "review_ref" not in w.head(derived_id)["data"]
        budget = w.budget(derived_id)["data"]
        assert budget["limits"] == BUDGET and len(budget["experiment_refs"]) == 1
        run = w.stage_run(derived_id)["data"]
        assert set(run["stages"]) == {"ablation"} and run["stages"]["ablation"]["state"] == "passed"
        assert run["stages"]["ablation"]["decision_class"] is not None  # recorded, never gating
        sampling = w.sampling(run["stages"]["ablation"]["experiment_ref"])
        assert (sampling["stage"], sampling["split"], sampling["arms"]) == (
            "ablation", "development", ["baseline", "candidate"],
        )  # fmt: skip
        assert sampling["max_tasks"] == 12 and len(sampling["case_ids"]) == 12
        experiment = w.experiment(run["stages"]["ablation"]["experiment_ref"])
        policy = w.store.get(w.scope, "analysis-plan", experiment["analysis_plan_ref"])["policy"]
        assert policy["purpose"] == "exploratory" and experiment["budget"] == BUDGET
    assert (
        seen == {"role_prompt", "env_bootstrap"} == set(plan_value(w, pid)["ablation_components"])
    )
    # the parent root holds screening and focused only: the ablation spends the derived roots
    assert len(w.budget(pid)["data"]["experiment_refs"]) == 2
    # the parent's evolution head is untouched by the ablation
    assert w.state(pid) == "screened"


def test_ic19_derived_proposals_run_without_a_human_review_even_for_class_b(w: World) -> None:
    _pid, _runner, derived = through_ablation(w, "b2")
    assert len(derived) == 2
    classes = set()
    for derived_id in derived:
        head = w.head(derived_id)
        classes.add(head["data"]["classification"]["surface_class"])
        assert head["state"] == "draft" and "review_ref" not in head["data"]
        assert w.stage_run(derived_id)["data"]["stages"]["ablation"]["state"] == "passed"
    # removing attempt_policy is a class B change: it still needed no review, because it brings
    # no new content (the parent's review and screen covered every component)
    assert "B" in classes


def test_ic19_a_derived_proposal_is_never_screened_nor_approved_for_an_experiment(
    w: World,
) -> None:
    pid, runner, derived = through_ablation(w)
    for derived_id in derived:
        hold("DERIVED_PROPOSAL", w.ops.screen, derived_id)
        hold("META_STATE", w.ops.approve_experiment, derived_id, max_tokens=10, max_wall_seconds=10)
        hold("DERIVED_PROPOSAL", runner.plan, derived_id, cell_id=CELL, root_budget=BUDGET)
        hold("DERIVED_PROPOSAL", w.ops.search, derived_id, cell_id=CELL, root_budget=BUDGET)
        hold("DERIVED_PROPOSAL", w.ops.derived_proposal, derived_id, slot="role_prompt", reason="x")
        assert w.state(derived_id) == "draft"
    # even the whole search to the end leaves every derived head at draft
    runner.approve_stage(pid, "holdout")
    assert {w.state(d) for d in derived} == {"draft"}
    assert w.state(pid) == "offline_evaluated"


def test_derived_proposal_accepts_a_slot_or_a_component_name(w: World) -> None:
    pid, _runner = planned(w)
    by_slot = w.ops.derived_proposal(pid, slot="prompt_bundle_ref", reason="by slot")
    by_name = w.ops.derived_proposal(pid, slot="role_prompt", reason="by name")
    for derived_id in (by_slot, by_name):
        assert w.state(derived_id) == "draft"
        assert stages.is_derived(w.store, w.scope, w.proposal(derived_id))
    assert w.proposal(by_slot)["candidate_ref"] == w.proposal(by_name)["candidate_ref"]


def test_derived_proposal_holds_for_a_slot_the_parent_did_not_change(w: World) -> None:
    pid, _ = planned(w)
    error = hold("DERIVED_PROPOSAL", w.ops.derived_proposal, pid, slot="limits", reason="x")
    assert error.details == {"slot": "limits", "changed": ["env_bootstrap", "role_prompt"]}


def test_derived_proposal_needs_a_reason_and_a_stage_plan(w: World) -> None:
    pid, _ = planned(w)
    fault("DERIVED_REASON", w.ops.derived_proposal, pid, slot="role_prompt", reason="  ")
    unplanned = w.propose("a2")
    hold("META_STATE", w.ops.derived_proposal, unplanned, slot="role_prompt", reason="x")


def parent_and_variant(w: World, **slots: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """A parent proposal and a derived-shaped dict whose candidate sets `slots` on the
    parent's candidate."""
    pid = w.propose("a2")
    parent = w.proposal(pid)
    manifests = w.ops.manifests
    manifest = manifests.change(manifests.of_composition(parent["candidate_ref"]), **slots)
    variant = manifests.materialize(
        w.local.proposer, base_composition_ref=parent["candidate_ref"], manifest=manifest,
        suffix="drv" + str(w.counter),
    )  # fmt: skip
    return parent, {"baseline_ref": parent["candidate_ref"], "candidate_ref": variant}


def check(w: World, parent: dict[str, Any], derived: dict[str, Any]) -> None:
    stages.check_derived(w.store, w.scope, w.ops.manifests, parent, derived)


def test_check_derived_holds_for_new_content_outside_the_parents_two_manifests(
    w: World,
) -> None:
    parent, derived = parent_and_variant(
        w, limits=w.component("limits", "new", max_wall_seconds=1700)
    )
    error = hold("DERIVED_PROPOSAL", check, w, parent, derived)
    assert error.details == {"slots": ["limits"]}
    # a third version of a slot the parent did change is new content as well
    parent, derived = parent_and_variant(
        w, env_bootstrap=w.component("env_bootstrap", "third", enabled=True, tree_depth=3)
    )
    error = hold("DERIVED_PROPOSAL", check, w, parent, derived)
    assert error.details == {"slots": ["env_bootstrap"]}


def test_check_derived_accepts_a_slot_back_at_the_parents_baseline(w: World) -> None:
    pid = w.propose("a2")
    parent = w.proposal(pid)
    manifests = w.ops.manifests
    base = manifests.of_composition(parent["baseline_ref"])
    # reverting the role prompt to the baseline's is not new content
    manifest = manifests.change(
        manifests.of_composition(parent["candidate_ref"]), prompt_bundle_ref=base.prompt_bundle_ref
    )
    variant = manifests.materialize(
        w.local.proposer, base_composition_ref=parent["candidate_ref"], manifest=manifest,
        suffix="back",
    )  # fmt: skip
    check(w, parent, {"baseline_ref": parent["candidate_ref"], "candidate_ref": variant})


def test_check_derived_holds_when_the_baseline_is_not_the_parents_candidate(w: World) -> None:
    parent, derived = parent_and_variant(w)
    derived = {**derived, "baseline_ref": parent["baseline_ref"]}
    hold("DERIVED_PROPOSAL", check, w, parent, derived)


def test_check_derived_holds_for_a_composition_that_cannot_be_read(w: World) -> None:
    parent, derived = parent_and_variant(w)
    ghost = {"id": "app-codex__ghost", "revision": 1, "digest": "sha256:" + "0" * 64}
    error = hold("DERIVED_PROPOSAL", check, w, parent, {**derived, "candidate_ref": ghost})
    assert "cannot be read" in str(error)


# -- ablation never gates (§8.1): unbuildable variants and an interrupted ablation ----------------
class Crash(BaseException):
    """A process stop inside a stage (not a RuntimeFault: nothing records it)."""


def combination_parent(w: World, *, with_prompt: bool) -> str:
    """A reviewed class B parent whose attempt_policy (fresh_base, feedback) and feedback_form
    (header v1_fresh) only combine with each other (§2.3): leaving either one out fails
    `policies.check_combination` with MANIFEST_COMBINATION. `with_prompt` adds a role prompt,
    whose leave-one-out variant builds."""
    suffix = f"combo{w.counter + 1}"
    changes = {
        "attempt_policy": w.component(
            "attempt_policy", suffix, repair_base="fresh_base", feedback=True
        ),
        "feedback_form": w.component("feedback_form", suffix, header="v1_fresh"),
    }
    if with_prompt:
        changes["role_prompt"] = w.role_prompt(suffix)
    pid = w.propose("b2", suffix=suffix, changes=changes)
    w.script(pid)
    w.ops.review(pid, outcome="pass", note="read the attempt policy and the feedback header")
    return pid


def to_ablation(w: World, pid: str) -> StageRunner:
    runner = w.runner()
    runner.plan(pid, cell_id=CELL, root_budget=BUDGET)
    runner.advance(pid)
    focused = runner.approve_stage(pid, "focused")
    assert focused.state == "passed"
    return runner


def derived_of(w: World, pid: str) -> list[str]:
    """Every proposal that names the parent's stage plan as its experiment plan (IC-19 A)."""
    plan_ref = w.stage_run(pid)["data"]["plan_ref"]
    return [
        value["proposal_id"]
        for _, value in w.objects("harness-change-proposal")
        if value["experiment_plan_ref"] == plan_ref
    ]


def test_an_unbuildable_leave_one_out_variant_is_skipped_and_the_holdout_gate_still_opens(
    w: World,
) -> None:
    pid = combination_parent(w, with_prompt=True)
    runner = to_ablation(w, pid)
    assert sorted(plan_value(w, pid)["ablation_components"]) == [
        "attempt_policy", "feedback_form", "role_prompt",
    ]  # fmt: skip
    steps = runner.advance(pid)
    assert states(steps)["ablation"] == "passed"  # one variant measured, never gating
    assert w.step(steps, "holdout").waiting_for == "approve-stage holdout"
    ablation = w.stage_run(pid)["data"]["stages"]["ablation"]
    assert sorted(ablation["guard_findings"]) == [
        "ABLATION attempt_policy: MANIFEST_COMBINATION",
        "ABLATION feedback_form: MANIFEST_COMBINATION",
    ]
    # only the role prompt variant exists and ran; the unbuildable ones left no proposal
    assert ablation["ablation_proposals"] == derived_of(w, pid) and len(derived_of(w, pid)) == 1
    run = w.stage_run(ablation["ablation_proposals"][0])["data"]["stages"]["ablation"]
    assert run["state"] == "passed" and run["report_ref"] is not None
    assert w.ops.stages(pid)["findings"] == {"ablation": ablation["guard_findings"]}
    holdout = runner.approve_stage(pid, "holdout")
    assert holdout.state == "passed" and w.state(pid) == "offline_evaluated"


def test_an_ablation_whose_variants_all_fail_to_build_is_skipped_not_blocking(w: World) -> None:
    pid = combination_parent(w, with_prompt=False)
    runner = to_ablation(w, pid)
    calls = len(w.executor.calls)
    result = w.ops.search(pid, cell_id=CELL, root_budget=BUDGET)
    steps = {s["stage"]: s for s in result["steps"]}
    assert steps["ablation"]["state"] == "skipped"  # nothing measured
    assert steps["holdout"]["waiting_for"] == "approve-stage holdout"
    assert result["stopped"] is None  # the ablation never stops the search
    assert sorted(result["findings"]["ablation"]) == [
        "ABLATION attempt_policy: MANIFEST_COMBINATION",
        "ABLATION feedback_form: MANIFEST_COMBINATION",
    ]
    assert len(w.executor.calls) == calls and derived_of(w, pid) == []
    assert runner.approve_stage(pid, "holdout").state == "passed"


@pytest.mark.parametrize("where", ["before_freeze", "before_run"])
def test_an_interrupted_ablation_finishes_only_the_unfinished_variant(
    w: World, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    pid, runner = planned(w)
    runner.advance(pid)
    runner.approve_stage(pid, "focused")
    seen = {"n": 0}
    if where == "before_freeze":
        real_experiment = runner._experiment

        def stop_at_freeze(proposal_id: str, *args: Any, **kwargs: Any) -> Any:
            if proposal_id != pid:  # a derived proposal's freeze
                seen["n"] += 1
                if seen["n"] == 2:
                    raise Crash()
            return real_experiment(proposal_id, *args, **kwargs)

        monkeypatch.setattr(runner, "_experiment", stop_at_freeze)
    else:
        real_run = w.local.evaluation.run

        def stop_at_run(*args: Any, **kwargs: Any) -> Any:
            seen["n"] += 1
            if seen["n"] == 2:
                raise Crash()
            return real_run(*args, **kwargs)

        monkeypatch.setattr(w.local.evaluation, "run", stop_at_run)
    with pytest.raises(Crash):
        runner.advance(pid)
    monkeypatch.undo()
    ablation = w.stage_run(pid)["data"]["stages"]["ablation"]
    assert ablation["state"] == "running" and len(ablation["ablation_proposals"]) == 2
    first, second = ablation["ablation_proposals"]
    assert w.stage_run(first)["data"]["stages"]["ablation"]["state"] == "passed"
    interrupted = w.stage_run(second)["data"]["stages"]["ablation"]
    assert interrupted["state"] == ("pending" if where == "before_freeze" else "running")
    first_report = w.stage_run(first)["data"]["stages"]["ablation"]["report_ref"]

    # the next search (another process: a new runner) resumes the ablation
    calls = len(w.executor.calls)
    steps = w.runner().advance(pid)
    assert states(steps)["ablation"] == "passed"
    assert w.step(steps, "holdout").waiting_for == "approve-stage holdout"
    ablation = w.stage_run(pid)["data"]["stages"]["ablation"]
    assert ablation["ablation_proposals"] == [first, second] and ablation["guard_findings"] == []
    assert sorted(derived_of(w, pid)) == sorted([first, second])  # no second variant set
    # the finished variant is not run again; the interrupted one runs its 12 tasks x 2 arms
    assert len(w.executor.calls) - calls == 24
    assert w.stage_run(first)["data"]["stages"]["ablation"]["report_ref"] == first_report
    done = w.stage_run(second)["data"]["stages"]["ablation"]
    assert done["state"] == "passed" and done["report_ref"] is not None
    # one stage experiment on the interrupted variant's root: a frozen one is run, not re-frozen
    assert len(w.budget(second)["data"]["experiment_refs"]) == 1


def test_a_variant_created_just_before_an_interruption_is_adopted_not_duplicated(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid, runner = planned(w)
    runner.advance(pid)
    runner.approve_stage(pid, "focused")
    real = w.ops.derived_proposal
    seen = {"n": 0}

    def stop_after_submit(*args: Any, **kwargs: Any) -> str:
        made = real(*args, **kwargs)
        seen["n"] += 1
        if seen["n"] == 2:
            raise Crash()  # submitted, not yet recorded in the parent's stage-run head
        return made

    monkeypatch.setattr(w.ops, "derived_proposal", stop_after_submit)
    with pytest.raises(Crash):
        runner.advance(pid)
    monkeypatch.undo()
    assert len(w.stage_run(pid)["data"]["stages"]["ablation"]["ablation_proposals"]) == 1
    assert len(derived_of(w, pid)) == 2
    steps = w.runner().advance(pid)
    assert states(steps)["ablation"] == "passed"
    ablation = w.stage_run(pid)["data"]["stages"]["ablation"]
    assert sorted(ablation["ablation_proposals"]) == sorted(derived_of(w, pid))
    assert len(derived_of(w, pid)) == 2  # the orphan is the second variant
    for derived_id in ablation["ablation_proposals"]:
        assert w.stage_run(derived_id)["data"]["stages"]["ablation"]["state"] == "passed"


def test_a_dispatched_variant_is_never_replayed_and_does_not_block_the_holdout_gate(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid, runner = planned(w)
    runner.advance(pid)
    runner.approve_stage(pid, "focused")
    real_run = w.local.evaluation.run
    seen = {"n": 0}

    def stop_in_a_trial(actor: Any, ref: Any, executor: Any, **kwargs: Any) -> Any:
        seen["n"] += 1
        if seen["n"] == 2:

            def lost(*_: Any, **__: Any) -> Any:
                raise Crash()

            return real_run(actor, ref, lost, **kwargs)
        return real_run(actor, ref, executor, **kwargs)

    monkeypatch.setattr(w.local.evaluation, "run", stop_in_a_trial)
    with pytest.raises(Crash):
        runner.advance(pid)
    monkeypatch.undo()
    first, second = w.stage_run(pid)["data"]["stages"]["ablation"]["ablation_proposals"]
    experiment_ref = w.stage_run(second)["data"]["stages"]["ablation"]["experiment_ref"]
    experiment_id = w.experiment(experiment_ref)["experiment_id"]
    assert w.store.head(w.scope, "experiment", experiment_id)["state"] == "running"
    calls = len(w.executor.calls)
    steps = w.runner().advance(pid)
    assert len(w.executor.calls) == calls  # no automatic re-execution (EXPERIMENT_REPLAY)
    assert states(steps)["ablation"] == "passed"  # the first variant was measured
    assert w.step(steps, "holdout").waiting_for == "approve-stage holdout"
    name = stages.component_name(
        w.ops.manifests.diff(
            w.ops.manifests.of_composition(w.proposal(pid)["candidate_ref"]),
            w.ops.manifests.of_composition(w.proposal(second)["candidate_ref"]),
        )[0].slot
    )
    ablation = w.stage_run(pid)["data"]["stages"]["ablation"]
    assert ablation["guard_findings"] == [f"ABLATION {name}: EXPERIMENT_REPLAY"]
    aborted = w.stage_run(second)["data"]["stages"]["ablation"]
    assert aborted["state"] == "aborted" and aborted["guard_findings"] == ["EXPERIMENT_REPLAY"]
    assert w.stage_run(first)["data"]["stages"]["ablation"]["state"] == "passed"


def test_a_variant_frozen_before_a_task_edit_is_not_run_and_does_not_gate(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid, runner = planned(w)
    runner.advance(pid)
    runner.approve_stage(pid, "focused")
    real_run = w.local.evaluation.run
    seen = {"n": 0}

    def stop_at_run(*args: Any, **kwargs: Any) -> Any:
        seen["n"] += 1
        if seen["n"] == 2:
            raise Crash()  # the second variant is frozen, not run
        return real_run(*args, **kwargs)

    monkeypatch.setattr(w.local.evaluation, "run", stop_at_run)
    with pytest.raises(Crash):
        runner.advance(pid)
    monkeypatch.undo()
    _first, second = w.stage_run(pid)["data"]["stages"]["ablation"]["ablation_proposals"]
    frozen = w.sampling(w.stage_run(second)["data"]["stages"]["ablation"]["experiment_ref"])
    edit = frozen["case_ids"][0]
    # the next process loads a corpus whose task of a frozen case changed
    w.ops.corpus = replace(
        w.ops.corpus,
        tasks=tuple(
            replace(t, objective="Make add return the product.") if t.task_id == edit else t
            for t in w.ops.corpus.tasks
        ),
    )
    calls = len(w.executor.calls)
    resumed = w.runner()
    steps = resumed.advance(pid)
    assert len(w.executor.calls) == calls  # the frozen variant does not run another task
    assert states(steps)["ablation"] == "passed"
    assert w.step(steps, "holdout").waiting_for == "approve-stage holdout"
    name = resumed._left_out(w.proposal(pid), w.proposal(second))
    ablation = w.stage_run(pid)["data"]["stages"]["ablation"]
    assert ablation["guard_findings"] == [f"ABLATION {name}: CORPUS_CHANGED"]
    variant = w.stage_run(second)["data"]["stages"]["ablation"]
    assert variant["state"] == "aborted" and variant["guard_findings"] == ["CORPUS_CHANGED"]


# ==================================================================================================
# review (§3.9): class B human code review
# ==================================================================================================
def test_review_pass_admits_the_operator_receipt_and_screen_then_passes(w: World) -> None:
    pid = w.propose("b2")
    hold("CODE_REVIEW_REQUIRED", w.local.meta.screen, w.operator, pid)
    out = w.ops.review(pid, outcome="pass", note="  read both components  ")
    assert out["outcome"] == "pass" and out["state"] == "draft" and out["review_ref"]
    assert w.head(pid)["data"]["review_ref"] == out["review_ref"]
    review = w.store.get(w.scope, "harness-review", out["review_ref"])
    assert review["reviewer"]["kind"] == "human"
    assert review["reviewer"]["subject_id"] == w.operator.subject_id
    receipt = read_receipt(w.artifacts.read(w.scope, review["artifact_ref"], trusted=True))
    assert receipt["outcome"] == "pass" and receipt["note"] == "read both components"
    assert receipt["protected_controls_changed"] is False
    assert w.local.meta.screen(w.operator, pid)["state"] == "screened"


def test_review_fail_rejects_the_proposal_and_leaves_no_review_ref(w: World) -> None:
    pid = w.propose("b2")
    out = w.ops.review(pid, outcome="fail", note="the retry rule drops the feedback")
    assert out == {"outcome": "fail", "review_ref": None, "state": "rejected"}
    head = w.head(pid)
    assert head["state"] == "rejected" and "review_ref" not in head["data"]
    rejection = head["data"]["rejection"]
    assert rejection["reason"] == "code review failed: the retry rule drops the feedback"
    assert rejection["from_state"] == "draft" and rejection["by"]["kind"] == "human"
    assert w.objects("harness-review") == []
    hold("META_STATE", w.runner().plan, pid, cell_id=CELL, root_budget=BUDGET)


def test_review_needs_a_note_and_a_known_outcome(w: World) -> None:
    pid = w.propose("b2")
    for outcome in ("pass", "fail"):
        fault("REVIEW_NOTE", w.ops.review, pid, outcome=outcome, note="   ")
        fault("REVIEW_NOTE", w.ops.review, pid, outcome=outcome, note="")
    fault("REVIEW_OUTCOME", w.ops.review, pid, outcome="maybe", note="read it")
    assert w.state(pid) == "draft" and "review_ref" not in w.head(pid)["data"]


def test_the_proposer_identity_cannot_review_its_own_candidate(w: World) -> None:
    pid = w.propose("b2")
    fault("FORBIDDEN", w.local.meta.record_review, w.local.proposer, pid, w.refs["corpus_ref"])
    assert "review_ref" not in w.head(pid)["data"]


# ==================================================================================================
# leak gate (§10.4): the MetaHarness.screen hook and the plan check
# ==================================================================================================
def leaky(w: World, line: str, suffix: str = "leak") -> str:
    component = w.ops.components.register(
        w.local.proposer, component_id=f"role_prompt.{suffix}", kind="role_prompt",
        content={"implementer": [*GOOD, line]}, source="proposer", rationale="leak test",
    )  # fmt: skip
    return w.propose("a1", suffix=suffix, changes={"role_prompt": component})


def test_screen_refuses_a_candidate_that_names_a_validation_hidden_test(w: World) -> None:
    secret = "test_bug_val_00_ok"
    pid = leaky(w, f"Make {secret} pass first.")
    error = hold("LEAK_GATE", w.ops.screen, pid)
    assert w.state(pid) == "draft"  # the screen did not move the machine
    assert error.details and all(f["code"] == "LEAK_GATE" for f in error.details)
    assert all(f["severity"] == "blocking" for f in error.details)
    assert "hidden test name" in error.details[0]["statement"]
    assert "/components/0/implementer/2" in error.details[0]["statement"]  # where, never the token
    assert secret not in repr(error.details) and secret not in str(error)
    assert error.details[0]["source_refs"][0]["id"] == "leak-s11-corpus"


def test_screen_refuses_a_holdout_hidden_test_and_a_task_id_in_any_case(w: World) -> None:
    hold("LEAK_GATE", w.ops.screen, leaky(w, "also check test_data_hol_08_ok", "hol"))
    for text in ("BUG-HOL-00", "bug-val-05"):
        pid = w.propose("a1", suffix="id" + text[-2:], hypothesis=f"Fix {text} properly")
        error = hold("LEAK_GATE", w.ops.screen, pid)
        assert "task id" in error.details[0]["statement"]
        assert text.lower() not in repr(error.details).lower()


def test_a_development_task_is_not_a_leak(w: World) -> None:
    pid = leaky(w, "Think about test_bug_dev_00_ok and bug-dev-05.", "dev")
    assert w.ops.screen(pid)["state"] == "screened"


def test_search_stops_at_the_leak_gate_before_any_stage_runs(w: World) -> None:
    pid = leaky(w, "Run test_feature_val_01_ok first.", "stop")
    w.script(pid)
    hold("LEAK_GATE", w.ops.search, pid, cell_id=CELL, root_budget=BUDGET)
    assert w.state(pid) == "draft" and w.objects("eval-experiment") == []
    assert w.executor.calls == [] and w.objects("stage-plan") == []


def test_the_screen_hook_refuses_even_when_the_plan_check_is_bypassed(w: World) -> None:
    pid = leaky(w, "Run test_refactor_hol_02_ok.", "bypass")
    w.script(pid)
    runner = w.runner()
    # a runner whose gate finds nothing plans the proposal: MetaHarness.screen is the backstop
    runner.leak_gate = SimpleNamespace(findings=lambda scope, subject: [])  # type: ignore[assignment]
    runner.plan(pid, cell_id=CELL, root_budget=BUDGET)
    hold("LEAK_GATE", runner.advance, pid)
    assert w.state(pid) == "draft" and w.executor.calls == []
    assert w.stage_run(pid)["data"]["stages"]["screening"]["state"] == "pending"


def test_plan_refuses_the_leak_before_anything_is_planned(w: World) -> None:
    pid = leaky(w, "Run test_cli_ops_val_04_ok.", "plan")
    hold("LEAK_GATE", w.runner().plan, pid, cell_id=CELL, root_budget=BUDGET)
    assert w.objects("stage-plan") == []


def test_the_leak_hook_serves_one_scope_and_no_index_means_no_finding(w: World) -> None:
    from amplai_foundry.runtime.storage.store import Scope

    pid = w.propose("a1")
    fault("SCOPE_MISMATCH", w.local.leak_findings, Scope("other", "project"), w.proposal(pid))
    assert w.local.leak_findings(w.scope, w.proposal(pid)) == []
    assert len(w.local.leak_index_refs(w.scope)) == 1  # the frozen corpus has one leak index


# ==================================================================================================
# the real reference_validator (§7.4, IC-06): another cell or another component is refused
# ==================================================================================================
def strategy(w: World, n: int) -> dict[str, Any]:
    return w.ops.components.register(
        w.local.proposer, component_id=f"execution_strategy.bon{n}", kind="execution_strategy",
        content={"enabled": ["best_of_n"], "params": {"best_of_n": {"n": n}}}, source="proposer",
        rationale="reference arm test",
    )  # fmt: skip


def reference(
    w: World, base: dict[str, Any], suffix: str, n: int = 2, **slots: Any
) -> dict[str, Any]:
    manifests = w.ops.manifests
    manifest = manifests.change(
        manifests.of_composition(base), execution_strategy=strategy(w, n), **slots
    )
    return manifests.materialize(w.local.proposer, base_composition_ref=base, manifest=manifest,
                                 suffix=suffix)  # fmt: skip


def declared(ref: dict[str, Any], n: int = 2) -> dict[str, Any]:
    return {"reference_arm": {"composition_ref": ref, "kind": "best_of_n", "n": n,
                              "cost_match": "tokens"}}  # fmt: skip


def validate(w: World, baseline: dict[str, Any], policy: dict[str, Any]) -> None:
    w.local.reference_validator(w.scope, {"baseline_ref": baseline}, policy)


def installed(w: World) -> dict[str, Any]:
    ref: dict[str, Any] = w.rig.service.apps["app"].compositions[CELL]
    return ref


def test_the_validator_accepts_a_best_of_n_of_the_baseline_and_nothing_else(w: World) -> None:
    base = installed(w)
    validate(w, base, declared(reference(w, base, "r2")))
    validate(w, base, declared(reference(w, base, "r3", n=3), n=3))
    # attempt_policy is the other slot a reference may differ in (§7.4)
    wider = w.component("attempt_policy", "wide", max_attempts=2)
    validate(w, base, declared(reference(w, base, "r2b", attempt_policy=wider)))


def test_the_validator_refuses_a_reference_that_changes_another_component(w: World) -> None:
    base = installed(w)
    other = reference(
        w, base, "r2env", env_bootstrap=w.component("env_bootstrap", "r", enabled=True)
    )
    error = hold("REFERENCE_ARM", validate, w, base, declared(other))
    assert error.details == {"slots": ["env_bootstrap"]}
    alt = w.ops.manifests.of_composition(w.proposal(w.propose("a1"))["candidate_ref"])
    prompt = reference(w, base, "r2p", prompt_bundle_ref=alt.prompt_bundle_ref)
    error = hold("REFERENCE_ARM", validate, w, base, declared(prompt))
    assert error.details == {"slots": ["prompt_bundle_ref"]}


def test_the_validator_refuses_a_reference_of_another_cell(w: World) -> None:
    base = installed(w)
    value = dict(w.store.get(w.scope, "harness-composition", base))
    sibling = w.local.meta.compositions.register(
        w.local.proposer, {**value, "composition_id": "app-claude"}
    )  # a second cell with its own installed composition
    foreign = reference(w, sibling, "r2")
    w.local.installed_compositions = lambda: [{CELL: base, "claude-cli": sibling}]
    error = hold("REFERENCE_ARM", validate, w, base, declared(foreign))
    assert error.details == {"cells": [CELL, "claude-cli"]}
    # the same reference is fine for its own cell's baseline
    validate(w, sibling, declared(foreign))


def test_the_validator_refuses_what_it_cannot_pin(w: World) -> None:
    base = installed(w)
    value = dict(w.store.get(w.scope, "harness-composition", base))
    stranger = w.local.meta.compositions.register(
        w.local.proposer, {**value, "composition_id": "stranger"}
    )
    hold("REFERENCE_ARM", validate, w, base, declared(reference(w, stranger, "x")))  # not pinnable
    hold("REFERENCE_ARM", validate, w, stranger, declared(reference(w, base, "r2")))  # baseline
    ghost = {"id": "app-codex__ghost", "revision": 1, "digest": "sha256:" + "0" * 64}
    hold("REFERENCE_ARM", validate, w, base, declared(ghost))


def test_the_validator_refuses_a_wrong_n_or_a_non_best_of_n_reference(w: World) -> None:
    base = installed(w)
    two = reference(w, base, "r2")
    hold("REFERENCE_ARM", validate, w, base, declared(two, n=3))  # declared n differs from content
    for bad in (0, 4, True, "2", None):
        hold("REFERENCE_ARM", validate, w, base, declared(two, n=bad))  # type: ignore[arg-type]
    hold("REFERENCE_ARM", validate, w, base, declared(base))  # the baseline is not a best_of_n
    hold("REFERENCE_ARM", validate, w, base, {})
    hold("REFERENCE_ARM", validate, w, base, {"reference_arm": {"n": 2}})
    hold("REFERENCE_ARM", validate, w, base, {"reference_arm": "two"})


def test_a_refused_reference_freezes_nothing(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    pid, runner = planned(w, baseline=(2, 2), candidate=(4, 4))
    runner.advance(pid)
    base = w.proposal(pid)["baseline_ref"]
    bad = reference(w, base, "r2env", env_bootstrap=w.component("env_bootstrap", "r", enabled=True))
    monkeypatch.setattr(runner, "_reference", lambda *a, **k: ((bad, 2), "scripted reference"))
    frozen = len(w.objects("eval-experiment"))
    hold("REFERENCE_ARM", runner.approve_stage, pid, "focused")
    assert len(w.objects("eval-experiment")) == frozen  # the evaluator refused before freezing
    assert w.stage_run(pid)["data"]["stages"]["focused"]["state"] == "waiting_approval"


# ==================================================================================================
# the baseline-reuse cache (§8.5): exploratory stages only
# ==================================================================================================
KEY_FIELDS = {
    "manifest_digest", "cell", "task_artifact_digest", "corpus_version", "harness_sha",
    "strategy", "repeat_slot",
}  # fmt: skip
CELL_FIELDS = {"provider_model_id", "reasoning_profile", "driver_version", "image"}


def case_digests(w: World, *splits: str) -> dict[str, str]:
    corpus = w.store.get(w.scope, "eval-corpus", w.refs["corpus_ref"])
    return {
        c["case_id"]: c["artifact_ref"]["digest"] for c in corpus["cases"] if c["split"] in splits
    }


def test_screening_trials_are_indexed_by_the_section_8_5_key(w: World) -> None:
    pid, runner = planned(w)
    runner.advance(pid)
    heads = w.heads("observation-cache")
    assert len(heads) == 24  # 12 tasks x 2 arms, one repeat
    dev = case_digests(w, "development")
    keys = []
    for head_id, state, data in heads:
        assert state == "active" and head_id == "obs-" + digest(data["key"])[7:31]
        key = data["key"]
        assert set(key) == KEY_FIELDS and set(key["cell"]) == CELL_FIELDS
        assert key["corpus_version"] == "1.0.0" and key["harness_sha"] == HARNESS_SHA
        assert key["strategy"] == "repair_loop" and key["repeat_slot"] == 0
        assert key["cell"]["provider_model_id"] == "model-x" and key["cell"]["image"] == "img:1"
        assert key["task_artifact_digest"] in dev.values()
        assert len(data["trial_refs"]) == 1 and data["calibration_trial_refs"] == []
        keys.append(key)
    assert len({k["manifest_digest"] for k in keys}) == 2  # baseline and candidate config hashes
    assert len({k["task_artifact_digest"] for k in keys}) == 12


def test_only_exploratory_stages_feed_the_cache_never_validation_or_holdout(w: World) -> None:
    pid, runner = planned(w)
    runner.advance(pid)
    after_screening = len(w.heads("observation-cache"))
    runner.approve_stage(pid, "focused")
    assert len(w.heads("observation-cache")) == after_screening  # confirmatory: nothing added
    runner.advance(pid)  # ablation (development): derived trials are indexed
    after_ablation = len(w.heads("observation-cache"))
    assert after_ablation == 48 > after_screening
    runner.approve_stage(pid, "holdout")
    heads = w.heads("observation-cache")
    assert len(heads) == after_ablation
    held_out = set(case_digests(w, "validation", "holdout").values())
    assert all(h[2]["key"]["task_artifact_digest"] not in held_out for h in heads)
    # the parent candidate's keys are reused by both ablation baselines: three trials per head
    assert sorted(len(h[2]["trial_refs"]) for h in heads).count(3) == 12


def test_a_second_proposal_over_the_same_baseline_shares_its_cache_keys(w: World) -> None:
    first, runner = planned(w)
    runner.advance(first)
    second = w.propose("a1")
    w.script(second)
    again = w.runner()
    again.plan(second, cell_id=CELL, root_budget=BUDGET)
    calls = len(w.executor.calls)
    steps = again.advance(second)
    assert w.step(steps, "screening").state == "passed"
    heads = w.heads("observation-cache")
    assert len(heads) == 36  # 12 shared baseline keys + 12 + 12 candidate keys
    assert sorted(len(h[2]["trial_refs"]) for h in heads).count(2) == 12
    # a frozen experiment never substitutes a cached trial: every trial ran again
    assert len(w.executor.calls) - calls == 24
    refs = {json.dumps(r, sort_keys=True) for h in heads for r in h[2]["trial_refs"]}
    assert len(refs) == 48


@pytest.mark.parametrize(
    "change",
    [
        {"snapshot": {**SNAPSHOT, "provider_model_id": "model-y"}},
        {"snapshot": {**SNAPSHOT, "driver_version": "0.156.0"}},
        {"snapshot": {**SNAPSHOT, "image": "img:2"}},
        {"corpus_version": "2.0.0"},
        {"harness_sha": "2" * 40},
    ],
)
def test_a_changed_model_snapshot_corpus_version_or_harness_sha_is_another_key(
    w: World, change: dict[str, Any]
) -> None:
    first, runner = planned(w)
    runner.advance(first)
    second = w.propose("a1")
    executor = w.script(second)
    for name, value in change.items():
        setattr(executor, name, value)
    again = w.runner()
    again.plan(second, cell_id=CELL, root_budget=BUDGET)
    again.advance(second)
    heads = w.heads("observation-cache")
    assert len(heads) == 48  # nothing shared: the baseline trials sit under their own keys
    assert {len(h[2]["trial_refs"]) for h in heads} == {1}


def test_the_key_is_not_reused_without_a_known_harness_sha_or_a_model_snapshot(
    w: World,
) -> None:
    for name, value in (("harness_sha", "unknown"), ("harness_sha", None), ("snapshot", None)):
        pid = w.propose("a1")
        executor = w.script(pid)
        setattr(executor, name, value)
        runner = w.runner()
        runner.plan(pid, cell_id=CELL, root_budget=BUDGET)
        assert w.step(runner.advance(pid), "screening").state == "passed"
        assert w.heads("observation-cache") == [], (name, value)


def test_cache_key_rebuilds_the_receipts_key_and_refuses_anything_else(w: World) -> None:
    pid, runner = planned(w)
    screening = w.step(runner.advance(pid), "screening")
    trial = w.trials(screening.report_ref)[0]
    receipt = read_receipt(w.artifacts.read(w.scope, trial["artifact_refs"][0], trusted=True))
    case = next(
        c for c in w.store.get(w.scope, "eval-corpus", w.refs["corpus_ref"])["cases"]
        if c["case_id"] == trial["task_id"]
    )  # fmt: skip
    key = runner.cache_key(trial, receipt, case)
    assert key is not None and digest(key) == receipt["cache_key"]
    assert key["repeat_slot"] == trial["repeat"]
    assert key["task_artifact_digest"] == case["artifact_ref"]["digest"]
    assert key["corpus_version"] == receipt["corpus_version"]
    assert key["harness_sha"] == HARNESS_SHA
    refused = [
        {**receipt, "harness_sha": None},
        {**receipt, "harness_sha": "unknown"},
        {**receipt, "model_snapshot": None},
        {**receipt, "model_snapshot": "model-x"},
        {**receipt, "cache_key": "sha256:" + "0" * 64},  # not the key of what ran
        {**receipt, "corpus_version": "9.9.9"},  # the rebuilt key differs from the receipt's
        {**receipt, "model_snapshot": {**SNAPSHOT, "image": "img:other"}},
    ]
    for tampered in refused:
        assert runner.cache_key(trial, tampered, case) is None
    assert runner.cache_key(trial, receipt, {**case, "artifact_ref": None}) is None
    assert (
        runner.cache_key(trial, receipt, {k: v for k, v in case.items() if k != "artifact_ref"})
        is None
    )


def test_recording_a_report_twice_never_duplicates_a_cached_trial(w: World) -> None:
    pid, runner = planned(w)
    screening = w.step(runner.advance(pid), "screening")
    report = w.store.get(w.scope, "eval-report", screening.report_ref)
    cases = CorpusService(w.store, w.artifacts).select(
        w.operator, w.refs["corpus_ref"], "development", purpose="frozen_experiment"
    )
    before = {h[0]: len(h[2]["trial_refs"]) for h in w.heads("observation-cache")}
    runner._record(report, cases, exploratory=True)
    assert {h[0]: len(h[2]["trial_refs"]) for h in w.heads("observation-cache")} == before
    # a confirmatory record writes metrics but no cache entry
    runner._record(report, cases, exploratory=False)
    assert {h[0]: len(h[2]["trial_refs"]) for h in w.heads("observation-cache")} == before


# ==================================================================================================
# §8.1: the ablation never gates the holdout stage
# ==================================================================================================
def after_focused(w: World) -> tuple[str, StageRunner, list[str]]:
    """A two-component proposal whose focused stage passed; the planned ablation components."""
    pid, runner = planned(w)
    runner.advance(pid)
    assert runner.approve_stage(pid, "focused").state == "passed"
    return pid, runner, list(plan_value(w, pid)["ablation_components"])


def ablation_of(w: World, pid: str) -> dict[str, Any]:
    value: dict[str, Any] = w.stage_run(pid)["data"]["stages"]["ablation"]
    return value


def test_a_non_runtime_fault_building_one_variant_is_a_finding_and_the_holdout_gate_opens(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid, runner, names = after_focused(w)
    real = w.ops.derived_proposal
    seen = {"n": 0}

    def broken_once(*args: Any, **kwargs: Any) -> str:
        seen["n"] += 1
        if seen["n"] == 1:
            raise ValueError("not a RuntimeFault")
        return real(*args, **kwargs)

    monkeypatch.setattr(w.ops, "derived_proposal", broken_once)
    steps = runner.advance(pid)
    assert states(steps)["ablation"] == "passed"  # the other variant passed
    assert w.step(steps, "holdout").waiting_for == "approve-stage holdout"
    assert ablation_of(w, pid)["guard_findings"] == [f"ABLATION {names[0]}: ValueError"]
    assert len(ablation_of(w, pid)["ablation_proposals"]) == 1
    assert runner.approve_stage(pid, "holdout").state == "passed"


def test_an_exception_inside_one_variant_run_aborts_that_variant_only(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid, runner, names = after_focused(w)
    real = runner._experiment
    seen = {"n": 0}

    def broken_freeze(proposal_id: str, *args: Any, **kwargs: Any) -> Any:
        if proposal_id != pid:
            seen["n"] += 1
            if seen["n"] == 1:
                raise KeyError("lost field")
        return real(proposal_id, *args, **kwargs)

    monkeypatch.setattr(runner, "_experiment", broken_freeze)
    steps = runner.advance(pid)
    assert states(steps)["ablation"] == "passed"
    assert w.step(steps, "holdout").waiting_for == "approve-stage holdout"
    ablation = ablation_of(w, pid)
    assert ablation["guard_findings"] == [f"ABLATION {names[0]}: KeyError"]
    first, second = ablation["ablation_proposals"]
    assert ablation_of(w, first)["state"] == "aborted"
    assert ablation_of(w, first)["guard_findings"] == ["KeyError"]
    assert ablation_of(w, second)["state"] == "passed"
    # the finished stage is not run again by a later search
    calls = len(w.executor.calls)
    assert states(w.runner().advance(pid))["ablation"] == "passed"
    assert len(w.executor.calls) == calls


def test_variants_whose_runs_end_aborted_are_findings_not_measurements(w: World) -> None:
    pid, runner, names = after_focused(w)
    # every variant arm ("other") reports an unknown effect: each variant run stops
    w.executor.outcome = lambda role, case, repeat: (
        {"success": True, "unknown_effects": 1} if role == "other" else True
    )
    steps = runner.advance(pid)
    assert states(steps)["ablation"] == "skipped"  # nothing measured
    assert w.step(steps, "holdout").waiting_for == "approve-stage holdout"
    ablation = ablation_of(w, pid)
    assert sorted(ablation["guard_findings"]) == sorted(f"ABLATION {n}: aborted" for n in names)
    for derived_id in ablation["ablation_proposals"]:
        variant = ablation_of(w, derived_id)
        assert variant["state"] == "aborted" and variant["report_ref"] is not None
    w.executor.outcome = default_outcome
    assert runner.approve_stage(pid, "holdout").state == "passed"


@pytest.mark.parametrize("state", ["failed", "aborted", "inconclusive"])
def test_an_ablation_left_in_a_failed_state_is_closed_with_findings_never_blocking(
    w: World, state: str
) -> None:
    pid, runner, names = after_focused(w)
    # a state this module never writes for the parent (an earlier version, an external write)
    stages.update_stage(w.store, w.scope, pid, "ablation", state=state)
    calls = len(w.executor.calls)
    steps = runner.advance(pid)
    assert len(w.executor.calls) == calls  # closing runs nothing
    assert states(steps)["ablation"] == "skipped"
    assert w.step(steps, "holdout").waiting_for == "approve-stage holdout"
    assert ablation_of(w, pid)["guard_findings"] == [f"ABLATION {n}: {state}" for n in names]
    assert runner.approve_stage(pid, "holdout").state == "passed"


def test_closing_a_failed_ablation_keeps_its_passed_variants(w: World) -> None:
    pid, runner, _names = after_focused(w)
    assert states(runner.advance(pid))["ablation"] == "passed"
    stages.update_stage(w.store, w.scope, pid, "ablation", state="aborted")
    steps = runner.advance(pid)
    assert states(steps)["ablation"] == "passed"  # both variants passed before
    assert ablation_of(w, pid)["guard_findings"] == []
    assert w.step(steps, "holdout").waiting_for == "approve-stage holdout"


def test_a_failing_screening_still_stops_the_search(w: World) -> None:
    """Only the ablation is never gating: another auto stage left aborted stays there."""
    pid, runner = planned(w)
    stages.update_stage(w.store, w.scope, pid, "screening", state="aborted")
    steps = runner.advance(pid)
    assert states(steps)["screening"] == "aborted" and states(steps)["focused"] == "pending"
    assert w.executor.calls == []


# ==================================================================================================
# one runner per proposal (stage-lock, Hold SEARCH_BUSY)
# ==================================================================================================
def lock(w: World, pid: str) -> dict[str, Any]:
    return dict(w.store.head(w.scope, "stage-lock", "stagelock-" + pid))


def test_a_second_runner_is_busy_while_the_first_runs_a_variant_and_never_aborts_it(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid, runner, _names = after_focused(w)
    real_run = w.local.evaluation.run
    refused: list[Hold] = []

    def run_with_a_rival(*args: Any, **kwargs: Any) -> Any:
        if not refused:
            derived = ablation_of(w, pid)["ablation_proposals"]
            for attempt in (
                lambda: w.runner().advance(pid),
                lambda: w.runner().approve_stage(pid, "holdout"),
                lambda: w.ops.search(pid, cell_id=CELL, root_budget=BUDGET),
                lambda: w.ops.reconcile(derived[0], stage="ablation"),
            ):
                refused.append(hold("SEARCH_BUSY", attempt))
        return real_run(*args, **kwargs)

    monkeypatch.setattr(w.local.evaluation, "run", run_with_a_rival)
    steps = runner.advance(pid)
    assert len(refused) == 4
    assert refused[0].details["proposal_id"] == pid
    assert states(steps)["ablation"] == "passed"
    ablation = ablation_of(w, pid)
    assert ablation["guard_findings"] == []  # the live variant was not aborted by the rival
    assert {ablation_of(w, d)["state"] for d in ablation["ablation_proposals"]} == {"passed"}
    assert lock(w, pid)["state"] == "released"


def test_a_lock_of_an_ended_process_is_taken_over_and_one_of_this_process_is_busy(
    w: World,
) -> None:
    pid, runner = planned(w)
    stale = {"proposal_id": pid, "holder": "gone", "owner_epoch": w.store.epoch - 1,
             "acquired_at": now()}  # fmt: skip
    with w.store.tx() as db:
        w.store.cas(db, w.scope, "stage-lock", "stagelock-" + pid, 0, "held", stale)
    steps = runner.advance(pid)  # the earlier owner process ended: taken over
    assert states(steps)["screening"] == "passed"
    assert lock(w, pid)["state"] == "released" and lock(w, pid)["data"]["holder"] != "gone"
    live = {**stale, "holder": "live", "owner_epoch": w.store.epoch}
    with w.store.tx() as db:
        w.store.cas(db, w.scope, "stage-lock", "stagelock-" + pid, lock(w, pid)["row_version"],
                    "held", live)  # fmt: skip
    calls = len(w.executor.calls)
    hold("SEARCH_BUSY", runner.advance, pid)
    hold("SEARCH_BUSY", runner.approve_stage, pid, "focused")
    assert len(w.executor.calls) == calls
    assert lock(w, pid)["data"]["holder"] == "live"  # a refused runner leaves it alone


def test_the_lock_is_released_after_a_refusal_and_after_a_process_stop(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = w.propose("a2")
    hold("META_STATE", w.runner().advance, pid)  # no stage plan
    assert lock(w, pid)["state"] == "released"
    w.script(pid)
    runner = w.runner()
    runner.plan(pid, cell_id=CELL, root_budget=BUDGET)

    def stop(*_: Any, **__: Any) -> Any:
        raise Crash()

    monkeypatch.setattr(runner, "_stage", stop)
    with pytest.raises(Crash):
        runner.advance(pid)
    assert lock(w, pid)["state"] == "released"
    monkeypatch.undo()
    assert states(w.runner().advance(pid))["screening"] == "passed"


def test_lock_root_of_a_derived_proposal_is_its_parent(w: World) -> None:
    pid, _runner, derived = through_ablation(w)
    assert stages.lock_root(w.store, w.scope, derived[0]) == pid
    assert stages.lock_root(w.store, w.scope, pid) == pid
    assert stages.lock_root(w.store, w.scope, "harness-proposal-unplanned") == (
        "harness-proposal-unplanned"
    )


# ==================================================================================================
# IC-18 (provisional): the human operator's reconcile path
# ==================================================================================================
RECONCILE = {"stage": "screening"}


def test_only_the_human_operator_holds_experiment_reconcile() -> None:
    assert "experiment.reconcile" in META_OPERATOR_PERMISSIONS
    assert "experiment.reconcile" not in PROPOSER_PERMISSIONS


def test_reconcile_refuses_a_non_human_actor_and_a_proposer(w: World) -> None:
    pid, _runner = planned(w)
    service = replace(w.operator, kind="service")
    hold("APPROVAL_HUMAN", meta_ops.reconcile, w.dep, service, pid, **RECONCILE)
    hold("APPROVAL_HUMAN", meta_ops.reconcile, w.dep, w.local.proposer, pid, **RECONCILE)
    other_scope = replace(w.operator, scope=replace(w.scope, project_id="another"))
    hold("APPROVAL_HUMAN", meta_ops.reconcile, w.dep, other_scope, pid, **RECONCILE)
    proposing = replace(w.operator, permissions=w.operator.permissions | {"harness.propose"})
    hold("SELF_RECONCILE", meta_ops.reconcile, w.dep, proposing, pid, **RECONCILE)
    as_proposer = replace(w.operator, subject_id=PROPOSER_ID)
    hold("SELF_RECONCILE", meta_ops.reconcile, w.dep, as_proposer, pid, **RECONCILE)
    lacking = replace(w.operator, permissions=w.operator.permissions - {"experiment.reconcile"})
    fault("FORBIDDEN", meta_ops.reconcile, w.dep, lacking, pid, **RECONCILE)
    # nothing was touched: no stage lock was taken
    assert w.heads("stage-lock") == []


def test_reconcile_names_exactly_one_target(w: World) -> None:
    pid, _runner = planned(w)
    fault("RECONCILE_TARGET", w.ops.reconcile, pid)
    fault("RECONCILE_TARGET", w.ops.reconcile, pid, stage="screening", allocation_id="a")
    fault("RECONCILE_TARGET", w.ops.reconcile, pid, allocation_id="a", tokens=1, cost=0)


def test_reconcile_of_a_stage_needs_a_running_stage_experiment(w: World) -> None:
    pid = w.propose("a2")
    hold("META_STATE", w.ops.reconcile, pid, stage="screening")  # no stage run
    w.script(pid)
    runner = w.runner()
    runner.plan(pid, cell_id=CELL, root_budget=BUDGET)
    error = hold("META_STATE", w.ops.reconcile, pid, stage="screening")
    assert error.details == {"stage": "screening", "state": "pending"}
    runner.advance(pid)
    hold("META_STATE", w.ops.reconcile, pid, stage="screening")  # passed
    # the parent's ablation has no experiment of its own: its variants are reconciled
    stages.update_stage(w.store, w.scope, pid, "ablation", state="running")
    hold("META_STATE", w.ops.reconcile, pid, stage="ablation")


def stop_in_first_trial(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """The next ``EvaluationService.run`` stops the process inside its first trial."""
    real_run = w.local.evaluation.run

    def stop(actor: Any, ref: Any, executor: Any, **kwargs: Any) -> Any:
        def lost(*_: Any, **__: Any) -> Any:
            raise Crash()

        return real_run(actor, ref, lost, **kwargs)

    monkeypatch.setattr(w.local.evaluation, "run", stop)


def receipt_of(allocation_id: str, *, tokens: int = 4, stopped: bool = True) -> dict[str, Any]:
    return {"allocation_id": allocation_id, "tokens": tokens, "cost_microunits": 0,
            "process_stopped": stopped, "unknown_effects": 0}  # fmt: skip


def test_reconcile_ends_an_interrupted_stage_and_settles_its_reserved_allocation(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid, runner = planned(w)
    stop_in_first_trial(w, monkeypatch)
    with pytest.raises(Crash):
        runner.advance(pid)
    monkeypatch.undo()
    screening = w.stage_run(pid)["data"]["stages"]["screening"]
    assert screening["state"] == "running"
    experiment_id = w.experiment(screening["experiment_ref"])["experiment_id"]
    assert w.store.head(w.scope, "experiment", experiment_id)["state"] == "running"
    reserved = [k for k, v in w.budget(pid)["data"]["allocations"].items()
                if v["status"] == "reserved"]  # fmt: skip
    assert len(reserved) == 1
    # an interrupted auto stage stays where it stopped: the search does not re-run it
    assert states(w.runner().advance(pid))["screening"] == "running"
    # this process owns the store: its runner may still be live
    hold("EXPERIMENT_OWNER", w.ops.reconcile, pid, stage="screening")
    hold("EXPERIMENT_OWNER", w.ops.reconcile, pid, allocation_id=reserved[0], tokens=4,
         cost=0, receipt=receipt_of(reserved[0]))  # fmt: skip

    w.store.epoch += 1  # the next process owns the store
    result = w.ops.reconcile(pid, stage="screening")
    assert result["state"] == "aborted" and result["recovered"]["rerun_permitted"] is False
    assert [a["allocation_id"] for a in result["unresolved_allocations"]] == reserved
    assert w.store.head(w.scope, "experiment", experiment_id)["state"] == "interrupted"
    assert w.stage_run(pid)["data"]["stages"]["screening"]["state"] == "aborted"
    hold("META_STATE", w.ops.reconcile, pid, stage="screening")  # once
    # the reserved allocation: exact stopped-process evidence only
    hold("RECONCILIATION_EVIDENCE", w.ops.reconcile, pid, allocation_id=reserved[0], tokens=4,
         cost=0, receipt=receipt_of(reserved[0], stopped=False))  # fmt: skip
    hold("RECONCILIATION_STATE", w.ops.reconcile, pid, allocation_id="trial-nope", tokens=4,
         cost=0, receipt=receipt_of("trial-nope"))  # fmt: skip
    settled = w.ops.reconcile(
        pid, allocation_id=reserved[0], tokens=4, cost=0, receipt=receipt_of(reserved[0])
    )
    assert settled["status"] == "settled" and settled["unresolved_allocations"] == []
    allocation = w.budget(pid)["data"]["allocations"][reserved[0]]
    assert (
        allocation["status"] == "settled"
        and allocation["reconciliation_ref"] == (settled["evidence_ref"])
    )
    # no automatic re-execution: the aborted screening stays aborted
    assert states(w.runner().advance(pid))["screening"] == "aborted"


def test_reconcile_settles_an_unknown_effect_root_so_its_budget_admits_again(w: World) -> None:
    pid = w.propose("a2")
    w.script(pid, lambda role, case, repeat: {"success": True, "unknown_effects": 1})
    runner = w.runner()
    runner.plan(pid, cell_id=CELL, root_budget=BUDGET)
    runner.advance(pid)
    assert runner._covers(pid, plan_value(w, pid), 1)["code"] == "META_USAGE_UNKNOWN"
    unknown = [k for k, v in w.budget(pid)["data"]["allocations"].items()
               if v["status"] == "unknown"]  # fmt: skip
    assert len(unknown) == 1
    # a proposer never clears it, whatever it holds
    proposer = replace(w.local.proposer, kind="human",
                       permissions=PROPOSER_PERMISSIONS | {"experiment.reconcile"})  # fmt: skip
    hold("SELF_RECONCILE", meta_ops.reconcile, w.dep, proposer, pid, allocation_id=unknown[0],
         tokens=4, cost=0, receipt=receipt_of(unknown[0]))  # fmt: skip
    settled = w.ops.reconcile(
        pid, allocation_id=unknown[0], tokens=4, cost=0, receipt=receipt_of(unknown[0])
    )
    assert settled["unresolved_allocations"] == []
    assert runner._covers(pid, plan_value(w, pid), 1) is None


def test_an_interrupted_holdout_is_reconciled_and_then_aborted_by_the_operator(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid, runner, _derived = through_ablation(w)
    stop_in_first_trial(w, monkeypatch)
    with pytest.raises(Crash):
        runner.approve_stage(pid, "holdout")
    monkeypatch.undo()
    assert w.state(pid) == "offline_running"
    w.store.epoch += 1
    assert w.ops.reconcile(pid, stage="holdout")["state"] == "aborted"
    assert w.state(pid) == "offline_running"  # reconcile chains no gate
    assert w.ops.abort(pid, "holdout interrupted; reconciled")["state"] == "aborted"


# ==================================================================================================
# a gating stage stopped before its first dispatch resumes ("Clarifications After The S9/S11 Fix
# Wave": the open item); a stage with dispatched trials still needs reconcile
# ==================================================================================================
def stop_before_first_dispatch(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """The next ``EvaluationService.run`` stops the process before it turns the experiment
    ``running`` (the stage is already ``running``, its experiment still ``frozen``)."""

    def stop(*_: Any, **__: Any) -> Any:
        raise Crash()

    monkeypatch.setattr(w.local.evaluation, "run", stop)


def experiment_state(w: World, ref: dict[str, Any]) -> str:
    return str(w.store.head(w.scope, "experiment", w.experiment(ref)["experiment_id"])["state"])


def test_a_screening_stopped_before_its_first_dispatch_resumes_on_advance(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid, runner = planned(w)
    stop_before_first_dispatch(w, monkeypatch)
    with pytest.raises(Crash):
        runner.advance(pid)
    monkeypatch.undo()
    screening = w.stage_run(pid)["data"]["stages"]["screening"]
    frozen = screening["experiment_ref"]
    assert screening["state"] == "running" and experiment_state(w, frozen) == "frozen"
    assert w.executor.calls == []  # no trial ran
    experiments = len(w.objects("eval-experiment"))
    steps = w.runner().advance(pid)  # a new runner (the next process) resumes it
    assert states(steps)["screening"] == "passed"
    assert w.step(steps, "focused").waiting_for == "approve-stage focused"
    after = w.stage_run(pid)["data"]["stages"]["screening"]
    assert after["experiment_ref"] == frozen and experiment_state(w, frozen) == "evaluated"
    assert after["report_ref"] is not None
    assert len(w.objects("eval-experiment")) == experiments  # resumed, never frozen again
    assert len(w.budget(pid)["data"]["experiment_refs"]) == 1


def test_focused_and_holdout_stopped_before_their_first_dispatch_resume_on_advance(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid, runner = planned(w)
    runner.advance(pid)
    stop_before_first_dispatch(w, monkeypatch)
    with pytest.raises(Crash):
        runner.approve_stage(pid, "focused")
    monkeypatch.undo()
    focused = w.stage_run(pid)["data"]["stages"]["focused"]
    assert focused["state"] == "running" and experiment_state(w, focused["experiment_ref"]) == (
        "frozen"
    )
    # the operator approved that exact experiment: advance runs it, then the ablation
    steps = w.runner().advance(pid)
    assert states(steps)["focused"] == "passed" and states(steps)["ablation"] == "passed"
    assert (
        w.stage_run(pid)["data"]["stages"]["focused"]["experiment_ref"]
        == (focused["experiment_ref"])
    )
    assert w.step(steps, "holdout").waiting_for == "approve-stage holdout"
    # the holdout: approve_experiment and start_offline ran before the stop
    stop_before_first_dispatch(w, monkeypatch)
    with pytest.raises(Crash):
        runner.approve_stage(pid, "holdout")
    monkeypatch.undo()
    assert w.state(pid) == "offline_running"
    holdout = w.stage_run(pid)["data"]["stages"]["holdout"]
    assert holdout["state"] == "running"
    assert experiment_state(w, holdout["experiment_ref"]) == "frozen"
    steps = w.runner().advance(pid)
    assert states(steps)["holdout"] == "passed"
    assert w.state(pid) == "offline_evaluated"  # evaluated through the evolution machine
    assert w.head(pid)["data"]["experiment_ref"] == holdout["experiment_ref"]


def test_a_stage_with_a_dispatched_trial_is_not_resumed(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid, runner = planned(w)
    stop_in_first_trial(w, monkeypatch)
    with pytest.raises(Crash):
        runner.advance(pid)
    monkeypatch.undo()
    screening = w.stage_run(pid)["data"]["stages"]["screening"]
    assert experiment_state(w, screening["experiment_ref"]) == "running"
    calls = len(w.executor.calls)
    assert states(w.runner().advance(pid))["screening"] == "running"  # reconcile, not resume
    assert len(w.executor.calls) == calls
    w.store.epoch += 1
    assert w.ops.reconcile(pid, stage="screening")["state"] == "aborted"


def test_a_frozen_stage_of_a_candidate_that_moved_on_is_not_resumed(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid, runner = planned(w)
    stop_before_first_dispatch(w, monkeypatch)
    with pytest.raises(Crash):
        runner.advance(pid)
    monkeypatch.undo()
    w.ops.reject(pid, "the operator ended it")
    assert states(w.runner().advance(pid))["screening"] == "running"
    assert w.executor.calls == []


def test_a_resumed_stage_still_checks_the_loaded_corpus(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid, runner = planned(w)
    stop_before_first_dispatch(w, monkeypatch)
    with pytest.raises(Crash):
        runner.advance(pid)
    monkeypatch.undo()
    frozen = w.stage_run(pid)["data"]["stages"]["screening"]["experiment_ref"]
    case_id = w.sampling(frozen)["case_ids"][0]
    w.ops.corpus = replace(
        w.ops.corpus,
        tasks=tuple(
            replace(t, objective="Something else.") if t.task_id == case_id else t
            for t in w.ops.corpus.tasks
        ),
    )
    error = hold("CORPUS_CHANGED", w.runner().advance, pid)
    assert error.details["cases"] == [case_id]
    assert w.executor.calls == []
    assert experiment_state(w, frozen) == "frozen"


# ==================================================================================================
# IC-10 mechanics (clarification after S12): the approval issuer and the queued focused stage
# ==================================================================================================
def nightly_runner(w: World, derive: Any = None) -> StageRunner:
    """A runner whose approvals the nightly identity derives (``derive``; none is needed for the
    refusals below)."""
    from amplai_foundry.runtime.execution.meta_local import nightly_actor

    def never(plan: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("no approval may be derived here")

    return StageRunner(
        w.ops, w.refs, calibration_summary_ref=w.summary_ref,
        evaluator_version_ref=w.version_ref, metrics=TrialMetrics(w.dep.service),
        leak_gate=LeakGate(w.operator, w.store, w.refs["leak_index_ref"]), parallel=1,
        issuer=stages.standing_issuer(nightly_actor(w.scope), derive or never),
    )  # fmt: skip


def test_the_default_issuer_is_the_human_operators(w: World) -> None:
    runner = w.runner()
    assert runner.issuer.human and runner.actor == w.ops.operator


def test_a_nightly_runner_never_screens_a_draft(w: World) -> None:
    pid = w.propose("a2")
    runner = nightly_runner(w)
    runner.plan(pid, cell_id=CELL, root_budget=BUDGET)
    steps = runner.advance(pid)
    assert w.state(pid) == "draft" and w.executor.calls == []
    assert runner.last_stop == {"stage": "screening", "code": "OPERATOR_SCREEN"}
    assert w.step(steps, "screening").state == "pending"


def test_a_queued_focused_stage_is_built_once_without_approval_and_superseded_by_a_gate(
    w: World,
) -> None:
    pid = w.propose("a2")
    w.script(pid)
    runner = w.runner()
    runner.plan(pid, cell_id=CELL, root_budget=BUDGET)
    runner.advance(pid)
    approvals = len(w.objects("meta-approval"))
    item = runner.queue_stage(pid, "focused")
    assert item is not None and item["proposal_id"] == pid
    assert runner.queue_stage(pid, "focused") == item  # idempotent while queued
    assert len(w.objects("meta-approval")) == approvals  # no approval was issued
    head = stages.queue_head(w.store, w.scope, pid)
    assert head is not None and head["state"] == "queued"
    hold("META_STATE", runner.run_queued, pid, "focused")  # not approved yet
    # the operator's in-process gate runs a fresh experiment; the queued build is superseded
    assert runner.approve_stage(pid, "focused").state == "passed"
    head = stages.queue_head(w.store, w.scope, pid)
    assert head is not None and head["state"] == "superseded"
    hold("META_STATE", runner.approve_queued, pid, "focused")
    hold("META_STATE", runner.queue_stage, pid, "focused")  # the gate is not waiting any more


def test_an_approved_queued_stage_runs_once_as_the_operator_too(w: World) -> None:
    pid = w.propose("a2")
    w.script(pid)
    runner = w.runner()
    runner.plan(pid, cell_id=CELL, root_budget=BUDGET)
    runner.advance(pid)
    runner.queue_stage(pid, "focused")
    calls = len(w.executor.calls)
    step = runner.approve_queued(pid, "focused")
    assert step.state == "frozen" and len(w.executor.calls) == calls
    hold("META_STATE", runner.approve_stage, pid, "focused")  # frozen: not waiting
    assert runner.run_queued(pid, "focused").state == "passed"
    head = stages.queue_head(w.store, w.scope, pid)
    assert head is not None and head["state"] == "ran"
    hold("META_STATE", runner.run_queued, pid, "focused")  # ran once


def test_pending_trials_is_the_next_auto_stage_and_zero_at_a_gate(w: World) -> None:
    pid = w.propose("a2")
    w.script(pid)
    runner = w.runner()
    assert runner.pending_trials(pid) == 0  # no plan yet
    runner.plan(pid, cell_id=CELL, root_budget=BUDGET)
    assert runner.pending_trials(pid) == 12 * 2  # screening: 12 tasks x 2 arms x 1 repeat
    runner.advance(pid)
    assert runner.pending_trials(pid) == 0  # the focused gate
    runner.approve_stage(pid, "focused")
    assert runner.pending_trials(pid) == 2 * 12 * 2  # ablation: 2 variants x 12 x 2 arms


# -- a queued stage is never left ``running`` in its queue head (states ... ran / failed ...) --
def approved_queue(w: World) -> tuple[str, StageRunner]:
    pid = w.propose("a2")
    w.script(pid)
    runner = w.runner()
    runner.plan(pid, cell_id=CELL, root_budget=BUDGET)
    runner.advance(pid)
    runner.queue_stage(pid, "focused")
    runner.approve_queued(pid, "focused")
    return pid, runner


def queue_state(w: World, pid: str) -> tuple[str, dict[str, Any]]:
    head = stages.queue_head(w.store, w.scope, pid)
    assert head is not None
    return str(head["state"]), dict(head["data"])


def test_a_queued_stage_whose_run_faults_ends_failed_with_the_fault(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid, runner = approved_queue(w)

    def down(*_: Any, **__: Any) -> Any:
        raise RuntimeFault("EXECUTOR_DOWN", "the executor is gone")

    monkeypatch.setattr(w.local.evaluation, "run", down)
    fault("EXECUTOR_DOWN", runner.run_queued, pid, "focused")
    state, data = queue_state(w, pid)
    assert state == "failed" and data["failure"]["code"] == "EXECUTOR_DOWN"
    assert data["failure"]["stage_state"] == "aborted" and data["failed_at"]
    assert w.stage_run(pid)["data"]["stages"]["focused"]["state"] == "aborted"
    hold("META_STATE", runner.run_queued, pid, "focused")  # a failed queue item never re-runs


def test_a_queued_stage_whose_run_raises_anything_else_ends_failed_too(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid, runner = approved_queue(w)

    def broken(*_: Any, **__: Any) -> Any:
        raise ValueError("a bug")

    monkeypatch.setattr(runner, "_select", broken)
    with pytest.raises(ValueError):
        runner.run_queued(pid, "focused")
    state, data = queue_state(w, pid)
    assert state == "failed" and data["failure"]["code"] == "ValueError"
    assert data["failure"]["stage_state"] == "running"


def test_a_queued_stage_whose_run_report_aborts_ends_failed(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid, runner = approved_queue(w)
    real = runner._run_stage

    def aborting(proposal_id: str, plan: Any, entry: Any, ref: Any, cases: Any) -> None:
        real(proposal_id, plan, entry, ref, cases)
        runner._update(proposal_id, entry["stage"], state="aborted")  # an aborted run report

    monkeypatch.setattr(runner, "_run_stage", aborting)
    assert runner.run_queued(pid, "focused").state == "aborted"
    state, data = queue_state(w, pid)
    assert state == "failed" and data["failure"]["code"] == "STAGE_ABORTED"


def test_a_crash_inside_a_queued_stage_is_settled_failed_by_the_next_advance(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid, runner = approved_queue(w)
    stop_in_first_trial(w, monkeypatch)
    with pytest.raises(Crash):
        runner.run_queued(pid, "focused")
    monkeypatch.undo()
    assert queue_state(w, pid)[0] == "running"  # a process stop runs no handler
    calls = len(w.executor.calls)
    w.runner().advance(pid)  # the next process
    state, data = queue_state(w, pid)
    assert state == "failed"
    assert data["failure"] == {"code": "INTERRUPTED", "stage_state": "running"}
    assert len(w.executor.calls) == calls  # nothing replayed
    w.store.epoch += 1
    assert w.ops.reconcile(pid, stage="focused")["state"] == "aborted"
    assert queue_state(w, pid)[0] == "failed"


def test_a_reconcile_after_a_crash_inside_a_queued_stage_ends_it_failed(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid, runner = approved_queue(w)
    stop_in_first_trial(w, monkeypatch)
    with pytest.raises(Crash):
        runner.run_queued(pid, "focused")
    monkeypatch.undo()
    w.store.epoch += 1
    assert w.ops.reconcile(pid, stage="focused")["state"] == "aborted"
    state, data = queue_state(w, pid)
    assert state == "failed" and data["failure"]["code"] == "STAGE_ABORTED"


def test_a_queued_stage_stopped_before_its_first_dispatch_resumes_and_ends_ran(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid, runner = approved_queue(w)
    stop_before_first_dispatch(w, monkeypatch)
    with pytest.raises(Crash):
        runner.run_queued(pid, "focused")
    monkeypatch.undo()
    assert queue_state(w, pid)[0] == "running"
    steps = w.runner().advance(pid)
    assert states(steps)["focused"] == "passed"
    state, data = queue_state(w, pid)
    focused = w.stage_run(pid)["data"]["stages"]["focused"]
    assert state == "ran" and focused["report_ref"] is not None
    assert data["report_ref"] == focused["report_ref"]


def test_a_nightly_issuer_is_refused_the_holdout_before_anything_is_built(w: World) -> None:
    pid, _runner, _derived = through_ablation(w)
    night = nightly_runner(w)
    plan = night._plan(pid)[1]
    entry = next(s for s in plan["stages"] if s["stage"] == "holdout")
    kinds = ("analysis-plan", "sampling-plan", "eval-experiment", "meta-approval", "stage-run")
    before = {k: len(w.objects(k)) for k in kinds}
    hold("APPROVAL_HUMAN", night._stage, pid, plan, entry)
    assert {k: len(w.objects(k)) for k in kinds} == before  # no record written
    assert w.stage_run(pid)["data"]["stages"]["holdout"]["state"] == "waiting_approval"
    assert w.state(pid) == "screened"


def test_a_nightly_advance_never_resumes_a_frozen_holdout(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # IC-10: the resume path (``_resume_frozen``) runs no holdout under the nightly identity
    pid, runner, _derived = through_ablation(w)
    stop_before_first_dispatch(w, monkeypatch)
    with pytest.raises(Crash):
        runner.approve_stage(pid, "holdout")
    monkeypatch.undo()
    holdout = w.stage_run(pid)["data"]["stages"]["holdout"]
    assert holdout["state"] == "running" and w.state(pid) == "offline_running"
    assert experiment_state(w, holdout["experiment_ref"]) == "frozen"
    calls = len(w.executor.calls)

    def no_evaluate(*_: Any, **__: Any) -> Any:
        raise AssertionError("the nightly identity never evaluates the holdout")

    def no_run(*_: Any, **__: Any) -> Any:
        raise AssertionError("the nightly identity never runs the holdout")

    monkeypatch.setattr(w.local.meta, "evaluate", no_evaluate)
    monkeypatch.setattr(w.local.evaluation, "run", no_run)
    steps = nightly_runner(w).advance(pid)
    assert states(steps)["holdout"] == "running"
    assert len(w.executor.calls) == calls
    assert experiment_state(w, holdout["experiment_ref"]) == "frozen"
    assert w.state(pid) == "offline_running"
    monkeypatch.undo()
    # left for the operator: the human runner's advance resumes and evaluates it
    steps = w.runner().advance(pid)
    assert states(steps)["holdout"] == "passed"
    assert w.state(pid) == "offline_evaluated"
