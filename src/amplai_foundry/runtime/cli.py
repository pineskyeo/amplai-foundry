"""Two public task entrypoints; operational functions are grouped under ops."""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, TypeVar

import typer

from amplai_foundry.control_plane.api_v3.client import AmplaiClient
from amplai_foundry.runtime.contracts.identity import canonical, new_id
from amplai_foundry.runtime.contracts.registry import Contracts
from amplai_foundry.runtime.errors import Hold, RuntimeFault

T = TypeVar("T")

app = typer.Typer(name="amplai", help="AMPLAI V3 — Intent to Verified Work", no_args_is_help=True)
ops = typer.Typer(
    help="Explicit administration, qualification, distribution and recovery", no_args_is_help=True
)
kit = typer.Typer(help="Signed, ownership-aware Kit install/update/recovery", no_args_is_help=True)
app.add_typer(ops, name="ops")
ops.add_typer(kit, name="kit")


def emit(value: object) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2))


def guarded(operation: Callable[[], T]) -> T:
    try:
        result = operation()
        emit(result)
        return result
    except RuntimeFault as exc:
        emit(exc.as_dict())
        raise typer.Exit(3 if exc.outcome == "hold" else 2) from exc
    except (OSError, ValueError) as exc:
        emit({"code": "LOCAL_INPUT", "message": str(exc)})
        raise typer.Exit(2) from exc


def client() -> AmplaiClient:
    from .deployment import private_bytes

    token_file = os.getenv("AMPLAI_TOKEN_FILE")
    token = (
        private_bytes(Path(token_file)).decode().strip()
        if token_file
        else os.getenv("AMPLAI_TOKEN", "")
    )
    return AmplaiClient(os.getenv("AMPLAI_URL", "http://127.0.0.1:5083/"), token)


def submit(text: str, mode: str, targets: list[str], request_id: str | None) -> None:
    def operation() -> Any:
        connection = client()
        try:
            return connection.submit(
                text, mode=mode, target_hints=targets, key=request_id or new_id("cli")
            )
        finally:
            connection.close()

    guarded(operation)


@app.command("work")
def work(
    text: Annotated[str, typer.Argument()],
    target: Annotated[list[str] | None, typer.Option("--app")] = None,
    request_id: Annotated[str | None, typer.Option("--request-id")] = None,
    wait: Annotated[bool, typer.Option("--wait/--no-wait")] = True,
) -> None:
    """Submit a goal. On a local product server, draft its contract and show it for approval."""
    _submit_and_plan(text, "work", target, request_id, wait)


def _submit_and_plan(
    text: str, mode: str, target: list[str] | None, request_id: str | None, wait: bool
) -> None:
    connection = client()
    try:
        submitted = connection.submit(
            text, mode=mode, target_hints=target or [], key=request_id or new_id("cli")
        )
        goal_id = submitted["goal_id"]
        try:
            connection.local_plan(goal_id)
        except RuntimeFault as exc:
            if exc.code not in {"NOT_FOUND", "HTTP_ERROR"}:
                raise
            emit(submitted)  # not a local product server: submission only
            return
        typer.echo(f"goal {goal_id}: planning (read-only planner on the base commit)")
        record = connection.local_goal(goal_id)
        deadline = time.time() + 20 * 60
        while wait and record.get("status") == "planning" and time.time() < deadline:
            time.sleep(5)
            record = connection.local_goal(goal_id)
        typer.echo(render_plan(record))
    except RuntimeFault as exc:
        emit(exc.as_dict())
        raise typer.Exit(3 if exc.outcome == "hold" else 2) from exc
    finally:
        connection.close()


def render_plan(record: dict[str, Any]) -> str:
    goal, status = record.get("goal_id", "?"), record.get("status", "?")
    lines = [f"goal {goal}: {status}"]
    draft = record.get("draft") or {}
    if draft:
        lines += [
            f"  summary   : {draft.get('summary', '')}",
            f"  objective : {draft.get('objective', '')}",
        ]
        for label, key in (
            ("in scope", "in_scope"),
            ("not doing", "non_goals"),
            ("constraints", "constraints"),
        ):
            for item in draft.get(key) or []:
                lines.append(f"  {label:<10}: {item}")
        items = record.get("work_items") or []
        mapping = record.get("acceptance_map") or {}
        if len(items) > 1 and mapping:  # several apps (D-081): each app's part and acceptance
            for item in items:
                after = f" (after {', '.join(item['after'])})" if item["after"] else ""
                lines.append(f"  app       : {item['app']}{after}: {item['objective']}")
                for ac, entry in mapping.items():
                    if entry["app"] == item["app"]:
                        lines.append(f"  {ac:<10}: {entry['statement']}  [{entry['verifier']}]")
        else:
            for i, a in enumerate(draft.get("acceptance") or [], start=1):
                lines.append(f"  AC-{i:<7}: {a['statement']}  [{a['verifier']}]")
        lines.append(
            f"  risk      : {draft.get('risk', '')}   class: {draft.get('task_class', '-')}"
            f"   base: {record.get('base_commit', '')[:12]}"
        )
        for q in draft.get("questions") or []:
            lines.append(f"  QUESTION  : {q}")
    chosen = record.get("composition") or {}
    if chosen:  # selected by the system (D-079); the operator approves the plan, not a driver
        others = "; ".join(
            f"{c['driver_id']} "
            + ("eligible" if c["eligible"] else "excluded (" + ", ".join(c["reasons"]) + ")")
            for c in chosen.get("candidates", [])
            if c["driver_id"] != chosen["driver_id"]
        )
        lines.append(
            f"  driver    : {chosen['driver_id']} ({chosen['model']}), policy rank {chosen['rank']}"
            + (f"; {others}" if others else "")
        )
    check = record.get("base_check") or {}
    if check and check.get("outcome") != "pass":
        lines.append(
            "  WARNING   : the acceptance suite already fails on the base "
            f"({check.get('reason')}); approve only if the goal is to fix that"
        )
    elif check:
        lines.append("  base check: acceptance suite passes on the untouched base")
    if record.get("reason"):
        lines.append(f"  reason    : {record['reason']}")
    replan = record.get("replan") or {}
    if replan:
        why = replan["reason"][:200]
        lines.append(f"  revision  : {record.get('revision')} (replanned: {why})")
    for step in record.get("steering") or []:
        lines.append(f"  steered   : {step['text'][:200]}")
    for i, a in enumerate(record.get("attempts") or [], start=1):
        detail = ", ".join(f"{v['acceptance']}={v['outcome']}" for v in a.get("verdicts", []))
        lines.append(f"  attempt {i} : {a['outcome']} {detail} ({a.get('seconds', '?')}s)")
    publication = record.get("publication") or {}
    if publication.get("pr_url"):
        lines.append(f"  draft PR  : {publication['pr_url']}")
    elif publication.get("branch"):
        lines.append(f"  branch    : {publication['branch']}")
    elif publication.get("error"):
        lines.append(f"  publish   : {publication['error']} {publication.get('message', '')}")
    outcome = record.get("publication_outcome") or {}
    if outcome:
        edited = ", changed by a human" if outcome.get("revised") else ""
        lines.append(f"  PR state  : {outcome['state']}{edited} (checked {outcome['checked_at']})")
    nxt = {
        "awaiting_approval": f"next: amplai approve {goal}   (or amplai cancel {goal})",
        "needs_answers": "next: answer the questions in a refined `amplai work` goal",
        "approved": f"next: amplai status {goal}",
        "running": f'next: amplai status {goal}   (guide it: amplai steer {goal} "..."; '
        f'change it: amplai replan {goal} "...")',
        "replan_failed": f"next: amplai cancel {goal}   (the goal is blocked)",
    }.get(status)
    if nxt:
        lines.append(nxt)
    return "\n".join(lines)


def _local(operation: Callable[[AmplaiClient], Any], *, render: bool = True) -> None:
    connection = client()
    try:
        result = operation(connection)
        if render and isinstance(result, dict):
            typer.echo(render_plan(result))
        else:
            emit(result)
    except RuntimeFault as exc:
        emit(exc.as_dict())
        raise typer.Exit(3 if exc.outcome == "hold" else 2) from exc
    finally:
        connection.close()


@app.command("approve")
def goal_approve(goal_id: Annotated[str, typer.Argument()]) -> None:
    """Approve the drafted contract: AMPLAI may run it and publish a draft PR when verified."""
    _local(lambda c: c.local_approve(goal_id))


@app.command("status")
def goal_status(goal_id: Annotated[str | None, typer.Argument()] = None) -> None:
    """Show one goal (plan, attempts, verification, PR) or the recent goals."""
    if goal_id:
        _local(lambda c: c.local_goal(goal_id))
    else:
        _local(lambda c: c.local_goals(), render=False)


@app.command("steer")
def goal_steer(
    goal_id: Annotated[str, typer.Argument()], text: Annotated[str, typer.Argument()]
) -> None:
    """Guide the running agent: it pauses at a checkpoint and resumes with your message."""
    _local(lambda c: c.local_steer(goal_id, text), render=False)


@app.command("replan")
def goal_replan(
    goal_id: Annotated[str, typer.Argument()], reason: Annotated[str, typer.Argument()]
) -> None:
    """Change a running goal's plan: it stops, a new revision is drafted, you approve it."""
    _local(lambda c: c.local_replan(goal_id, reason), render=False)


@app.command("cancel")
def goal_cancel(goal_id: Annotated[str, typer.Argument()]) -> None:
    """Revoke the approval and stop the goal (a running container is stopped)."""
    _local(lambda c: c.local_cancel(goal_id))


@ops.command("pr-sync")
def pr_sync() -> None:
    """Read the state of every undecided draft PR now (merged / closed / changed by a human)."""
    _local(lambda c: c.local_pr_sync(), render=False)


@app.command("design")
def design(
    text: Annotated[str, typer.Argument()],
    target: Annotated[list[str] | None, typer.Option("--app")] = None,
    request_id: Annotated[str | None, typer.Option("--request-id")] = None,
    wait: Annotated[bool, typer.Option("--wait/--no-wait")] = True,
) -> None:
    """Design-only goal: a reviewed design document, never implementation or deployment."""
    _submit_and_plan(text, "design", target, request_id, wait)


@ops.command("version")
def version() -> None:
    """Report development/package status separately from the wire schema version."""
    from amplai_foundry import __version__

    emit(
        {
            "package_version": __version__,
            "stage": "DEV-03",
            "schema_version": "3.0.0",
            "release_status": "development_snapshot",
            "production_release": False,
        }
    )


@ops.command("status")
def status(goal_id: str) -> None:
    def operation() -> Any:
        c = client()
        try:
            return c.goal(goal_id)
        finally:
            c.close()

    guarded(operation)


@ops.command("demo")
def demo(output: Annotated[Path, typer.Option("--output")] = Path("./amplai-v3-demo")) -> None:
    """Execute actual cross-app files and independent verification, without an LLM."""
    from .reference import run_reference

    if (output / "state" / "runtime.sqlite3").exists() or (
        output.exists() and any(output.iterdir())
    ):
        raise typer.BadParameter(
            "Use a new empty output directory; existing work will not be overwritten"
        )
    result = guarded(lambda: run_reference(output.absolute()))
    if result.get("status") != "verified":
        raise typer.Exit(2)


@ops.command("execution-demo")
def execution_demo(
    output: Annotated[Path, typer.Option("--output")] = Path("./amplai-dev02-execution"),
) -> None:
    """Execute real parallel worker/session/snapshot flow and independent checks."""
    from .execution.reference import run_execution_reference

    if output.exists() and any(output.iterdir()):
        raise typer.BadParameter("Use a new empty output directory")
    result = guarded(lambda: run_execution_reference(output.absolute()))
    if result.get("status") != "verified":
        raise typer.Exit(2)


@ops.command("meta-demo")
def meta_demo(
    output: Annotated[Path, typer.Option("--output")] = Path("./amplai-v3-meta-demo"),
) -> None:
    """Run real paired arithmetic trials, canary, CAS promotion and rollback."""
    from amplai_foundry.meta_harness.reference import run_meta_reference

    if output.exists() and any(output.iterdir()):
        raise typer.BadParameter("Use a new empty output directory")
    result = guarded(lambda: run_meta_reference(output.absolute()))
    if result.get("verdict") != "pass":
        raise typer.Exit(2)


@ops.command("evolution-demo")
def evolution_demo(
    output: Annotated[Path, typer.Option("--output")] = Path("./amplai-dev03-evolution"),
) -> None:
    """Run 48 real V3 paired trials, two canaries, signed promotion and rollback."""
    from amplai_foundry.meta_harness.pipeline_reference import run_pipeline_evolution

    if output.exists() and any(output.iterdir()):
        raise typer.BadParameter(
            "Use a new empty output directory; existing work will not be overwritten"
        )
    result = guarded(lambda: run_pipeline_evolution(output.absolute()))
    if result.get("verdict") != "pass":
        raise typer.Exit(2)


@ops.command("observatory")
def observatory(
    composition: str | None = None,
    model: str | None = None,
    driver: str | None = None,
    repo: str | None = None,
    risk: str | None = None,
    task_class: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> None:
    """Read scoped metrics from the authenticated control plane; never mutate work."""
    from urllib.parse import urlencode

    def operation() -> Any:
        params = {
            k: v
            for k, v in {
                "composition": composition,
                "model": model,
                "driver": driver,
                "repo": repo,
                "risk": risk,
                "task_class": task_class,
                "since": since,
                "until": until,
            }.items()
            if v is not None
        }
        c = client()
        try:
            return c.call("GET", "api/v3/metrics?" + urlencode(params))
        finally:
            c.close()

    guarded(operation)


@ops.command("validate")
def validate(kind: str, document: Path) -> None:
    def operation() -> Any:
        value = json.loads(document.read_bytes())
        Contracts().validate(kind, value)
        return {
            "schema": kind,
            "structural_validation": "pass",
            "authority_or_outcome_verified": False,
        }

    guarded(operation)


@ops.command("schemas")
def schemas(output: Annotated[Path | None, typer.Option("--output")] = None) -> None:
    def operation() -> Any:
        c = Contracts()
        if output:
            output.mkdir(parents=True, exist_ok=True)
            for name, schema in c.definitions.items():
                target = output / (name + ".schema.json")
                if target.exists() and json.loads(target.read_bytes()) != schema:
                    raise Hold("SCHEMA_LOCAL_CHANGE", "Existing schema differs: " + name)
                target.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n")
        return {
            "version": "3.0.0",
            "count": len(c.definitions),
            "schemas": sorted(c.definitions),
            "output": str(output) if output else None,
        }

    guarded(operation)


@ops.command("keygen")
def keygen(path: Path) -> None:
    """Create an owner-only Ed25519 key; this does not grant execution authority."""
    from .deployment import generate_key

    guarded(lambda: generate_key(path))


@ops.command("serve")
def serve(
    config: Annotated[Path, typer.Option("--config")],
    host: str = "127.0.0.1",
    port: int = 5083,
    behind_tls_proxy: Annotated[bool, typer.Option("--behind-tls-proxy")] = False,
) -> None:
    """Start one control-plane owner. Enroll Foundry identities and keys first."""
    if host not in {"127.0.0.1", "localhost", "::1"} and not behind_tls_proxy:
        raise typer.BadParameter(
            "Non-loopback bind requires an explicitly configured TLS reverse proxy"
        )
    import uvicorn

    from .deployment import RuntimeDeployment

    deployment = None
    try:
        deployment = RuntimeDeployment(config)
        uvicorn.run(
            deployment.app,
            host=host,
            port=port,
            workers=1,
            access_log=False,
            proxy_headers=behind_tls_proxy,
        )
    except RuntimeFault as exc:
        emit(exc.as_dict())
        raise typer.Exit(3) from exc
    finally:
        if deployment:
            deployment.close()


@ops.command("local-init")
def local_init(
    repo: Annotated[Path, typer.Option("--repo")],
    app_id: Annotated[str, typer.Option("--app")],
    codex_home: Annotated[Path, typer.Option("--codex-home")],
    container_profile: Annotated[Path, typer.Option("--container-profile")],
    qualification_report: Annotated[Path, typer.Option("--qualification-report")],
    verifier: Annotated[list[str], typer.Option("--verifier", help="id=command ... | description")],
    home: Annotated[Path, typer.Option("--home")] = Path("~/.amplai/local"),
    egress_profile: Annotated[Path, typer.Option("--egress-profile")] = Path(
        "deployment/local-egress.json"
    ),
    egress_qualification: Annotated[Path, typer.Option("--egress-qualification")] = Path(
        "deployment/local-egress-qualification.json"
    ),
    base_branch: Annotated[str, typer.Option("--base-branch")] = "main",
    operator: Annotated[str, typer.Option("--operator")] = os.getenv("USER", "operator"),
) -> None:
    """Create keys, the operator token (0600) and the local product configuration."""

    def operation() -> Any:
        import secrets
        import shlex

        from .deployment import generate_key

        root = home.expanduser().absolute()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        config = root / "local.json"
        if config.exists():
            raise Hold("CONFIG_EXISTS", "A local configuration already exists; not overwriting")
        generate_key(root / "authority.pem")
        generate_key(root / "verifier.pem")
        token = root / "operator.token"
        fd = os.open(token, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as out:
            out.write(secrets.token_urlsafe(48))
        verifiers = []
        for item in verifier:
            head, _, description = item.partition("|")
            vid, _, command = head.partition("=")
            if not vid.strip() or not command.strip():
                raise Hold("VERIFIER_SPEC", "Use --verifier 'id=command | description'")
            verifiers.append(
                {"id": vid.strip(), "argv": shlex.split(command),
                 "description": description.strip() or command.strip()}
            )  # fmt: skip
        value = {
            "schema_version": "local-1",
            "runtime_root": str(root / "runtime"),
            "workspace_root": str(root / "workspaces"),
            "scope": {"tenant_id": "local", "project_id": app_id},
            "operator_subject": operator,
            "operator_token_file": str(token),
            "signing_key_file": str(root / "authority.pem"),
            "verifier_key_file": str(root / "verifier.pem"),
            "codex": {
                "credential_home": str(codex_home.expanduser().absolute()),
                "egress_profile": str(egress_profile.absolute()),
                "egress_qualification": str(egress_qualification.absolute()),
            },
            "apps": [
                {
                    "app_id": app_id,
                    "repo": str(repo.expanduser().absolute()),
                    "container_profile": str(container_profile.absolute()),
                    "qualification_report": str(qualification_report.absolute()),
                    "verifiers": verifiers,
                    "base_branch": base_branch,
                }
            ],
        }
        fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as out:
            out.write(json.dumps(value, indent=2))
        return {
            "config": str(config),
            "token_file": str(token),
            "next": f"amplai ops local-serve --config {config}  |  "
            f"export AMPLAI_TOKEN_FILE={token}",
        }

    guarded(operation)


def _edit_local_config(config: Path, change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """Change local.json atomically (0600), validated before it replaces the old file."""
    from .deployment import private_bytes
    from .local_deployment import LocalConfig

    path = config.expanduser().absolute()
    value: dict[str, Any] = json.loads(private_bytes(path))
    change(value)
    LocalConfig.model_validate(value)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as out:
        out.write(json.dumps(value, indent=2))
    os.replace(tmp, path)
    return value


@ops.command("local-claude")
def local_claude(
    token_file: Annotated[Path, typer.Option("--token-file")],
    qualification_report: Annotated[Path, typer.Option("--qualification-report")],
    app_id: Annotated[str | None, typer.Option("--app")] = None,
    model: Annotated[str, typer.Option("--model")] = "claude-sonnet-5",
    config: Annotated[Path, typer.Option("--config")] = Path("~/.amplai/local/local.json"),
) -> None:
    """Add Claude CLI as a candidate composition (Codex stays first; the system selects).

    The operator creates the 0600 token file (CLAUDE_CODE_OAUTH_TOKEN=...); it is referenced,
    never copied. The report must be a passing qualification in the app's image.
    """

    def change(value: dict[str, Any]) -> None:
        value["claude"] = {
            "token_file": str(token_file.expanduser().absolute()),
            "model": model,
            "enabled": True,
        }
        for app in value["apps"]:
            if app_id in (None, app["app_id"]):
                app["claude_qualification_report"] = str(qualification_report.absolute())

    guarded(lambda: {"config": str(_edit_local_config(config, change) and config),
                     "next": "restart amplai ops local-serve"})  # fmt: skip


@ops.command("local-add-app")
def local_add_app(
    repo: Annotated[Path, typer.Option("--repo")],
    app_id: Annotated[str, typer.Option("--app")],
    container_profile: Annotated[Path, typer.Option("--container-profile")],
    qualification_report: Annotated[Path, typer.Option("--qualification-report")],
    verifier: Annotated[list[str], typer.Option("--verifier", help="id=command ... | description")],
    claude_qualification_report: Annotated[
        Path | None, typer.Option("--claude-qualification-report")
    ] = None,
    base_branch: Annotated[str, typer.Option("--base-branch")] = "main",
    config: Annotated[Path, typer.Option("--config")] = Path("~/.amplai/local/local.json"),
) -> None:
    """Add another app to the local product (a goal may then target several apps)."""
    verifiers = _verifier_specs(verifier)

    def change(value: dict[str, Any]) -> None:
        if any(a["app_id"] == app_id for a in value["apps"]):
            raise Hold("APP_EXISTS", "The app is already configured")
        value["apps"].append(
            {
                "app_id": app_id,
                "repo": str(repo.expanduser().absolute()),
                "container_profile": str(container_profile.absolute()),
                "qualification_report": str(qualification_report.absolute()),
                **(
                    {"claude_qualification_report": str(claude_qualification_report.absolute())}
                    if claude_qualification_report
                    else {}
                ),
                "verifiers": verifiers,
                "base_branch": base_branch,
            }
        )

    guarded(lambda: {"config": str(_edit_local_config(config, change) and config),
                     "next": "restart amplai ops local-serve"})  # fmt: skip


def _verifier_specs(items: list[str]) -> list[dict[str, Any]]:
    """--verifier 'id=command ... | description' values as config verifier entries."""
    import shlex

    verifiers = []
    for item in items:
        head, _, description = item.partition("|")
        vid, _, command = head.partition("=")
        if not vid.strip() or not command.strip():
            raise typer.BadParameter("Use --verifier 'id=command | description'")
        verifiers.append(
            {"id": vid.strip(), "argv": shlex.split(command),
             "description": description.strip() or command.strip()}
        )  # fmt: skip
    return verifiers


@ops.command("local-verifier")
def local_verifier(
    app_id: Annotated[str, typer.Option("--app")],
    verifier: Annotated[
        list[str] | None, typer.Option("--verifier", help="id=command ... | description")
    ] = None,
    remove: Annotated[list[str] | None, typer.Option("--remove", help="verifier id")] = None,
    config: Annotated[Path, typer.Option("--config")] = Path("~/.amplai/local/local.json"),
) -> None:
    """Add, replace (same id, same place) or remove an app's acceptance verifier commands.

    New goals plan against the app's verifiers; goals planned earlier keep their contract.
    """
    specs = _verifier_specs(verifier or [])
    dropped = set(remove or [])
    if not specs and not dropped:
        raise typer.BadParameter("Give --verifier and/or --remove")

    def change(value: dict[str, Any]) -> None:
        app = next((a for a in value["apps"] if a["app_id"] == app_id), None)
        if app is None:
            raise Hold("APP_UNKNOWN", "The app is not configured")
        current = [v for v in app["verifiers"] if v["id"] not in dropped]
        for spec in specs:
            at = next((i for i, v in enumerate(current) if v["id"] == spec["id"]), None)
            if at is None:
                current.append(spec)
            else:
                current[at] = spec
        if not current:
            raise Hold("VERIFIERS_EMPTY", "An app keeps at least one verifier")
        app["verifiers"] = current

    guarded(lambda: {"config": str(_edit_local_config(config, change) and config),
                     "app": app_id, "next": "restart amplai ops local-serve"})  # fmt: skip


@ops.command("local-integration")
def local_integration(
    integration_id: Annotated[str, typer.Option("--id")],
    apps: Annotated[list[str], typer.Option("--app")],
    command: Annotated[str, typer.Option("--command")],
    description: Annotated[str, typer.Option("--description")],
    timeout_seconds: Annotated[int, typer.Option("--timeout")] = 900,
    config: Annotated[Path, typer.Option("--config")] = Path("~/.amplai/local/local.json"),
) -> None:
    """Add a cross-app integration command (apps are read-only at /amplai-input/apps/<app>)."""
    import shlex

    def change(value: dict[str, Any]) -> None:
        known = {a["app_id"] for a in value["apps"]}
        if not set(apps) <= known or len(set(apps)) < 2:
            raise Hold("INTEGRATION_APPS", "Name at least two configured apps")
        value.setdefault("integrations", [])
        value["integrations"] = [i for i in value["integrations"] if i["id"] != integration_id]
        value["integrations"].append(
            {"id": integration_id, "apps": list(dict.fromkeys(apps)), "argv": shlex.split(command),
             "description": description, "timeout_seconds": timeout_seconds}
        )  # fmt: skip

    guarded(lambda: {"config": str(_edit_local_config(config, change) and config),
                     "next": "restart amplai ops local-serve"})  # fmt: skip


@ops.command("local-driver")
def local_driver(
    driver: Annotated[str, typer.Argument(help="codex or claude")],
    enabled: Annotated[bool, typer.Option("--enable/--disable")] = True,
    config: Annotated[Path, typer.Option("--config")] = Path("~/.amplai/local/local.json"),
) -> None:
    """Make a driver eligible or not for selection (a disabled model is filtered out)."""

    def change(value: dict[str, Any]) -> None:
        if driver not in {"codex", "claude"} or not value.get(driver):
            raise Hold("DRIVER_UNKNOWN", "Configure the driver before switching it")
        value[driver]["enabled"] = enabled

    guarded(lambda: {"config": str(_edit_local_config(config, change) and config),
                     "driver": driver, "enabled": enabled,
                     "next": "restart amplai ops local-serve"})  # fmt: skip


@ops.command("local-serve")
def local_serve(
    config: Annotated[Path, typer.Option("--config")] = Path("~/.amplai/local/local.json"),
    port: int = 5083,
) -> None:
    """Start the local single-operator product (API + execution loop), loopback only."""
    import uvicorn

    from .local_deployment import LocalProductDeployment

    deployment = None
    try:
        deployment = LocalProductDeployment(config)
        uvicorn.run(deployment.app, host="127.0.0.1", port=port, workers=1, access_log=False)
    except RuntimeFault as exc:
        emit(exc.as_dict())
        raise typer.Exit(3) from exc
    finally:
        if deployment:
            deployment.close()


@ops.command("doctor")
def doctor(root: Annotated[Path | None, typer.Option("--runtime-root")] = None) -> None:
    """Read-only checks. Never increments owner epoch or creates an empty store."""

    def operation() -> Any:
        if root is None:
            c = client()
            try:
                return c.call("GET", "api/v3/readyz")
            finally:
                c.close()
        from .storage.store import Store

        with Store(root, readonly=True) as store:
            result = store.conn.execute("PRAGMA integrity_check").fetchone()[0]
            return {
                "store_integrity": result,
                "owner_epoch": store.epoch,
                "schema_major": 3,
                "read_only": True,
                "live_deployment_qualified": False,
                "python": sys.version.split()[0],
                "authority_checked": False,
            }

    guarded(operation)


def installer(
    root: Path, bundle_path: Path | None, trust_path: Path | None
) -> tuple[Any, Any, Any, Any]:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    from amplai_foundry.distribution.installer import KitInstaller
    from amplai_foundry.distribution.packs import PackRegistry

    from .contracts.authority import Actor
    from .storage.store import Scope, Store

    bundle = json.loads(bundle_path.read_bytes()) if bundle_path else None
    trust = json.loads(trust_path.read_bytes()) if trust_path else {}
    keys = {
        publisher: {
            key: Ed25519PublicKey.from_public_bytes(bytes.fromhex(raw))
            for key, raw in values.items()
        }
        for publisher, values in trust.items()
    }
    store = Store(root / ".amplai" / "v3-installer")
    actor = Actor(
        "local-kit-operator",
        Scope("local-kit", "repository"),
        frozenset({"pack.install"}),
        authn_context_ref="explicit-local-cli",
    )
    return store, actor, KitInstaller(PackRegistry(store, Contracts(), keys)), bundle


@kit.command("plan")
def kit_plan(root: Path, bundle: Path, trust: Path, output: Path) -> None:
    def operation() -> Any:
        store, _actor, service, data = installer(root.absolute(), bundle, trust)
        try:
            plan = service.plan(root.absolute(), data)
            if output.exists():
                raise Hold("PLAN_EXISTS", "Do not overwrite a reviewed install plan")
            output.write_bytes(canonical(plan))
            return plan
        finally:
            store.close()

    guarded(operation)


@kit.command("apply")
def kit_apply(root: Path, bundle: Path, trust: Path, plan: Path) -> None:
    def operation() -> Any:
        store, actor, service, data = installer(root.absolute(), bundle, trust)
        try:
            return service.apply(actor, root.absolute(), data, json.loads(plan.read_bytes()))
        finally:
            store.close()

    guarded(operation)


@kit.command("recover")
def kit_recover(root: Path) -> None:
    def operation() -> Any:
        store, actor, service, _data = installer(root.absolute(), None, None)
        try:
            return service.recover(actor, root.absolute())
        finally:
            store.close()

    guarded(operation)


@ops.command("restore")
def restore(snapshot: Path, destination: Path) -> None:
    """Restore into an EMPTY store; restored work starts behind a kill switch."""
    from .recovery.service import RecoveryService

    guarded(lambda: RecoveryService.restore(snapshot, destination, operator_confirmed=True))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
