"""Readers and validators of the manifest carriers (Work 033 S3, D-096, interfaces.md §2-3).

A composition's existing 3.0.0 refs carry its manifest: ``context_policy_ref`` → a
``context-policy`` record, ``budget_policy_ref`` → ``budget-policy``, ``router_policy_ref`` →
``router-policy`` (layered shape). Each carrier names ``harness-component`` versions whose content
is validated here. A composition whose context or budget ref still points at the shared ``policy``
record (installed before Work 033, ``product.py`` install) reads as the v1 manifest, and a legacy
``task_class_baseline`` router reads as route_policy v1 with no deciders.

Runtime-owned (interfaces.md §1.3): the execution path reads manifests without importing
``meta_harness/``. Every v1 content reproduces today's behaviour (§2.2, last column).
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from ..contracts.identity import ID
from ..contracts.semantics import resolve_ref
from ..errors import Hold, RuntimeFault
from ..storage.store import Scope, Store
from . import prompts
from .planner_codex import TASK_CLASSES

Ref = dict[str, Any]
COMPONENT = "harness-component"
LEGACY_POLICY = "policy"  # the shared record install pointed every carrier at (product.py:394-396)

# plan.md §8 numbering; "simpler" = lower rank (interfaces.md §3.1)
STRATEGIES: tuple[str, ...] = (
    "single", "repair_loop", "workgraph_split", "plan_execute", "best_of_n",
    "generator_reviewer", "cascade", "orchestrator", "parallel_readonly", "vote",
)  # fmt: skip
STRATEGY_RANK: dict[str, int] = {s: i for i, s in enumerate(STRATEGIES, start=1)}
BOOTSTRAP_FACTS = ("repo_tree", "verifier_commands", "tool_versions", "fast_test_command")
ROLES = ("planner", "executor", "reviewer", "proposer")
CLAUDE_TOOLS = ("Read", "Edit", "Write", "Glob", "Grep", "Bash")
# §2.2: starts empty; a key is added only after an argv-capture test and a probe turn (§14 Q1).
# S4: Codex 0.155.1 documents `-c key=value` on exec and exec resume (specs/033-harness-taxonomy/
# runs/cli-effort-facts.md), but no key has had a probe turn, so the list stays empty; effort is
# the cell's (model_reasoning_effort, §2.4) and never a driver option.
CODEX_CONFIG_ALLOWLIST: frozenset[str] = frozenset()
# The Claude driver options (driver_options.claude) a component may set non-null. §14 Q13: each
# needs an argv capture plus one probe turn with the OAuth isolation flags; until then
# driver_options.claude stays empty in every allowed version. `claude --help` 2.1.278 lists
# --allowedTools and --append-system-prompt but not --max-turns (cli-effort-facts.md); no probe
# turn has run, so none is admitted.
CLAUDE_OPTION_ALLOWLIST: frozenset[str] = frozenset()
DECIDER_LAYERS = ("L1", "L2", "L3", "L4", "L5", "L6", "L7", "L8")
SOURCES = ("baseline", "operator", "proposer", "dreaming", "sweep")
PINNED_IMAGE = re.compile(r"[^@\s]+@sha256:[0-9a-f]{64}")

# carrier slots (§2.3); the required ones are "ref", the others "ref|null"
CONTEXT_SLOTS = ("env_bootstrap", "memory_notes", "retrieval", "feedback_form")
BUDGET_SLOTS = ("attempt_policy", "execution_strategy", "driver_options", "fast_checks", "limits")
ROUTER_SLOTS = ("route_policy", "interpretation")
CONTEXT_DECIDERS = ("L4",)
BUDGET_DECIDERS = ("L5", "L6", "L7", "L8")
ROUTER_DECIDERS = ("L1", "L2", "L3")
REQUIRED_SLOTS = frozenset(
    {"feedback_form", "attempt_policy", "execution_strategy", "limits", "route_policy",
     "interpretation"}
)  # fmt: skip
LIMIT_FIELDS = ("max_wall_seconds", "max_tokens", "max_attempts")

# v1 content per kind (§2.2, last column). Fields a disabled v1 does not read (env_bootstrap,
# retrieval, fast_checks beyond "enabled") are inert; S3 chose them so switching "enabled" on
# gives a bounded section. limits = the deployment Budget defaults (product.py:110-126) and
# route_policy = ROUTER_ORDER (product.py:52); tests pin both equalities.
V1: dict[str, dict[str, Any]] = {
    "role_prompt": {"implementer": list(prompts.IMPLEMENTER_BASELINE)},
    "interpretation": {"planner_instruction": "v1", "contract_form": "v1"},
    "env_bootstrap": {
        "enabled": False, "facts": list(BOOTSTRAP_FACTS), "tree_depth": 2,
        "tree_max_entries": 200, "max_chars": 4000,
    },
    "memory_notes": {"enabled": False, "notes": []},
    "retrieval": {
        "enabled": False, "method": "path_keyword_v1", "sources": ["files"], "max_items": 10,
        "max_chars": 2000,
    },
    # loop.py:587-599 before components: 3,000-char tail, three design-check keys, 20 items
    "feedback_form": {
        "mode": "tail", "tail_chars": 3000,
        "detail_keys": ["outside", "missing_sections", "unresolved_sources"],
        "detail_limit": 20, "header": "v1",
    },
    # product.py:113 (3 attempts), loop.py:468-469 (repair on the previous patch, feedback)
    "attempt_policy": {"max_attempts": 3, "repair_base": "previous_patch", "feedback": True},
    "execution_strategy": {"enabled": ["repair_loop"], "params": {}},
    "fast_checks": {"enabled": False, "checks": [], "max_followups": 0},
    "limits": {
        "max_wall_seconds": 1800, "max_tokens": 60_000_000, "max_attempts": 3,
        "aux_max_tokens": 0,
    },
    "route_policy": {"order": {"*": ["codex-cli", "claude-cli", "opencode-server"]}, "roles": {}},
    "judge_model": {"judge": "none"},
}  # fmt: skip


# -- content validation (§2.2) --------------------------------------------------------------------
def _bad(kind: str, why: str, details: object = None) -> RuntimeFault:
    return RuntimeFault("COMPONENT_CONTENT", f"{kind}: {why}", details=details)


def _fields(
    kind: str, value: Any, required: tuple[str, ...], optional: tuple[str, ...] = ()
) -> None:
    if not isinstance(value, dict):
        raise _bad(kind, "content is an object")
    missing, unknown = set(required) - set(value), set(value) - set(required) - set(optional)
    if missing or unknown:
        raise _bad(kind, "fields", {"missing": sorted(missing), "unknown": sorted(unknown)})


def _int(kind: str, name: str, value: Any, low: int, high: int) -> None:
    if type(value) is not int or not low <= value <= high:
        raise _bad(kind, f"{name} is an integer in {low}..{high}")


def _number(kind: str, name: str, value: Any, low: float, high: float) -> None:
    if type(value) not in (int, float) or not low <= value <= high:
        raise _bad(kind, f"{name} is a number in {low}..{high}")


def _bool(kind: str, name: str, value: Any) -> None:
    if type(value) is not bool:
        raise _bad(kind, f"{name} is a boolean")


def _choice(kind: str, name: str, value: Any, options: tuple[str, ...]) -> None:
    if value not in options:
        raise _bad(kind, f"{name} is one of {', '.join(options)}")


def _list(kind: str, name: str, value: Any, low: int, high: int) -> list[Any]:
    if not isinstance(value, list) or not low <= len(value) <= high:
        raise _bad(kind, f"{name} is a list of {low}..{high} items")
    return value


def _subset(kind: str, name: str, value: Any, options: tuple[str, ...]) -> None:
    items = _list(kind, name, value, 0, len(options))
    if any(i not in options for i in items) or len(set(items)) != len(items):
        raise _bad(kind, f"{name} is a subset of {', '.join(options)}")


def _ids(kind: str, name: str, value: Any, low: int, high: int) -> None:
    items = _list(kind, name, value, low, high)
    well_formed = all(isinstance(i, str) and ID.fullmatch(i) for i in items)
    if not well_formed or len(set(items)) != len(items):
        raise _bad(kind, f"{name} lists distinct identifiers")


def _text(kind: str, name: str, value: Any, high: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > high:
        raise _bad(kind, f"{name} is nonempty text of at most {high} characters")


def _ref(kind: str, name: str, value: Any, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if (
        not isinstance(value, dict)
        or set(value) != {"id", "revision", "digest"}
        or not isinstance(value["id"], str)
        or type(value["revision"]) is not int
        or not isinstance(value["digest"], str)
    ):
        raise _bad(kind, f"{name} is a ref" + (" or null" if nullable else ""))


def _role_prompt(c: dict[str, Any]) -> None:
    _fields("role_prompt", c, ("implementer",))
    try:  # the same rules as a prompt bundle's lines (prompts.py:45-63)
        prompts.validate(
            {"bundle_id": "x", "surface": "implementer_role", "implementer": c["implementer"],
             "source": "x"}
        )  # fmt: skip
    except RuntimeFault as exc:
        raise _bad("role_prompt", exc.message) from None


def _interpretation(c: dict[str, Any]) -> None:
    _fields("interpretation", c, ("planner_instruction", "contract_form"))
    _choice("interpretation", "planner_instruction", c["planner_instruction"],
            ("v1", "ask_first", "assume_and_state"))  # fmt: skip
    _choice("interpretation", "contract_form", c["contract_form"], ("v1", "steps"))


def _env_bootstrap(c: dict[str, Any]) -> None:
    k = "env_bootstrap"
    _fields(k, c, ("enabled", "facts", "tree_depth", "tree_max_entries", "max_chars"))
    _bool(k, "enabled", c["enabled"])
    _subset(k, "facts", c["facts"], BOOTSTRAP_FACTS)
    _int(k, "tree_depth", c["tree_depth"], 1, 3)
    _int(k, "tree_max_entries", c["tree_max_entries"], 1, 400)
    _int(k, "max_chars", c["max_chars"], 1, 6000)


def _dated(value: Any) -> bool:
    try:
        return isinstance(value, str) and date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _memory_notes(c: dict[str, Any]) -> None:
    k = "memory_notes"
    _fields(k, c, ("enabled", "notes"))
    _bool(k, "enabled", c["enabled"])
    notes = c["notes"]
    if not isinstance(notes, list):
        raise _bad(k, "notes is a list")
    per: dict[tuple[str, str | None], int] = {}
    for note in notes:
        _fields(k, note, ("text", "app", "task_class", "evidence", "dated"))
        _text(k, "text", note["text"], 300)
        if not isinstance(note["app"], str) or not ID.fullmatch(note["app"]):
            raise _bad(k, "app is an app id")
        if note["task_class"] is not None and note["task_class"] not in TASK_CLASSES:
            raise _bad(k, "task_class is a task class or null")
        _ids(k, "evidence", note["evidence"], 0, 8)
        if not _dated(note["dated"]):
            raise _bad(k, "dated is YYYY-MM-DD")
        key = (note["app"], note["task_class"])
        per[key] = per.get(key, 0) + 1
    if any(n > 40 for n in per.values()):
        raise _bad(k, "at most 40 notes per (app, task_class)")


def _retrieval(c: dict[str, Any]) -> None:
    k = "retrieval"
    _fields(k, c, ("enabled", "method", "sources", "max_items", "max_chars"))
    _bool(k, "enabled", c["enabled"])
    _choice(k, "method", c["method"], ("path_keyword_v1",))
    _subset(k, "sources", c["sources"], ("files", "decisions"))
    _int(k, "max_items", c["max_items"], 1, 20)
    _int(k, "max_chars", c["max_chars"], 1, 4000)


def _feedback_form(c: dict[str, Any]) -> None:
    k = "feedback_form"
    _fields(k, c, ("mode", "tail_chars", "detail_keys", "detail_limit", "header"))
    _choice(k, "mode", c["mode"], ("tail", "failing_only_tail", "summary"))
    _int(k, "tail_chars", c["tail_chars"], 0, 20000)
    keys = _list(k, "detail_keys", c["detail_keys"], 0, 8)
    if any(not isinstance(x, str) or not x for x in keys) or len(set(keys)) != len(keys):
        raise _bad(k, "detail_keys are distinct nonempty strings")
    _int(k, "detail_limit", c["detail_limit"], 1, 50)
    _choice(k, "header", c["header"], ("v1", "v1_fresh"))


def _attempt_policy(c: dict[str, Any]) -> None:
    k = "attempt_policy"
    _fields(k, c, ("max_attempts", "repair_base", "feedback"))
    _int(k, "max_attempts", c["max_attempts"], 1, 3)  # IC-06
    _choice(k, "repair_base", c["repair_base"], ("previous_patch", "fresh_base"))
    _bool(k, "feedback", c["feedback"])


def _strategy_params(strategy: str, p: Any) -> None:
    """The per-strategy params of §5.2 (only the bounds the table states)."""
    k = "execution_strategy"
    shapes: dict[str, tuple[str, ...]] = {
        "workgraph_split": ("max_nodes",), "plan_execute": ("planner_role",),
        "best_of_n": ("n",), "generator_reviewer": ("reviewer_role", "max_rounds"),
        "cascade": ("cells", "max_escalations"), "orchestrator": ("max_parts", "lead_role"),
        "parallel_readonly": ("steps",), "vote": ("k",),
    }  # fmt: skip
    if strategy not in shapes:
        raise _bad(k, f"{strategy} takes no params")
    _fields(k, p, shapes[strategy])
    for name, value in p.items():
        if name in {"max_nodes", "max_parts"}:
            _int(k, name, value, 2, 4)
        elif name in {"n", "k"}:
            _int(k, name, value, 2, 3)  # IC-06
        elif name == "max_rounds":
            _int(k, name, value, 1, 2)
        elif name == "max_escalations":
            _int(k, name, value, 1, 1)
        elif name.endswith("_role"):
            _choice(k, name, value, ROLES)
        elif name == "cells":
            _ids(k, name, value, 2, 2)
        elif name == "steps":
            _list(k, name, value, 0, 4)  # step shape is S9's (§5.2 strategy 9)


def _execution_strategy(c: dict[str, Any]) -> None:
    k = "execution_strategy"
    _fields(k, c, ("enabled", "params"))
    enabled = _list(k, "enabled", c["enabled"], 1, len(STRATEGIES))
    if any(s not in STRATEGIES for s in enabled) or len(set(enabled)) != len(enabled):
        raise _bad(k, "enabled lists distinct strategy ids")
    if not isinstance(c["params"], dict) or not set(c["params"]) <= set(enabled):
        raise _bad(k, "params is an object keyed by enabled strategies")
    for strategy, params in c["params"].items():
        _strategy_params(strategy, params)


def _driver_options(c: dict[str, Any]) -> None:
    k = "driver_options"
    _fields(k, c, ("claude", "codex"))
    claude, codex = c["claude"], c["codex"]
    _fields(k, claude, ("max_turns", "append_system_prompt", "allowed_tools"))
    unmeasured = sorted(n for n, v in claude.items() if v is not None)
    if set(unmeasured) - CLAUDE_OPTION_ALLOWLIST:
        raise _bad(k, "claude options come from CLAUDE_OPTION_ALLOWLIST (§14 Q13)", unmeasured)
    if claude["max_turns"] is not None:
        _int(k, "max_turns", claude["max_turns"], 1, 500)
    if claude["append_system_prompt"] is not None:
        _text(k, "append_system_prompt", claude["append_system_prompt"], 2000)
    if claude["allowed_tools"] is not None:
        _subset(k, "allowed_tools", claude["allowed_tools"], CLAUDE_TOOLS)
    _fields(k, codex, ("config",))
    pairs = _list(k, "config", codex["config"], 0, 8)
    for pair in pairs:
        if not isinstance(pair, list) or len(pair) != 2 or pair[0] not in CODEX_CONFIG_ALLOWLIST:
            raise _bad(k, "codex config keys come from CODEX_CONFIG_ALLOWLIST (§14 Q1)")


def _fast_checks(c: dict[str, Any]) -> None:
    k = "fast_checks"
    _fields(k, c, ("enabled", "checks", "max_followups"))
    _bool(k, "enabled", c["enabled"])
    _ids(k, "checks", c["checks"], 0, 4)
    _int(k, "max_followups", c["max_followups"], 0, 2)


def _limits(c: dict[str, Any]) -> None:
    k = "limits"
    _fields(k, c, ("max_wall_seconds", "max_tokens", "max_attempts", "aux_max_tokens"))
    # the bounds of common.schema.json $defs.budget; the ceiling is checked by budget_policy
    _int(k, "max_wall_seconds", c["max_wall_seconds"], 1, 604800)
    _int(k, "max_tokens", c["max_tokens"], 1, 9007199254740991)
    _int(k, "max_attempts", c["max_attempts"], 1, 3)
    _int(k, "aux_max_tokens", c["aux_max_tokens"], 0, 9007199254740991)
    if c["aux_max_tokens"] >= c["max_tokens"]:
        raise _bad(k, "aux_max_tokens < max_tokens (IC-21)")


def _route_policy(c: dict[str, Any]) -> None:
    k = "route_policy"
    _fields(k, c, ("order", "roles"))
    order, roles = c["order"], c["roles"]
    # "*" is the fallback select_composition reads (product.py:460)
    if not isinstance(order, dict) or "*" not in order:
        raise _bad(k, 'order is an object with a "*" entry')
    for task_class, cells in order.items():
        if task_class != "*" and task_class not in TASK_CLASSES:
            raise _bad(k, "order keys are task classes or *")
        _ids(k, "order[" + task_class + "]", cells, 1, 64)
    if not isinstance(roles, dict) or any(r not in ROLES for r in roles):
        raise _bad(k, "roles are keyed by " + ", ".join(ROLES))
    for role, cells in roles.items():
        _ids(k, "roles[" + role + "]", cells, 1, 64)


def _decider(c: dict[str, Any]) -> None:
    k = "decider"
    _fields(k, c, ("layer", "method", "table", "judge", "options", "policy"))
    _choice(k, "layer", c["layer"], DECIDER_LAYERS)
    _ref(k, "method", c["method"])
    _ref(k, "table", c["table"], nullable=True)
    _ref(k, "judge", c["judge"], nullable=True)
    if c["layer"] in {"L5", "L8"}:  # options: driver_options (L5) / limits (L8) versions
        for ref in _list(k, "options", c["options"], 1, 8):
            _ref(k, "options[]", ref)
    elif c["options"] is not None:
        raise _bad(k, "options is null outside L5 and L8 (§6.1)")
    policy = c["policy"]
    _fields(k, policy, ("min_samples", "margin", "pooling_strength"))
    _int(k, "min_samples", policy["min_samples"], 1, 1000)
    _number(k, "margin", policy["margin"], 0, 0.5)
    _number(k, "pooling_strength", policy["pooling_strength"], 0, 100)


def _decision_method(c: dict[str, Any]) -> None:
    k = "decision_method"
    _fields(k, c, ("features", "estimator", "selection", "fallback", "utility_lambda"))
    features = _list(k, "features", c["features"], 1, 64)
    if any(not isinstance(f, str) or not f for f in features) or len(set(features)) != len(
        features
    ):
        raise _bad(k, "features are distinct feature ids")
    _choice(k, "estimator", c["estimator"], ("pooled_beta_binomial_v1", "judge_v1"))
    _choice(k, "selection", c["selection"],
            ("noninferior_then_cheapest_v1", "max_success_v1", "utility_v1"))  # fmt: skip
    _choice(k, "fallback", c["fallback"], ("prior_v1", "coarser_bucket_v1", "judge_v1"))
    if c["utility_lambda"] is not None:
        _number(k, "utility_lambda", c["utility_lambda"], float("-inf"), float("inf"))


def _judge_model(c: dict[str, Any]) -> None:
    k = "judge_model"
    # v1 is {"judge": "none"} (§2.2), so cell and question_types are optional fields
    _fields(k, c, ("judge",), ("cell", "question_types"))
    _choice(k, "judge", c["judge"], ("none", "llm_cell", "jev"))
    cell = c.get("cell")
    if cell is not None and (not isinstance(cell, str) or not ID.fullmatch(cell)):
        raise _bad(k, "cell is a cell id or null")
    if "question_types" in c:
        _subset(k, "question_types", c["question_types"], ("yes_no", "choice", "score"))


def _environment_image(c: dict[str, Any]) -> None:
    k = "environment_image"
    _fields(k, c, ("image",))
    if not isinstance(c["image"], str) or not PINNED_IMAGE.fullmatch(c["image"]):
        raise _bad(k, "image is pinned by digest (…@sha256:<64 hex>)")


_VALIDATORS: dict[str, Callable[[dict[str, Any]], None]] = {
    "role_prompt": _role_prompt,
    "interpretation": _interpretation,
    "env_bootstrap": _env_bootstrap,
    "memory_notes": _memory_notes,
    "retrieval": _retrieval,
    "feedback_form": _feedback_form,
    "attempt_policy": _attempt_policy,
    "execution_strategy": _execution_strategy,
    "driver_options": _driver_options,
    "fast_checks": _fast_checks,
    "limits": _limits,
    "route_policy": _route_policy,
    "decider": _decider,
    "decision_method": _decision_method,
    "judge_model": _judge_model,
    "environment_image": _environment_image,
}
CONTENT_KINDS: tuple[str, ...] = tuple(_VALIDATORS)


def validate_content(kind: str, content: dict[str, Any]) -> None:
    """RuntimeFault COMPONENT_KIND for an unknown kind, COMPONENT_CONTENT for invalid content."""
    check = _VALIDATORS.get(kind)
    if check is None:
        raise RuntimeFault("COMPONENT_KIND", "Unknown component kind", details=kind)
    check(content)


def check_combination(
    feedback_form: dict[str, Any], attempt_policy: dict[str, Any], limits: dict[str, Any]
) -> None:
    """The manifest combination rules of §2.3 (RuntimeFault MANIFEST_COMBINATION).

    The feedback header must describe the repair base the next attempt starts from; it is read
    only when feedback is on, so the rule binds only then.
    """
    if attempt_policy["max_attempts"] > limits["max_attempts"]:
        raise RuntimeFault(
            "MANIFEST_COMBINATION", "attempt_policy.max_attempts exceeds limits.max_attempts",
            details={"attempts": attempt_policy["max_attempts"],
                     "limit": limits["max_attempts"]},
        )  # fmt: skip
    if attempt_policy["feedback"]:
        want = "v1_fresh" if attempt_policy["repair_base"] == "fresh_base" else "v1"
        if feedback_form["header"] != want:
            raise RuntimeFault(
                "MANIFEST_COMBINATION",
                f"repair_base {attempt_policy['repair_base']} with feedback needs header {want}",
                details={"header": feedback_form["header"]},
            )


# -- budgets (§2.3, IC-21) ------------------------------------------------------------------------
def limits_of(ceiling: dict[str, Any]) -> dict[str, Any]:
    """The limits a legacy composition runs with: the deployment ceiling, no auxiliary share."""
    return {**{f: ceiling[f] for f in LIMIT_FIELDS}, "aux_max_tokens": 0}


def root_budget(ceiling: dict[str, Any], limits: dict[str, Any]) -> dict[str, Any]:
    """Contract root budget = min(limits, ceiling) per field (product.py:840)."""
    return {**ceiling, **{f: min(limits[f], ceiling[f]) for f in LIMIT_FIELDS}}


def node_budget(
    root: dict[str, Any], attempt_policy: dict[str, Any], limits: dict[str, Any], nodes: int
) -> dict[str, Any]:
    """Node budget: attempts from the attempt policy; tokens (root - aux) // (attempts * nodes).

    With v1 (3 attempts, limits 3, aux 0) this is today's node budget (product.py:1033-1036).
    """
    attempts = attempt_policy["max_attempts"]
    tokens = (root["max_tokens"] - limits["aux_max_tokens"]) // (attempts * nodes)
    return {**root, "max_attempts": attempts, "max_tokens": tokens}


# -- readers (§3.1) -------------------------------------------------------------------------------
def _on(part: dict[str, Any] | None) -> bool:
    return bool(part and part.get("enabled"))


@dataclass(frozen=True)
class ContextPolicy:
    env_bootstrap: dict[str, Any] | None
    memory_notes: dict[str, Any] | None
    retrieval: dict[str, Any] | None
    feedback_form: dict[str, Any]
    decider_l4: dict[str, Any] | None
    refs: dict[str, Ref | None]  # component refs, for decision/trial records

    @property
    def is_v1(self) -> bool:
        """Renders today's prompt: no context part on, the v1 feedback form, no L4 decider."""
        return (
            not (_on(self.env_bootstrap) or _on(self.memory_notes) or _on(self.retrieval))
            and self.feedback_form == V1["feedback_form"]
            and self.decider_l4 is None
        )


@dataclass(frozen=True)
class BudgetPolicy:
    attempt_policy: dict[str, Any]
    execution_strategy: dict[str, Any]
    driver_options: dict[str, Any] | None
    fast_checks: dict[str, Any] | None
    limits: dict[str, Any]
    deciders: dict[str, dict[str, Any] | None]  # "L5".."L8"
    refs: dict[str, Ref | None]


@dataclass(frozen=True)
class RouterPolicy:
    order: dict[str, list[str]]  # task class or "*" -> cell ids
    roles: dict[str, list[str]]
    interpretation: dict[str, Any]
    deciders: dict[str, dict[str, Any] | None]  # "L1".."L3"
    ref: Ref


def v1_context() -> ContextPolicy:
    return ContextPolicy(
        env_bootstrap=copy.deepcopy(V1["env_bootstrap"]),
        memory_notes=copy.deepcopy(V1["memory_notes"]),
        retrieval=copy.deepcopy(V1["retrieval"]),
        feedback_form=copy.deepcopy(V1["feedback_form"]),
        decider_l4=None,
        refs={s: None for s in (*CONTEXT_SLOTS, *CONTEXT_DECIDERS)},
    )


def v1_budget(ceiling: dict[str, Any]) -> BudgetPolicy:
    return BudgetPolicy(
        attempt_policy=copy.deepcopy(V1["attempt_policy"]),
        execution_strategy=copy.deepcopy(V1["execution_strategy"]),
        driver_options=None,
        fast_checks=copy.deepcopy(V1["fast_checks"]),
        limits=limits_of(ceiling),
        deciders={layer: None for layer in BUDGET_DECIDERS},
        refs={s: None for s in (*BUDGET_SLOTS, *BUDGET_DECIDERS)},
    )


def _carrier_kind(kind: str, why: str, details: object = None) -> Hold:
    return Hold("CARRIER_KIND", f"{kind}: {why}", details=details)


def _carrier(store: Store, scope: Scope, ref: Ref | None, kind: str) -> dict[str, Any] | None:
    """The carrier record, or None when the composition reads as the v1 manifest."""
    if ref is None:
        # every 3.0.0 composition has the field (schema); only a stand-in value lacks it
        return None
    found, value = resolve_ref(store, scope, ref)
    if found == LEGACY_POLICY:
        return None  # installed before carriers existed (§2.3 compatibility)
    if found != kind or value.get("schema") != f"amplai.{kind}.v1":
        raise _carrier_kind(kind, "the ref resolves to another kind", {"found": found})
    return value


def component_content(store: Store, scope: Scope, ref: Ref, kind: str, slot: str) -> dict[str, Any]:
    """The validated content of the component a carrier slot names."""
    value = store.get(scope, COMPONENT, ref)
    content = value.get("content")
    if (
        not isinstance(content, dict)
        or value.get("kind") != kind
        or (kind == "decider" and content.get("layer") != slot)
    ):
        raise _carrier_kind(slot, "the slot names a component of another kind or layer",
                            {"kind": value.get("kind")})  # fmt: skip
    validate_content(kind, content)
    return content


def _slots(
    store: Store, scope: Scope, carrier: dict[str, Any], kind: str,
    slots: tuple[str, ...], deciders: tuple[str, ...],
) -> tuple[dict[str, dict[str, Any] | None], dict[str, Ref | None]]:  # fmt: skip
    """Contents and refs of a carrier's component and decider slots (exactly the §2.3 keys)."""
    components, chosen = carrier.get("components"), carrier.get("deciders")
    if (
        not isinstance(components, dict)
        or not isinstance(chosen, dict)
        or set(components) != set(slots)
        or set(chosen) != set(deciders)
    ):
        raise _carrier_kind(kind, "the carrier does not have the slots of §2.3")
    contents: dict[str, dict[str, Any] | None] = {}
    refs: dict[str, Ref | None] = {}
    for slot in (*slots, *deciders):
        ref = components[slot] if slot in slots else chosen[slot]
        refs[slot] = ref
        if ref is None:
            if slot in REQUIRED_SLOTS:
                raise _carrier_kind(kind, "a required slot is empty", slot)
            contents[slot] = None
            continue
        component_kind = "decider" if slot in deciders else slot
        contents[slot] = component_content(store, scope, ref, component_kind, slot)
    return contents, refs


def context_policy(store: Store, scope: Scope, composition: dict[str, Any]) -> ContextPolicy:
    carrier = _carrier(store, scope, composition.get("context_policy_ref"), "context-policy")
    if carrier is None:
        return v1_context()
    parts, refs = _slots(store, scope, carrier, "context-policy", CONTEXT_SLOTS, CONTEXT_DECIDERS)
    feedback = parts["feedback_form"]
    assert feedback is not None  # required slot (checked by _slots)
    return ContextPolicy(
        env_bootstrap=parts["env_bootstrap"],
        memory_notes=parts["memory_notes"],
        retrieval=parts["retrieval"],
        feedback_form=feedback,
        decider_l4=parts["L4"],
        refs=refs,
    )


def budget_policy(
    store: Store, scope: Scope, composition: dict[str, Any], *, ceiling: dict[str, Any]
) -> BudgetPolicy:
    """``ceiling`` = the deployment ``Budget.wire()`` (product.py:117-126).

    Hold LIMITS_ABOVE_CEILING when the limits exceed it; Hold CARRIER_KIND when the ref resolves
    to a kind other than budget-policy or the legacy policy (which reads as v1).
    """
    carrier = _carrier(store, scope, composition.get("budget_policy_ref"), "budget-policy")
    if carrier is None:
        return v1_budget(ceiling)
    parts, refs = _slots(store, scope, carrier, "budget-policy", BUDGET_SLOTS, BUDGET_DECIDERS)
    attempt, strategy = parts["attempt_policy"], parts["execution_strategy"]
    limits = parts["limits"]
    assert attempt is not None and strategy is not None and limits is not None  # required slots
    above = {f: {"limit": limits[f], "ceiling": ceiling[f]} for f in LIMIT_FIELDS
             if limits[f] > ceiling[f]}  # fmt: skip
    if above:
        raise Hold("LIMITS_ABOVE_CEILING", "L8 limits exceed the deployment budget", details=above)
    return BudgetPolicy(
        attempt_policy=attempt,
        execution_strategy=strategy,
        driver_options=parts["driver_options"],
        fast_checks=parts["fast_checks"],
        limits=limits,
        deciders={layer: parts[layer] for layer in BUDGET_DECIDERS},
        refs=refs,
    )


def router_policy(store: Store, scope: Scope, ref: Ref) -> RouterPolicy:
    """The layered router, or a legacy task_class_baseline router (product.py:357-368) as
    route_policy v1 with its own order, the v1 interpretation and no deciders."""
    found, value = resolve_ref(store, scope, ref)
    if found != "router-policy":
        raise _carrier_kind("router-policy", "the ref resolves to another kind", {"found": found})
    if value.get("kind") == "task_class_baseline":
        legacy: dict[str, Any] = {"order": value.get("order"), "roles": {}}
        validate_content("route_policy", legacy)
        return RouterPolicy(
            order=legacy["order"], roles={}, interpretation=copy.deepcopy(V1["interpretation"]),
            deciders={layer: None for layer in ROUTER_DECIDERS}, ref=ref,
        )  # fmt: skip
    if value.get("kind") != "layered_v1":
        raise _carrier_kind("router-policy", "unknown router shape", value.get("kind"))
    parts, _refs = _slots(store, scope, value, "router-policy", ROUTER_SLOTS, ROUTER_DECIDERS)
    route, interpretation = parts["route_policy"], parts["interpretation"]
    assert route is not None and interpretation is not None  # required slots
    return RouterPolicy(
        order=route["order"],
        roles=route["roles"],
        interpretation=interpretation,
        deciders={layer: parts[layer] for layer in ROUTER_DECIDERS},
        ref=ref,
    )
