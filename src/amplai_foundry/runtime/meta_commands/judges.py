"""``amplai meta judge list | label | qualify`` (Work 033 S10, interfaces.md §6.6, §6.7, §12.1).

``label`` stores an operator-labelled ``judge-label-set``: the file holds
``{"items": [{"item_id", "state": {"text", "data_class"}, "question": {"question_id", "text",
"choices"?, "scale"?}, "label"}]}``; each state is admitted as an artifact (only its digest ever
reaches a judge-call). ``qualify`` asks one judge every item ``--repeats`` times and stores the
``judge-qualification`` (accuracy, calibration, stability, cost, §6.7). Judges:
``llm_cell.<cell>`` (the cell's read-only turn, on an empty scratch directory, IC-14) and ``jev``
(a disabled stub: it holds JUDGE_NOT_CONFIGURED, §14 Q9). An unqualified judge is never used.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from ..errors import Hold, RuntimeFault
from . import DEFAULT_CONFIG, ConfigOption, Guarded, opened_deployment

TypeOption = Annotated[str, typer.Option("--type", help="yes_no, choice or score")]


def _question_type(value: str) -> str:
    from ...meta_harness.judges import QUESTION_TYPES

    if value not in QUESTION_TYPES:
        raise RuntimeFault("JUDGE_QUESTION", "The question type is yes_no, choice or score")
    return value


def connector(dep: Any, judge_id: str) -> Any:
    """The connector a judge id names (``llm_cell.<cell>`` or ``jev``)."""
    from ...meta_harness.judges import JevConfig, JevJudge, LlmCellJudge

    if judge_id == "jev":
        entry = getattr(dep.config, "jev", None)
        config = (
            JevConfig(
                enabled=entry.enabled, endpoint=entry.endpoint, token_file=entry.token_file,
                data_classes_allowed=tuple(entry.data_classes_allowed),
            )
            if entry is not None
            else None
        )  # fmt: skip
        return JevJudge(config)
    if not judge_id.startswith("llm_cell."):
        raise RuntimeFault("JUDGE_UNKNOWN", "A judge is llm_cell.<cell> or jev", details=judge_id)
    cell = judge_id[len("llm_cell.") :]
    composition = next(
        (app.compositions[cell] for app in dep.service.apps.values() if cell in app.compositions),
        None,
    )
    if composition is None:
        raise Hold("CELL_UNKNOWN", "No installed app has this cell", details=cell)
    value = dep.store.get(dep.scope, "harness-composition", composition)
    model = dep.store.get(dep.scope, "model-profile", value["model_profile_ref"])
    return LlmCellJudge(
        dep.service.strategy_runner().turns(cell),
        data_classes_allowed=frozenset(model.get("data_classes_allowed") or []),
        scratch_root=dep.service.workspaces.root,
    )  # fmt: skip


def label_set_ref(dep: Any, text: str) -> dict[str, Any]:
    """``ID`` (its latest revision) or ``ID@REVISION`` of a judge-label-set."""
    from ...meta_harness.judges import LABELS_KIND

    name, _, revision = text.partition("@")
    rows = [ref for ref, _ in dep.store.list_objects(dep.scope, LABELS_KIND) if ref["id"] == name]
    if revision:
        rows = [r for r in rows if str(r["revision"]) == revision]
    if not rows:
        raise RuntimeFault("NOT_FOUND", "No such judge-label-set", details=text)
    ref: dict[str, Any] = max(rows, key=lambda r: r["revision"])
    return ref


def label(dep: Any, question_type: str, file: Path) -> dict[str, Any]:
    from ...meta_harness.judges import admit_state, write_label_set

    raw = json.loads(Path(file).expanduser().read_text())
    entries = raw.get("items") if isinstance(raw, dict) else None
    if not isinstance(entries, list) or not entries:
        raise RuntimeFault("JUDGE_LABELS", 'The file holds {"items": [...]}')
    actor = dep.operator()
    items = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("state"), dict):
            raise RuntimeFault("JUDGE_LABELS", "Every item has a state {text, data_class}")
        state = entry["state"]
        items.append({
            "item_id": str(entry.get("item_id") or ""),
            "state_ref": admit_state(
                dep.artifacts, dep.scope, state.get("text"), state.get("data_class")
            ),
            "question": {**(entry.get("question") or {}), "type": question_type},
            "label": entry.get("label"),
            "labelled_by": actor.wire(),
            "source": "operator",
        })  # fmt: skip
    ref = write_label_set(dep.store, dep.scope, question_type=question_type, items=items)
    return {"label_set_ref": ref, "items": len(items), "question_type": question_type}


def listing(dep: Any) -> dict[str, Any]:
    from ...meta_harness.judges import LABELS_KIND, qualifications

    labels = [
        {"ref": ref, "question_type": value.get("question_type"), "items": len(value["items"])}
        for ref, value in dep.store.list_objects(dep.scope, LABELS_KIND)
    ]
    qualified = [
        {"ref": ref, **{k: value.get(k) for k in (
            "judge_id", "judge_version", "question_type", "n", "accuracy", "accuracy_interval",
            "ece_10", "repeat_agreement", "median_cost_tokens", "median_latency_ms", "status")}}
        for ref, value in qualifications(dep.store, dep.scope)
    ]  # fmt: skip
    cells = sorted({cell for app in dep.service.apps.values() for cell in app.compositions})
    jev = getattr(dep.config, "jev", None)
    return {
        "judges": [*("llm_cell." + c for c in cells), "jev"],
        "jev_enabled": bool(jev is not None and jev.enabled),
        "label_sets": labels,
        "qualifications": qualified,
    }


def register(meta: typer.Typer, guarded: Guarded) -> None:
    group = typer.Typer(help="Judges: list, label, qualify (§6.6-6.7)")
    meta.add_typer(group, name="judge")

    @group.command("list")
    def list_command(config: ConfigOption = DEFAULT_CONFIG) -> None:
        """Judges, label sets and qualifications."""

        def call() -> Any:
            with opened_deployment(config) as dep:
                return listing(dep)

        guarded(call)

    @group.command("label")
    def label_command(
        question_type: TypeOption,
        file: Annotated[Path, typer.Option("--file", help="the labelled items (JSON)")],
        config: ConfigOption = DEFAULT_CONFIG,
    ) -> None:
        """Store operator-labelled items as a judge-label-set."""

        def call() -> Any:
            with opened_deployment(config) as dep:
                return label(dep, _question_type(question_type), file)

        guarded(call)

    @group.command("qualify")
    def qualify_command(
        judge: Annotated[str, typer.Option("--judge", help="llm_cell.<cell> or jev")],
        question_type: TypeOption,
        labels: Annotated[str, typer.Option("--labels", help="a judge-label-set ID[@REVISION]")],
        repeats: Annotated[int, typer.Option("--repeats", help="asks per item")] = 3,
        config: ConfigOption = DEFAULT_CONFIG,
    ) -> None:
        """Qualify one judge for one question type on a label set (§6.7 thresholds)."""

        def call() -> Any:
            from ...meta_harness.judges import QUALIFICATION_KIND, JudgeQualifier

            with opened_deployment(config) as dep:
                ref = label_set_ref(dep, labels)
                stored = dep.store.get(dep.scope, "judge-label-set", ref)
                if stored["question_type"] != _question_type(question_type):
                    raise RuntimeFault("JUDGE_LABELS", "The label set has another question type")
                qualifier = JudgeQualifier(
                    dep.store, dep.scope, artifacts=dep.artifacts,
                    workspace_root=dep.service.workspaces.root,
                )  # fmt: skip
                result = qualifier.qualify(
                    connector(dep, judge), ref, repeats=repeats, thresholds={}
                )
                stored = dep.store.get(dep.scope, QUALIFICATION_KIND, result)
                return {"qualification_ref": result, "qualification": stored}

        guarded(call)
