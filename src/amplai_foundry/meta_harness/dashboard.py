"""Operator dashboard: a read-only feed over the stored records and static HTML pages (Work 033 S15;
interfaces.md §2.16, §3.13, §8.10, §11.2).

``build_feed(store, scope)`` reads records and returns a ``DashboardFeed`` (a frozen dataclass tree,
never stored). ``render_pages(feed)`` turns it into the pages of §11.2, ``write_site(feed, out)``
writes them (and, with ``feed_json``, ``feed.json`` for tests). ``amplai meta dashboard`` is
``runtime/meta_commands/dashboard.py``.

Read-only: the build reads ``objects`` and ``heads`` through ``Store.list_objects`` and SELECTs and
the verifier-trust analysis artifacts of reports through ``ArtifactStore.read``; it writes no
record, head, event or artifact. It never reads ``harness-trace``, ``leak-index``, case payload
artifacts (prompts, hidden digests) or ``judge-label-set`` items.

Operator view, not a proposer or decider input (§2.16): it reads validation and holdout results
too, but it names a validation or holdout task only by a pseudonym ("holdout task #3") and replaces
the id in every displayed free text with "[withheld]" (``_Names``), so no task id of a split other
than development appears; the ids of those tasks are what the leak index is built from (§2.7).

Choices where the contract is silent (reported with the slice):

- Matrix cell figures come from the newest ``calibration-summary`` that names the cell. The API
  equivalent cost per solved task is the sum of the ``api_cost`` of the cell's calibration
  ``trial-metrics`` of that plan over the solved count; any unpriced row makes it "unpriced"
  (D-094, descriptive only).
- Best composition of a cell: among the compositions with development ``trial-metrics`` on at
  least ``MIN_TASKS`` (4, as the elite archive's ``MIN_N``) tasks, the highest mean per-task pass
  rate (the unit is the task, as in the evaluator); a tie goes to the baseline, then to fewer
  tokens per solved task. The delta is over the tasks both it and the baseline (the first
  composition of the cell's calibration plan) ran, so it is paired.
- Ablation contribution of a component = the baseline arm minus the candidate arm of its
  leave-one-out experiment (what removing the component costs), per task, from ``trial-metrics``.
- A promoted candidate is "stale: re-derive" when its release component's router differs from the
  router of the latest installed composition it derives from (``<id>__<suffix>``, ``releases``).
- Waiting for the operator: ``draft`` class B without a review, ``screened``, ``offline_evaluated``
  with verdict ``pass`` (canary approval), ``canary_approved``, ``promotion_pending``; stage-run
  stages in ``waiting_approval``; evaluator changes ``proposed``/``qualified``; the queued
  confirmations of ``nightly-run`` heads; proposal roots with an ``unknown`` allocation.
"""

from __future__ import annotations

import dataclasses
import hashlib
import html
import json
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Any, TypeGuard

from ..runtime.contracts.identity import now
from ..runtime.errors import RuntimeFault
from ..runtime.storage.store import Scope, Store

Ref = dict[str, Any]
DEVELOPMENT = "development"
MIN_TASKS = 4  # a composition is "best" only on at least this many development tasks
BASELINE_RELEASE_PREFIX = "local-baseline-"
CANDIDATE_SEP = "__"  # releases.CANDIDATE_SEP: <installed composition id>__<suffix>
STAGE_ORDER = ("screening", "focused", "ablation", "holdout")
EFFORT_ORDER = (
    "provider-default",
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
)
LAYERS = ("L1", "L2", "L3", "L4", "L5", "L6", "L7", "L8")
STRATEGY_IDS = frozenset(
    {
        "single",
        "repair_loop",
        "workgraph_split",
        "plan_execute",
        "best_of_n",
        "generator_reviewer",
        "cascade",
        "orchestrator",
        "parallel_readonly",
        "vote",
    }
)
PAGE_LINKS = (
    ("index.html", "Matrix"),
    ("lineage.html", "Lineage"),
    ("approvals.html", "Approvals"),
    ("corpus.html", "Corpus"),
    ("experiments.html", "Experiments"),
    ("layers.html", "Layers"),
    ("strategies.html", "Strategies"),
    ("judges.html", "Judges"),
    ("budget.html", "Budget"),
    ("evaluation.html", "Evaluation"),
)
STANDING_ACTION = "nightly.explore"
NO_DATA = "no data"
TEXT_LIMIT = 400


# ===== feed =====================================================================================
@dataclass(frozen=True)
class CostPerSolved:
    microunits: float | None
    currency: str | None
    status: str  # "estimated" | "unpriced:<status>" | "no_solved" | "no_data"


@dataclass(frozen=True)
class BestComposition:
    composition_id: str
    success: float
    tasks: int
    tokens_per_solved: float | None
    delta: float | None  # best - baseline over the tasks both ran; None when not comparable
    delta_tasks: int
    baseline_id: str | None
    is_baseline: bool


@dataclass(frozen=True)
class MatrixCell:
    cell_id: str
    driver_id: str
    model: str
    effort: str
    page: str
    tasks: int
    pass_rate: float | None
    interval: tuple[float, float] | None
    pass_k: int | None
    pass_k_rate: float | None
    informative: int | None
    median_seconds: float | None
    tokens_per_solved: float | None
    api_cost: CostPerSolved
    best: BestComposition | None
    plan_id: str | None
    summarized_at: str | None


@dataclass(frozen=True)
class ComponentState:
    slot: str
    component: str  # id@version, or "-"
    version: int | None
    state: str  # "on" | "off"
    detail: str


@dataclass(frozen=True)
class ManifestView:
    composition_id: str
    components: tuple[ComponentState, ...]
    error: str | None


@dataclass(frozen=True)
class CompositionStat:
    composition_id: str
    key: str
    tasks: int
    runs: int
    solved: int
    success: float | None
    tokens_per_solved: float | None
    is_baseline: bool
    is_champion: bool
    on_frontier: bool
    manifest: ManifestView | None


@dataclass(frozen=True)
class AblationRow:
    parent_proposal: str
    derived_proposal: str
    component: str
    state: str
    decision_class: str | None
    contribution: float | None  # baseline arm minus candidate arm (what removing it costs)
    tasks: int


@dataclass(frozen=True)
class StageRow:
    proposal_id: str
    stage: str
    state: str
    decision_class: str | None
    experiment_id: str | None
    experiment_page: str | None
    findings: tuple[str, ...]


@dataclass(frozen=True)
class CellPage:
    cell_id: str
    page: str
    driver_id: str
    model: str
    effort: str
    champion: str | None
    compositions: tuple[CompositionStat, ...]
    ablations: tuple[AblationRow, ...]
    stages: tuple[StageRow, ...]
    archive_lineage: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class LineageRow:
    proposal_id: str
    cell_id: str | None
    surface_class: str | None
    status: str
    parent_proposal: str | None
    baseline: str | None
    candidate: str | None
    hypothesis: str
    experiments: tuple[StageRow, ...]
    verdict: str | None
    release: str | None


@dataclass(frozen=True)
class ReleaseView:
    active: str | None
    baseline: bool
    stale: tuple[str, ...]


@dataclass(frozen=True)
class ApprovalRow:
    kind: str
    subject: str
    surface_class: str
    detail: str
    since: str | None


@dataclass(frozen=True)
class StandingApproval:
    approval_id: str
    status: str  # "active" | "expired" | "not_yet_valid" | "revoked" | "undated"
    approved_by: str | None
    approved_at: str | None
    valid_from: str | None
    valid_until: str | None
    detail: dict[str, Any]


@dataclass(frozen=True)
class DomainRow:
    domain: str
    splits: dict[str, int]
    tasks: int
    measured: int
    mean_pass_rate: float | None
    classes: dict[str, int]


@dataclass(frozen=True)
class CorpusView:
    corpus_id: str
    version: str | None
    domains: tuple[DomainRow, ...]
    split_counts: dict[str, int]
    saturated: tuple[str, ...]  # development ids only
    saturated_withheld: int
    flaky: tuple[str, ...]
    flaky_withheld: int


@dataclass(frozen=True)
class TaskResult:
    label: str
    split: str
    baseline: tuple[int, int] | None  # (passes, runs)
    candidate: tuple[int, int] | None
    reference: tuple[int, int] | None
    regression: bool


@dataclass(frozen=True)
class ExperimentView:
    experiment_id: str
    page: str
    proposal_id: str | None
    state: str
    verdict: str | None
    decision_class: str | None
    endpoint: str | None
    purpose: str | None
    delta: float | None
    interval: tuple[float, float] | None
    task_count: int | None
    safety_failures: int | None
    reasons: tuple[str, ...]
    stage: str | None
    splits: tuple[str, ...]
    tasks: tuple[TaskResult, ...]


@dataclass(frozen=True)
class TableView:
    table_id: str
    rows: int | None
    options: tuple[str, ...]
    entries: int
    specialised: int  # entries whose bucket is not the global one
    pooled: int  # entries of the global bucket (every option borrows from nothing coarser)
    top: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class LayerView:
    layer: str
    decisions: int
    coverage: float | None
    specialised_decisions: int
    pooled_decisions: int
    chosen: dict[str, int]
    tables: tuple[TableView, ...]
    options: tuple[dict[str, Any], ...]  # per option: n, successes, rate, tokens mean
    regret: dict[str, Any] | None
    note: str | None


@dataclass(frozen=True)
class StrategyRow:
    strategy: str
    cell_id: str
    n: int
    tasks: int
    success: float | None
    pass_k: int | None
    pass_k_rate: float | None
    tokens_per_solved: float | None
    seconds_per_solved: float | None
    metrics: dict[str, float | None]


@dataclass(frozen=True)
class JudgeQualificationRow:
    judge_id: str
    version: str
    question_type: str
    n: int | None
    accuracy: float | None
    accuracy_interval: tuple[float, float] | None
    brier: float | None
    ece_10: float | None
    repeat_agreement: float | None
    status: str
    qualified_at: str | None


@dataclass(frozen=True)
class JudgeCallRow:
    judge_id: str
    version: str
    question_type: str
    calls: int
    tokens: int | None
    cost_microunits: int | None
    median_latency_ms: float | None


@dataclass(frozen=True)
class NightRow:
    date: str
    budget_trials: int | None
    shares: dict[str, Any]
    cells: tuple[str, ...]
    pilot: bool | None
    phase: str | None
    trials: int | None
    stopped: str | None
    proposals: int
    queued: int
    finished_at: str | None


@dataclass(frozen=True)
class QuotaRow:
    driver_id: str
    night: str | None
    window: str
    runs: int | None
    input_tokens: int | None
    output_tokens: int | None
    cached_input_tokens: int | None
    rate_limit_events: int | None
    limit_errors: int | None
    first_signal_at: str | None


@dataclass(frozen=True)
class HeadroomRow:
    driver_id: str
    tokens: int | None
    basis: str  # "at first limit signal" | ">= max observed"
    suggested_trials: int | None


@dataclass(frozen=True)
class BudgetView:
    nights: tuple[NightRow, ...]
    quota: tuple[QuotaRow, ...]
    headroom: tuple[HeadroomRow, ...]
    budget_trials: int | None  # B of the newest nightly plan
    configured_trials: int | None  # B of local.json meta.nightly
    keep_operator_share: float | None
    median_tokens_per_trial: float | None


@dataclass(frozen=True)
class EvaluatorVersionRow:
    version: str
    approved_at: str | None
    requalification: str | None


@dataclass(frozen=True)
class QualityView:
    version: str
    record_id: str
    measured_at: str | None
    since: str | None
    metrics: dict[str, Any]


@dataclass(frozen=True)
class EvaluationView:
    versions: tuple[EvaluatorVersionRow, ...]
    quality: tuple[QualityView, ...]
    changes: tuple[dict[str, Any], ...]
    requalifications: tuple[dict[str, Any], ...]
    trace_drops: dict[str, int]


@dataclass(frozen=True)
class DashboardFeed:
    generated_at: str
    scope: dict[str, str]
    sources: dict[str, int]  # record kind -> records read (0 renders "no data")
    efforts: tuple[str, ...]
    matrix: tuple[MatrixCell, ...]
    cells: tuple[CellPage, ...]
    release: ReleaseView | None
    lineage: tuple[LineageRow, ...]
    approvals: tuple[ApprovalRow, ...]
    standing: tuple[StandingApproval, ...]
    corpora: tuple[CorpusView, ...]
    experiments: tuple[ExperimentView, ...]
    layers: tuple[LayerView, ...]
    strategies: tuple[StrategyRow, ...]
    judge_qualifications: tuple[JudgeQualificationRow, ...]
    judge_calls: tuple[JudgeCallRow, ...]
    budget: BudgetView
    evaluation: EvaluationView
    notes: tuple[str, ...] = field(default_factory=tuple)


def feed_wire(feed: DashboardFeed) -> dict[str, Any]:
    """The feed as plain JSON-able data (``--feed-json``)."""
    wire: dict[str, Any] = dataclasses.asdict(feed)
    return wire


# ===== reading helpers ==========================================================================
def _d(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _l(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _s(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _interval(value: Any) -> tuple[float, float] | None:
    items = _l(value)
    if len(items) != 2:
        return None
    low, high = _num(items[0]), _num(items[1])
    return (low, high) if low is not None and high is not None else None


def _is_ref(value: Any) -> TypeGuard[Ref]:
    return (
        isinstance(value, dict)
        and isinstance(value.get("id"), str)
        and isinstance(value.get("revision"), int)
        and isinstance(value.get("digest"), str)
    )


def _key(ref: Ref) -> str:
    return f"{ref['id']}@{ref['revision']}"


def _mean(values: Iterable[float]) -> float | None:
    items = list(values)
    return sum(items) / len(items) if items else None


def _epoch(value: Any) -> float | None:
    """An ISO-8601 time with an offset (``...Z``) as epoch seconds, else None."""
    from datetime import UTC, datetime

    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC).timestamp() if parsed.tzinfo is not None else None


def _slug(raw: str, used: set[str]) -> str:
    """A file-name-safe form of a record id (a ``:`` in a relative href would read as a scheme)."""
    out = re.sub(r"[^A-Za-z0-9._-]", "_", raw)[:80].strip(".") or "x"
    if out != raw or out in used:
        out = f"{out}-{hashlib.sha1(raw.encode(), usedforsecurity=False).hexdigest()[:8]}"
    used.add(out)
    return out


def _effort_rank(effort: str) -> tuple[int, str]:
    return (EFFORT_ORDER.index(effort), effort) if effort in EFFORT_ORDER else (99, effort)


class _Reader:
    """Read-only access with per-kind caching; every failure of a single record is absorbed."""

    def __init__(self, store: Store, scope: Scope) -> None:
        self.store, self.scope = store, scope
        self._latest: dict[str, dict[str, tuple[Ref, dict[str, Any]]]] = {}
        self._all: dict[str, list[tuple[Ref, dict[str, Any]]]] = {}
        self.sources: dict[str, int] = {}

    def objects(self, kind: str) -> list[tuple[Ref, dict[str, Any]]]:
        if kind not in self._all:
            try:
                rows = self.store.list_objects(self.scope, kind)
            except (RuntimeFault, ValueError):
                rows = []
            self._all[kind] = [(ref, value) for ref, value in rows if isinstance(value, dict)]
        return self._all[kind]

    def latest(self, kind: str) -> dict[str, tuple[Ref, dict[str, Any]]]:
        if kind not in self._latest:
            out: dict[str, tuple[Ref, dict[str, Any]]] = {}
            for ref, value in self.objects(kind):
                if ref["id"] not in out or ref["revision"] > out[ref["id"]][0]["revision"]:
                    out[ref["id"]] = (ref, value)
            self._latest[kind] = out
            self.sources[kind] = len(out)
        return self._latest[kind]

    def heads(self, kind: str) -> list[tuple[str, str, dict[str, Any]]]:
        try:
            with self.store._lock:
                rows = self.store.conn.execute(
                    "SELECT id,state,data FROM heads WHERE tenant=? AND project=? AND kind=? "
                    "ORDER BY id",
                    (*self.scope.keys(), kind),
                ).fetchall()
            out = [(r["id"], r["state"], _d(json.loads(r["data"]))) for r in rows]
        except (ValueError, RuntimeError, OSError):
            out = []
        self.sources["head:" + kind] = len(out)
        return out

    def get(self, kind: str, ref: Any) -> dict[str, Any] | None:
        if not _is_ref(ref):
            return None
        try:
            value = self.store.get(self.scope, kind, ref)
        except (RuntimeFault, ValueError, KeyError, TypeError):
            return None
        return value if isinstance(value, dict) else None

    def resolve(self, ref: Any) -> tuple[str, dict[str, Any]] | None:
        from ..runtime.contracts.semantics import resolve_ref

        if not _is_ref(ref):
            return None
        try:
            kind, value = resolve_ref(self.store, self.scope, ref)
        except (RuntimeFault, ValueError, KeyError, TypeError):
            return None
        return (kind, value) if isinstance(value, dict) else None


def _scrub_tree(value: Any, names: _Names) -> Any:
    """``value`` with every string, dict key and nested field scrubbed: whatever a record carried,
    no id of a validation or holdout task reaches the feed or a page."""
    if isinstance(value, str):
        return names.scrub(value, limit=1 << 20)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.replace(
            value,
            **{
                f.name: _scrub_tree(getattr(value, f.name), names)
                for f in dataclasses.fields(value)
            },
        )
    if isinstance(value, dict):
        return {_scrub_tree(k, names): _scrub_tree(v, names) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return type(value)(_scrub_tree(v, names) for v in value)
    return value


class _Names:
    """What the pages may say about a task: development ids as they are, every other task by a
    pseudonym, and every displayed text with such an id replaced (§2.16, §2.7)."""

    def __init__(self, hidden: dict[str, str]) -> None:
        self.hidden = hidden  # task id -> split
        ordered = sorted(hidden)
        self._number = {task: i + 1 for i, task in enumerate(ordered)}
        pattern = "|".join(re.escape(t) for t in sorted(ordered, key=lambda t: (-len(t), t)))
        self._pattern = (
            re.compile(rf"(?<![A-Za-z0-9._-])(?:{pattern})(?![A-Za-z0-9._-])") if ordered else None
        )

    def task(self, task_id: str) -> str:
        if task_id in self.hidden:
            return f"{self.hidden[task_id]} task #{self._number[task_id]}"
        return task_id

    def scrub(self, text: Any, limit: int = TEXT_LIMIT) -> str:
        out = "" if text is None else str(text)
        if self._pattern is not None:
            out = self._pattern.sub("[withheld]", out)
        return out if len(out) <= limit else out[: limit - 1] + "…"


@dataclass(frozen=True)
class _Row:
    cell: str
    split: str
    task: str
    domain: str
    arm: str
    strategy: str
    source: str
    stage: str | None
    experiment: str | None
    plan: str | None
    trial: Ref | None
    success: bool | None
    tokens: int | None
    seconds: float | None
    cost: dict[str, Any]
    raw: dict[str, Any]


def _rows(reader: _Reader) -> list[_Row]:
    out = []
    for _ref, tm in sorted(reader.latest("trial-metrics").values(), key=lambda r: r[0]["id"]):
        task, cell = _s(tm.get("task_id")), _s(tm.get("cell_id"))
        if task is None or cell is None:
            continue
        tokens = _d(tm.get("tokens"))
        spent = (
            tokens["input"] + tokens["output"]
            if _int(tokens.get("input")) is not None and _int(tokens.get("output")) is not None
            else None
        )
        success = tm.get("success")
        experiment, plan = tm.get("experiment_ref"), tm.get("calibration_plan_ref")
        out.append(
            _Row(
                cell=cell,
                split=_s(tm.get("split")) or "unknown",
                task=task,
                domain=_s(tm.get("domain")) or "",
                arm=_s(tm.get("arm")) or "",
                strategy=_s(tm.get("strategy")) or "",
                source=_s(tm.get("source")) or "",
                stage=_s(tm.get("stage")),
                experiment=experiment["id"] if _is_ref(experiment) else None,
                plan=plan["id"] if _is_ref(plan) else None,
                trial=tm.get("trial_ref") if _is_ref(tm.get("trial_ref")) else None,
                success=success if isinstance(success, bool) else None,
                tokens=spent,
                seconds=_num(tm.get("wall_seconds")),
                cost=_d(tm.get("api_cost")),
                raw=tm,
            )
        )
    return out


def _names(reader: _Reader, rows: list[_Row]) -> _Names:
    hidden: dict[str, str] = {}
    for _ref, index in reader.latest("corpus-task-index").values():
        for task in _l(index.get("tasks")):
            case, split = _s(_d(task).get("case_id")), _s(_d(task).get("split"))
            if case and split and split != DEVELOPMENT:
                hidden[case] = split
    for row in rows:
        if row.split != DEVELOPMENT:
            hidden[row.task] = row.split
    # a task that any record names as non-development is never shown by its id
    return _Names(hidden)


# ===== statistics over rows =====================================================================
Tasks = dict[str, list[int]]  # task -> [runs, passes]


def _tasks(rows: Iterable[_Row]) -> Tasks:
    out: Tasks = {}
    for row in rows:
        if row.success is None:
            continue
        entry = out.setdefault(row.task, [0, 0])
        entry[0] += 1
        entry[1] += int(row.success)
    return out


def _success(tasks: Tasks) -> float | None:
    return _mean(p / r for r, p in tasks.values() if r)


def _pass_k(tasks: Tasks) -> tuple[int | None, float | None]:
    """pass^k over the tasks with the largest run count k (§7.3 unit: every run passes)."""
    k = max((r for r, _ in tasks.values()), default=0)
    complete = [p == r for r, p in tasks.values() if r == k]
    return (k, sum(complete) / len(complete)) if k and complete else (None, None)


def _per_solved(rows: list[_Row], value: str) -> float | None:
    solved = sum(1 for r in rows if r.success)
    values = [getattr(r, value) for r in rows if r.success is not None]
    if not solved or any(v is None for v in values):
        return None
    return round(float(sum(values)) / solved, 1)


def _cost_per_solved(rows: list[_Row]) -> CostPerSolved:
    solved = sum(1 for r in rows if r.success)
    if not rows:
        return CostPerSolved(None, None, "no_data")
    if not solved:
        return CostPerSolved(None, None, "no_solved")
    total, currency = 0, None
    for row in rows:
        if row.success is None:
            continue
        cost = row.cost
        if cost.get("status") != "estimated" or _int(cost.get("cost_microunits")) is None:
            return CostPerSolved(None, None, "unpriced:" + str(cost.get("status") or "unknown"))
        total += int(cost["cost_microunits"])
        currency = _s(cost.get("currency")) or currency
    return CostPerSolved(round(total / solved, 1), currency, "estimated")


# ===== feed builders ============================================================================
def build_feed(
    source: Store | str | Path,
    scope: Scope,
    *,
    now_utc: str | None = None,
    nightly: dict[str, Any] | None = None,
) -> DashboardFeed:
    """The dashboard feed of one scope, read-only. A record kind with no records renders as
    "no data" on its page; a record that cannot be read or has an unexpected shape is skipped.

    ``source`` is an open ``Store`` or a runtime root, which is opened with ``readonly=True`` for
    the build and closed (the nightly runner calls ``build_feed(runtime_root, scope)``; a read-only
    connection takes no owner lock, so a running deployment is not disturbed).

    ``nightly`` is the ``meta.nightly`` object of ``local.json`` (§12.2; ``budget_trials`` and
    ``keep_operator_share`` are the only keys read), shown next to the stored nightly records."""
    if isinstance(source, Store):
        return _build_feed(source, scope, now_utc, nightly)
    with Store(Path(source), readonly=True) as store:
        return _build_feed(store, scope, now_utc, nightly)


def _build_feed(
    store: Store, scope: Scope, now_utc: str | None, nightly: dict[str, Any] | None
) -> DashboardFeed:
    reader = _Reader(store, scope)
    stamp = now_utc or now()
    rows = _rows(reader)
    names = _names(reader, rows)
    builder = _Builder(reader, rows, names, stamp, _d(nightly))
    matrix, cells = builder.matrix_and_cells()
    experiments = builder.experiments()
    lineage = builder.lineage(experiments)
    approvals, standing = builder.approvals()
    feed = DashboardFeed(
        generated_at=stamp,
        scope=scope.wire(),
        sources={},  # filled below, after every reader ran
        efforts=tuple(sorted({m.effort for m in matrix}, key=_effort_rank)),
        matrix=matrix,
        cells=cells,
        release=builder.release(),
        lineage=lineage,
        approvals=approvals,
        standing=standing,
        corpora=builder.corpora(),
        experiments=experiments,
        layers=builder.layers(),
        strategies=builder.strategies(),
        judge_qualifications=builder.judge_qualifications(),
        judge_calls=builder.judge_calls(),
        budget=builder.budget(),
        evaluation=builder.evaluation(),
        notes=(
            "Calibrated figures come from calibration summaries; best compositions and ablation "
            "contributions from development trial-metrics.",
            "API-equivalent cost is descriptive (D-094): a subscription run is not billed "
            "per token.",
            "Tasks of the validation and holdout splits are named by a pseudonym.",
        ),
    )
    feed = dataclasses.replace(feed, sources=dict(sorted(reader.sources.items())))
    cleaned: DashboardFeed = _scrub_tree(feed, names)
    return cleaned


class _Builder:
    def __init__(
        self,
        reader: _Reader,
        rows: list[_Row],
        names: _Names,
        stamp: str,
        nightly: dict[str, Any],
    ) -> None:
        self.r, self.rows, self.names, self.stamp = reader, rows, names, stamp
        self.nightly = nightly
        self.store, self.scope = reader.store, reader.scope
        self._composition_of: dict[str, Ref] | None = None
        self._experiment_pages: dict[str, str] = {}
        self._slugs: set[str] = set()

    # -- compositions and manifests ------------------------------------------------------------
    def composition_of(self, trial: Ref | None) -> Ref | None:
        if self._composition_of is None:
            index: dict[str, Ref] = {}
            for kind in ("eval-trial", "calibration-trial"):
                for ref, value in self.r.objects(kind):
                    found = value.get("composition_ref")
                    if _is_ref(found):
                        index[_key(ref)] = found
            self._composition_of = index
        return self._composition_of.get(_key(trial)) if trial is not None else None

    def manifest(self, ref: Ref) -> ManifestView:
        from .components import ComponentService
        from .manifest import ManifestService

        value = self.r.get("harness-composition", ref)
        name = _s(_d(value).get("composition_id")) or ref["id"]
        try:
            manifest = ManifestService(
                self.store, self.scope, None, ComponentService(self.store, self.scope)
            ).of_composition(ref)
        except (RuntimeFault, KeyError, TypeError, ValueError) as exc:
            return ManifestView(name, (), getattr(exc, "code", type(exc).__name__))
        return ManifestView(
            name, tuple(self.component_state(s, c) for s, c in manifest.flat().items()), None
        )

    def component_state(self, slot: str, ref: Ref | None) -> ComponentState:
        if ref is None:
            return ComponentState(slot, "-", None, "off", "")
        found = self.r.resolve(ref)
        if found is None:
            return ComponentState(slot, _key(ref), ref.get("revision"), "off", "unreadable")
        kind, value = found
        if kind != "harness-component":  # the role prompt carrier (a prompt-bundle)
            return ComponentState(slot, _key(ref), ref["revision"], "on", "")
        content = _d(value.get("content"))
        enabled = content.get("enabled")
        state = "off" if enabled is False else "on"
        detail = ""
        listed = [s for s in _l(content.get("enabled")) if s in STRATEGY_IDS]
        if slot == "execution_strategy" and listed:
            detail = ", ".join(listed)
        return ComponentState(
            slot,
            f"{_s(value.get('component_id')) or ref['id']}@{ref['revision']}",
            _int(value.get("version")) or ref["revision"],
            state,
            detail,
        )

    # -- matrix and cell pages -----------------------------------------------------------------
    def cell_info(self) -> dict[str, dict[str, Any]]:
        return {cid: v for cid, (_r, v) in self.r.latest("harness-cell").items()}

    def summaries(self) -> dict[str, tuple[Ref, dict[str, Any], dict[str, Any]]]:
        """cell -> (summary ref, summary, that cell's entry) of the newest summary naming it."""
        out: dict[str, tuple[Ref, dict[str, Any], dict[str, Any]]] = {}
        for ref, summary in self.r.latest("calibration-summary").values():
            for cell, entry in _d(summary.get("cells")).items():
                if not isinstance(entry, dict):
                    continue
                held = out.get(cell)
                if held is None or str(summary.get("summarized_at") or "") >= str(
                    held[1].get("summarized_at") or ""
                ):
                    out[cell] = (ref, summary, entry)
        return out

    def compositions(
        self, cell: str, baseline: Ref | None, champion: Ref | None
    ) -> tuple[list[CompositionStat], BestComposition | None]:
        groups: dict[str, list[_Row]] = {}
        refs: dict[str, Ref] = {}
        for row in self.rows:
            if row.cell != cell or row.split != DEVELOPMENT or row.success is None:
                continue
            comp = self.composition_of(row.trial)
            if comp is not None:
                groups.setdefault(_key(comp), []).append(row)
                refs[_key(comp)] = comp
        for extra in (baseline, champion):
            if extra is not None:
                refs.setdefault(_key(extra), extra)
                groups.setdefault(_key(extra), [])
        tasks = {k: _tasks(g) for k, g in groups.items()}
        stats: list[tuple[str, CompositionStat]] = []
        for key, group in groups.items():
            t = tasks[key]
            stats.append(
                (
                    key,
                    CompositionStat(
                        composition_id=refs[key]["id"],
                        key=key,
                        tasks=len(t),
                        runs=sum(r for r, _ in t.values()),
                        solved=sum(p for _, p in t.values()),
                        success=_success(t),
                        tokens_per_solved=_per_solved(group, "tokens"),
                        is_baseline=baseline is not None and key == _key(baseline),
                        is_champion=champion is not None and key == _key(champion),
                        on_frontier=False,
                        manifest=None,
                    ),
                )
            )
        frontier = _frontier([s for _k, s in stats])
        ordered = sorted(
            stats, key=lambda p: (not p[1].is_baseline, -(p[1].success or 0.0), p[1].key)
        )
        result = [
            dataclasses.replace(s, on_frontier=s.key in frontier, manifest=self.manifest(refs[k]))
            for k, s in ordered
        ]
        return result, self.best(result, tasks, baseline)

    @staticmethod
    def best(
        stats: list[CompositionStat], tasks: dict[str, Tasks], baseline: Ref | None
    ) -> BestComposition | None:
        eligible = [s for s in stats if s.tasks >= MIN_TASKS and s.success is not None]
        if not eligible:
            return None
        top = min(
            eligible,
            key=lambda s: (
                -round(s.success or 0.0, 9),
                not s.is_baseline,
                s.tokens_per_solved if s.tokens_per_solved is not None else math.inf,
                s.key,
            ),
        )
        base_key = _key(baseline) if baseline is not None else None
        delta, common = None, 0
        if base_key is not None and base_key in tasks:
            if top.key == base_key:
                delta, common = 0.0, top.tasks
            else:
                shared = sorted(set(tasks[top.key]) & set(tasks[base_key]))
                common = len(shared)
                if shared:
                    delta = _mean(
                        tasks[top.key][t][1] / tasks[top.key][t][0]
                        - tasks[base_key][t][1] / tasks[base_key][t][0]
                        for t in shared
                    )
        return BestComposition(
            top.composition_id,
            top.success or 0.0,
            top.tasks,
            top.tokens_per_solved,
            delta,
            common,
            baseline["id"] if baseline is not None else None,
            top.is_baseline,
        )

    def matrix_and_cells(self) -> tuple[tuple[MatrixCell, ...], tuple[CellPage, ...]]:
        info, summaries = self.cell_info(), self.summaries()
        ids = sorted({*info, *summaries, *(r.cell for r in self.rows)})
        used: set[str] = set()
        matrix: list[MatrixCell] = []
        pages: list[CellPage] = []
        for cell in ids:
            meta = info.get(cell, {})
            driver = _s(meta.get("driver_id")) or "unknown"
            model = _s(meta.get("provider_model_id")) or "unknown"
            effort = _s(meta.get("effort")) or "unknown"
            page = "cell-" + _slug(cell, used) + ".html"
            held = summaries.get(cell)
            baseline = self.baseline_of(cell, held)
            champion = self.champion_of(cell)
            comps, best = self.compositions(cell, baseline, champion)
            entry = held[2] if held else {}
            plan_id = (
                held[1].get("plan_ref", {}).get("id")
                if held and _is_ref(held[1].get("plan_ref"))
                else None
            )
            plan_rows = [
                r
                for r in self.rows
                if r.cell == cell
                and r.source == "calibration"
                and plan_id is not None
                and r.plan == plan_id
            ]
            classes = [_s(_d(t).get("class")) for t in _d(entry.get("tasks")).values()]
            pass_k = _d(entry.get("pass_k"))
            matrix.append(
                MatrixCell(
                    cell_id=cell,
                    driver_id=driver,
                    model=model,
                    effort=effort,
                    page=page,
                    tasks=len(_d(entry.get("tasks"))),
                    pass_rate=_num(entry.get("pass_rate")),
                    interval=_interval(entry.get("pass_rate_interval")),
                    pass_k=_int(pass_k.get("k")),
                    pass_k_rate=_num(pass_k.get("rate")),
                    informative=sum(1 for c in classes if c == "informative") if entry else None,
                    median_seconds=_num(entry.get("median_seconds")),
                    tokens_per_solved=_num(entry.get("tokens_per_solved")),
                    api_cost=_cost_per_solved(plan_rows)
                    if entry
                    else CostPerSolved(None, None, "no_data"),
                    best=best,
                    plan_id=plan_id,
                    summarized_at=_s(held[1].get("summarized_at")) if held else None,
                )
            )
            ablations, stages = self.stage_history(cell)
            pages.append(
                CellPage(
                    cell_id=cell,
                    page=page,
                    driver_id=driver,
                    model=model,
                    effort=effort,
                    champion=champion["id"] if champion else None,
                    compositions=tuple(comps),
                    ablations=tuple(ablations),
                    stages=tuple(stages),
                    archive_lineage=tuple(self.archive_lineage(cell)),
                )
            )
        matrix.sort(key=lambda m: (m.driver_id, m.model, _effort_rank(m.effort), m.cell_id))
        return tuple(matrix), tuple(pages)

    def baseline_of(
        self, cell: str, held: tuple[Ref, dict[str, Any], dict[str, Any]] | None
    ) -> Ref | None:
        if held is None:
            return None
        plan = self.r.get("calibration-plan", held[1].get("plan_ref"))
        refs = _l(_d(_d(plan).get("composition_refs")).get(cell))
        return refs[0] if refs and _is_ref(refs[0]) else None

    def champion_of(self, cell: str) -> Ref | None:
        for head_id, _state, data in self.r.heads("elite-archive"):
            if head_id == "archive-" + cell:
                found = _d(data.get("champion")).get("composition_ref")
                return found if _is_ref(found) else None
        return None

    def archive_lineage(self, cell: str) -> list[dict[str, Any]]:
        for head_id, _state, data in self.r.heads("elite-archive"):
            if head_id == "archive-" + cell:
                return [
                    {
                        "child": _d(e.get("child")).get("id"),
                        "parents": [_d(p).get("id") for p in _l(e.get("parents"))],
                        "proposal_id": e.get("proposal_id"),
                        "verdict": e.get("verdict"),
                    }
                    for e in map(_d, _l(data.get("lineage")))
                ]
        return []

    # -- stages and ablation -------------------------------------------------------------------
    def stage_plans(self) -> dict[str, dict[str, Any]]:
        return {
            _s(v.get("proposal_id")) or i: v for i, (_r, v) in self.r.latest("stage-plan").items()
        }

    def stage_history(self, cell: str) -> tuple[list[AblationRow], list[StageRow]]:
        plans = self.stage_plans()
        by_ref = {_key(r): v for r, v in (p for p in self.r.latest("stage-plan").values())}
        ablations: list[AblationRow] = []
        stages: list[StageRow] = []
        for head_id, _state, data in self.r.heads("stage-run"):
            plan = by_ref.get(_key(data["plan_ref"])) if _is_ref(data.get("plan_ref")) else None
            if plan is None:
                continue
            if _s(plan.get("cell_id")) != cell:
                continue
            proposal = head_id.removeprefix("stagerun-")
            entries = _d(data.get("stages"))
            stages.extend(self.stage_rows(proposal, entries))
            for derived in _l(_d(entries.get("ablation")).get("ablation_proposals")):
                row = self.ablation_row(proposal, str(derived), plans)
                if row is not None:
                    ablations.append(row)
        return ablations, stages

    def stage_rows(self, proposal: str, entries: dict[str, Any]) -> list[StageRow]:
        out = []
        for stage in sorted(
            entries, key=lambda s: (STAGE_ORDER.index(s) if s in STAGE_ORDER else 9, s)
        ):
            entry = _d(entries[stage])
            experiment = (
                _d(entry.get("experiment_ref")).get("id")
                if _is_ref(entry.get("experiment_ref"))
                else None
            )
            out.append(
                StageRow(
                    proposal_id=proposal,
                    stage=stage,
                    state=_s(entry.get("state")) or "unknown",
                    decision_class=_s(entry.get("decision_class")),
                    experiment_id=experiment,
                    experiment_page=None,
                    findings=tuple(
                        self.names.scrub(f) for f in _l(entry.get("guard_findings"))[:12]
                    ),
                )
            )
        return out

    def ablation_row(
        self, parent: str, derived: str, plans: dict[str, dict[str, Any]]
    ) -> AblationRow | None:
        head = next((h for h in self.r.heads("stage-run") if h[0] == "stagerun-" + derived), None)
        entry = _d(_d(head[2].get("stages")).get("ablation")) if head else {}
        proposal = self.r.latest("harness-change-proposal").get(derived)
        parent_proposal = self.r.latest("harness-change-proposal").get(parent)
        component = self.left_out(
            parent_proposal[1] if parent_proposal else None, proposal[1] if proposal else None
        )
        experiment = (
            _d(entry.get("experiment_ref")).get("id")
            if _is_ref(entry.get("experiment_ref"))
            else None
        )
        rows = [r for r in self.rows if experiment and r.experiment == experiment]
        base = _tasks(r for r in rows if r.arm == "baseline")
        cand = _tasks(r for r in rows if r.arm == "candidate")
        shared = sorted(set(base) & set(cand))
        contribution = (
            _mean(base[t][1] / base[t][0] - cand[t][1] / cand[t][0] for t in shared)
            if shared
            else None
        )
        return AblationRow(
            parent_proposal=parent,
            derived_proposal=derived,
            component=component or "unknown",
            state=_s(entry.get("state")) or "unknown",
            decision_class=_s(entry.get("decision_class")),
            contribution=contribution,
            tasks=len(shared),
        )

    def left_out(self, parent: dict[str, Any] | None, derived: dict[str, Any] | None) -> str | None:
        from .components import ComponentService
        from .manifest import ManifestService

        if parent is None or derived is None:
            return None
        service = ManifestService(
            self.store, self.scope, None, ComponentService(self.store, self.scope)
        )
        try:
            changes = service.diff(
                service.of_composition(parent["candidate_ref"]),
                service.of_composition(derived["candidate_ref"]),
            )
        except (RuntimeFault, KeyError, TypeError, ValueError):
            return None
        return changes[0].slot if len(changes) == 1 else None

    # -- experiments ---------------------------------------------------------------------------
    def analysis(self, report: dict[str, Any]) -> dict[str, Any]:
        from ..runtime.evidence.cas import ArtifactStore

        artifact = report.get("analysis_artifact")
        if not isinstance(artifact, dict):
            return {}
        try:
            raw = ArtifactStore(self.store).read(self.scope, artifact, trusted=True)
            return _d(json.loads(raw))
        except (RuntimeFault, ValueError, KeyError, TypeError, OSError):
            return {}

    def experiments(self) -> tuple[ExperimentView, ...]:
        out = []
        for experiment_id, state, data in self.r.heads("experiment"):
            page = "experiment-" + _slug(experiment_id, self._slugs) + ".html"
            self._experiment_pages[experiment_id] = page
            experiment = self.r.get("eval-experiment", data.get("experiment_ref")) or {}
            proposal = self.r.resolve(experiment.get("proposal_ref"))
            report = self.r.get("eval-report", data.get("report_ref")) or {}
            analysis = self.analysis(report) if report else {}
            rows = [r for r in self.rows if r.experiment == experiment_id]
            out.append(
                ExperimentView(
                    experiment_id=experiment_id,
                    page=page,
                    proposal_id=_s(proposal[1].get("proposal_id")) if proposal else None,
                    state=state,
                    verdict=_s(report.get("verdict")) or _s(data.get("verdict")),
                    decision_class=_s(analysis.get("decision_class")),
                    endpoint=_s(analysis.get("endpoint")) or _s(experiment.get("primary_endpoint")),
                    purpose=_s(analysis.get("purpose")),
                    delta=_num(analysis.get("candidate_minus_baseline")),
                    interval=_interval(analysis.get("confidence_interval")),
                    task_count=_int(analysis.get("task_count")),
                    safety_failures=_int(analysis.get("safety_failures")),
                    reasons=tuple(self.names.scrub(x) for x in _l(analysis.get("reasons"))[:12]),
                    stage=next((r.stage for r in rows if r.stage), None),
                    splits=tuple(sorted({r.split for r in rows})),
                    tasks=tuple(self.task_results(rows)),
                )
            )
        return tuple(out)

    def task_results(self, rows: list[_Row]) -> list[TaskResult]:
        by_task: dict[str, list[_Row]] = {}
        for row in rows:
            by_task.setdefault(row.task, []).append(row)
        out = []
        for task in sorted(by_task, key=lambda t: (t in self.names.hidden, self.names.task(t))):
            group = by_task[task]
            arms = {
                a: _tasks(r for r in group if r.arm == a).get(task)
                for a in ("baseline", "candidate", "reference")
            }
            base, cand = arms["baseline"], arms["candidate"]
            out.append(
                TaskResult(
                    label=self.names.task(task),
                    split=group[0].split,
                    baseline=(base[1], base[0]) if base else None,
                    candidate=(cand[1], cand[0]) if cand else None,
                    reference=(arms["reference"][1], arms["reference"][0])
                    if arms["reference"]
                    else None,
                    regression=bool(base and cand and cand[1] / cand[0] < base[1] / base[0]),
                )
            )
        return out

    # -- lineage and release -------------------------------------------------------------------
    def lineage(self, experiments: tuple[ExperimentView, ...]) -> tuple[LineageRow, ...]:
        evolution = {i: (s, d) for i, s, d in self.r.heads("evolution")}
        runs = {i.removeprefix("stagerun-"): d for i, _s_, d in self.r.heads("stage-run")}
        plans_by_ref = {_key(r): v for r, v in self.r.latest("stage-plan").values()}
        pages = self._experiment_pages
        out = []
        for proposal_id, (_ref, proposal) in sorted(
            self.r.latest("harness-change-proposal").items()
        ):
            state, head = evolution.get(proposal_id, (None, {}))
            plan = (
                plans_by_ref.get(_key(proposal["experiment_plan_ref"]))
                if _is_ref(proposal.get("experiment_plan_ref"))
                else None
            )
            plan_owner = _s(_d(plan).get("proposal_id"))
            stages = tuple(
                dataclasses.replace(row, experiment_page=pages.get(row.experiment_id or ""))
                for row in self.stage_rows(proposal_id, _d(_d(runs.get(proposal_id)).get("stages")))
            )
            release = _d(_d(head.get("promotion_plan")).get("candidate_release_ref")).get("id")
            out.append(
                LineageRow(
                    proposal_id=proposal_id,
                    cell_id=_s(_d(plan).get("cell_id")),
                    surface_class=_s(proposal.get("surface_class")),
                    status=state or _s(proposal.get("status")) or "unknown",
                    parent_proposal=plan_owner
                    if plan_owner and plan_owner != proposal_id
                    else None,
                    baseline=_d(proposal.get("baseline_ref")).get("id"),
                    candidate=_d(proposal.get("candidate_ref")).get("id"),
                    hypothesis=self.names.scrub(proposal.get("hypothesis")),
                    experiments=stages,
                    verdict=_s(head.get("verdict")),
                    release=release if isinstance(release, str) else None,
                )
            )
        return tuple(out)

    def release(self) -> ReleaseView | None:
        pointer = next((h for h in self.r.heads("release-pointer") if h[0] == "active"), None)
        if pointer is None or not _is_ref(pointer[2].get("release_ref")):
            return None
        ref = pointer[2]["release_ref"]
        baseline = ref["id"].startswith(BASELINE_RELEASE_PREFIX)
        stale: list[str] = []
        release = self.r.get("release-set", ref)
        if release is not None and not baseline:
            latest_by_id = self.r.latest("harness-composition")
            for component_ref in _l(release.get("component_refs")):
                value = self.r.get("harness-composition", component_ref)
                name = _s(_d(value).get("composition_id"))
                if value is None or name is None or CANDIDATE_SEP not in name:
                    continue
                base = latest_by_id.get(name.split(CANDIDATE_SEP, 1)[0])
                if base is not None and base[1].get("router_policy_ref") != value.get(
                    "router_policy_ref"
                ):
                    stale.append(name)
        return ReleaseView(ref["id"], baseline, tuple(sorted(stale)))

    # -- approvals -----------------------------------------------------------------------------
    def approvals(self) -> tuple[tuple[ApprovalRow, ...], tuple[StandingApproval, ...]]:
        proposals = {i: v for i, (_r, v) in self.r.latest("harness-change-proposal").items()}

        def surface(proposal_id: str, data: dict[str, Any] | None = None) -> str:
            found = _s(_d(_d(data).get("classification")).get("surface_class")) or _s(
                _d(proposals.get(proposal_id)).get("surface_class")
            )
            return found or "-"

        rows: list[ApprovalRow] = []
        for proposal_id, state, data in self.r.heads("evolution"):
            what = {
                "screened": "experiment approval",
                "canary_approved": "canary start",
                "promotion_pending": "promotion approval",
            }.get(state)
            if (
                state == "draft"
                and surface(proposal_id, data) == "B"
                and not data.get("review_ref")
            ):
                what = "class B code review (amplai meta review)"
            if state == "offline_evaluated" and data.get("verdict") == "pass":
                what = "canary approval"
            if what:
                rows.append(
                    ApprovalRow(
                        "evolution",
                        proposal_id,
                        surface(proposal_id, data),
                        f"{what} (state {state})",
                        None,
                    )
                )
        evolution = {i: s_ for i, s_, _d_ in self.r.heads("evolution")}
        for head_id, _state, data in self.r.heads("stage-run"):
            proposal_id = head_id.removeprefix("stagerun-")
            for stage, entry in _d(data.get("stages")).items():
                if _d(entry).get("state") == "waiting_approval":
                    rows.append(
                        ApprovalRow(
                            "stage gate",
                            proposal_id,
                            surface(proposal_id),
                            f"stage {stage}",
                            None,
                        )
                    )
                # IC-10: a screening a night ran that failed waits for the operator's reject
                # (the nightly identity never rejects)
                if (
                    stage == "screening"
                    and _d(entry).get("state") == "failed"
                    and evolution.get(proposal_id) == "screened"
                ):
                    found = [
                        f for f in _l(_d(entry).get("guard_findings"))
                        if isinstance(f, str) and f.startswith("SCREENING_FAILED")
                    ]  # fmt: skip
                    rows.append(
                        ApprovalRow(
                            "screening failed",
                            proposal_id,
                            surface(proposal_id),
                            (self.names.scrub(found[0]) if found else "screening failed")
                            + "; reject with amplai meta reject",
                            None,
                        )
                    )
        # IC-10: focused stages a night froze for the operator's --queue approval
        queued: set[str] = set()
        for queue_id, state, data in self.r.heads("stage-queue"):
            proposal_id = _s(data.get("proposal_id")) or queue_id
            stage = _s(data.get("stage")) or "focused"
            queued.add(proposal_id)
            detail = {
                "queued": (
                    f"{stage} stage frozen by a night (subject "
                    f"{_s(data.get('subject_digest')) or '?'}); approve with amplai meta "
                    f"approve-stage {proposal_id} --stage {stage} --queue"
                ),
                "approved": f"{stage} stage approved (--queue); runs on the next night",
            }.get(state)
            if detail is not None:
                rows.append(
                    ApprovalRow(
                        "queued confirmation",
                        proposal_id,
                        surface(proposal_id),
                        detail,
                        _s(data.get("approved_at") or data.get("queued_at")),
                    )
                )
        for change_id, state, data in self.r.heads("evaluator-change"):
            if state in ("proposed", "qualified"):
                rows.append(
                    ApprovalRow(
                        "evaluator change",
                        change_id,
                        "-",
                        ("qualify" if state == "proposed" else "approve")
                        + f" (state {state}); "
                        + self.names.scrub(data.get("reason")),
                        None,
                    )
                )
        night_roots: list[ApprovalRow] = []
        for night, _state, data in self.r.heads("nightly-run"):
            for ref in _l(data.get("queued_confirmations")):
                subject = str(_d(ref).get("proposal_id") or _d(ref).get("id") or "?")
                if subject in queued:  # listed above from its stage-queue head
                    continue
                rows.append(
                    ApprovalRow(
                        "queued confirmation",
                        subject,
                        "-",
                        f"frozen by {night}; waits for the operator",
                        _s(data.get("finished_at")),
                    )
                )
            # IC-18: a night that stopped at an unknown effect lists its roots (below, unless
            # the root's budget already lists it)
            for root in _l(data.get("reconcile_pending")):
                night_roots.append(
                    ApprovalRow(
                        "reconcile pending",
                        str(root),
                        surface(str(root)),
                        f"unknown effect on {night} (IC-18); amplai meta reconcile",
                        _s(data.get("finished_at")),
                    )
                )
        for proposal_id, _state, data in self.r.heads("meta-budget"):
            unknown = [
                a for a in _d(data.get("allocations")).values() if _d(a).get("status") == "unknown"
            ]
            if unknown:
                rows.append(
                    ApprovalRow(
                        "reconcile pending",
                        proposal_id,
                        surface(proposal_id),
                        f"{len(unknown)} allocation(s) with unknown usage (IC-18)",
                        None,
                    )
                )
        listed = {r.subject for r in rows if r.kind == "reconcile pending"}
        for row in night_roots:
            if row.subject not in listed:
                listed.add(row.subject)
                rows.append(row)
        return tuple(rows), self.standing()

    def standing(self) -> tuple[StandingApproval, ...]:
        revoked = {i for i, _s_, d in self.r.heads("meta-approval-state") if d.get("revoked")}
        out = []
        for _ref, value in self.r.latest("meta-approval").values():
            if value.get("action") != STANDING_ACTION:
                continue
            approval_id = _s(value.get("approval_id")) or "?"
            sources = [value, _d(value.get("policy")), _d(value.get("standing"))]

            def pick(name: str, sources: list[dict[str, Any]] = sources) -> Any:
                return next((s[name] for s in sources if name in s), None)

            start, end = _s(pick("valid_from")), _s(pick("valid_until"))
            if approval_id in revoked or value.get("revoked") is True:
                status = "revoked"
            elif _epoch(start) is None or _epoch(end) is None:
                status = "undated"
            elif (_epoch(self.stamp) or 0.0) < (_epoch(start) or 0.0):
                status = "not_yet_valid"
            else:  # inside the dates when start <= now < end (meta_local standing)
                status = (
                    "expired" if (_epoch(self.stamp) or 0.0) >= (_epoch(end) or 0.0) else "active"
                )
            by = _d(value.get("approved_by"))
            detail = {
                k: v
                for k in ("nights", "budget_trials", "cells", "shares", "max_budget")
                if (v := pick(k)) is not None
            }
            out.append(
                StandingApproval(
                    approval_id,
                    status,
                    _s(by.get("subject_id")) or _s(by.get("kind")),
                    _s(value.get("approved_at")),
                    start,
                    end,
                    detail,
                )
            )
        return tuple(sorted(out, key=lambda a: a.approved_at or "", reverse=True))

    # -- corpus --------------------------------------------------------------------------------
    def corpora(self) -> tuple[CorpusView, ...]:
        summaries = self.summaries()
        per_task: dict[str, list[dict[str, Any]]] = {}
        every, informative = set(), set()
        for _cell, (_ref, summary, entry) in summaries.items():
            for task, stat in _d(entry.get("tasks")).items():
                per_task.setdefault(task, []).append(_d(stat))
            every |= {t for t in _l(summary.get("saturated_everywhere")) if isinstance(t, str)}
            informative |= {t for t in _l(summary.get("informative_any")) if isinstance(t, str)}
        out = []
        for corpus_id, (_ref, index) in sorted(self.r.latest("corpus-task-index").items()):
            domains: dict[str, DomainRow] = {}
            split_counts: dict[str, int] = {}
            saturated: list[str] = []
            flaky: list[str] = []
            for entry_task in map(_d, _l(index.get("tasks"))):
                case = _s(entry_task.get("case_id"))
                domain = _s(entry_task.get("domain")) or "unknown"
                split = _s(entry_task.get("split")) or "unknown"
                if case is None:
                    continue
                split_counts[split] = split_counts.get(split, 0) + 1
                stats = per_task.get(case, [])
                cls = self.task_class(case, stats, every, informative)
                rate = _mean(r for r in (_num(s.get("pass_rate")) for s in stats) if r is not None)
                held = domains.get(domain) or DomainRow(domain, {}, 0, 0, None, {})
                splits = {**held.splits, split: held.splits.get(split, 0) + 1}
                classes = dict(held.classes)
                if cls:
                    classes[cls] = classes.get(cls, 0) + 1
                domains[domain] = DomainRow(
                    domain,
                    splits,
                    held.tasks + 1,
                    held.measured + (1 if stats else 0),
                    held.mean_pass_rate,
                    classes,
                )
                if rate is not None:
                    domains[domain] = dataclasses.replace(
                        domains[domain],
                        mean_pass_rate=_running(held.mean_pass_rate, held.measured, rate),
                    )
                if cls == "saturated":
                    saturated.append(case)
                if cls == "flaky_grading":
                    flaky.append(case)

            def visible(ids: list[str], hidden: dict[str, str] = self.names.hidden) -> list[str]:
                return sorted(i for i in ids if i not in hidden)

            out.append(
                CorpusView(
                    corpus_id=corpus_id,
                    version=_s(index.get("corpus_version")),
                    domains=tuple(sorted(domains.values(), key=lambda d: d.domain)),
                    split_counts=dict(sorted(split_counts.items())),
                    saturated=tuple(visible(saturated)),
                    saturated_withheld=len(saturated) - len(visible(saturated)),
                    flaky=tuple(visible(flaky)),
                    flaky_withheld=len(flaky) - len(visible(flaky)),
                )
            )
        return tuple(out)

    @staticmethod
    def task_class(
        case: str, stats: list[dict[str, Any]], saturated: set[str], informative: set[str]
    ) -> str | None:
        classes = [_s(s.get("class")) for s in stats]
        if "flaky_grading" in classes:
            return "flaky_grading"
        if case in saturated:
            return "saturated"
        if case in informative or "informative" in classes:
            return "informative"
        if classes and all(c == "unsolved" for c in classes):
            return "unsolved"
        return "unknown" if classes else None

    # -- layers --------------------------------------------------------------------------------
    def layers(self) -> tuple[LayerView, ...]:
        decisions = [d for _r, d in self.r.latest("harness-decision").values()]
        tables = [(i, v) for i, (_r, v) in self.r.latest("decider-table").items()]
        if not decisions and not tables:
            return ()
        dev_cells = sorted({r.cell for r in self.rows if r.split == DEVELOPMENT})
        out = []
        for layer in LAYERS:
            mine = [d for d in decisions if d.get("layer") == layer]
            mine_tables = [(i, v) for i, v in sorted(tables) if v.get("layer") == layer]
            if mine or mine_tables or layer in ("L2", "L3"):  # L2/L3 rows fall back to trials
                options, regret, note = self.layer_metrics(layer, dev_cells)
            else:
                options, regret, note = (), None, None
            chosen: dict[str, int] = {}
            for d in mine:
                option = str(d.get("chosen"))
                chosen[option] = chosen.get(option, 0) + 1
            specialised = sum(1 for d in mine if _l(d.get("bucket")))
            out.append(
                LayerView(
                    layer=layer,
                    decisions=len(mine),
                    coverage=(sum(1 for d in mine if not d.get("used_prior")) / len(mine))
                    if mine
                    else None,
                    specialised_decisions=specialised,
                    pooled_decisions=len(mine) - specialised,
                    chosen=dict(sorted(chosen.items())),
                    tables=tuple(self.table_view(i, v) for i, v in mine_tables),
                    options=options,
                    regret=regret,
                    note=note,
                )
            )
        return tuple(out)

    @staticmethod
    def table_view(table_id: str, value: dict[str, Any]) -> TableView:
        entries = [_d(e) for e in _l(value.get("cells"))]
        glob = [e for e in entries if not _d(e.get("bucket"))]
        top = sorted(entries, key=lambda e: -(_int(e.get("n")) or 0))[:12]
        return TableView(
            table_id=table_id,
            rows=_int(_d(value.get("source")).get("rows")),
            options=tuple(str(o) for o in _l(value.get("options"))),
            entries=len(entries),
            specialised=len(entries) - len(glob),
            pooled=len(glob),
            top=tuple(
                {
                    "bucket": ", ".join(f"{k}={v}" for k, v in sorted(_d(e.get("bucket")).items()))
                    or "(global)",
                    "option": e.get("option"),
                    "n": e.get("n"),
                    "successes": e.get("successes"),
                    "posterior_success": e.get("posterior_success"),
                    "interval": e.get("interval"),
                    "pooled_depth": len(_l(e.get("pooled_from"))),
                }
                for e in top
            ),
        )

    def layer_metrics(
        self, layer: str, cells: list[str]
    ) -> tuple[tuple[dict[str, Any], ...], dict[str, Any] | None, str | None]:
        """Per option success (development rows of the layer) and the layer's regret (§6.8)."""
        if not cells:
            return (), None, "no development trial-metrics"
        from ..runtime.evidence.cas import ArtifactStore
        from . import deciders

        try:
            rows = deciders.rows_from_trial_metrics(
                self.store, self.scope, layer=layer, cells=cells
            )
            measured = deciders.measured_from_rows(rows)
            artifacts = ArtifactStore(self.store)
            chosen = []
            for _id, (_ref, tm) in sorted(
                deciders.latest(self.store, self.scope, "trial-metrics").items()
            ):
                if tm.get("split") != DEVELOPMENT or tm.get("cell_id") not in cells:
                    continue
                try:
                    _trial, plan = deciders.trial_plan(
                        self.store, self.scope, artifacts, tm["trial_ref"]
                    )
                except (RuntimeFault, KeyError, ValueError, TypeError):
                    continue
                if plan is None:
                    continue
                for decision in deciders.plan_decisions(self.store, self.scope, plan):
                    if decision.get("layer") == layer:
                        chosen.append({**decision, "task_id": str(tm["task_id"])})
            regret = deciders.regret(chosen, measured)
        except (RuntimeFault, KeyError, ValueError, TypeError) as exc:
            return (), None, f"not measurable: {getattr(exc, 'code', type(exc).__name__)}"
        by_option: dict[str, list[Any]] = {}
        for row in rows:
            by_option.setdefault(row.option, []).append(row)
        options = []
        for option, items in sorted(by_option.items()):
            known = [r for r in items if r.success is not None]
            tokens = [r.tokens for r in items if r.tokens is not None]
            options.append(
                {
                    "option": option,
                    "n": len(known),
                    "successes": sum(1 for r in known if r.success),
                    "rate": (sum(1 for r in known if r.success) / len(known)) if known else None,
                    "tokens_mean": round(sum(tokens) / len(tokens), 1) if tokens else None,
                }
            )
        regret.pop("per_decision", None)  # task-level detail stays out of the pages
        return tuple(options), regret, None

    # -- strategies ----------------------------------------------------------------------------
    def strategies(self) -> tuple[StrategyRow, ...]:
        groups: dict[tuple[str, str], list[_Row]] = {}
        for row in self.rows:
            if row.strategy:
                groups.setdefault((row.strategy, row.cell), []).append(row)
        out = []
        metric_names = (
            "agent_calls",
            "turns",
            "attempts_used",
            "escalations",
            "reviewer_rounds",
            "fix_requests",
            "best_of_n_first_pass",
            "nodes",
            "sub_agents",
            "integration_conflicts",
            "re_verifications",
        )
        for (strategy, cell), group in sorted(groups.items()):
            tasks = _tasks(group)
            k, rate = _pass_k(tasks)
            known = [r for r in group if r.success is not None]
            out.append(
                StrategyRow(
                    strategy=strategy,
                    cell_id=cell,
                    n=len(known),
                    tasks=len(tasks),
                    success=(sum(1 for r in known if r.success) / len(known)) if known else None,
                    pass_k=k,
                    pass_k_rate=rate,
                    tokens_per_solved=_per_solved(group, "tokens"),
                    seconds_per_solved=_per_solved(group, "seconds"),
                    metrics={
                        name: (
                            _mean(
                                v for v in (_num(r.raw.get(name)) for r in group) if v is not None
                            )
                        )
                        for name in metric_names
                    },
                )
            )
        return tuple(out)

    # -- judges --------------------------------------------------------------------------------
    def judge_qualifications(self) -> tuple[JudgeQualificationRow, ...]:
        out = []
        for _id, (_ref, v) in sorted(self.r.latest("judge-qualification").items()):
            out.append(
                JudgeQualificationRow(
                    judge_id=_s(v.get("judge_id")) or "?",
                    version=str(v.get("judge_version")),
                    question_type=_s(v.get("question_type")) or "?",
                    n=_int(v.get("n")),
                    accuracy=_num(v.get("accuracy")),
                    accuracy_interval=_interval(v.get("accuracy_interval")),
                    brier=_num(v.get("brier")),
                    ece_10=_num(v.get("ece_10")),
                    repeat_agreement=_num(v.get("repeat_agreement")),
                    status=_s(v.get("status")) or "unknown",
                    qualified_at=_s(v.get("qualified_at")),
                )
            )
        return tuple(out)

    def judge_calls(self) -> tuple[JudgeCallRow, ...]:
        groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for _id, (_ref, v) in self.r.latest("judge-call").items():
            key = (
                _s(v.get("judge_id")) or "?",
                str(v.get("judge_version")),
                _s(v.get("question_type")) or "?",
            )
            groups.setdefault(key, []).append(v)
        out = []
        for (judge, version, qtype), calls in sorted(groups.items()):
            tokens = [_usage_tokens(_d(c.get("usage"))) for c in calls]
            costs = [_int(_d(c.get("usage")).get("cost_microunits")) for c in calls]
            latency = [x for x in (_num(c.get("latency_ms")) for c in calls) if x is not None]
            out.append(
                JudgeCallRow(
                    judge,
                    version,
                    qtype,
                    len(calls),
                    sum(t for t in tokens if t is not None)
                    if any(t is not None for t in tokens)
                    else None,
                    sum(c for c in costs if c is not None)
                    if any(c is not None for c in costs)
                    else None,
                    median(latency) if latency else None,
                )
            )
        return tuple(out)

    # -- budget --------------------------------------------------------------------------------
    def budget(self) -> BudgetView:
        plans = {i: v for i, (_r, v) in self.r.latest("nightly-plan").items()}
        nights = []
        for night_id, _state, data in self.r.heads("nightly-run"):
            plan = _d(self.r.get("nightly-plan", data.get("plan_ref"))) or plans.get(
                "nightplan-" + night_id.removeprefix("night-"), {}
            )
            nights.append(
                NightRow(
                    date=night_id.removeprefix("night-"),
                    budget_trials=_int(plan.get("budget_trials")),
                    shares=_d(plan.get("shares")),
                    cells=tuple(str(c) for c in _l(plan.get("cells"))),
                    pilot=plan.get("pilot") if isinstance(plan.get("pilot"), bool) else None,
                    phase=_s(data.get("phase")),
                    trials=_int(data.get("trials")),
                    stopped=self.names.scrub(data.get("stopped")) if data.get("stopped") else None,
                    proposals=len(_l(data.get("proposals"))),
                    queued=len(_l(data.get("queued_confirmations"))),
                    finished_at=_s(data.get("finished_at")),
                )
            )
        known = {n.date for n in nights}
        for plan_id, plan in sorted(plans.items()):  # planned, not yet run
            date = plan_id.removeprefix("nightplan-")
            if date not in known:
                nights.append(
                    NightRow(
                        date,
                        _int(plan.get("budget_trials")),
                        _d(plan.get("shares")),
                        tuple(str(c) for c in _l(plan.get("cells"))),
                        plan.get("pilot") if isinstance(plan.get("pilot"), bool) else None,
                        "planned",
                        None,
                        None,
                        0,
                        0,
                        None,
                    )
                )
        quota = self.quota_rows()
        drivers = {c: _s(v.get("driver_id")) for c, v in self.cell_info().items()}
        per_trial = [
            r.tokens for r in self.rows if r.tokens is not None and r.source != "calibration"
        ]
        median_tokens = float(median(per_trial)) if per_trial else None
        by_driver: dict[str, list[int]] = {}
        for r in self.rows:
            driver = drivers.get(r.cell)
            if driver and r.tokens is not None and r.source != "calibration":
                by_driver.setdefault(driver, []).append(r.tokens)
        medians = {d: float(median(v)) for d, v in by_driver.items()}
        latest_plan = max(plans.items(), default=None, key=lambda p: p[0])
        budget = _int(latest_plan[1].get("budget_trials")) if latest_plan else None
        keep = _num(self.nightly.get("keep_operator_share"))
        return BudgetView(
            nights=tuple(sorted(nights, key=lambda n: n.date)),
            quota=tuple(quota),
            headroom=tuple(self.headroom(quota, medians, median_tokens, keep)),
            budget_trials=budget,
            configured_trials=_int(self.nightly.get("budget_trials")),
            keep_operator_share=keep,
            median_tokens_per_trial=median_tokens,
        )

    def quota_rows(self) -> list[QuotaRow]:
        out = []
        for record_id, (_ref, value) in sorted(self.r.latest("quota-observation").items()):
            night = record_id.rsplit("-", 3)
            date = "-".join(night[-3:]) if len(night) >= 4 else None
            windows = [w for w in _l(value.get("windows")) if isinstance(w, dict)]
            if not windows and "window" in value:
                windows = [value]
            for w in windows:
                out.append(
                    QuotaRow(
                        driver_id=_s(w.get("driver_id")) or _s(value.get("driver_id")) or "?",
                        night=_s(value.get("night")) or date,
                        window=str(w.get("window")),
                        runs=_int(w.get("runs")),
                        input_tokens=_int(w.get("input_tokens")),
                        output_tokens=_int(w.get("output_tokens")),
                        cached_input_tokens=_int(w.get("cached_input_tokens")),
                        rate_limit_events=_int(w.get("rate_limit_events")),
                        limit_errors=_int(w.get("limit_errors")),
                        first_signal_at=_s(w.get("first_signal_at")),
                    )
                )
        return out

    @staticmethod
    def headroom(
        quota: list[QuotaRow],
        medians: dict[str, float],
        fallback: float | None,
        keep: float | None,
    ) -> list[HeadroomRow]:
        """§8.6 (as ``QuotaObserver.headroom``): per driver the tokens of the 5 h window at the
        first limit signal (the earliest one), else the largest observed, marked ">= max
        observed"; suggested B = floor(keep x headroom / the driver's median tokens per trial; the
        median over every driver when it has none), ``keep`` = ``keep_operator_share`` of the
        config (§8.6 default 0.5)."""
        share = 0.5 if keep is None else keep
        out = []
        for driver in sorted({q.driver_id for q in quota}):
            windows = [q for q in quota if q.driver_id == driver and q.window == "5h"]
            signalled = [q for q in windows if q.first_signal_at]
            if signalled:
                first = min(signalled, key=lambda q: q.first_signal_at or "")
                tokens: int | None = (first.input_tokens or 0) + (first.output_tokens or 0)
            else:
                totals = [(q.input_tokens or 0) + (q.output_tokens or 0) for q in windows]
                tokens = max(totals) if totals else None
            per_trial = medians.get(driver, fallback)
            out.append(
                HeadroomRow(
                    driver,
                    tokens,
                    "at first limit signal" if signalled else ">= max observed",
                    int(share * tokens / per_trial) if tokens is not None and per_trial else None,
                )
            )
        return out

    # -- evaluation ----------------------------------------------------------------------------
    def evaluation(self) -> EvaluationView:
        versions = [
            EvaluatorVersionRow(
                version=_s(v.get("version")) or i,
                approved_at=_s(v.get("at")),
                requalification=_d(v.get("requalification_ref")).get("id"),
            )
            for i, (_r, v) in sorted(self.r.latest("evaluator-version").items())
        ]
        quality = [
            QualityView(
                version=_s(v.get("evaluator_version")) or "?",
                record_id=i,
                measured_at=_s(v.get("measured_at")),
                since=_s(v.get("since")),
                metrics=_d(v.get("metrics")),
            )
            for i, (_r, v) in sorted(self.r.latest("evaluation-quality").items())
        ]
        changes = [
            {
                "change_id": i,
                "state": s,
                "reason": self.names.scrub(d.get("reason")),
                "vacuous": _d(d.get("qualification_scope")).get("vacuous"),
            }
            for i, s, d in self.r.heads("evaluator-change")
        ]
        requal = [
            {
                "id": i,
                "evaluator_version": v.get("evaluator_version"),
                "all_equal": v.get("all_equal"),
                "reports": len(_l(v.get("reports"))),
            }
            for i, (_r, v) in sorted(self.r.latest("evaluator-requalification").items())
        ]
        drops: dict[str, int] = {}
        for _id, (_ref, v) in self.r.latest("trace-drop").items():
            reason = _s(v.get("reason")) or "unknown"
            drops[reason] = drops.get(reason, 0) + 1  # counts only: the dropped text is never read
        return EvaluationView(
            tuple(versions),
            tuple(quality),
            tuple(changes),
            tuple(requal),
            dict(sorted(drops.items())),
        )


def _running(previous: float | None, count: int, value: float) -> float:
    """Running mean of the measured tasks (``count`` of them held ``previous``)."""
    return value if previous is None or count == 0 else (previous * count + value) / (count + 1)


def _usage_tokens(usage: dict[str, Any]) -> int | None:
    total = _int(usage.get("total_tokens"))
    if total is not None:
        return total
    spent_in, spent_out = _int(usage.get("input_tokens")), _int(usage.get("output_tokens"))
    return spent_in + spent_out if spent_in is not None and spent_out is not None else None


def _frontier(stats: list[CompositionStat]) -> set[str]:
    """The compositions no other one beats on both success and tokens per solved task."""
    points = sorted(
        (s for s in stats if s.success is not None and s.tokens_per_solved is not None),
        key=lambda s: (s.tokens_per_solved, -(s.success or 0.0), s.key),
    )
    best, out = -1.0, set()
    for s in points:
        if (s.success or 0.0) > best:
            out.add(s.key)
            best = s.success or 0.0
    return out


# ===== rendering ================================================================================
class Raw(str):
    """Markup that is already safe: every other value goes through ``html.escape``."""


def _na(value: Any) -> str:
    return NO_DATA if value is None else str(value)


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _c(value: Any) -> str:
    return str(value) if isinstance(value, Raw) else _e(value)


def _pct(value: float | None, digits: int = 1) -> str:
    return "—" if value is None else f"{value * 100:.{digits}f}%"


def _signed(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:+.1f} pts"


def _ival(value: tuple[float, float] | None) -> str:
    return "" if value is None else f"[{value[0] * 100:.1f}%, {value[1] * 100:.1f}%]"


def _num_s(value: Any, digits: int = 1) -> str:
    n = _num(value)
    if n is None:
        return "—"
    return f"{n:,.0f}" if float(n).is_integer() and digits == 0 else f"{n:,.{digits}f}"


def _cost_s(cost: CostPerSolved) -> str:
    if cost.status == "estimated" and cost.microunits is not None:
        return f"{cost.microunits / 1_000_000:.4f} {cost.currency or ''}".strip()
    if cost.status.startswith("unpriced:"):
        return f"unpriced ({cost.status.removeprefix('unpriced:')})"
    return "no solved task" if cost.status == "no_solved" else "—"


def _link(page: str, text: str) -> Raw:
    return Raw(f'<a href="{_e(page)}">{_e(text)}</a>')


def _no_data() -> Raw:
    return Raw(f'<p class="nodata">{NO_DATA}</p>')


def _table(caption: str, headers: list[str], rows: list[list[Any]], *, empty: bool = True) -> Raw:
    if not rows:
        return _no_data() if empty else Raw("")
    head = "".join(f'<th scope="col">{_e(h)}</th>' for h in headers)
    body = "".join(
        "<tr>"
        + "".join(
            (f'<th scope="row">{_c(cell)}</th>' if i == 0 else f"<td>{_c(cell)}</td>")
            for i, cell in enumerate(row)
        )
        + "</tr>"
        for row in rows
    )
    return Raw(
        f'<div class="scroll"><table><caption>{_e(caption)}</caption>'
        f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"
    )


def _facts(value: Any, names: _Names | None = None, depth: int = 0) -> Raw:
    """A nested record value as markup: bounded, every string escaped."""

    def text(x: Any) -> str:
        if isinstance(x, float):
            return f"{x:.4g}"
        out = "" if x is None else str(x)
        return names.scrub(out, 200) if names else out[:200]

    if depth > 3:
        return Raw("…")
    if isinstance(value, dict):
        items = list(value.items())[:30]
        return Raw(
            "<dl>"
            + "".join(
                f"<dt>{_e(text(k))}</dt><dd>{_c(_facts(v, names, depth + 1))}</dd>"
                for k, v in items
            )
            + "</dl>"
        )
    if isinstance(value, list | tuple):
        if all(not isinstance(v, dict | list | tuple) for v in value):
            return Raw(_e(", ".join(text(v) for v in value[:30])))
        return Raw(
            "<ul>"
            + "".join(f"<li>{_c(_facts(v, names, depth + 1))}</li>" for v in value[:30])
            + "</ul>"
        )
    return Raw(_e(text(value)))


CSS = """
:root{--bg:#fff;--fg:#1b1f24;--muted:#57606a;--line:#d0d7de;--panel:#f6f8fa;--link:#0550ae;
--ok:#1a7f37;--bad:#cf222e;--warn:#9a6700;--accent:#0969da;--point:#0969da;--front:#1a7f37}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0d1117;--fg:#e6edf3;
--muted:#8b949e;--line:#30363d;--panel:#161b22;--link:#58a6ff;--ok:#3fb950;--bad:#f85149;
--warn:#d29922;--accent:#58a6ff;--point:#58a6ff;--front:#3fb950}}
:root[data-theme="dark"]{--bg:#0d1117;--fg:#e6edf3;--muted:#8b949e;--line:#30363d;--panel:#161b22;
--link:#58a6ff;--ok:#3fb950;--bad:#f85149;--warn:#d29922;--accent:#58a6ff;--point:#58a6ff;
--front:#3fb950}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
a{color:var(--link)}.skip{position:absolute;left:-999px}.skip:focus{left:8px;top:8px;
background:var(--panel);padding:6px 10px;z-index:2}
header,nav,main,footer{max-width:1180px;margin:0 auto;padding:0 16px}
header{padding-top:20px}h1{font-size:1.6rem;margin:.2em 0}h2{font-size:1.2rem;margin:1.6em 0 .4em;
border-bottom:1px solid var(--line);padding-bottom:.2em}h3{font-size:1rem;margin:1.2em 0 .3em}
nav ul{list-style:none;display:flex;flex-wrap:wrap;gap:4px 14px;padding:8px 0;margin:0;
border-bottom:1px solid var(--line)}nav a[aria-current]{font-weight:700;color:var(--fg);
text-decoration:none}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;margin:.5em 0;font-size:.9rem}
caption{text-align:left;color:var(--muted);padding:4px 0}th,td{border:1px solid var(--line);
padding:5px 8px;text-align:left;vertical-align:top}thead th{background:var(--panel)}
tbody th{font-weight:600;background:var(--panel)}dl{margin:0}dt{color:var(--muted);font-size:.8rem}
dd{margin:0 0 .3em}.cellbox dt{float:left;clear:left;margin-right:.4em}.cellbox dd{margin:0}
.nodata{color:var(--muted);font-style:italic}.muted{color:var(--muted)}.ok{color:var(--ok)}
.bad{color:var(--bad)}.warn{color:var(--warn)}code{font-size:.85em}
.chart{max-width:100%;height:auto;border:1px solid var(--line);background:var(--panel)}
.chart text{fill:var(--muted);font-size:11px}.chart .axis{stroke:var(--line);fill:none}
.chart .pt{fill:var(--point)}.chart .fr{fill:var(--front)}.chart .ln{stroke:var(--front);
fill:none;stroke-width:1.5}footer{padding-bottom:30px;color:var(--muted);font-size:.85rem}
.tag{display:inline-block;border:1px solid var(--line);border-radius:4px;padding:0 5px;
font-size:.78rem;margin-right:3px}
"""


def _shell(feed: DashboardFeed, page: str, title: str, body: str) -> str:
    nav = "".join(
        f'<li><a href="{_e(href)}"'
        + (' aria-current="page"' if href == page else "")
        + f">{_e(label)}</a></li>"
        for href, label in PAGE_LINKS
    )
    scope = _e("/".join(feed.scope.values()))
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="Content-Security-Policy" content="default-src &#39;none&#39;; '
        'style-src &#39;unsafe-inline&#39;; base-uri &#39;none&#39;; form-action &#39;none&#39;">'
        f"<title>{_e(title)} - Meta-Harness Dashboard</title><style>{CSS}</style></head><body>"
        '<a class="skip" href="#content">Skip to content</a>'
        f"<header><h1>{_e(title)}</h1>"
        f'<p class="muted">Scope {scope} · generated {_e(feed.generated_at)} · read-only build</p>'
        f'</header><nav aria-label="Dashboard pages"><ul>{nav}</ul></nav>'
        f'<main id="content" tabindex="-1">{body}</main>'
        f"<footer><p>{_e(' '.join(feed.notes))}</p></footer></body></html>\n"
    )


def _section(title: str, content: Any, level: int = 2) -> str:
    return f"<h{level}>{_e(title)}</h{level}>{_c(content)}"


def _cell_box(m: MatrixCell) -> Raw:
    if m.pass_rate is None and m.best is None and m.tasks == 0:
        return Raw(f'<span class="nodata">{NO_DATA}</span><br>{_c(_link(m.page, "cell page"))}')
    best = f"no composition with >= {MIN_TASKS} dev tasks"
    if m.best is not None:
        delta = "baseline" if m.best.is_baseline else _signed(m.best.delta)
        best = f"{m.best.composition_id} ({_pct(m.best.success)}, {delta})"
    pk = "—" if m.pass_k_rate is None else f"{_pct(m.pass_k_rate)} (k={m.pass_k})"
    parts = [
        ("cell", m.cell_id),
        ("pass rate", f"{_pct(m.pass_rate)} {_ival(m.interval)}".strip()),
        ("pass^k", pk),
        ("informative", _num_s(m.informative, 0)),
        ("median time", "—" if m.median_seconds is None else f"{m.median_seconds:.1f} s"),
        ("tokens / solved", _num_s(m.tokens_per_solved, 0)),
        ("API-equiv. cost / solved (descriptive)", _cost_s(m.api_cost)),
        ("best composition", best),
    ]
    dl = "".join(f"<dt>{_e(k)}</dt><dd>{_e(v)}</dd>" for k, v in parts)
    return Raw(f'<dl class="cellbox">{dl}</dl>{_c(_link(m.page, "cell page"))}')


def _index(feed: DashboardFeed) -> str:
    if not feed.matrix:
        matrix: Any = _no_data()
    else:
        # one row per driver/model; a cell whose (driver, model, effort) slot is taken by
        # another cell id gets a row of its own, so no cell is hidden by another
        lines: list[tuple[str, str, dict[str, MatrixCell]]] = []
        for m in feed.matrix:
            line = next(
                (
                    by_effort
                    for d, mo, by_effort in lines
                    if (d, mo) == (m.driver_id, m.model) and m.effort not in by_effort
                ),
                None,
            )
            if line is None:
                line = {}
                lines.append((m.driver_id, m.model, line))
            line[m.effort] = m
        rows = [
            [
                f"{driver} / {model} ({', '.join(c.cell_id for c in by_effort.values())})",
                *[
                    _cell_box(by_effort[e])
                    if e in by_effort
                    else Raw('<span class="muted">—</span>')
                    for e in feed.efforts
                ],
            ]
            for driver, model, by_effort in lines
        ]
        matrix = _table(
            "Performance per driver/model (rows) and reasoning effort (columns)",
            ["driver / model", *feed.efforts],
            rows,
        )
    release = feed.release
    state = (
        "no data"
        if release is None
        else f"active release {release.active} ({'baseline' if release.baseline else 'promoted'})"
    )
    sources = _table(
        "Records read", ["kind", "count"], [[k, v] for k, v in feed.sources.items() if v]
    )
    return _shell(
        feed,
        "index.html",
        "Meta-Harness Matrix",
        (
            "<p>Calibrated pass rate with its 95 % interval, pass^k, informative tasks, "
            "median time, "
            "tokens and API-equivalent cost per solved task (descriptive, D-094), and the best "
            "composition of each cell with its delta to the baseline (paired, development "
            "tasks).</p>"
            + _section("Matrix", matrix)
            + _section("Release", Raw(f"<p>{_e(state)}</p>"))
            + _section("Records", sources)
        ),
    )


def _manifest_table(manifest: ManifestView | None) -> Raw:
    if manifest is None:
        return _no_data()
    if manifest.error:
        return Raw(f'<p class="warn">manifest unreadable: {_e(manifest.error)}</p>')
    rows = [
        [
            c.slot,
            c.component,
            "—" if c.version is None else c.version,
            Raw(f'<span class="{"ok" if c.state == "on" else "muted"}">{_e(c.state)}</span>'),
            c.detail,
        ]
        for c in manifest.components
    ]
    return _table(
        f"Manifest of {manifest.composition_id}",
        ["component", "id@version", "version", "state", "detail"],
        rows,
    )


def _frontier_svg(stats: tuple[CompositionStat, ...]) -> Raw:
    points = [s for s in stats if s.success is not None and s.tokens_per_solved is not None]
    if not points:
        return _no_data()
    width, height, left, bottom, top, right = 560, 300, 52, 36, 14, 16
    xmax = max(s.tokens_per_solved or 0.0 for s in points) * 1.1 or 1.0

    def px(x: float) -> float:
        return left + (width - left - right) * x / xmax

    def py(y: float) -> float:
        return height - bottom - (height - bottom - top) * y

    parts = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
        'aria-labelledby="frontier-title frontier-desc">'
        '<title id="frontier-title">Success versus tokens per solved task</title>'
        '<desc id="frontier-desc">One point per composition; the line joins the compositions no '
        "other one beats on both axes.</desc>",
        f'<path class="axis" d="M{left},{top}V{height - bottom}H{width - right}"/>',
    ]
    for i in range(6):
        y = i / 5
        parts.append(
            f'<text x="{left - 6}" y="{py(y) + 4:.1f}" text-anchor="end">{y * 100:.0f}%</text>'
        )
        x = xmax * i / 5
        parts.append(
            f'<text x="{px(x):.1f}" y="{height - bottom + 16}" text-anchor="middle">{x:,.0f}</text>'
        )
    parts.append(
        f'<text x="{(left + width - right) / 2:.0f}" y="{height - 4}" '
        'text-anchor="middle">tokens per solved task</text>'
    )
    line = sorted((s for s in points if s.on_frontier), key=lambda s: s.tokens_per_solved or 0.0)
    if len(line) > 1:
        path = " ".join(
            f"{px(s.tokens_per_solved or 0.0):.1f},{py(s.success or 0.0):.1f}" for s in line
        )
        parts.append(f'<polyline class="ln" points="{path}"/>')
    for s in points:
        tip = (
            f"{s.composition_id}: success {_pct(s.success)}, "
            f"{_num_s(s.tokens_per_solved, 0)} tokens per solved, {s.tasks} tasks"
        )
        parts.append(
            f'<circle class="{"fr" if s.on_frontier else "pt"}" '
            f'cx="{px(s.tokens_per_solved or 0.0):.1f}" '
            f'cy="{py(s.success or 0.0):.1f}" r="5"><title>{_e(tip)}</title></circle>'
        )
    parts.append("</svg>")
    return Raw("".join(parts))


def _stage_table(rows: tuple[StageRow, ...], caption: str) -> Raw:
    return _table(
        caption,
        ["proposal", "stage", "state", "decision class", "experiment", "findings"],
        [
            [
                r.proposal_id,
                r.stage,
                r.state,
                r.decision_class or "—",
                _link(r.experiment_page, r.experiment_id or "")
                if r.experiment_page
                else (r.experiment_id or "—"),
                Raw("<br>".join(_e(f) for f in r.findings)) if r.findings else "—",
            ]
            for r in rows
        ],
    )


def _cell_page(feed: DashboardFeed, cell: CellPage) -> str:
    comps = _table(
        "Compositions (development split; unit = task)",
        ["composition", "role", "tasks", "runs", "success", "tokens / solved", "frontier"],
        [
            [
                c.composition_id,
                ", ".join(
                    x for x, on in (("baseline", c.is_baseline), ("champion", c.is_champion)) if on
                )
                or "—",
                c.tasks,
                c.runs,
                _pct(c.success),
                _num_s(c.tokens_per_solved, 0),
                "yes" if c.on_frontier else "no",
            ]
            for c in cell.compositions
        ],
    )
    manifests = "".join(
        _section(c.composition_id, _manifest_table(c.manifest), 3) for c in cell.compositions
    )
    ablation = _table(
        "Leave-one-out ablation (contribution = what removing the component costs, development)",
        [
            "component",
            "derived proposal",
            "of proposal",
            "state",
            "decision class",
            "tasks",
            "contribution",
        ],
        [
            [
                a.component,
                a.derived_proposal,
                a.parent_proposal,
                a.state,
                a.decision_class or "—",
                a.tasks,
                _signed(a.contribution),
            ]
            for a in cell.ablations
        ],
    )
    lineage = _table(
        "Archive lineage",
        ["child", "parents", "proposal", "verdict"],
        [
            [
                e.get("child"),
                ", ".join(str(p) for p in e.get("parents") or []),
                e.get("proposal_id"),
                e.get("verdict") or "—",
            ]
            for e in cell.archive_lineage
        ],
    )
    title = f"Cell {cell.cell_id}"
    body = (
        f"<p>{_e(cell.driver_id)} · {_e(cell.model)} · effort {_e(cell.effort)} · champion "
        f"{_e(cell.champion or 'no data')}</p>"
        + _section("Success versus tokens", _frontier_svg(cell.compositions))
        + _section("Compositions", comps)
        + _section("Manifests", Raw(manifests) if manifests else _no_data())
        + _section("Ablation contributions", ablation)
        + _section("Stage history", _stage_table(cell.stages, "Stages of the cell's proposals"))
        + _section("Archive", lineage)
    )
    return _shell(feed, cell.page, title, body)


def _lineage(feed: DashboardFeed) -> str:
    release = feed.release
    head = (
        _no_data()
        if release is None
        else Raw(
            f"<p>Active release <code>{_e(release.active)}</code> "
            f"({'baseline' if release.baseline else 'promoted'}).</p>"
            + (
                "".join(
                    f'<p class="warn">stale: re-derive — {_e(n)} names a router that is no '
                    "longer installed</p>"
                    for n in release.stale
                )
            )
        )
    )
    rows = [
        [
            r.proposal_id,
            r.cell_id or "—",
            r.surface_class or "—",
            r.status,
            r.parent_proposal or "—",
            f"{r.baseline or '—'} → {r.candidate or '—'}",
            r.hypothesis or "—",
            Raw(
                "<br>".join(
                    (
                        _c(_link(e.experiment_page, f"{e.stage}: {e.state}"))
                        if e.experiment_page
                        else _e(f"{e.stage}: {e.state}")
                    )
                    + (f" ({_e(e.decision_class)})" if e.decision_class else "")
                    for e in r.experiments
                )
                or "—"
            ),
            r.verdict or "—",
            r.release or "—",
        ]
        for r in feed.lineage
    ]
    return _shell(
        feed,
        "lineage.html",
        "Lineage",
        (
            _section("Release", head)
            + _section(
                "Proposal to release",
                _table(
                    "Proposal → candidate → experiments → verdict → release",
                    [
                        "proposal",
                        "cell",
                        "class",
                        "state",
                        "derived of",
                        "baseline → candidate",
                        "hypothesis",
                        "experiments",
                        "verdict",
                        "release",
                    ],
                    rows,
                ),
            )
        ),
    )


def _approvals(feed: DashboardFeed) -> str:
    rows = [[a.kind, a.subject, a.surface_class, a.detail, a.since or "—"] for a in feed.approvals]
    standing = _table(
        "Standing approval nightly.explore (IC-17)",
        ["approval", "status", "approved by", "approved at", "valid from", "valid until", "terms"],
        [
            [
                s.approval_id,
                Raw(
                    f'<span class="{"ok" if s.status == "active" else "warn"}">'
                    f"{_e(s.status)}</span>"
                ),
                s.approved_by or "—",
                s.approved_at or "—",
                s.valid_from or "—",
                s.valid_until or "—",
                _facts(s.detail),
            ]
            for s in feed.standing
        ],
    )
    return _shell(
        feed,
        "approvals.html",
        "Approvals",
        (
            "<p>Everything waiting for the operator, with its surface class.</p>"
            + _section(
                "Waiting",
                _table(
                    "Waiting for the operator",
                    ["kind", "subject", "surface class", "what", "since"],
                    rows,
                ),
            )
            + _section("Standing approval", standing)
        ),
    )


def _corpus(feed: DashboardFeed) -> str:
    if not feed.corpora:
        return _shell(feed, "corpus.html", "Corpus", _section("Corpus", _no_data()))
    body = []
    for c in feed.corpora:
        rows = [
            [
                d.domain,
                d.tasks,
                ", ".join(f"{k} {v}" for k, v in sorted(d.splits.items())),
                d.measured,
                _pct(d.mean_pass_rate),
                ", ".join(f"{k} {v}" for k, v in sorted(d.classes.items())) or "—",
            ]
            for d in c.domains
        ]
        lists = Raw(
            f"<p>Saturated: {_e(', '.join(c.saturated) or 'none')}"
            + (f" (+{c.saturated_withheld} withheld)" if c.saturated_withheld else "")
            + f"</p><p>Flaky grading: {_e(', '.join(c.flaky) or 'none')}"
            + (f" (+{c.flaky_withheld} withheld)" if c.flaky_withheld else "")
            + "</p>"
        )
        body.append(
            _section(
                f"{c.corpus_id} {c.version or ''}".strip(),
                Raw(
                    "<p>Split counts: "
                    + _e(", ".join(f"{k} {v}" for k, v in c.split_counts.items()) or "—")
                    + "</p>"
                    + _c(
                        _table(
                            "Domains by measured difficulty",
                            ["domain", "tasks", "splits", "measured", "mean pass rate", "classes"],
                            rows,
                        )
                    )
                    + _c(lists)
                ),
            )
        )
    return _shell(feed, "corpus.html", "Corpus", "".join(body))


def _experiments(feed: DashboardFeed) -> str:
    rows = [
        [
            _link(x.page, x.experiment_id),
            x.proposal_id or "—",
            x.stage or "—",
            x.state,
            x.verdict or "—",
            x.decision_class or "—",
            x.endpoint or "—",
            f"{_signed(x.delta)} {_ival(x.interval)}".strip(),
            "—" if x.task_count is None else x.task_count,
        ]
        for x in feed.experiments
    ]
    return _shell(
        feed,
        "experiments.html",
        "Experiments",
        _section(
            "Experiments",
            _table(
                "Verdict, class, endpoint and interval per experiment",
                [
                    "experiment",
                    "proposal",
                    "stage",
                    "state",
                    "verdict",
                    "decision class",
                    "endpoint",
                    "candidate - baseline",
                    "tasks",
                ],
                rows,
            ),
        ),
    )


def _experiment_page(feed: DashboardFeed, x: ExperimentView) -> str:
    def arm(value: tuple[int, int] | None) -> str:
        return "—" if value is None else f"{value[0]}/{value[1]}"

    rows = [
        [
            t.label,
            t.split,
            arm(t.baseline),
            arm(t.candidate),
            arm(t.reference),
            Raw('<span class="bad">regression</span>') if t.regression else "",
        ]
        for t in x.tasks
    ]
    facts = {
        "verdict": x.verdict,
        "decision class": x.decision_class,
        "endpoint": x.endpoint,
        "purpose": x.purpose,
        "candidate - baseline": _signed(x.delta),
        "interval": _ival(x.interval),
        "tasks": x.task_count,
        "safety failures": x.safety_failures,
        "state": x.state,
        "splits": ", ".join(x.splits),
        "reasons": "; ".join(x.reasons),
    }
    return _shell(
        feed,
        x.page,
        f"Experiment {x.experiment_id}",
        (
            _section("Result", _facts({k: v for k, v in facts.items() if v not in (None, "")}))
            + _section(
                "Per task",
                _table(
                    "Passes/runs per arm; regressions marked",
                    ["task", "split", "baseline", "candidate", "reference", ""],
                    rows,
                ),
            )
            + f"<p>{_c(_link('experiments.html', 'All experiments'))}</p>"
        ),
    )


def _layers(feed: DashboardFeed) -> str:
    if not feed.layers:
        return _shell(feed, "layers.html", "Layers", _section("Layers", _no_data()))
    body = []
    for v in feed.layers:
        if not (v.decisions or v.tables or v.options):
            body.append(_section(v.layer, _no_data()))
            continue
        regret = v.regret or {}
        summary = _table(
            f"{v.layer}",
            [
                "decisions",
                "coverage",
                "specialised bucket",
                "pooled (global)",
                "regret (success)",
                "regret (cost)",
                "measured",
            ],
            [
                [
                    v.decisions,
                    _pct(v.coverage),
                    v.specialised_decisions,
                    v.pooled_decisions,
                    _num_s(regret.get("success_regret_mean"), 4),
                    _num_s(regret.get("cost_regret_mean"), 1),
                    regret.get("measured", "—"),
                ]
            ],
        )
        options = _table(
            "Measured per option (development rows)",
            ["option", "n", "successes", "rate", "tokens mean", "chosen"],
            [
                [
                    o["option"],
                    o["n"],
                    o["successes"],
                    _pct(o["rate"]),
                    _num_s(o["tokens_mean"], 0),
                    v.chosen.get(o["option"], 0),
                ]
                for o in v.options
            ],
            empty=False,
        )
        tables = "".join(
            _c(
                _table(
                    f"Table {t.table_id}: {t.entries} entries, {t.specialised} specialised, "
                    f"{t.pooled} pooled, rows {t.rows}",
                    ["bucket", "option", "n", "successes", "posterior", "interval", "pooled depth"],
                    [
                        [
                            e["bucket"],
                            e["option"],
                            e["n"],
                            e["successes"],
                            _num_s(e["posterior_success"], 3),
                            _facts(e["interval"]),
                            e["pooled_depth"],
                        ]
                        for e in t.top
                    ],
                )
            )
            for t in v.tables
        )
        note = f'<p class="muted">{_e(v.note)}</p>' if v.note else ""
        body.append(_section(v.layer, Raw(_c(summary) + _c(options) + tables + note)))
    return _shell(feed, "layers.html", "Layers", "".join(body))


def _strategies(feed: DashboardFeed) -> str:
    names = (
        "agent_calls",
        "turns",
        "attempts_used",
        "escalations",
        "reviewer_rounds",
        "fix_requests",
        "best_of_n_first_pass",
        "nodes",
        "sub_agents",
        "integration_conflicts",
        "re_verifications",
    )
    rows = [
        [
            s.strategy,
            s.cell_id,
            s.n,
            s.tasks,
            _pct(s.success),
            "—" if s.pass_k_rate is None else f"{_pct(s.pass_k_rate)} (k={s.pass_k})",
            _num_s(s.tokens_per_solved, 0),
            _num_s(s.seconds_per_solved, 1),
            *[_num_s(s.metrics.get(n), 2) for n in names],
        ]
        for s in feed.strategies
    ]
    return _shell(
        feed,
        "strategies.html",
        "Strategies",
        _section(
            "Strategy by cell",
            _table(
                "Per strategy and cell (all splits, aggregated; means of the §8.1 metrics)",
                [
                    "strategy",
                    "cell",
                    "n",
                    "tasks",
                    "success",
                    "pass^k",
                    "tokens / solved",
                    "seconds / solved",
                    *[n.replace("_", " ") for n in names],
                ],
                rows,
            ),
        ),
    )


def _judges(feed: DashboardFeed) -> str:
    qual = _table(
        "Qualifications",
        [
            "judge",
            "version",
            "question type",
            "n",
            "accuracy",
            "interval",
            "brier",
            "ECE",
            "repeat agreement",
            "status",
            "qualified at",
        ],
        [
            [
                q.judge_id,
                q.version,
                q.question_type,
                "—" if q.n is None else q.n,
                _pct(q.accuracy),
                _ival(q.accuracy_interval) or "—",
                _num_s(q.brier, 3),
                _num_s(q.ece_10, 3),
                _pct(q.repeat_agreement),
                Raw(f'<span class="{"ok" if q.status == "pass" else "bad"}">{_e(q.status)}</span>'),
                q.qualified_at or "—",
            ]
            for q in feed.judge_qualifications
        ],
    )
    calls = _table(
        "Calls",
        [
            "judge",
            "version",
            "question type",
            "calls",
            "tokens",
            "cost (microunits)",
            "median latency (ms)",
        ],
        [
            [
                c.judge_id,
                c.version,
                c.question_type,
                c.calls,
                _num_s(c.tokens, 0),
                _num_s(c.cost_microunits, 0),
                _num_s(c.median_latency_ms, 0),
            ]
            for c in feed.judge_calls
        ],
    )
    return _shell(
        feed, "judges.html", "Judges", _section("Qualifications", qual) + _section("Calls", calls)
    )


def _budget(feed: DashboardFeed) -> str:
    b = feed.budget
    nights = _table(
        "Nights",
        [
            "night",
            "B (trials)",
            "shares",
            "cells",
            "pilot",
            "phase",
            "trials run",
            "stopped",
            "proposals",
            "queued",
            "finished",
        ],
        [
            [
                n.date,
                "—" if n.budget_trials is None else n.budget_trials,
                _facts(n.shares),
                ", ".join(n.cells) or "—",
                "—" if n.pilot is None else ("yes" if n.pilot else "no"),
                n.phase or "—",
                "—" if n.trials is None else n.trials,
                n.stopped or "—",
                n.proposals,
                n.queued,
                n.finished_at or "—",
            ]
            for n in b.nights
        ],
    )
    quota = _table(
        "Observed quota windows (observation windows, not provider limits)",
        [
            "driver",
            "night",
            "window",
            "runs",
            "input",
            "output",
            "cached input",
            "rate-limit events",
            "limit errors",
            "first signal",
        ],
        [
            [
                q.driver_id,
                q.night or "—",
                q.window,
                _num_s(q.runs, 0),
                _num_s(q.input_tokens, 0),
                _num_s(q.output_tokens, 0),
                _num_s(q.cached_input_tokens, 0),
                _num_s(q.rate_limit_events, 0),
                _num_s(q.limit_errors, 0),
                q.first_signal_at or "—",
            ]
            for q in b.quota
        ],
    )
    keep = 0.5 if b.keep_operator_share is None else b.keep_operator_share
    head = _table(
        f"Headroom estimate (§8.6; keep {keep})",
        ["driver", "5 h tokens", "basis", "suggested B"],
        [
            [
                h.driver_id,
                _num_s(h.tokens, 0),
                h.basis,
                "—" if h.suggested_trials is None else h.suggested_trials,
            ]
            for h in b.headroom
        ],
    )
    top = Raw(
        f"<p>Nightly B: {_e(_na(b.budget_trials))} trials in the newest plan, "
        f"{_e(_na(b.configured_trials))} configured; "
        f"median tokens per trial {_e(_num_s(b.median_tokens_per_trial, 0))}.</p>"
    )
    return _shell(
        feed,
        "budget.html",
        "Budget",
        _section("Nightly budget", top)
        + _section("Nights", nights)
        + _section("Quota", quota)
        + _section("Headroom", head),
    )


def _evaluation(feed: DashboardFeed) -> str:
    ev = feed.evaluation
    names = None
    versions = _table(
        "Evaluator versions",
        ["version", "approved at", "requalification"],
        [[v.version, v.approved_at or "—", v.requalification or "—"] for v in ev.versions],
    )
    quality = []
    for q in ev.quality:
        metrics = "".join(
            _section(name, _facts(value, names), 3) for name, value in sorted(q.metrics.items())
        )
        quality.append(
            _section(
                f"{q.version} ({q.record_id})",
                Raw(
                    f"<p>Measured {_e(q.measured_at or '—')}, since {_e(q.since or '—')}.</p>"
                    + metrics
                ),
            )
        )
    changes = _table(
        "Evaluator changes",
        ["change", "state", "reason", "vacuous"],
        [[c["change_id"], c["state"], c["reason"], c["vacuous"]] for c in ev.changes],
    )
    requal = _table(
        "Requalifications",
        ["id", "version", "all equal", "reports"],
        [
            [r["id"], r["evaluator_version"], r["all_equal"], r["reports"]]
            for r in ev.requalifications
        ],
    )
    drops = _table(
        "Trace capture drops (counts only)",
        ["reason", "records"],
        [[k, v] for k, v in ev.trace_drops.items()],
    )
    return _shell(
        feed,
        "evaluation.html",
        "Evaluation",
        _section("Versions", versions)
        + (
            _section("Quality by version", Raw("".join(quality)))
            if quality
            else _section("Quality by version", _no_data())
        )
        + _section("Changes", changes)
        + _section("Requalification", requal)
        + _section("Traces", drops),
    )


def render_pages(feed: DashboardFeed) -> dict[str, str]:
    """Every page of §11.2 as ``file name -> HTML``: the ten fixed pages, one page per cell and
    one per experiment."""
    pages = {
        "index.html": _index(feed),
        "lineage.html": _lineage(feed),
        "approvals.html": _approvals(feed),
        "corpus.html": _corpus(feed),
        "experiments.html": _experiments(feed),
        "layers.html": _layers(feed),
        "strategies.html": _strategies(feed),
        "judges.html": _judges(feed),
        "budget.html": _budget(feed),
        "evaluation.html": _evaluation(feed),
    }
    for cell in feed.cells:
        pages[cell.page] = _cell_page(feed, cell)
    for experiment in feed.experiments:
        pages[experiment.page] = _experiment_page(feed, experiment)
    return pages


def write_site(feed: DashboardFeed, out: Path | str, *, feed_json: bool = False) -> list[Path]:
    """Write the pages (and ``feed.json``) into ``out``; nothing else is touched."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for name, text in render_pages(feed).items():
        path = out / name
        path.write_text(text, encoding="utf-8")
        written.append(path)
    if feed_json:
        path = out / "feed.json"
        text = json.dumps(feed_wire(feed), indent=2, sort_keys=True, default=str) + "\n"
        # still valid JSON; markup characters never appear raw, so no file of the site holds one
        text = text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written


render = (
    write_site  # the name ``NightlyBackend.dashboard`` calls (``nightly.py``): render(feed, dir)
)
