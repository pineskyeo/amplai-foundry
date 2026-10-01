"""``amplai meta nightly approve | revoke | run | status | print-agent`` and ``amplai meta quota``
(Work 033 S12, interfaces.md §8.6, §8.8, §8.9, §12.1; IC-17 and IC-18 provisional).

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
- ``status``: the night's head and the current standing approval.
- ``print-agent``: prints the launchd agent filled from
  ``deployment/launchd/ai.amplai.meta-nightly.plist.template``; it installs nothing.
- ``amplai meta quota``: the quota windows observed now and the pilot headroom of the nights
  recorded since ``--since`` (§8.6).
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
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
    from ...meta_harness.nightly import RUN_KIND

    out: dict[str, Any] = {"date": date, "standing_ref": None, "night": None}
    approvals = dep.meta_local.approvals
    ref = approvals.current_standing()
    if ref is not None:
        policy = approvals.standing(ref)["policy"]
        out["standing_ref"] = ref
        out["standing"] = {k: policy[k] for k in ("cells", "budget_trials", "valid_until")}
    for head_id in (f"night-{date}", f"night-{date}-dry"):
        try:
            head = dep.store.head(dep.scope, RUN_KIND, head_id)
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
        from ...meta_harness.nightly import LocalNightlyBackend, NightlyRunner

        def call() -> Any:
            with opened_meta(config) as dep:
                runner = NightlyRunner(dep, nightly_config(dep))
                backend = LocalNightlyBackend.open(dep, runner)
                backend.proposer_run, backend.dream_turn = _proposer_hooks(backend.ops)
                runner._backend = backend
                if per_trial_tokens is not None:
                    if basis is None or not evidence:
                        raise RuntimeFault(
                            "LOCAL_INPUT", "--per-trial-tokens needs --basis and --evidence"
                        )
                    backend.ops.qualify_executor(basis, list(evidence), per_trial_tokens)
                ref = runner.run(date or today(), dry_run=dry_run)
                return {"run_ref": ref}

        guarded(call)

    @group.command("status")
    def status_cmd(date: DateOption = None, config: MetaConfigOption = DEFAULT_META_CONFIG) -> None:
        """The night's head and the current standing approval."""

        def call() -> Any:
            with opened_meta(config) as dep:
                return status(dep, date or today())

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
