"""Work 033 S2: Q-02 / G5, every stored report re-analyzes to its recorded verdict (§4.2, §7.8).

`scripts/evaluator_requalify.py` (S1) recomputes each stored `eval-report` from its immutable
inputs. G5 covers the test stores of `tests/v3/test_dev03_evaluation.py` and
`tests/v3/test_rc12_meta_cli.py`; S2 adds the reports its versioned plans write (stage subset,
reference arm, accumulated e-process, stop reasons). A passing requalification is the input of the
`evaluator-version` record `eval-2` (§2.15, `evaluation/versions.py`).
"""

import importlib.util
import json
from dataclasses import replace
from pathlib import Path

import pytest
from meta_world import MetaReference
from test_033_s2_service import (
    ARMS3,
    FakeValidator,
    Scripted,
    confirm,
    e_process_key,
    e_run,
    make_world,
    policy,
    reference_arm,
    subset,
)

from amplai_foundry.evaluation import versions
from amplai_foundry.runtime.contracts.identity import canonical, digest
from amplai_foundry.runtime.errors import Hold

REPO = Path(__file__).resolve().parents[2]


def _script():
    spec = importlib.util.spec_from_file_location(
        "evaluator_requalify", REPO / "scripts" / "evaluator_requalify.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


requalify = _script()


@pytest.fixture
def w(tmp_path):
    with make_world(tmp_path) as world:
        yield world


def verdicts(store, scope):
    return {
        ref["id"]: store.get(scope, "eval-report", ref)["verdict"]
        for ref, _ in store.list_objects(scope, "eval-report")
    }


def assert_requalified(store, artifacts, scope, label):
    """requalify_scope gives one equal row per stored report; returns the record value."""
    value = requalify.requalify_scope(store, artifacts, scope, store_label=label)
    requalify.validate_requalification(value)
    recorded = verdicts(store, scope)
    assert {row["report_ref"]["id"] for row in value["reports"]} == set(recorded)
    for row in value["reports"]:
        assert row["recorded_verdict"] == recorded[row["report_ref"]["id"]]
        assert row["recomputed_verdict"] == row["recorded_verdict"], row
        assert row["equal"] is True
    assert value["all_equal"] is True
    assert value["evaluator_version"] == versions.VERSION == "eval-2"
    return value


# --- G5 over test_dev03_evaluation stores (legacy plans) ------------------------------------
# `MetaReference.prepare` writes fixed release ids, so dev03 runs one prepared experiment per
# store; each scenario below is its own store, as in `tests/v3/test_dev03_evaluation.py`.


def _lost(m, prepared):
    def callback(*args):
        raise ConnectionError("response lost")

    return callback


def _untrusted(m, prepared):
    def callback(*args):
        obs = m.execute_case(*args)
        receipt = json.loads(m.d.artifacts.read(m.d.scope, obs.artifact_refs[0]))
        ar = m.d.artifacts.admit(m.d.scope, canonical(receipt), "application/json", trust="worker")
        return replace(obs, artifact_refs=(ar,))

    return callback


def _revoke(m, prepared):
    plan = m.d.store.get(m.d.scope, "eval-experiment", prepared["experiment_ref"])

    def callback(*args):
        obs = m.execute_case(*args)
        m.approvals[digest(plan["approval_ref"])]["revoked"] = True
        return obs

    return callback


def _drift(m, prepared):
    def callback(*args):
        obs = m.execute_case(*args)
        m.eval.environment_probe = lambda env: {**env, "drifted": True}
        return obs

    return callback


# name -> (bad candidate, callback factory or None for the promoted reference flow)
DEV03 = {
    "promoted_pass": (False, None),
    "failing_candidate": (True, lambda m, p: m.execute_case),
    "lost_response": (False, _lost),
    "untrusted_receipt": (False, _untrusted),
    "approval_revoked": (False, _revoke),
    "environment_drift": (False, _drift),
}


def dev03_store(root, bad, factory):
    """Writes one dev03 report into a fresh store at `root`; returns the store's runtime root."""
    m = MetaReference(root)
    try:
        prepared = m.prepare(bad_candidate=bad)
        if factory is None:
            assert m.execute(prepared)["verdict"] == "pass"
        else:
            m.meta.start_offline(m.reviewer, prepared["proposal_id"])
            m.eval.run(m.reviewer, prepared["experiment_ref"], factory(m, prepared))
        assert len(verdicts(m.d.store, m.d.scope)) == 1
        assert_requalified(m.d.store, m.d.artifacts, m.d.scope, "dev03")
        return m.d.store.root, next(iter(verdicts(m.d.store, m.d.scope).values()))
    finally:
        m.close()


def test_q02_every_dev03_report_recomputes_to_its_recorded_verdict(tmp_path):
    seen = {}
    for name, (bad, factory) in DEV03.items():
        root, verdict = dev03_store(tmp_path / name, bad, factory)
        seen[name] = verdict
        # The command form over the closed store writes one passing record and exits 0.
        written = requalify.requalify_root(root)
        assert len(written) == 1
        ref, value = written[0]
        assert ref["id"].startswith("requal-") and value["all_equal"] is True
        assert [row["recorded_verdict"] for row in value["reports"]] == [verdict]
        assert requalify.main(["--runtime-root", str(root)]) == 0
    assert seen["promoted_pass"] == "pass" and seen["failing_candidate"] == "fail"
    # every verdict of the report enum occurs, so each post-rule of `run` is exercised
    assert set(seen.values()) == {"pass", "fail", "aborted", "inconclusive"}, seen


def test_q02_a_tampered_recorded_verdict_is_not_equal(tmp_path):
    m = MetaReference(tmp_path / "meta")
    try:
        run = m.prepare(bad_candidate=True)
        m.meta.start_offline(m.reviewer, run["proposal_id"])
        report_ref = m.eval.run(m.reviewer, run["experiment_ref"], m.execute_case)
        store, scope = m.d.store, m.d.scope
        report = store.get(scope, "eval-report", report_ref)
        assert report["verdict"] == "fail"
        # Only the verdict is compared here; the stored report is not modified.
        assert requalify.recompute_verdict(store, m.d.artifacts, scope, report) == "fail"
        forged = {**report, "verdict": "pass"}
        assert requalify.recompute_verdict(store, m.d.artifacts, scope, forged) == "fail"
    finally:
        m.close()


# --- G5 over a test_rc12_meta_cli store --------------------------------------------------------


def test_q02_a_rc12_meta_cli_store_holds_no_eval_report(tmp_path):
    """The rc12 commands that need no driver never write an eval-report (their trial gates are
    refused with META_STATE), so G5 is vacuous there: the command reports nothing to requalify."""
    from test_rc12_meta_cli import GOOD, meta, mini_corpus, propose
    from typer.testing import CliRunner

    from amplai_foundry.runtime import cli
    from amplai_foundry.runtime.execution.codex import AUTH
    from rc06_rig import codex_inputs, make_repo

    repo = make_repo(tmp_path)
    inputs = codex_inputs(tmp_path)
    codex_home = tmp_path / "codex-home"
    (codex_home / ".codex").mkdir(parents=True)
    (codex_home / AUTH).write_text('{"tokens": "x"}')
    where = tmp_path / "amplai"
    result = CliRunner().invoke(
        cli.app,
        [
            "ops", "local-init", "--repo", str(repo), "--app", "app",
            "--codex-home", str(codex_home),
            "--container-profile", str(inputs.container_profile),
            "--qualification-report", str(inputs.qualification_report),
            "--egress-profile", str(inputs.egress_profile),
            "--egress-qualification", str(inputs.egress_qualification),
            "--verifier", "check=python3 -c 'import app' | app imports",
            "--home", str(where), "--operator", "pinesky",
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    prompt = tmp_path / "prompt.txt"
    prompt.write_text(GOOD)
    home = {
        "config": str(where / "local.json"),
        "corpus": str(mini_corpus(tmp_path / "corpus")),
        "prompt": str(prompt),
    }
    pid = propose(home)
    assert meta(home, "screen", pid)[1]["state"] == "screened"
    code, out = meta(home, "run-experiment", pid, "--per-trial-tokens", "10", "--basis", "x",
                     "--evidence", "y")  # fmt: skip
    assert code == 3 and out["code"] == "META_STATE"
    root = Path(json.loads((where / "local.json").read_text())["runtime_root"])
    assert (root / "runtime.sqlite3").is_file()
    assert requalify.requalify_root(root) == []
    assert requalify.main(["--runtime-root", str(root)]) == 0


# --- S2 versioned reports, then eval-2 -------------------------------------------------------


def s2_store(w):
    """One report of each S2 shape: a stage subset, a reference arm (dominated and matched), an
    e-process accumulated over three experiments (the last crossing on basis e_process), a
    candidate safety stop, a baseline-only safety stop and a lost executor response."""
    s = w.setup(val=24)
    ids = [f"val-{i:02d}" for i in range(16)]
    ref = w.freeze(w.experiment(s, policy(s.ev), ids=ids, sampling=subset("all_v1", 16)))
    w.run(ref, Scripted(w, w.roles(s)))

    w.m.eval.reference_validator = FakeValidator()
    for outcome in (lambda r, c, k: r == "reference", lambda r, c, k: r != "baseline"):
        t = w.setup(val=16, reference=True)
        pol = confirm(t.ev, 16, reference_arm=reference_arm(t))
        ref = w.freeze(w.experiment(t, pol, sampling={"arms": ARMS3}))
        w.run(ref, Scripted(w, w.roles(t), outcome))

    e = w.setup(val=16)
    reports, state, last = [], None, None
    for _ in range(3):
        report_ref, last = e_run(w, e, list(reports), state)
        reports.append(report_ref)
        state = last["e_process"]["state"]
    assert last["basis"] == "e_process" and last["e_process"]["crossed"] is True
    assert last["e_process"]["key"] == e_process_key(e.base, e.cand, "focused")

    u = w.setup(val=16)
    for outcome in (
        lambda r, c, k: (True, 1) if (r, c) == ("candidate", "val-00") else True,
        lambda r, c, k: (True, 1) if r == "baseline" else True,
    ):
        ref = w.freeze(w.experiment(u, policy(u.ev)))
        w.run(ref, Scripted(w, w.roles(u), outcome))
    ref = w.freeze(w.experiment(u, policy(u.ev)))
    w.run(ref, Scripted(w, w.roles(u), raises=lambda r, c, k: c == "val-01"))


def test_q02_every_s2_versioned_report_recomputes_to_its_recorded_verdict(w):
    s2_store(w)
    store, artifacts = w.m.d.store, w.m.d.artifacts
    recorded = verdicts(store, w.scope)
    assert len(recorded) == 9
    assert set(recorded.values()) == {"pass", "fail", "aborted", "inconclusive"}
    assert_requalified(store, artifacts, w.scope, "s2")


def test_q02_a_passing_requalification_writes_eval2_and_a_failing_one_cannot(w):
    s2_store(w)
    store, artifacts, reviewer = w.m.d.store, w.m.d.artifacts, w.m.reviewer
    value = assert_requalified(store, artifacts, w.scope, "s2")
    requal_ref = requalify.write_record(store, w.scope, value)
    corpus_ref, _ = w.corpus(val=16)
    ref = versions.write_version(
        store, reviewer, corpus_ref=corpus_ref, requalification_ref=requal_ref
    )
    assert ref["id"] == "evaluator-2"
    stored = versions.read_version(store, w.scope, ref)
    assert stored["version"] == "eval-2" and stored["requalification_ref"] == requal_ref
    assert {k: stored[k] for k in versions.DIGEST_FIELDS} == versions.code_digests()
    assert versions.current_version_ref(store, w.scope) == ref
    # A requalification with one unequal row never qualifies a version.
    row = {**value["reports"][0], "recomputed_verdict": None, "equal": False}
    failing = {**value, "reports": [row, *value["reports"][1:]], "all_equal": False}
    failing_ref = requalify.write_record(store, w.scope, failing)
    with pytest.raises(Hold) as caught:
        versions.write_version(
            store, reviewer, corpus_ref=corpus_ref, requalification_ref=failing_ref
        )
    assert caught.value.code == "EVALUATOR_UNQUALIFIED"
