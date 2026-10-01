"""Proposer ensemble, predictions and scoring, dreaming job, removal sweep (Work 033 S13, D-100,
interfaces.md §2.12, §3.12, §9.4-§9.10, IC-11, IC-14, ``plan.md`` §11).

The proposer reads development data only (§9.4): the input builder takes ``trial-metrics`` rows
with ``split == "development"`` (experiment and calibration sources; validation calibration rows
are dropped by that field), development traces through ``TraceService`` as the proposer identity
(``Hold TRACE_ACL`` otherwise), ``EliteArchive.proposer_view`` (no lineage verdicts) and, of
earlier proposals, what they changed, their own prediction and their screening-stage prediction
score only (never a stage state or decision class). Hack-guard
signals are left out of its inputs: they reject only, they are never a score (§9.10).

An earlier proposal may come from the operator (``amplai meta propose``), whose prediction and
hypothesis no one filtered: its prediction keeps development task ids only, and an earlier
proposal or a trace whose shown entry the leak gate hits is left out (counted in ``last``). As the
last line of defence the leak gate scans the whole ``inputs.json`` and every trace body before the
scratch directory is written; a hit holds ``LEAK_GATE`` (kind and place, never the token) before
any turn runs. The dreaming job does the same with its traces and inputs.

Its read-only turns run on an **empty scratch directory** holding only these inputs (IC-14), never
on a copy of this repository (which holds the corpus with hidden tests and holdout); the scratch
directory is removed after the run.

Ensemble (§9.4): the cheap cell drafts at most ``drafts`` edits in one turn (``PROPOSAL_SCHEMA``);
drafts with invalid content are dropped; prediction task ids outside the development split are
dropped (the count goes into the component rationale, never the ids); a draft whose normalized
content digest equals an existing version of its kind or another draft, or whose word-set Jaccard
similarity with an existing version of its kind is >= 0.9, is dropped (``deduplicated``); every
draft passes the leak gate (a hit drops it, ``leak_refused``); the strong cell refines the top
``refine`` drafts by ``expected_delta`` (the surrogate is S12) in one turn each, and each refined
edit is checked again. Only refined edits are submitted: ``ComponentService.register`` (source
``proposer``), ``LocalMetaOps.propose_components`` with the prediction (a ``proposal-prediction``
record). Proposals stay ``draft`` until screened.

Model-facing schemas carry no count or length keywords (as the S9 schemas; whether Codex
``--output-schema`` strict mode accepts them or a free-form ``content`` object is 확인 필요); the
bounds of §9.5 are checked here.

Prediction scoring (§9.6, IC-11): after screening, task level (improved = candidate unit passes
and baseline fails; regressed = the reverse; precision and recall over the predicted tasks that
were evaluated) → ``prediction-score`` stage ``screening``, the only score the proposer reads.
After focused and holdout, bucket level (the sign of the per-domain success delta) → operator-only
scores. A bucket naming ``task_class`` is not scored (a trial records only its domain).
``StageRunner`` calls ``score_predictions`` after each evaluated screening, focused and holdout
report (``stages.StageRunner._after_report``; a scoring failure is a stage finding, never a
verdict change); ``score_evaluated`` writes every score due for a proposal (``amplai meta proposer
score``) and the proposer run writes a missing screening score before it reads it, both
idempotent.

Dreaming (§9.7): one read-only turn over the night's development traces, their outcomes and the
champion's ``memory_notes``; deltas whose evidence is not among the traces shown, whose app is not
installed or whose targets are invalid are refused; the notes are dated with the night, checked
(≤ 40 per (app, task class)), leak-gated, registered (source ``dreaming``) and submitted as a
class-A proposal.

Removal sweep (§9.8, IC-24 provisional): for every manifest slot where the cell's champion differs
in content from its v1 content (``policies.V1``; the empty slot for ``driver_options`` and the
deciders, which have none), an ordinary draft proposal with origin ``removal_sweep`` in its change
artifact (baseline = champion, candidate = champion with that slot back to v1: the install's
``<kind>.baseline`` version with that content, else a ``<kind>.v1`` component of source
``sweep``), reviewed and screened like any proposal. A champion slot whose installed content is
not ``policies.V1`` (e.g. ``limits`` under a lower deployment ceiling, a route order other than
the v1 one) is swept too; a variant that cannot be built is skipped. An identical open sweep
proposal is not proposed again. ``removal_verdict`` is the §9.8 criterion (focused class
``efficiency``, or ``non_inferior`` with fewer tokens per solved task) and the removal gate
(``stages.check_removal``) holds the holdout stage and the experiment and canary approvals of a
sweep proposal with ``Hold NOT_A_REMOVAL`` unless it holds; ``amplai meta proposer sweep`` shows
it for the open sweep proposals (``sweep_proposals``). IC-19 is unchanged (derived proposals run
only inside their parent's ablation stage).

Change artifacts of submitted proposals name their ``proposer_run_ref`` and ``leak_scan`` (§2.12;
``ProposerEnsemble.run``).
"""

from __future__ import annotations

import contextlib
import copy
import json
import re
import shutil
import tempfile
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..runtime.contracts.identity import ID, canonical, digest, new_id, now
from ..runtime.contracts.semantics import resolve_ref
from ..runtime.errors import Hold, RuntimeFault
from ..runtime.execution import policies, releases
from ..runtime.execution.codex import put_record
from ..runtime.execution.planner_codex import TASK_CLASSES
from ..runtime.storage.store import Scope, Store
from .archive import EliteArchive
from .components import COMPONENT_ID, KINDS, ComponentService
from .traces import TRACE_KIND, TraceService, cut_copy

if TYPE_CHECKING:
    from ..runtime.execution.meta_ops import LocalMetaOps
    from ..runtime.execution.readonly_turn import ReadOnlyTurn, TurnResult
    from .leak_gate import LeakGate

Ref = dict[str, Any]

PROPOSER_KINDS = (
    "role_prompt",
    "interpretation",
    "env_bootstrap",
    "memory_notes",
    "retrieval",
    "feedback_form",
    "attempt_policy",
    "execution_strategy",
    "driver_options",
    "fast_checks",
    "limits",
    "route_policy",
)  # §9.5: environment_image, judge_model, decider, decision_method are not proposer kinds
MAX_EDITS = 6
MAX_TASK_IDS = 20
RATIONALE_MAX, HYPOTHESIS_MAX = 2000, 1000
RISKS = ("low", "medium", "high")
MAX_TRACES = 12  # §9.4 inputs: up to 12 traces, failures first
TRACE_INPUT_BYTES = 32 * 1024  # each cut to 32 KiB
JACCARD_LIMIT = 0.9
PARENTS = 3
RUN_KIND, RUN_SCHEMA = "proposer-run", "amplai.proposer-run.v1"
SCORE_KIND, SCORE_SCHEMA = "prediction-score", "amplai.prediction-score.v1"
PREDICTION_KIND = "proposal-prediction"
OBSERVATION_KIND = "harness-observation"
SCORED_STAGES = ("screening", "focused", "holdout")
PROPOSER_SCORE_STAGE = "screening"  # §9.6: the only stage whose scores the proposer reads
STAGE_ORDER = ("screening", "focused", "ablation", "holdout")
VERDICT_STAGES = ("focused", "holdout")
DREAM_MAX_DELTAS = 10
NOTE_TEXT_MAX = 300
EVIDENCE_MAX = 8
DREAM_OPS = ("add", "merge", "delete")
TERMINAL_STATES = frozenset({"promoted", "rejected", "rolled_back", "aborted"})
REMOVAL_STAGE = "focused"  # §9.8: the removal criterion reads the variant's focused stage
WORD = re.compile(r"[a-z0-9_]+")
NIGHT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
REPO_ROOT = Path(__file__).resolve().parents[3]  # IC-14: no read-only turn runs on this checkout
# the development row fields the proposer sees (§9.4); guards are left out (§9.10)
ROW_FIELDS = (
    "task_id",
    "domain",
    "arm",
    "cell_id",
    "strategy",
    "source",
    "stage",
    "phase",
    "success",
    "tokens",
    "wall_seconds",
    "attempts_used",
    "turns",
    "escalations",
    "reviewer_rounds",
    "fix_requests",
    "agent_calls",
)

# -- model-facing schemas (§9.5, §9.7) --
_STRINGS: dict[str, Any] = {"type": "array", "items": {"type": "string"}}
_BUCKETS: dict[str, Any] = {
    "type": "array",
    "items": {
        "type": "object",
        "additionalProperties": False,
        "properties": {"domain": {"type": "string"}, "task_class": {"type": "string"}},
    },
}
PREDICTION_FIELDS = (
    "improve_task_ids",
    "regress_task_ids",
    "improve_buckets",
    "regress_buckets",
    "expected_delta",
)
EDIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "kind",
        "component_id",
        "content",
        "rationale",
        "hypothesis",
        "predictions",
        "risk",
    ],
    "properties": {
        "kind": {"type": "string", "enum": list(PROPOSER_KINDS)},
        "component_id": {"type": "string"},
        "content": {"type": "object"},
        "rationale": {"type": "string"},
        "hypothesis": {"type": "string"},
        "predictions": {
            "type": "object",
            "additionalProperties": False,
            "required": list(PREDICTION_FIELDS),
            "properties": {
                "improve_task_ids": _STRINGS,
                "regress_task_ids": _STRINGS,
                "improve_buckets": _BUCKETS,
                "regress_buckets": _BUCKETS,
                "expected_delta": {"type": "number"},
            },
        },
        "risk": {"type": "string", "enum": list(RISKS)},
    },
}
PROPOSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["edits"],
    "properties": {"edits": {"type": "array", "items": EDIT_SCHEMA}},
}
DREAM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["deltas"],
    "properties": {
        "deltas": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["op", "text", "targets", "app", "task_class", "evidence"],
                "properties": {
                    "op": {"type": "string", "enum": list(DREAM_OPS)},
                    "text": {"type": "string"},
                    "targets": {"type": "array", "items": {"type": "integer"}},
                    "app": {"type": "string"},
                    "task_class": {"type": ["string", "null"]},
                    "evidence": _STRINGS,
                },
            },
        }
    },
}


# -- parsing and dedup --
def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _buckets(value: Any) -> list[dict[str, str]] | None:
    """Buckets with a ``domain`` and/or ``task_class`` string (empty ones left out)."""
    if not isinstance(value, list):
        return None
    out: list[dict[str, str]] = []
    for bucket in value:
        if not isinstance(bucket, dict) or not set(bucket) <= {"domain", "task_class"}:
            return None
        if any(not isinstance(v, str) or not v.strip() for v in bucket.values()):
            return None
        if bucket and bucket not in out:
            out.append({k: str(v) for k, v in sorted(bucket.items())})
    return out


def parse_edit(raw: Any, *, development: set[str]) -> tuple[dict[str, Any] | None, int]:
    """(the checked edit, or None when it breaks §9.5; the number of prediction task ids dropped
    because they are not development task ids). Content is checked by
    ``policies.validate_content``."""
    if not isinstance(raw, dict):
        return None, 0
    kind, component_id, content = raw.get("kind"), raw.get("component_id"), raw.get("content")
    rationale, hypothesis = raw.get("rationale"), raw.get("hypothesis")
    predictions, risk = raw.get("predictions"), raw.get("risk")
    if (
        kind not in PROPOSER_KINDS
        or not isinstance(component_id, str)
        or not COMPONENT_ID.fullmatch(component_id)
        or not ID.fullmatch(component_id)
        or component_id.split(".", 1)[0] != kind
        or not isinstance(content, dict)
        or not isinstance(rationale, str)
        or len(rationale) > RATIONALE_MAX
        or not isinstance(hypothesis, str)
        or len(hypothesis) > HYPOTHESIS_MAX
        or risk not in RISKS
        or not isinstance(predictions, dict)
        or set(predictions) != set(PREDICTION_FIELDS)
    ):
        return None, 0
    try:
        policies.validate_content(str(kind), content)
    except RuntimeFault:
        return None, 0
    delta = predictions["expected_delta"]
    if not _number(delta) or not -1 <= delta <= 1:
        return None, 0
    dropped = 0
    ids: dict[str, list[str]] = {}
    for name in ("improve_task_ids", "regress_task_ids"):
        value = predictions[name]
        if not isinstance(value, list) or len(value) > MAX_TASK_IDS:
            return None, 0
        if any(not isinstance(i, str) for i in value):
            return None, 0
        unique = list(dict.fromkeys(value))
        ids[name] = [i for i in unique if i in development]
        dropped += len(unique) - len(ids[name])
    improve, regress = (
        _buckets(predictions["improve_buckets"]),
        _buckets(predictions["regress_buckets"]),
    )
    if improve is None or regress is None:
        return None, 0
    edit = {
        "kind": kind,
        "component_id": component_id,
        "content": copy.deepcopy(content),
        "rationale": rationale,
        "hypothesis": hypothesis,
        "predictions": {
            "improve_task_ids": ids["improve_task_ids"],
            "regress_task_ids": ids["regress_task_ids"],
            "improve_buckets": improve,
            "regress_buckets": regress,
            "expected_delta": float(delta),
        },
        "risk": risk,
    }
    return edit, dropped


def _normalized(value: Any) -> Any:
    if isinstance(value, str):
        return " ".join(value.lower().split())
    if isinstance(value, dict):
        return {str(k): _normalized(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalized(v) for v in value]
    return value


def content_digest(content: Any) -> str:
    """The normalized content digest of §9.4 step 3 (case and whitespace folded)."""
    return digest(_normalized(content))


def words(content: Any) -> frozenset[str]:
    return frozenset(WORD.findall(canonical(_normalized(content)).decode()))


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    union = a | b
    return 1.0 if not union else len(a & b) / len(union)


def seen_versions(store: Store, scope: Scope) -> dict[str, list[tuple[str, frozenset[str]]]]:
    """Per kind, (normalized digest, word set) of every existing version: the stored components,
    the stored prompt bundles (as ``role_prompt``) and the v1 content."""
    out: dict[str, list[tuple[str, frozenset[str]]]] = {}

    def add(kind: str, content: Any) -> None:
        out.setdefault(kind, []).append((content_digest(content), words(content)))

    for _ref, value in store.list_objects(scope, ComponentService.KIND):
        if isinstance(value.get("content"), dict):
            add(str(value.get("kind")), value["content"])
    for _ref, value in store.list_objects(scope, "prompt-bundle"):
        if isinstance(value.get("implementer"), list):
            add("role_prompt", {"implementer": value["implementer"]})
    for kind, content in policies.V1.items():
        add(kind, content)
    return out


def is_duplicate(
    edit: dict[str, Any],
    seen: dict[str, list[tuple[str, frozenset[str]]]],
    drafted: set[tuple[str, str]],
) -> bool:
    """§9.4 step 3: the digest of an existing version of the kind or of another draft, or a
    Jaccard similarity >= 0.9 with an existing version of the kind."""
    found, bag = content_digest(edit["content"]), words(edit["content"])
    versions = seen.get(edit["kind"], [])
    return (
        (edit["kind"], found) in drafted
        or any(found == d for d, _ in versions)
        or any(jaccard(bag, w) >= JACCARD_LIMIT for _, w in versions)
    )


# -- inputs (development only) --
def _latest_records(store: Store, scope: Scope, kind: str) -> dict[str, tuple[Ref, dict[str, Any]]]:
    latest: dict[str, tuple[Ref, dict[str, Any]]] = {}
    for ref, value in store.list_objects(scope, kind):
        if ref["id"] not in latest or ref["revision"] > latest[ref["id"]][0]["revision"]:
            latest[ref["id"]] = (ref, value)
    return latest


def development_rows(store: Store, scope: Scope, cell_id: str) -> list[dict[str, Any]]:
    """The cell's ``trial-metrics`` rows with ``split == "development"`` (experiment and
    calibration sources; the filter is on the record field, §2.10), projected on ``ROW_FIELDS``
    plus the composition id of the trial; guard signals are left out (§9.10)."""
    rows = []
    compositions: dict[str, str | None] = {}
    for _ref, value in _latest_records(store, scope, "trial-metrics").values():
        if value.get("split") != "development" or value.get("cell_id") != cell_id:
            continue
        trial = value.get("trial_ref")
        key = digest(trial) if isinstance(trial, dict) else ""
        if key not in compositions:
            compositions[key] = None
            if isinstance(trial, dict):
                with contextlib.suppress(RuntimeFault, KeyError, TypeError):
                    _kind, record = resolve_ref(store, scope, trial)
                    ref = record.get("composition_ref")
                    compositions[key] = str(ref["id"]) if isinstance(ref, dict) else None
        rows.append({**{k: value.get(k) for k in ROW_FIELDS}, "composition_id": compositions[key]})
    return sorted(rows, key=lambda r: (str(r["task_id"]), str(r["arm"]), str(r["composition_id"])))


def _cell_proposals(
    ops: LocalMetaOps, cell_id: str, *, derived: bool
) -> list[tuple[str, dict[str, Any]]]:
    """(proposal id, proposal record) of the cell's proposals: their baseline pins to the cell.
    Derived (IC-19) ablation proposals only when ``derived``."""
    from .stages import is_derived

    out = []
    installed = ops.app.compositions
    for _ref, proposal in ops.store.list_objects(ops.scope, "harness-change-proposal"):
        try:
            cell = releases.pin_allowed(ops.store, ops.scope, installed, proposal["baseline_ref"])
            if cell != cell_id or (not derived and is_derived(ops.store, ops.scope, proposal)):
                continue
        except (RuntimeFault, KeyError, TypeError):
            continue
        out.append((str(proposal["proposal_id"]), proposal))
    return sorted(out, key=lambda row: row[0])


def _stage_run(store: Store, scope: Scope, proposal_id: str) -> dict[str, Any] | None:
    try:
        data: dict[str, Any] = store.head(scope, "stage-run", "stagerun-" + proposal_id)["data"]
    except RuntimeFault as exc:
        if exc.code != "NOT_FOUND":
            raise
        return None
    return data


def refresh_archive(ops: LocalMetaOps, archive: EliteArchive, cell_id: str) -> None:
    """``EliteArchive.update`` for every evaluated proposal of the cell (host code): its stages'
    development ``trial-metrics`` rows and, operator-only, the verdict of its last focused or
    holdout stage. ``StageRunner`` updates the archive after each evaluated stage; this catch-up
    (idempotent) covers stages evaluated before that hook existed."""
    metrics = {
        value["trial_ref"]["id"]: value
        for _ref, value in _latest_records(ops.store, ops.scope, "trial-metrics").values()
        if isinstance(value.get("trial_ref"), dict)
    }
    for proposal_id, proposal in _cell_proposals(ops, cell_id, derived=True):
        run = _stage_run(ops.store, ops.scope, proposal_id)
        if run is None:
            continue
        rows: list[dict[str, Any]] = []
        verdict = None
        evaluated = False
        for stage in STAGE_ORDER:
            entry = (run.get("stages") or {}).get(stage) or {}
            report_ref = entry.get("report_ref")
            if not isinstance(report_ref, dict):
                continue
            evaluated = True
            report = ops.store.get(ops.scope, "eval-report", report_ref)
            rows += [metrics[r["id"]] for r in report["run_refs"] if r["id"] in metrics]
            if stage in VERDICT_STAGES:
                verdict = f"{stage}:{entry.get('decision_class') or entry.get('state')}"
        if evaluated:
            archive.update(
                cell_id,
                composition_ref=proposal["candidate_ref"],
                stage_metrics=rows,
                proposal_id=proposal_id,
                verdict=verdict,
            )


def _scratch_dir(root: Path) -> Path:
    """A new empty directory under ``root``, outside this checkout (IC-14)."""
    root = Path(root).expanduser().absolute()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = Path(tempfile.mkdtemp(prefix="proposer-", dir=root)).resolve()
    if path.is_relative_to(REPO_ROOT.resolve()) or any(path.iterdir()):
        shutil.rmtree(path, ignore_errors=True)
        raise Hold(
            "TURN_FAILED",
            "A proposer turn runs on an empty scratch directory outside the repository (IC-14)",
            details={"root": str(root)},
        )
    return path


def default_scratch_root(ops: LocalMetaOps) -> Path:
    """``<workspace_root>/proposer-scratch`` (under $HOME: colima shares $HOME only), else the
    system temporary directory."""
    dep = getattr(ops, "dep", None)
    config = getattr(dep, "config", None)
    if dep is not None and config is not None and hasattr(dep, "local"):
        return Path(dep.local(config.workspace_root)) / "proposer-scratch"
    return Path(tempfile.gettempdir()) / "amplai-proposer-scratch"


def development_ids(value: Any, development: set[str]) -> list[str]:
    """The development task ids of a stored prediction list, once each, in order (§9.4: the
    proposer's inputs are development data only; an operator prediction is not filtered when it
    is stored)."""
    out: list[str] = []
    for task_id in value if isinstance(value, list) else []:
        if isinstance(task_id, str) and task_id in development and task_id not in out:
            out.append(task_id)
    return out


def leak_check(gate: LeakGate, inputs: dict[str, Any], bodies: dict[str, Any]) -> None:
    """The last line of defence before a proposer or dreaming turn (§9.4, IC-11): the leak gate
    over the whole ``inputs.json`` and every trace body the scratch directory will hold. Hold
    LEAK_GATE before any turn runs; the details name the token kind and the place, never the
    token."""
    hits = gate.scan({"inputs.json": inputs, "traces": bodies})
    if hits:
        raise Hold(
            "LEAK_GATE",
            "The proposer inputs name validation or holdout task material",
            details=[{"kind": h.kind, "where": h.where} for h in hits],
        )


def _write_inputs(scratch: Path, inputs: dict[str, Any], bodies: dict[str, Any]) -> None:
    (scratch / "traces").mkdir(mode=0o700)
    for trace_id, body in bodies.items():
        (scratch / "traces" / f"{trace_id}.json").write_text(json.dumps(body, indent=1))
    (scratch / "inputs.json").write_text(json.dumps(inputs, indent=1, sort_keys=True))


def _usage(results: list[TurnResult]) -> dict[str, Any]:
    """The turns' usage: per turn as reported, and token totals when every turn reported them."""
    each = [r.usage if isinstance(r.usage, dict) else None for r in results]

    def total(name: str) -> int | None:
        values = [u.get(name) if u else None for u in each]
        if not all(type(v) is int for v in values):
            return None
        return sum(int(v) for v in values if v is not None)

    return {
        "turns": len(results),
        "input_tokens": total("input_tokens"),
        "output_tokens": total("output_tokens"),
        "per_turn": each,
    }


def outcome_label(outcome: bool | None) -> str:
    return "unknown" if outcome is None else "passed" if outcome else "failed"


def _observation(ops: LocalMetaOps, issue: str, trace_ids: list[str], score_refs: list[Ref]) -> Ref:
    """A ``harness-observation`` whose artifact is ``{"issue", "trace_ids", "score_refs"}``
    (§2.12; worker trust: it is the proposer's reading of traces)."""
    artifact = ops.dep.artifacts.admit(
        ops.scope,
        canonical({"issue": issue[:4000], "trace_ids": trace_ids, "score_refs": score_refs}),
        "application/json",
        trust="worker",
    )
    observation_id = new_id("observation")
    ref: Ref = put_record(
        ops.store,
        ops.scope,
        ops.dep.contracts,
        OBSERVATION_KIND,
        observation_id,
        {"observation_id": observation_id, "artifact": artifact},
    )
    return ref


def _champion(ops: LocalMetaOps, cell_id: str) -> Ref:
    effective = releases.effective(ops.store, ops.scope, ops.app.compositions)
    if cell_id not in effective:
        raise Hold("CELL_UNKNOWN", "Not an installed cell of the app", details=[cell_id])
    return dict(effective[cell_id])


def _release_ref(ops: LocalMetaOps) -> Ref | None:
    try:
        head = ops.store.head(ops.scope, releases.POINTER_KIND, releases.POINTER_ID)
    except RuntimeFault as exc:
        if exc.code != "NOT_FOUND":
            raise
        return None
    ref = head["data"].get("release_ref")
    return dict(ref) if isinstance(ref, dict) else None


def _slot_content(ops: LocalMetaOps, ref: Ref | None) -> dict[str, Any] | None:
    """What a manifest slot holds: a component's id, version and content, or a prompt bundle's
    role lines."""
    if ref is None:
        return None
    kind, value = resolve_ref(ops.store, ops.scope, ref)
    if kind == ComponentService.KIND:
        return {
            "component_id": value.get("component_id"),
            "version": value.get("version"),
            "content": value.get("content"),
        }
    if kind == "prompt-bundle":
        return {"bundle_id": value.get("bundle_id"), "implementer": value.get("implementer")}
    return {"kind": kind}


def manifest_contents(ops: LocalMetaOps, composition_ref: Ref) -> dict[str, Any]:
    """``"<kind>[.<layer>]"`` -> what that slot of the composition holds."""
    flat = ops.manifests.of_composition(composition_ref).flat()
    return {name: _slot_content(ops, ref) for name, ref in flat.items()}


def development_tasks(ops: LocalMetaOps, rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    """The development tasks of the frozen corpus v2 (task index rows with ``split ==
    "development"``), else those the development rows name."""
    from . import corpus_v2

    try:
        index_ref = ops.frozen_corpus()["task_index_ref"]
        found = [
            {"task_id": str(r["case_id"]), "domain": str(r["domain"])}
            for r in corpus_v2.index_for(ops.operator, ops.store, index_ref)
            if r.get("split") == "development"
        ]
    except (RuntimeFault, KeyError, TypeError):
        found = [
            {"task_id": str(r["task_id"]), "domain": str(r.get("domain") or "")}
            for r in rows
            if r.get("task_id")
        ]
    unique = {t["task_id"]: t for t in found}
    return [unique[k] for k in sorted(unique)]


def _proposer_score(
    store: Store, scope: Scope, proposal_id: str
) -> tuple[Ref, dict[str, Any]] | None:
    rows = _latest_records(store, scope, SCORE_KIND)
    return rows.get(f"predscore-{proposal_id}-{PROPOSER_SCORE_STAGE}")


def _prediction(store: Store, scope: Scope, proposal_id: str) -> dict[str, Any] | None:
    found = _latest_records(store, scope, PREDICTION_KIND).get("pred-" + proposal_id)
    return found[1] if found else None


# -- the ensemble --
class ProposerEnsemble:
    def __init__(
        self,
        ops: LocalMetaOps,
        *,
        breadth: ReadOnlyTurn,
        depth: ReadOnlyTurn,
        traces: TraceService,
        archive: EliteArchive,
        leak_gate: LeakGate,
        components: ComponentService,
        scratch_root: Path | None = None,
    ) -> None:
        """``breadth`` = the cheap cell's read-only turn (``roles.proposer[0]``), ``depth`` =
        the strong cell's (``roles.proposer[1]``); ``leak_gate`` is built by host code with a
        non-proposer actor (§3.11)."""
        self.ops, self.breadth, self.depth = ops, breadth, depth
        self.traces, self.archive, self.leak_gate = traces, archive, leak_gate
        self.components = components
        self.store, self.scope = ops.store, ops.scope
        self.proposer = ops.local.proposer
        self.scratch_root = scratch_root
        self.last: dict[str, Any] | None = None  # the last run's summary (for the command)

    # -- inputs --
    def _traces(self, cell_id: str) -> tuple[list[dict[str, Any]], dict[str, Any], list[Ref], int]:
        """Up to 12 development traces of the cell, failures first, each cut to 32 KiB, and the
        number left out because the leak gate hits their listing entry or shown copy."""
        refs = self.traces.list(self.proposer, cell_id=cell_id, split="development")
        records = [(ref, self.store.get(self.scope, TRACE_KIND, ref)) for ref in refs]
        outcomes = self.traces.outcomes([r for _, r in records])
        rank = {False: 0, None: 1, True: 2}
        ordered = sorted(
            enumerate(records), key=lambda item: (rank[outcomes.get(item[1][1]["run_id"])], item[0])
        )
        listing: list[dict[str, Any]] = []
        bodies: dict[str, Any] = {}
        shown: list[Ref] = []
        left_out = 0
        for _, (ref, record) in ordered:
            if len(shown) == MAX_TRACES:
                break
            read = self.traces.read(self.proposer, ref)
            trace_id = str(ref["id"])
            body = cut_copy(read["body"], TRACE_INPUT_BYTES)
            entry = {
                "trace_id": trace_id,
                "task_id": record["task_id"],
                "arm": record.get("arm"),
                "outcome": outcome_label(outcomes.get(record["run_id"])),
                "file": f"traces/{trace_id}.json",
            }
            if self.leak_gate.scan([entry, body]):
                left_out += 1
                continue
            bodies[trace_id] = body
            listing.append(entry)
            shown.append(dict(ref))
        return listing, bodies, shown, left_out

    def _earlier(
        self, cell_id: str, development: set[str]
    ) -> tuple[list[dict[str, Any]], list[Ref], int]:
        """Earlier proposals of the cell with their screening-stage prediction scores only
        (§9.4); a screening score not written yet is written first. A prediction keeps its
        ``development`` task ids only (an operator prediction is stored unfiltered), and a
        proposal whose entry the leak gate hits is left out (the count is returned)."""
        out, score_refs = [], []
        left_out = 0
        for proposal_id, proposal in _cell_proposals(self.ops, cell_id, derived=False):
            run = _stage_run(self.store, self.scope, proposal_id) or {}
            screening = (run.get("stages") or {}).get(PROPOSER_SCORE_STAGE) or {}
            prediction = _prediction(self.store, self.scope, proposal_id)
            score = _proposer_score(self.store, self.scope, proposal_id)
            if score is None and prediction is not None and screening.get("report_ref"):
                with contextlib.suppress(RuntimeFault):
                    score_predictions(self.store, self.scope, proposal_id, PROPOSER_SCORE_STAGE)
                    score = _proposer_score(self.store, self.scope, proposal_id)
            changes: list[dict[str, Any]] = []
            with contextlib.suppress(RuntimeFault, ValueError, KeyError, TypeError):
                change = json.loads(
                    self.ops.dep.artifacts.read(self.scope, proposal["change_artifact"])
                )
                changes = [
                    {"kind": c.get("kind"), "component_id": c.get("component_id")}
                    for c in change.get("component_changes") or []
                ]
            shown_prediction = None
            if prediction:
                shown_prediction = {k: prediction.get(k) for k in (*PREDICTION_FIELDS, "risk")}
                for name in ("improve_task_ids", "regress_task_ids"):
                    shown_prediction[name] = development_ids(shown_prediction[name], development)
            entry = {
                "proposal_id": proposal_id,
                "hypothesis": proposal.get("hypothesis"),
                "component_changes": changes,
                "prediction": shown_prediction,
                "screening_score": {k: score[1].get(k) for k in ("task_level", "scored_at")}
                if score
                else None,
            }
            if self.leak_gate.scan(entry):
                left_out += 1
                continue
            out.append(entry)
            if score is not None:
                score_refs.append(dict(score[0]))
        return out, score_refs, left_out

    def _inputs(
        self, cell_id: str
    ) -> tuple[dict[str, Any], dict[str, Any], list[Ref], list[Ref], dict[str, int]]:
        """(inputs.json, trace bodies, trace refs shown, score refs shown, entries left out by
        the leak gate); Hold LEAK_GATE when the leak gate still hits the assembled inputs."""
        champion = _champion(self.ops, cell_id)
        self.archive.set_champion(
            cell_id, composition_ref=champion, release_ref=_release_ref(self.ops)
        )
        refresh_archive(self.ops, self.archive, cell_id)
        rows = development_rows(self.store, self.scope, cell_id)
        tasks = development_tasks(self.ops, rows)
        listing, bodies, trace_refs, traces_left_out = self._traces(cell_id)
        proposals, score_refs, proposals_left_out = self._earlier(
            cell_id, {t["task_id"] for t in tasks}
        )
        parents = [
            {
                "composition_ref": ref,
                "composition_id": ref["id"],
                "manifest": manifest_contents(self.ops, ref),
            }
            for ref in self.archive.parents(cell_id, k=PARENTS)
        ]
        inputs = {
            "cell_id": cell_id,
            "catalogue": {
                kind: {
                    "layer": KINDS[kind].layer,
                    "surface_class": KINDS[kind].surface_class,
                    "v1": policies.V1.get(kind),
                }
                for kind in PROPOSER_KINDS
            },
            "champion": {
                "composition_ref": champion,
                "composition_id": champion["id"],
                "manifest": manifest_contents(self.ops, champion),
            },
            "development_tasks": tasks,
            "metrics": rows,
            "traces": listing,
            "archive": self.archive.proposer_view(cell_id),
            "parents": parents,
            "proposals": proposals,
        }
        leak_check(self.leak_gate, inputs, bodies)
        left_out = {"traces": traces_left_out, "proposals": proposals_left_out}
        return inputs, bodies, trace_refs, score_refs, left_out

    def inputs(self, cell_id: str) -> dict[str, Any]:
        """What the proposer turns read as ``inputs.json`` (development data only, §9.4)."""
        return self._inputs(cell_id)[0]

    # -- one run --
    @staticmethod
    def breadth_prompt(drafts: int) -> str:
        return (
            "You improve the AMPLAI execution harness of one cell by proposing edits to its "
            "components.\nThe current directory holds your only inputs (read-only): inputs.json "
            "(the component catalogue with each kind's v1 content, the champion's components, "
            "the development tasks, per-task development results, the elite archive and its "
            "parents, earlier proposals with their screening scores) and traces/*.json "
            "(sanitized development traces, failures first).\n"
            f"Propose at most {drafts} edits. Each edit gives one component of the champion new "
            'content of the same kind: kind (a catalogue kind), component_id "<kind>.<name>", '
            "content valid for that kind (shaped like the champion's or the v1 content), "
            f"rationale (at most {RATIONALE_MAX} characters), hypothesis (at most "
            f"{HYPOTHESIS_MAX} characters), predictions and risk (low, medium or high).\n"
            f"predictions: improve_task_ids and regress_task_ids name at most {MAX_TASK_IDS} "
            "development task ids each (development_tasks); improve_buckets and regress_buckets "
            "name groups of unseen tasks by domain and/or task_class; expected_delta is the "
            "expected change of the success rate, from -1 to 1.\nReturn only the JSON object."
        )

    @staticmethod
    def depth_prompt(draft: dict[str, Any]) -> str:
        return (
            "You refine one draft edit of the AMPLAI execution harness. The current directory "
            "holds the same read-only inputs the draft was made from (inputs.json, "
            "traces/*.json).\nDraft:\n" + json.dumps(draft, indent=1, sort_keys=True) + "\n"
            "Return exactly one edit of the same kind, improved where the inputs show how; keep "
            "every field and limit of the draft's format. Return only the JSON object."
        )

    def _checked(
        self, edit: dict[str, Any], seen: Any, drafted: set[tuple[str, str]], counts: dict[str, int]
    ) -> bool:
        if is_duplicate(edit, seen, drafted):
            counts["deduplicated"] += 1
            return False
        if self.leak_gate.scan(edit):
            counts["leak_refused"] += 1
            return False
        return True

    def run(self, *, cell_id: str, drafts: int = 6, refine: int = 2) -> Ref:
        """Breadth -> parse -> dedup -> leak gate -> depth -> submit; the ``proposer-run``
        record (§2.12).

        A submitted proposal's change artifact names its run (``proposer_run_ref``) and the leak
        scan of its content (``leak_scan``: hits 0 and the leak index; a hit drops the edit).
        A change artifact is written before the run knows which proposals it submitted, so a run
        that submits writes two revisions of ``proprun-<uuid>``: revision 1 just before the
        first submit (``submitted`` empty; the change artifacts name it) and revision 2 with the
        submitted proposal ids, which ``run`` returns. A run that submits nothing writes one."""
        if type(drafts) is not int or not 1 <= drafts <= MAX_EDITS:
            raise RuntimeFault("PROPOSER_RUN", f"drafts is 1..{MAX_EDITS}")
        if type(refine) is not int or not 0 <= refine <= drafts:
            raise RuntimeFault("PROPOSER_RUN", "refine is 0..drafts")
        inputs, bodies, trace_refs, score_refs, left_out = self._inputs(cell_id)
        archive_ref = self.archive.head_ref(cell_id)
        run_id = new_id("proprun")
        development = {t["task_id"] for t in inputs["development_tasks"]}
        champion = inputs["champion"]["composition_ref"]
        counts = {"drafted": 0, "deduplicated": 0, "leak_refused": 0, "refined": 0}
        results: list[TurnResult] = []
        submitted: list[str] = []
        failures: list[dict[str, Any]] = []
        scratch = _scratch_dir(self.scratch_root or default_scratch_root(self.ops))
        try:
            _write_inputs(scratch, inputs, bodies)
            result = self.breadth.run(
                prompt=self.breadth_prompt(drafts), schema=PROPOSAL_SCHEMA, workspace=scratch
            )
            results.append(result)
            raw = result.output.get("edits")
            raw_edits = raw[:drafts] if isinstance(raw, list) else []
            counts["drafted"] = len(raw_edits)
            seen = seen_versions(self.store, self.scope)
            drafted: set[tuple[str, str]] = set()
            candidates: list[tuple[int, dict[str, Any], int]] = []
            for index, item in enumerate(raw_edits):
                edit, dropped = parse_edit(item, development=development)
                if edit is None or not self._checked(edit, seen, drafted, counts):
                    continue
                drafted.add((edit["kind"], content_digest(edit["content"])))
                candidates.append((index, edit, dropped))
            ranked = sorted(
                candidates,
                key=lambda c: (
                    -c[1]["predictions"]["expected_delta"],
                    RISKS.index(c[1]["risk"]),
                    c[0],
                ),
            )
            refined: list[tuple[dict[str, Any], int]] = []
            final: set[tuple[str, str]] = set()  # two refinements never submit one content twice
            for _index, draft, dropped in ranked[:refine]:
                try:
                    turn = self.depth.run(
                        prompt=self.depth_prompt(draft), schema=PROPOSAL_SCHEMA, workspace=scratch
                    )
                except RuntimeFault as exc:  # a failed refinement is not submitted
                    failures.append({"component_id": draft["component_id"], "code": exc.code})
                    continue
                results.append(turn)
                out = turn.output.get("edits")
                edit, more = (
                    parse_edit(out[0], development=development)
                    if isinstance(out, list) and len(out) == 1
                    else (None, 0)
                )
                if edit is None or edit["kind"] != draft["kind"]:
                    failures.append({"component_id": draft["component_id"], "code": "TURN_OUTPUT"})
                    continue
                counts["refined"] += 1
                if not self._checked(edit, seen, final, counts):
                    continue
                final.add((edit["kind"], content_digest(edit["content"])))
                refined.append((edit, dropped + more))
            if refined:
                observation = _observation(
                    self.ops,
                    f"proposer run on cell {cell_id}: development traces and results",
                    [str(r["id"]) for r in trace_refs],
                    score_refs,
                )
                # revision 1: the run as it stands before its first submit (module docstring)
                run_ref = self._write_run(
                    run_id, 1, counts, [], trace_refs, score_refs, archive_ref, results
                )
                for edit, dropped in refined:
                    try:
                        submitted.append(
                            self._submit(cell_id, champion, edit, dropped, observation, run_ref)
                        )
                    except RuntimeFault as exc:
                        failures.append({"component_id": edit["component_id"], "code": exc.code})
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        ref = self._write_run(
            run_id, 2 if refined else 1, counts, submitted, trace_refs, score_refs, archive_ref,
            results,
        )  # fmt: skip
        self.last = {
            "proposer_run_ref": ref,
            **counts,
            "submitted": submitted,
            "failures": failures,
            "inputs_left_out": left_out,  # leak gate hits on earlier proposals and traces
        }
        return ref

    def _write_run(
        self,
        run_id: str,
        revision: int,
        counts: dict[str, int],
        submitted: list[str],
        trace_refs: list[Ref],
        score_refs: list[Ref],
        archive_ref: Ref | None,
        results: list[TurnResult],
    ) -> Ref:
        value = {
            "schema": RUN_SCHEMA,
            "scope": self.scope.wire(),
            "cell_breadth": str(self.breadth.cell_id),
            "cell_depth": str(self.depth.cell_id),
            **counts,
            "submitted": list(submitted),
            "inputs": {
                "trace_refs": trace_refs,
                "score_refs": score_refs,
                "archive_ref": archive_ref,
            },
            "usage": _usage(results),
            "at": now(),
        }
        validate_run(value)
        with self.store.tx() as db:
            ref: Ref = self.store.put(db, self.scope, RUN_KIND, run_id, revision, value)
        return ref

    def _submit(
        self,
        cell_id: str,
        champion: Ref,
        edit: dict[str, Any],
        dropped: int,
        observation: Ref,
        run_ref: Ref,
    ) -> str:
        """Register the component (proposer identity, source ``proposer``) and submit the
        candidate with its prediction, its proposer run and its leak scan (§9.4 step 6, §2.12;
        ``_checked`` dropped an edit with any hit, so a submitted scan has 0 hits)."""
        kind = edit["kind"]
        current = self.ops.manifests.of_composition(champion).flat().get(kind)
        parent = None
        if current is not None:
            with contextlib.suppress(RuntimeFault):
                found, value = resolve_ref(self.store, self.scope, current)
                if (
                    found == ComponentService.KIND
                    and value.get("component_id") == edit["component_id"]
                ):
                    parent = current
        rationale = edit["rationale"].strip() or edit["hypothesis"].strip() or f"{kind} edit"
        if dropped:
            rationale += (
                f" ({dropped} prediction task ids outside the development split were dropped)"
            )
        ref = self.components.register(
            self.proposer,
            component_id=edit["component_id"],
            kind=kind,
            content=edit["content"],
            source="proposer",
            rationale=rationale[:4000],
            parent=parent,
        )
        delta = edit["predictions"]["expected_delta"]
        return self.ops.propose_components(
            cell_id=cell_id,
            changes={kind: ref},
            suffix="prop-" + digest({"cell": cell_id, "component": ref})[7:19],
            hypothesis=edit["hypothesis"].strip() or f"A new {kind} improves the cell",
            expected_benefit=f"expected success delta {delta:+.2f} (proposer prediction)",
            risks=[f"{edit['risk']} risk (proposer estimate)"],
            observation_refs=[observation],
            prediction={**edit["predictions"], "risk": edit["risk"]},
            baseline_ref=champion,
            proposer_run_ref=run_ref,
            leak_scan={
                "hits": len(self.leak_gate.scan(edit)),
                "index_ref": dict(self.leak_gate.index_ref),
            },
        )


def validate_run(value: Any) -> None:
    """The §2.12 ``proposer-run`` shape; RuntimeFault PROPOSER_RUN otherwise."""
    keys = {
        "schema",
        "scope",
        "cell_breadth",
        "cell_depth",
        "drafted",
        "deduplicated",
        "leak_refused",
        "refined",
        "submitted",
        "inputs",
        "usage",
        "at",
    }
    inputs = value.get("inputs") if isinstance(value, dict) else None
    ok = (
        isinstance(value, dict)
        and set(value) == keys
        and value["schema"] == RUN_SCHEMA
        and all(
            type(value[k]) is int and value[k] >= 0
            for k in ("drafted", "deduplicated", "leak_refused", "refined")
        )
        and isinstance(value["submitted"], list)
        and all(isinstance(p, str) for p in value["submitted"])
        and isinstance(inputs, dict)
        and set(inputs) == {"trace_refs", "score_refs", "archive_ref"}
        and isinstance(inputs["trace_refs"], list)
        and isinstance(inputs["score_refs"], list)
        and isinstance(value["usage"], dict)
    )
    if not ok:
        raise RuntimeFault("PROPOSER_RUN", "Invalid proposer-run record (§2.12)")


# -- prediction scoring (§9.6, IC-11) --
def _units(trials: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per task: the domain and each arm's unit result (every repeat known without an unknown
    effect, all passed), None when a repeat is missing (as ``analyze_pairs`` pairs units)."""
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = {}
    domains: dict[str, str] = {}
    for trial in trials:
        if trial.get("arm") not in ("baseline", "candidate"):
            continue
        grouped.setdefault(trial["task_id"], {}).setdefault(trial["arm"], []).append(trial)
        domains.setdefault(trial["task_id"], str(trial.get("task_class") or ""))
    units: dict[str, dict[str, Any]] = {}
    for task, arms in grouped.items():
        unit: dict[str, Any] = {"domain": domains[task]}
        for arm in ("baseline", "candidate"):
            values = arms.get(arm) or []
            complete = bool(values) and all(
                isinstance(v.get("success"), bool) and not v.get("unknown_effects") for v in values
            )
            unit[arm] = all(v["success"] for v in values) if complete else None
        units[task] = unit
    return units


def _pr(predicted: set[str], actual: set[str]) -> dict[str, Any]:
    hits = predicted & actual
    return {
        "precision": round(len(hits) / len(predicted), 4) if predicted else None,
        "recall": round(len(hits) / len(actual), 4) if actual else None,
        "n_pred": len(predicted),
        "n_actual": len(actual),
    }


def task_scores(prediction: dict[str, Any], units: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Task level (screening): precision and recall of the predicted improving and regressing
    development tasks, over the tasks the stage evaluated in both arms."""
    evaluated = {
        t for t, u in units.items() if u["baseline"] is not None and u["candidate"] is not None
    }
    improved = {t for t in evaluated if units[t]["candidate"] and not units[t]["baseline"]}
    regressed = {t for t in evaluated if units[t]["baseline"] and not units[t]["candidate"]}
    return {
        "improve": _pr(set(prediction.get("improve_task_ids") or []) & evaluated, improved),
        "regress": _pr(set(prediction.get("regress_task_ids") or []) & evaluated, regressed),
    }


def bucket_scores(prediction: dict[str, Any], units: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Bucket level (focused, holdout; operator-only): the sign of each domain's success delta
    (candidate minus baseline units) against the predicted domain buckets."""
    by_domain: dict[str, list[dict[str, Any]]] = {}
    for unit in units.values():
        if unit["baseline"] is not None and unit["candidate"] is not None and unit["domain"]:
            by_domain.setdefault(unit["domain"], []).append(unit)
    delta = {
        d: sum(int(u["candidate"]) - int(u["baseline"]) for u in us) / len(us)
        for d, us in by_domain.items()
    }
    unscored = 0

    def predicted(name: str) -> set[str]:
        nonlocal unscored
        out = set()
        for bucket in prediction.get(name) or []:
            if set(bucket) == {"domain"} and bucket["domain"] in delta:
                out.add(bucket["domain"])
            else:
                unscored += 1
        return out

    improve, regress = predicted("improve_buckets"), predicted("regress_buckets")
    return {
        "improve": _pr(improve, {d for d, x in delta.items() if x > 0}),
        "regress": _pr(regress, {d for d, x in delta.items() if x < 0}),
        "buckets": [
            {"bucket": {"domain": d}, "delta": round(delta[d], 4), "n_tasks": len(by_domain[d])}
            for d in sorted(delta)
        ],
        "unscored": unscored,
    }


def score_predictions(store: Store, scope: Scope, proposal_id: str, stage: str) -> Ref:
    """The ``prediction-score`` record ``predscore-<proposal_id>-<stage>`` after an evaluated
    stage (a new revision only when the scores changed). Hold META_STATE without a prediction or
    an evaluated stage."""
    if stage not in SCORED_STAGES:
        raise RuntimeFault("PREDICTION_STAGE", "Scored stages are screening, focused and holdout")
    prediction = _prediction(store, scope, proposal_id)
    if prediction is None:
        raise Hold("META_STATE", "The proposal has no prediction", details=proposal_id)
    run = _stage_run(store, scope, proposal_id) or {}
    entry = (run.get("stages") or {}).get(stage) or {}
    if not isinstance(entry.get("report_ref"), dict):
        raise Hold(
            "META_STATE", f"The {stage} stage of the proposal has no report", details=proposal_id
        )
    report = store.get(scope, "eval-report", entry["report_ref"])
    units = _units([store.get(scope, "eval-trial", ref) for ref in report["run_refs"]])
    screening = stage == PROPOSER_SCORE_STAGE
    value = {
        "schema": SCORE_SCHEMA,
        "scope": scope.wire(),
        "proposal_id": proposal_id,
        "stage": stage,
        "task_level": task_scores(prediction, units) if screening else None,
        "bucket_level": None if screening else bucket_scores(prediction, units),
        "scored_at": now(),
    }
    score_id = f"predscore-{proposal_id}-{stage}"
    latest = _latest_records(store, scope, SCORE_KIND).get(score_id)
    if latest is not None and {k: v for k, v in latest[1].items() if k != "scored_at"} == {
        k: v for k, v in value.items() if k != "scored_at"
    }:
        return latest[0]
    with store.tx() as db:
        ref: Ref = store.put(
            db, scope, SCORE_KIND, score_id, latest[0]["revision"] + 1 if latest else 1, value
        )
    return ref


def score_evaluated(store: Store, scope: Scope, proposal_id: str) -> dict[str, Ref]:
    """Every ``prediction-score`` due for the proposal (§9.6): stage -> ref for each scored stage
    (screening, focused, holdout) whose report is stored; nothing without a prediction. What
    ``amplai meta proposer score`` runs, and the call a stage runner or the nightly runner makes
    after an evaluated stage (that hook is outside S13, reported). Idempotent
    (``score_predictions`` keeps an unchanged score)."""
    if _prediction(store, scope, proposal_id) is None:
        return {}
    stages = (_stage_run(store, scope, proposal_id) or {}).get("stages") or {}
    return {
        stage: score_predictions(store, scope, proposal_id, stage)
        for stage in SCORED_STAGES
        if isinstance((stages.get(stage) or {}).get("report_ref"), dict)
    }


# -- dreaming (§9.7) --
def _night(value: Any) -> str:
    try:
        if isinstance(value, str) and NIGHT.fullmatch(value):
            return date.fromisoformat(value).isoformat()
    except ValueError:
        pass
    raise RuntimeFault("DREAM_NIGHT", "The night is a date YYYY-MM-DD")


def apply_deltas(
    notes: list[dict[str, Any]],
    deltas: list[Any],
    *,
    night: str,
    shown: set[str],
    apps: set[str],
) -> tuple[list[dict[str, Any]], int]:
    """(the notes after the deltas, the number of deltas refused). ``add`` appends a note;
    ``merge`` replaces its target notes by one; ``delete`` removes its targets. Targets index the
    notes before any delta, and one note is the target of one delta at most. A delta is refused
    when its evidence (1..8) is not among the traces shown, its app is not installed, its task
    class is unknown, its text is empty or longer than 300 characters (add, merge), or its
    targets are invalid. New and merged notes are dated with the night (an absolute date)."""
    removed: set[int] = set()
    claimed: set[int] = set()
    merged: list[dict[str, Any]] = []
    added: list[dict[str, Any]] = []
    refused = 0
    for delta in deltas[:DREAM_MAX_DELTAS]:
        if not isinstance(delta, dict):
            refused += 1
            continue
        op, targets = delta.get("op"), delta.get("targets")
        evidence = delta.get("evidence")
        ok = (
            op in DREAM_OPS
            and isinstance(targets, list)
            and all(type(t) is int for t in targets)
            and isinstance(evidence, list)
            and all(isinstance(e, str) for e in evidence)
            and 1 <= len(set(evidence)) <= EVIDENCE_MAX
            and all(e in shown for e in evidence)
        )
        unique = list(dict.fromkeys(targets)) if ok and isinstance(targets, list) else []
        if ok and op == "add":
            ok = not unique
        elif ok:
            ok = bool(unique) and all(0 <= t < len(notes) and t not in claimed for t in unique)
        if ok and op in ("add", "merge"):
            text, app, task_class = delta.get("text"), delta.get("app"), delta.get("task_class")
            ok = (
                isinstance(text, str)
                and bool(text.strip())
                and len(text.strip()) <= NOTE_TEXT_MAX
                and isinstance(app, str)
                and app in apps
                and (task_class is None or task_class in TASK_CLASSES)
            )
        if not ok:
            refused += 1
            continue
        claimed |= set(unique)
        if op in ("merge", "delete"):
            removed |= set(unique)
        if op in ("add", "merge"):
            note = {
                "text": str(delta["text"]).strip(),
                "app": delta["app"],
                "task_class": delta["task_class"],
                "evidence": list(dict.fromkeys(evidence or [])),
                "dated": night,
            }
            (merged if op == "merge" else added).append(note)
    refused += max(0, len(deltas) - DREAM_MAX_DELTAS)
    kept = [copy.deepcopy(n) for i, n in enumerate(notes) if i not in removed]
    return kept + merged + added, refused


def _leak_gate(ops: LocalMetaOps) -> LeakGate:
    """The leak gate of the frozen corpus, opened by the host-side leak reader (§3.11)."""
    from .leak_gate import LeakGate

    return LeakGate(ops.local.leak_reader, ops.store, ops.frozen_corpus()["leak_index_ref"])


def dream(
    ops: LocalMetaOps,
    *,
    cell_id: str,
    night: str,
    turn: ReadOnlyTurn,
    traces: TraceService | None = None,
    leak_gate: LeakGate | None = None,
    trace_refs: list[Ref] | None = None,
    scratch_root: Path | None = None,
) -> str | None:
    """The dreaming job of one night for one cell (§9.7): the proposal id of the class-A
    ``memory_notes`` candidate, or None when there is nothing to consolidate (no development
    trace of the night, no accepted delta, no change). The night's traces are the cell's
    development traces captured on that UTC date unless ``trace_refs`` names them (the nightly
    runner, S12); a trace the leak gate hits is left out. Hold LEAK_GATE before the turn when
    the leak gate still hits the inputs, and when the new notes name validation or holdout
    material; RuntimeFault COMPONENT_CONTENT when they break the ``memory_notes`` rules (≤ 40 per
    (app, task class), every dreaming note cites a trace)."""
    night = _night(night)
    service = traces or TraceService(ops.store, ops.scope, ops.dep.artifacts)
    proposer = ops.local.proposer
    listed = service.list(proposer, cell_id=cell_id, split="development")
    if trace_refs is not None:
        wanted = {digest(dict(r)) for r in trace_refs}
        refs = [r for r in listed if digest(dict(r)) in wanted]
    else:
        refs = [
            r
            for r in listed
            if str(ops.store.get(ops.scope, TRACE_KIND, r).get("captured_at", ""))[:10] == night
        ]
    if not refs:
        return None
    records = [ops.store.get(ops.scope, TRACE_KIND, r) for r in refs]
    outcomes = service.outcomes(records)
    champion = _champion(ops, cell_id)
    current_ref = ops.manifests.of_composition(champion).context.get("memory_notes")
    current_value = (
        ops.store.get(ops.scope, ComponentService.KIND, current_ref) if current_ref else None
    )
    current = copy.deepcopy(
        current_value["content"] if current_value else policies.V1["memory_notes"]
    )
    apps = set(ops.dep.service.apps)
    gate = leak_gate or _leak_gate(ops)
    # a trace whose listing entry or shown copy the leak gate hits is left out (as in the
    # proposer's inputs); nothing left means nothing to consolidate
    listing: list[dict[str, Any]] = []
    bodies: dict[str, Any] = {}
    shown_refs: list[Ref] = []
    for ref, rec in zip(refs, records, strict=True):
        body = cut_copy(service.read(proposer, ref)["body"], TRACE_INPUT_BYTES)
        entry = {
            "trace_id": str(ref["id"]),
            "task_id": rec["task_id"],
            "arm": rec.get("arm"),
            "outcome": outcome_label(outcomes.get(rec["run_id"])),
            "file": f"traces/{ref['id']}.json",
        }
        if gate.scan([entry, body]):
            continue
        listing.append(entry)
        bodies[str(ref["id"])] = body
        shown_refs.append(ref)
    if not shown_refs:
        return None
    refs = shown_refs
    inputs = {
        "night": night,
        "cell_id": cell_id,
        "apps": sorted(apps),
        "task_classes": list(TASK_CLASSES),
        "memory_notes": {
            "enabled": current["enabled"],
            "notes": [{"index": i, **n} for i, n in enumerate(current["notes"])],
        },
        "traces": listing,
    }
    leak_check(gate, inputs, bodies)
    prompt = (
        f"You consolidate the memory notes of one AMPLAI harness cell after the night {night}. "
        "The current directory holds your only inputs (read-only): inputs.json (the current "
        "notes with their indices, the installed apps, the task classes and the night's "
        "development traces with their outcomes) and traces/*.json (the sanitized traces).\n"
        f"Propose at most {DREAM_MAX_DELTAS} deltas: add (a new note; targets empty), merge "
        "(replace the notes at targets by one note) or delete (remove the notes at targets). "
        f"A note's text is at most {NOTE_TEXT_MAX} characters of advice for future tasks of "
        "its app and task class (null for every class); evidence lists 1 to "
        f"{EVIDENCE_MAX} trace ids from inputs.json that support the delta.\n"
        "Return only the JSON object."
    )
    scratch = _scratch_dir(scratch_root or default_scratch_root(ops))
    try:
        _write_inputs(scratch, inputs, bodies)
        result = turn.run(prompt=prompt, schema=DREAM_SCHEMA, workspace=scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    deltas = result.output.get("deltas")
    notes, refused = apply_deltas(
        current["notes"],
        deltas if isinstance(deltas, list) else [],
        night=night,
        shown={str(r["id"]) for r in refs},
        apps=apps,
    )
    content = {"enabled": bool(notes), "notes": notes}
    if content == {"enabled": current["enabled"], "notes": current["notes"]}:
        return None
    policies.validate_content("memory_notes", content)
    hits = gate.scan(content)
    if hits:
        raise Hold(
            "LEAK_GATE",
            "The dreamed notes name validation or holdout task material",
            details=[{"kind": h.kind, "where": h.where} for h in hits],
        )
    component_id = "memory_notes.dream." + cell_id
    same = current_value is not None and current_value.get("component_id") == component_id
    accepted = len(deltas if isinstance(deltas, list) else []) - refused
    ref = ComponentService(ops.store, ops.scope).register(
        proposer,
        component_id=component_id,
        kind="memory_notes",
        content=content,
        source="dreaming",
        rationale=(
            f"dreaming job of the night {night} for cell {cell_id}: {accepted} deltas "
            f"applied, {refused} refused"
        ),
        parent=current_ref if same else None,
        merge_parents=[current_ref] if current_ref and not same else [],
    )
    observation = _observation(
        ops,
        f"dreaming job of the night {night} on cell {cell_id}",
        [str(r["id"]) for r in refs],
        [],
    )
    return ops.propose_components(
        cell_id=cell_id,
        changes={"memory_notes": ref},
        suffix=f"dream-{night}-" + digest(ref)[7:15],
        hypothesis=f"Consolidated memory notes of the night {night} help later tasks of the cell",
        expected_benefit="fewer repeated failures on tasks the notes cover (dreaming job, §9.7)",
        risks=["notes distilled from development traces may not generalize"],
        observation_refs=[observation],
        prediction=None,
        baseline_ref=champion,
    )


# -- removal sweep (§9.8) --
def _content_of(ops: LocalMetaOps, ref: Ref | None) -> Any:
    if ref is None:
        return None
    kind, value = resolve_ref(ops.store, ops.scope, ref)
    if kind == ComponentService.KIND:
        return value.get("content")
    if kind == "prompt-bundle":
        return {"implementer": value.get("implementer")}
    return {"kind": kind, "ref": ref}


def v1_content(slot: str) -> dict[str, Any] | None:
    """The v1 content of a manifest slot (IC-24: ``policies.V1``); None for a kind without v1
    content (``driver_options``: argv unchanged; deciders: absent = the v1 prior, §2.2), whose v1
    is the empty slot."""
    from .manifest import DECIDERS, slot_kind

    if slot in DECIDERS:
        return None
    content = policies.V1.get(slot_kind(slot))
    return copy.deepcopy(content) if content is not None else None


def removal_targets(ops: LocalMetaOps, cell_id: str) -> list[dict[str, Any]]:
    """The champion's slots whose content differs from its v1 content (IC-24, ``policies.V1``;
    an empty slot for a kind without v1 content), in manifest slot order: ``{"slot", "kind",
    "from" (the champion's ref), "v1" (the v1 content, None = the empty slot)}``."""
    from .manifest import slot_kind
    from .stages import ALL_SLOTS, _slots

    champion = _champion(ops, cell_id)
    if ops.app.compositions.get(cell_id) is None:
        raise Hold("CELL_UNKNOWN", "Not an installed cell of the app", details=[cell_id])
    slots = _slots(ops.manifests.of_composition(champion))
    out = []
    for slot in ALL_SLOTS:
        current, v1 = slots.get(slot), v1_content(slot)
        if (current is None) if v1 is None else _content_of(ops, current) == v1:
            continue
        out.append({"slot": slot, "kind": slot_kind(slot), "from": current, "v1": v1})
    return out


def _v1_ref(ops: LocalMetaOps, kind: str, content: dict[str, Any]) -> Ref:
    """A component holding the v1 content: the install's ``<kind>.baseline`` version with that
    content when there is one, else ``<kind>.v1`` registered by the proposer identity (source
    ``sweep``; identical content returns the existing version)."""
    found = ops.components.find(f"{kind}.baseline", content)
    if found is not None:
        return found
    return ops.components.register(
        ops.local.proposer,
        component_id=f"{kind}.v1",
        kind=kind,
        content=content,
        source="sweep",
        rationale="v1 content (policies.V1) for a removal sweep (IC-24, interfaces.md §9.8)",
    )


def _open_candidate(ops: LocalMetaOps, composition_id: str) -> bool:
    """An open proposal (not promoted, rejected, rolled back or aborted) already has this
    candidate composition."""
    for _ref, proposal in ops.store.list_objects(ops.scope, "harness-change-proposal"):
        if proposal.get("candidate_ref", {}).get("id") != composition_id:
            continue
        try:
            state = ops.store.head(ops.scope, "evolution", proposal["proposal_id"])["state"]
        except RuntimeFault:
            continue
        if state not in TERMINAL_STATES:
            return True
    return False


def _tokens_per_solved(trials: list[dict[str, Any]], arm: str) -> float | None:
    """Input plus output tokens of the arm's trials per solved trial; None without a solved
    trial or with a trial of unknown usage (unknown is never "fewer")."""
    rows = [t for t in trials if t.get("arm") == arm]
    solved = sum(t.get("success") is True for t in rows)
    if not solved or any(
        type(t.get("input_tokens")) is not int or type(t.get("output_tokens")) is not int
        for t in rows
    ):
        return None
    spent = sum(int(t["input_tokens"]) + int(t["output_tokens"]) for t in rows)
    return round(spent / solved, 1)


def removal_verdict(store: Store, scope: Scope, proposal_id: str) -> dict[str, Any]:
    """The §9.8 removal criterion of a sweep proposal (baseline = the champion, candidate = the
    champion with one component back to v1), read after its focused stage: decision class
    ``efficiency``, or ``non_inferior`` with fewer tokens per solved task in the candidate arm.
    ``removal_candidate`` is None until the focused stage has a report. Tokens per solved task
    are the input plus output tokens of the focused report's trials of each arm per solved trial
    (the report has no per-arm summary record). Reading it changes nothing; the IC-24 removal
    gate (``stages.check_removal``) consumes it."""
    stages = (_stage_run(store, scope, proposal_id) or {}).get("stages") or {}
    entry = stages.get(REMOVAL_STAGE) or {}
    decision = entry.get("decision_class")
    out: dict[str, Any] = {
        "proposal_id": proposal_id,
        "stage": REMOVAL_STAGE,
        "decision_class": decision,
        "tokens_per_solved": {"baseline": None, "candidate": None},
        "removal_candidate": None,
    }
    if not isinstance(entry.get("report_ref"), dict):
        return out
    report = store.get(scope, "eval-report", entry["report_ref"])
    trials = [store.get(scope, "eval-trial", ref) for ref in report["run_refs"]]
    per = {arm: _tokens_per_solved(trials, arm) for arm in ("baseline", "candidate")}
    base, cand = per["baseline"], per["candidate"]
    cheaper = base is not None and cand is not None and cand < base
    out["tokens_per_solved"] = per
    out["removal_candidate"] = decision == "efficiency" or (decision == "non_inferior" and cheaper)
    return out


def sweep_proposals(ops: LocalMetaOps, cell_id: str) -> list[dict[str, Any]]:
    """The open sweep proposals of the cell's current champion (candidate composition
    ``<champion id>__sweep-…``) with their state and §9.8 ``removal_verdict`` (what the IC-24
    removal gate reads)."""
    champion = _champion(ops, cell_id)
    champion_id = str(ops.store.get(ops.scope, "harness-composition", champion)["composition_id"])
    prefix = f"{champion_id}{releases.CANDIDATE_SEP}sweep-"
    found: dict[str, dict[str, Any]] = {}
    for _ref, proposal in ops.store.list_objects(ops.scope, "harness-change-proposal"):
        candidate = proposal.get("candidate_ref") or {}
        if not str(candidate.get("id") or "").startswith(prefix):
            continue
        proposal_id = str(proposal["proposal_id"])
        try:
            state = ops.store.head(ops.scope, "evolution", proposal_id)["state"]
        except RuntimeFault:
            continue
        if state in TERMINAL_STATES:
            continue
        found[proposal_id] = {
            "proposal_id": proposal_id,
            "state": state,
            "removal": removal_verdict(ops.store, ops.scope, proposal_id),
        }
    return [found[k] for k in sorted(found)]


def removal_sweep(ops: LocalMetaOps, *, cell_id: str, reason: str) -> list[str]:
    """IC-24 (provisional): one ordinary draft proposal per non-v1 component of the champion
    (origin ``removal_sweep``; baseline = the champion, candidate = the champion with that
    component back to its v1 content, ``policies.V1``), reviewed and screened like any proposal;
    the removal gate (``stages.check_removal``, Hold NOT_A_REMOVAL) holds its holdout stage and
    approvals unless its focused stage shows a removal. Proposal ids, in slot order. A slot whose
    variant cannot be built (e.g. a combination rule, ``MANIFEST_COMBINATION``, or a v1 limit
    above the deployment ceiling) is skipped; an identical open sweep proposal is not
    repeated."""
    if not isinstance(reason, str) or not reason.strip():
        raise RuntimeFault("SWEEP_REASON", "A removal sweep states why it runs")
    from .stages import REMOVAL_ORIGIN, component_name

    champion = _champion(ops, cell_id)
    champion_id = str(ops.store.get(ops.scope, "harness-composition", champion)["composition_id"])
    out: list[str] = []
    observation: Ref | None = None
    for target in removal_targets(ops, cell_id):
        suffix = (
            "sweep-"
            + digest({"champion": champion, "slot": target["slot"], "reason": reason.strip()})[7:19]
        )
        if _open_candidate(ops, f"{champion_id}{releases.CANDIDATE_SEP}{suffix}"):
            continue
        if observation is None:
            observation = _observation(ops, f"removal sweep: {reason.strip()}", [], [])
        name = component_name(target["slot"])
        try:
            to = _v1_ref(ops, target["kind"], target["v1"]) if target["v1"] is not None else None
            out.append(
                ops.propose_components(
                    cell_id=cell_id,
                    changes={target["slot"]: to},
                    suffix=suffix,
                    hypothesis=(
                        f"Removal sweep ({reason.strip()}): {name} back to v1 is "
                        "non-inferior and cheaper (§9.8)"
                    )[:4096],
                    expected_benefit=(
                        "efficiency, or non-inferior with fewer tokens per solved "
                        "task, marks a removal candidate (§9.8)"
                    ),
                    risks=[
                        "leave-one-out of an active component; promoted only through the "
                        "ordinary stages, canary and the operator",
                        "a removal only when removal_verdict holds after focused (§9.8); the "
                        "removal gate holds holdout and approvals otherwise (IC-24)",
                    ],
                    observation_refs=[observation],
                    prediction=None,
                    baseline_ref=champion,
                    origin=REMOVAL_ORIGIN,
                )
            )
        except RuntimeFault:
            continue
    return out
