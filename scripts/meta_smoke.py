"""Run corpus tasks as real trials on a local product deployment (Work 030 S4/S7).

    .venv/bin/python scripts/meta_smoke.py --config ~/.amplai/local-smoke/local.json \\
        --tasks s01-semver-parse,s05-truncate --out specs/030-meta-harness-live/runs/smoke.json

Use a deployment of its own (``amplai ops local-init --home ~/.amplai/local-smoke ...``): trials are
goals in that deployment's store, so they never appear among the operator's real goals and never
share a store epoch with the running server. Publication is off. Each task runs once on the
installed baseline composition and once on a control candidate whose prompt text is identical
under another bundle id: the pinning plumbing is exercised and no behaviour differs.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from amplai_foundry.meta_harness import local_corpus
from amplai_foundry.meta_harness.local_executor import LocalTrialExecutor
from amplai_foundry.runtime.execution import prompts
from amplai_foundry.runtime.execution.loop import ExecutionLoop
from amplai_foundry.runtime.local_deployment import LocalProductDeployment

CORPUS = Path(__file__).resolve().parents[1] / "specs" / "030-meta-harness-live" / "corpus"


def arms(dep: LocalProductDeployment, app: str, driver: str) -> dict[str, dict[str, Any]]:
    service = dep.service
    base_ref = service.apps[app].compositions[driver]
    base = dep.store.get(dep.scope, "harness-composition", base_ref)
    bundle = service._put(
        prompts.KIND,
        "implementer-smoke-control",
        prompts.bundle(
            "implementer-smoke-control",
            prompts.IMPLEMENTER_BASELINE,
            "smoke control: the baseline text under another bundle id",
        ),
    )
    value = {
        **base,
        "composition_id": base["composition_id"] + "__smoke",
        "prompt_bundle_ref": bundle,
    }
    control = service._put("harness-composition", value["composition_id"], value)
    return {"baseline": base_ref, "control": control}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--tasks", required=True, help="comma-separated corpus task ids")
    ap.add_argument("--driver", default="codex-cli")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)

    corpus = local_corpus.load(CORPUS)
    ids = args.tasks.split(",")
    for task_id in ids:
        corpus.task(task_id)  # unknown ids stop here, before anything runs
    dep = LocalProductDeployment(args.config, start_loop=False)
    try:
        loop = ExecutionLoop(dep.service, dep.coordinator, publisher=None)
        executor = LocalTrialExecutor(dep.service, loop, dep.goals, dep.operator(), corpus)
        pinned = arms(dep, corpus.app_id, args.driver)
        rows: list[dict[str, Any]] = []
        for repeat, task_id in enumerate(ids):
            for arm, ref in pinned.items():
                began = time.monotonic()
                obs = executor(ref, {"case_id": task_id}, repeat, "sandbox_rerun")
                trial = executor.trials[-1]
                row = {
                    "task_id": task_id,
                    "arm": arm,
                    "success": obs.success,
                    "goal_status": trial.get("goal_status"),
                    "goal_reason": trial.get("goal_reason"),
                    "hidden_passed": trial.get("hidden_passed"),
                    "visible_passed": trial.get("visible_passed"),
                    "input_tokens": obs.input_tokens,
                    "output_tokens": obs.output_tokens,
                    "usage_status": obs.usage_status,
                    "seconds": round(time.monotonic() - began, 1),
                }
                rows.append(row)
                print(json.dumps(row), flush=True)
        report = {
            "kind": "meta_smoke",
            "corpus_id": corpus.corpus_id,
            "base_commit": corpus.base_commit,
            "driver": args.driver,
            "trials": rows,
        }
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    finally:
        dep.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
