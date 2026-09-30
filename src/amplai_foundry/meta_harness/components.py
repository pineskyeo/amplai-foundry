"""Versioned harness components (Work 033 S3, D-096, interfaces.md §2.2, §3.2).

A component is one ``harness-component`` record per version: id = ``component_id``
(``<kind>.<name>``), store revision = ``version``. Nothing is overwritten; a new version is a new
store revision and identical content returns the latest version. The kind fixes the layer, the
surface class (design 16 §3: task prompt excerpts and approved notes are A; repair heuristics,
retrieval, driver mapping and routing are B) and the carrier record that names it. Content is
validated by ``runtime/execution/policies.py``, which the execution path reads.

Who registers: the install (``runtime.admin``) writes only ``baseline`` content; everything else
is written by a ``harness.propose`` identity (the proposer, or the operator through it).
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any, Literal

from ..runtime.contracts.authority import Actor
from ..runtime.contracts.identity import ID, digest, now
from ..runtime.errors import Hold, RuntimeFault
from ..runtime.execution import policies
from ..runtime.storage.store import Scope, Store

Ref = dict[str, Any]
COMPONENT_ID = re.compile(r"^[a-z_]+\.[A-Za-z0-9._-]+$")  # no ":" (a change path, §2.12)


@dataclass(frozen=True)
class KindSpec:
    kind: str
    layer: str  # "L1".."L9", a range as in the §2.2 table, or "-"
    surface_class: Literal["A", "B"]
    carrier: Literal[
        "prompt-bundle", "context-policy", "budget-policy", "router-policy", "decider", "none"
    ]  # "decider": decision_method / judge_model, referenced by a decider (§2.2)
    slot: str  # key inside the carrier's "components"/"deciders"; "method"/"judge" for "decider"


# Exactly the §2.2 table. A decider's carrier depends on its layer (DECIDER_CARRIERS): router
# (L1-L3), context (L4), budget (L5-L8); the kind itself names no single carrier.
KINDS: dict[str, KindSpec] = {
    k.kind: k
    for k in (
        KindSpec("role_prompt", "L4", "A", "prompt-bundle", "prompt_bundle_ref"),
        KindSpec("interpretation", "L1", "A", "router-policy", "interpretation"),
        KindSpec("env_bootstrap", "L4", "A", "context-policy", "env_bootstrap"),
        KindSpec("memory_notes", "L4", "A", "context-policy", "memory_notes"),
        KindSpec("retrieval", "L4", "B", "context-policy", "retrieval"),
        KindSpec("feedback_form", "L6", "B", "context-policy", "feedback_form"),
        KindSpec("attempt_policy", "L6", "B", "budget-policy", "attempt_policy"),
        KindSpec("execution_strategy", "L2", "B", "budget-policy", "execution_strategy"),
        KindSpec("driver_options", "L5", "B", "budget-policy", "driver_options"),
        KindSpec("fast_checks", "L7", "B", "budget-policy", "fast_checks"),
        KindSpec("limits", "L8", "B", "budget-policy", "limits"),
        KindSpec("route_policy", "L2/L3", "B", "router-policy", "route_policy"),
        KindSpec("decider", "L1-L8", "B", "none", "deciders"),
        KindSpec("decision_method", "-", "B", "decider", "method"),
        KindSpec("judge_model", "-", "B", "decider", "judge"),
        KindSpec("environment_image", "L9", "B", "none", "-"),  # none this round (D-096)
    )
}
DECIDER_CARRIERS = {
    **{layer: "router-policy" for layer in policies.ROUTER_DECIDERS},
    **{layer: "context-policy" for layer in policies.CONTEXT_DECIDERS},
    **{layer: "budget-policy" for layer in policies.BUDGET_DECIDERS},
}
# what a decider's refs must name (§2.2 decider row); options by layer
DECIDER_REFS = {"method": "decision_method", "judge": "judge_model"}
DECIDER_OPTIONS = {"L5": "driver_options", "L8": "limits"}


def _check_id(component_id: Any, kind: str) -> None:
    if (
        not isinstance(component_id, str)
        or not COMPONENT_ID.fullmatch(component_id)
        or not ID.fullmatch(component_id)
        or component_id.split(".", 1)[0] != kind  # "<kind>.<name>" (§9.5)
    ):
        raise RuntimeFault(
            "COMPONENT_ID", "A component id is <kind>.<name> without ':' (at most 128 characters)",
            details=component_id,
        )  # fmt: skip


class ComponentService:
    KIND = "harness-component"

    def __init__(self, store: Store, scope: Scope) -> None:
        self.store, self.scope = store, scope

    def _authorize(self, actor: Actor, source: str) -> None:
        if actor.scope != self.scope:
            raise RuntimeFault("FORBIDDEN", "Actor acts in another scope")
        if source not in policies.SOURCES:
            raise RuntimeFault("COMPONENT_CONTENT", "Unknown component source", details=source)
        # install writes the v1 baselines; every other version comes from a proposer identity
        actor.require("runtime.admin" if source == "baseline" else "harness.propose")

    def _check_refs(self, kind: str, content: dict[str, Any]) -> None:
        """A decider names components of the kinds its row allows (§2.2)."""
        if kind != "decider":
            return
        named = [(content[f], want) for f, want in DECIDER_REFS.items() if content[f] is not None]
        want_option = DECIDER_OPTIONS.get(content["layer"])
        named += [(ref, want_option) for ref in content["options"] or [] if want_option]
        for ref, want in named:
            try:
                found = self.get(ref)["kind"]
            except (RuntimeFault, KeyError, TypeError):
                found = None
            if found != want:
                raise RuntimeFault(
                    "COMPONENT_CONTENT", f"decider: a ref must name a {want} component",
                    details={"ref": ref, "found": found},
                )  # fmt: skip
        if content["table"] is not None:
            try:
                self.store.get(self.scope, "decider-table", content["table"])
            except RuntimeFault as exc:
                raise RuntimeFault(
                    "COMPONENT_CONTENT", "decider: table must name a decider-table record"
                ) from exc

    def _check_parents(
        self, component_id: str, kind: str, parent: Ref | None, merge_parents: list[Ref]
    ) -> None:
        """Hold COMPONENT_PARENT: a parent is an earlier version of this component; a dreaming
        merge parent is a component of the same kind."""
        checks = [(parent, True)] if parent is not None else []
        checks += [(m, False) for m in merge_parents]
        for ref, same_id in checks:
            try:
                value = self.get(ref)
            except (RuntimeFault, KeyError, TypeError):  # missing, or not a ref at all
                value = {}
            if value.get("kind") != kind or (same_id and value.get("component_id") != component_id):
                raise Hold(
                    "COMPONENT_PARENT", "The parent is a component of another kind or id",
                    details={"parent": ref},
                )  # fmt: skip

    def register(
        self,
        actor: Actor,
        *,
        component_id: str,
        kind: str,
        content: dict[str, Any],
        source: str,
        rationale: str,
        parent: Ref | None = None,
        merge_parents: list[Ref] | None = None,
    ) -> Ref:
        """Version = latest store revision + 1; identical content returns the latest ref.

        RuntimeFault FORBIDDEN | COMPONENT_KIND | COMPONENT_CONTENT | COMPONENT_ID;
        Hold COMPONENT_PARENT.
        """
        self._authorize(actor, source)
        spec = KINDS.get(kind)
        if spec is None:
            raise RuntimeFault("COMPONENT_KIND", "Unknown component kind", details=kind)
        _check_id(component_id, kind)
        policies.validate_content(kind, content)
        merges = list(merge_parents or [])
        if not isinstance(rationale, str) or not 1 <= len(rationale) <= 4000:
            raise RuntimeFault("COMPONENT_CONTENT", "rationale is 1..4000 characters")
        if len(merges) > 4 or (merges and source != "dreaming"):
            raise RuntimeFault("COMPONENT_CONTENT", "0..4 merge parents, dreaming merges only")
        if (
            kind == "memory_notes"
            and source == "dreaming"
            and any(not n["evidence"] for n in content["notes"])
        ):
            raise RuntimeFault("COMPONENT_CONTENT", "A dreaming note cites at least one trace")
        self._check_refs(kind, content)
        self._check_parents(component_id, kind, parent, merges)
        versions = self.versions(component_id)
        content_digest = digest(content)
        if versions and versions[-1][1]["content_digest"] == content_digest:
            return versions[-1][0]
        version = versions[-1][0]["revision"] + 1 if versions else 1
        record = {
            "schema": "amplai.harness-component.v1",
            "scope": self.scope.wire(),
            "component_id": component_id,
            "kind": kind,
            # a decider's layer is its content's; every other kind's is fixed by KINDS
            "layer": content["layer"] if kind == "decider" else spec.layer,
            "version": version,
            "surface_class": spec.surface_class,
            "content": content,
            "content_digest": content_digest,
            "parent": parent,
            "merge_parents": merges,
            "source": source,
            "rationale": rationale,
            "created_at": now(),
        }
        with self.store.tx() as db:
            return self.store.put(db, self.scope, self.KIND, component_id, version, record)

    def get(self, ref: Ref) -> dict[str, Any]:
        return self.store.get(self.scope, self.KIND, ref)

    def versions(self, component_id: str) -> list[tuple[Ref, dict[str, Any]]]:
        rows = [
            (ref, value) for ref, value in self.store.list_objects(self.scope, self.KIND)
            if ref["id"] == component_id
        ]  # fmt: skip
        return sorted(rows, key=lambda row: row[0]["revision"])

    def baseline(self, actor: Actor, kind: str) -> Ref:
        """``<kind>.baseline`` with the v1 content (idempotent)."""
        if kind not in policies.V1 or kind not in KINDS:
            raise RuntimeFault("COMPONENT_KIND", "The kind has no v1 content", details=kind)
        return self.register(
            actor,
            component_id=kind + ".baseline",
            kind=kind,
            content=copy.deepcopy(policies.V1[kind]),
            source="baseline",
            rationale="v1: today's behaviour (interfaces.md §2.2)",
        )

    def find(self, component_id: str, content: dict[str, Any]) -> Ref | None:
        """The version of ``component_id`` whose content equals ``content``, if any."""
        want = digest(content)
        return next((r for r, v in self.versions(component_id) if v["content_digest"] == want),
                    None)  # fmt: skip
