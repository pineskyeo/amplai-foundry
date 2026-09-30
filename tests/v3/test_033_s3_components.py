"""Work 033 S3 (D-096, interfaces.md §2.2, §3.1-3.2): components are versioned records with a class.

Real: the store, `ComponentService`, the content validators of `runtime/execution/policies.py`.
AC-01 (unit part): versions are store revisions, identical content is not a new version, the kind
fixes layer and surface class, a component id with ":" is refused, and every v1 content reproduces
today's values (the deployment Budget, the router order, the IMPLEMENTER lines, the feedback tail).
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from amplai_foundry.meta_harness.components import KINDS, ComponentService
from amplai_foundry.runtime.contracts.authority import Actor
from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.runtime.execution import loop, policies, prompts
from amplai_foundry.runtime.execution.product import ROUTER_ORDER, Budget
from amplai_foundry.runtime.storage.store import Scope, Store

SCOPE = Scope("tenant-s3", "project-s3")
ADMIN = Actor("installer", SCOPE, frozenset({"runtime.admin"}), "service")
PROPOSER = Actor("meta-proposer", SCOPE, frozenset({"harness.propose"}), "service")
ON = {**policies.V1["env_bootstrap"], "enabled": True}


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    value = Store(tmp_path / "state")
    yield value
    value.close()


@pytest.fixture
def components(store: Store) -> ComponentService:
    return ComponentService(store, SCOPE)


def register(components: ComponentService, **kw: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "component_id": "env_bootstrap.on",
        "kind": "env_bootstrap",
        "content": ON,
        "source": "proposer",
        "rationale": "switch the environment facts on",
    }
    args.update(kw)
    actor = args.pop("actor", PROPOSER)
    return components.register(actor, **args)


# ---------------------------------------------------------------- the catalogue (§2.2)

TABLE = {  # kind: (layer, class, carrier) exactly as the §2.2 table
    "role_prompt": ("L4", "A", "prompt-bundle"),
    "interpretation": ("L1", "A", "router-policy"),
    "env_bootstrap": ("L4", "A", "context-policy"),
    "memory_notes": ("L4", "A", "context-policy"),
    "retrieval": ("L4", "B", "context-policy"),
    "feedback_form": ("L6", "B", "context-policy"),
    "attempt_policy": ("L6", "B", "budget-policy"),
    "execution_strategy": ("L2", "B", "budget-policy"),
    "driver_options": ("L5", "B", "budget-policy"),
    "fast_checks": ("L7", "B", "budget-policy"),
    "limits": ("L8", "B", "budget-policy"),
    "route_policy": ("L2/L3", "B", "router-policy"),
    "decider": ("L1-L8", "B", "none"),
    "decision_method": ("-", "B", "decider"),
    "judge_model": ("-", "B", "decider"),
    "environment_image": ("L9", "B", "none"),
}


def test_the_kind_catalogue_is_the_interfaces_table() -> None:
    assert {k: (s.layer, s.surface_class, s.carrier) for k, s in KINDS.items()} == TABLE
    # every kind has a content validator in the runtime, and nothing else does
    assert set(policies.CONTENT_KINDS) == set(KINDS)


@pytest.mark.parametrize("kind", sorted(policies.V1))
def test_every_v1_content_is_valid(kind: str) -> None:
    policies.validate_content(kind, copy.deepcopy(policies.V1[kind]))


def test_the_v1_contents_are_todays_values() -> None:
    v1 = policies.V1
    budget = Budget()
    assert v1["limits"] == {
        "max_wall_seconds": budget.max_wall_seconds,
        "max_tokens": budget.max_tokens,
        "max_attempts": budget.max_attempts,
        "aux_max_tokens": 0,
    }
    assert v1["attempt_policy"]["max_attempts"] == budget.max_attempts
    assert v1["route_policy"] == {"order": {"*": list(ROUTER_ORDER)}, "roles": {}}
    assert v1["role_prompt"]["implementer"] == list(prompts.IMPLEMENTER_BASELINE)
    assert v1["feedback_form"]["tail_chars"] == loop.FEEDBACK_TAIL == 3000
    assert all(not v1[k]["enabled"] for k in ("env_bootstrap", "memory_notes", "retrieval"))
    assert v1["execution_strategy"] == {"enabled": ["repair_loop"], "params": {}}
    assert "driver_options" not in v1  # v1 is null: argv unchanged
    assert [policies.STRATEGY_RANK[s] for s in policies.STRATEGIES] == list(range(1, 11))


# ---------------------------------------------------------------- content validation


def _with(kind: str, **fields: Any) -> tuple[str, dict[str, Any]]:
    return kind, {**copy.deepcopy(policies.V1[kind]), **fields}


def _note(**fields: Any) -> dict[str, Any]:
    note = {
        "text": "run the unit tests first",
        "app": "app",
        "task_class": None,
        "evidence": [],
        "dated": "2026-09-30",
    }
    return {**note, **fields}


DRIVER_OPTIONS = {
    "claude": {"max_turns": None, "append_system_prompt": None, "allowed_tools": None},
    "codex": {"config": []},
}
METHOD = {
    "features": ["task_class"],
    "estimator": "pooled_beta_binomial_v1",
    "selection": "noninferior_then_cheapest_v1",
    "fallback": "prior_v1",
    "utility_lambda": None,
}
REF = {"id": "x", "revision": 1, "digest": "sha256:" + "0" * 64}
POLICY = {"min_samples": 5, "margin": 0.1, "pooling_strength": 4}
DECIDER = {
    "layer": "L4",
    "method": REF,
    "table": None,
    "judge": None,
    "options": None,
    "policy": POLICY,
}

INVALID = [
    _with("env_bootstrap", tree_depth=4),
    _with("env_bootstrap", facts=["secrets"]),
    _with("env_bootstrap", enabled="yes"),
    _with("env_bootstrap", extra=1),
    _with("memory_notes", notes=[_note()] * 41),  # > 40 per (app, task_class)
    _with("memory_notes", notes=[_note(text="x" * 301)]),
    _with("memory_notes", notes=[_note(dated="2026-13-01")]),
    _with("memory_notes", notes=[_note(task_class="not_a_class")]),
    _with("memory_notes", notes=[_note(evidence=["t"] * 9)]),
    _with("retrieval", method="bm25"),
    _with("retrieval", max_items=21),
    _with("feedback_form", mode="full_log"),
    _with("feedback_form", tail_chars=20001),
    _with("feedback_form", header="v2"),
    _with("feedback_form", detail_keys=[f"k{i}" for i in range(9)]),
    _with("attempt_policy", max_attempts=4),  # IC-06
    _with("attempt_policy", repair_base="stash"),
    _with("attempt_policy", feedback="true"),
    _with("execution_strategy", enabled=[]),
    _with("execution_strategy", enabled=["swarm"]),
    _with("execution_strategy", enabled=["single"], params={"vote": {"k": 2}}),
    _with("execution_strategy", enabled=["best_of_n"], params={"best_of_n": {"n": 4}}),
    ("driver_options", {**DRIVER_OPTIONS, "codex": {"config": [["model", "x"]]}}),  # empty list
    (
        "driver_options",
        {**DRIVER_OPTIONS, "claude": {**DRIVER_OPTIONS["claude"], "max_turns": 501}},
    ),
    (
        "driver_options",
        {**DRIVER_OPTIONS, "claude": {**DRIVER_OPTIONS["claude"], "allowed_tools": ["Task"]}},
    ),
    _with("fast_checks", checks=["a", "b", "c", "d", "e"]),
    _with("fast_checks", max_followups=3),
    _with("limits", aux_max_tokens=60_000_000),  # aux < max_tokens (IC-21)
    _with("limits", max_attempts=4),
    _with("limits", max_wall_seconds=0),
    _with("route_policy", order={"bug_fix": ["codex-cli"]}),  # no "*"
    _with("route_policy", order={"*": ["codex-cli"], "no_class": ["codex-cli"]}),
    _with("route_policy", roles={"judge": ["codex-cli"]}),
    ("role_prompt", {"implementer": ["uses {secret}"]}),
    _with("interpretation", planner_instruction="guess"),
    ("judge_model", {"judge": "oracle"}),
    ("environment_image", {"image": "localhost:5000/image:latest"}),
    ("decider", {**DECIDER, "options": [REF]}),  # options only for L5 and L8
    ("decider", {**DECIDER, "layer": "L5"}),  # L5 needs options
    ("decider", {**DECIDER, "policy": {**POLICY, "margin": 0.6}}),
    ("decision_method", {**METHOD, "estimator": "neural"}),
]


@pytest.mark.parametrize(("kind", "content"), INVALID)
def test_invalid_content_is_refused(kind: str, content: dict[str, Any]) -> None:
    with pytest.raises(RuntimeFault) as bad:
        policies.validate_content(kind, content)
    assert bad.value.code == "COMPONENT_CONTENT"


@pytest.mark.parametrize(
    ("kind", "content"),
    [
        ("driver_options", DRIVER_OPTIONS),
        ("decision_method", METHOD),
        ("decider", DECIDER),
        ("environment_image", {"image": "localhost:5000/x@sha256:" + "a" * 64}),
        ("judge_model", {"judge": "llm_cell", "cell": "claude-cli", "question_types": ["yes_no"]}),
        _with(
            "execution_strategy",
            enabled=["repair_loop", "cascade"],
            params={"cascade": {"cells": ["codex-cli", "claude-cli"], "max_escalations": 1}},
        ),
        _with("memory_notes", enabled=True, notes=[_note()] * 40 + [_note(task_class="bug_fix")]),
    ],
)
def test_valid_non_v1_content_is_accepted(kind: str, content: dict[str, Any]) -> None:
    policies.validate_content(kind, content)


def test_an_unknown_kind_is_a_fault(components: ComponentService) -> None:
    with pytest.raises(RuntimeFault) as unknown:
        policies.validate_content("prompt_magic", {})
    assert unknown.value.code == "COMPONENT_KIND"
    with pytest.raises(RuntimeFault) as again:
        register(components, component_id="prompt_magic.x", kind="prompt_magic")
    assert again.value.code == "COMPONENT_KIND"


# ---------------------------------------------------------------- versions


def test_versions_are_store_revisions_and_identical_content_is_not_new(
    components: ComponentService, store: Store
) -> None:
    first = register(components)
    assert first["id"] == "env_bootstrap.on" and first["revision"] == 1
    assert register(components) == first  # identical content -> the latest ref
    second = register(components, content={**ON, "tree_depth": 3}, parent=first)
    assert second["revision"] == 2
    rows = components.versions("env_bootstrap.on")
    assert [r["revision"] for r, _ in rows] == [1, 2]
    value = components.get(second)
    assert value["schema"] == "amplai.harness-component.v1" and value["scope"] == SCOPE.wire()
    assert (value["kind"], value["layer"], value["surface_class"]) == ("env_bootstrap", "L4", "A")
    assert value["version"] == 2 and value["parent"] == first and value["source"] == "proposer"
    assert value["content"]["tree_depth"] == 3
    from amplai_foundry.runtime.contracts.identity import digest

    assert value["content_digest"] == digest(value["content"])
    # nothing was overwritten: version 1 still reads with its own content
    assert components.get(first)["content"]["tree_depth"] == ON["tree_depth"]


def test_the_kind_fixes_the_surface_class(components: ComponentService) -> None:
    ref = register(
        components,
        component_id="feedback_form.short",
        kind="feedback_form",
        content={**policies.V1["feedback_form"], "tail_chars": 500},
    )
    assert components.get(ref)["surface_class"] == "B"
    assert components.get(ref)["layer"] == "L6"


@pytest.mark.parametrize(
    "component_id",
    [
        "env_bootstrap.a:b",  # ":" is refused (a change path, §2.12)
        "env_bootstrap:on",
        "memory_notes.on",  # the prefix is the kind (<kind>.<name>, §9.5)
        "env_bootstrap",
        "Env_bootstrap.on",
        "env_bootstrap.on/x",
        "env_bootstrap." + "x" * 120,  # longer than 128 characters
    ],
)
def test_component_id_rules(components: ComponentService, component_id: str) -> None:
    with pytest.raises(RuntimeFault) as bad:
        register(components, component_id=component_id)
    assert bad.value.code == "COMPONENT_ID"
    assert components.versions(component_id) == []


# ---------------------------------------------------------------- who registers


@pytest.mark.parametrize(
    ("actor", "source"),
    [
        (Actor("nobody", SCOPE, frozenset(), "service"), "proposer"),
        (ADMIN, "proposer"),  # the install writes baselines only
        (PROPOSER, "baseline"),  # a proposer never writes a baseline
        (
            Actor("x", Scope("other", "scope"), frozenset({"harness.propose"}), "service"),
            "proposer",
        ),
    ],
)
def test_who_may_register(components: ComponentService, actor: Actor, source: str) -> None:
    with pytest.raises(RuntimeFault) as denied:
        register(components, actor=actor, source=source)
    assert denied.value.code == "FORBIDDEN"


def test_record_fields_are_bounded(components: ComponentService) -> None:
    for kw in ({"rationale": ""}, {"rationale": "x" * 4001}, {"source": "somebody"}):
        with pytest.raises(RuntimeFault) as bad:
            register(components, **kw)
        assert bad.value.code == "COMPONENT_CONTENT"


# ---------------------------------------------------------------- parents


def test_a_parent_is_an_earlier_version_of_the_same_component(
    components: ComponentService,
) -> None:
    other_id = register(components, component_id="env_bootstrap.other")
    notes = register(
        components,
        component_id="memory_notes.x",
        kind="memory_notes",
        content=policies.V1["memory_notes"],
    )
    for parent in (other_id, notes, REF):
        with pytest.raises(Hold) as held:
            register(components, content={**ON, "max_chars": 10}, parent=parent)
        assert held.value.code == "COMPONENT_PARENT"
    first = register(components)
    assert register(components, content={**ON, "max_chars": 10}, parent=first)["revision"] == 2


def test_merge_parents_are_for_dreaming_and_dreaming_notes_cite_traces(
    components: ComponentService,
) -> None:
    a = register(
        components,
        component_id="memory_notes.a",
        kind="memory_notes",
        content={"enabled": True, "notes": [_note(evidence=["trace-1"])]},
    )
    with pytest.raises(RuntimeFault) as not_dreaming:
        register(
            components,
            component_id="memory_notes.m",
            kind="memory_notes",
            content={"enabled": True, "notes": [_note(evidence=["trace-2"])]},
            merge_parents=[a],
        )
    assert not_dreaming.value.code == "COMPONENT_CONTENT"
    with pytest.raises(RuntimeFault) as uncited:
        register(
            components,
            component_id="memory_notes.m",
            kind="memory_notes",
            content={"enabled": True, "notes": [_note()]},
            source="dreaming",
        )
    assert uncited.value.code == "COMPONENT_CONTENT"
    other_kind = register(components)
    with pytest.raises(Hold) as held:
        register(
            components,
            component_id="memory_notes.m",
            kind="memory_notes",
            content={"enabled": True, "notes": [_note(evidence=["trace-2"])]},
            source="dreaming",
            merge_parents=[a, other_kind],
        )
    assert held.value.code == "COMPONENT_PARENT"
    merged = register(
        components,
        component_id="memory_notes.m",
        kind="memory_notes",
        content={"enabled": True, "notes": [_note(evidence=["trace-2"])]},
        source="dreaming",
        merge_parents=[a],
    )
    assert components.get(merged)["merge_parents"] == [a]


# ---------------------------------------------------------------- baselines and deciders


def test_baseline_is_v1_and_idempotent(components: ComponentService) -> None:
    ref = components.baseline(ADMIN, "feedback_form")
    value = components.get(ref)
    assert ref["id"] == "feedback_form.baseline" and value["version"] == 1
    assert value["source"] == "baseline" and value["content"] == policies.V1["feedback_form"]
    assert components.baseline(ADMIN, "feedback_form") == ref
    with pytest.raises(RuntimeFault) as none:
        components.baseline(ADMIN, "driver_options")  # v1 is null: nothing to register
    assert none.value.code == "COMPONENT_KIND"
    with pytest.raises(RuntimeFault) as proposer:
        components.baseline(PROPOSER, "limits")
    assert proposer.value.code == "FORBIDDEN"


def test_a_decider_names_components_of_its_rows_kinds(components: ComponentService) -> None:
    method = register(
        components, component_id="decision_method.v1", kind="decision_method", content=METHOD
    )
    ref = register(
        components, component_id="decider.l4", kind="decider", content={**DECIDER, "method": method}
    )
    assert components.get(ref)["layer"] == "L4"  # the decider's own layer
    wrong: list[Callable[[], Any]] = [
        lambda: register(
            components,
            component_id="decider.bad",
            kind="decider",
            content={**DECIDER, "method": ref},
        ),  # a decider, not a method
        lambda: register(
            components,
            component_id="decider.bad",
            kind="decider",
            content={**DECIDER, "method": REF},
        ),  # names nothing
        lambda: register(
            components,
            component_id="decider.bad",
            kind="decider",
            content={**DECIDER, "method": method, "layer": "L8", "options": [method]},
        ),  # L8 options are limits versions
    ]
    for attempt in wrong:
        with pytest.raises(RuntimeFault) as bad:
            attempt()
        assert bad.value.code == "COMPONENT_CONTENT"
