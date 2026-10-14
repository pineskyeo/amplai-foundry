"""``amplai meta nightly approve | revoke | run | tick | status | print-agent`` and ``amplai meta
quota`` (Work 033 S12, interfaces.md §8.6, §8.8, §8.9, §12.1; IC-17 and IC-18 provisional; Work 034
S1d for ``tick``, the read-only ``status`` and the controls).

- ``approve`` / ``revoke``: the human operator (``local_deployment.meta_operator``, permission
  ``nightly.approve``) issues or revokes the standing approval ``nightly.explore`` bound to the
  nightly policy of the config's ``meta.nightly`` (≤ 7 nights). Nothing runs a night without it.
- ``run``: the nightly runner as ``amplai-meta-nightly`` on the deployment at ``--config``, which
  must be a **separate meta deployment** with ``meta.nightly`` set (its own ``local.json`` and
  store); the server's default config ``~/.amplai/local/local.json``, and any config whose
  runtime root is the one that config names, are refused (Hold NIGHT_DEPLOYMENT), so the running
  server is never touched. IC-29 (provisional): the night rebuilds each cell's executor
  qualification from the newest ``executor-qualification`` record a human operator wrote for it
  (any ``--per-trial-tokens``/``--basis``/``--evidence`` trial command writes one), so the launchd
  agent needs no extra argument; a cell without one stops the night at preflight
  (``QUALIFIED_EXECUTOR_REQUIRED``). ``--per-trial-tokens``, ``--basis`` and ``--evidence`` here
  still qualify the first cell as the operator.
  With ``meta.control_store`` the night reads the product's ``nightly-control`` records read-only
  (``runtime/nightly_control.py``) and stops when it cannot (Work 034 S1d, D-117).
- ``tick`` (Work 034 S1d, N-1): what the service manager runs every five minutes; it starts
  tonight's night at its scheduled start or on a front agent's start request that meets N-9, and
  otherwise ends at once.
- ``status``: the night's head, the current standing approval and the controls, read from the meta
  store opened read-only (no boot, no owner lock), so it works while a night runs (N-5, AC-N4).
- ``print-agent``: prints the launchd agent filled from
  ``deployment/launchd/ai.amplai.meta-nightly.plist.template`` (the tick every 300 seconds); it
  installs nothing.
- ``amplai meta quota``: the quota windows observed now and the pilot headroom of the nights
  recorded since ``--since`` (§8.6).
"""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from xml.sax.saxutils import escape

import typer

from ..errors import Hold, RuntimeFault
from . import REPO_ROOT, Guarded, opened_deployment

# §8.9: the meta deployment's config and logs live under ~/.amplai/meta/
DEFAULT_META_CONFIG = Path("~/.amplai/meta/local.json")
SERVER_CONFIG = Path("~/.amplai/local/local.json")  # the running server's (never touched)
LOG_DIR = Path("~/.amplai/meta/logs")
LABEL = "ai.amplai.meta-nightly"
TEMPLATE = REPO_ROOT / "deployment" / "launchd" / (LABEL + ".plist.template")
PLACEHOLDERS = ("LABEL", "AMPLAI", "CONFIG", "LOG_DIR", "PATH")

MetaConfigOption = Annotated[
    Path, typer.Option("--config", help="the separate meta deployment's local.json")
]
DateOption = Annotated[
    str | None, typer.Option("--date", help="the night, YYYY-MM-DD (default: today, local)")
]


def meta_config(config: Path) -> Path:
    """``--config`` unless it is the running server's config (Hold NIGHT_DEPLOYMENT, §8.8)."""
    path = Path(config).expanduser().absolute()
    if path == SERVER_CONFIG.expanduser().absolute():
        raise Hold(
            "NIGHT_DEPLOYMENT",
            "The nightly runner works on a separate meta deployment, never the server's config",
            details={"config": str(path)},
        )
    return path


def server_runtime_root(server: Path = SERVER_CONFIG) -> Path | None:
    """The running server's runtime root (its store) from its config; None without a readable
    one."""
    from ..local_deployment import LocalConfig

    path = Path(server).expanduser().absolute()
    try:
        cfg = LocalConfig.model_validate_json(path.read_bytes())
    except (OSError, ValueError):
        return None
    root = Path(cfg.runtime_root).expanduser()
    return (root if root.is_absolute() else path.parent / root).absolute()


def check_separate(dep: Any, server: Path = SERVER_CONFIG) -> None:
    """§8.8: the meta deployment has its own store; Hold NIGHT_DEPLOYMENT when its runtime root
    is the running server's."""
    mine = dep.local(dep.config.runtime_root).resolve()
    theirs = server_runtime_root(server)
    if theirs is not None and mine == theirs.resolve():
        raise Hold(
            "NIGHT_DEPLOYMENT",
            "The meta deployment shares the server's runtime root; give it its own store",
            details={"runtime_root": str(mine)},
        )


@contextmanager
def opened_meta(config: Path) -> Iterator[Any]:
    """The separate meta deployment at ``config`` (``meta_config``, ``check_separate``)."""
    with opened_deployment(meta_config(config)) as dep:
        check_separate(dep)
        yield dep


def nightly_config(dep: Any) -> Any:
    """The deployment's ``meta.nightly`` as a ``NightlyConfig`` (Hold NIGHT_CONFIG without it)."""
    from ...meta_harness.nightly import NightlyConfig

    meta = getattr(dep.config, "meta", None)
    entry = getattr(meta, "nightly", None)
    if entry is None:
        raise Hold("NIGHT_CONFIG", "The config has no meta.nightly: not a meta deployment")
    return NightlyConfig.from_entry(entry)


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def today() -> str:
    return datetime.now().astimezone().date().isoformat()


def tonight(cfg: Any) -> str:
    """The night now belongs to (``nightly_control.night_date`` with ``meta.nightly.start_at``):
    the default date of ``run`` and ``status`` (PR #95 failure review P3), the date the tick
    uses. Without ``meta.nightly`` the calendar date."""
    from ..nightly_control import night_date

    nightly = getattr(getattr(cfg, "meta", None), "nightly", None)
    if nightly is None:
        return today()
    return night_date(time.time(), nightly.start_at)


def approve(
    dep: Any, *, nights: int, budget_trials: int, max_tokens: int, max_wall_seconds: int
) -> dict[str, Any]:
    """Issue the standing approval as the human operator (IC-17)."""
    from ...meta_harness.nightly import nightly_policy

    if type(nights) is not int or not 1 <= nights <= 7:
        raise RuntimeFault("NIGHTLY_POLICY", "--nights is 1 to 7 (IC-13)")
    config = nightly_config(dep)
    start = float(dep.store.clock())
    policy = nightly_policy(
        config,
        budget_trials=budget_trials,
        valid_from=iso(start),
        valid_until=iso(start + nights * 86400),
        max_budget={
            "max_wall_seconds": max_wall_seconds,
            "max_attempts": 10,
            "max_tokens": max_tokens,
            "max_cost_microunits": 0,
            "currency": "USD",
            "max_parallel_works": config.max_parallel,
            "max_delegation_depth": 0,
        },
    )
    ref = dep.meta_local.approvals.issue_nightly(dep.meta_operator(), policy)
    return {"standing_ref": ref, "policy": policy, "provisional": ["IC-17", "IC-18"]}


def revoke(dep: Any, approval_id: str | None) -> dict[str, Any]:
    """Revoke the named (or the current) standing approval as the human operator."""
    from ..execution.meta_local import APPROVAL_KIND, NIGHTLY_ACTION

    approvals = dep.meta_local.approvals
    if approval_id is None:
        ref = approvals.current_standing()
        if ref is None:
            raise Hold("STANDING_APPROVAL", "No current standing approval to revoke")
    else:
        found = [
            r
            for r, v in dep.store.list_objects(dep.scope, APPROVAL_KIND)
            if r["id"] == approval_id and v.get("action") == NIGHTLY_ACTION
        ]
        if not found:
            raise Hold("STANDING_APPROVAL", "No standing approval with that id")
        ref = found[0]
    approvals.revoke(dep.meta_operator(), ref)
    return {"revoked": ref}


def status(dep: Any, date: str) -> dict[str, Any]:
    return _status(dep.store, dep.scope, dep.meta_local.approvals, date)


def _status(store: Any, scope: Any, approvals: Any, date: str) -> dict[str, Any]:
    from ...meta_harness.nightly import RUN_KIND

    out: dict[str, Any] = {"date": date, "standing_ref": None, "night": None}
    ref = approvals.current_standing()
    if ref is not None:
        policy = approvals.standing(ref)["policy"]
        out["standing_ref"] = ref
        out["standing"] = {k: policy[k] for k in ("cells", "budget_trials", "valid_until")}
    for head_id in (f"night-{date}", f"night-{date}-dry"):
        try:
            head = store.head(scope, RUN_KIND, head_id)
        except RuntimeFault as exc:
            if exc.code != "NOT_FOUND":
                raise
            continue
        out["night" if not head_id.endswith("-dry") else "dry_run"] = {
            "state": head["state"],
            **{
                k: head["data"].get(k)
                for k in (
                    "phase",
                    "trials",
                    "stopped",
                    "reconcile_pending",
                    "queued_confirmations",
                    "proposals",
                    "finished_at",
                )
            },
        }
    return out


# -- Work 034 S1d: read-only status, the controls and the tick ------------------------------------
def load_config(config: Path) -> tuple[Path, Any]:
    """(``--config`` absolute, its ``LocalConfig``) without booting the deployment (no owner lock,
    nothing installed); the server's config is refused (``meta_config``)."""
    from ..local_deployment import LocalConfig

    path = meta_config(config)
    try:
        cfg = LocalConfig.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise RuntimeFault("LOCAL_INPUT", f"--config is not a readable config: {exc}") from exc
    if cfg.schema_version != "local-1":
        raise Hold("CONFIG_VERSION", "Unknown local product configuration")
    return path, cfg


def _under(path: Path, value: str) -> Path:
    root = Path(value).expanduser()
    return (root if root.is_absolute() else path.parent / root).absolute()


def control_reader(path: Path, cfg: Any) -> Any:
    """The night's read-only reader of the product's control records (``meta.control_store``), or
    None when the config names none (then no control applies, as before S1d). A control store
    that is the meta deployment's own runtime root reads as unreadable (PR #95 review P2-1)."""
    from ..nightly_control import ControlReader
    from ..storage.store import Scope

    entry = getattr(getattr(cfg, "meta", None), "control_store", None)
    if entry is None:
        return None
    return ControlReader(
        _under(path, entry.runtime_root),
        Scope.parse(entry.scope),
        meta_root=_under(path, cfg.runtime_root),
    )


class TickBusy(Exception):
    """The meta deployment could not be opened because another process owns its store."""


def status_readonly(config: Path, date: str | None = None) -> dict[str, Any]:
    """``status`` read-only (N-5, AC-N4): the meta store opened with ``Store(readonly=True)`` and
    no boot, so it answers while a night holds the store (``ACTIVE_OWNER``). With
    ``meta.control_store`` it also shows the controls binding the night (or that they cannot be
    read)."""
    from ..execution.meta_local import LocalMetaApprovals
    from ..storage.store import Scope, Store

    path, cfg = load_config(config)
    date = date or tonight(cfg)
    root = _under(path, cfg.runtime_root)
    if not (root / "runtime.sqlite3").is_file():
        raise RuntimeFault("NOT_FOUND", "No runtime store at the configured runtime root")
    scope = Scope.parse(cfg.scope)
    with Store(root, readonly=True) as store:
        out = _status(store, scope, LocalMetaApprovals(store, scope), date)
    reader = control_reader(path, cfg)
    if reader is not None:
        try:
            found = reader(date)
        except Hold as exc:
            out["controls"] = {"unreadable": exc.code}
        else:
            out["controls"] = {
                "stop": found.stop,
                "budget_trials": found.budget_trials,
                "stop_at": list(found.stop_at),
                "start_requests": [r["control_id"] for r in found.starts],
                "applied": list(found.applied),
            }
    return out


def run_night(
    dep: Any, date: str, *, dry_run: bool = False, trigger: dict[str, Any] | None = None,
    qualify: tuple[int, str, list[str]] | None = None,
) -> dict[str, Any]:  # fmt: skip
    """One night on the opened meta deployment, with the controls of ``meta.control_store``."""
    from ...meta_harness.nightly import LocalNightlyBackend, NightlyRunner

    runner = NightlyRunner(
        dep, nightly_config(dep), controls=control_reader(Path(dep.path), dep.config)
    )
    runner.trigger = trigger
    backend = LocalNightlyBackend.open(dep, runner)
    backend.proposer_run, backend.dream_turn = _proposer_hooks(backend.ops)
    runner._backend = backend
    if qualify is not None:
        per_trial_tokens, basis, evidence = qualify
        backend.ops.qualify_executor(basis, list(evidence), per_trial_tokens)
    ref: dict[str, Any] = runner.run(date, dry_run=dry_run)
    return ref


def tick(
    config: Path,
    *,
    clock: Callable[[], float] = time.time,
    start: Callable[[str, dict[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """The service manager's five-minute tick (N-1, D-117, D-118): start tonight's night when its
    scheduled start (``meta.nightly.start_at``) has come and its stop has not, or when a front
    agent's start request still meets N-9 (``nightly_control.start_refusal``, checked again here
    right before the start); otherwise end at once. One date runs once (``NIGHT_STATE``).

    Everything before the start is read-only. A night already recorded for the date ends the tick
    (``ran``). When the meta deployment cannot be opened because another process owns its store
    (a calibration, a manual command), the tick ends with ``busy`` (``TickBusy``) and the next
    tick tries again: for the schedule, every five minutes until the scheduled night's end
    (``nightly_control.scheduled_end``: min(``stop_at``, start + ``max_hours``, each tightened
    stop time), all resolved against the scheduled start), after which that night does not run;
    for a start
    request, while it still meets N-9 (the window above all). Only the open is ``busy``: an
    ``ACTIVE_OWNER`` raised later, while the night runs, is the night's own result and propagates
    (PR #95 review P3). ``start`` (tests) raises ``TickBusy`` to say the open failed."""
    from ..nightly_control import (
        MetaView,
        NightPolicy,
        night_date,
        scheduled_end,
        scheduled_start,
        standing_covers,
        start_refusal,
    )
    from ..storage.store import Scope

    path, cfg = load_config(config)
    nightly = cfg.meta.nightly if cfg.meta is not None else None
    if nightly is None:
        raise Hold("NIGHT_CONFIG", "The config has no meta.nightly: not a meta deployment")
    policy = NightPolicy.from_entry(nightly)
    moment = float(clock())
    date = night_date(moment, policy.start_at)
    meta = MetaView(_under(path, cfg.runtime_root), Scope.parse(cfg.scope), policy)
    state = meta.read([date])
    if date in state.nights:
        return {"tick": "ran", "date": date, "state": state.nights[date].get("state")}
    begin = scheduled_start(date, policy.start_at)
    reader = control_reader(path, cfg)
    controls, unreadable = None, None
    if reader is not None:
        try:
            controls = reader(date)
        except Hold as exc:
            unreadable = exc.code  # a scheduled night still starts and stops on it, recorded
    # PR #95 failure review P1: the window ends at the tightened stop too, every stop time
    # resolved against the scheduled start
    end = scheduled_end(date, policy, controls.stop_at if controls is not None else ())
    trigger: dict[str, Any]
    if begin <= moment < end:
        trigger = {"by": "schedule", "start_at": policy.start_at}
    else:
        if reader is None:
            return {"tick": "idle", "date": date}
        if controls is None:
            return {"tick": "idle", "date": date, "reason": unreadable}
        if not controls.starts:
            return {"tick": "idle", "date": date}
        why = start_refusal(now_epoch=moment, date=date, policy=policy, meta=state,
                            controls=controls)  # fmt: skip
        if why is not None:
            return {"tick": "idle", "date": date, "start_refused": why}
        first = controls.starts[0]
        trigger = {
            "by": "start_request", "control_id": first["control_id"],
            "issued_by": first["issued_by"].get("authn_context_ref"),
        }  # fmt: skip
    why = standing_covers(state.standing, policy)
    if why is not None:  # the run would hold before writing anything: say so once per tick
        return {"tick": "idle", "date": date, "reason": why}
    if start is None:

        def start(night: str, why: dict[str, Any]) -> Any:
            with ExitStack() as stack:
                try:
                    dep = stack.enter_context(opened_meta(config))
                except Hold as exc:
                    if exc.code == "ACTIVE_OWNER":
                        raise TickBusy from exc
                    raise
                return run_night(dep, night, trigger=why)

    try:
        ref = start(date, trigger)
    except TickBusy:
        return {"tick": "busy", "date": date}
    return {"tick": "started", "date": date, "trigger": trigger, "run_ref": ref}


def print_agent(*, amplai: str | None, config: Path, path_env: str | None = None) -> str:
    """The filled launchd agent (§8.9); values are XML-escaped; nothing is installed."""
    binary = amplai or shutil.which("amplai")
    if not binary:
        raise Hold("AMPLAI_PATH", "No amplai on PATH; pass --amplai")
    values = {
        "LABEL": LABEL,
        "AMPLAI": str(Path(binary).expanduser().absolute()),
        "CONFIG": str(meta_config(config)),
        "LOG_DIR": str(LOG_DIR.expanduser().absolute()),
        "PATH": path_env if path_env is not None else os.environ.get("PATH", ""),
    }
    text = TEMPLATE.read_text()
    for name in PLACEHOLDERS:
        text = text.replace("{{" + name + "}}", escape(values[name]))
    return text


def register(meta: typer.Typer, guarded: Guarded) -> None:
    group = typer.Typer(help="The nightly loop (IC-17, provisional): standing approval and runs")
    meta.add_typer(group, name="nightly")

    @group.command("approve")
    def approve_cmd(
        nights: Annotated[int, typer.Option("--nights", help="1 to 7 nights")],
        budget_trials: Annotated[int, typer.Option("--budget-trials", help="trials per night B")],
        max_tokens: Annotated[int, typer.Option("--max-tokens", help="per-plan token ceiling")],
        max_wall_seconds: Annotated[
            int, typer.Option("--max-wall-seconds", help="per-plan wall ceiling")
        ],
        config: MetaConfigOption = DEFAULT_META_CONFIG,
    ) -> None:
        """Issue the standing approval nightly.explore (human operator, nightly.approve)."""

        def call() -> Any:
            with opened_meta(config) as dep:
                return approve(
                    dep, nights=nights, budget_trials=budget_trials, max_tokens=max_tokens,
                    max_wall_seconds=max_wall_seconds,
                )  # fmt: skip

        guarded(call)

    @group.command("revoke")
    def revoke_cmd(
        approval: Annotated[
            str | None, typer.Option("--approval", help="the approval id (default: the current)")
        ] = None,
        config: MetaConfigOption = DEFAULT_META_CONFIG,
    ) -> None:
        """Revoke the standing approval; a running night stops at its next trial guard."""

        def call() -> Any:
            with opened_meta(config) as dep:
                return revoke(dep, approval)

        guarded(call)

    @group.command("run")
    def run_cmd(
        date: DateOption = None,
        dry_run: Annotated[bool, typer.Option("--dry-run", help="plan and record; no trial")] = (
            False
        ),
        per_trial_tokens: Annotated[
            int | None, typer.Option("--per-trial-tokens", help="the qualified per-trial ceiling")
        ] = None,
        basis: Annotated[str | None, typer.Option("--basis")] = None,
        evidence: Annotated[list[str] | None, typer.Option("--evidence")] = None,
        config: MetaConfigOption = DEFAULT_META_CONFIG,
    ) -> None:
        """Run one night as amplai-meta-nightly (Hold NIGHT_STOPPED without a standing approval)."""

        def call() -> Any:
            qualify = None
            if per_trial_tokens is not None:
                if basis is None or not evidence:
                    raise RuntimeFault(
                        "LOCAL_INPUT", "--per-trial-tokens needs --basis and --evidence"
                    )
                qualify = (per_trial_tokens, basis, list(evidence))
            with opened_meta(config) as dep:
                ref = run_night(dep, date or tonight(dep.config), dry_run=dry_run, qualify=qualify)
                return {"run_ref": ref}

        guarded(call)

    @group.command("tick")
    def tick_cmd(config: MetaConfigOption = DEFAULT_META_CONFIG) -> None:
        """The service manager's 5-minute tick: start tonight's night when it is due (schedule or
        a front agent's start request inside the standing approval, N-9), else end at once."""

        def call() -> Any:
            return tick(config)

        guarded(call)

    @group.command("status")
    def status_cmd(date: DateOption = None, config: MetaConfigOption = DEFAULT_META_CONFIG) -> None:
        """The night's head, the current standing approval and the controls, read-only (works
        while a night runs)."""

        def call() -> Any:
            return status_readonly(config, date)

        guarded(call)

    @group.command("print-agent")
    def print_agent_cmd(
        amplai: Annotated[
            str | None, typer.Option("--amplai", help="the amplai executable (default: on PATH)")
        ] = None,
        config: MetaConfigOption = DEFAULT_META_CONFIG,
    ) -> None:
        """Print the filled launchd agent; copy it to ~/Library/LaunchAgents/ yourself."""
        try:
            typer.echo(print_agent(amplai=amplai, config=config), nl=False)
        except RuntimeFault as exc:
            typer.echo(str(exc.as_dict()), err=True)
            raise typer.Exit(3 if exc.outcome == "hold" else 2) from exc

    @meta.command("quota")
    def quota_cmd(
        since: Annotated[
            str | None,
            typer.Option("--since", help="first pilot night YYYY-MM-DD (default: every night)"),
        ] = None,
        config: MetaConfigOption = DEFAULT_META_CONFIG,
    ) -> None:
        """Quota windows observed now, and the pilot headroom and suggested B (§8.6)."""
        from ...meta_harness.quota import KIND, QuotaObserver

        def call() -> Any:
            with opened_meta(config) as dep:
                observer = QuotaObserver(dep.store, dep.scope)
                windows = observer.observe(until=iso(float(dep.store.clock())))
                nights = sorted(
                    {
                        str(v.get("night"))
                        for _r, v in dep.store.list_objects(dep.scope, KIND)
                        if since is None or str(v.get("night")) >= since
                    }
                )
                meta = getattr(dep.config, "meta", None)
                entry = getattr(meta, "nightly", None)
                keep = entry.keep_operator_share if entry is not None else 0.5
                return {
                    "windows": [w.__dict__ for w in windows],
                    "headroom": observer.headroom(nights, keep=keep),
                    "budget_trials": entry.budget_trials if entry is not None else None,
                }

        guarded(call)


def _proposer_hooks(ops: Any) -> tuple[Any, Any]:
    """(proposer run, dreaming turn) from the S13 command helpers; (None, None) when
    ``roles.proposer`` is not configured (the search drafts nothing and dreaming is skipped)."""
    from .proposer import ensemble, proposer_cells, turn_of

    try:
        cheap, _strong = proposer_cells(ops)
    except Hold:
        return None, None

    def run(cell_id: str) -> Any:
        return ensemble(ops).run(cell_id=cell_id)

    def turn(_cell_id: str) -> Any:
        return turn_of(ops, cheap)

    return run, turn
