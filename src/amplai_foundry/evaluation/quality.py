"""Evaluation-quality track and the `evaluator-change` lifecycle (Work 033 S14; interfaces.md
§2.15, §3.13, §11.1, D-105).

`QualityService.measure` computes the §11.1 metrics of one evaluator version from stored records
and writes an `evaluation-quality` record (id `evalq-<version>-<date>`; a second measurement of the
same day is a new store revision of the same id, §2.0). Every metric is a dict with `status`
`ok` or `no_data` and its figures; a metric whose records are missing says so and is never a
zero or a pass.

Metrics and their sources (§11.1):

- discrimination: champion minus negative-control pass rate per cell, over the development
  trials of calibration plans (`calibration-trial`); the cell's champion is the plan's first
  (v1) composition, the negative control is a declared variant of that plan (`negative_controls`);
- saturation: `calibration-summary.saturated_everywhere` over the summary's tasks;
- grader flakiness: the output of `amplai meta corpus check --repeats 3` (`corpus_check`; the check
  stores no record), plus the summaries' `flaky_grading` classes;
- contamination: `holdout-use` heads of the version's corpus, corpus and report contamination
  findings, and the `leak_scan` of the stage plans' change artifacts;
- dev-vs-holdout gap: per proposal with both reports, the screening delta minus the holdout delta
  (`stage-run` heads, analysis artifacts);
- agreement with real outcomes: per promoted candidate its canary outcomes and the verified,
  merged and closed rates of real goals (plans without `trial`) before and after the promotion;
- domain coverage: total variation distance between the corpus domain mix and the real goals' task
  classes mapped by `TASK_CLASS_TO_DOMAIN`;
- cost per decision: trials and tokens per concluded (pass/fail) report vs inconclusive.

Evaluator changes (retire, add or re-weight tasks; repeats; stage templates; analysis code) are an
`evaluator-change` head: proposed by the human operator, qualified alone (the §7.8 requalification
re-run over every stored report plus the quality of the replaced version), approved by the human
operator, which writes the new `evaluator-version` (`evaluation/versions.py`). A proposer or a
service identity is refused at every step, a change never starts while a candidate experiment, a
calibration or a stage is running, and nothing in this module is reachable from the evaluation
service, the stage runner or a trial: the track runs beside candidate experiments, never inside
one (design 16 §3).

Qualification (IC-25, provisional): `qualify_change` runs the §7.8 Q-suite
(`QUALIFICATION_FILES`: the golden, analysis, sequential, service, calibration and requalification
test files, which cover Q-01 .. Q-15) in a subprocess of the running interpreter (`sys.executable`,
`--junitxml`), parses the JUnit file and stores `{files, passed, failed, errors, junit_digest,
code_digests}` (plus skipped, tests, returncode) in the change head as `qualification_suite`. A
change is `qualified` only when pytest exited 0, every file ran at least one passing test with zero
failures and errors, and Q-02 (the requalification over every stored report) is `all_equal`.
`approve_change` re-checks the code digests (`EVALUATOR_CHANGED`). Q-02 over a scope with no stored
report is vacuously equal and stays allowed; the head and `status` flag it `vacuous`. The suite
runner is injectable (`QualityService(suite_runner=...)`); the real suite is never run by a test.

Authority (IC-26, provisional): every lifecycle call and `requalify` needs a human actor without
`harness.propose` that holds `evaluator.approve` (`CHANGE_PERMISSION`); `corpus.manage` does not
suffice.

This module reads validation and holdout reports. It is operator-side only: the meta command group
runs it as the human operator, and no decider or proposer code imports it.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from statistics import NormalDist, mean, stdev
from typing import Any, TypeGuard

from amplai_foundry.runtime.contracts.authority import Actor
from amplai_foundry.runtime.contracts.identity import new_id, now
from amplai_foundry.runtime.contracts.semantics import resolve_ref
from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.runtime.evidence.cas import ArtifactStore
from amplai_foundry.runtime.storage.store import Scope, Store

from . import versions
from .receipts import read_receipt

QUALITY_KIND = "evaluation-quality"
QUALITY_SCHEMA = "amplai.evaluation-quality.v1"
CHANGE_KIND = "evaluator-change"
CHANGE_STATES = ("proposed", "qualified", "approved", "rejected")
OPEN_STATES = ("proposed", "qualified")
# the evaluator changes of §11.1
CHANGE_KINDS = (
    "retire_tasks",
    "add_tasks",
    "reweight_tasks",
    "repeats",
    "stage_templates",
    "analysis_code",
)
METRICS = (
    "discrimination",
    "saturation",
    "grader_flakiness",
    "contamination",
    "dev_vs_holdout_gap",
    "agreement_with_real_outcomes",
    "domain_coverage",
    "cost_per_decision",
)
# What the human operator must hold to propose, qualify, approve or reject an evaluator change and
# to requalify (IC-26, provisional): `evaluator.approve`, granted to the operator by
# `runtime/execution/meta_local.py` META_OPERATOR_PERMISSIONS, never to a proposer or a service.
# `corpus.manage` (corpus authority) does not suffice.
CHANGE_PERMISSION = "evaluator.approve"
# §7.8 Q-suite files (IC-25, provisional); Q-01 .. Q-15 are spread over them
QUALIFICATION_FILES = (
    "tests/v3/test_033_golden_analysis.py",
    "tests/v3/test_033_s1_analysis.py",
    "tests/v3/test_033_s1_sequential.py",
    "tests/v3/test_033_s2_service.py",
    "tests/v3/test_033_s2_calibration.py",
    "tests/v3/test_033_s2_requalify.py",
)
SUITE_TIMEOUT_SECONDS = 3600
# The planner's task classes (`runtime/execution/planner_codex.py` TASK_CLASSES) that name one
# corpus v2 domain unambiguously (§11.1: a declared table). Every other class is reported as
# unmapped and is left out of the distance; none is guessed. PROVISIONAL (IC-27): the table is
# reported as provisional in the metric output until the operator confirms or extends it.
TASK_CLASS_TO_DOMAIN_PROVISIONAL = "provisional (IC-27): confirm or extend before relying on it"
TASK_CLASS_TO_DOMAIN: dict[str, str] = {
    "bug_fix": "bug",
    "new_feature": "feature",
    "refactor": "refactor",
    "operations": "cli_ops",
}
# store kinds the quality track reads (their writers are `runtime/execution/product.py:49-50`)
PLAN_RECORD_KIND = "execution-plan"
TASK_CLASS_KIND = "task-class"
STAGE_PLAN_KIND = "stage-plan"
STAGE_RUN_KIND = "stage-run"
EPOCH = "1970-01-01T00:00:00Z"
CONFIDENCE = 0.95
FLAKY_REPEATS = 3  # §11.1: `corpus_check --repeats 3`
REAL_TERMINAL = ("verified", "published", "failed")
REAL_VERIFIED = ("verified", "published")
_REPO = Path(__file__).resolve().parents[3]
_REQUALIFIER_SCRIPT = _REPO / "scripts" / "evaluator_requalify.py"
_Z = NormalDist().inv_cdf(1 - (1 - CONFIDENCE) / 2)


# -- pure helpers --------------------------------------------------------------------------------
def total_variation(p: Mapping[str, float], q: Mapping[str, float]) -> float:
    """Total variation distance between two distributions over named categories."""
    return 0.5 * sum(abs(p.get(k, 0.0) - q.get(k, 0.0)) for k in set(p) | set(q))


def shares(counts: Mapping[str, int]) -> dict[str, float]:
    total = sum(counts.values())
    return {k: v / total for k, v in sorted(counts.items())} if total else {}


def paired_interval(deltas: list[float]) -> list[float] | None:
    """Normal 95 % interval of the mean of task-level paired differences (the task is the unit);
    None below two tasks."""
    if len(deltas) < 2:
        return None
    half = _Z * stdev(deltas) / math.sqrt(len(deltas))
    centre = mean(deltas)
    return [centre - half, centre + half]


def _instant(value: Any, name: str = "since") -> float:
    if not isinstance(value, str):
        raise RuntimeFault("QUALITY_SINCE", f"{name} is an ISO-8601 timestamp with a timezone")
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeFault(
            "QUALITY_SINCE", f"{name} is an ISO-8601 timestamp with a timezone"
        ) from exc
    if moment.tzinfo is None:
        raise RuntimeFault("QUALITY_SINCE", f"{name} is an ISO-8601 timestamp with a timezone")
    return moment.timestamp()


def _stamp(value: Any) -> float | None:
    """A stored timestamp, or None when it is absent or unreadable."""
    try:
        return _instant(value)
    except RuntimeFault:
        return None


def _is_ref(value: Any) -> TypeGuard[dict[str, Any]]:
    return (
        isinstance(value, dict)
        and set(value) == {"id", "revision", "digest"}
        and isinstance(value["id"], str)
        and type(value["revision"]) is int
        and isinstance(value["digest"], str)
    )


def _no_data(reason: str) -> dict[str, Any]:
    return {"status": "no_data", "reason": reason}


def _version_number(version: str) -> int:
    return int(version.split("-", 1)[1])


def validate_quality(value: Any) -> None:
    """§2.0: a new internal kind is validated before `put` (RuntimeFault QUALITY_RECORD)."""
    ok = (
        isinstance(value, dict)
        and set(value)
        == {
            "schema",
            "scope",
            "evaluator_version",
            "evaluator_version_ref",
            "since",
            "measured_at",
            "inputs",
            "metrics",
        }
        and value["schema"] == QUALITY_SCHEMA
        and isinstance(value["scope"], dict)
        and isinstance(value["evaluator_version"], str)
        and _is_ref(value["evaluator_version_ref"])
        and isinstance(value["since"], str)
        and isinstance(value["measured_at"], str)
        and isinstance(value["inputs"], dict)
        and isinstance(value["metrics"], dict)
        and tuple(value["metrics"]) == METRICS
        and all(
            isinstance(m, dict) and m.get("status") in ("ok", "no_data")
            for m in value["metrics"].values()
        )
    )
    if not ok:
        raise RuntimeFault("QUALITY_RECORD", "Invalid evaluation-quality record (§11.1)")


def load_requalifier() -> Any:
    """`scripts/evaluator_requalify.py` of this checkout as a module (it is a script of the
    repository, not part of the package)."""
    if not _REQUALIFIER_SCRIPT.is_file():
        raise RuntimeFault(
            "EVALUATOR_REQUALIFIER",
            "scripts/evaluator_requalify.py is not part of this installation",
        )
    spec = importlib.util.spec_from_file_location("amplai_evaluator_requalify", _REQUALIFIER_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeFault("EVALUATOR_REQUALIFIER", "scripts/evaluator_requalify.py cannot load")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# a suite runner runs `files` and writes a JUnit XML file at `junit`; it returns pytest's exit code
SuiteRunner = Callable[[tuple[str, ...], Path], int]


def run_qualification_suite(files: tuple[str, ...], junit: Path, *, root: Path = _REPO) -> int:
    """The default suite runner: pytest of the running interpreter over `files` (paths relative to
    the repository root), JUnit XML at `junit`. Returns pytest's exit code."""
    missing = [f for f in files if not (root / f).is_file()]
    if missing:
        raise RuntimeFault(
            "EVALUATOR_SUITE_MISSING",
            "The Q-suite files are not part of this installation",
            details=missing,
        )
    command = [
        sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-o", "junit_family=xunit2",
        f"--junitxml={junit}", *files,
    ]  # fmt: skip
    try:
        done = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=SUITE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeFault("EVALUATOR_SUITE_RUN", f"The Q-suite did not finish: {exc}") from exc
    return done.returncode


def parse_junit(junit: Path, files: tuple[str, ...]) -> dict[str, Any]:
    """Read a pytest JUnit file into per-file and total counts. A test case belongs to the file
    whose stem is a component of its `classname` or `name` (pytest's xunit2 names a case by its
    module); a failed or errored case that belongs to none is counted in the totals, never
    dropped. `RuntimeFault EVALUATOR_SUITE_JUNIT` when the file is missing or not JUnit XML."""
    try:
        raw = Path(junit).read_bytes()
        root = ET.fromstring(raw)  # the file is the one this process just had pytest write
    except (OSError, ET.ParseError) as exc:
        raise RuntimeFault("EVALUATOR_SUITE_JUNIT", f"The JUnit file is unreadable: {exc}") from exc
    rows: dict[str, dict[str, Any]] = {
        f: {"file": f, "tests": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0}
        for f in files
    }
    stems = {Path(f).stem: f for f in files}
    other = {"failed": 0, "errors": 0}
    cases = [root] if root.tag == "testcase" else list(root.iter("testcase"))
    for case in cases:
        parts = re.split(r"[./\\:]+", f"{case.get('classname', '')}.{case.get('name', '')}")
        name = next((stems[p] for p in parts if p in stems), None)
        if case.find("error") is not None:
            outcome = "errors"
        elif case.find("failure") is not None:
            outcome = "failed"
        elif case.find("skipped") is not None:
            outcome = "skipped"
        else:
            outcome = "passed"
        if name is None:
            if outcome in other:
                other[outcome] += 1
            continue
        rows[name]["tests"] += 1
        rows[name][outcome] += 1
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    attr = {
        k: sum(int(s.get(k, "0")) for s in suites if s.get(k, "0").isdigit())
        for k in ("failures", "errors")
    }
    listed = list(rows.values())
    failed = max(sum(r["failed"] for r in listed) + other["failed"], attr["failures"])
    errors = max(sum(r["errors"] for r in listed) + other["errors"], attr["errors"])
    return {
        "files": listed,
        "tests": sum(r["tests"] for r in listed),
        "passed": sum(r["passed"] for r in listed),
        "failed": failed,
        "errors": errors,
        "skipped": sum(r["skipped"] for r in listed),
        "junit_digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
    }


def suite_passed(suite: Any) -> bool:
    """True only for a stored suite result with exit code 0, no failure and no error, and at least
    one passing test in every file (a file that ran nothing did not run)."""
    return (
        isinstance(suite, dict)
        and suite.get("returncode") == 0
        and suite.get("failed") == 0
        and suite.get("errors") == 0
        and isinstance(suite.get("files"), list)
        and {r.get("file") for r in suite["files"]} == set(QUALIFICATION_FILES)
        and all(
            r.get("passed", 0) > 0 and not r.get("failed") and not r.get("errors")
            for r in suite["files"]
        )
    )


class QualityService:
    def __init__(
        self,
        store: Store,
        scope: Scope,
        *,
        requalifier: Any | None = None,
        suite_runner: SuiteRunner | None = None,
    ) -> None:
        """`requalifier` is a module with the functions `requalify_scope` and `write_record` of
        `scripts/evaluator_requalify.py` (default: that script, loaded on first use).
        `suite_runner` runs the Q-suite (default: a pytest subprocess)."""
        self.store, self.scope = store, scope
        self.artifacts = ArtifactStore(store)
        self._requalifier = requalifier
        self._suite_runner: SuiteRunner = suite_runner or run_qualification_suite

    # -- store reads -----------------------------------------------------------------------------
    def _heads(self, kind: str) -> list[tuple[str, str, dict[str, Any]]]:
        """(id, state, data) of every head of `kind` in this scope (read-only query; the Store
        API reads one head at a time)."""
        with self.store._lock:
            rows = self.store.conn.execute(
                "SELECT id,state,data FROM heads WHERE tenant=? AND project=? AND kind=? "
                "ORDER BY id",
                (*self.scope.keys(), kind),
            ).fetchall()
        return [(r["id"], r["state"], json.loads(r["data"])) for r in rows]

    def _head_or_none(self, kind: str, object_id: str) -> dict[str, Any] | None:
        try:
            return self.store.head(self.scope, kind, object_id)
        except RuntimeFault as exc:
            if exc.code != "NOT_FOUND":
                raise
            return None

    def _latest(self, kind: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """The newest revision of every object of `kind` (digest-checked)."""
        newest: dict[str, dict[str, Any]] = {}
        for ref, _ in self.store.list_objects(self.scope, kind):
            if ref["id"] not in newest or ref["revision"] > newest[ref["id"]]["revision"]:
                newest[ref["id"]] = ref
        return [(ref, self.store.get(self.scope, kind, ref)) for ref in newest.values()]

    def _events(self, event_type: str) -> list[tuple[str, str]]:
        with self.store._lock:
            rows = self.store.conn.execute(
                "SELECT aggregate_id,created_at FROM events WHERE tenant=? AND project=? "
                "AND event_type=? ORDER BY seq",
                (*self.scope.keys(), event_type),
            ).fetchall()
        return [(r["aggregate_id"], r["created_at"]) for r in rows]

    def _analysis(self, report: dict[str, Any]) -> dict[str, Any] | None:
        try:
            return read_receipt(
                self.artifacts.read(self.scope, report["analysis_artifact"], trusted=True)
            )
        except RuntimeFault:
            return None

    # -- measure ---------------------------------------------------------------------------------
    def measure(
        self,
        evaluator_version_ref: dict[str, Any],
        *,
        since: str,
        negative_controls: Mapping[str, dict[str, Any]] | None = None,
        corpus_check: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """The §11.1 metrics of `evaluator_version_ref` over records since `since`; the written
        `evaluation-quality` ref.

        `negative_controls` maps a cell id to the composition ref of its deliberately bad
        `role_prompt` variant (a variant of the calibration plan's composition list);
        `corpus_check` is the JSON output of `amplai meta corpus check --repeats 3`. Without
        either, that metric is `no_data`."""
        version = versions.read_version(self.store, self.scope, evaluator_version_ref)
        floor = _instant(since)
        summaries = [
            (ref, value)
            for ref, value in self._latest("calibration-summary")
            if value.get("evaluator_version_ref") == evaluator_version_ref
            and (_stamp(value.get("summarized_at")) or 0.0) >= floor
        ]
        plans = [
            (ref, value)
            for ref, value in self._latest(STAGE_PLAN_KIND)
            if value.get("evaluator_version_ref") == evaluator_version_ref
            and (_stamp(value.get("created_at")) or 0.0) >= floor
        ]
        stage_rows = self._stage_rows(plans)
        metrics = {
            "discrimination": self._discrimination(summaries, negative_controls or {}),
            "saturation": self._saturation(summaries),
            "grader_flakiness": self._flakiness(summaries, corpus_check),
            "contamination": self._contamination(version, plans, stage_rows),
            "dev_vs_holdout_gap": self._gap(stage_rows),
            "agreement_with_real_outcomes": self._agreement(floor),
            "domain_coverage": self._coverage(version, floor),
            "cost_per_decision": self._cost(stage_rows),
        }
        measured_at = now()
        value = {
            "schema": QUALITY_SCHEMA,
            "scope": self.scope.wire(),
            "evaluator_version": version["version"],
            "evaluator_version_ref": evaluator_version_ref,
            "since": since,
            "measured_at": measured_at,
            "inputs": {
                "calibration_summaries": [ref for ref, _ in summaries],
                "stage_plans": [ref for ref, _ in plans],
                "corpus_check": corpus_check is not None,
                "negative_controls": sorted(negative_controls or {}),
            },
            "metrics": metrics,
        }
        validate_quality(value)
        object_id = f"evalq-{version['version']}-{measured_at[:10]}"
        with self.store.tx() as db:
            newest = db.execute(
                "SELECT MAX(revision) FROM objects WHERE tenant=? AND project=? AND kind=? "
                "AND id=?",
                (*self.scope.keys(), QUALITY_KIND, object_id),
            ).fetchone()[0]
            ref: dict[str, Any] = self.store.put(
                db, self.scope, QUALITY_KIND, object_id, (newest or 0) + 1, value
            )
        return ref

    # -- discrimination --------------------------------------------------------------------------
    def _discrimination(
        self,
        summaries: list[tuple[dict[str, Any], dict[str, Any]]],
        negative_controls: Mapping[str, dict[str, Any]],
    ) -> dict[str, Any]:
        if not negative_controls:
            return _no_data("no negative control declared")
        if any(not _is_ref(ref) for ref in negative_controls.values()):
            raise RuntimeFault("QUALITY_NEGATIVE_CONTROL", "A negative control is a record ref")
        trials = [t for _, t in self._latest("calibration-trial")]
        rows = []
        for summary_ref, summary in summaries:
            plan = self.store.get(self.scope, "calibration-plan", summary["plan_ref"])
            for cell, control in sorted(negative_controls.items()):
                compositions = plan["composition_refs"].get(cell) or []
                if control not in compositions[1:]:
                    continue  # not a variant of this plan's cell
                champion = compositions[0]
                good = _task_rates(trials, summary["plan_ref"], cell, champion)
                bad = _task_rates(trials, summary["plan_ref"], cell, control)
                tasks = sorted(set(good) & set(bad))
                if not tasks:
                    continue
                deltas = [good[t] - bad[t] for t in tasks]
                interval = paired_interval(deltas)
                rows.append(
                    {
                        "summary_ref": summary_ref,
                        "cell_id": cell,
                        "champion_ref": champion,
                        "negative_control_ref": control,
                        "tasks": len(tasks),
                        "champion_pass_rate": mean(good[t] for t in tasks),
                        "negative_control_pass_rate": mean(bad[t] for t in tasks),
                        "difference": mean(deltas),
                        "interval": interval,
                        "confidence": CONFIDENCE,
                        "discriminates": None if interval is None else interval[0] > 0,
                    }
                )
        if not rows:
            return _no_data("no development trials of a declared negative control")
        return {
            "status": "ok",
            "unit": "task (development split, mean over runs)",
            "cells": rows,
        }

    # -- saturation and flakiness ----------------------------------------------------------------
    @staticmethod
    def _saturation(summaries: list[tuple[dict[str, Any], dict[str, Any]]]) -> dict[str, Any]:
        rows = []
        for ref, summary in summaries:
            tasks = {t for cell in summary["cells"].values() for t in cell["tasks"]}
            saturated = [t for t in summary["saturated_everywhere"] if t in tasks]
            rows.append(
                {
                    "summary_ref": ref,
                    "summarized_at": summary["summarized_at"],
                    "tasks": len(tasks),
                    "saturated_everywhere": len(saturated),
                    "informative_any": len(summary["informative_any"]),
                    "share": len(saturated) / len(tasks) if tasks else None,
                }
            )
        if not rows:
            return _no_data("no calibration summary of this evaluator version")
        latest = max(rows, key=lambda r: r["summarized_at"])
        return {"status": "ok", "share": latest["share"], "latest_summary": latest, "rows": rows}

    @staticmethod
    def _flakiness(
        summaries: list[tuple[dict[str, Any], dict[str, Any]]], corpus_check: dict[str, Any] | None
    ) -> dict[str, Any]:
        calibration = sorted(
            {
                task
                for _, summary in summaries
                for cell in summary["cells"].values()
                for task, row in cell["tasks"].items()
                if row.get("class") == "flaky_grading"
            }
        )
        if corpus_check is None:
            return {
                **_no_data("no corpus check output given (amplai meta corpus check --repeats 3)"),
                "calibration_flaky_tasks": calibration,
            }
        tasks = corpus_check.get("tasks")
        repeats = corpus_check.get("repeats")
        if (
            not isinstance(tasks, dict)
            or type(repeats) is not int
            or repeats < 1
            or any(not isinstance(row, dict) for row in tasks.values())
        ):
            raise RuntimeFault(
                "QUALITY_CORPUS_CHECK", "corpus_check is the JSON output of meta corpus check"
            )
        flagged = sorted(t for t, row in tasks.items() if row.get("flaky") is True)
        checked = corpus_check.get("checked")
        if type(checked) is not int or checked < 0:
            checked = sum(row.get("fair") is not None for row in tasks.values())
        return {
            "status": "ok" if checked else "no_data",
            "repeats": repeats,
            "meets_protocol": repeats >= FLAKY_REPEATS,
            "checked": checked,
            "flagged": flagged,
            "share": len(flagged) / checked if checked else None,
            "calibration_flaky_tasks": calibration,
        }

    # -- stage reports ---------------------------------------------------------------------------
    def _stage_rows(
        self, plans: list[tuple[dict[str, Any], dict[str, Any]]]
    ) -> list[dict[str, Any]]:
        """One row per reported stage of the plans' proposals (stage-run heads)."""
        rows = []
        for _, plan in plans:
            run = self._head_or_none(STAGE_RUN_KIND, "stagerun-" + plan["proposal_id"])
            if run is None:
                continue
            for stage, entry in run["data"]["stages"].items():
                report_ref = entry.get("report_ref")
                if not _is_ref(report_ref):
                    continue
                report = self.store.get(self.scope, "eval-report", report_ref)
                rows.append(
                    {
                        "proposal_id": plan["proposal_id"],
                        "stage": stage,
                        "report_ref": report_ref,
                        "report": report,
                        "analysis": self._analysis(report),
                    }
                )
        return rows

    @staticmethod
    def _gap(stage_rows: list[dict[str, Any]]) -> dict[str, Any]:
        deltas: dict[str, dict[str, float]] = {}
        for row in stage_rows:
            delta = (row["analysis"] or {}).get("candidate_minus_baseline")
            if row["stage"] in ("screening", "holdout") and isinstance(delta, int | float):
                deltas.setdefault(row["proposal_id"], {})[row["stage"]] = float(delta)
        rows: list[dict[str, Any]] = [
            {
                "proposal_id": proposal_id,
                "screening_delta": both["screening"],
                "holdout_delta": both["holdout"],
                "gap": both["screening"] - both["holdout"],
            }
            for proposal_id, both in sorted(deltas.items())
            if {"screening", "holdout"} <= set(both)
        ]
        if not rows:
            return _no_data("no proposal with both a screening and a holdout report")
        return {"status": "ok", "proposals": rows, "mean_gap": mean(r["gap"] for r in rows)}

    def _cost(self, stage_rows: list[dict[str, Any]]) -> dict[str, Any]:
        groups: dict[str, dict[str, int]] = {}
        for row in stage_rows:
            verdict = row["report"]["verdict"]
            name = "concluded" if verdict in ("pass", "fail") else verdict
            group = groups.setdefault(
                name, {"reports": 0, "trials": 0, "tokens": 0, "unknown_token_trials": 0}
            )
            group["reports"] += 1
            for ref in row["report"]["run_refs"]:
                trial = self.store.get(self.scope, "eval-trial", ref)
                group["trials"] += 1
                if (
                    type(trial.get("input_tokens")) is int
                    and type(trial.get("output_tokens")) is int
                ):
                    group["tokens"] += trial["input_tokens"] + trial["output_tokens"]
                else:
                    group["unknown_token_trials"] += 1
        if not groups:
            return _no_data("no stage report of this evaluator version")
        out: dict[str, Any] = {}
        for name, g in sorted(groups.items()):
            out[name] = {
                **g,
                "trials_per_report": g["trials"] / g["reports"],
                "tokens_per_report": g["tokens"] / g["reports"],
            }
        total = sum(g["reports"] for g in groups.values())
        return {
            "status": "ok",
            "verdicts": out,
            "concluded_share": groups.get("concluded", {}).get("reports", 0) / total,
            "note": "tokens sum the trials with known usage; unknown ones are counted apart",
        }

    # -- contamination ---------------------------------------------------------------------------
    def _contamination(
        self,
        version: dict[str, Any],
        plans: list[tuple[dict[str, Any], dict[str, Any]]],
        stage_rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        corpus_ref = version["corpus_ref"]
        corpus = self.store.get(self.scope, "eval-corpus", corpus_ref)
        keys = [corpus_ref["id"]] + [
            "case-" + c["artifact_ref"]["digest"][7:]
            for c in corpus["cases"]
            if c["split"] == "holdout"
        ]
        heads = [(k, self._head_or_none("holdout-use", k)) for k in keys]
        corpus_head = heads[0][1]
        uses = [len(h["data"]["experiment_refs"]) for _, h in heads if h is not None]
        # a stage plan's proposal names its change artifact: `leak_scan` is null until a leak scan
        # is recorded there (`runtime/execution/meta_ops.py`), so unscanned is reported apart
        proposals = {
            ref["id"]: value
            for ref, value in self.store.list_objects(self.scope, "harness-change-proposal")
        }
        scanned = unscanned = unreadable = hits = 0
        for _, plan in plans:
            proposal = proposals.get(plan["proposal_id"])
            if proposal is None:
                continue
            try:
                change = read_receipt(self.artifacts.read(self.scope, proposal["change_artifact"]))
            except RuntimeFault:
                unreadable += 1
                continue
            scan = change.get("leak_scan")
            if isinstance(scan, dict) and type(scan.get("hits")) is int:
                scanned += 1
                hits += scan["hits"]
            else:
                unscanned += 1
        return {
            "status": "ok",
            "corpus_ref": corpus_ref,
            "holdout_uses": len(corpus_head["data"]["experiment_refs"]) if corpus_head else 0,
            "holdout_use_limit": corpus["holdout_use_limit"],
            "max_case_uses": max(uses) if uses else 0,
            "contaminated_heads": sum(
                1 for _, h in heads if h is not None and h["data"].get("contaminated")
            ),
            "corpus_findings": len(corpus.get("contamination_findings") or []),
            "reports_with_findings": sum(
                1 for r in stage_rows if r["report"].get("contamination_findings")
            ),
            "leak_scan": {
                "proposals_scanned": scanned,
                "proposals_unscanned": unscanned,
                "unreadable_change_artifacts": unreadable,
                "hits": hits,
            },
            "note": "leak-gate refusals at screen are not stored; only a recorded leak_scan counts",
        }

    # -- real goals ------------------------------------------------------------------------------
    def _real_goals(self) -> list[dict[str, Any]]:
        """The real (non-trial) goals' plan records, with the planner's task class."""
        classes = {
            value["graph_ref"]["digest"]: value["task_class"]
            for _, value in self.store.list_objects(self.scope, TASK_CLASS_KIND)
            if _is_ref(value.get("graph_ref"))
        }
        goals = []
        for goal_id, status, plan in self._heads(PLAN_RECORD_KIND):
            if plan.get("trial"):
                continue
            graph = plan.get("graph_ref")
            goals.append(
                {
                    "goal_id": goal_id,
                    "status": status,
                    "created_at": _stamp(plan.get("created_at")),
                    "finished_at": _stamp(plan.get("finished_at")),
                    "outcome": (plan.get("publication_outcome") or {}).get("state"),
                    "task_class": classes.get(graph["digest"]) if _is_ref(graph) else None,
                }
            )
        return goals

    @staticmethod
    def _window(goals: list[dict[str, Any]], low: float, high: float) -> dict[str, Any]:
        inside = [
            g
            for g in goals
            if g["status"] in REAL_TERMINAL
            and g["finished_at"] is not None
            and low <= g["finished_at"] < high
        ]
        verified = sum(g["status"] in REAL_VERIFIED for g in inside)
        merged = sum(g["outcome"] == "MERGED" for g in inside)
        closed = sum(g["outcome"] == "CLOSED" for g in inside)
        n = len(inside)
        return {
            "goals": n,
            "verified": verified,
            "merged": merged,
            "closed": closed,
            "verified_rate": verified / n if n else None,
            "acceptance_rate": merged / (merged + closed) if merged + closed else None,
            "verified_and_merged_rate": merged / n if n else None,
        }

    def _agreement(self, floor: float) -> dict[str, Any]:
        promoted_at: dict[str, float] = {}
        promoted_text: dict[str, str] = {}
        for proposal_id, created in self._events("evolution.promoted"):
            moment = _stamp(created)
            if moment is not None:
                promoted_at[proposal_id], promoted_text[proposal_id] = moment, created
        states = {
            proposal_id: state
            for proposal_id, state, _ in self._heads("evolution")
            if state in ("promoted", "rolled_back")
        }
        promoted = [
            (proposal_id, data)
            for proposal_id, _, data in self._heads("evolution")
            if proposal_id in states and proposal_id in promoted_at
        ]
        if not promoted:
            return _no_data("no promoted candidate")
        order = sorted(promoted_at[p] for p, _ in promoted)
        goals = self._real_goals()
        rows = []
        for proposal_id, data in sorted(promoted, key=lambda row: promoted_at[row[0]]):
            at = promoted_at[proposal_id]
            if at < floor:
                continue
            index = order.index(at)
            before_from = order[index - 1] if index else -math.inf
            after_to = order[index + 1] if index + 1 < len(order) else math.inf
            trials = data.get("canary_trials") or []
            successes = sum(t.get("success") is True for t in trials)
            rows.append(
                {
                    "proposal_id": proposal_id,
                    "state": states[proposal_id],
                    "promoted_at": promoted_text[proposal_id],
                    "canary": {
                        "runs": len(trials),
                        "successes": successes,
                        "success_rate": successes / len(trials) if trials else None,
                        "incidents": len(data.get("canary_incidents") or []),
                    },
                    "before": self._window(goals, before_from, at),
                    "after": self._window(goals, at, after_to),
                }
            )
        if not rows:
            return _no_data("no promotion since the window start")
        return {
            "status": "ok",
            "promotions": rows,
            "note": "descriptive, small n; before = since the previous promotion, after = until "
            "the next; verified_and_merged_rate = merged / terminal real goals",
        }

    def _coverage(self, version: dict[str, Any], floor: float) -> dict[str, Any]:
        corpus = self.store.get(self.scope, "eval-corpus", version["corpus_ref"])
        corpus_counts: dict[str, int] = {}
        for case in corpus["cases"]:
            corpus_counts[case["task_class"]] = corpus_counts.get(case["task_class"], 0) + 1
        real: dict[str, int] = {}
        unmapped: dict[str, int] = {}
        for goal in self._real_goals():
            if (goal["created_at"] or 0.0) < floor:
                continue
            task_class = goal["task_class"] or "unreported"
            domain = TASK_CLASS_TO_DOMAIN.get(task_class)
            if domain is None:
                unmapped[task_class] = unmapped.get(task_class, 0) + 1
            else:
                real[domain] = real.get(domain, 0) + 1
        if not real:
            return {
                **_no_data("no real goal with a mapped task class"),
                "unmapped_task_classes": unmapped,
                "corpus_mix": shares(corpus_counts),
                "table_status": TASK_CLASS_TO_DOMAIN_PROVISIONAL,
            }
        corpus_mix, real_mix = shares(corpus_counts), shares(real)
        return {
            "status": "ok",
            "total_variation": total_variation(corpus_mix, real_mix),
            "corpus_mix": corpus_mix,
            "real_mix": real_mix,
            "real_goals_mapped": sum(real.values()),
            "unmapped_task_classes": unmapped,
            "corpus_domains_without_real_class": sorted(
                d for d in corpus_mix if d not in TASK_CLASS_TO_DOMAIN.values()
            ),
            "table": dict(TASK_CLASS_TO_DOMAIN),
            "table_status": TASK_CLASS_TO_DOMAIN_PROVISIONAL,
        }

    # -- status ----------------------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        """The running evaluator code, every stored evaluator version (latest revision) with its
        requalification, and the evaluator changes."""
        running = versions.code_digests()
        rows = []
        for ref, value in sorted(
            self._latest(versions.KIND), key=lambda row: _version_number(row[1]["version"])
        ):
            try:
                _, record = resolve_ref(self.store, self.scope, value["requalification_ref"])
                all_equal: bool | None = record.get("all_equal") is True
                vacuous: bool | None = not record.get("reports")
            except RuntimeFault:
                all_equal, vacuous = None, None
            rows.append(
                {
                    "version": value["version"],
                    "ref": ref,
                    "analysis_code_digest": value["analysis_code_digest"],
                    "service_code_digest": value["service_code_digest"],
                    "code_matches_running": all(
                        value[k] == running[k] for k in versions.DIGEST_FIELDS
                    ),
                    "requalification_ref": value["requalification_ref"],
                    "requalification_all_equal": all_equal,
                    "requalification_vacuous": vacuous,  # Q-02 over zero stored reports
                    "approved_by": value["approved_by"]["subject_id"],
                    "at": value["at"],
                }
            )
        return {
            "running_code": running,
            "versions": rows,
            "current_version_ref": versions.current_version_ref(self.store, self.scope),
            "changes": [self.change(cid) for cid, _, _ in self._heads(CHANGE_KIND)],
            "qualification_suite_files": list(QUALIFICATION_FILES),
            "task_class_to_domain": {
                "table": dict(TASK_CLASS_TO_DOMAIN),
                "status": TASK_CLASS_TO_DOMAIN_PROVISIONAL,
            },
        }

    def version_ref(self, version: str | None = None) -> dict[str, Any]:
        """The newest stored revision of `version` (default: of the newest version); NOT_FOUND
        when none is stored."""
        stored = self._latest(versions.KIND)
        if version is not None:
            versions.version_id(version)
            stored = [row for row in stored if row[1]["version"] == version]
        if not stored:
            raise RuntimeFault("NOT_FOUND", "No such stored evaluator version")
        ref: dict[str, Any] = max(stored, key=lambda row: _version_number(row[1]["version"]))[0]
        return ref

    def requalify(self, actor: Actor, evaluator_version: str) -> dict[str, Any]:
        """Re-analyze every stored report of this scope with the running evaluator (§7.8 Q-02) and
        write the `evaluator-requalification` record; returns its ref and counts. The operator
        (`evaluator.approve`, IC-26) runs it, never a proposer or a service. `vacuous` is true
        when the scope holds no report."""
        self._operator(actor)
        versions.version_id(evaluator_version)
        ref, value = self._requalify(evaluator_version)
        return {
            "requalification_ref": ref,
            "evaluator_version": evaluator_version,
            "reports": len(value["reports"]),
            "equal": sum(row["equal"] for row in value["reports"]),
            "all_equal": value["all_equal"],
            "vacuous": not value["reports"],
        }

    def _requalify(self, evaluator_version: str) -> tuple[dict[str, Any], dict[str, Any]]:
        script = self._requalifier or load_requalifier()
        value: dict[str, Any] = script.requalify_scope(
            self.store,
            self.artifacts,
            self.scope,
            store_label=str(self.store.root),
            evaluator_version=evaluator_version,
        )
        ref: dict[str, Any] = script.write_record(self.store, self.scope, value)
        return ref, value

    # -- evaluator change lifecycle --------------------------------------------------------------
    def _operator(self, actor: Actor) -> None:
        """A human operator without `harness.propose` that holds `evaluator.approve` (IC-26); a
        proposer or a service identity, or a human without the permission (`corpus.manage` does
        not suffice), is refused (`FORBIDDEN`), as `versions.write_version` refuses it."""
        if actor.scope != self.scope:
            raise RuntimeFault("SCOPE_MISMATCH", "The actor belongs to another scope")
        if actor.kind != "human" or "harness.propose" in actor.permissions:
            raise RuntimeFault(
                "FORBIDDEN", "Only a human non-proposer operator handles an evaluator change"
            )
        actor.require(CHANGE_PERMISSION)

    def _no_active_experiment(self) -> None:
        """A change is qualified and approved beside candidate experiments, never inside one."""
        running = [i for i, state, _ in self._heads("experiment") if state == "running"]
        running += [i for i, state, _ in self._heads("calibration-run") if state == "running"]
        running += [
            i
            for i, _, data in self._heads(STAGE_RUN_KIND)
            if any(s.get("state") == "running" for s in data.get("stages", {}).values())
        ]
        if running:
            raise Hold(
                "EVALUATOR_CHANGE_ACTIVE",
                "An experiment, calibration or stage is running; an evaluator change waits",
                details=sorted(running),
            )

    def _latest_version(self) -> tuple[dict[str, Any], dict[str, Any]] | None:
        stored = self._latest(versions.KIND)
        if not stored:
            return None
        return max(stored, key=lambda row: _version_number(row[1]["version"]))

    def _target(
        self, to: Any, current: tuple[dict[str, Any], dict[str, Any]] | None
    ) -> dict[str, Any]:
        """The validated, completed target of a change: `{"version", "changes", "corpus_ref",
        "stage_templates"}`. `corpus_ref` and `stage_templates` default to the replaced version's;
        the first version needs a `corpus_ref`."""
        fields = {"version", "changes", "corpus_ref", "stage_templates"}
        if not isinstance(to, dict) or not {"version", "changes"} <= set(to) or set(to) - fields:
            raise RuntimeFault(
                "EVALUATOR_CHANGE_TARGET",
                "`to` holds version and changes, and optionally corpus_ref and stage_templates",
            )
        version = to["version"]
        versions.version_id(version)  # RuntimeFault EVALUATOR_VERSION for a bad name
        stored = {value["version"] for _, value in self._latest(versions.KIND)}
        if version in stored or (
            current is not None
            and _version_number(version) <= _version_number(current[1]["version"])
        ):
            raise RuntimeFault(
                "EVALUATOR_CHANGE_TARGET", "The target is a new, higher evaluator version"
            )
        changes = to["changes"]
        if (
            not isinstance(changes, list)
            or not changes
            or any(
                not isinstance(c, dict)
                or set(c) != {"kind", "detail"}
                or c["kind"] not in CHANGE_KINDS
                or not isinstance(c["detail"], str)
                or not c["detail"].strip()
                for c in changes
            )
        ):
            raise RuntimeFault(
                "EVALUATOR_CHANGE_TARGET",
                "changes is a nonempty list of {kind, detail}; kinds: " + ", ".join(CHANGE_KINDS),
            )
        corpus_ref = to.get("corpus_ref", current[1]["corpus_ref"] if current else None)
        if not _is_ref(corpus_ref):
            raise RuntimeFault("EVALUATOR_CHANGE_TARGET", "A corpus_ref is required")
        kind, _ = resolve_ref(self.store, self.scope, corpus_ref)
        if kind != "eval-corpus":
            raise RuntimeFault("EVALUATOR_CHANGE_TARGET", "corpus_ref names another record kind")
        templates = to.get("stage_templates", current[1]["stage_templates"] if current else {})
        if not isinstance(templates, dict):
            raise RuntimeFault("EVALUATOR_CHANGE_TARGET", "stage_templates is an object")
        return {
            "version": version,
            "changes": [dict(c) for c in changes],
            "corpus_ref": corpus_ref,
            "stage_templates": templates,
        }

    def _change_head(self, change_id: str) -> dict[str, Any]:
        head = self.store.head(self.scope, CHANGE_KIND, change_id)  # NOT_FOUND if unknown
        return head

    def change(self, change_id: str) -> dict[str, Any]:
        head = self._change_head(change_id)
        return {"change_id": change_id, **head["data"]}

    def _move(
        self,
        change_id: str,
        expected: tuple[int, str],
        state: str,
        updates: dict[str, Any],
    ) -> None:
        """One compare-and-set of the change head: `expected` = (row_version, state)."""
        with self.store.tx() as db:
            head = self.store.head(self.scope, CHANGE_KIND, change_id, db=db)
            if (head["row_version"], head["state"]) != expected:
                raise Hold("EVALUATOR_CHANGE_STATE", "The evaluator change moved underneath")
            data = {**head["data"], **updates, "state": state}
            self.store.cas(db, self.scope, CHANGE_KIND, change_id, head["row_version"], state, data)
            self.store.event(
                db, self.scope, CHANGE_KIND, change_id, "evaluator.change." + state,
                {"state": state},
            )  # fmt: skip

    def propose_change(self, actor: Actor, *, to: dict[str, Any], reason: str) -> str:
        """Open an `evaluator-change` head (state `proposed`); returns its id. One change is open
        at a time. `from` is the newest stored evaluator version, or null for the first one."""
        self._operator(actor)
        if not isinstance(reason, str) or not reason.strip():
            raise RuntimeFault("EVALUATOR_CHANGE_REASON", "A change needs a reason")
        for change_id, state, _ in self._heads(CHANGE_KIND):
            if state in OPEN_STATES:
                raise Hold(
                    "EVALUATOR_CHANGE_OPEN",
                    "Another evaluator change is open; approve or reject it first",
                    details=change_id,
                )
        current = self._latest_version()
        target = self._target(to, current)
        change_id = new_id("evalchange")
        data = {
            "state": "proposed",
            "from": current[0] if current else None,
            "to": None,  # the new evaluator-version ref, set by approve_change
            "target": target,
            "reason": reason.strip(),
            "requalification_ref": None,
            "quality_ref": None,
            "comparison": None,
            "code_digests": None,
            "qualification_suite": None,  # set by qualify_change (IC-25)
            "qualification_scope": None,
            "proposed_by": actor.wire(),
            "proposed_at": now(),
            "qualified_by": None,
            "decided_by": None,
        }
        with self.store.tx() as db:
            self.store.cas(db, self.scope, CHANGE_KIND, change_id, 0, "proposed", data)
            self.store.event(
                db, self.scope, CHANGE_KIND, change_id, "evaluator.change.proposed",
                {"state": "proposed", "version": target["version"]},
            )  # fmt: skip
        return change_id

    def _run_suite(self) -> dict[str, Any]:
        """Run the Q-suite (IC-25) and parse its JUnit file. `EVALUATOR_CHANGED` (Hold) when the
        evaluator code changed while the suite ran: its result would not describe either version."""
        before = versions.code_digests()
        with tempfile.TemporaryDirectory(prefix="amplai-qsuite-") as tmp:
            junit = Path(tmp) / "junit.xml"
            returncode = self._suite_runner(QUALIFICATION_FILES, junit)
            result = parse_junit(junit, QUALIFICATION_FILES)
        if versions.code_digests() != before:
            raise Hold(
                "EVALUATOR_CHANGED",
                "The evaluator code changed while the Q-suite ran; run it again",
            )
        return {**result, "returncode": returncode, "code_digests": before}

    def qualify_change(self, actor: Actor, change_id: str) -> dict[str, Any]:
        """Qualify the change alone (IC-25): run the §7.8 Q-suite in a subprocess, re-run the Q-02
        requalification of the target version over every stored report and measure the quality of
        the replaced version. The head becomes `qualified` only when the suite passed (exit code 0,
        every file ran with zero failures and errors) and every recomputed verdict equals its
        recorded one; otherwise it stays `proposed` with the differences in `comparison` and the
        suite result in `qualification_suite`. Returns the requalification ref.

        Q-02 over zero stored reports is vacuously equal: allowed, flagged
        `qualification_scope.vacuous`. `approve_change` binds to the code digests of this call."""
        self._operator(actor)
        head = self._change_head(change_id)
        if head["state"] not in OPEN_STATES:
            raise Hold("EVALUATOR_CHANGE_STATE", "Only an open change is qualified")
        self._no_active_experiment()
        data = head["data"]
        target = data["target"]
        suite = self._run_suite()
        ref, requal = self._requalify(target["version"])
        quality_ref = self.measure(data["from"], since=EPOCH) if data["from"] is not None else None
        changed = [row["report_ref"]["id"] for row in requal["reports"] if not row["equal"]]
        comparison = {
            "reports": len(requal["reports"]),
            "equal": len(requal["reports"]) - len(changed),
            "changed_reports": changed,
        }
        qualified = suite_passed(suite) and bool(requal["all_equal"])
        self._move(
            change_id,
            (head["row_version"], head["state"]),
            "qualified" if qualified else "proposed",
            {
                "requalification_ref": ref,
                "quality_ref": quality_ref,
                "comparison": comparison,
                "code_digests": suite["code_digests"] if qualified else None,
                "qualified_by": actor.wire() if qualified else None,
                "qualification_suite": suite,
                "qualification_scope": {
                    "suite_files": list(QUALIFICATION_FILES),
                    "suite_passed": suite_passed(suite),
                    "q02_all_equal": bool(requal["all_equal"]),
                    "vacuous": not requal["reports"],  # Q-02 over zero stored reports
                    "provisional": "IC-25",
                },
            },
        )
        return ref

    def approve_change(self, actor: Actor, change_id: str) -> dict[str, Any]:
        """Human operator only: write the new `evaluator-version` of a qualified change and mark
        the change `approved` (the ref of the new version is returned and kept as `to`)."""
        self._operator(actor)
        head = self._change_head(change_id)
        if head["state"] != "qualified":
            raise Hold(
                "EVALUATOR_UNQUALIFIED", "Only a change qualified alone can be approved",
                details=head["state"],
            )  # fmt: skip
        self._no_active_experiment()
        data = head["data"]
        suite = data.get("qualification_suite")
        if not suite_passed(suite):
            raise Hold(
                "EVALUATOR_UNQUALIFIED",
                "The change carries no passing Q-suite result",
                details="qualification_suite",
            )
        running = versions.code_digests()
        if data["code_digests"] != running or suite["code_digests"] != running:
            raise Hold(
                "EVALUATOR_CHANGED",
                "The evaluator code changed after qualification; qualify the change again",
            )
        target = data["target"]
        new_ref: dict[str, Any] = versions.write_version(
            self.store,
            actor,
            corpus_ref=target["corpus_ref"],
            requalification_ref=data["requalification_ref"],
            stage_templates=target["stage_templates"],
            version=target["version"],
        )
        self._move(
            change_id,
            (head["row_version"], head["state"]),
            "approved",
            {"to": new_ref, "decided_by": actor.wire()},
        )
        return new_ref

    def reject_change(self, actor: Actor, change_id: str, reason: str) -> str:
        """Human operator only: close an open change without a new version."""
        self._operator(actor)
        if not isinstance(reason, str) or not reason.strip():
            raise RuntimeFault("EVALUATOR_CHANGE_REASON", "A rejection needs a reason")
        head = self._change_head(change_id)
        if head["state"] not in OPEN_STATES:
            raise Hold("EVALUATOR_CHANGE_STATE", "Only an open change is rejected")
        self._move(
            change_id,
            (head["row_version"], head["state"]),
            "rejected",
            {"decided_by": actor.wire(), "reject_reason": reason.strip()},
        )
        return "rejected"


def _task_rates(
    trials: list[dict[str, Any]], plan_ref: dict[str, Any], cell: str, composition: dict[str, Any]
) -> dict[str, float]:
    """Per development task: the pass rate over the runs of `composition` in `cell` of the plan
    (a run with an unknown result or an unknown effect does not count, as in calibration)."""
    runs: dict[str, list[bool]] = {}
    for trial in trials:
        if (
            trial["calibration_plan_ref"] == plan_ref
            and trial["cell_id"] == cell
            and trial["composition_ref"] == composition
            and trial["split"] == "development"
            and trial["success"] is not None
            and not trial["unknown_effects"]
        ):
            runs.setdefault(trial["task_id"], []).append(bool(trial["success"]))
    return {task: sum(r) / len(r) for task, r in runs.items()}
