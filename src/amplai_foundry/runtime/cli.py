"""Two public task entrypoints; operational functions are grouped under ops."""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path
from typing import Annotated

import typer
from amplai_foundry.runtime.errors import RuntimeFault, Hold
from amplai_foundry.runtime.contracts.identity import canonical, new_id
from amplai_foundry.runtime.contracts.registry import Contracts

app = typer.Typer(name="amplai", help="AMPLAI V3 — Intent to Verified Work", no_args_is_help=True)
ops = typer.Typer(help="Explicit administration, qualification, distribution and recovery", no_args_is_help=True)
kit = typer.Typer(help="Signed, ownership-aware Kit install/update/recovery", no_args_is_help=True)
app.add_typer(ops, name="ops")
ops.add_typer(kit, name="kit")


def emit(value): typer.echo(json.dumps(value, ensure_ascii=False, indent=2))

def guarded(operation):
    try:
        result = operation()
        emit(result)
        return result
    except RuntimeFault as exc:
        emit(exc.as_dict()); raise typer.Exit(3 if exc.outcome == "hold" else 2) from exc
    except (OSError, ValueError) as exc:
        emit({"code": "LOCAL_INPUT", "message": str(exc)}); raise typer.Exit(2) from exc


def client():
    from amplai_foundry.control_plane.api_v3.client import AmplaiClient
    from .deployment import private_bytes
    token_file = os.getenv("AMPLAI_TOKEN_FILE")
    token = private_bytes(Path(token_file)).decode().strip() if token_file else os.getenv("AMPLAI_TOKEN", "")
    return AmplaiClient(os.getenv("AMPLAI_URL", "http://127.0.0.1:5083/"), token)


def submit(text, mode, targets, request_id):
    def operation():
        connection = client()
        try: return connection.submit(text, mode=mode, target_hints=targets, key=request_id or new_id("cli"))
        finally: connection.close()
    guarded(operation)


@app.command("work")
def work(text: Annotated[str, typer.Argument()], target: Annotated[list[str] | None, typer.Option("--app")] = None,
         request_id: Annotated[str | None, typer.Option("--request-id")] = None):
    """Submit intent; the runtime resolves targets and measurable completion."""
    submit(text, "work", target or [], request_id)


@app.command("design")
def design(text: Annotated[str, typer.Argument()], target: Annotated[list[str] | None, typer.Option("--app")] = None,
           request_id: Annotated[str | None, typer.Option("--request-id")] = None):
    """Design-only intent. It does not authorize implementation or deployment."""
    submit(text, "design", target or [], request_id)


@ops.command("version")
def version():
    """Report development/package status separately from the wire schema version."""
    from amplai_foundry import __version__
    emit({"package_version": __version__, "stage": "DEV-03", "schema_version": "3.0.0",
          "release_status": "development_snapshot", "production_release": False})


@ops.command("status")
def status(goal_id: str):
    def operation():
        c = client()
        try: return c.goal(goal_id)
        finally: c.close()
    guarded(operation)


@ops.command("demo")
def demo(output: Annotated[Path, typer.Option("--output")] = Path("./amplai-v3-demo")):
    """Execute actual cross-app files and independent verification, without an LLM."""
    from .reference import run_reference
    if (output / "state" / "runtime.sqlite3").exists() or output.exists() and any(output.iterdir()):
        raise typer.BadParameter("Use a new empty output directory; existing work will not be overwritten")
    result = guarded(lambda: run_reference(output.absolute()))
    if result.get("status") != "verified": raise typer.Exit(2)


@ops.command("execution-demo")
def execution_demo(output: Annotated[Path, typer.Option("--output")] = Path("./amplai-dev02-execution")):
    """Execute real parallel worker/session/snapshot flow and independent checks."""
    from .execution.reference import run_execution_reference
    if output.exists() and any(output.iterdir()):
        raise typer.BadParameter("Use a new empty output directory")
    result=guarded(lambda:run_execution_reference(output.absolute()))
    if result.get("status")!="verified":raise typer.Exit(2)


@ops.command("meta-demo")
def meta_demo(output: Annotated[Path, typer.Option("--output")] = Path("./amplai-v3-meta-demo")):
    """Run real paired arithmetic trials, canary, CAS promotion and rollback."""
    from amplai_foundry.meta_harness.reference import run_meta_reference
    if output.exists() and any(output.iterdir()): raise typer.BadParameter("Use a new empty output directory")
    result = guarded(lambda: run_meta_reference(output.absolute()))
    if result.get("verdict") != "pass": raise typer.Exit(2)


@ops.command("evolution-demo")
def evolution_demo(output: Annotated[Path, typer.Option("--output")] = Path("./amplai-dev03-evolution")):
    """Run 48 real V3 paired trials, two canaries, signed promotion and rollback."""
    from amplai_foundry.meta_harness.pipeline_reference import run_pipeline_evolution
    if output.exists() and any(output.iterdir()):
        raise typer.BadParameter("Use a new empty output directory; existing work will not be overwritten")
    result = guarded(lambda: run_pipeline_evolution(output.absolute()))
    if result.get("verdict") != "pass": raise typer.Exit(2)


@ops.command("observatory")
def observatory(composition: str | None = None, model: str | None = None,
                driver: str | None = None, repo: str | None = None,
                risk: str | None = None, since: str | None = None, until: str | None = None):
    """Read scoped metrics from the authenticated control plane; never mutate work."""
    from urllib.parse import urlencode
    def operation():
        params = {k:v for k,v in {"composition":composition,"model":model,"driver":driver,
            "repo":repo,"risk":risk,"since":since,"until":until}.items() if v is not None}
        c = client()
        try: return c.call("GET", "api/v3/metrics?" + urlencode(params))
        finally: c.close()
    guarded(operation)


@ops.command("validate")
def validate(kind: str, document: Path):
    def operation():
        value = json.loads(document.read_bytes())
        Contracts().validate(kind, value)
        return {"schema": kind, "structural_validation": "pass", "authority_or_outcome_verified": False}
    guarded(operation)


@ops.command("schemas")
def schemas(output: Annotated[Path | None, typer.Option("--output")] = None):
    def operation():
        c = Contracts()
        if output:
            output.mkdir(parents=True, exist_ok=True)
            for name, schema in c.definitions.items():
                target = output / (name + ".schema.json")
                if target.exists() and json.loads(target.read_bytes()) != schema:
                    raise Hold("SCHEMA_LOCAL_CHANGE", "Existing schema differs: " + name)
                target.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n")
        return {"version": "3.0.0", "count": len(c.definitions), "schemas": sorted(c.definitions), "output": str(output) if output else None}
    guarded(operation)


@ops.command("keygen")
def keygen(path: Path):
    """Create an owner-only Ed25519 key; this does not grant execution authority."""
    from .deployment import generate_key
    guarded(lambda: generate_key(path))


@ops.command("serve")
def serve(config: Annotated[Path, typer.Option("--config")], host: str = "127.0.0.1", port: int = 5083,
          behind_tls_proxy: Annotated[bool, typer.Option("--behind-tls-proxy")] = False):
    """Start one control-plane owner. Enroll Foundry identities and keys first."""
    if host not in {"127.0.0.1", "localhost", "::1"} and not behind_tls_proxy:
        raise typer.BadParameter("Non-loopback bind requires an explicitly configured TLS reverse proxy")
    from .deployment import RuntimeDeployment
    import uvicorn
    deployment = None
    try:
        deployment = RuntimeDeployment(config)
        uvicorn.run(deployment.app, host=host, port=port, workers=1, access_log=False, proxy_headers=behind_tls_proxy)
    except RuntimeFault as exc:
        emit(exc.as_dict()); raise typer.Exit(3) from exc
    finally:
        if deployment: deployment.close()


@ops.command("doctor")
def doctor(root: Annotated[Path | None, typer.Option("--runtime-root")] = None):
    """Read-only checks. Never increments owner epoch or creates an empty store."""
    def operation():
        if root is None:
            c = client()
            try: return c.call("GET", "api/v3/readyz")
            finally: c.close()
        from .storage.store import Store
        with Store(root, readonly=True) as store:
            result = store.conn.execute("PRAGMA integrity_check").fetchone()[0]
            return {"store_integrity": result, "owner_epoch": store.epoch, "schema_major": 3,
                    "read_only": True, "live_deployment_qualified": False,
                    "python": sys.version.split()[0], "authority_checked": False}
    guarded(operation)


def installer(root, bundle_path, trust_path):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from .storage.store import Store, Scope
    from .contracts.authority import Actor
    from amplai_foundry.distribution.packs import PackRegistry
    from amplai_foundry.distribution.installer import KitInstaller
    bundle = json.loads(bundle_path.read_bytes()) if bundle_path else None
    trust = json.loads(trust_path.read_bytes()) if trust_path else {}
    keys = {publisher: {key: Ed25519PublicKey.from_public_bytes(bytes.fromhex(raw)) for key, raw in values.items()} for publisher, values in trust.items()}
    store = Store(root / ".amplai" / "v3-installer")
    actor = Actor("local-kit-operator", Scope("local-kit", "repository"), frozenset({"pack.install"}), authn_context_ref="explicit-local-cli")
    return store, actor, KitInstaller(PackRegistry(store, Contracts(), keys)), bundle


@kit.command("plan")
def kit_plan(root: Path, bundle: Path, trust: Path, output: Path):
    def operation():
        store, actor, service, data = installer(root.absolute(), bundle, trust)
        try:
            plan = service.plan(root.absolute(), data)
            if output.exists(): raise Hold("PLAN_EXISTS", "Do not overwrite a reviewed install plan")
            output.write_bytes(canonical(plan))
            return plan
        finally: store.close()
    guarded(operation)


@kit.command("apply")
def kit_apply(root: Path, bundle: Path, trust: Path, plan: Path):
    def operation():
        store, actor, service, data = installer(root.absolute(), bundle, trust)
        try: return service.apply(actor, root.absolute(), data, json.loads(plan.read_bytes()))
        finally: store.close()
    guarded(operation)


@kit.command("recover")
def kit_recover(root: Path):
    def operation():
        store, actor, service, data = installer(root.absolute(), None, None)
        try: return service.recover(actor, root.absolute())
        finally: store.close()
    guarded(operation)


@ops.command("restore")
def restore(snapshot: Path, destination: Path):
    """Restore into an EMPTY store; restored work starts behind a kill switch."""
    from .recovery.service import RecoveryService
    guarded(lambda: RecoveryService.restore(snapshot, destination, operator_confirmed=True))


def main():
    app()


if __name__ == "__main__":
    main()
