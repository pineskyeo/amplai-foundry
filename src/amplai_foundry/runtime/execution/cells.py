"""Cells, reasoning effort and driver options (Work 033 S4, D-097, interfaces.md §2.4, §3.4).

A cell is one ``model-profile`` (3.0.0): a driver, a provider model and a reasoning effort
(``reasoning_profile``). The legacy cell of a driver keeps the driver id as its cell id and the
effort ``provider-default`` (no effort flag, IC-07); every other cell is
``<driver>.<model_slug>.<effort>``.

Effort is never substituted (``spec.md`` Constraints): a value outside the driver's documented
syntax is refused (``EFFORT_UNSUPPORTED``), and a documented value runs only after an accepted
``cell-effort-probe`` turn for that exact cell (``EFFORT_UNPROBED`` / ``EFFORT_REFUSED``). Whether
the provider *applied* the effort is not observable today (확인 필요, §14 Q2).

``DispatchOptions`` carry model, effort and driver options to a port. ``check_binding`` refuses
options that differ from the activated model profile (``DISPATCH_OPTIONS_BINDING``), so a
dispatch can never run another model or effort than the one the operator approved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from jsonschema import Draft202012Validator

from ..contracts.identity import ID, digest, now
from ..errors import Hold, RuntimeFault
from ..storage.store import Scope, Store

if TYPE_CHECKING:
    from .codex import CodexProfileInputs
    from .policies import BudgetPolicy
    from .readonly_turn import ReadOnlyTurn

Ref = dict[str, Any]
DriverId = Literal["codex-cli", "claude-cli", "opencode-server"]

LEGACY_EFFORT = "provider-default"
LEGACY_CELLS: tuple[str, ...] = ("codex-cli", "claude-cli", "opencode-server")
PROVIDERS: dict[str, str] = {
    "codex-cli": "codex",
    "claude-cli": "claude",
    "opencode-server": "opencode",
}
# The effort values each driver's CLI documents (specs/033-harness-taxonomy/runs/
# cli-effort-facts.md, quoted from the worker image and the official references, 2026-10-01).
# Documented is not accepted: every value still needs an accepted probe per model (§14 Q1, Q2).
EFFORT_SYNTAX: dict[str, tuple[str, ...]] = {
    # `claude --help`: "--effort <level>"; values from the CLI reference `--effort` row
    # ("low, medium, high, xhigh, max, or ultracode"); ultracode turns on workflow orchestration
    # and is not a reasoning level, so it is excluded (cli-effort-facts.md, §14 Q15)
    "claude-cli": ("low", "medium", "high", "xhigh", "max"),
    # `codex exec --help` / `codex exec resume --help`: "-c, --config <key=value>"; values of
    # `model_reasoning_effort` from the config reference (cli-effort-facts.md, §14 Q1)
    "codex-cli": ("low", "medium", "high", "xhigh", "max", "ultra"),
    # no effort axis this round (spec.md OD-12): the per-message HTTP field is 확인 필요 (§14 Q3)
    "opencode-server": (),
}
EFFORT_TOKEN = re.compile(r"[a-z][a-z0-9-]{0,31}")
MODEL_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}")
UNPINNED_MODELS = frozenset({"latest", "default", "auto"})

CELL_KIND = "harness-cell"
REGISTRY_KIND, REGISTRY_ID = "cell-registry", "cells"
PROBE_KIND = "cell-effort-probe"
PROBE_OUTCOMES = ("accepted", "refused", "error")
# One structured read-only turn on an empty scratch directory (IC-14): proves only that the
# provider completed a turn with the flag (§2.4, §14 Q2).
PROBE_PROMPT = 'Reply with the JSON object {"ok": true}. Do not read or change any file.'
PROBE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ok"],
    "properties": {"ok": {"type": "boolean"}},
}


def model_slug(model: str) -> str:
    """``model`` with ``/`` → ``.`` (a record id has no ``/``, ``codex.py`` model-profile ids)."""
    return model.replace("/", ".")


def check_model(model: str) -> None:
    if (
        not isinstance(model, str)
        or model in UNPINNED_MODELS
        or not MODEL_TOKEN.fullmatch(model)
        or ".." in model
    ):
        raise Hold("MODEL_UNPINNED", "An explicit, pinned model id is required", details=model)


def check_effort_token(effort: str) -> None:
    """The effort reaches argv (``--effort E`` / ``-c model_reasoning_effort=E``): a bare token."""
    if not isinstance(effort, str) or not EFFORT_TOKEN.fullmatch(effort):
        raise Hold("EFFORT_UNSUPPORTED", "An effort is a lowercase token", details=effort)


@dataclass(frozen=True)
class Cell:
    cell_id: str
    driver_id: DriverId
    model: str
    effort: str
    # (app_id, path) per model; effort variants of one model share them (D-097); = local.json
    qualification_reports: tuple[tuple[str, str], ...]
    legacy: bool

    def __post_init__(self) -> None:
        if self.driver_id not in LEGACY_CELLS:
            raise RuntimeFault("DRIVER_KIND", "Unknown driver", details=self.driver_id)
        check_model(self.model)
        if self.effort != LEGACY_EFFORT:
            check_effort_token(self.effort)
        if self.legacy and self.effort != LEGACY_EFFORT:
            raise Hold("EFFORT_UNSUPPORTED", "A legacy cell has the provider-default effort")
        want = self.make_id(self.driver_id, self.model, self.effort, legacy=self.legacy)
        if self.cell_id != want:
            raise Hold("CELL_UNKNOWN", "The cell id does not follow IC-07", details=self.cell_id)

    @staticmethod
    def make_id(driver_id: str, model: str, effort: str, *, legacy: bool) -> str:
        """IC-07: the legacy cell id is the driver id, others ``<driver>.<model_slug>.<effort>``."""
        if legacy:
            return driver_id
        cell_id = f"{driver_id}.{model_slug(model)}.{effort}"
        if not ID.fullmatch(cell_id):
            raise Hold("MODEL_UNPINNED", "The cell id is not a record id", details=cell_id)
        return cell_id

    @property
    def provider(self) -> str:
        return PROVIDERS[self.driver_id]

    def report_for(self, app_id: str) -> str | None:
        return dict(self.qualification_reports).get(app_id)


@dataclass(frozen=True)
class DispatchOptions:
    model: str
    effort: str | None  # None == provider default (no flag)
    max_turns: int | None = None
    append_system_prompt: str | None = None
    allowed_tools: tuple[str, ...] | None = None
    codex_config: tuple[tuple[str, str], ...] = ()
    capture_trace: bool = False

    def __post_init__(self) -> None:
        from .policies import CLAUDE_OPTION_ALLOWLIST, CLAUDE_TOOLS, CODEX_CONFIG_ALLOWLIST

        check_model(self.model)
        if self.effort is not None:
            check_effort_token(self.effort)
            if self.effort == LEGACY_EFFORT:
                raise Hold("EFFORT_UNSUPPORTED", "provider-default is effort None, never a flag")
        bad: list[str] = []
        if self.max_turns is not None and (
            type(self.max_turns) is not int or not 1 <= self.max_turns <= 500
        ):
            bad.append("max_turns")
        if self.append_system_prompt is not None and (
            not isinstance(self.append_system_prompt, str)
            or not 0 < len(self.append_system_prompt) <= 2000
            # an argv value, never a flag or a C-string terminator (§3.4 argv contract)
            or self.append_system_prompt.startswith("-")
            or "\x00" in self.append_system_prompt
        ):
            bad.append("append_system_prompt")
        if self.allowed_tools is not None and (
            not self.allowed_tools
            or len(set(self.allowed_tools)) != len(self.allowed_tools)
            or not set(self.allowed_tools) <= set(CLAUDE_TOOLS)
        ):
            bad.append("allowed_tools")
        # "-c k=v" per allowlisted key only (§3.4 argv contract); effort is the cell's, never a
        # driver option (§2.2), so model_reasoning_effort can never enter through codex_config
        if any(
            len(pair) != 2 or pair[0] not in CODEX_CONFIG_ALLOWLIST or not isinstance(pair[1], str)
            for pair in self.codex_config
        ):
            bad.append("codex_config")
        if bad:
            raise Hold(
                "DRIVER_OPTIONS_UNSUPPORTED", "Driver options outside their allowed values",
                details=bad,
            )  # fmt: skip
        # §14 Q13: a Claude option enters the argv only once CLAUDE_OPTION_ALLOWLIST admits it.
        # Checked here, like the Codex allowlist above, so every construction path (direct
        # callers, from_wire replay in resume_exact) is gated, not only resolve_options.
        unmeasured = sorted(
            name
            for name, value in (
                ("max_turns", self.max_turns),
                ("append_system_prompt", self.append_system_prompt),
                ("allowed_tools", self.allowed_tools),
            )
            if value is not None and name not in CLAUDE_OPTION_ALLOWLIST
        )
        if unmeasured:
            raise Hold(
                "DRIVER_OPTIONS_UNSUPPORTED", "Claude driver options not yet measured (§14 Q13)",
                details=unmeasured,
            )  # fmt: skip

    def wire(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "effort": self.effort,
            "max_turns": self.max_turns,
            "append_system_prompt": self.append_system_prompt,
            "allowed_tools": list(self.allowed_tools) if self.allowed_tools is not None else None,
            "codex_config": [list(pair) for pair in self.codex_config],
            "capture_trace": self.capture_trace,
        }

    def digest(self) -> str:
        return digest(self.wire())

    def has_driver_options(self) -> bool:
        return (
            self.max_turns is not None
            or self.append_system_prompt is not None
            or self.allowed_tools is not None
            or bool(self.codex_config)
        )

    def is_default(self) -> bool:
        """Effort None, no driver options, no trace capture: today's argv (golden G2)."""
        return self.effort is None and not self.has_driver_options() and not self.capture_trace

    @classmethod
    def default(cls, model: str) -> DispatchOptions:
        return cls(model, None)

    @classmethod
    def from_wire(cls, value: dict[str, Any]) -> DispatchOptions:
        tools = value.get("allowed_tools")
        return cls(
            value["model"],
            value.get("effort"),
            max_turns=value.get("max_turns"),
            append_system_prompt=value.get("append_system_prompt"),
            allowed_tools=tuple(tools) if tools is not None else None,
            codex_config=tuple((k, v) for k, v in value.get("codex_config") or []),
            capture_trace=bool(value.get("capture_trace")),
        )


def normalized_digest(options: DispatchOptions | None) -> str | None:
    """None for no options or default options, else the options digest (journal, checkpoint)."""
    return None if options is None or options.is_default() else options.digest()


# -- resolution and binding ----------------------------------------------------------------------
def _profile_records(
    store: Store, scope: Scope, profile: dict[str, Ref]
) -> tuple[dict[str, Any], dict[str, Any]]:
    model = store.get(scope, "model-profile", profile["model_profile_ref"])
    driver = store.get(scope, "driver-capabilities", profile["driver_profile_ref"])
    return model, driver


def profile_effort(model_profile: dict[str, Any]) -> str | None:
    effort = model_profile.get("reasoning_profile")
    return None if effort == LEGACY_EFFORT else effort


def resolve_options(
    store: Store,
    scope: Scope,
    profile: dict[str, Ref],
    policy: BudgetPolicy,
    *,
    capture_trace: bool,
) -> DispatchOptions:
    """Model and effort from the activated model profile; driver options from the budget
    policy's ``driver_options`` component (L5), only the keys the policies allowlists admit
    (``CLAUDE_OPTION_ALLOWLIST`` is empty until §14 Q13 is measured; ``CODEX_CONFIG_ALLOWLIST``
    until §14 Q1). A non-empty option for a driver it does not apply to is refused."""
    from .policies import CLAUDE_OPTION_ALLOWLIST

    model, driver = _profile_records(store, scope, profile)
    driver_id = driver["driver_id"]
    options = policy.driver_options or {}
    claude = {k: v for k, v in (options.get("claude") or {}).items() if v is not None}
    codex = [tuple(pair) for pair in (options.get("codex") or {}).get("config") or []]
    if (driver_id != "claude-cli" and claude) or (driver_id != "codex-cli" and codex):
        raise Hold(
            "DRIVER_OPTIONS_UNSUPPORTED", "Driver options for another driver",
            details={"driver": driver_id},
        )  # fmt: skip
    if set(claude) - CLAUDE_OPTION_ALLOWLIST:
        raise Hold(
            "DRIVER_OPTIONS_UNSUPPORTED", "Claude driver options not yet measured (§14 Q13)",
            details=sorted(set(claude) - CLAUDE_OPTION_ALLOWLIST),
        )  # fmt: skip
    tools = claude.get("allowed_tools")
    return DispatchOptions(
        model["provider_model_id"],
        profile_effort(model),
        max_turns=claude.get("max_turns"),
        append_system_prompt=claude.get("append_system_prompt"),
        allowed_tools=tuple(tools) if tools is not None else None,
        codex_config=tuple((str(k), str(v)) for k, v in codex),
        capture_trace=capture_trace,
    )


def check_binding(
    store: Store, scope: Scope, profile: dict[str, Ref], options: DispatchOptions
) -> None:
    """Hold DISPATCH_OPTIONS_BINDING unless ``options`` name the activated model profile's model
    and effort (None for provider-default)."""
    model = store.get(scope, "model-profile", profile["model_profile_ref"])
    want = {"model": model.get("provider_model_id"), "effort": profile_effort(model)}
    got = {"model": options.model, "effort": options.effort}
    if got != want:
        raise Hold(
            "DISPATCH_OPTIONS_BINDING", "Dispatch options differ from the activated model profile",
            details={"profile": want, "options": got},
        )  # fmt: skip


def _probe_matches(cell: Cell, probe: dict[str, Any]) -> bool:
    return (
        probe.get("cell_id") == cell.cell_id
        and probe.get("driver_id") == cell.driver_id
        and probe.get("model") == cell.model
        and probe.get("effort") == cell.effort
    )


def validate_effort(cell: Cell, probe: dict[str, Any] | None) -> None:
    """Refuse an effort that is not documented for the driver or not accepted by a probe of this
    exact cell. Never substitutes another value (``spec.md`` Constraints).

    Hold EFFORT_UNSUPPORTED (outside ``EFFORT_SYNTAX``; with an empty syntax, only an accepted
    probe admits a value, §3.4) | EFFORT_UNPROBED | EFFORT_REFUSED (probe outcome != accepted).
    """
    if cell.effort == LEGACY_EFFORT:
        return
    if cell.driver_id == "opencode-server":
        raise Hold("EFFORT_UNSUPPORTED", "OpenCode cells have no effort axis (OD-12, §14 Q3)")
    syntax = EFFORT_SYNTAX[cell.driver_id]
    matching = probe if probe is not None and _probe_matches(cell, probe) else None
    if cell.effort not in syntax and (syntax or matching is None):
        raise Hold(
            "EFFORT_UNSUPPORTED", f"{cell.driver_id} does not document this effort",
            details={"effort": cell.effort, "documented": list(syntax)},
        )  # fmt: skip
    if matching is None:
        raise Hold("EFFORT_UNPROBED", "Run amplai ops local-cell probe for this cell first")
    if matching.get("outcome") != "accepted":
        raise Hold(
            "EFFORT_REFUSED", "The effort probe of this cell was not accepted",
            details={"outcome": matching.get("outcome")},
        )  # fmt: skip


# -- record validators (§2.0: every new kind is validated before put) ----------------------------
_REF = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "revision", "digest"],
    "properties": {
        "id": {"type": "string", "pattern": ID.pattern},
        "revision": {"type": "integer", "minimum": 1},
        "digest": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
    },
}
_STR = {"type": "string", "minLength": 1, "maxLength": 512}
CELL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema", "scope", "cell_id", "driver_id", "provider_model_id", "effort",
        "model_profile_ref", "driver_profile_ref", "qualification_ref", "effort_probe_ref",
        "image", "enabled", "legacy", "created_at",
    ],
    "properties": {
        "schema": {"const": "amplai.harness-cell.v1"},
        "scope": {"type": "object"},
        "cell_id": {"type": "string", "pattern": ID.pattern},
        "driver_id": {"enum": list(LEGACY_CELLS)},
        "provider_model_id": _STR,
        "effort": {"type": "string", "minLength": 1, "maxLength": 32},
        "model_profile_ref": _REF,
        "driver_profile_ref": _REF,
        "qualification_ref": _REF,
        "effort_probe_ref": {"anyOf": [_REF, {"type": "null"}]},
        "image": _STR,
        "enabled": {"type": "boolean"},
        "legacy": {"type": "boolean"},
        "created_at": _STR,
    },
}  # fmt: skip
PROBE_RECORD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema", "scope", "cell_id", "driver_id", "model", "effort", "driver_version", "image",
        "outcome", "argv_digest", "stream_evidence", "usage", "checked_at",
    ],
    "properties": {
        "schema": {"const": "amplai.cell-effort-probe.v1"},
        "scope": {"type": "object"},
        "cell_id": {"type": "string", "pattern": ID.pattern},
        "driver_id": {"enum": list(LEGACY_CELLS)},
        "model": _STR,
        "effort": {"type": "string", "minLength": 1, "maxLength": 32},
        "driver_version": _STR,
        "image": _STR,
        "outcome": {"enum": list(PROBE_OUTCOMES)},
        "argv_digest": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
        "stream_evidence": {
            "type": "object",
            "additionalProperties": False,
            "required": ["effort_reported", "error_text_digest"],
            "properties": {
                "effort_reported": {"type": ["string", "null"]},
                "error_text_digest": {"type": ["string", "null"]},
            },
        },
        "usage": {"type": ["object", "null"]},
        "checked_at": _STR,
    },
}  # fmt: skip


def _validate(kind: str, schema: dict[str, Any], value: dict[str, Any]) -> None:
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda e: str(e.path))
    if errors:
        raise RuntimeFault(
            "SCHEMA_INVALID", kind,
            details=[{"path": list(e.path), "message": e.message} for e in errors],
        )  # fmt: skip


def validate_cell_record(value: dict[str, Any]) -> None:
    _validate(CELL_KIND, CELL_SCHEMA, value)
    if value["effort"] != LEGACY_EFFORT and value["effort_probe_ref"] is None:
        raise RuntimeFault("SCHEMA_INVALID", CELL_KIND, details="effort_probe_ref is required")


def validate_probe_record(value: dict[str, Any]) -> None:
    _validate(PROBE_KIND, PROBE_RECORD_SCHEMA, value)


def probe_id(cell_id: str) -> str:
    return "probe-" + digest(cell_id)[7:31]


# -- probes -------------------------------------------------------------------------------------
def run_probe(
    scope: Scope,
    cell: Cell,
    turn: ReadOnlyTurn,
    scratch: Path,
    *,
    driver_version: str,
    image: str,
    argv: list[str],
) -> dict[str, Any]:
    """One effort probe turn on an empty scratch directory (IC-14) → a ``cell-effort-probe``
    value. "accepted" = the provider completed a structured turn with the flag; a turn that did
    not complete is "refused" and anything else (timeout, output shape) "error". Whether the
    effort was applied is not observable (확인 필요, §14 Q2), so ``effort_reported`` stays null."""
    if cell.effort == LEGACY_EFFORT:
        raise Hold("EFFORT_UNSUPPORTED", "A provider-default cell has no effort to probe")
    if cell.driver_id == "opencode-server" or cell.effort not in EFFORT_SYNTAX[cell.driver_id]:
        validate_effort(cell, None)  # raises EFFORT_UNSUPPORTED; nothing undocumented is sent
    scratch = Path(scratch).absolute()
    if scratch.resolve() != scratch or not scratch.is_dir() or any(scratch.iterdir()):
        raise Hold("TURN_FAILED", "The probe needs an empty, link-free scratch directory")
    usage: dict[str, Any] | None = None
    error: str | None = None
    try:
        result = turn.run(prompt=PROBE_PROMPT, schema=PROBE_SCHEMA, workspace=scratch)
        usage = result.usage
        outcome = "accepted"
    except Hold as hold:
        outcome = "refused" if hold.code == "TURN_FAILED" else "error"
        error = digest({"code": hold.code, "details": str(hold.details or "")})
    value = {
        "schema": "amplai.cell-effort-probe.v1",
        "scope": scope.wire(),
        "cell_id": cell.cell_id,
        "driver_id": cell.driver_id,
        "model": cell.model,
        "effort": cell.effort,
        "driver_version": driver_version,
        "image": image,
        "outcome": outcome,
        "argv_digest": digest(argv),
        "stream_evidence": {"effort_reported": None, "error_text_digest": error},
        "usage": usage,
        "checked_at": now(),
    }
    validate_probe_record(value)
    return value


def store_probe(store: Store, scope: Scope, value: dict[str, Any]) -> Ref:
    """Write a probe as the next revision of ``probe-<cell hash>`` (the latest one counts)."""
    validate_probe_record(value)
    return _put_latest(store, scope, PROBE_KIND, probe_id(value["cell_id"]), value)


def latest_probe(store: Store, scope: Scope, cell_id: str) -> tuple[Ref, dict[str, Any]] | None:
    rows = [
        (r, v) for r, v in store.list_objects(scope, PROBE_KIND) if r["id"] == probe_id(cell_id)
    ]
    return max(rows, key=lambda row: row[0]["revision"]) if rows else None


def _put_latest(
    store: Store, scope: Scope, kind: str, object_id: str, value: dict[str, Any]
) -> Ref:
    rows = [r for r, _ in store.list_objects(scope, kind) if r["id"] == object_id]
    latest = max(rows, key=lambda r: r["revision"]) if rows else None
    if latest and latest["digest"] == digest(value):
        return latest
    with store.tx() as db:
        return store.put(db, scope, kind, object_id, latest["revision"] + 1 if latest else 1, value)


# -- install ------------------------------------------------------------------------------------
class CellInstaller:
    def __init__(self, store: Store, scope: Scope) -> None:
        self.store, self.scope = store, scope

    def registry(self) -> dict[str, Any]:
        """The ``cell-registry`` head data, ``{"cells": {}, "order": []}`` before any install."""
        try:
            data: dict[str, Any] = self.store.head(self.scope, REGISTRY_KIND, REGISTRY_ID)["data"]
        except RuntimeFault as exc:
            if exc.code != "NOT_FOUND":
                raise
            return {"cells": {}, "order": []}
        return data

    def _legacy_model(self, driver_id: str) -> str | None:
        ref = self.registry()["cells"].get(driver_id)
        if ref is None:
            return None
        value: str = self.store.get(self.scope, CELL_KIND, ref)["provider_model_id"]
        return value

    def profile(
        self,
        cell: Cell,
        inputs: CodexProfileInputs,
        capabilities: list[dict[str, Any]],
        *,
        probe: dict[str, Any] | None,
    ) -> tuple[dict[str, Ref], dict[str, Any]]:
        """environment → qualification → driver-capabilities → model-profile of ``cell`` in the
        image of ``inputs``, and the measured qualification. No ``harness-cell`` record: a cell
        used by apps in several images keeps one record (``install``, first image)."""
        from dataclasses import replace

        from .codex import install_codex_profile, measured_qualification

        if cell.provider != inputs.provider:
            raise Hold("CELL_UNKNOWN", "The profile inputs name another driver")
        inputs = replace(inputs, model=cell.model)
        validate_effort(cell, probe)
        measured = measured_qualification(inputs)
        if (
            probe is not None
            and cell.effort != LEGACY_EFFORT
            and (
                probe.get("image") != measured["image"]
                or probe.get("driver_version") != measured["driver_version"]
            )
        ):
            raise Hold("EFFORT_UNPROBED", "The probe ran on another image or driver version")
        legacy_model = None if cell.legacy else self._legacy_model(cell.driver_id)
        refs = install_codex_profile(
            self.store, self.scope, inputs, capabilities,
            effort=cell.effort, legacy=cell.legacy,
            own_driver_record=not cell.legacy and cell.model != legacy_model,
        )  # fmt: skip
        return refs, measured

    def install(
        self,
        cell: Cell,
        inputs: CodexProfileInputs,
        capabilities: list[dict[str, Any]],
        *,
        probe: dict[str, Any] | None,
    ) -> dict[str, Ref]:
        """environment → qualification → driver-capabilities → model-profile → harness-cell.

        The qualification is the model's (D-097: effort variants share it); a legacy cell writes
        exactly today's records plus its ``harness-cell``. ``measured_qualification`` holds
        DRIVER_UNQUALIFIED unchanged.
        """
        refs, measured = self.profile(cell, inputs, capabilities, probe=probe)
        probe_ref = (
            store_probe(self.store, self.scope, probe)
            if probe is not None and cell.effort != LEGACY_EFFORT
            else None
        )
        value = {
            "schema": "amplai.harness-cell.v1",
            "scope": self.scope.wire(),
            "cell_id": cell.cell_id,
            "driver_id": cell.driver_id,
            "provider_model_id": cell.model,
            "effort": cell.effort,
            "model_profile_ref": refs["model"],
            "driver_profile_ref": refs["driver"],
            "qualification_ref": refs["qualification"],
            "effort_probe_ref": probe_ref,
            "image": measured["image"],
            "enabled": inputs.enabled,
            "legacy": cell.legacy,
        }
        cell_ref = self._put_cell(value)
        self._register(cell.cell_id, cell_ref)
        return {**refs, "cell": cell_ref}

    def _put_cell(self, value: dict[str, Any]) -> Ref:
        """A new revision only when more than ``created_at`` changed (an idempotent boot)."""
        rows = [
            (r, v) for r, v in self.store.list_objects(self.scope, CELL_KIND)
            if r["id"] == value["cell_id"]
        ]  # fmt: skip
        latest = max(rows, key=lambda row: row[0]["revision"]) if rows else None
        if latest and {k: v for k, v in latest[1].items() if k != "created_at"} == value:
            return latest[0]
        record = {**value, "created_at": now()}
        validate_cell_record(record)
        with self.store.tx() as db:
            revision = latest[0]["revision"] + 1 if latest else 1
            return self.store.put(db, self.scope, CELL_KIND, value["cell_id"], revision, record)

    def _register(self, cell_id: str, ref: Ref) -> None:
        with self.store.tx() as db:
            try:
                head = self.store.head(self.scope, REGISTRY_KIND, REGISTRY_ID, db=db)
                version, data = head["row_version"], head["data"]
            except RuntimeFault as exc:
                if exc.code != "NOT_FOUND":
                    raise
                version, data = 0, {"cells": {}, "order": []}
            if data["cells"].get(cell_id) == ref:
                return
            order = data["order"] if cell_id in data["order"] else [*data["order"], cell_id]
            self.store.cas(
                db, self.scope, REGISTRY_KIND, REGISTRY_ID, version, "active",
                {"cells": {**data["cells"], cell_id: ref}, "order": order},
            )  # fmt: skip
