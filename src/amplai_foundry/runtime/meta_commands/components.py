"""``amplai meta component list | show | add`` and ``amplai meta propose-components``
(Work 033 S11, interfaces.md §2.2, §3.9, §12.1).

``component add`` registers operator-authored content through the proposer identity with source
``operator`` (the install alone writes ``baseline`` versions). ``propose-components`` submits a
component candidate: ``--set SLOT=ID@VERSION`` names a stored component version per manifest slot
(``SLOT=none`` empties an optional slot); ``--observation`` states the evidence the screen needs.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated, Any

import typer

from ..errors import RuntimeFault
from . import (
    DEFAULT_CONFIG,
    AppOption,
    CellOption,
    ConfigOption,
    CorpusOption,
    Guarded,
    opened_deployment,
    opened_v2,
)

KIND = "harness-component"
SET = re.compile(r"^([A-Za-z0-9_.]+)=(?:none|([a-z_]+\.[A-Za-z0-9._-]+)@([1-9][0-9]*))$")


def _rows(store: Any, scope: Any) -> dict[str, list[tuple[dict[str, Any], dict[str, Any]]]]:
    by_id: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for ref, value in store.list_objects(scope, KIND):
        by_id.setdefault(ref["id"], []).append((ref, value))
    return {k: sorted(v, key=lambda row: row[0]["revision"]) for k, v in sorted(by_id.items())}


def _summary(ref: dict[str, Any], value: dict[str, Any]) -> dict[str, Any]:
    return {
        "ref": ref,
        **{
            k: value.get(k)
            for k in (
                "component_id",
                "kind",
                "layer",
                "version",
                "surface_class",
                "source",
                "content_digest",
                "created_at",
            )
        },
    }


def parse_set(store: Any, scope: Any, values: list[str]) -> dict[str, dict[str, Any] | None]:
    """``SLOT=ID@VERSION`` (or ``SLOT=none``) -> {slot: the stored component ref | None}."""
    changes: dict[str, dict[str, Any] | None] = {}
    rows = _rows(store, scope)
    for item in values:
        match = SET.fullmatch(item)
        if match is None:
            raise RuntimeFault("COMPONENT_SET", "--set is SLOT=ID@VERSION or SLOT=none",
                               details=item)  # fmt: skip
        slot, component_id, version = match.groups()
        if slot in changes:
            raise RuntimeFault("COMPONENT_SET", "A slot is set twice", details=slot)
        if component_id is None:
            changes[slot] = None
            continue
        found = [ref for ref, _ in rows.get(component_id, []) if ref["revision"] == int(version)]
        if not found:
            raise RuntimeFault("NOT_FOUND", "No such component version", details=item)
        changes[slot] = found[0]
    return changes


def register(meta: typer.Typer, guarded: Guarded) -> None:
    component = typer.Typer(help="Harness components: list, show, add operator content")
    meta.add_typer(component, name="component")

    @component.command("list")
    def list_(
        kind: Annotated[str | None, typer.Option("--kind", help="one component kind")] = None,
        config: ConfigOption = DEFAULT_CONFIG,
    ) -> None:
        """The latest version of every component (or of one kind)."""

        def call() -> Any:
            with opened_deployment(config) as dep:
                rows = _rows(dep.store, dep.scope)
                latest = [_summary(*versions[-1]) for versions in rows.values()]
                return {"components": [r for r in latest if kind is None or r["kind"] == kind]}

        guarded(call)

    @component.command("show")
    def show(component_id: str, config: ConfigOption = DEFAULT_CONFIG) -> None:
        """Every version of one component with its content."""

        def call() -> Any:
            with opened_deployment(config) as dep:
                versions = _rows(dep.store, dep.scope).get(component_id)
                if not versions:
                    raise RuntimeFault("NOT_FOUND", "No such component", details=component_id)
                return {
                    "component_id": component_id,
                    "versions": [{**_summary(ref, value), "content": value.get("content"),
                                  "parent": value.get("parent"),
                                  "rationale": value.get("rationale")}
                                 for ref, value in versions],
                }  # fmt: skip

        guarded(call)

    @component.command("add")
    def add(
        kind: Annotated[str, typer.Option("--kind", help="the component kind (§2.2)")],
        name: Annotated[str, typer.Option("--name", help="the id is <kind>.<name>")],
        file: Annotated[Path, typer.Option("--file", help="the content as JSON")],
        rationale: Annotated[
            str, typer.Option("--rationale", help="why this version exists")
        ] = "operator-authored content (amplai meta component add)",
        config: ConfigOption = DEFAULT_CONFIG,
    ) -> None:
        """Register operator-authored content as a new version (source operator)."""

        def call() -> Any:
            from ...meta_harness.components import ComponentService

            content = json.loads(Path(file).expanduser().read_text())
            if not isinstance(content, dict):
                raise RuntimeFault("COMPONENT_CONTENT", "The content file holds a JSON object")
            with opened_deployment(config) as dep:
                ref = ComponentService(dep.store, dep.scope).register(
                    dep.meta_local.proposer, component_id=f"{kind}.{name}", kind=kind,
                    content=content, source="operator", rationale=rationale,
                )  # fmt: skip
                return {"component_id": f"{kind}.{name}", "ref": ref}

        guarded(call)

    @meta.command("propose-components")
    def propose_components(
        cell: CellOption,
        changes: Annotated[list[str], typer.Option("--set", help="SLOT=ID@VERSION or SLOT=none")],
        suffix: Annotated[str, typer.Option("--suffix", help="names the candidate")],
        hypothesis: Annotated[str, typer.Option("--hypothesis")],
        benefit: Annotated[str, typer.Option("--benefit")],
        risk: Annotated[list[str], typer.Option("--risk")],
        observation: Annotated[
            list[str], typer.Option("--observation", help="the evidence behind the hypothesis")
        ],
        prediction_file: Annotated[
            Path | None, typer.Option("--prediction-file", help="a §2.12 prediction (JSON)")
        ] = None,
        config: ConfigOption = DEFAULT_CONFIG,
        corpus: CorpusOption = None,
        app: AppOption = None,
    ) -> None:
        """Submit a component candidate of a cell as the meta-proposer."""

        def call() -> Any:
            prediction = (
                json.loads(Path(prediction_file).expanduser().read_text())
                if prediction_file is not None
                else None
            )
            with opened_v2(config, corpus, cell=cell, app=app) as ops:
                return {
                    "proposal_id": ops.propose_components(
                        cell_id=cell,
                        changes=parse_set(ops.store, ops.scope, list(changes)),
                        suffix=suffix,
                        hypothesis=hypothesis,
                        expected_benefit=benefit,
                        risks=list(risk),
                        observation_refs=[ops._observation(text) for text in observation],
                        prediction=prediction,
                    )
                }

        guarded(call)
