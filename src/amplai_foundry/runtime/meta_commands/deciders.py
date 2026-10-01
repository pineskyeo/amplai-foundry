"""``amplai meta decider fit | show | regret | decisions`` (Work 033 S10, interfaces.md §6, §12.1).

``fit`` reads development-split ``trial-metrics`` of the named cells (``DECIDER_SPLIT`` for any
other split, ``plan.md`` §10.3), fits a rule table with partial pooling (§6.3) and stores it as a
``decider-table``; with ``--decider`` it also registers the next version of that ``decider``
component naming the table (source ``operator``, through the proposer identity, as
``component add``), which is a class B candidate like any other (§6.5 step 3). ``show`` lists a
layer's tables and deciders, ``regret`` measures a layer's decisions in hindsight on development
trials (§6.8), ``decisions`` prints the decision records of one goal.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

import typer

from ..errors import RuntimeFault
from . import DEFAULT_CONFIG, ConfigOption, Guarded, opened_deployment

LayerOption = Annotated[str, typer.Option("--layer", help="the decision layer, L1..L8")]
CellsOption = Annotated[str, typer.Option("--cells", help="comma-separated cell ids")]
VERSION = re.compile(r"^([a-z_]+\.[A-Za-z0-9._-]+)@([1-9][0-9]*)$")


def _cells(text: str) -> list[str]:
    cells = [c.strip() for c in text.split(",") if c.strip()]
    if not cells:
        raise RuntimeFault("DECIDER_CELLS", "Name at least one cell (--cells)")
    return cells


def _layer(layer: str) -> str:
    from ...meta_harness.deciders import LAYERS

    if layer not in LAYERS:
        raise RuntimeFault("DECIDER_LAYER", "The layer is one of L1..L8", details=layer)
    return layer


def _component(store: Any, scope: Any, component_id: str) -> tuple[dict[str, Any], Any]:
    """The latest version of a component (``ID``) or exactly ``ID@VERSION``."""
    from ...meta_harness.components import ComponentService

    match = VERSION.fullmatch(component_id)
    name, version = (match.group(1), int(match.group(2))) if match else (component_id, None)
    versions = ComponentService(store, scope).versions(name)
    found = [row for row in versions if version is None or row[0]["revision"] == version]
    if not found:
        raise RuntimeFault("NOT_FOUND", "No such component version", details=component_id)
    ref, value = found[-1]
    return ref, value


def _corpus_ref(store: Any, scope: Any, cells: list[str]) -> dict[str, Any] | None:
    """The one corpus the development trial-metrics of ``cells`` come from, or None (none or
    several: the table then records none)."""
    from ...meta_harness import deciders

    found: dict[str, dict[str, Any]] = {}
    for _id, (_ref, tm) in deciders.latest(store, scope, deciders.TRIAL_METRICS_KIND).items():
        if tm.get("split") != deciders.DEVELOPMENT or tm.get("cell_id") not in cells:
            continue
        for kind, field in (
            ("eval-experiment", "experiment_ref"),
            ("calibration-plan", "calibration_plan_ref"),
        ):
            source = tm.get(field)
            if not source:
                continue
            try:
                corpus = store.get(scope, kind, source).get("corpus_ref")
            except RuntimeFault:
                continue
            if isinstance(corpus, dict) and corpus.get("digest"):
                found[str(corpus["digest"])] = corpus
    return next(iter(found.values())) if len(found) == 1 else None


def fit(
    dep: Any,
    *,
    layer: str,
    cells: list[str],
    decider: str | None,
    method: str | None,
    pooling_strength: float | None,
    confidence: float,
) -> dict[str, Any]:
    """Fit and store a rule table of ``layer`` from development trials of ``cells``."""
    from ...meta_harness import deciders
    from ...meta_harness.components import ComponentService

    store, scope = dep.store, dep.scope
    decider_ref = decider_value = None
    if decider is not None:
        decider_ref, decider_value = _component(store, scope, decider)
        if decider_value.get("kind") != "decider" or decider_value["content"]["layer"] != layer:
            raise RuntimeFault("DECIDER_LAYER", "--decider names a decider of another layer")
    if method is not None:
        method_ref, method_value = _component(store, scope, method)
    elif decider_value is not None:
        method_ref = decider_value["content"]["method"]
        method_value = ComponentService(store, scope).get(method_ref)
    else:
        raise RuntimeFault("DECIDER_METHOD", "Name the decision method (--method or --decider)")
    if method_value.get("kind") != "decision_method":
        raise RuntimeFault("DECIDER_METHOD", "--method names a decision_method component")
    features = tuple(f for f in method_value["content"]["features"]
                     if f in deciders.FEATURES[layer])  # fmt: skip
    rows = deciders.rows_from_trial_metrics(store, scope, layer=layer, cells=cells)
    options = deciders.OPTIONS.get(layer) or tuple(sorted({r.option for r in rows}))
    if not options:
        raise RuntimeFault("DECIDER_ROWS", "No development trial names an option of this layer")
    if pooling_strength is None:
        policy = (decider_value or {}).get("content", {}).get("policy") or {}
        pooling_strength = float(policy.get("pooling_strength", 4))
    table = deciders.RuleTable.fit(
        rows, layer=layer, features=features, hierarchy=deciders.default_hierarchy(features),
        options=tuple(options), pooling_strength=float(pooling_strength),
        confidence=float(confidence),
    )  # fmt: skip
    source = {
        "corpus_ref": _corpus_ref(store, scope, cells),
        "trial_metrics_query": {"cells": cells, "since": None, "splits": ["development"]},
    }
    table_ref = deciders.write_table(store, scope, table, method_ref=method_ref, source=source)
    out: dict[str, Any] = {
        "table_ref": table_ref, "layer": layer, "rows": table.rows, "options": list(options),
        "features": list(features),
    }  # fmt: skip
    if decider_value is not None:
        out["decider_ref"] = ComponentService(store, scope).register(
            dep.meta_local.proposer,
            component_id=decider_value["component_id"], kind="decider",
            content={**decider_value["content"], "table": table_ref}, source="operator",
            rationale=f"rule table fitted on {', '.join(cells)} (amplai meta decider fit)",
            parent=decider_ref,
        )  # fmt: skip
    return out


def show(dep: Any, layer: str) -> dict[str, Any]:
    from ...meta_harness import deciders

    store, scope = dep.store, dep.scope
    tables = [
        {"ref": ref, "rows": value["source"].get("rows"), "options": value["options"],
         "features": value["features"],
         "cells": value["source"]["trial_metrics_query"].get("cells"),
         "entries": len(value["cells"]), "fitted_at": value.get("fitted_at")}
        for ref, value in deciders.latest(store, scope, deciders.TABLE_KIND).values()
        if value.get("layer") == layer
    ]  # fmt: skip
    components = [
        {"ref": ref, "component_id": value["component_id"], "version": value["version"],
         "table": value["content"].get("table"), "policy": value["content"].get("policy")}
        for ref, value in store.list_objects(scope, "harness-component")
        if value.get("kind") == "decider" and value["content"].get("layer") == layer
    ]  # fmt: skip
    return {"layer": layer, "tables": tables, "deciders": components}


def regret_of(dep: Any, layer: str, cells: list[str]) -> dict[str, Any]:
    """The layer's decisions on development trials of ``cells``, against what was measured on
    the same tasks (development trial-metrics only). A decision is found through its trial's
    goal plan (``plan["decisions"]``), so its task is the trial's."""
    from ...meta_harness import deciders
    from ..evidence.cas import ArtifactStore

    store, scope = dep.store, dep.scope
    rows = deciders.rows_from_trial_metrics(store, scope, layer=layer, cells=cells)
    measured = deciders.measured_from_rows(rows)
    artifacts = ArtifactStore(store)
    chosen = []
    for _id, (_ref, tm) in sorted(
        deciders.latest(store, scope, deciders.TRIAL_METRICS_KIND).items()
    ):
        if tm.get("split") != deciders.DEVELOPMENT or tm.get("cell_id") not in cells:
            continue
        try:
            _trial, plan = deciders.trial_plan(store, scope, artifacts, tm["trial_ref"])
        except (RuntimeFault, KeyError, ValueError):
            continue
        if plan is None:
            continue
        for decision in deciders.plan_decisions(store, scope, plan):
            if decision.get("layer") == layer:
                chosen.append({**decision, "task_id": str(tm["task_id"])})
    return {"layer": layer, "cells": cells, **deciders.regret(chosen, measured)}


def goal_decisions(dep: Any, goal_id: str) -> dict[str, Any]:
    from ...meta_harness import deciders

    plan = dep.service.plan_record(goal_id)
    return {"goal_id": goal_id,
            "decisions": deciders.plan_decisions(dep.store, dep.scope, plan)}  # fmt: skip


def register(meta: typer.Typer, guarded: Guarded) -> None:
    group = typer.Typer(help="Per-layer deciders: fit tables, show, regret, decisions (§6)")
    meta.add_typer(group, name="decider")

    @group.command("fit")
    def fit_command(
        layer: LayerOption,
        cells: CellsOption,
        decider: Annotated[
            str | None,
            typer.Option("--decider", help="a decider component (ID or ID@VERSION) to version"),
        ] = None,
        method: Annotated[
            str | None,
            typer.Option("--method", help="a decision_method (ID or ID@VERSION) without --decider"),
        ] = None,
        pooling_strength: Annotated[
            float | None, typer.Option("--pooling-strength", help="m of §6.3 (default 4)")
        ] = None,
        confidence: Annotated[float, typer.Option("--confidence")] = 0.95,
        config: ConfigOption = DEFAULT_CONFIG,
    ) -> None:
        """Fit a rule table from development trials (and version a decider with it)."""

        def call() -> Any:
            with opened_deployment(config) as dep:
                return fit(
                    dep, layer=_layer(layer), cells=_cells(cells), decider=decider,
                    method=method, pooling_strength=pooling_strength, confidence=confidence,
                )  # fmt: skip

        guarded(call)

    @group.command("show")
    def show_command(layer: LayerOption, config: ConfigOption = DEFAULT_CONFIG) -> None:
        """The layer's rule tables and decider components."""

        def call() -> Any:
            with opened_deployment(config) as dep:
                return show(dep, _layer(layer))

        guarded(call)

    @group.command("regret")
    def regret_command(
        layer: LayerOption, cells: CellsOption, config: ConfigOption = DEFAULT_CONFIG
    ) -> None:
        """Regret and coverage of the layer's decisions on development trials (§6.8)."""

        def call() -> Any:
            with opened_deployment(config) as dep:
                return regret_of(dep, _layer(layer), _cells(cells))

        guarded(call)

    @group.command("decisions")
    def decisions_command(
        goal: Annotated[str, typer.Option("--goal", help="the goal id")],
        config: ConfigOption = DEFAULT_CONFIG,
    ) -> None:
        """Every harness-decision of one goal, in order."""

        def call() -> Any:
            with opened_deployment(config) as dep:
                return goal_decisions(dep, goal)

        guarded(call)
