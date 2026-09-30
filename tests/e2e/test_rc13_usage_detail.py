"""D-094 — a run's provider breakdown is stored beside its 3.0.0 usage, and the trial metrics read
it back. Stand-ins as in the rc06 rig (a scripted Codex agent that reports 10 input and 5 output
tokens); the real check is recorded under specs/030-meta-harness-live/runs/.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from amplai_foundry.meta_harness import local_corpus
from amplai_foundry.meta_harness.trial_metrics import TrialMetrics
from amplai_foundry.runtime.execution.worker import USAGE_DETAIL_KIND
from rc06_rig import approved, rig_with_codex

sys.path.insert(0, str(Path(__file__).parent))  # the rc10 executor helpers live beside this file
from test_rc10_local_executor import baseline, case, executor_for


def run_usage(rig: Any, goal: str) -> dict[str, Any]:
    plan = rig.service.plan_record(goal)
    run_id = plan["attempts"][-1]["run_id"]
    usage: dict[str, Any] = rig.d.store.head(rig.d.scope, "run", run_id)["data"]["record"]["usage"]
    return usage


def test_the_run_usage_points_at_its_stored_breakdown(deployment: Any, tmp_path: Path) -> None:
    rig, loop, _container = rig_with_codex(deployment, tmp_path, "right")
    goal = approved(rig)
    assert loop.run_goal(goal)["status"] == "published"
    usage = run_usage(rig, goal)
    # the 3.0.0 usage is unchanged in shape: totals, and a reference in source_ref
    assert (usage["input_tokens"], usage["output_tokens"], usage["status"]) == (10, 5, "measured")
    ref = usage["source_ref"]
    assert ref["id"].startswith(USAGE_DETAIL_KIND + "-")
    detail = rig.d.store.get(rig.d.scope, USAGE_DETAIL_KIND, ref)
    assert detail["provider"] == "codex" and detail["input_includes_cache"] is True
    assert detail["fields"] == {"input_tokens": 10, "output_tokens": 5}
    assert detail["run_id"] == rig.service.plan_record(goal)["attempts"][-1]["run_id"]
    assert detail["cost_source_ref"] is None


def test_trial_metrics_read_back_a_real_trial(deployment: Any, tmp_path: Path) -> None:
    executor, rig, _container, _corpus = executor_for(deployment, tmp_path, "right")
    executor(baseline(rig), case("t-pass"), 0, "sandbox_rerun")
    executor(baseline(rig), case("t-doc"), 0, "sandbox_rerun")
    metrics = TrialMetrics(rig.service)
    rows = []
    for record in executor.trials:
        receipt_trial = {
            "task_id": record["task_id"],
            "arm": "baseline",
            "success": record["success"],
            "elapsed_ms": 1000,
            "input_tokens": record["input_tokens"],
            "output_tokens": record["output_tokens"],
            "artifact_refs": [
                rig.d.artifacts.admit(
                    rig.d.scope, _canonical(record), "application/json", trust="verifier"
                )
            ],
        }
        rows.append(metrics.trial(receipt_trial))
    passed, docless = rows
    assert passed["diff"]["paths"] == ["app.py"] and passed["diff"]["tests_changed"] == []
    assert docless["verified_hidden_fail"] is True  # the app verifier passed, the hidden test not
    # the stand-in reports no cache count: priced at the shipped gpt-5.6-sol table as an upper
    # bound, 10 input and 5 output tokens = (10*4 + 5*20) USD per 1M = 140 microunits
    cost = passed["api_cost"]
    assert (cost["status"], cost["cost_microunits"], cost["model"]) == (
        "estimated",
        140,
        "gpt-5.6-sol",
    )
    assert cost["upper_bound"] is True and cost["notes"] == ["no_cache_breakdown"]
    summary = TrialMetrics.summarize(rows)["baseline"]
    assert (summary["solved"], summary["verified_hidden_fail"]) == (1, 1)
    assert summary["tokens_per_solved"] == 30  # both trials' tokens over the one solved task


def test_a_grader_that_changes_its_mind_is_flaky(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    (root / "base").mkdir(parents=True)
    (root / "base" / "app.py").write_text("def value():\n    return 1\n")
    folder = root / "tasks" / "coin"
    (folder / "hidden").mkdir(parents=True)
    (folder / "reference").mkdir(parents=True)
    # passes on the reference only every other run: a counter file outside the scratch copies
    counter = tmp_path / "count"
    (folder / "hidden" / "test_coin.py").write_text(
        "from pathlib import Path\nfrom app import value\n\n\ndef test_coin() -> None:\n"
        f"    p = Path({str(counter)!r})\n"
        "    n = int(p.read_text()) if p.exists() else 0\n"
        "    p.write_text(str(n + 1))\n"
        "    assert value() == 2 and n % 2 == 0\n"
    )
    (folder / "reference" / "app.py").write_text("def value():\n    return 2\n")
    (folder / "task.json").write_text(
        '{"task_id": "coin", "difficulty": "small", "objective": "Make value() return 2.",'
        ' "acceptance": ["value() returns 2"]}'
    )
    corpus = local_corpus.load(root, ["coin"])
    once = local_corpus.validate(corpus, tmp_path / "s1", repeats=1)["coin"]
    counter.unlink()
    thrice = local_corpus.validate(corpus, tmp_path / "s3", repeats=3)["coin"]
    assert once["flaky"] is False
    assert thrice["flaky"] is True and thrice["fair"] is False


def _canonical(value: dict[str, Any]) -> bytes:
    from amplai_foundry.runtime.contracts.identity import canonical

    return canonical(value)
