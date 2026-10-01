"""``amplai meta evaluator status | requalify | quality | propose-change | qualify-change |
approve-change | reject-change`` (Work 033 S14, interfaces.md §7.8, §11.1, §12.1, D-105).

``status`` shows the running evaluator code, every stored ``evaluator-version`` (S2,
``evaluation/versions.py``) and the evaluator changes. ``requalify`` re-analyzes every stored
report with the running evaluator and writes the ``evaluator-requalification`` record: with
``--runtime-root`` it opens that store itself (``scripts/evaluator_requalify.py``, every scope;
stop the deployment first), otherwise it works on the deployment of ``--config``. ``quality``
measures the §11.1 metrics of one evaluator version (``evaluation/quality.py``). The change
lifecycle (``propose-change``, ``qualify-change``, ``approve-change``, ``reject-change``) and
``requalify`` run as the human operator that holds ``evaluator.approve`` (IC-26, provisional); a
proposer, a service identity or a human without that permission is refused by the library.
``qualify-change`` runs the §7.8 Q-suite in a subprocess (IC-25, provisional; minutes long).
``status`` shows a Q-02 over zero stored reports as ``vacuous``. None of these commands opens a
corpus, runs a trial or touches a candidate experiment.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from ...evaluation import versions
from ...evaluation.quality import QualityService, load_requalifier
from ..contracts.authority import Actor
from ..errors import RuntimeFault
from ..storage.store import Scope, Store
from . import DEFAULT_CONFIG, ConfigOption, Guarded, opened_deployment

EPOCH = "1970-01-01T00:00:00Z"


def _read_json(path: Path, what: str) -> Any:
    try:
        return json.loads(Path(path).expanduser().read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeFault("LOCAL_INPUT", f"{what} is not readable JSON: {exc}") from exc


def _offline_operator(scope: Scope) -> Actor:
    """The local human operator of a store opened by ``--runtime-root`` (no deployment, hence no
    configured identity): the operator permission sets of the local deployment, never a proposer.
    The library applies the same IC-26 check to it as to ``dep.meta_operator()``."""
    from ..execution.meta_local import META_OPERATOR_PERMISSIONS
    from ..local_deployment import OPERATOR_PERMISSIONS

    return Actor(
        "local-operator", scope, OPERATOR_PERMISSIONS | META_OPERATOR_PERMISSIONS, "human",
        "local-operator-token",
    )  # fmt: skip


def _requalify_root(root: Path, evaluator_version: str) -> dict[str, Any]:
    """``requalify`` over every scope with a stored report of the store at ``root`` (the
    deployment must be stopped), each as the local operator through ``QualityService.requalify``."""
    if not (root / "runtime.sqlite3").is_file():
        raise RuntimeFault("NOT_FOUND", "No runtime store at this runtime root")
    script = load_requalifier()
    rows = []
    with Store(root) as store:
        for scope in script.scopes_with_reports(store):
            result = QualityService(store, scope, requalifier=script).requalify(
                _offline_operator(scope), evaluator_version
            )
            rows.append({"scope": scope.wire(), **result})
    return {"evaluator_version": evaluator_version, "scopes": rows}


def register(meta: typer.Typer, guarded: Guarded) -> None:
    group = typer.Typer(
        help="Evaluator versions, requalification, quality metrics and evaluator changes"
    )
    meta.add_typer(group, name="evaluator")

    @group.command("status")
    def status(config: ConfigOption = DEFAULT_CONFIG) -> None:
        """The running evaluator code, the stored versions (``requalification_vacuous``: Q-02 over
        zero stored reports), the evaluator changes (``qualification_scope.vacuous``) and the
        provisional ``task_class_to_domain`` table (IC-27)."""

        def call() -> Any:
            with opened_deployment(config) as dep:
                return QualityService(dep.store, dep.scope).status()

        guarded(call)

    @group.command("requalify")
    def requalify(
        runtime_root: Annotated[
            Path | None,
            typer.Option(
                "--runtime-root",
                help="the runtime root of a local store (the deployment must be stopped); "
                "default: the deployment of --config",
            ),
        ] = None,
        evaluator_version: Annotated[
            str, typer.Option("--evaluator-version", help="the version being qualified")
        ] = versions.VERSION,
        config: ConfigOption = DEFAULT_CONFIG,
    ) -> None:
        """Re-analyze every stored eval-report with the running evaluator (Q-02, G5); needs the
        operator's ``evaluator.approve`` (IC-26). A scope without a stored report is ``vacuous``."""

        def call() -> Any:
            versions.version_id(evaluator_version)
            if runtime_root is not None:
                return _requalify_root(
                    Path(runtime_root).expanduser().absolute(), evaluator_version
                )
            with opened_deployment(config) as dep:
                return QualityService(dep.store, dep.scope).requalify(
                    dep.meta_operator(), evaluator_version
                )

        guarded(call)

    @group.command("quality")
    def quality(
        since: Annotated[
            str, typer.Option("--since", help="ISO-8601 timestamp with a timezone")
        ] = EPOCH,
        evaluator_version: Annotated[
            str | None,
            typer.Option("--evaluator-version", help="default: the newest stored version"),
        ] = None,
        corpus_check: Annotated[
            Path | None,
            typer.Option(
                "--corpus-check",
                help="the JSON output of `amplai meta corpus check --repeats 3` (grader flakiness)",
            ),
        ] = None,
        negative_controls: Annotated[
            Path | None,
            typer.Option(
                "--negative-controls",
                help="JSON {cell id: composition ref} of each cell's negative control",
            ),
        ] = None,
        config: ConfigOption = DEFAULT_CONFIG,
    ) -> None:
        """Measure the §11.1 evaluation-quality metrics of one evaluator version."""

        def call() -> Any:
            check = _read_json(corpus_check, "--corpus-check") if corpus_check else None
            controls = (
                _read_json(negative_controls, "--negative-controls") if negative_controls else None
            )
            if controls is not None and not isinstance(controls, dict):
                raise RuntimeFault("LOCAL_INPUT", "--negative-controls is a JSON object")
            with opened_deployment(config) as dep:
                service = QualityService(dep.store, dep.scope)
                ref = service.measure(
                    service.version_ref(evaluator_version),
                    since=since,
                    negative_controls=controls,
                    corpus_check=check,
                )
                return {
                    "quality_ref": ref,
                    "metrics": dep.store.get(dep.scope, "evaluation-quality", ref)["metrics"],
                }

        guarded(call)

    @group.command("propose-change")
    def propose_change(
        to_file: Annotated[
            Path,
            typer.Option(
                "--to-file",
                help='JSON {"version": "eval-N", "changes": [{"kind", "detail"}], '
                '"corpus_ref"?, "stage_templates"?}',
            ),
        ],
        reason: Annotated[str, typer.Option("--reason")],
        config: ConfigOption = DEFAULT_CONFIG,
    ) -> None:
        """Open an evaluator change (operator only)."""

        def call() -> Any:
            target = _read_json(to_file, "--to-file")
            with opened_deployment(config) as dep:
                service = QualityService(dep.store, dep.scope)
                change_id = service.propose_change(dep.meta_operator(), to=target, reason=reason)
                return service.change(change_id)

        guarded(call)

    @group.command("qualify-change")
    def qualify_change(change_id: str, config: ConfigOption = DEFAULT_CONFIG) -> None:
        """Qualify the change alone (IC-25): run the §7.8 Q-suite in a subprocess of this
        interpreter (minutes), re-run the Q-02 requalification and measure the quality of the
        replaced version. Qualified only when every suite file ran with zero failures and errors
        and Q-02 is all_equal. Refused while a candidate experiment, calibration or stage is
        running."""

        def call() -> Any:
            with opened_deployment(config) as dep:
                service = QualityService(dep.store, dep.scope)
                ref = service.qualify_change(dep.meta_operator(), change_id)
                return {"requalification_ref": ref, "change": service.change(change_id)}

        guarded(call)

    @group.command("approve-change")
    def approve_change(change_id: str, config: ConfigOption = DEFAULT_CONFIG) -> None:
        """Approve a qualified change: writes the new evaluator-version (human operator only)."""

        def call() -> Any:
            with opened_deployment(config) as dep:
                service = QualityService(dep.store, dep.scope)
                ref = service.approve_change(dep.meta_operator(), change_id)
                return {"evaluator_version_ref": ref, "change": service.change(change_id)}

        guarded(call)

    @group.command("reject-change")
    def reject_change(
        change_id: str,
        reason: Annotated[str, typer.Option("--reason")],
        config: ConfigOption = DEFAULT_CONFIG,
    ) -> None:
        """Close an open change without a new version (human operator only)."""

        def call() -> Any:
            with opened_deployment(config) as dep:
                service = QualityService(dep.store, dep.scope)
                service.reject_change(dep.meta_operator(), change_id, reason)
                return service.change(change_id)

        guarded(call)
