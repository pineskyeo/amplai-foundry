"""Re-analyze every stored eval-report of a store with the current evaluator (Work 033 S1, G5).

    .venv/bin/python scripts/evaluator_requalify.py --runtime-root DIR

DIR is the runtime root of a local store (the directory that holds `runtime.sqlite3`, the
`runtime_root` of `local.json`). For every project scope that holds an `eval-report`, each report
is recomputed from its immutable inputs, exactly as `MetaHarness._report` does
(`meta_harness/service.py:329-368`): the frozen analysis plan, the sampling plan's case ids, the
report's trials, drift flag and contamination findings. The post-analysis rules of
`EvaluationService.run` (`evaluation/service.py:423-434`) are then applied in the same order.
One `evaluator-requalification` record (interfaces.md §2.15) is written per scope, and the exit
code is 0 only when every recomputed verdict equals the recorded verdict.

The run's stop reason is not recomputable from trials (guards such as a kill switch or a
revoked approval are run-time facts); it is read from the report's trusted analysis artifact,
where `run` appended it after the analysis reasons (`service.py:426-431`).

Opening the store for writing takes its owner lock: stop the deployment first (a running one
gives `ACTIVE_OWNER`).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from amplai_foundry.evaluation.analysis import analyze_pairs
from amplai_foundry.evaluation.receipts import read_receipt
from amplai_foundry.runtime.contracts.identity import new_id
from amplai_foundry.runtime.contracts.semantics import resolve_ref
from amplai_foundry.runtime.errors import RuntimeFault
from amplai_foundry.runtime.evidence.cas import ArtifactStore
from amplai_foundry.runtime.storage.store import Scope, Store

# §2.15 / §7.8: the evaluator this code is qualified as. S2 writes the `evaluator-version`
# record with the code digests after Q-01..Q-15 pass.
EVALUATOR_VERSION = "eval-2"
KIND = "evaluator-requalification"
SCHEMA = "amplai.evaluator-requalification.v1"
STATIC_REASON = "static_or_replay_does_not_establish_live_policy_improvement"
EXPLORATORY_REASON = "exploratory_not_confirmatory"
VERDICTS = {"pass", "fail", "inconclusive", "aborted"}


def scopes_with_reports(store: Store) -> list[Scope]:
    """Every project scope holding an eval-report. The Store API reads one scope at a time, so
    the scope list is a read-only query of its object table."""
    with store._lock:
        rows = store.conn.execute(
            "SELECT DISTINCT tenant, project FROM objects WHERE kind='eval-report' "
            "ORDER BY tenant, project"
        ).fetchall()
    return [Scope(row[0], row[1]) for row in rows]


def _stop_reason(recorded: list[Any], recomputed: list[str], mode: str) -> tuple[bool, str | None]:
    """Split the recorded reasons into the analysis reasons and what `run` appended after them:
    the static/replay reason, at most one stop reason, then the exploratory reason
    (`service.py:423-434`). Returns (consistent, stop reason)."""
    if recorded[: len(recomputed)] != recomputed:
        return False, None
    rest = list(recorded[len(recomputed) :])
    if mode in {"static", "replay"}:
        if rest[:1] != [STATIC_REASON]:
            return False, None
        rest = rest[1:]
    if rest[-1:] == [EXPLORATORY_REASON]:
        rest = rest[:-1]
    if len(rest) > 1 or any(not isinstance(r, str) or not r for r in rest):
        return False, None
    return True, rest[0] if rest else None


def _post_rules(result: dict[str, Any], mode: str, stop: str | None, purpose: str) -> str:
    """`EvaluationService.run` after `analyze_pairs` (`evaluation/service.py:423-434`)."""
    verdict = result["verdict"]
    if mode in {"static", "replay"}:
        verdict = "inconclusive"
    if stop and verdict != "fail":
        verdict = "aborted" if stop != "ENVIRONMENT_DRIFT" else "inconclusive"
    if purpose == "exploratory" and verdict == "pass":
        verdict = "inconclusive"
    return str(verdict)


def recompute_verdict(
    store: Store, artifacts: ArtifactStore, scope: Scope, report: dict[str, Any]
) -> str:
    """The verdict the current evaluator gives this report's immutable inputs. Raises
    `RuntimeFault` when an input is missing or fails its integrity check (store and artifact
    codes) and `ValueError` when the recorded reasons are not the recomputed ones plus what
    `run` appends."""
    exp = store.get(scope, "eval-experiment", report["experiment_ref"])
    recorded = read_receipt(artifacts.read(scope, report["analysis_artifact"], trusted=True))
    trials = [store.get(scope, "eval-trial", ref) for ref in report["run_refs"]]
    _, analysis_plan = resolve_ref(store, scope, exp["analysis_plan_ref"])
    _, sampling_plan = resolve_ref(store, scope, exp["sampling_plan_ref"])
    for trial in trials:
        if trial["experiment_ref"] != report["experiment_ref"] or trial["mode"] != exp["mode"]:
            raise RuntimeFault("TRIAL_PROVENANCE", "Foreign trial in a stored report")
    policy = analysis_plan["policy"]
    result = analyze_pairs(
        trials,
        policy,
        expected_tasks=sampling_plan["case_ids"],
        environment_drifted=report["environment_drifted"],
        contamination=bool(report["contamination_findings"]),
    )
    reasons = recorded.get("reasons")
    consistent, stop = (
        _stop_reason(reasons, list(result["reasons"]), exp["mode"])
        if isinstance(reasons, list)
        else (False, None)
    )
    if not consistent:
        raise ValueError("recorded analysis reasons differ from the recomputed ones")
    return _post_rules(result, exp["mode"], stop, policy.get("purpose", "exploratory"))


def requalify_scope(
    store: Store,
    artifacts: ArtifactStore,
    scope: Scope,
    *,
    store_label: str,
    evaluator_version: str = EVALUATOR_VERSION,
) -> dict[str, Any]:
    """The requalification record value for one scope (not yet written)."""
    rows = []
    for ref, _ in store.list_objects(scope, "eval-report"):
        report = store.get(scope, "eval-report", ref)  # digest-checked read
        recomputed: str | None
        try:
            recomputed = recompute_verdict(store, artifacts, scope, report)
        except RuntimeFault as fault:
            print(f"{ref['id']}: {fault.code} {fault.message}", file=sys.stderr)
            recomputed = None
        except ValueError as exc:
            print(f"{ref['id']}: {exc}", file=sys.stderr)
            recomputed = None
        rows.append(
            {
                "report_ref": ref,
                "recorded_verdict": report["verdict"],
                "recomputed_verdict": recomputed,
                "equal": recomputed is not None and recomputed == report["verdict"],
            }
        )
    return {
        "schema": SCHEMA,
        "scope": scope.wire(),
        "evaluator_version": evaluator_version,
        "store": store_label,
        "reports": rows,
        "all_equal": all(row["equal"] for row in rows),
    }


def validate_requalification(value: dict[str, Any]) -> None:
    """§2.0: a new internal kind is validated before `put` (shape of §2.15)."""
    ok = (
        isinstance(value, dict)
        and set(value) == {"schema", "scope", "evaluator_version", "store", "reports", "all_equal"}
        and value["schema"] == SCHEMA
        and isinstance(value["evaluator_version"], str)
        and value["evaluator_version"]
        and isinstance(value["store"], str)
        and isinstance(value["reports"], list)
        and type(value["all_equal"]) is bool
    )
    if ok:
        for row in value["reports"]:
            ok = ok and (
                isinstance(row, dict)
                and set(row) == {"report_ref", "recorded_verdict", "recomputed_verdict", "equal"}
                and row["recorded_verdict"] in VERDICTS
                and (row["recomputed_verdict"] is None or row["recomputed_verdict"] in VERDICTS)
                and type(row["equal"]) is bool
                and row["equal"]
                == (
                    row["recomputed_verdict"] is not None
                    and row["recomputed_verdict"] == row["recorded_verdict"]
                )
            )
        ok = ok and value["all_equal"] == all(row["equal"] for row in value["reports"])
    if not ok:
        raise ValueError("invalid evaluator-requalification record")


def write_record(store: Store, scope: Scope, value: dict[str, Any]) -> dict[str, Any]:
    validate_requalification(value)
    with store.tx() as db:
        return store.put(db, scope, KIND, new_id("requal"), 1, value)


def requalify_root(
    root: Path, *, evaluator_version: str = EVALUATOR_VERSION
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Requalify every scope of the store at `root`; returns (record ref, record) per scope."""
    if not (root / "runtime.sqlite3").is_file():
        raise RuntimeFault("NOT_FOUND", "No runtime store at this runtime root")
    written = []
    with Store(root) as store:
        artifacts = ArtifactStore(store)
        for scope in scopes_with_reports(store):
            value = requalify_scope(
                store,
                artifacts,
                scope,
                store_label=str(store.root),
                evaluator_version=evaluator_version,
            )
            written.append((write_record(store, scope, value), value))
    return written


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runtime-root", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        written = requalify_root(args.runtime_root.expanduser().absolute())
    except RuntimeFault as fault:
        print(f"{fault.outcome.upper()} {fault.code}: {fault.message}", file=sys.stderr)
        return 2
    if not written:
        print("no eval-report in this store; nothing to requalify")
        return 0
    for ref, value in written:
        equal = sum(row["equal"] for row in value["reports"])
        scope = value["scope"]
        print(
            f"{scope['tenant_id']}/{scope['project_id']}: {equal}/{len(value['reports'])} "
            f"reports equal -> {ref['id']}"
        )
    return 0 if all(value["all_equal"] for _, value in written) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
