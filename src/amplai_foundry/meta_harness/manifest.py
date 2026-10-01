"""Manifests: the component versions a composition uses (Work 033 S3, D-096, interfaces.md §2.3).

No 3.0.0 field is added. A manifest lives in the records the composition's existing refs point to:
``prompt_bundle_ref`` (the role prompt itself), ``context_policy_ref`` → ``context-policy``,
``budget_policy_ref`` → ``budget-policy`` and ``router_policy_ref`` → ``router-policy`` (layered
shape). Context and budget carriers are content-addressed (``ctx-``/``bud-<digest24>``). The
installed baseline router keeps the id ``<app>-router`` and gets a layered revision; candidate
routers are content-addressed (``router-<digest24>``), because ``put_record`` appends a revision
whenever a value differs (``runtime/execution/codex.py:250-255``) and candidates written under
``<app>-router`` would make every boot re-append the baseline. ``harness-manifest`` records are an
index only; the composition refs are authoritative.

A composition installed before carriers existed points its context and budget refs at the shared
``policy`` record and reads as the v1 manifest (the baseline component versions, when this store
has them).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from ..runtime.contracts.authority import Actor
from ..runtime.contracts.identity import digest
from ..runtime.contracts.semantics import resolve_ref
from ..runtime.errors import Hold, RuntimeFault
from ..runtime.execution import policies, prompts, releases
from ..runtime.execution.codex import put_record
from ..runtime.execution.releases import CANDIDATE_SEP, ENV_SEP
from ..runtime.storage.store import Scope, Store
from .components import KINDS, ComponentService
from .composition import CompositionService

Ref = dict[str, Any]
CONTEXT = (*policies.CONTEXT_SLOTS, *policies.CONTEXT_DECIDERS)
BUDGET = (*policies.BUDGET_SLOTS, *policies.BUDGET_DECIDERS)
ROUTER = (*policies.ROUTER_SLOTS, *policies.ROUTER_DECIDERS)
DECIDERS = frozenset(policies.DECIDER_LAYERS)
SLOT_GROUP = {
    **{s: "context" for s in CONTEXT},
    **{s: "budget" for s in BUDGET},
    **{s: "router" for s in ROUTER},
}
# composition field -> (Manifest group, slots, carrier kind)
GROUPS: dict[str, tuple[str, tuple[str, ...], str]] = {
    "context_policy_ref": ("context", CONTEXT, "context-policy"),
    "budget_policy_ref": ("budget", BUDGET, "budget-policy"),
    "router_policy_ref": ("router", ROUTER, "router-policy"),
}
BASELINE_KINDS = (
    "env_bootstrap", "memory_notes", "retrieval", "feedback_form", "attempt_policy",
    "execution_strategy", "fast_checks", "limits", "interpretation",
)  # fmt: skip
ROUTER_BASELINE_SOURCE = (
    "baseline manifest (interfaces.md §2.3); order: operator decisions 2026-09-28 (D-079), "
    "2026-09-29 (Work 031 D4)"
)
ROUTER_CANDIDATE_SOURCE = "manifest candidate (interfaces.md §2.3)"


def slot_kind(slot: str) -> str:
    """The component kind a manifest slot holds."""
    if slot in DECIDERS:
        return "decider"
    return "role_prompt" if slot == "prompt_bundle_ref" else slot


def _slot_fault(why: str, details: object = None) -> RuntimeFault:
    return RuntimeFault("MANIFEST_SLOT", why, details=details)


@dataclass(frozen=True)
class Manifest:
    prompt_bundle_ref: Ref
    context: dict[str, Ref | None]  # env_bootstrap, memory_notes, retrieval, feedback_form, L4
    budget: dict[str, Ref | None]  # attempt_policy, execution_strategy, driver_options,
    # fast_checks, limits, L5..L8
    router: dict[str, Ref | None]  # route_policy, interpretation, L1..L3

    def flat(self) -> dict[str, Ref | None]:
        """``"<kind>[.<layer>]"`` -> ref, sorted (``harness-manifest.components``, §2.3)."""
        out: dict[str, Ref | None] = {"role_prompt": self.prompt_bundle_ref}
        for group in (self.context, self.budget, self.router):
            for slot, ref in group.items():
                out["decider." + slot if slot in DECIDERS else slot] = ref
        return dict(sorted(out.items()))


@dataclass(frozen=True)
class ComponentChange:
    slot: str
    kind: str
    before: Ref | None
    after: Ref | None
    surface_class: Literal["A", "B"]


def _change(slot: str, before: Ref | None, after: Ref | None) -> ComponentChange:
    kind = slot_kind(slot)
    return ComponentChange(slot, kind, before, after, KINDS[kind].surface_class)


# -- IC-12 environment siblings (Work 033 S7b, interfaces.md §3.2, §10.5 step 6) ---------------
CARRIER_FIELDS = (
    "prompt_bundle_ref", "context_policy_ref", "budget_policy_ref", "router_policy_ref",
)  # fmt: skip
# what a sibling keeps from its source composition (§3.2: same carriers, pack_refs,
# verification_policy_ref) and what it takes from the task environment's installed composition
SIBLING_KEEPS = (*CARRIER_FIELDS, "pack_refs", "verification_policy_ref")
ENVIRONMENT_FIELDS = (*releases.CLASS_A_FIXED, "qualification_ref")


def _unqualified(why: str, details: object = None) -> Hold:
    return Hold("ENVIRONMENT_UNQUALIFIED", why, details=details)


def put_composition(
    store: Store, scope: Scope, contracts: Any, name: str, value: dict[str, Any]
) -> Ref:
    """Write a composition whose ``revision`` field is the store revision it is written at; the
    latest one when nothing else changed (the rule of ``LocalExecutionService._put_composition``).

    The profile checks of ``CompositionService.register`` apply first (Hold COMPOSITION_DRIVER,
    MODEL_VERSION_POLICY, COMPOSITION_UNQUALIFIED); the schema is validated by ``put_record``."""
    model = store.get(scope, "model-profile", value["model_profile_ref"])
    driver = store.get(scope, "driver-capabilities", value["driver_profile_ref"])
    if model["driver_profile_ref"] != value["driver_profile_ref"]:
        raise Hold("COMPOSITION_DRIVER", "Model is qualified for a different driver")
    if model["model_version_policy"] != "pinned":
        raise Hold(
            "MODEL_VERSION_POLICY", "Autonomous composition requires a pinned model snapshot"
        )
    if not model["enabled"] or driver["maturity"] != "qualified":
        raise Hold(
            "COMPOSITION_UNQUALIFIED", "Composition contains a disabled model or unqualified driver"
        )
    kind = "harness-composition"
    last = releases.latest(store, scope, kind, name)
    if last and digest({**value, "revision": last["revision"]}) == last["digest"]:
        return last
    revision = last["revision"] + 1 if last else 1
    return put_record(store, scope, contracts, kind, name, {**value, "revision": revision})


def env_sibling(
    store: Store,
    scope: Scope,
    contracts: Any,
    composition_ref: Ref,
    environment_id: str,
    *,
    installed: dict[str, Ref] | None = None,
) -> Ref:
    """IC-12: the composition ``composition_ref`` in task environment ``environment_id``.

    The sibling keeps the source's carriers, ``pack_refs`` and ``verification_policy_ref`` and
    takes the sandbox, driver, model and qualification refs of the installed environment
    composition ``<installed id>:env-<env12>`` (written by ``LocalExecutionService.install`` from
    the environment's qualified records); its model must have the source's ``provider_model_id``
    and ``reasoning_profile``. Its id is that environment composition's id plus the source's
    candidate suffix (``__<suffix>``); a source whose kept fields equal the environment
    composition's gives that composition itself.

    ``installed`` (cell id -> installed ref) resolves the source's installed composition through
    ``releases.pin_allowed``; without it the installed id is the source id before its first
    ``__``, and the source must keep that composition's driver, model and sandbox profiles.
    Hold CELL_UNKNOWN when the source is no installed composition or class-A candidate of one,
    ENVIRONMENT_UNQUALIFIED when the environment has no installed composition for that cell, when
    the models differ, or when the source is already a sibling of another environment."""
    source = store.get(scope, "harness-composition", composition_ref)
    name = str(source["composition_id"])
    if ENV_SEP in name:
        tag = name.split(ENV_SEP, 1)[1].split(CANDIDATE_SEP, 1)[0]
        if tag == releases.env12(environment_id):
            return composition_ref
        raise _unqualified(
            "The composition is already the sibling of another task environment",
            {"composition_id": name, "environment_id": environment_id},
        )
    if installed is not None:
        cell = releases.pin_allowed(store, scope, installed, composition_ref)
        if cell is None:
            raise Hold("CELL_UNKNOWN", "The composition is not one of this app's cells")
        base = store.get(scope, "harness-composition", installed[cell])
    else:
        base_ref = releases.latest(
            store, scope, "harness-composition", name.split(CANDIDATE_SEP, 1)[0]
        )
        base = store.get(scope, "harness-composition", base_ref) if base_ref else {}
        if not base or any(base.get(k) != source[k] for k in releases.CLASS_A_FIXED):
            raise Hold("CELL_UNKNOWN", "The composition is no installed one or its candidate")
    env_id = releases.env_composition_id(base["composition_id"], environment_id)
    env_ref = releases.latest(store, scope, "harness-composition", env_id)
    if env_ref is None:
        raise _unqualified(
            "The task environment has no qualified composition of this cell",
            {"environment_id": environment_id, "installed": base["composition_id"]},
        )
    env = store.get(scope, "harness-composition", env_ref)
    model = store.get(scope, "model-profile", source["model_profile_ref"])
    env_model = store.get(scope, "model-profile", env["model_profile_ref"])
    for key in ("provider_model_id", "reasoning_profile"):
        if model.get(key) != env_model.get(key):
            raise _unqualified(
                "The task environment qualified another model or effort for this cell",
                {"field": key, "composition": model.get(key), "environment": env_model.get(key)},
            )
    if all(source[k] == env[k] for k in SIBLING_KEEPS):
        return env_ref
    suffix = name[len(base["composition_id"]) :]
    if not suffix:  # the installed composition, whose kept fields differ from the env one's
        suffix = CANDIDATE_SEP + "sibling-" + digest({k: source[k] for k in SIBLING_KEEPS})[7:19]
    sibling = env["composition_id"] + suffix
    value = {
        **source,
        **{k: env[k] for k in ENVIRONMENT_FIELDS},
        "composition_id": sibling,
    }
    return put_composition(store, scope, contracts, sibling, value)


def environment_of(store: Store, scope: Scope, composition_ref: Ref) -> str | None:
    """The env12 tag of an environment sibling (None for an app-environment composition)."""
    name = str(store.get(scope, "harness-composition", composition_ref)["composition_id"])
    if ENV_SEP not in name:
        return None
    return name.split(ENV_SEP, 1)[1].split(CANDIDATE_SEP, 1)[0]


class ManifestService:
    def __init__(
        self, store: Store, scope: Scope, contracts: Any, components: ComponentService
    ) -> None:
        self.store, self.scope = store, scope
        self.contracts, self.components = contracts, components

    # -- reading ------------------------------------------------------------------------------
    def of_composition(self, composition_ref: Ref) -> Manifest:
        """The manifest a composition's refs carry; a legacy ``policy`` carrier reads as v1."""
        composition = self.store.get(self.scope, "harness-composition", composition_ref)
        groups = {GROUPS[f][0]: self._group(f, composition[f]) for f in GROUPS}
        return Manifest(composition["prompt_bundle_ref"], **groups)

    def _group(self, field: str, ref: Ref) -> dict[str, Ref | None]:
        """Slot -> ref of one carrier. Hold CARRIER_KIND for an unexpected kind or shape."""
        _name, slots, carrier = GROUPS[field]
        found, value = resolve_ref(self.store, self.scope, ref)
        if field == "router_policy_ref" and found == carrier:
            if value.get("kind") == "task_class_baseline":  # product.py:357-368 before S3
                return self._v1(slots, order=value.get("order"))
            if value.get("kind") == "layered_v1":
                return self._slots(value, slots, carrier)
        elif found == policies.LEGACY_POLICY and field != "router_policy_ref":
            return self._v1(slots)
        elif found == carrier and value.get("schema") == f"amplai.{carrier}.v1":
            return self._slots(value, slots, carrier)
        raise Hold("CARRIER_KIND", f"{carrier}: unexpected carrier", details={"found": found})

    @staticmethod
    def _slots(
        value: dict[str, Any], slots: tuple[str, ...], carrier: str
    ) -> dict[str, Ref | None]:
        components, deciders = value.get("components"), value.get("deciders")
        if (
            not isinstance(components, dict)
            or not isinstance(deciders, dict)
            or set(components) | set(deciders) != set(slots)
        ):
            raise Hold("CARRIER_KIND", f"{carrier}: the carrier does not have the slots of §2.3")
        return {slot: {**components, **deciders}[slot] for slot in slots}

    def _v1(self, slots: tuple[str, ...], *, order: Any = None) -> dict[str, Ref | None]:
        """The baseline versions whose content is v1 (None where this store has none)."""
        out: dict[str, Ref | None] = {}
        for slot in slots:
            content = policies.V1.get(slot)
            if slot == "route_policy" and order is not None:
                content = {"order": order, "roles": {}}
            out[slot] = (
                self.components.find(slot + ".baseline", content)
                if content is not None and slot not in DECIDERS
                else None
            )
        return out

    # -- building -----------------------------------------------------------------------------
    def baseline(
        self, actor: Actor, app_id: str, *, prompt_bundle_ref: Ref, route_order: list[str]
    ) -> Manifest:
        """The v1 manifest: every baseline component, no driver options, no deciders.

        Every app shares the baselines today: ``app_id`` does not change them (it names the
        installed router in ``write``); the route order is the installed task-class order. An
        order other than the latest ``route_policy.baseline`` becomes its next version (S4 cell
        orders per app need their own ids, 확인 필요).
        """
        refs = {kind: self.components.baseline(actor, kind) for kind in BASELINE_KINDS}
        refs["route_policy"] = self.components.register(
            actor,
            component_id="route_policy.baseline",
            kind="route_policy",
            content={"order": {"*": list(route_order)}, "roles": {}},
            source="baseline",
            rationale="v1: the installed task-class order (product.py ROUTER_ORDER, D-079)",
        )
        return Manifest(
            prompt_bundle_ref,
            context={s: refs.get(s) for s in CONTEXT},
            budget={s: refs.get(s) for s in BUDGET},
            router={s: refs.get(s) for s in ROUTER},
        )

    def change(self, base: Manifest, **slots: Ref | None) -> Manifest:
        """``base`` with the named slots replaced. RuntimeFault MANIFEST_SLOT for an unknown
        slot, a ref of another kind (or layer), or an empty required slot."""
        groups = {"context": dict(base.context), "budget": dict(base.budget),
                  "router": dict(base.router)}  # fmt: skip
        prompt = base.prompt_bundle_ref
        for slot, ref in slots.items():
            if slot == "prompt_bundle_ref":
                if ref is None:
                    raise _slot_fault("The prompt bundle slot is required")
                self._prompt(ref)
                prompt = ref
                continue
            group = SLOT_GROUP.get(slot)
            if group is None:
                raise _slot_fault("Unknown manifest slot", slot)
            self._content(slot, ref)
            groups[group][slot] = ref
        return Manifest(prompt, groups["context"], groups["budget"], groups["router"])

    def diff(self, a: Manifest, b: Manifest) -> list[ComponentChange]:
        before = {"prompt_bundle_ref": a.prompt_bundle_ref, **a.context, **a.budget, **a.router}
        after = {"prompt_bundle_ref": b.prompt_bundle_ref, **b.context, **b.budget, **b.router}
        return [
            _change(slot, before.get(slot), after.get(slot))
            for slot in ("prompt_bundle_ref", *CONTEXT, *BUDGET, *ROUTER)
            if before.get(slot) != after.get(slot)
        ]

    def surface_class(self, changes: list[ComponentChange]) -> Literal["A", "B"]:
        """The highest class among the changed components (D-096); no change is class A."""
        return "B" if any(c.surface_class == "B" for c in changes) else "A"

    def carrier_changes(self, field: str, before: Ref, after: Ref) -> list[ComponentChange]:
        """The component changes behind one changed composition carrier field (classify)."""
        if before == after:
            return []
        if field == "prompt_bundle_ref":
            return [_change("prompt_bundle_ref", before, after)]
        a, b = self._group(field, before), self._group(field, after)
        return [_change(s, a[s], b[s]) for s in GROUPS[field][1] if a[s] != b[s]]

    # -- writing ------------------------------------------------------------------------------
    def write(self, actor: Actor, manifest: Manifest, *, app_id: str) -> dict[str, Ref]:
        """The carrier records of ``manifest`` (content-addressed, idempotent) and its index."""
        self._authorize(actor)
        self._check(manifest)
        carriers = {
            "context_policy_ref": self._put_group("context_policy_ref", manifest.context),
            "budget_policy_ref": self._put_group("budget_policy_ref", manifest.budget),
            "router_policy_ref": self._put_router(manifest, app_id=app_id),
        }
        return {**carriers, "manifest_ref": self._put_manifest(manifest, carriers)}

    def materialize(
        self, actor: Actor, *, base_composition_ref: Ref, manifest: Manifest, suffix: str | None
    ) -> Ref:
        """A composition = the base with this manifest's carriers, id ``<base id>__<suffix>``
        (the base id when ``suffix`` is None), registered through ``CompositionService.register``
        (its Holds: COMPOSITION_DRIVER, MODEL_VERSION_POLICY, COMPOSITION_UNQUALIFIED). A carrier
        group the manifest leaves as the base has keeps the base's ref."""
        actor.require("harness.propose")
        self._authorize(actor)
        self._check(manifest)
        base = self.store.get(self.scope, "harness-composition", base_composition_ref)
        current = self.of_composition(base_composition_ref)
        refs = {
            "context_policy_ref": base["context_policy_ref"]
            if manifest.context == current.context
            else self._put_group("context_policy_ref", manifest.context),
            "budget_policy_ref": base["budget_policy_ref"]
            if manifest.budget == current.budget
            else self._put_group("budget_policy_ref", manifest.budget),
            "router_policy_ref": base["router_policy_ref"]
            if manifest.router == current.router
            else self._put_router(manifest, app_id=None),
        }
        self._put_manifest(manifest, refs)
        value = {
            **base,
            **refs,
            "prompt_bundle_ref": manifest.prompt_bundle_ref,
            "composition_id": base["composition_id"] + (CANDIDATE_SEP + suffix if suffix else ""),
        }
        return CompositionService(self.store, self.contracts).register(actor, value)

    def env_sibling(self, actor: Actor, composition_ref: Ref, environment_id: str) -> Ref:
        """IC-12 (§3.2): the environment sibling of ``composition_ref`` in task environment
        ``environment_id`` (module ``env_sibling``; Hold CELL_UNKNOWN | ENVIRONMENT_UNQUALIFIED).
        The actor needs ``harness.propose`` or ``runtime.admin`` as for ``write``. The trial
        path builds siblings through ``LocalExecutionService.env_sibling`` (the service actor,
        the app's installed compositions), which applies the same rule."""
        self._authorize(actor)
        return env_sibling(self.store, self.scope, self.contracts, composition_ref, environment_id)

    def _authorize(self, actor: Actor) -> None:
        if actor.scope != self.scope:
            raise RuntimeFault("FORBIDDEN", "Actor acts in another scope")
        if not {"harness.propose", "runtime.admin"} & actor.permissions:
            actor.require("harness.propose")  # FORBIDDEN

    def _prompt(self, ref: Ref) -> None:
        try:
            found, value = resolve_ref(self.store, self.scope, ref)
        except (RuntimeFault, KeyError, TypeError) as exc:
            raise _slot_fault("The prompt slot names no record") from exc
        if found != prompts.KIND:
            raise _slot_fault("The prompt slot names another kind", found)
        prompts.validate(value)

    def _content(self, slot: str, ref: Ref | None) -> dict[str, Any] | None:
        if ref is None:
            if slot in policies.REQUIRED_SLOTS:
                raise _slot_fault("A required slot is empty", slot)
            return None
        try:
            return policies.component_content(self.store, self.scope, ref, slot_kind(slot), slot)
        except (RuntimeFault, KeyError, TypeError) as exc:
            code = getattr(exc, "code", type(exc).__name__)
            raise _slot_fault("The slot names no component of its kind", {slot: code}) from exc

    def _check(self, manifest: Manifest) -> None:
        """Slots and kinds (MANIFEST_SLOT), then the combination rules (MANIFEST_COMBINATION)."""
        self._prompt(manifest.prompt_bundle_ref)
        contents: dict[str, dict[str, Any] | None] = {}
        for group, slots in ((manifest.context, CONTEXT), (manifest.budget, BUDGET),
                             (manifest.router, ROUTER)):  # fmt: skip
            if set(group) != set(slots):
                raise _slot_fault("A manifest group has other slots", sorted(group))
            contents.update({slot: self._content(slot, group[slot]) for slot in slots})
        feedback, attempt, limits = (
            contents["feedback_form"], contents["attempt_policy"], contents["limits"]
        )  # fmt: skip
        assert feedback is not None and attempt is not None and limits is not None  # required
        policies.check_combination(feedback, attempt, limits)

    def _put(self, kind: str, object_id: str, value: dict[str, Any]) -> Ref:
        return put_record(self.store, self.scope, self.contracts, kind, object_id, value)

    def _put_group(self, field: str, group: dict[str, Ref | None]) -> Ref:
        _name, _slots, carrier = GROUPS[field]
        deciders = {s for s in group if s in DECIDERS}
        value = {
            "schema": f"amplai.{carrier}.v1",
            "scope": self.scope.wire(),
            "components": {s: r for s, r in group.items() if s not in deciders},
            "deciders": {s: r for s, r in group.items() if s in deciders},
        }
        prefix = "ctx" if carrier == "context-policy" else "bud"
        return self._put(carrier, f"{prefix}-{digest(value)[7:31]}", value)

    def _put_router(self, manifest: Manifest, *, app_id: str | None) -> Ref:
        """``<app>-router`` for the installed baseline router, else ``router-<digest24>``."""
        components = {s: manifest.router[s] for s in policies.ROUTER_SLOTS}
        deciders = {s: manifest.router[s] for s in policies.ROUTER_DECIDERS}
        baseline = (
            app_id is not None
            and not any(deciders.values())
            and all(
                ref is not None and self.components.get(ref)["source"] == "baseline"
                for ref in components.values()
            )
        )
        value = {
            "scope": self.scope.wire(),
            "kind": "layered_v1",
            "components": components,
            "deciders": deciders,
            "source": ROUTER_BASELINE_SOURCE if baseline else ROUTER_CANDIDATE_SOURCE,
        }
        policy_id = f"{app_id}-router" if baseline else f"router-{digest(value)[7:31]}"
        return self._put("router-policy", policy_id, {"policy_id": policy_id, **value})

    def _put_manifest(self, manifest: Manifest, carriers: dict[str, Ref]) -> Ref:
        four = {"prompt_bundle_ref": manifest.prompt_bundle_ref, **carriers}
        manifest_digest = digest(four)
        value = {
            "schema": "amplai.harness-manifest.v1",
            "scope": self.scope.wire(),
            **four,
            "components": manifest.flat(),
            "manifest_digest": manifest_digest,
        }
        return self._put("harness-manifest", f"manifest-{manifest_digest[7:31]}", value)
