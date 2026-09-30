"""Work 030 S6 (D-089, D-092, D-093) — the whole evolution lifecycle on the local product.

The operator's gates run one by one on the real MetaHarness, the real EvaluationService and the
real trial executor; only the agent is a stand-in that reads its prompt. A candidate prompt that
keeps the agent working passes the pre-registered non-inferiority test, goes through a canary and a
promotion, changes what a new goal is planned with, and rolls back. A candidate prompt that
sabotages the agent (the negative control) fails the test and cannot reach a canary.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import rc06_rig
from amplai_foundry.meta_harness import local_corpus
from amplai_foundry.meta_harness.local_executor import LocalTrialExecutor
from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.runtime.execution import releases
from amplai_foundry.runtime.execution.loop import ExecutionLoop
from amplai_foundry.runtime.execution.meta_local import META_OPERATOR_PERMISSIONS, LocalMeta
from amplai_foundry.runtime.execution.meta_ops import LocalMetaOps
from rc06_rig import ScriptContainer, git, rig_with_codex

TASKS = 16  # the smallest corpus for which "no difference at all" passes margin 0.25 at 0.95
KEY = Ed25519PrivateKey.generate()
GOOD = [
    "You are the IMPLEMENTER for {app_id} at commit {base_commit}.",
    "Make the change below and run the acceptance commands yourself. Do not commit.",
]
SABOTAGE = ["You are the IMPLEMENTER for {app_id}. SABOTAGE: leave the code as it is."]


class PromptSensitive(ScriptContainer):
    """A stand-in agent that follows its prompt: a SABOTAGE prompt leaves the code wrong."""

    def command(self, argv: list[str], workspace: Path, run_name: str, **kw: Any) -> list[str]:
        self.mode = "always-wrong" if "SABOTAGE" in argv[-1] else "right"
        return super().command(argv, workspace, run_name, **kw)


def make_corpus(root: Path, base_commit: str) -> local_corpus.Corpus:
    (root / "base").mkdir(parents=True)
    (root / "base" / "app.py").write_text("def value():\n    return 1\n")
    ids = [f"t{i:02d}" for i in range(TASKS)]
    for task_id in ids:
        folder = root / "tasks" / task_id
        (folder / "hidden").mkdir(parents=True)
        (folder / "reference").mkdir(parents=True)
        (folder / "hidden" / "test_t.py").write_text(
            "from app import value\n\n\ndef test_two() -> None:\n    assert value() == 2\n"
        )
        (folder / "reference" / "app.py").write_text("def value():\n    return 2\n")
        (folder / "task.json").write_text(
            json.dumps(
                {
                    "task_id": task_id,
                    "difficulty": "small",
                    "objective": f"Make value() in app.py return 2 ({task_id}).",
                    "acceptance": [f"value() returns 2 ({task_id})"],
                }
            )
        )
    (root / "manifest.json").write_text(
        json.dumps(
            {"corpus_id": "rehearsal", "app_id": "app", "base_commit": base_commit, "task_ids": ids}
        )
    )
    return local_corpus.load(root)


def build(deployment: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(rc06_rig, "ScriptContainer", PromptSensitive)
    rig, loop, _container = rig_with_codex(deployment, tmp_path, "right")
    d = rig.d
    corpus = make_corpus(tmp_path / "corpus", git(rig.repo, "rev-parse", "HEAD").strip())
    installed = [ref for app in rig.service.apps.values() for ref in app.compositions.values()]
    releases.bootstrap(
        d.store, d.scope, d.contracts, installed, signer=KEY, key_id="local-authority"
    )
    local = LocalMeta(d.store, d.contracts, d.artifacts, d.scope, KEY.public_key())
    operator = replace(
        rig.operator, permissions=rig.operator.permissions | META_OPERATOR_PERMISSIONS
    )
    dep = SimpleNamespace(
        service=rig.service,
        store=d.store,
        scope=d.scope,
        artifacts=d.artifacts,
        contracts=d.contracts,
        meta_local=local,
        meta_operator=lambda: operator,
        _release_signer=KEY,
    )
    trial_loop = ExecutionLoop(rig.service, loop.coordinator, publisher=None)
    executor = LocalTrialExecutor(
        rig.service, trial_loop, d.goals, operator, corpus, behaviour_verifier="check"
    )
    ops = LocalMetaOps(dep, corpus, executor, driver="codex-cli")  # type: ignore[arg-type]
    ops.qualify_executor("stand-in agent rehearsal", ["tests/e2e/test_rc12_meta_rehearsal.py"], 60)
    return SimpleNamespace(ops=ops, rig=rig, corpus=corpus, dep=dep)


def propose(ops: LocalMetaOps, lines: list[str], suffix: str) -> str:
    return ops.propose(
        suffix=suffix,
        implementer_lines=lines,
        hypothesis="The candidate prompt keeps the pass rate.",
        expected_benefit="Rehearsal of the lifecycle",
        observation="the implementer prompt could be shorter",
        risks=["a changed prompt may drop a useful instruction"],
    )


def active_ref(w: Any) -> dict[str, Any]:
    head = w.dep.store.head(w.dep.scope, releases.POINTER_KIND, releases.POINTER_ID)
    ref: dict[str, Any] = head["data"]["release_ref"]
    return ref


def selected(w: Any) -> dict[str, Any]:
    ref: dict[str, Any] = w.rig.service.select_composition(w.rig.service.apps["app"])["ref"]
    return ref


def test_a_good_candidate_goes_from_proposal_to_promotion_and_back(
    deployment: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = build(deployment, tmp_path, monkeypatch)
    ops = w.ops
    baseline_composition = selected(w)
    baseline_release = active_ref(w)

    pid = propose(ops, GOOD, "good")
    assert ops.screen(pid)["state"] == "screened"
    ops.approve_experiment(pid, max_tokens=TASKS * 2 * 120, max_wall_seconds=1800)
    experiment = ops.run_experiment(pid)
    assert experiment["verdict"] == "pass", experiment

    ops.approve_canary(pid, [t.task_id for t in w.corpus.tasks[:3]], max_trial_tokens=60)
    canary = ops.run_canary(pid)
    assert canary["state"] == "promotion_pending", canary
    assert selected(w) == baseline_composition  # nothing changed before the operator promotes

    promoted = ops.promote(pid)
    assert active_ref(w) == promoted["candidate_release_ref"] != baseline_release
    proposal = w.dep.store.get(
        w.dep.scope, "harness-change-proposal", ops._head(pid)["data"]["proposal_ref"]
    )
    assert selected(w) == proposal["candidate_ref"]  # new plans use the promoted prompt

    ops.rollback(pid)
    assert active_ref(w) == baseline_release
    assert selected(w) == baseline_composition
    assert ops.status(pid)["state"] == "rolled_back"


def test_a_sabotaged_candidate_fails_and_never_reaches_a_canary(
    deployment: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = build(deployment, tmp_path, monkeypatch)
    ops = w.ops
    baseline_release = active_ref(w)

    pid = propose(ops, SABOTAGE, "sabotage")
    ops.screen(pid)
    ops.approve_experiment(pid, max_tokens=TASKS * 2 * 120, max_wall_seconds=1800)
    experiment = ops.run_experiment(pid)
    assert experiment["verdict"] == "fail", experiment
    with pytest.raises(Hold):  # only a passing comparison can be canaried
        ops.approve_canary(pid, [t.task_id for t in w.corpus.tasks[:3]], max_trial_tokens=60)
    assert ops.reject(pid, "the negative control failed as it should") == "rejected"
    assert ops.status(pid)["rejection"]["from_state"] == "offline_evaluated"
    assert active_ref(w) == baseline_release


def test_gates_need_their_order_and_the_proposer_takes_none(
    deployment: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = build(deployment, tmp_path, monkeypatch)
    ops = w.ops
    pid = propose(ops, GOOD, "order")
    # being proposed approves nothing: the experiment and the canary each need their gate
    for gate in (
        lambda: ops.approve_experiment(pid, max_tokens=1, max_wall_seconds=1),
        lambda: ops.run_experiment(pid),
        lambda: ops.approve_canary(pid, ["t00"], max_trial_tokens=1),
        lambda: ops.run_canary(pid),
        lambda: ops.promote(pid),
    ):
        with pytest.raises(Hold) as refused:
            gate()
        assert refused.value.code == "META_STATE"
    proposer = w.dep.meta_local.proposer
    with pytest.raises(RuntimeFault) as forbidden:
        w.dep.meta_local.meta.screen(proposer, pid)
    assert forbidden.value.code == "FORBIDDEN"
    assert ops.status(pid)["state"] == "draft"
