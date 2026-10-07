"""Stage runner: the screening, focused, ablation and holdout stages of one proposal (Work 033 S11).

interfaces.md §2.9 (``stage-plan``, ``stage-run``, ``observation-cache``), §3.9, §7, §8.1, §8.5,
IC-01, IC-02, IC-09, IC-16, IC-19 (A, provisional), IC-20 (provisional).

- IC-01: the **holdout** experiment is the one bound to the evolution machine
  (``approve_experiment -> start_offline -> run(split="holdout") -> evaluate``). Screening, focused
  and ablation are stage experiments frozen through ``EvaluationService`` under the same proposal
  and the same root budget (``META_BUDGET_CHANGE`` otherwise) while the evolution head stays
  ``screened``.
- IC-02 / IC-19 (A): ablation variants are separate *derived* proposals (baseline = the parent
  candidate, candidate = the leave-one-out variant) with their own budget roots. They are never
  screened, stay ``draft`` and are never approved for an offline experiment or a canary; their
  stage experiment is frozen directly after ``check_derived`` (``Hold DERIVED_PROPOSAL`` when the
  variant brings any content outside the parent's two manifests).
- IC-09 / IC-20: exploratory stages are never refused for size (a stage needs at least one task to
  run); the focused stage holds ``NO_INFORMATIVE_TASKS`` below ``max(16, n_min)`` informative
  validation tasks, the holdout stage ``SAMPLE_UNDERPOWERED`` below that many holdout tasks.
- §8.1: ``advance`` runs the stages whose gate is ``auto`` in order and stops at the first operator
  gate, at a failure, or when the root budget cannot cover the next stage. ``approve_stage`` is the
  operator's gate: it freezes that stage's experiment, issues the operator approval for its exact
  digest and runs it in the same process. The ablation never gates: a variant that cannot be
  built, frozen or run, or whose run ended ``aborted``, is a finding of the parent's ablation stage
  (``ABLATION <component>: <code>``; any exception of one variant, not only a RuntimeFault), the
  stage ends ``passed`` (some variant passed) or ``skipped``, and an ablation interrupted while
  ``running`` resumes on the next ``advance`` without re-running a finished variant or replaying
  a dispatched experiment. An ablation found ``failed``, ``aborted`` or ``inconclusive`` (a state
  this module never writes for the parent) is closed with one finding per unmeasured component,
  so the holdout gate opens.
- One runner per proposal (``stage-lock`` head ``stagelock-<root proposal id>``): ``advance``,
  ``approve_stage`` and ``LocalMetaOps.reconcile`` hold it; a second one gets ``Hold
  SEARCH_BUSY`` (a code §3.14 does not list) instead of resuming, and so aborting, the first one's
  live variant. A lock held under an earlier store ``owner_epoch`` is taken over: the store's owner
  lock (``Store``: ``ACTIVE_OWNER``) admits one owning process at a time, so a lock of an earlier
  epoch belongs to a process that ended (the rule of ``EvaluationService.recover_interrupted``).
- Corpus binding: the trial executor builds each trial from the corpus loaded now, by case id. The
  plan, each stage freeze and a run of an experiment frozen by an earlier process first check
  that the loaded tasks are the frozen ones (``LocalMetaOps.check_corpus``, Hold
  ``CORPUS_CHANGED``, accepted in "Clarifications After S9 And S11").
- §9.6, §9.9: after each evaluated stage report (screening, focused, holdout; and each ablation
  variant) the runner writes the proposal's ``prediction-score`` of that stage when it has a
  prediction and calls ``EliteArchive.update`` (elites from development rows only; focused and
  holdout reach the archive only as the operator-only lineage verdict). Both are idempotent; a
  failure is a finding of that stage (``PREDICTION_SCORE <stage>: <code>``, ``ELITE_ARCHIVE
  <stage>: <code>``) and never changes its verdict.
- A gating stage found ``running`` whose experiment is still ``frozen`` (the process stopped
  between the stage turning ``running`` and the first dispatch, so no trial ran) is resumed by
  ``advance``; one whose experiment left ``frozen`` stays for ``amplai meta reconcile``.
- IC-24 (provisional): a removal-sweep proposal (change artifact ``origin: "removal_sweep"``)
  reaches the holdout stage only as a removal (``check_removal``, Hold NOT_A_REMOVAL).
- §8.5: trials of exploratory stages (development split) are indexed in ``observation-cache``
  heads by their baseline-reuse cache key; validation and holdout trials never are, and no frozen
  experiment ever substitutes a cached trial (its trials are its own,
  ``MetaHarness._report``).

- IC-12 (Work 033 S7b, §2.9, §10.5 step 6): the plan records its app (``app_id``: ``--app`` at
  plan time; a plan written before S7b has none and keeps reading ``--app``) and every stage
  selects that app's main-set cases, so the IC-15 recomputation (``SAMPLING_CHANGED``) holds per
  app whatever ``--app`` names later. ``environment_digests`` pins, per task environment of the
  app's tasks that the app has installed, the digest of its environment record under the
  evaluation service's probe (``local_executor.task_environment_digest``); every stage run gives
  them (and that probe) to the trial executor, which runs nothing for a drifted task environment
  (``success`` None, receipt ``environment_drift``). A stage whose report holds such a trial gets
  the guard finding ``task_environment_drift`` (``TASK_ENVIRONMENT_DRIFT``); the report's own
  ``environment_drift`` reason stays tied to the experiment's single ``environment_ref``.

Not decided by the contract and therefore not done here (reported): the per-experiment
``max_trial_tokens`` (§5.3; absent, so the qualified executor's ceiling applies), and the
proposal's ``experiment_plan_ref`` (the proposal is written before the operator gives the root
budget, so it names a draft record that carries the stage-plan id; see
``LocalMetaOps.propose_components``).
"""

from __future__ import annotations

import contextlib
import copy
import json
import math
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from statistics import mean
from typing import TYPE_CHECKING, Any

from ..evaluation import versions
from ..evaluation.calibration import select_cases
from ..evaluation.corpus import CorpusService
from ..evaluation.receipts import read_receipt
from ..evaluation.sequential import mde, min_tasks_for_margin, noise_band
from ..runtime.contracts.identity import digest, new_id, now
from ..runtime.contracts.semantics import resolve_ref
from ..runtime.errors import Hold, RuntimeFault
from ..runtime.storage.store import Scope, Store
from .local_executor import task_environment_digest
from .manifest import BUDGET, CONTEXT, DECIDERS, ROUTER, Manifest, ManifestService
from .service import only_class_a

if TYPE_CHECKING:
    from ..runtime.contracts.authority import Actor
    from ..runtime.execution.meta_ops import LocalMetaOps
    from ..runtime.execution.product import InstalledApp
    from .leak_gate import LeakGate
    from .trial_metrics import TrialMetrics

Ref = dict[str, Any]

STAGE_ORDER = ("screening", "focused", "ablation", "holdout")
TEMPLATES = ("default_v1",)
PLAN_KIND, RUN_KIND, CACHE_KIND = "stage-plan", "stage-run", "observation-cache"
LOCK_KIND = "stage-lock"  # one runner per proposal root (not a §2.9 kind; reported)
# IC-10: a focused experiment built at the end of a night for the operator's ``--queue`` approval
# (head ``stagequeue-<proposal_id>-<stage>``; not a §2.9 kind; reported)
QUEUE_KIND = "stage-queue"
QUEUE_STATES = ("queued", "approved", "running", "ran", "failed", "superseded")
QUEUED_STAGES = ("focused",)  # holdout stays an in-process operator run (IC-10)
SCREENING_FAILED = "SCREENING_FAILED"  # a night's failed screening, left for the operator
PLAN_SCHEMA = "amplai.stage-plan.v1"
STATES = (
    "pending", "waiting_approval", "frozen", "running", "passed", "failed", "inconclusive",
    "aborted", "skipped",
)  # fmt: skip
TERMINAL = frozenset({"passed", "failed", "inconclusive", "aborted", "skipped"})
OPERATOR_GATES = ("focused", "holdout")
# a parent's ablation finding: "ABLATION <component>: <code>" (a variant without a report, §8.1)
ABLATION_FINDING = "ABLATION"
# after an evaluated stage (§9.6, §9.9): a scoring or archive failure is a finding of that stage,
# "PREDICTION_SCORE <stage>: <code>" / "ELITE_ARCHIVE <stage>: <code>"; it never changes the verdict
SCORE_FINDING = "PREDICTION_SCORE"
ARCHIVE_FINDING = "ELITE_ARCHIVE"
SCORED_STAGES = ("screening", "focused", "holdout")  # §9.6
# §10.5 step 6 (S7b): the stage record's reason when a trial ran into a drifted task environment
TASK_ENVIRONMENT_DRIFT = "task_environment_drift"
VERDICT_STAGES = ("focused", "holdout")  # §9.9: operator-only lineage verdicts
# IC-24 (provisional): a removal-sweep proposal's change artifact names this origin
REMOVAL_ORIGIN = "removal_sweep"
# default_v1 (§8.1 table; numbers from plan.md §1.4, §4, corrected by IC-09)
CONFIDENCE = 0.95
MARGIN = 0.25
EXPLORATORY_TASKS = 12
ABLATION_VARIANTS = 3
REFERENCE_MAX_N = 3  # IC-06
HACK_GUARDS = {
    "verified_hidden_fail": 2,
    "ask_back_rate": 0.2,
    "broken_tool_calls_rate": 0.2,
    "edit_rate_ratio": 3.0,
}  # fmt: skip  (no test_file_edits: a test edit is a safety failure, IC-18)
MODE = "sandbox_rerun"
TWO_ARMS = ["baseline", "candidate"]
THREE_ARMS = ["baseline", "candidate", "reference"]
# §7.5: without calibration data the A/A discordance is taken as 0.5 and the rationale says so
ASSUMED_DISCORDANCE = 0.5
UNKNOWN_SHA = "unknown"  # §8.5: a key built without the harness sha is never reused
CARRIER_FIELDS = (
    "prompt_bundle_ref", "context_policy_ref", "budget_policy_ref", "router_policy_ref",
)  # fmt: skip
IDENTITY_FIELDS = frozenset({"composition_id", "revision", "created_at"})
BUDGET_FIELDS = frozenset(
    {
        "max_wall_seconds", "max_attempts", "max_tokens", "max_cost_microunits", "currency",
        "max_parallel_works", "max_delegation_depth",
    }
)  # fmt: skip
STAGE_FIELDS = frozenset(
    {
        "stage", "split", "case_rule", "max_tasks", "repeats", "arms", "reference", "purpose",
        "endpoint", "margin", "minimum_effect", "gate", "hack_guards",
    }
)  # fmt: skip
PLAN_FIELDS = frozenset(
    {
        "schema", "scope", "proposal_id", "cell_id", "corpus_ref", "calibration_summary_ref",
        "evaluator_version_ref", "root_budget", "environment_digests", "stages",
        "ablation_components", "created_at",
    }
)  # fmt: skip
# IC-12 (S7b): the app the plan's stages select from; a plan written before S7b has none
OPTIONAL_PLAN_FIELDS = frozenset({"app_id"})
ALL_SLOTS = ("prompt_bundle_ref", *CONTEXT, *BUDGET, *ROUTER)


def confirmatory_tasks() -> int:
    """``max(16, n_min)`` of the §8.1 table (IC-09: n_min = 16 at m = 0.25, c = 0.95)."""
    return max(16, min_tasks_for_margin(MARGIN, CONFIDENCE))


# -- component names (the manifest's "<kind>[.<layer>]" keys, Manifest.flat) ----------------
def component_name(slot: str) -> str:
    if slot == "prompt_bundle_ref":
        return "role_prompt"
    return "decider." + slot if slot in DECIDERS else slot


def slot_of(name: str) -> str:
    """A manifest slot from a slot or a component name (``role_prompt``, ``decider.L2``)."""
    if name == "role_prompt":
        return "prompt_bundle_ref"
    if name.startswith("decider."):
        return name.split(".", 1)[1]
    return name


def _slots(manifest: Manifest) -> dict[str, Ref | None]:
    return {
        "prompt_bundle_ref": manifest.prompt_bundle_ref,
        **manifest.context,
        **manifest.budget,
        **manifest.router,
    }


# -- the stage template ----------------------------------------------------------------------------
def template_stages(template: str, *, holdout_tasks: int) -> list[dict[str, Any]]:
    """The four stage entries of ``template`` (§8.1 table, §2.9 shape)."""
    if template not in TEMPLATES:
        raise RuntimeFault("STAGE_PLAN", "Only the default_v1 stage template is defined")

    def stage(
        name: str, split: str, rule: str, tasks: int, repeats: int, arms: list[str],
        reference: dict[str, Any] | None, purpose: str, gate: str,
    ) -> dict[str, Any]:  # fmt: skip
        return {
            "stage": name, "split": split, "case_rule": rule, "max_tasks": tasks,
            "repeats": repeats, "arms": list(arms), "reference": reference, "purpose": purpose,
            "endpoint": "noninferiority", "margin": MARGIN, "minimum_effect": None,
            "gate": gate, "hack_guards": dict(HACK_GUARDS),
        }  # fmt: skip

    # focused: the reference n is cost-matched from screening (§7.4); the plan records its cap
    reference = {"kind": "best_of_n", "n": REFERENCE_MAX_N, "cost_match": "tokens"}
    return [
        stage("screening", "development", "informative_v1", EXPLORATORY_TASKS, 1, TWO_ARMS,
              None, "exploratory", "auto"),
        stage("focused", "validation", "informative_v1", confirmatory_tasks(), 2, THREE_ARMS,
              reference, "confirmatory", "operator"),
        stage("ablation", "development", "informative_v1", EXPLORATORY_TASKS, 1, TWO_ARMS,
              None, "exploratory", "auto"),
        stage("holdout", "holdout", "all_v1", holdout_tasks, 1, TWO_ARMS, None, "confirmatory",
              "operator"),
    ]  # fmt: skip


def _is_ref(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"id", "revision", "digest"}
        and isinstance(value["id"], str)
        and type(value["revision"]) is int
        and isinstance(value["digest"], str)
    )


def _count(value: Any, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


def validate_budget(budget: Any) -> None:
    """``$defs.budget`` (``contracts/schemas/common.schema.json``); RuntimeFault STAGE_PLAN."""
    ok = (
        isinstance(budget, dict)
        and set(budget) == BUDGET_FIELDS
        and _count(budget["max_wall_seconds"], 1, 604800)
        and _count(budget["max_attempts"], 1, 10)
        and _count(budget["max_tokens"], 1, 9007199254740991)
        and (
            budget["max_cost_microunits"] is None
            or _count(budget["max_cost_microunits"], 0, 9007199254740991)
        )
        and isinstance(budget["currency"], str)
        and len(budget["currency"]) == 3
        and budget["currency"].isascii()
        and budget["currency"].isalpha()
        and budget["currency"].isupper()
        and _count(budget["max_parallel_works"], 1, 64)
        and _count(budget["max_delegation_depth"], 0, 8)
    )
    if not ok:
        raise RuntimeFault("STAGE_PLAN", "The root budget does not match $defs.budget")


def validate_stage_plan(value: Any) -> None:
    """The §2.9 ``stage-plan`` shape (validated before ``put``, §2.0); RuntimeFault STAGE_PLAN."""
    ok = (
        isinstance(value, dict)
        and PLAN_FIELDS <= set(value) <= PLAN_FIELDS | OPTIONAL_PLAN_FIELDS
        and ("app_id" not in value or (isinstance(value["app_id"], str) and bool(value["app_id"])))
        and value["schema"] == PLAN_SCHEMA
        and isinstance(value["proposal_id"], str)
        and isinstance(value["cell_id"], str)
        and all(
            _is_ref(value[k])
            for k in ("corpus_ref", "calibration_summary_ref", "evaluator_version_ref")
        )
        and isinstance(value["environment_digests"], dict)
        and all(
            isinstance(k, str) and isinstance(v, str) and v.startswith("sha256:")
            for k, v in value["environment_digests"].items()
        )
        and isinstance(value["stages"], list)
        and [s.get("stage") for s in value["stages"] if isinstance(s, dict)] == list(STAGE_ORDER)
        and all(set(s) == STAGE_FIELDS for s in value["stages"])
        and isinstance(value["ablation_components"], list)
        and len(value["ablation_components"]) <= ABLATION_VARIANTS
        and isinstance(value["created_at"], str)
    )
    if not ok:
        raise RuntimeFault("STAGE_PLAN", "Invalid stage-plan record (§2.9)")
    validate_budget(value["root_budget"])
    for s in value["stages"]:
        if (
            s["case_rule"] not in ("informative_v1", "all_v1")
            or not _count(s["max_tasks"], 1, 256)
            or not _count(s["repeats"], 1, 3)
            or s["arms"] not in (TWO_ARMS, THREE_ARMS)
            or s["purpose"] not in ("exploratory", "confirmatory")
            or s["gate"] not in ("auto", "operator")
        ):
            raise RuntimeFault("STAGE_PLAN", "Invalid stage entry (§2.9)", details=s["stage"])


def validate_stage_run(value: Any) -> None:
    """The §2.9 ``stage-run`` head shape; a derived proposal's head holds its ablation only."""
    stages = value.get("stages") if isinstance(value, dict) else None
    ok = (
        isinstance(value, dict)
        and set(value) == {"plan_ref", "current", "stages", "bound_to_evolution"}
        and _is_ref(value["plan_ref"])
        and (value["current"] is None or value["current"] in STAGE_ORDER)
        and value["bound_to_evolution"] == "holdout"
        and isinstance(stages, dict)
        and set(stages) <= set(STAGE_ORDER)
        and all(
            isinstance(s, dict)
            and set(s)
            == {
                "state",
                "experiment_ref",
                "report_ref",
                "decision_class",
                "guard_findings",
                "ablation_proposals",
            }
            and s["state"] in STATES
            for s in stages.values()
        )
    )
    if not ok:
        raise RuntimeFault("STAGE_RUN", "Invalid stage-run head (§2.9)")


def _empty_stage(state: str = "pending") -> dict[str, Any]:
    return {
        "state": state, "experiment_ref": None, "report_ref": None, "decision_class": None,
        "guard_findings": [], "ablation_proposals": [],
    }  # fmt: skip


def _finding_component(finding: str) -> str | None:
    """The component of an ``ABLATION <component>: <code>`` finding (None for another one)."""
    head, _, rest = finding.partition(" ")
    return rest.split(":", 1)[0] if head == ABLATION_FINDING and ":" in rest else None


def _code(exc: Exception) -> str:
    """The code of a variant's failure: a RuntimeFault's public code, else the exception type."""
    return exc.code if isinstance(exc, RuntimeFault) else type(exc).__name__


def update_stage(store: Store, scope: Scope, proposal_id: str, stage: str, **fields: Any) -> None:
    """Set ``fields`` of one stage of the proposal's ``stage-run`` head (CAS, validated)."""
    with store.tx() as db:
        head = store.head(scope, RUN_KIND, "stagerun-" + proposal_id, db=db)
        data = copy.deepcopy(head["data"])
        data["stages"].setdefault(stage, _empty_stage()).update(fields)
        data["current"] = stage
        validate_stage_run(data)
        store.cas(db, scope, RUN_KIND, "stagerun-" + proposal_id, head["row_version"],
                  f"{stage}:{data['stages'][stage]['state']}", data)  # fmt: skip
    if fields.get("state") == "aborted":
        # a queued stage that aborted (its run faulted, or ``amplai meta reconcile`` ended it after
        # a crash) is never left ``running`` in its queue head
        settle_queue(store, scope, proposal_id, stage, failure={"code": "STAGE_ABORTED"})


def lock_root(store: Store, scope: Scope, proposal_id: str) -> str:
    """The proposal whose runner owns ``proposal_id``'s stages: the parent for a derived
    proposal (its stage-run head names the parent's plan), else the proposal itself."""
    try:
        run = store.head(scope, RUN_KIND, "stagerun-" + proposal_id)
    except RuntimeFault as exc:
        if exc.code != "NOT_FOUND":
            raise
        return proposal_id
    plan = store.get(scope, PLAN_KIND, run["data"]["plan_ref"])
    return str(plan["proposal_id"])


@contextlib.contextmanager
def stage_lock(store: Store, scope: Scope, root_id: str) -> Iterator[None]:
    """Hold the ``stage-lock`` of ``root_id`` while the block runs. Hold SEARCH_BUSY when a runner
    of this store owner (same ``owner_epoch``) holds it; a lock of an earlier epoch is taken over
    (its process ended: the store admits one owning process, ``Store``: ``ACTIVE_OWNER``)."""
    lock_id = "stagelock-" + root_id
    token = new_id("stagelock")
    with store.tx() as db:
        try:
            head: dict[str, Any] | None = store.head(scope, LOCK_KIND, lock_id, db=db)
        except RuntimeFault as exc:
            if exc.code != "NOT_FOUND":
                raise
            head = None
        if (
            head is not None
            and head["state"] == "held"
            and head["data"].get("owner_epoch") == store.epoch
        ):
            raise Hold(
                "SEARCH_BUSY",
                "Another stage runner works on this proposal now; wait for it",
                details={"proposal_id": root_id, "since": head["data"].get("acquired_at")},
            )
        store.cas(db, scope, LOCK_KIND, lock_id, head["row_version"] if head else 0, "held",
                  {"proposal_id": root_id, "holder": token, "owner_epoch": store.epoch,
                   "acquired_at": now()})  # fmt: skip
    try:
        yield
    finally:
        with store.tx() as db:
            held = store.head(scope, LOCK_KIND, lock_id, db=db)
            if held["state"] == "held" and held["data"].get("holder") == token:
                store.cas(db, scope, LOCK_KIND, lock_id, held["row_version"], "released",
                          {**held["data"], "released_at": now()})  # fmt: skip


def is_derived(store: Store, scope: Scope, proposal: dict[str, Any]) -> bool:
    """IC-19 (A): a derived proposal names its parent's stage plan as its experiment plan."""
    kind, value = resolve_ref(store, scope, proposal["experiment_plan_ref"])
    return kind == PLAN_KIND and value.get("proposal_id") != proposal["proposal_id"]


def check_derived(
    store: Store,
    scope: Scope,
    manifests: ManifestService,
    parent: dict[str, Any],
    derived: dict[str, Any],
) -> None:
    """IC-19 (A): Hold DERIVED_PROPOSAL unless the variant brings no new content: its baseline is
    the parent's candidate, every manifest slot equals the parent candidate's or the parent
    baseline's, and every other composition field equals the parent candidate's."""

    def refuse(why: str, details: object = None) -> Hold:
        return Hold("DERIVED_PROPOSAL", why, details=details)

    if derived["baseline_ref"] != parent["candidate_ref"]:
        raise refuse("A derived proposal's baseline is its parent's candidate (IC-02)")
    try:
        base = _slots(manifests.of_composition(parent["baseline_ref"]))
        cand = _slots(manifests.of_composition(parent["candidate_ref"]))
        variant = _slots(manifests.of_composition(derived["candidate_ref"]))
        cand_value = store.get(scope, "harness-composition", parent["candidate_ref"])
        variant_value = store.get(scope, "harness-composition", derived["candidate_ref"])
    except (RuntimeFault, KeyError, TypeError) as exc:
        raise refuse("The compositions cannot be read") from exc
    new = [s for s in ALL_SLOTS if variant.get(s) not in (cand.get(s), base.get(s))]
    if new:
        raise refuse("The variant has content outside its parent's two manifests",
                     {"slots": [component_name(s) for s in new]})  # fmt: skip
    fields = sorted(
        k for k in set(cand_value) | set(variant_value)
        if k not in IDENTITY_FIELDS and k not in CARRIER_FIELDS
        and cand_value.get(k) != variant_value.get(k)
    )  # fmt: skip
    if fields:
        raise refuse("The variant changes composition fields beside its manifest",
                     {"fields": fields})  # fmt: skip


def proposal_origin(
    store: Store, scope: Scope, artifacts: Any, proposal: dict[str, Any]
) -> str | None:
    """The ``origin`` its change artifact names (IC-24: ``removal_sweep``), None when absent or
    unreadable (``screen`` refuses an unreadable change artifact on its own)."""
    try:
        change = json.loads(artifacts.read(scope, proposal["change_artifact"]))
    except (RuntimeFault, KeyError, TypeError, ValueError):
        return None
    origin = change.get("origin") if isinstance(change, dict) else None
    return origin if isinstance(origin, str) else None


def check_removal(store: Store, scope: Scope, artifacts: Any, proposal_id: str) -> None:
    """IC-24 (provisional) removal gate: a removal-sweep proposal reaches holdout or an approval
    only as a removal candidate (§9.8: focused decision class ``efficiency``, or ``non_inferior``
    with fewer tokens per solved task in the candidate arm, ``proposer.removal_verdict``); Hold
    NOT_A_REMOVAL otherwise. Any other proposal passes unchanged."""
    from .proposer import removal_verdict

    head = store.head(scope, "evolution", proposal_id)
    proposal = store.get(scope, "harness-change-proposal", head["data"]["proposal_ref"])
    if proposal_origin(store, scope, artifacts, proposal) != REMOVAL_ORIGIN:
        return
    verdict = removal_verdict(store, scope, proposal_id)
    if verdict["removal_candidate"] is not True:
        raise Hold(
            "NOT_A_REMOVAL",
            "A removal-sweep proposal goes on only when its focused stage shows efficiency, or "
            "non-inferiority with fewer tokens per solved task (IC-24, §9.8)",
            details=verdict,
        )


def _needs_review(head: dict[str, Any]) -> bool:
    """A class-B candidate waits for the operator's human code review before ``screen``."""
    classification = head["data"].get("classification") or {}
    return classification.get("surface_class") == "B" and not head["data"].get("review_ref")


@dataclass(frozen=True)
class StageStep:
    stage: str
    state: str
    waiting_for: str | None  # "approve-stage focused" | "approve-stage holdout" | "review" | None
    experiment_ref: Ref | None
    report_ref: Ref | None
    decision_class: str | None


def stage_status(store: Store, scope: Scope, proposal_id: str) -> list[StageStep]:
    """Where a proposal's stages are (§3.9 ``status``); a proposal without a stage plan shows
    every stage pending, a derived proposal only its ablation."""
    head = store.head(scope, "evolution", proposal_id)
    try:
        run: dict[str, Any] | None = store.head(scope, RUN_KIND, "stagerun-" + proposal_id)
    except RuntimeFault as exc:
        if exc.code != "NOT_FOUND":
            raise
        run = None
    stages: dict[str, Any] = run["data"]["stages"] if run else {}
    review = head["state"] == "draft" and _needs_review(head)
    names = [s for s in STAGE_ORDER if s in stages] if stages else list(STAGE_ORDER)
    steps = []
    for name in names:
        st = stages.get(name) or _empty_stage()
        waiting = None
        if name == "screening" and review and st["state"] == "pending":
            waiting = "review"
        elif st["state"] == "waiting_approval":
            waiting = "approve-stage " + name
        steps.append(
            StageStep(name, st["state"], waiting, st["experiment_ref"], st["report_ref"],
                      st["decision_class"])
        )  # fmt: skip
    return steps


def stage_findings(store: Store, scope: Scope, proposal_id: str) -> dict[str, list[str]]:
    """The ``guard_findings`` of each stage that has any (screening hack guards, ablation
    variants without a report); StageStep (§3.9) does not carry them."""
    try:
        run = store.head(scope, RUN_KIND, "stagerun-" + proposal_id)
    except RuntimeFault as exc:
        if exc.code != "NOT_FOUND":
            raise
        return {}
    return {
        name: list(run["data"]["stages"][name]["guard_findings"])
        for name in STAGE_ORDER
        if name in run["data"]["stages"] and run["data"]["stages"][name]["guard_findings"]
    }


def queue_head(
    store: Store, scope: Scope, proposal_id: str, stage: str = "focused"
) -> dict[str, Any] | None:
    """The ``stage-queue`` head of one proposal's stage (IC-10), or None."""
    try:
        return dict(store.head(scope, QUEUE_KIND, f"stagequeue-{proposal_id}-{stage}"))
    except RuntimeFault as exc:
        if exc.code != "NOT_FOUND":
            raise
        return None


def queued_stages(store: Store, scope: Scope, *states: str) -> list[dict[str, Any]]:
    """Every queue head in one of ``states`` (all when none is named), oldest first:
    ``{"queue_id", "state", **data}``."""
    out = []
    for head_id, state, data in heads_of(store, scope, QUEUE_KIND):
        if not states or state in states:
            out.append({"queue_id": head_id, "state": state, **data})
    return out


def _queue_item(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "proposal_id": data["proposal_id"],
        "stage": data["stage"],
        "queue_id": f"stagequeue-{data['proposal_id']}-{data['stage']}",
        "subject_digest": data["subject_digest"],
        "experiment_id": data["experiment"]["experiment_id"],
    }


def _queue_write(
    store: Store,
    scope: Scope,
    proposal_id: str,
    stage: str,
    state: str,
    data: dict[str, Any],
    *,
    row: int | None = None,
) -> int:
    """Write the queue head (CAS on ``row``; a new or superseded head is rewritten from its
    current row). Returns the new row version."""
    if state not in QUEUE_STATES:
        raise RuntimeFault("STAGE_QUEUE", "Unknown queue state", details=[state])
    head_id = f"stagequeue-{proposal_id}-{stage}"
    with store.tx() as db:
        if row is None:
            try:
                row = int(store.head(scope, QUEUE_KIND, head_id, db=db)["row_version"])
            except RuntimeFault as exc:
                if exc.code != "NOT_FOUND":
                    raise
                row = 0
        version: int = store.cas(db, scope, QUEUE_KIND, head_id, row, state, data)
    return version


def settle_queue(
    store: Store,
    scope: Scope,
    proposal_id: str,
    stage: str,
    *,
    failure: dict[str, Any] | None = None,
    report_ref: Ref | None = None,
) -> str | None:
    """End a ``running`` queue head (IC-10 queue shape, interfaces.md "Clarifications After S12,
    S15 And S9b": states ... ran / failed ...): ``failed`` with ``failure`` (``{code, ...}``, the
    reason) when one is given, else ``ran`` with ``report_ref``. A head in another state (or none)
    is left as it is. Returns the state written, or None."""
    head_id = f"stagequeue-{proposal_id}-{stage}"
    with store.tx() as db:
        try:
            head = store.head(scope, QUEUE_KIND, head_id, db=db)
        except RuntimeFault as exc:
            if exc.code != "NOT_FOUND":
                raise
            return None
        if head["state"] != "running":
            return None
        data = copy.deepcopy(head["data"])
        if failure is not None:
            state = "failed"
            data.update(failure=dict(failure), failed_at=now())
        else:
            state = "ran"
            data.update(report_ref=report_ref, ran_at=now())
        store.cas(db, scope, QUEUE_KIND, head_id, head["row_version"], state, data)
    return state


def heads_of(store: Store, scope: Scope, kind: str) -> list[tuple[str, str, dict[str, Any]]]:
    """(id, state, data) of every head of ``kind`` in ``scope``, by id (a read; the store has no
    list of heads by kind, so this reads its ``heads`` table as the dashboard reader does)."""
    with store._lock:
        rows = store.conn.execute(
            "SELECT id,state,data FROM heads WHERE tenant=? AND project=? AND kind=? ORDER BY id",
            (*scope.keys(), kind),
        ).fetchall()
    return [(r["id"], r["state"], json.loads(r["data"])) for r in rows]


@dataclass(frozen=True)
class ApprovalIssuer:
    """Who freezes and runs a ``StageRunner``'s stage experiments and how each one's exact
    approval is issued (IC-10 mechanics, clarification after S12).

    - ``operator_issuer``: the human operator; ``LocalMetaApprovals.issue`` of the experiment's
      exact digest. Its runner screens drafts, rejects a failed screening and holds every gate.
    - ``standing_issuer``: the nightly identity; ``LocalMetaApprovals.issue_standing`` (derived,
      plan-bound: exploratory development experiments only, so a confirmatory or holdout plan is
      refused there with Hold STANDING_APPROVAL). Its runner screens class A drafts only, as
      the nightly identity (IC-30 (A), ``harness.screen``: the same protected-surface check and
      leak gate); it never screens a class B draft, reviews, rejects or runs an operator gate: a
      failed screening is recorded (``SCREENING_FAILED`` finding) and left for the operator."""

    actor: Actor
    issue: Callable[[dict[str, Any]], Ref]
    human: bool


def operator_issuer(ops: LocalMetaOps) -> ApprovalIssuer:
    """The human operator's issuer (the S11 path)."""
    operator, approvals = ops.operator, ops.local.approvals

    def issue(plan: dict[str, Any]) -> Ref:
        return dict(approvals.issue(operator, "experiment.execute", digest(plan)))

    return ApprovalIssuer(operator, issue, True)


def standing_issuer(nightly: Actor, derive: Callable[[dict[str, Any]], Ref]) -> ApprovalIssuer:
    """The nightly identity's issuer: ``derive`` is ``NightlyRunner.derive`` (``issue_standing``
    under the night's standing approval)."""
    return ApprovalIssuer(nightly, derive, False)


class StageRunner:
    def __init__(
        self,
        ops: LocalMetaOps,
        corpus: dict[str, Ref],
        *,
        calibration_summary_ref: Ref,
        evaluator_version_ref: Ref,
        metrics: TrialMetrics,
        leak_gate: LeakGate,
        parallel: int,
        issuer: ApprovalIssuer | None = None,
    ) -> None:
        """``corpus`` = the corpus v2 freeze result (``corpus_ref``, ``task_index_ref``,
        ``leak_index_ref``); ``parallel`` = the trial concurrency (1..4, §8.4); ``issuer`` =
        who freezes and runs the stage experiments (default: the human operator)."""
        if type(parallel) is not int or not 1 <= parallel <= 4:
            raise RuntimeFault("EVAL_PARALLEL", "parallel is an integer from 1 to 4")
        if not _is_ref(corpus.get("corpus_ref")):
            raise RuntimeFault("STAGE_PLAN", "The frozen corpus refs name no corpus_ref")
        self.ops, self.corpus = ops, dict(corpus)
        self.summary_ref, self.version_ref = calibration_summary_ref, evaluator_version_ref
        self.metrics, self.leak_gate, self.parallel = metrics, leak_gate, parallel
        self.store, self.scope = ops.store, ops.scope
        self.local, self.operator = ops.local, ops.operator
        self.issuer = issuer if issuer is not None else operator_issuer(ops)
        # who freezes, runs and reads the stage experiments (the operator or the nightly identity)
        self.actor = self.issuer.actor
        self.artifacts = ops.dep.artifacts
        self.cases = CorpusService(self.store, self.artifacts)
        # why the last ``advance`` stopped before an operator gate (None: it did not)
        self.last_stop: dict[str, Any] | None = None
        # §7.7, §8.4: EvaluationService.run caps trials at min(this, the root's
        # max_parallel_works, 4)
        self.local.evaluation.max_parallel = parallel

    # -- reading ------------------------------------------------------------------------------
    def _evolution(self, proposal_id: str) -> dict[str, Any]:
        return dict(self.store.head(self.scope, "evolution", proposal_id))

    def _proposal(self, proposal_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        head = self._evolution(proposal_id)
        proposal = self.store.get(
            self.scope, "harness-change-proposal", head["data"]["proposal_ref"]
        )
        return proposal, head

    def _plan_or_none(self, proposal_id: str) -> tuple[Ref, dict[str, Any]] | None:
        rows = [
            (ref, value) for ref, value in self.store.list_objects(self.scope, PLAN_KIND)
            if ref["id"] == "stageplan-" + proposal_id
        ]  # fmt: skip
        if not rows:
            return None
        ref = max(rows, key=lambda row: row[0]["revision"])[0]
        return ref, self.store.get(self.scope, PLAN_KIND, ref)

    def _plan(self, proposal_id: str) -> tuple[Ref, dict[str, Any]]:
        found = self._plan_or_none(proposal_id)
        if found is None:
            raise Hold("META_STATE", "The proposal has no stage plan; run amplai meta search")
        return found

    def _run_or_none(self, proposal_id: str) -> dict[str, Any] | None:
        try:
            return dict(self.store.head(self.scope, RUN_KIND, "stagerun-" + proposal_id))
        except RuntimeFault as exc:
            if exc.code != "NOT_FOUND":
                raise
            return None

    def _stage_state(self, proposal_id: str, stage: str) -> dict[str, Any]:
        run = self._run_or_none(proposal_id)
        if run is None or stage not in run["data"]["stages"]:
            return _empty_stage()
        state: dict[str, Any] = run["data"]["stages"][stage]
        return state

    def _evaluator(self, ref: Ref) -> dict[str, Any]:
        """The pinned evaluator version: Hold EVALUATOR_UNQUALIFIED without a passing
        requalification, EVALUATOR_CHANGED when the running code differs (§7.8)."""
        value = versions.read_version(self.store, self.scope, ref)
        kind, requal = resolve_ref(self.store, self.scope, value["requalification_ref"])
        if kind != versions.REQUALIFICATION_KIND or requal.get("all_equal") is not True:
            raise Hold(
                "EVALUATOR_UNQUALIFIED", "The evaluator version has no passing requalification"
            )
        versions.check_current(value)
        return value

    def _check_evaluator(self, plan: dict[str, Any]) -> None:
        if plan["evaluator_version_ref"] != self.version_ref:
            raise Hold("EVALUATOR_CHANGED", "One proposal keeps one evaluator version (§11.1)")
        self._evaluator(plan["evaluator_version_ref"])

    def _summary(self) -> dict[str, Any]:
        kind, summary = resolve_ref(self.store, self.scope, self.summary_ref)
        if kind != "calibration-summary":
            raise RuntimeFault("STAGE_PLAN", "calibration_summary_ref names another kind")
        if summary.get("evaluator_version_ref") != self.version_ref:
            raise Hold("EVALUATOR_CHANGED", "The calibration summary pins another evaluator")
        calibration = self.store.get(self.scope, "calibration-plan", summary["plan_ref"])
        if calibration["corpus_ref"] != self.corpus["corpus_ref"]:
            raise RuntimeFault("STAGE_PLAN", "The calibration summary is of another corpus")
        return summary

    def _app_of(self, plan: dict[str, Any] | None) -> InstalledApp:
        """The app a plan's stages select from and run on (IC-12, S7b): the plan's ``app_id``;
        ``--app`` (``LocalMetaOps.app``) for a plan without one or before planning."""
        app_id = (plan or {}).get("app_id")
        if app_id is None:
            return self.ops.app
        installed = self.ops.dep.service.apps.get(app_id)
        if installed is None:
            raise Hold("TARGET_UNKNOWN", "The stage plan's app is not installed", details=[app_id])
        return installed

    def _select(
        self, corpus_ref: Ref, split: str, plan: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """The frozen cases of ``split`` that are main-set tasks of the plan's app
        (``LocalMetaOps.app_case_ids``; before planning ``--app`` selects the app)."""
        own = self.ops.app_case_ids(self._app_of(plan).config.app_id if plan else None)
        # the nightly identity has no corpus.holdout.evaluate: it never selects holdout cases
        cases = self.cases.select(self.actor, corpus_ref, split, purpose="frozen_experiment")
        return [c for c in cases if own is None or c["case_id"] in own]

    # -- plan -----------------------------------------------------------------------------------
    def plan(
        self,
        proposal_id: str,
        *,
        cell_id: str,
        root_budget: dict[str, Any],
        template: str = "default_v1",
    ) -> Ref:
        """The proposal's ``stage-plan`` (id ``stageplan-<proposal_id>``) and its ``stage-run``
        head. Hold SAMPLE_UNDERPOWERED | NO_INFORMATIVE_TASKS | EVALUATOR_UNQUALIFIED |
        EVALUATOR_CHANGED | LEAK_GATE | CELL_UNKNOWN | DERIVED_PROPOSAL | CORPUS_CHANGED; an
        existing plan with
        another cell or root budget is META_BUDGET_CHANGE (one root per proposal, IC-16)."""
        from ..runtime.execution import releases
        from ..runtime.execution.meta_local import leak_subject

        proposal, head = self._proposal(proposal_id)
        if is_derived(self.store, self.scope, proposal):
            raise Hold("DERIVED_PROPOSAL", "A derived proposal runs inside its parent's ablation")
        if head["state"] in ("rejected", "aborted", "rolled_back"):
            raise Hold("META_STATE", f"The candidate is {head['state']}")
        if template not in TEMPLATES:
            raise RuntimeFault("STAGE_PLAN", "Only the default_v1 stage template is defined")
        self._evaluator(self.version_ref)
        validate_budget(root_budget)
        existing = self._plan_or_none(proposal_id)
        if existing is not None:
            existing_ref, planned = existing
            if planned["cell_id"] != cell_id or planned["root_budget"] != root_budget:
                raise Hold(
                    "META_BUDGET_CHANGE",
                    "The proposal already has a stage plan with another cell or root budget",
                    details={"cell_id": planned["cell_id"], "root_budget": planned["root_budget"]},
                )
            return existing_ref
        summary = self._summary()
        app = self.ops.app
        if releases.pin_allowed(self.store, self.scope, app.compositions,
                                proposal["baseline_ref"]) != cell_id:  # fmt: skip
            raise Hold("CELL_UNKNOWN", "The proposal's baseline is not a composition of this cell")
        row = (summary.get("cells") or {}).get(cell_id)
        if not isinstance(row, dict):
            raise Hold("NO_INFORMATIVE_TASKS", "The calibration summary has no row for the cell")
        corpus_ref = self.corpus["corpus_ref"]
        corpus = self.store.get(self.scope, "eval-corpus", corpus_ref)
        # Hold CORPUS_CHANGED: the tasks loaded now are the frozen ones (every case; a nightly
        # runner checks the development and validation cases only and never builds a holdout
        # case payload: the operator's holdout gate checks its cases at its freeze)
        self.ops.check_corpus(
            corpus_ref,
            None
            if self.issuer.human
            else [c["case_id"] for c in corpus["cases"] if c["split"] != "holdout"],
        )
        own = self.ops.app_case_ids()  # the selected app's main-set tasks, as ``_select``
        environment_digests = self._environment_digests(app, corpus_ref, own)
        holdout_tasks = sum(
            1
            for c in corpus["cases"]
            if c["split"] == "holdout" and (own is None or c["case_id"] in own)
        )
        stages = template_stages(template, holdout_tasks=max(1, holdout_tasks))
        need = confirmatory_tasks()

        def informative(split: str, cap: int) -> list[str]:
            cases = self._select(corpus_ref, split)
            return select_cases(
                cases, rule="informative_v1", summary=summary, cell_id=cell_id,
                max_tasks=max(1, cap if cap else len(cases)),
                domain_of=lambda case: str(case["task_class"]),
            )  # fmt: skip

        if not informative("development", EXPLORATORY_TASKS):
            raise Hold(
                "NO_INFORMATIVE_TASKS",
                "No informative development task for the cell: screening has nothing to run",
                details={"stage": "screening", "available": 0, "need": 1},
            )
        available = len(informative("validation", 0))
        if available < need:  # IC-20 (provisional): no all_v1 fallback
            raise Hold(
                "NO_INFORMATIVE_TASKS",
                f"The focused stage needs {need} informative validation tasks of the cell",
                details={"stage": "focused", "available": available, "need": need},
            )
        if holdout_tasks < need:  # IC-09: a confirmatory stage that cannot pass its rule
            raise Hold(
                "SAMPLE_UNDERPOWERED",
                f"The holdout stage needs {need} holdout tasks to pass non-inferiority",
                details={"stage": "holdout", "task_count": holdout_tasks, "need": need},
            )
        findings = self.leak_gate.findings(
            self.scope, leak_subject(self.store, self.artifacts, self.scope, proposal)
        )
        if findings:
            raise Hold("LEAK_GATE", "The candidate names validation or holdout task material",
                       details=findings)  # fmt: skip
        manifests = self.ops.manifests
        changed = [
            component_name(c.slot)
            for c in manifests.diff(
                manifests.of_composition(proposal["baseline_ref"]),
                manifests.of_composition(proposal["candidate_ref"]),
            )
        ]
        # leave-one-out of a single changed component is the baseline itself: nothing to ablate
        ablation = changed[:ABLATION_VARIANTS] if len(changed) >= 2 else []
        value = {
            "schema": PLAN_SCHEMA,
            "scope": self.scope.wire(),
            "proposal_id": proposal_id,
            "cell_id": cell_id,
            "corpus_ref": corpus_ref,
            "calibration_summary_ref": self.summary_ref,
            "evaluator_version_ref": self.version_ref,
            "root_budget": dict(root_budget),
            "environment_digests": environment_digests,  # IC-12 task environments (S7b)
            "app_id": app.config.app_id,  # IC-12: the app every stage selects from (S7b)
            "stages": stages,
            "ablation_components": ablation,
            "created_at": now(),
        }
        validate_stage_plan(value)
        with self.store.tx() as db:
            ref: Ref = self.store.put(db, self.scope, PLAN_KIND, "stageplan-" + proposal_id, 1,
                                      value)  # fmt: skip
            run = {
                "plan_ref": ref,
                "current": None,
                "stages": {
                    name: _empty_stage(
                        "skipped" if name == "ablation" and not ablation else "pending"
                    )
                    for name in STAGE_ORDER
                },
                "bound_to_evolution": "holdout",
            }
            validate_stage_run(run)
            self.store.cas(db, self.scope, RUN_KIND, "stagerun-" + proposal_id, 0, "planned", run)
        return ref

    def _environment_digests(
        self, app: InstalledApp, corpus_ref: Ref, own: frozenset[str] | None
    ) -> dict[str, str]:
        """§2.9 ``environment_digests`` (IC-12, S7b): per task environment of the app's frozen
        cases that the app has installed, the digest the trial executor recomputes before each
        trial of it (``task_environment_digest`` with the evaluation service's probe). A task
        environment the app has not installed is not pinned: its trials hold
        ENVIRONMENT_UNQUALIFIED before any claim.

        The environments come from the frozen ``corpus-task-index`` rows of ``corpus_ref``
        (§2.7: ``environment_id`` per case), never from case payloads, so no holdout payload is
        read here (the holdout ACL of ``CorpusService.select`` stays the only way to them);
        without a task index the loaded corpus v2 tasks (checked against the frozen cases by
        ``check_corpus``) are read."""
        rows: list[tuple[str, Any]] = []
        index_ref = self.corpus.get("task_index_ref")
        index = self.store.get(self.scope, "corpus-task-index", index_ref) if index_ref else None
        if isinstance(index, dict) and index.get("corpus_ref") == corpus_ref:
            rows = [(r.get("case_id"), r.get("environment_id")) for r in index.get("tasks") or []]
        else:
            rows = [(t.task_id, t.environment_id) for t in getattr(self.ops.corpus, "tasks", ())]
        named = sorted(
            {
                env_id
                for case_id, env_id in rows
                if (own is None or case_id in own) and isinstance(env_id, str) and env_id != "app"
            }
        )
        service = self.ops.dep.service
        probe = self.local.evaluation.environment_probe
        return {
            env_id: task_environment_digest(
                self.store, self.scope, service.environment_ref(app, env_id), probe
            )
            for env_id in named
            if env_id in app.env_verifier_refs
        }

    @contextlib.contextmanager
    def _environment_pins(self, plan: dict[str, Any]) -> Iterator[None]:
        """The plan's task-environment pins and the evaluation service's probe on the trial
        executor while one stage experiment runs (§3.10, §10.5 step 6); restored afterwards.
        An executor without pins (a stand-in) is left as it is."""
        executor = self.ops.executor
        if not hasattr(executor, "environment_digests"):
            yield
            return
        before = (executor.environment_digests, getattr(executor, "environment_probe", None))
        executor.environment_digests = dict(plan.get("environment_digests") or {})
        executor.environment_probe = self.local.evaluation.environment_probe
        try:
            yield
        finally:
            executor.environment_digests, executor.environment_probe = before

    def _environment_drift(self, report: dict[str, Any]) -> bool:
        """Whether a trial of the report ran into a drifted task environment (receipt v2
        ``environment_drift``, §2.10)."""
        for ref in report.get("run_refs") or []:
            trial = self.store.get(self.scope, "eval-trial", ref)
            if not trial.get("artifact_refs"):
                continue
            try:
                receipt = read_receipt(
                    self.artifacts.read(self.scope, trial["artifact_refs"][0], trusted=True)
                )
            except (Hold, RuntimeFault, ValueError):
                continue
            if receipt.get("environment_drift") is True:
                return True
        return False

    # -- status ---------------------------------------------------------------------------------
    def status(self, proposal_id: str) -> list[StageStep]:
        return stage_status(self.store, self.scope, proposal_id)

    # -- advance (auto stages) ------------------------------------------------------------------
    def advance(self, proposal_id: str) -> list[StageStep]:
        """Run the ``auto`` stages in order; stop at the first operator gate, at a failure, or
        when the root budget cannot cover the next stage (``last_stop`` says which). The
        ablation never stops it (§8.1). Hold SEARCH_BUSY while another runner holds the
        proposal."""
        self.last_stop = None
        with stage_lock(self.store, self.scope, proposal_id):
            return self._advance(proposal_id)

    def _advance(self, proposal_id: str) -> list[StageStep]:
        _plan_ref, plan = self._plan(proposal_id)
        self._check_evaluator(plan)
        # a gating stage left "running" before its first dispatch resumes (no trial ran)
        self._resume_frozen(proposal_id, plan)
        self._settle_orphan_queue(proposal_id)
        head = self._evolution(proposal_id)
        if head["state"] == "draft":
            if _needs_review(head):
                return self.status(proposal_id)  # class B: the operator's review first
            if not self.issuer.human and not only_class_a(head["data"].get("classification") or {}):
                # IC-30 (A): the nightly identity screens class A drafts only (harness.screen);
                # a reviewed class B draft is still the operator's screen (harness.review)
                self.last_stop = {"stage": "screening", "code": "OPERATOR_SCREEN"}
                return self.status(proposal_id)
            # screen gate: protected surfaces, LEAK_GATE (MetaHarness.screen); a refusal stops
            # here. The operator's runner screens as the operator, the nightly one as itself.
            screener = self.operator if self.issuer.human else self.actor
            self.local.meta.screen(screener, proposal_id)
            head = self._evolution(proposal_id)
        if head["state"] != "screened":
            return self.status(proposal_id)
        for entry in plan["stages"]:
            name = entry["stage"]
            state = self._stage_state(proposal_id, name)["state"]
            if state in ("passed", "skipped"):
                continue
            if entry["gate"] == "operator":
                if state == "pending":
                    self._update(proposal_id, name, state="waiting_approval")
                break
            if name == "ablation":
                # never gating (§8.1): an interrupted ablation resumes; one that ended failed,
                # aborted or inconclusive is closed with its findings; either way the holdout
                # gate comes next
                if state in TERMINAL:
                    self._close_ablation(proposal_id, plan, state)
                else:
                    self._ablation(proposal_id, plan, entry)
                continue
            # another auto stage that is not pending stopped (failed, aborted, or interrupted
            # while running) and stays there
            if state != "pending":
                break
            ran = self._stage(proposal_id, plan, entry)
            if not ran or self._stage_state(proposal_id, name)["state"] not in (
                "passed",
                "skipped",
            ):
                break
        return self.status(proposal_id)

    # -- the operator's gate ----------------------------------------------------------------------
    def approve_stage(self, proposal_id: str, stage: str) -> StageStep:
        """Freeze, approve (the operator, the exact digest) and run ``focused`` or ``holdout``.
        Hold SEARCH_BUSY while another runner holds the proposal."""
        if stage not in OPERATOR_GATES:
            raise Hold("META_STATE", "Only the focused and holdout stages have an operator gate")
        self._human_only()
        with stage_lock(self.store, self.scope, proposal_id):
            return self._approve_stage(proposal_id, stage)

    def _human_only(self) -> None:
        if not self.issuer.human:
            raise Hold("APPROVAL_HUMAN", "An operator gate is the human operator's (IC-10)")

    def _approve_stage(self, proposal_id: str, stage: str) -> StageStep:
        _plan_ref, plan = self._plan(proposal_id)
        self._check_evaluator(plan)
        state = self._stage_state(proposal_id, stage)["state"]
        if state != "waiting_approval":
            raise Hold("META_STATE", f"The {stage} stage is {state}; amplai meta search first",
                       details={"stage": stage, "state": state})  # fmt: skip
        head = self._evolution(proposal_id)
        if head["state"] != "screened":
            raise Hold("META_STATE", f"The candidate is {head['state']}; stages need screened")
        if stage == "holdout":  # IC-24: Hold NOT_A_REMOVAL for a sweep that is no removal
            check_removal(self.store, self.scope, self.artifacts, proposal_id)
        entry = next(s for s in plan["stages"] if s["stage"] == stage)
        self._supersede(proposal_id, stage)  # a queued build of this stage is not the one run
        if not self._stage(proposal_id, plan, entry):
            stop = self.last_stop or {}
            raise Hold(str(stop.get("code") or "META_TOKEN_BUDGET"),
                       "The root budget cannot cover this stage", details=stop)  # fmt: skip
        return next(s for s in self.status(proposal_id) if s.stage == stage)

    # -- IC-10: a stage frozen at night, approved by the operator, run the next night -------------
    def queue_stage(self, proposal_id: str, stage: str = "focused") -> dict[str, Any] | None:
        """Build the exact experiment of the ``focused`` stage of a candidate whose screening
        passed and that waits at the focused gate, without any approval (a confirmatory approval
        is the human operator's, IC-10), and keep it in the ``stage-queue`` head
        ``stagequeue-<proposal_id>-<stage>`` (state ``queued``) for ``approve_queued``. Returns
        the queue item ``{proposal_id, stage, queue_id, subject_digest, experiment_id}``, the
        existing one when it is still queued or approved, or None when the root budget cannot
        cover the stage (``last_stop``). Hold META_STATE when the candidate is not there."""
        if stage not in QUEUED_STAGES:
            raise Hold("META_STATE", "Only the focused stage is queued (holdout stays in-process)")
        with stage_lock(self.store, self.scope, proposal_id):
            existing = queue_head(self.store, self.scope, proposal_id, stage)
            if existing is not None and existing["state"] in ("queued", "approved"):
                return _queue_item(existing["data"])
            _plan_ref, plan = self._plan(proposal_id)
            self._check_evaluator(plan)
            self._at_gate(proposal_id, stage, "waiting_approval")
            entry = next(s for s in plan["stages"] if s["stage"] == stage)
            proposal, _head = self._proposal(proposal_id)
            summary = resolve_ref(self.store, self.scope, plan["calibration_summary_ref"])[1]
            rule_summary = summary if entry["case_rule"] == "informative_v1" else None
            _cases, case_ids = self._case_ids(plan, entry, rule_summary)
            reference, note = (None, "")
            if entry["reference"] is not None:
                reference, note = self._reference(proposal_id, proposal, entry)
            arms = 3 if reference is not None else 2
            stop = self._covers(proposal_id, plan, len(case_ids) * arms * entry["repeats"])
            if stop is not None:
                self.last_stop = {"stage": stage, **stop}
                return None
            experiment, _cases, _ids = self._build(
                proposal_id, proposal, plan, entry, reference=reference, note=note
            )
            data = {
                "proposal_id": proposal_id,
                "stage": stage,
                "experiment": experiment,
                "subject_digest": digest(experiment),
                "trials": len(case_ids) * arms * entry["repeats"],
                "queued_by": self.actor.wire(),
                "queued_at": now(),
                "approval_ref": None,
                "approved_by": None,
                "experiment_ref": None,
                "report_ref": None,
            }
            _queue_write(self.store, self.scope, proposal_id, stage, "queued", data)
            return _queue_item(data)

    def approve_queued(
        self, proposal_id: str, stage: str = "focused", *, subject_digest: str | None = None
    ) -> StageStep:
        """``amplai meta approve-stage P --stage focused --queue``: the human operator's approval
        of the exact queued experiment (``issue`` of its digest; ``subject_digest``, when given,
        must be that digest) and its freeze; nothing runs. The stage turns ``frozen`` and waits
        for the next night's confirmation phase (or an operator's ``run_queued``). Hold
        APPROVAL_HUMAN for another issuer, META_STATE without a queued build, QUEUE_DIGEST for
        another digest, CORPUS_CHANGED when its tasks changed since."""
        self._human_only()
        with stage_lock(self.store, self.scope, proposal_id):
            head = queue_head(self.store, self.scope, proposal_id, stage)
            if head is None or head["state"] != "queued":
                raise Hold("META_STATE", f"No queued {stage} stage of this proposal",
                           details={"queue": head["state"] if head else None})  # fmt: skip
            data = copy.deepcopy(head["data"])
            experiment = data["experiment"]
            if digest(experiment) != data["subject_digest"]:
                raise Hold("QUEUE_DIGEST", "The queued experiment changed since it was queued")
            if subject_digest is not None and subject_digest != data["subject_digest"]:
                raise Hold("QUEUE_DIGEST", "The named digest is not the queued experiment's",
                           details={"queued": data["subject_digest"]})  # fmt: skip
            _plan_ref, plan = self._plan(proposal_id)
            self._check_evaluator(plan)
            self._at_gate(proposal_id, stage, "waiting_approval")
            sampling = resolve_ref(self.store, self.scope, experiment["sampling_plan_ref"])[1]
            self.ops.check_corpus(experiment["corpus_ref"], list(sampling["case_ids"]))
            stop = self._covers(proposal_id, plan, int(data["trials"]))
            if stop is not None:
                raise Hold(str(stop["code"]), "The root budget cannot cover this stage",
                           details={"stage": stage, **stop})  # fmt: skip
            approval_ref = self.issuer.issue(experiment)
            frozen = {**experiment, "approval_ref": approval_ref}
            ref = self.local.evaluation.freeze(self.actor, frozen)
            self._update(proposal_id, stage, state="frozen", experiment_ref=ref)
            _queue_write(self.store, self.scope, proposal_id, stage, "approved",
                         {**data, "approval_ref": approval_ref, "approved_by": self.actor.wire(),
                          "experiment_ref": ref, "approved_at": now()},
                         row=head["row_version"])  # fmt: skip
        return next(s for s in self.status(proposal_id) if s.stage == stage)

    def run_queued(self, proposal_id: str, stage: str = "focused") -> StageStep:
        """Run a queued stage the operator approved (state ``frozen``, its experiment head still
        ``frozen``), as this runner's actor; the approval is the operator's, re-checked at every
        trial guard. Holds META_STATE otherwise; SEARCH_BUSY while another runner holds the
        proposal."""
        with stage_lock(self.store, self.scope, proposal_id):
            head = queue_head(self.store, self.scope, proposal_id, stage)
            if head is None or head["state"] != "approved":
                raise Hold("META_STATE", f"No approved queued {stage} stage of this proposal")
            _plan_ref, plan = self._plan(proposal_id)
            self._check_evaluator(plan)
            current = self._at_gate(proposal_id, stage, "frozen")
            ref = current["experiment_ref"]
            if ref != head["data"]["experiment_ref"] or self._experiment_state(ref) != "frozen":
                raise Hold("META_STATE", "The approved experiment is not frozen any more")
            self._check_cases(ref)
            entry = next(s for s in plan["stages"] if s["stage"] == stage)
            data = copy.deepcopy(head["data"])
            _queue_write(self.store, self.scope, proposal_id, stage, "running", data,
                         row=head["row_version"])  # fmt: skip
            try:
                self._update(proposal_id, stage, state="running")
                cases = self._select(plan["corpus_ref"], entry["split"])
                self._run_stage(proposal_id, plan, entry, ref, cases)
            except Exception as exc:
                # never left ``running``: the queued run ended with this reason (a process stop
                # that runs no handler is settled by the next ``advance`` or reconcile)
                settle_queue(self.store, self.scope, proposal_id, stage, failure={
                    "code": _code(exc), "message": str(exc)[:300],
                    "stage_state": self._stage_state(proposal_id, stage)["state"],
                })  # fmt: skip
                raise
            current = self._stage_state(proposal_id, stage)
            if current["state"] == "aborted":  # an aborted run report (no exception)
                settle_queue(self.store, self.scope, proposal_id, stage,
                             failure={"code": "STAGE_ABORTED"})  # fmt: skip
            else:
                settle_queue(self.store, self.scope, proposal_id, stage,
                             report_ref=current["report_ref"])  # fmt: skip
        return next(s for s in self.status(proposal_id) if s.stage == stage)

    def _settle_orphan_queue(self, proposal_id: str) -> None:
        """A queue head found ``running`` while this runner holds the proposal's stage lock was
        left by a runner that stopped without a handler (a crash): it becomes ``ran`` when its
        stage ended with a verdict (e.g. resumed by ``_resume_frozen``), else ``failed`` with the
        reason (``INTERRUPTED`` and the stage state; a stage with dispatched trials still needs
        ``amplai meta reconcile``)."""
        for stage in QUEUED_STAGES:
            head = queue_head(self.store, self.scope, proposal_id, stage)
            if head is None or head["state"] != "running":
                continue
            current = self._stage_state(proposal_id, stage)
            if current["state"] in ("passed", "failed", "inconclusive", "skipped"):
                settle_queue(self.store, self.scope, proposal_id, stage,
                             report_ref=current["report_ref"])  # fmt: skip
            else:
                settle_queue(self.store, self.scope, proposal_id, stage, failure={
                    "code": "INTERRUPTED", "stage_state": current["state"],
                })  # fmt: skip

    def _at_gate(self, proposal_id: str, stage: str, state: str) -> dict[str, Any]:
        """The stage entry when the candidate is screened and the stage is in ``state``."""
        if self._evolution(proposal_id)["state"] != "screened":
            raise Hold("META_STATE", "The candidate is not screened")
        current = self._stage_state(proposal_id, stage)
        if current["state"] != state:
            raise Hold("META_STATE", f"The {stage} stage is {current['state']}",
                       details={"stage": stage, "state": current["state"]})  # fmt: skip
        return current

    def _supersede(self, proposal_id: str, stage: str) -> None:
        head = queue_head(self.store, self.scope, proposal_id, stage)
        if head is not None and head["state"] == "queued":
            _queue_write(self.store, self.scope, proposal_id, stage, "superseded",
                         dict(head["data"]), row=head["row_version"])  # fmt: skip

    def pending_trials(self, proposal_id: str) -> int:
        """The most trials the next ``advance`` dispatches before it stops (0 when it stops at a
        gate first): the next pending auto stage's cases x arms x repeats; for the ablation,
        every planned variant (2 arms each). Used by the nightly guard before ``advance``."""
        found = self._plan_or_none(proposal_id)
        if found is None:
            return 0
        _ref, plan = found
        summary = resolve_ref(self.store, self.scope, plan["calibration_summary_ref"])[1]
        for entry in plan["stages"]:
            state = self._stage_state(proposal_id, entry["stage"])["state"]
            if state in ("passed", "skipped"):
                continue
            if entry["gate"] == "operator" or state not in ("pending", "running"):
                return 0
            rule_summary = summary if entry["case_rule"] == "informative_v1" else None
            _cases, ids = self._case_ids(plan, entry, rule_summary)
            repeats = int(entry["repeats"])
            if entry["stage"] == "ablation":
                return len(plan["ablation_components"]) * len(ids) * 2 * repeats
            return len(ids) * len(entry["arms"]) * repeats
        return 0

    # -- running one stage ----------------------------------------------------------------------
    def _update(self, proposal_id: str, stage: str, **fields: Any) -> None:
        update_stage(self.store, self.scope, proposal_id, stage, **fields)

    def _case_ids(
        self, plan: dict[str, Any], entry: dict[str, Any], summary: dict[str, Any] | None
    ) -> tuple[list[dict[str, Any]], list[str]]:
        cases = self._select(plan["corpus_ref"], entry["split"], plan)
        ids = select_cases(
            cases, rule=entry["case_rule"], summary=summary, cell_id=plan["cell_id"],
            max_tasks=entry["max_tasks"], domain_of=lambda case: str(case["task_class"]),
        )  # fmt: skip
        return cases, ids

    def _covers(self, root_id: str, plan: dict[str, Any], trials: int) -> dict[str, Any] | None:
        """None when the root budget can cover ``trials`` more reservations, else the reason."""
        limits = plan["root_budget"]
        executor = self.local.evaluation.executor_policy
        reserve = executor.max_trial_tokens if executor is not None else 0
        need = trials * reserve
        spent = 0
        try:
            data = self.store.head(self.scope, "meta-budget", root_id)["data"]
        except RuntimeFault as exc:
            if exc.code != "NOT_FOUND":
                raise
            data = None
        if data is not None:
            allocations = list(data["allocations"].values())
            if any(a["status"] == "unknown" for a in allocations):
                return {"code": "META_USAGE_UNKNOWN", "root": root_id}
            if len(data["experiment_refs"]) >= limits["max_attempts"]:
                return {"code": "META_ATTEMPTS", "root": root_id,
                        "attempts": len(data["experiment_refs"])}  # fmt: skip
            if self.store.clock() - data["started_epoch"] >= limits["max_wall_seconds"]:
                return {"code": "META_WALL_BUDGET", "root": root_id}
            spent = sum(a["tokens"] for a in allocations)
        if spent + need > limits["max_tokens"]:
            return {"code": "META_TOKEN_BUDGET", "root": root_id, "need_tokens": need,
                    "remaining_tokens": limits["max_tokens"] - spent}  # fmt: skip
        return None

    def _discordance(self, summary: dict[str, Any], cell_id: str) -> float | None:
        row = (summary.get("cells") or {}).get(cell_id) or {}
        value = row.get("aa_discordance")
        return float(value) if isinstance(value, int | float) else None

    def _policy(
        self,
        plan: dict[str, Any],
        entry: dict[str, Any],
        n: int,
        summary: dict[str, Any],
        reference: tuple[Ref, int] | None,
        note: str,
    ) -> dict[str, Any]:
        """The analysis policy of one stage experiment (§7.1, §7.5)."""
        measured = self._discordance(summary, plan["cell_id"])
        # an MDE must be positive (§7.1): a measured 0 or no data falls back to 0.5 (§7.5)
        d = measured if measured else ASSUMED_DISCORDANCE
        value = mde(n, d)
        basis = (
            f"Paired binary outcomes per task; A/A discordance {measured!r} of cell "
            f"{plan['cell_id']} from calibration summary {self.summary_ref['id']}"
            if measured
            else "Paired binary outcomes per task; A/A discordance 0.5 assumed "
            f"(calibration discordance of the cell: {measured!r})"
        )
        policy: dict[str, Any] = {
            "method": "paired_binary_conservative",
            "confidence": CONFIDENCE,
            "minimum_tasks": max(2, n),
            "repeats_per_task": entry["repeats"],
            "purpose": entry["purpose"],
            "noninferiority_margin": entry["margin"],
            "safety_failure_limit": 0,
            "missing_policy": "inconclusive",
            "cost_basis": "not_compared",
            "evaluator_version_ref": plan["evaluator_version_ref"],
            "endpoint": entry["endpoint"],
            "mde": value,
            "sample_rationale": (
                f"Stage {entry['stage']} of proposal {plan['proposal_id']}: {n} {entry['split']} "
                f"tasks ({entry['case_rule']}, at most {entry['max_tasks']}), "
                f"{entry['repeats']} repeat(s). {note}\nMDE: {value!r} at n={n}"
            ),
            "variance_basis": basis,
            "sequential_rule": "fixed_sample_safety_abort_only",
        }
        if measured is not None:
            policy["noise_band"] = noise_band(measured, n)
        if entry["minimum_effect"] is not None:
            policy["minimum_effect"] = entry["minimum_effect"]
        if reference is not None:
            policy["reference_arm"] = {
                "composition_ref": reference[0], "kind": "best_of_n", "n": reference[1],
                "cost_match": "tokens",
            }  # fmt: skip
        return policy

    def _reference(
        self, proposal_id: str, proposal: dict[str, Any], entry: dict[str, Any]
    ) -> tuple[tuple[Ref, int] | None, str]:
        """§7.4: n = clamp(round(tokens per trial candidate / baseline), 1, cap) from screening;
        n = 1 is the baseline arm itself, so no reference arm is added then."""
        cap = int((entry["reference"] or {}).get("n") or 1)
        report_ref = self._stage_state(proposal_id, "screening")["report_ref"]
        per_arm: dict[str, list[int]] = {"baseline": [], "candidate": []}
        if report_ref is not None:
            report = self.store.get(self.scope, "eval-report", report_ref)
            for ref in report["run_refs"]:
                trial = self.store.get(self.scope, "eval-trial", ref)
                if (
                    trial["arm"] in per_arm
                    and trial.get("input_tokens") is not None
                    and trial.get("output_tokens") is not None
                ):
                    per_arm[trial["arm"]].append(trial["input_tokens"] + trial["output_tokens"])
        if not per_arm["baseline"] or not per_arm["candidate"] or mean(per_arm["baseline"]) <= 0:
            return None, "Reference arm: no screening token data to cost-match it; none added."
        ratio = mean(per_arm["candidate"]) / mean(per_arm["baseline"])
        n = min(cap, max(1, math.floor(ratio + 0.5)))
        if n == 1:
            return None, (
                f"Reference arm: tokens per trial candidate/baseline {ratio:.3f} gives n = 1, "
                "the baseline arm itself; none added."
            )
        ops = self.ops
        strategy = ops.components.register(
            ops.local.proposer,
            component_id=f"execution_strategy.best_of_n_{n}",
            kind="execution_strategy",
            content={"enabled": ["best_of_n"], "params": {"best_of_n": {"n": n}}},
            source="operator",
            rationale="Budget-matched best-of-n reference arm of a focused stage (§7.4)",
        )
        manifests = ops.manifests
        manifest = manifests.change(
            manifests.of_composition(proposal["baseline_ref"]), execution_strategy=strategy
        )
        ref = manifests.materialize(
            ops.local.proposer, base_composition_ref=proposal["baseline_ref"],
            manifest=manifest, suffix=f"ref-bo{n}",
        )  # fmt: skip
        return (ref, n), (
            f"Reference arm: best_of_n n = {n} (tokens per trial candidate/baseline {ratio:.3f})."
        )

    def _experiment(
        self,
        proposal_id: str,
        proposal: dict[str, Any],
        plan: dict[str, Any],
        entry: dict[str, Any],
        *,
        reference: tuple[Ref, int] | None = None,
        note: str = "",
    ) -> tuple[Ref, dict[str, Any], list[dict[str, Any]], list[str]]:
        """Build, approve (the issuer, exact digest) and freeze one stage experiment."""
        experiment, cases, case_ids = self._build(
            proposal_id, proposal, plan, entry, reference=reference, note=note
        )
        experiment["approval_ref"] = self.issuer.issue(experiment)
        ref = self.local.evaluation.freeze(self.actor, experiment)
        return ref, experiment, cases, case_ids

    def _build(
        self,
        proposal_id: str,
        proposal: dict[str, Any],
        plan: dict[str, Any],
        entry: dict[str, Any],
        *,
        reference: tuple[Ref, int] | None = None,
        note: str = "",
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
        """One stage experiment, not yet approved or frozen (its analysis, sampling and holdout
        policy records are written)."""
        head = self._evolution(proposal_id)
        summary = resolve_ref(self.store, self.scope, plan["calibration_summary_ref"])[1]
        rule_summary = summary if entry["case_rule"] == "informative_v1" else None
        cases, case_ids = self._case_ids(plan, entry, rule_summary)
        if not case_ids:
            raise Hold("NO_INFORMATIVE_TASKS", f"The {entry['stage']} stage selects no task",
                       details={"stage": entry["stage"]})  # fmt: skip
        # the trial executor builds each trial from the loaded task of its case id: before the
        # freeze, every selected task loaded now must be the frozen one (Hold CORPUS_CHANGED)
        self.ops.check_corpus(plan["corpus_ref"], case_ids)
        arms = THREE_ARMS if reference is not None else TWO_ARMS
        policy = self._policy(plan, entry, len(case_ids), summary, reference, note)
        analysis_id, sampling_id = new_id("analysis"), new_id("sampling")
        analysis = self.ops._put("analysis-plan", analysis_id, {
            "analysis_id": analysis_id, "policy": policy,
            "scope_note": (
                f"Stage {entry['stage']} ({entry['purpose']}); success rate only; cost is not "
                "compared (D-088)."
            ),
        })  # fmt: skip
        sampling = self.ops._put("sampling-plan", sampling_id, {
            "sampling_id": sampling_id, "split": entry["split"], "case_ids": case_ids,
            "stage": entry["stage"], "arms": arms, "order_rule": "alternate_by_repeat_v1",
            "case_rule": entry["case_rule"], "max_tasks": entry["max_tasks"],
            "cell_id": plan["cell_id"],
            "calibration_summary_ref": plan["calibration_summary_ref"]
            if entry["case_rule"] == "informative_v1" else None,
        })  # fmt: skip
        holdout_id = new_id("holdout")
        holdout = self.ops._put(
            "holdout-policy", holdout_id, {"policy_id": holdout_id, "sealed": True, "max_uses": 1}
        )
        base = self.store.get(self.scope, "harness-composition", proposal["baseline_ref"])
        app = self._app_of(plan)
        experiment = {
            "schema_version": "3.0.0",
            "experiment_id": new_id("experiment"),
            "scope": self.scope.wire(),
            "proposal_ref": head["data"]["proposal_ref"],
            "baseline_ref": proposal["baseline_ref"],
            "candidate_ref": proposal["candidate_ref"],
            "corpus_ref": plan["corpus_ref"],
            "verifier_ref": app.verifier_refs[next(iter(app.verifier_refs))],
            "environment_ref": base["sandbox_profile_ref"],
            "mode": MODE,
            "primary_endpoint": "task_all_repeats_pass",
            "analysis_plan_ref": analysis,
            "sampling_plan_ref": sampling,
            "safety_stop_rules": ["Any safety failure or unknown effect stops the run"],
            "holdout_policy_ref": holdout,
            "budget": dict(plan["root_budget"]),  # one root budget per proposal (IC-16)
            "frozen_at": now(),
        }
        return experiment, cases, case_ids

    def _check_cases(self, experiment_ref: Ref) -> None:
        """Before running a stage experiment frozen by an earlier process: every case of its
        sampling plan is, as loaded now, the frozen task (Hold CORPUS_CHANGED). ``_stage``
        freezes and runs in one call on one loaded corpus, so its pre-freeze check covers it."""
        experiment = self.store.get(self.scope, "eval-experiment", experiment_ref)
        sampling = resolve_ref(self.store, self.scope, experiment["sampling_plan_ref"])[1]
        self.ops.check_corpus(experiment["corpus_ref"], sampling["case_ids"])

    def _read_report(self, report_ref: Ref) -> tuple[dict[str, Any], dict[str, Any]]:
        report = self.store.get(self.scope, "eval-report", report_ref)
        analysis = read_receipt(
            self.artifacts.read(self.scope, report["analysis_artifact"], trusted=True)
        )
        return report, analysis

    def _stage(self, proposal_id: str, plan: dict[str, Any], entry: dict[str, Any]) -> bool:
        """Freeze and run screening, focused or holdout; False when the root budget cannot
        cover it (nothing frozen, ``last_stop`` set)."""
        name = entry["stage"]
        if name == "holdout":
            # IC-10: holdout stays an in-process operator run; refused before any reference,
            # sampling plan, experiment or approval is built for it
            self._human_only()
        proposal, _head = self._proposal(proposal_id)
        summary = resolve_ref(self.store, self.scope, plan["calibration_summary_ref"])[1]
        rule_summary = summary if entry["case_rule"] == "informative_v1" else None
        _cases, case_ids = self._case_ids(plan, entry, rule_summary)
        reference, note = (None, "")
        if entry["reference"] is not None:
            reference, note = self._reference(proposal_id, proposal, entry)
        arms = 3 if reference is not None else 2
        stop = self._covers(proposal_id, plan, len(case_ids) * arms * entry["repeats"])
        if stop is not None:
            self.last_stop = {"stage": name, **stop}
            return False
        experiment_ref, experiment, cases, _ = self._experiment(
            proposal_id, proposal, plan, entry, reference=reference, note=note
        )
        if name == "holdout":  # IC-01: the experiment bound to the evolution machine
            self.local.meta.approve_experiment(
                self.operator, proposal_id, experiment["approval_ref"], experiment_ref
            )
            self.local.meta.start_offline(self.operator, proposal_id)
        self._update(proposal_id, name, state="running", experiment_ref=experiment_ref)
        self._run_stage(proposal_id, plan, entry, experiment_ref, cases)
        return True

    def _experiment_state(self, experiment_ref: Ref) -> str:
        experiment = self.store.get(self.scope, "eval-experiment", experiment_ref)
        return str(self.store.head(self.scope, "experiment", experiment["experiment_id"])["state"])

    def _resume_frozen(self, proposal_id: str, plan: dict[str, Any]) -> None:
        """Run a gating stage (screening, focused, holdout) found ``running`` whose experiment is
        still ``frozen``: the process stopped between the stage turning ``running`` and the first
        dispatch (``EvaluationService.run`` turns the experiment ``running`` before it reserves
        or dispatches any trial), so no trial ran and nothing is replayed. The candidate must be
        where that stage left it (``screened``; ``offline_running`` for the holdout, whose
        ``start_offline`` precedes the stage turning ``running``). A stage whose experiment left
        ``frozen`` (trials may have been dispatched) stays for ``amplai meta reconcile``. Only the
        human operator's runner resumes the holdout (IC-10)."""
        for entry in plan["stages"]:
            name = entry["stage"]
            if name == "ablation":  # the ablation resumes on its own (``_ablation``)
                continue
            current = self._stage_state(proposal_id, name)
            ref = current["experiment_ref"]
            if current["state"] != "running" or ref is None:
                continue
            if name == "holdout" and not self.issuer.human:
                # IC-10: holdout stays an in-process operator run; the nightly identity never
                # resumes it (left for the operator's advance or ``amplai meta reconcile``)
                return
            if self._experiment_state(ref) != "frozen":
                return
            expected = "offline_running" if name == "holdout" else "screened"
            if self._evolution(proposal_id)["state"] != expected:
                return
            self._check_cases(ref)  # Hold CORPUS_CHANGED: frozen by an earlier process
            cases = self._select(plan["corpus_ref"], entry["split"], plan)
            self._run_stage(proposal_id, plan, entry, ref, cases)
            return

    def _run_stage(
        self,
        proposal_id: str,
        plan: dict[str, Any],
        entry: dict[str, Any],
        experiment_ref: Ref,
        cases: list[dict[str, Any]],
    ) -> None:
        """Run a frozen stage experiment, record its report, then score and archive (§9.6, §9.9;
        never changing the stage's verdict)."""
        name = entry["stage"]
        try:
            with self._environment_pins(plan):
                report_ref = self.local.evaluation.run(
                    self.actor, experiment_ref, self.ops.executor, split=entry["split"],
                    parallel=self.parallel,
                )  # fmt: skip
        except RuntimeFault as exc:
            # a queued stage's head ends ``failed`` with the fault's code (no-op otherwise)
            settle_queue(self.store, self.scope, proposal_id, name,
                         failure={"code": exc.code, "stage_state": "aborted"})  # fmt: skip
            self._update(proposal_id, name, state="aborted")
            self.last_stop = {"stage": name, "code": exc.code}
            raise
        if name == "holdout":
            self.local.meta.evaluate(self.operator, proposal_id, report_ref)
        report, analysis = self._read_report(report_ref)
        decision = analysis.get("decision_class")
        findings: list[str] = []
        if name == "screening":
            state, reasons = self._screening_gate(report, analysis, entry)
            findings = [r for r in reasons if r.startswith("HACK_GUARD")]
            if state == "failed" and self.issuer.human:
                self.local.meta.reject(self.operator, proposal_id,
                                       "screening: " + "; ".join(reasons))  # fmt: skip
            elif state == "failed":
                # IC-10: recorded and left for the operator (``amplai meta reject``); the nightly
                # identity never rejects or reviews
                findings.append(f"{SCREENING_FAILED}: " + "; ".join(reasons))
                self.last_stop = {"stage": name, "code": SCREENING_FAILED, "reasons": reasons}
            elif state == "aborted":
                self.last_stop = {"stage": name, "reasons": reasons}
        else:
            state = {"pass": "passed", "fail": "failed", "inconclusive": "inconclusive"}.get(
                report["verdict"], "aborted"
            )
        if self._environment_drift(report):
            findings.append(TASK_ENVIRONMENT_DRIFT)  # §10.5 step 6 (S7b)
        self._update(proposal_id, name, state=state, report_ref=report_ref,
                     decision_class=decision, guard_findings=findings)  # fmt: skip
        rows = self._record(report, cases, exploratory=entry["purpose"] == "exploratory")
        proposal, _head = self._proposal(proposal_id)
        self._after_report(
            proposal_id, plan["cell_id"], name, proposal["candidate_ref"], rows,
            f"{name}:{decision or state}" if name in VERDICT_STAGES else None,
        )  # fmt: skip

    def _after_report(
        self,
        proposal_id: str,
        cell_id: str,
        stage: str,
        composition_ref: Ref,
        rows: list[dict[str, Any]],
        verdict: str | None,
    ) -> None:
        """After an evaluated stage report: the ``prediction-score`` of a scored stage when the
        proposal has a prediction (§9.6, ``proposer.score_predictions``; unchanged scores are not
        rewritten) and ``EliteArchive.update`` (§9.9: elites from development rows only; focused
        and holdout reach the archive only as the operator-only lineage verdict). Both are
        idempotent. A failure of either is a finding of that stage (``PREDICTION_SCORE <stage>:
        <code>`` / ``ELITE_ARCHIVE <stage>: <code>``) and never changes the stage's verdict; a
        process stop (BaseException) propagates."""
        from .archive import EliteArchive
        from .proposer import _prediction, score_predictions

        findings: list[str] = []
        if stage in SCORED_STAGES:
            try:
                if _prediction(self.store, self.scope, proposal_id) is not None:
                    score_predictions(self.store, self.scope, proposal_id, stage)
            except Exception as exc:  # never the stage verdict
                findings.append(f"{SCORE_FINDING} {stage}: {_code(exc)}")
        try:
            EliteArchive(self.store, self.scope).update(
                cell_id, composition_ref=composition_ref, stage_metrics=rows,
                proposal_id=proposal_id, verdict=verdict,
            )  # fmt: skip
        except Exception as exc:  # never the stage verdict
            findings.append(f"{ARCHIVE_FINDING} {stage}: {_code(exc)}")
        if findings:
            known = self._stage_state(proposal_id, stage)["guard_findings"]
            self._update(proposal_id, stage, guard_findings=[*known, *findings])

    def _screening_gate(
        self, report: dict[str, Any], analysis: dict[str, Any], entry: dict[str, Any]
    ) -> tuple[str, list[str]]:
        """§8.1: advance iff the class is not regression, no safety failure and no hack guard.
        Attribution as IC-18 (a): a candidate-arm safety failure rejects; a safety failure of
        another arm, an unknown effect or any other stop reason only stops (``aborted``, no
        verdict about the candidate)."""
        reasons: list[str] = []
        if analysis.get("decision_class") == "regression":
            reasons.append("decision class regression")
        by_arm = analysis.get("safety_failures_by_arm") or {}
        if (by_arm.get("candidate") or 0) > 0:
            reasons.append("candidate safety failure")
        trials = [self.store.get(self.scope, "eval-trial", r) for r in report["run_refs"]]
        rows = [self.metrics.trial(t) for t in trials]
        guards = self.metrics.guards(
            [r for r in rows if r.get("arm") == "baseline"],
            [r for r in rows if r.get("arm") == "candidate"],
            entry["hack_guards"],
        )
        reasons += ["HACK_GUARD: " + g for g in guards]
        if reasons:
            return "failed", reasons
        unsafe = any(g.get("outcome") != "pass" for g in report["safety_gate_results"])
        if report["verdict"] == "aborted" or unsafe:
            return "aborted", ["run stopped: " + ", ".join(analysis.get("reasons") or [])]
        return "passed", []

    # -- ablation (IC-02, IC-19 A) ---------------------------------------------------------------
    def _ablation(self, proposal_id: str, plan: dict[str, Any], entry: dict[str, Any]) -> bool:
        """One derived proposal per changed component, each run on its own root. Never gating
        (§8.1): a variant that cannot be built, frozen or run (any exception of that variant, not
        only a RuntimeFault), or whose run ended ``aborted``, is recorded in the parent's
        ``guard_findings`` as ``ABLATION <component>: <code>`` and the next one goes on; the stage
        ends ``passed`` when some variant passed, else ``skipped`` (nothing measured).

        An ablation found in a non-terminal state other than ``pending`` (the process stopped
        inside it) resumes: a component with a finding or a variant with a finished stage-run head
        is not run again, a recorded variant continues where it stopped, and a variant submitted
        but not yet recorded is adopted. A process stop (BaseException) leaves it ``running``."""
        names = list(plan["ablation_components"])
        if not names:
            self._update(proposal_id, "ablation", state="skipped")
            return True
        parent, _ = self._proposal(proposal_id)
        current = self._stage_state(proposal_id, "ablation")
        resumed = current["state"] != "pending"
        if current["state"] != "running":
            self._update(proposal_id, "ablation", state="running")
        derived: list[str] = list(current["ablation_proposals"]) if resumed else []
        findings: list[str] = list(current["guard_findings"]) if resumed else []
        noted = {_finding_component(f) for f in findings}
        variants: dict[str, str] = {}
        for d in derived:
            left_out = self._left_out_of(parent, d)
            if left_out is not None:
                variants.setdefault(left_out, d)
        plan_ref, _ = self._plan(proposal_id)
        for name in names:
            if name in noted:
                continue
            code = self._variant(
                proposal_id, parent, plan_ref, plan, entry, name, variants.get(name), derived,
                resumed=resumed,
            )  # fmt: skip
            if code is not None:
                findings.append(f"{ABLATION_FINDING} {name}: {code}")
                self._update(proposal_id, "ablation", guard_findings=list(findings))
        # §10.5 step 6 (S7b): a variant that ran into a drifted task environment
        if TASK_ENVIRONMENT_DRIFT not in findings and any(self._variant_drift(d) for d in derived):
            findings.append(TASK_ENVIRONMENT_DRIFT)
            self._update(proposal_id, "ablation", guard_findings=list(findings))
        # contributions are recorded per derived proposal; they never gate (§8.1)
        measured = any(self._variant_passed(d) for d in derived)
        self._update(proposal_id, "ablation", state="passed" if measured else "skipped")
        return True

    def _variant(
        self,
        proposal_id: str,
        parent: dict[str, Any],
        plan_ref: Ref,
        plan: dict[str, Any],
        entry: dict[str, Any],
        name: str,
        known: str | None,
        derived: list[str],
        *,
        resumed: bool,
    ) -> str | None:
        """Build (or adopt) and run the leave-one-out variant of ``name``: None when it passed,
        else the code of why it has no passing report. ``derived`` is the parent's recorded list,
        extended here (and in the stage-run head) when a variant is made."""
        try:
            derived_id = known
            if derived_id is None and resumed:
                derived_id = self._orphan(plan_ref, parent, name, set(derived))
            if derived_id is None:
                # e.g. MANIFEST_COMBINATION, DERIVED_PROPOSAL
                derived_id = self.ops.derived_proposal(
                    proposal_id, slot=name, reason="ablation stage: leave one out " + name
                )
            if derived_id not in derived:
                derived.append(derived_id)
                self._update(proposal_id, "ablation", ablation_proposals=list(derived))
        except Exception as exc:  # §8.1: one variant never stops the ablation
            return _code(exc)
        return self._derived_stage(proposal_id, parent, derived_id, plan, entry)

    def _variant_passed(self, derived_id: str) -> bool:
        try:
            return bool(self._stage_state(derived_id, "ablation")["state"] == "passed")
        except RuntimeFault:
            return False

    def _variant_drift(self, derived_id: str) -> bool:
        """Whether the variant's recorded report holds a drifted task-environment trial."""
        try:
            report_ref = self._stage_state(derived_id, "ablation")["report_ref"]
            if report_ref is None:
                return False
            return self._environment_drift(self.store.get(self.scope, "eval-report", report_ref))
        except RuntimeFault:
            return False

    def _left_out_of(self, parent: dict[str, Any], derived_id: str) -> str | None:
        try:
            return self._left_out(parent, self._proposal(derived_id)[0])
        except RuntimeFault:
            return None

    def _close_ablation(self, proposal_id: str, plan: dict[str, Any], state: str) -> None:
        """An ablation that ended ``failed``, ``aborted`` or ``inconclusive`` never gates
        (§8.1): every planned component without a passed variant or a finding gets ``ABLATION
        <component>: <state>`` and the stage ends ``passed`` (some variant passed) or
        ``skipped``. Nothing runs."""
        parent, _ = self._proposal(proposal_id)
        current = self._stage_state(proposal_id, "ablation")
        findings = list(current["guard_findings"])
        noted = {_finding_component(f) for f in findings}
        passed = [d for d in current["ablation_proposals"] if self._variant_passed(d)]
        measured = {self._left_out_of(parent, d) for d in passed}
        for name in plan["ablation_components"]:
            if name not in noted and name not in measured:
                findings.append(f"{ABLATION_FINDING} {name}: {state}")
        self._update(proposal_id, "ablation", state="passed" if passed else "skipped",
                     guard_findings=findings)  # fmt: skip

    def _left_out(self, parent: dict[str, Any], derived: dict[str, Any]) -> str | None:
        """The component a leave-one-out proposal puts back at the parent baseline's."""
        manifests = self.ops.manifests
        try:
            changes = manifests.diff(
                manifests.of_composition(parent["candidate_ref"]),
                manifests.of_composition(derived["candidate_ref"]),
            )
        except RuntimeFault:
            return None
        return component_name(changes[0].slot) if len(changes) == 1 else None

    def _orphan(
        self, plan_ref: Ref, parent: dict[str, Any], name: str, known: set[str]
    ) -> str | None:
        """A leave-one-out proposal of ``name`` for this plan that the parent's stage-run head
        does not name: ``derived_proposal`` and the head update are two transactions, so a stop
        between them leaves it unrecorded. Only a draft without a stage-run head is adopted."""
        for _ref, value in self.store.list_objects(self.scope, "harness-change-proposal"):
            other = str(value.get("proposal_id"))
            if value.get("experiment_plan_ref") != plan_ref or other in known:
                continue
            if self._run_or_none(other) is not None or self._evolution(other)["state"] != "draft":
                continue
            if self._left_out(parent, value) == name:
                return other
        return None

    def _derived_head(self, parent_id: str, derived_id: str) -> dict[str, Any]:
        """The ablation entry of the derived proposal's stage-run head, which is created once
        (a rerun reads it back)."""
        plan_ref, _ = self._plan(parent_id)
        run = {
            "plan_ref": plan_ref, "current": "ablation",
            "stages": {"ablation": _empty_stage()}, "bound_to_evolution": "holdout",
        }  # fmt: skip
        validate_stage_run(run)
        with self.store.tx() as db:
            try:
                head = self.store.head(self.scope, RUN_KIND, "stagerun-" + derived_id, db=db)
            except RuntimeFault as exc:
                if exc.code != "NOT_FOUND":
                    raise
                self.store.cas(db, self.scope, RUN_KIND, "stagerun-" + derived_id, 0, "planned",
                               run)  # fmt: skip
                return _empty_stage()
        if head["data"]["plan_ref"] != plan_ref:
            raise Hold("DERIVED_PROPOSAL", "The derived stage run names another stage plan")
        state: dict[str, Any] = head["data"]["stages"].get("ablation") or _empty_stage()
        return state

    def _derived_stage(
        self,
        parent_id: str,
        parent: dict[str, Any],
        derived_id: str,
        plan: dict[str, Any],
        entry: dict[str, Any],
    ) -> str | None:
        """The ablation experiment of one derived proposal, on its own budget root: None when the
        variant passed (now or in an earlier run), else the code of why it did not. No exception
        of one variant leaves here (§8.1: the ablation never gates; a BaseException is a process
        stop and does); its stage-run head, when it has one, ends ``aborted`` with that code."""
        try:
            return self._derived_run(parent_id, parent, derived_id, plan, entry)
        except Exception as exc:
            code = _code(exc)
            with contextlib.suppress(RuntimeFault):  # the finding is recorded either way
                if (
                    self._run_or_none(derived_id) is not None
                    and self._stage_state(derived_id, "ablation")["state"] not in TERMINAL
                ):
                    self._update(derived_id, "ablation", state="aborted", guard_findings=[code])
            return code

    def _derived_run(
        self,
        parent_id: str,
        parent: dict[str, Any],
        derived_id: str,
        plan: dict[str, Any],
        entry: dict[str, Any],
    ) -> str | None:
        derived, _ = self._proposal(derived_id)
        if not is_derived(self.store, self.scope, derived):
            raise Hold("DERIVED_PROPOSAL", "Not a derived proposal of this stage plan")
        check_derived(self.store, self.scope, self.ops.manifests, parent, derived)
        state = self._derived_head(parent_id, derived_id)
        if state["state"] in TERMINAL:  # finished by an earlier run
            if state["state"] == "passed":
                return None
            return str((state["guard_findings"] or [state["state"]])[0])
        experiment_ref: Ref | None = state["experiment_ref"]
        if experiment_ref is None:
            summary = resolve_ref(self.store, self.scope, plan["calibration_summary_ref"])[1]
            _cases, case_ids = self._case_ids(plan, entry, summary)
            stop = self._covers(derived_id, plan, len(case_ids) * 2 * entry["repeats"])
            if stop is not None:
                raise Hold(str(stop["code"]), "The derived root cannot cover the ablation",
                           details=stop)  # fmt: skip
            experiment_ref, _, cases, _ = self._experiment(
                derived_id, derived, plan, entry,
                note=f"Leave-one-out ablation of proposal {parent_id} (IC-02, IC-19 A).",
            )  # fmt: skip
            self._update(derived_id, "ablation", state="running", experiment_ref=experiment_ref)
        else:
            # frozen by an earlier process: run it if it never started, read its report if it
            # ended before the stage-run head was updated; a dispatched one is never replayed
            # (EvaluationService.run holds EXPERIMENT_REPLAY; the operator reconciles it)
            cases = self._select(plan["corpus_ref"], entry["split"], plan)
            ended = self._evaluated(experiment_ref)
            if ended is not None:
                return self._derived_report(derived_id, ended, cases)
        self._check_cases(experiment_ref)
        with self._environment_pins(plan):
            report_ref = self.local.evaluation.run(
                self.actor, experiment_ref, self.ops.executor, split=entry["split"],
                parallel=self.parallel,
            )  # fmt: skip
        return self._derived_report(derived_id, report_ref, cases)

    def _evaluated(self, experiment_ref: Ref) -> Ref | None:
        """The report of a stage experiment whose run ended (experiment head ``evaluated``)."""
        experiment = self.store.get(self.scope, "eval-experiment", experiment_ref)
        head = self.store.head(self.scope, "experiment", experiment["experiment_id"])
        if head["state"] != "evaluated":
            return None
        report: Ref = head["data"]["report_ref"]
        return report

    def _derived_report(
        self, derived_id: str, report_ref: Ref, cases: list[dict[str, Any]]
    ) -> str | None:
        """Record the variant's report; ``aborted`` (its run stopped) when the verdict is."""
        report, analysis = self._read_report(report_ref)
        state = "aborted" if report["verdict"] == "aborted" else "passed"
        self._update(derived_id, "ablation", state=state, report_ref=report_ref,
                     decision_class=analysis.get("decision_class"))  # fmt: skip
        rows = self._record(report, cases, exploratory=True)
        derived, _head = self._proposal(derived_id)
        plan = self.store.get(self.scope, PLAN_KIND, derived["experiment_plan_ref"])
        # §9.9: the variant's development rows (no prediction to score: a derived proposal has none)
        self._after_report(derived_id, plan["cell_id"], "ablation", derived["candidate_ref"],
                           rows, None)  # fmt: skip
        return None if state == "passed" else state

    # -- after a run: trial metrics and the baseline-reuse cache (§8.5) --------------------------
    def _record(
        self, report: dict[str, Any], cases: list[dict[str, Any]], *, exploratory: bool
    ) -> list[dict[str, Any]]:
        """The ``trial-metrics`` record of every trial of the report (returned) and, for an
        exploratory stage, its baseline-reuse cache entry (§8.5)."""
        by_id = {c["case_id"]: c for c in cases}
        rows: list[dict[str, Any]] = []
        for ref in report["run_refs"]:
            rows.append(self.store.get(self.scope, "trial-metrics", self.metrics.record(ref)))
            if exploratory:
                self._cache(ref, by_id)
        return rows

    def cache_key(
        self, trial: dict[str, Any], receipt: dict[str, Any], case: dict[str, Any]
    ) -> dict[str, Any] | None:
        """§8.5 key of one trial, rebuilt from its composition, receipt and corpus case; None
        when the harness sha is unknown or the rebuilt key differs from the receipt's."""
        sha = receipt.get("harness_sha")
        artifact = (case.get("artifact_ref") or {}).get("digest")
        snapshot = receipt.get("model_snapshot")
        if not isinstance(sha, str) or sha == UNKNOWN_SHA or not artifact:
            return None
        if not isinstance(snapshot, dict):
            return None
        composition = self.store.get(self.scope, "harness-composition", trial["composition_ref"])
        model = self.store.get(self.scope, "model-profile", composition["model_profile_ref"])
        key = {
            "manifest_digest": digest({k: composition.get(k) for k in CARRIER_FIELDS}),
            "cell": {
                "provider_model_id": snapshot.get("provider_model_id"),
                "reasoning_profile": model.get("reasoning_profile"),
                "driver_version": snapshot.get("driver_version"),
                "image": snapshot.get("image"),
            },
            "task_artifact_digest": artifact,
            "corpus_version": receipt.get("corpus_version"),
            "harness_sha": sha,
            "strategy": receipt.get("strategy"),
            "repeat_slot": trial["repeat"],
        }
        return key if digest(key) == receipt.get("cache_key") else None

    def _cache(self, trial_ref: Ref, cases: dict[str, dict[str, Any]]) -> None:
        trial = self.store.get(self.scope, "eval-trial", trial_ref)
        case = cases.get(trial["task_id"])
        if (
            case is None
            or trial["success"] is None
            or trial["unknown_effects"]
            or not trial["artifact_refs"]
        ):
            return
        receipt = read_receipt(
            self.artifacts.read(self.scope, trial["artifact_refs"][0], trusted=True)
        )
        key = self.cache_key(trial, receipt, case)
        if key is None:
            return
        cache_id = "obs-" + digest(key)[7:31]
        with self.store.tx() as db:
            try:
                head = self.store.head(self.scope, CACHE_KIND, cache_id, db=db)
                row, data = head["row_version"], head["data"]
            except RuntimeFault as exc:
                if exc.code != "NOT_FOUND":
                    raise
                row, data = 0, {"key": key, "trial_refs": [], "calibration_trial_refs": []}
            if trial_ref in data["trial_refs"]:
                return
            self.store.cas(db, self.scope, CACHE_KIND, cache_id, row, "active",
                           {**data, "trial_refs": [*data["trial_refs"], trial_ref]})  # fmt: skip
