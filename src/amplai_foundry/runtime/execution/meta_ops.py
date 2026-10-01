"""The operator's meta-harness gates on the local product (Work 030 S6, D-089).

One method per gate, each an explicit operator command. The meta-proposer identity only proposes;
everything else runs as the human operator, and every approval is issued to the exact subject it
covers (``LocalMetaApprovals``). Nothing here decides for the operator: a gate that is not commanded
does not happen, and a candidate that does not pass stays where it stopped.

The gates in order: ``propose`` (proposer) → ``screen`` → ``approve_experiment`` →
``run_experiment`` → ``approve_canary`` → ``run_canary`` → ``promote`` → ``rollback``.
``reject`` and ``abort`` end a candidate; ``status`` reads.

Work 033 S11 (interfaces.md §3.9, §8.1, §12.1): ``propose_components`` submits a component
candidate (the prompt-only ``propose`` delegates to it); ``review`` records the operator's class-B
code review or rejects; ``search`` plans the proposal's stages and runs the ones without an
operator gate; ``approve_stage`` is the focused and holdout gate; ``calibrate`` freezes, approves
and runs a calibration; ``derived_proposal`` creates an IC-19 (A) leave-one-out proposal.
``promote`` derives every composition of the app for a candidate that changes the router.
``check_corpus`` holds ``CORPUS_CHANGED`` when the corpus v2 loaded now is not the frozen one
(stage plans, stage freezes and calibration call it); the app of a corpus v2 is ``app_id``
(``--app``) when given, else the one installed app its ``main`` set names (several: Hold
TARGET_UNKNOWN, name one with ``--app``). ``reconcile`` (IC-18, provisional) is the human
operator's path for an interrupted stage experiment or an unresolved allocation of a root.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from ...evaluation.corpus import CorpusService
from ...evaluation.service import ExecutorPolicy
from ...meta_harness import local_corpus
from ...meta_harness.corpus_v2 import CorpusV2
from ...meta_harness.local_canary import canary_execute, canary_policy
from ...meta_harness.local_executor import LocalTrialExecutor, corpus_cases
from ..contracts.identity import canonical, digest, digest_bytes, new_id, now
from ..contracts.semantics import resolve_ref
from ..errors import Hold, RuntimeFault
from ..local_deployment import LocalProductDeployment
from . import prompts, releases
from .meta_local import EXECUTOR_ID, RELEASE_KEY_ID

if TYPE_CHECKING:
    from ...meta_harness.components import ComponentService
    from ...meta_harness.manifest import ManifestService
    from ...meta_harness.stages import ApprovalIssuer, StageRunner
    from .product import InstalledApp

EXPERIMENT_MODE = "sandbox_rerun"
EXPERIMENT_SPLIT = "validation"
PREDICTION_KIND = "proposal-prediction"
PREDICTION_FIELDS = frozenset(
    {"improve_task_ids", "regress_task_ids", "improve_buckets", "regress_buckets",
     "expected_delta", "risk"}
)  # fmt: skip
# IC-16: the stage template's root budget admits 10 stage experiments per proposal
STAGE_MAX_ATTEMPTS = 10
# the corpus v2 set stages and calibration run on (§10.6: the regression set is its own corpus)
MAIN_SET = "main"
REGRESSION_SET = "regression"
# the role prompt slot holds a prompt bundle (the carrier is the content, §2.2)
PROMPT_SLOT = "prompt_bundle_ref"
PROMPT_SOURCE = "meta-harness proposal"


class LocalMetaOps:
    def __init__(
        self,
        dep: LocalProductDeployment,
        corpus: local_corpus.Corpus | CorpusV2,
        executor: LocalTrialExecutor,
        *,
        driver: str,
        app_id: str | None = None,
    ) -> None:
        """``corpus`` is the Work 030 corpus (one app) or a corpus v2, whose app is ``app_id``
        or the one installed app its ``main``-set ``app``-environment tasks name (the set
        ``frozen_corpus`` serves; the regression set names the Work 030 demo app)."""
        self.dep, self.corpus, self.executor, self.driver = dep, corpus, executor, driver
        self.store, self.scope = dep.store, dep.scope
        self.local = dep.meta_local
        self.operator = dep.meta_operator()
        self._app_id = app_id
        self._app: InstalledApp | None = None
        self._manifests: ManifestService | None = None
        if isinstance(corpus, local_corpus.Corpus) and app_id is None:
            self._app = dep.service.apps[corpus.app_id]
        service = dep.service
        # §7.4: the reference validator pins against the compositions this product installed
        self.local.installed_compositions = lambda: [
            dict(app.compositions) for app in service.apps.values()
        ]
        # IC-29 (provisional): opened fresh (no qualification pinned in this process), the
        # newest human operator's executor-qualification of the cell is pinned; none -> as today
        if self.local.evaluation.executor_policy is None:
            self.restore_qualification()

    # -- helpers -------------------------------------------------------------------------------
    @property
    def app(self) -> InstalledApp:
        if self._app is None:
            self._app = self.dep.service.apps[self._resolve_app_id()]
        return self._app

    def _resolve_app_id(self) -> str:
        """The app: ``app_id`` (``--app``) when given, which must be installed and, for a corpus
        v2, named by a main-set app-environment task's base; else the one installed app those
        bases name. Hold TARGET_UNKNOWN for none and, explicitly, for several (``--app``)."""
        corpus = self.corpus
        # stages and calibration run on the main set (``frozen_corpus`` resolves
        # ``taskindex-<corpus_id>``; the regression set freezes as ``amplai-regression-v1``,
        # §10.6), so the regression tasks' demo app does not compete with the main set's app
        named = (
            {
                str(corpus.bases.get(t.base_id, {}).get("app_id"))
                for t in corpus.tasks
                if t.set == MAIN_SET and t.environment_id == "app"
            }
            if isinstance(corpus, CorpusV2)
            else set()
        )
        if self._app_id is not None:
            if self._app_id not in self.dep.service.apps:
                raise Hold("TARGET_UNKNOWN", "Not an installed app", details=[self._app_id])
            if isinstance(corpus, CorpusV2) and self._app_id not in named:
                raise Hold(
                    "TARGET_UNKNOWN",
                    "No main-set app-environment task of the corpus names this app",
                    details={"app": self._app_id, "named": sorted(named)},
                )
            return self._app_id
        if isinstance(corpus, local_corpus.Corpus):
            return corpus.app_id
        installed = sorted(named & set(self.dep.service.apps))
        if len(installed) > 1:
            raise Hold(
                "TARGET_UNKNOWN",
                "The corpus's main set names several installed apps; name one with --app",
                details={"installed": installed, "named": sorted(named)},
            )
        if not installed:
            raise Hold(
                "TARGET_UNKNOWN",
                "The corpus names no installed app for its main-set app-environment tasks",
                details={"installed": installed, "named": sorted(named)},
            )
        return installed[0]

    def app_case_ids(self) -> frozenset[str] | None:
        """The case ids the stages may select (``--app`` selects the app): the main-set tasks of
        the loaded corpus v2 whose base names ``self.app``. None for the Work 030 corpus (one
        app: every case). A main set naming two apps keeps the other app's tasks out, so a stage
        trial never runs another app's task under this app's cell (the executor takes the app
        from the task's base, ``LocalTrialExecutor._spec``). Calibration still pins every
        development and validation case (``CalibrationService._cases``, §8.2)."""
        corpus = self.corpus
        if not isinstance(corpus, CorpusV2):
            return None
        app_id = self.app.config.app_id
        return frozenset(
            t.task_id
            for t in corpus.tasks
            if t.set == MAIN_SET and corpus.bases.get(t.base_id, {}).get("app_id") == app_id
        )

    @property
    def components(self) -> ComponentService:
        from ...meta_harness.components import ComponentService

        return ComponentService(self.store, self.scope)

    @property
    def manifests(self) -> ManifestService:
        from ...meta_harness.manifest import ManifestService

        if self._manifests is None:
            self._manifests = ManifestService(
                self.store, self.scope, self.dep.contracts, self.components
            )
        return self._manifests

    def _put(self, kind: str, object_id: str, value: dict[str, Any]) -> dict[str, Any]:
        return self.dep.service._put(kind, object_id, value)

    def _head(self, proposal_id: str) -> dict[str, Any]:
        return dict(self.store.head(self.scope, "evolution", proposal_id))

    def _proposal(self, proposal_id: str) -> dict[str, Any]:
        return self.store.get(
            self.scope, "harness-change-proposal", self._head(proposal_id)["data"]["proposal_ref"]
        )

    def _active_release(self) -> dict[str, Any]:
        head = self.store.head(self.scope, releases.POINTER_KIND, releases.POINTER_ID)
        ref: dict[str, Any] = head["data"]["release_ref"]
        return ref

    def _observation(self, issue: str) -> dict[str, Any]:
        """A ``harness-observation`` record whose artifact states the issue (verifier trust)."""
        observed = self.dep.artifacts.admit(
            self.scope, canonical({"issue": issue}), "application/json", trust="verifier"
        )
        observation_id = new_id("observation")
        return self._put(
            "harness-observation",
            observation_id,
            {"observation_id": observation_id, "artifact": observed},
        )

    def _not_derived(self, proposal_id: str) -> None:
        """IC-19 (A): a derived proposal is never screened nor approved for an experiment."""
        from ...meta_harness.stages import is_derived

        if is_derived(self.store, self.scope, self._proposal(proposal_id)):
            raise Hold(
                "DERIVED_PROPOSAL",
                "A derived ablation proposal is never screened or approved (IC-19 A); "
                "re-propose the variant as an ordinary proposal",
            )

    def _removal_gate(self, proposal_id: str) -> None:
        """IC-24 (provisional): Hold NOT_A_REMOVAL for a removal-sweep proposal whose focused
        stage does not show a removal (``stages.check_removal``); other proposals pass."""
        from ...meta_harness.stages import check_removal

        check_removal(self.store, self.scope, self.dep.artifacts, proposal_id)

    # -- 1. propose (the meta-proposer identity) -----------------------------------------------
    def propose(
        self,
        *,
        suffix: str,
        implementer_lines: list[str],
        hypothesis: str,
        expected_benefit: str,
        observation: str,
        risks: list[str],
    ) -> str:
        """A class-A change of the IMPLEMENTER prompt, submitted as the meta-proposer: the role
        lines become a ``role_prompt`` component and ``propose_components`` submits it (§12.1)."""
        component = self.components.register(
            self.local.proposer,
            component_id="role_prompt." + suffix,
            kind="role_prompt",
            content={"implementer": list(implementer_lines)},
            source="proposer",
            rationale=hypothesis[:4000] or "implementer prompt candidate",
        )
        return self.propose_components(
            cell_id=self.driver,
            changes={PROMPT_SLOT: component},
            suffix=suffix,
            hypothesis=hypothesis,
            expected_benefit=expected_benefit,
            risks=risks,
            observation_refs=[self._observation(observation)],
            prediction=None,
            baseline_ref=self.app.compositions[self.driver],
        )

    def propose_components(
        self,
        *,
        cell_id: str,
        changes: dict[str, dict[str, Any] | None],
        suffix: str,
        hypothesis: str,
        expected_benefit: str,
        risks: list[str],
        observation_refs: list[dict[str, Any]],
        prediction: dict[str, Any] | None,
        baseline_ref: dict[str, Any] | None = None,
        proposer_run_ref: dict[str, Any] | None = None,
        leak_scan: dict[str, Any] | None = None,
        origin: str | None = None,
    ) -> str:
        """A component candidate of ``cell_id``, submitted as the proposer identity (§3.9).

        ``changes`` maps manifest slots (``prompt_bundle_ref`` or ``role_prompt``, the context,
        budget and router slots, ``L1``..``L8`` or ``decider.Lx``) to ``harness-component`` refs
        (None empties an optional slot); a ``role_prompt`` component becomes the prompt bundle
        ``implementer-<suffix>``. The baseline is the cell's effective composition (the active
        release's) unless given. The change artifact is v2 (§2.12); ``experiment_plan_ref`` names
        a draft that carries the stage-plan id ``stageplan-<proposal_id>``: the stage plan needs
        the root budget the operator gives at ``search``, after this record is immutable.

        The change artifact stores ``proposer_run_ref`` (the proposer run that drafted it, §2.12)
        and ``leak_scan`` (``{"hits": int, "index_ref": ref}``, the scan the caller ran on the new
        content; the evaluation-quality contamination metric counts it) as given, null when not
        given, and ``origin`` (IC-24: ``removal_sweep`` for a sweep proposal; null otherwise). A
        prediction is checked (shape, PROPOSAL_PREDICTION; LEAK_GATE when it names a validation
        or holdout task, §9.5, IC-11) before anything is written."""
        from ...meta_harness.stages import REMOVAL_ORIGIN, slot_of

        proposer = self.local.proposer
        effective = releases.effective(self.store, self.scope, self.app.compositions)
        if cell_id not in effective:
            raise Hold("CELL_UNKNOWN", "Not an installed cell of the app", details=[cell_id])
        base_ref = baseline_ref if baseline_ref is not None else effective[cell_id]
        if releases.pin_allowed(self.store, self.scope, self.app.compositions, base_ref) != cell_id:
            raise Hold("CELL_UNKNOWN", "The baseline is not a composition of this cell")
        if not changes:
            raise RuntimeFault("MANIFEST_SLOT", "A candidate changes at least one slot")
        if origin not in (None, REMOVAL_ORIGIN):
            raise RuntimeFault("PROPOSAL_ORIGIN", "The origin is removal_sweep or none (IC-24)")
        if leak_scan is not None and (
            not isinstance(leak_scan, dict)
            or set(leak_scan) != {"hits", "index_ref"}
            or type(leak_scan["hits"]) is not int
            or leak_scan["hits"] < 0
            or not isinstance(leak_scan["index_ref"], dict)
        ):
            raise RuntimeFault("LEAK_SCAN", 'A leak scan is {"hits": int >= 0, "index_ref": ref}')
        if proposer_run_ref is not None and (
            not isinstance(proposer_run_ref, dict)
            or set(proposer_run_ref) != {"id", "revision", "digest"}
        ):
            raise RuntimeFault("PROPOSER_RUN", "proposer_run_ref is a record ref")
        if prediction:
            self._check_prediction(prediction)
        manifests = self.manifests
        base_manifest = manifests.of_composition(base_ref)
        slots: dict[str, dict[str, Any] | None] = {}
        # the role_prompt component behind a prompt bundle: (component id, version)
        named: dict[str, tuple[str, int]] = {}
        for name, ref in changes.items():
            slot = slot_of(name)
            if slot in slots:
                raise RuntimeFault("MANIFEST_SLOT", "A slot is named twice", details=name)
            if slot == PROMPT_SLOT:
                if ref is None:
                    raise RuntimeFault("MANIFEST_SLOT", "The prompt bundle slot is required")
                kind, value = resolve_ref(self.store, self.scope, ref)
                if kind == "harness-component" and value.get("kind") == "role_prompt":
                    named[slot] = (str(value["component_id"]), int(ref["revision"]))
                    bundle_id = f"implementer-{suffix}"
                    ref = self._put(
                        prompts.KIND,
                        bundle_id,
                        prompts.bundle(bundle_id, value["content"]["implementer"], PROMPT_SOURCE),
                    )
            slots[slot] = ref
        manifest = manifests.change(base_manifest, **slots)
        diff = manifests.diff(base_manifest, manifest)
        if not diff:
            raise Hold("NO_EVIDENCE_CHANGE", "The candidate equals its baseline")
        candidate = manifests.materialize(
            proposer, base_composition_ref=base_ref, manifest=manifest, suffix=suffix
        )
        component_changes, paths = [], []
        for change in diff:
            if change.slot in named:
                component_id, version = named[change.slot]
            else:
                component_id, version = self._component_id(
                    change.after if change.after is not None else change.before
                )
            component_changes.append(
                {"kind": change.kind, "component_id": component_id,
                 "from": change.before, "to": change.after}
            )  # fmt: skip
            paths.append(f"components/{change.kind}/{component_id}@{version}")
        proposal_id = new_id("harness-proposal")
        prediction_ref = self._prediction(proposal_id, prediction) if prediction else None
        change_artifact = self.dep.artifacts.admit(
            self.scope,
            canonical(
                {
                    "changed_paths": paths,
                    "baseline_ref": base_ref,
                    "candidate_ref": candidate,
                    "component_changes": component_changes,
                    "prediction_ref": prediction_ref,
                    "proposer_run_ref": proposer_run_ref,
                    # the caller's scan of the new content; screen runs the leak gate again
                    # (MetaHarness.screen hook) whether or not one is recorded here
                    "leak_scan": leak_scan,
                    "origin": origin,
                }
            ),
            "application/json",
            trust="operator",
        )
        classification = self.local.meta.compositions.classify(self.scope, base_ref, candidate)
        draft_id = "stagedraft-" + proposal_id
        proposal = {
            "schema_version": "3.0.0",
            "proposal_id": proposal_id,
            "scope": self.scope.wire(),
            "baseline_ref": base_ref,
            "candidate_ref": candidate,
            "surface_class": classification["surface_class"],
            "hypothesis": hypothesis,
            "observation_refs": list(observation_refs),
            "change_artifact": change_artifact,
            "expected_benefit": expected_benefit,
            "risks": list(risks),
            "protected_surface_findings": [],
            "experiment_plan_ref": self._put(
                "experiment-draft",
                draft_id,
                {
                    "draft_id": draft_id,
                    "comparison": "paired baseline and candidate stages (interfaces.md §8.1)",
                    "stage_plan_id": "stageplan-" + proposal_id,
                },
            ),
            "rollback_plan_ref": self._put(
                "rollback-plan",
                "rollback-" + proposal_id,
                {
                    "rollback_id": "rollback-" + proposal_id,
                    "target_release_ref": self._active_release(),
                },
            ),
            "proposer": proposer.wire(),
            "status": "draft",
        }
        self.local.meta.submit(proposer, proposal)
        return proposal_id

    def _component_id(self, ref: dict[str, Any] | None) -> tuple[str, int]:
        """The component id and version a change path names (§2.12) for a slot's ref."""
        if ref is None:
            raise RuntimeFault("MANIFEST_SLOT", "A change names no component")
        found, value = resolve_ref(self.store, self.scope, ref)
        if found == "harness-component":
            return str(value["component_id"]), int(ref["revision"])
        if found == prompts.KIND:
            return str(value["bundle_id"]), int(ref["revision"])
        return str(ref["id"]), int(ref["revision"])

    def _check_prediction(self, prediction: dict[str, Any]) -> None:
        """RuntimeFault PROPOSAL_PREDICTION unless it has exactly the §2.12 fields; Hold LEAK_GATE
        when it names validation or holdout material (a prediction names development task ids
        only, §9.5, IC-11): the leak gate of ``MetaHarness.screen`` over every stored leak index
        (task ids of the validation and holdout splits, hidden test names, distinctive reference
        identifiers, §10.4). The findings name the kind and place, never the token."""
        lists = ("improve_task_ids", "regress_task_ids", "improve_buckets", "regress_buckets")
        delta = prediction.get("expected_delta")
        if (
            not isinstance(prediction, dict)
            or set(prediction) != PREDICTION_FIELDS
            or any(not isinstance(prediction[k], list) for k in lists)
            or type(delta) not in (int, float)
            or prediction["risk"] not in ("low", "medium", "high")
        ):
            raise RuntimeFault(
                "PROPOSAL_PREDICTION",
                "A prediction has exactly the §2.12 fields",
                details=sorted(PREDICTION_FIELDS),
            )
        findings = self.local.leak_findings(
            self.scope, {"prediction": {k: prediction[k] for k in sorted(PREDICTION_FIELDS)}}
        )
        if findings:
            raise Hold(
                "LEAK_GATE",
                "The prediction names validation or holdout task material; a prediction names "
                "development task ids only (§9.5, IC-11)",
                details=findings,
            )

    def _prediction(self, proposal_id: str, prediction: dict[str, Any]) -> dict[str, Any]:
        """The ``proposal-prediction`` record ``pred-<proposal_id>`` (§2.12)."""
        self._check_prediction(prediction)
        value = {
            "schema": "amplai.proposal-prediction.v1",
            "scope": self.scope.wire(),
            "proposal_id": proposal_id,
            **prediction,
            "made_at": now(),
        }
        with self.store.tx() as db:
            ref: dict[str, Any] = self.store.put(
                db, self.scope, PREDICTION_KIND, "pred-" + proposal_id, 1, value
            )
        return ref

    # -- 1b. the class-B code review (§3.9) ----------------------------------------------------
    def review(
        self, proposal_id: str, *, outcome: Literal["pass", "fail"], note: str
    ) -> dict[str, Any]:
        """``pass``: the operator's review receipt (operator trust) through
        ``MetaHarness.record_review``. ``fail``: ``MetaHarness.reject`` with the note."""
        if not isinstance(note, str) or not note.strip():
            raise RuntimeFault("REVIEW_NOTE", "A review states what was reviewed")
        if outcome == "fail":
            state = self.local.meta.reject(
                self.operator, proposal_id, "code review failed: " + note.strip()
            )
            return {"outcome": "fail", "review_ref": None, "state": state}
        if outcome != "pass":
            raise RuntimeFault("REVIEW_OUTCOME", "The review outcome is pass or fail")
        head = self._head(proposal_id)
        proposal = self.store.get(
            self.scope, "harness-change-proposal", head["data"]["proposal_ref"]
        )
        classification = head["data"].get("classification") or {}
        receipt = self.dep.artifacts.admit(
            self.scope,
            canonical(
                {
                    "proposal_ref": head["data"]["proposal_ref"],
                    "candidate_ref": proposal["candidate_ref"],
                    "reviewer_subject_id": self.operator.subject_id,
                    "outcome": "pass",
                    # the operator attests what the classification shows (record_review binds
                    # False: a protected change cannot pass a code review)
                    "protected_controls_changed": bool(classification.get("protected")),
                    "note": note.strip(),
                    "reviewed_at": now(),
                }
            ),
            "application/json",
            trust="operator",
        )
        ref = self.local.meta.record_review(self.operator, proposal_id, receipt)
        return {"outcome": "pass", "review_ref": ref, "state": self._head(proposal_id)["state"]}

    # -- 2. screen / reject / abort ------------------------------------------------------------
    def screen(self, proposal_id: str) -> dict[str, Any]:
        self._not_derived(proposal_id)
        return self.local.meta.screen(self.operator, proposal_id)

    def reject(self, proposal_id: str, reason: str) -> str:
        return self.local.meta.reject(self.operator, proposal_id, reason)

    def abort(self, proposal_id: str, reason: str) -> dict[str, Any]:
        return self.local.meta.abort(self.operator, proposal_id, reason)

    # -- 3. the experiment ---------------------------------------------------------------------
    def approve_experiment(
        self,
        proposal_id: str,
        *,
        max_tokens: int,
        max_wall_seconds: int,
        margin: float = 0.25,
        confidence: float = 0.95,
    ) -> dict[str, Any]:
        """Freeze the corpus, the analysis plan (D-088, D-093) and the budget, then approve."""
        head = self._require(proposal_id, "screened")
        self._not_derived(proposal_id)
        self._removal_gate(proposal_id)
        if not isinstance(self.corpus, local_corpus.Corpus):
            raise Hold(
                "META_STATE",
                "approve-experiment freezes the Work 030 corpus; a corpus v2 proposal runs its "
                "stages (amplai meta search, approve-stage)",
            )
        legacy_corpus = self.corpus
        proposal = self.store.get(
            self.scope, "harness-change-proposal", head["data"]["proposal_ref"]
        )
        artifacts = self.dep.artifacts
        cases = []
        for payload in corpus_cases(legacy_corpus):
            cases.append(
                {
                    "case_id": payload["case_id"],
                    "split": EXPERIMENT_SPLIT,
                    "task_class": payload["difficulty"],
                    "artifact_ref": artifacts.admit(
                        self.scope, canonical(payload), "application/json", trust="operator"
                    ),
                }
            )
        corpus_ref = CorpusService(self.store, artifacts).freeze(
            self.operator, new_id("corpus"), cases, holdout_use_limit=1
        )
        tasks = len(cases)
        analysis = self._put(
            "analysis-plan",
            new_id("analysis"),
            {
                "analysis_id": new_id("analysis"),
                "policy": {
                    "method": "paired_binary_conservative",
                    "confidence": confidence,
                    "minimum_tasks": tasks,
                    "repeats_per_task": 1,
                    "purpose": "confirmatory",
                    "noninferiority_margin": margin,
                    "safety_failure_limit": 0,
                    "missing_policy": "inconclusive",
                    "cost_basis": "not_compared",
                    "sample_rationale": (
                        f"{tasks} fixed demo-app tasks, one run per arm: the corpus size the "
                        "operator set (D-089 OD-5). The baseline passes every task (D-093), so "
                        "the test can show non-inferiority, not a higher success rate."
                    ),
                    "variance_basis": (
                        "Paired binary outcomes per task; the conservative paired method is "
                        "declared before any candidate ran."
                    ),
                    "sequential_rule": "fixed_sample_safety_abort_only",
                },
                "scope_note": "Non-inferiority on success rate only; cost is not compared (D-088).",
            },
        )
        sampling = self._put(
            "sampling-plan",
            new_id("sampling"),
            {
                "sampling_id": new_id("sampling"),
                "split": EXPERIMENT_SPLIT,
                "case_ids": [c["case_id"] for c in cases],
            },
        )
        holdout = self._put(
            "holdout-policy", new_id("holdout"), {"policy_id": new_id("holdout"), "sealed": True,
                                                  "max_uses": 1}
        )  # fmt: skip
        base_composition = self.store.get(
            self.scope, "harness-composition", proposal["baseline_ref"]
        )
        plan = {
            "schema_version": "3.0.0",
            "experiment_id": new_id("experiment"),
            "scope": self.scope.wire(),
            "proposal_ref": head["data"]["proposal_ref"],
            "baseline_ref": proposal["baseline_ref"],
            "candidate_ref": proposal["candidate_ref"],
            "corpus_ref": corpus_ref,
            "verifier_ref": self.app.verifier_refs[next(iter(self.app.verifier_refs))],
            "environment_ref": base_composition["sandbox_profile_ref"],
            "mode": EXPERIMENT_MODE,
            "primary_endpoint": "task_all_repeats_pass",
            "analysis_plan_ref": analysis,
            "sampling_plan_ref": sampling,
            "safety_stop_rules": ["Any safety failure or unknown effect stops the run"],
            "holdout_policy_ref": holdout,
            "budget": {
                "max_wall_seconds": max_wall_seconds,
                "max_attempts": 1,
                "max_tokens": max_tokens,
                "max_cost_microunits": 0,
                "currency": "USD",
                "max_parallel_works": 1,
                "max_delegation_depth": 0,
            },
            "frozen_at": now(),
        }
        plan["approval_ref"] = self.local.approvals.issue(
            self.operator, "experiment.execute", digest(plan)
        )
        experiment_ref = self.local.evaluation.freeze(self.operator, plan)
        self.local.meta.approve_experiment(
            self.operator, proposal_id, plan["approval_ref"], experiment_ref
        )
        return {"experiment_ref": experiment_ref, "tasks": tasks}

    def qualify_executor(self, basis: str, evidence: list[str], per_trial_tokens: int) -> None:
        """Pin the offline executor's qualification (the operator states what it rests on).

        IC-29 (provisional): the ``executor-qualification`` record keeps ``per_trial_tokens``,
        ``basis``, ``evidence``, the cell (``self.driver``) and the qualifying actor, so a later
        process rebuilds the qualification from it (``restore_qualification``)."""
        if type(per_trial_tokens) is not int or per_trial_tokens < 0:
            raise RuntimeFault("EXECUTOR_BUDGET", "--per-trial-tokens is a nonnegative integer")
        qualification_id = new_id("executor-qualification")
        qualification = self._put(
            "executor-qualification",
            qualification_id,
            {
                "qualification_id": qualification_id,
                "status": "pass",
                "executor_id": EXECUTOR_ID,
                "scope_note": basis,
                "evidence": list(evidence),
                # IC-29: what a later process rebuilds the qualification from
                "basis": basis,
                "per_trial_tokens": per_trial_tokens,
                "cell_id": self.driver,
                "qualified_by": self.operator.wire(),
                "qualified_at": now(),
            },
        )
        self.local.evaluation.executor_policy = ExecutorPolicy(
            frozenset({EXPERIMENT_MODE}), per_trial_tokens, 0, qualification
        )

    def restore_qualification(self, cell_id: str | None = None) -> dict[str, Any] | None:
        """IC-29 (provisional): pin the qualification of the newest ``executor-qualification``
        record a **human operator** wrote for ``cell_id`` (default: this ops' cell) with
        ``per_trial_tokens``, ``basis`` and ``evidence``; returns its ref, or None (nothing is
        pinned then and the executor policy stays as it was). Records of a proposer or service
        identity (the nightly one) and records without the IC-29 fields never qualify."""
        found = qualification_record(self.store, self.scope, cell_id or self.driver)
        if found is None:
            return None
        ref, value = found
        self.local.evaluation.executor_policy = ExecutorPolicy(
            frozenset({EXPERIMENT_MODE}), int(value["per_trial_tokens"]), 0, ref
        )
        return ref

    def run_experiment(self, proposal_id: str) -> dict[str, Any]:
        head = self._require(proposal_id, "experiment_approved")
        experiment_ref = self._experiment_ref(head)
        self.local.meta.start_offline(self.operator, proposal_id)
        report_ref = self.local.evaluation.run(self.operator, experiment_ref, self.executor)
        report = self.store.get(self.scope, "eval-report", report_ref)
        self.local.meta.evaluate(self.operator, proposal_id, report_ref)
        return {"report_ref": report_ref, "verdict": report["verdict"]}

    def _experiment_ref(self, head: dict[str, Any]) -> dict[str, Any]:
        ref: dict[str, Any] = head["data"]["experiment_ref"]
        return ref

    # -- 4. the canary -------------------------------------------------------------------------
    def approve_canary(
        self, proposal_id: str, task_ids: list[str], *, max_trial_tokens: int
    ) -> dict[str, Any]:
        head = self._require(proposal_id, "offline_evaluated")
        self._removal_gate(proposal_id)
        policy = canary_policy(
            task_ids=task_ids,
            binding_ref=self.app.binding_ref,
            fallback_release_ref=self._active_release(),
            policy_id=new_id("canary-policy"),
            max_trial_tokens=max_trial_tokens,
        )
        policy_ref = self._put("canary-policy", policy["policy_id"], policy)
        approval = self.local.approvals.issue(
            self.operator,
            "canary.execute",
            digest(
                {
                    "proposal_ref": head["data"]["proposal_ref"],
                    "report_ref": head["data"]["report_ref"],
                    "policy_ref": policy_ref,
                }
            ),
        )
        # Approval only. The canary is owned by the process that starts it (its owner epoch), so
        # ``run_canary`` starts it in the process that runs its trials; a canary started by one
        # command and run by another needs recovery, which ends it.
        self.local.meta.approve_canary(self.operator, proposal_id, policy_ref, approval)
        return {"policy_ref": policy_ref, "tasks": task_ids}

    def run_canary(self, proposal_id: str) -> dict[str, Any]:
        """Start the approved canary and run its tasks in this process (one owner)."""
        head = self._require(proposal_id, "canary_approved")
        self.local.meta.start_canary(self.operator, proposal_id)
        proposal = self.store.get(
            self.scope, "harness-change-proposal", head["data"]["proposal_ref"]
        )
        policy = self.store.get(self.scope, "canary-policy", head["data"]["canary_policy_ref"])
        execute = canary_execute(
            self.executor,
            self.dep.artifacts,
            self.scope,
            candidate_ref=proposal["candidate_ref"],
            task_binding_refs=policy["task_binding_refs"],
        )
        outcomes = []
        for task in policy["eligible_task_ids"]:
            outcome = self.local.meta.canary_trial(self.operator, proposal_id, task, execute)
            outcomes.append(outcome)
            if outcome["state"] != "canary_running":
                return {"state": outcome["state"], "outcomes": outcomes}
        return {
            "state": self.local.meta.request_promotion(self.operator, proposal_id),
            "outcomes": outcomes,
        }

    # -- 5. promote and rollback ---------------------------------------------------------------
    def promote(self, proposal_id: str) -> dict[str, Any]:
        head = self._require(proposal_id, "promotion_pending")
        proposal = self.store.get(
            self.scope, "harness-change-proposal", head["data"]["proposal_ref"]
        )
        policy = self.store.get(self.scope, "canary-policy", head["data"]["canary_policy_ref"])
        baseline_release = self._active_release()
        components = []
        for app in self.dep.service.apps.values():
            # the compositions new plans of the app use now (the active release's), the
            # baseline replaced by the candidate; a router change derives every one (§1.2)
            refs = list(releases.effective(self.store, self.scope, app.compositions).values())
            if proposal["baseline_ref"] in refs:
                refs = self._promoted(proposal, refs)
            components += refs
        candidate_release = releases.build(
            self.store,
            self.scope,
            self.dep.contracts,
            "local-candidate-" + digest(proposal["candidate_ref"])[7:19],
            sorted(components, key=lambda r: (r["id"], r["revision"])),
            signer=self.dep._release_signer,
            key_id=RELEASE_KEY_ID,
            basis="operator-promoted class-A candidate; each component keeps its driver report",
        )
        plan = {
            "schema_version": "3.0.0",
            "promotion_id": new_id("promotion"),
            "scope": self.scope.wire(),
            "expected_active_release_ref": baseline_release,
            "candidate_release_ref": candidate_release,
            "eval_report_ref": head["data"]["report_ref"],
            "target_binding_refs": policy["target_binding_refs"],
            "canary_policy_ref": head["data"]["canary_policy_ref"],
            "abort_rules": ["Abort on an unknown effect", "Abort on a safety failure"],
            "rollback_release_ref": baseline_release,
            "expires_at": (datetime.now(UTC) + timedelta(minutes=30))
            .isoformat()
            .replace("+00:00", "Z"),
            "active_run_policy": "drain",
        }
        plan["grant_ref"] = self.local.approvals.issue(
            self.operator, "release.promote", digest(plan)
        )
        promoted = self.local.meta.promote(self.operator, proposal_id, plan)
        return {
            "promotion": promoted,
            "candidate_release_ref": candidate_release,
            "baseline_release_ref": baseline_release,
        }

    def _promoted(
        self, proposal: dict[str, Any], refs: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """The app's compositions with the candidate in place of the baseline. A candidate whose
        router differs from the baseline's derives every other composition of the app with the
        candidate's router components (``<id>__<suffix>``, same profiles, written by the proposer
        identity): an app's compositions must name one router (``ROUTER_INCONSISTENT``)."""
        baseline, candidate = proposal["baseline_ref"], proposal["candidate_ref"]
        out = [candidate if ref == baseline else ref for ref in refs]
        base = self.store.get(self.scope, "harness-composition", baseline)
        cand = self.store.get(self.scope, "harness-composition", candidate)
        if base["router_policy_ref"] == cand["router_policy_ref"]:
            return out
        from ...meta_harness.manifest import ROUTER

        if releases.CANDIDATE_SEP not in cand["composition_id"]:
            raise Hold("RELEASE_COMPOSITION", "A router candidate is named <id>__<suffix>")
        suffix = cand["composition_id"].rsplit(releases.CANDIDATE_SEP, 1)[1]
        router = self.manifests.of_composition(candidate).router
        for i, ref in enumerate(out):
            if ref == candidate:
                continue
            other = self.store.get(self.scope, "harness-composition", ref)
            if other["router_policy_ref"] == cand["router_policy_ref"]:
                continue
            manifest = self.manifests.change(
                self.manifests.of_composition(ref), **{slot: router[slot] for slot in ROUTER}
            )
            out[i] = self.manifests.materialize(
                self.local.proposer, base_composition_ref=ref, manifest=manifest, suffix=suffix
            )
        return out

    def rollback(self, proposal_id: str) -> dict[str, Any]:
        """Return the pointer to the release the promotion named (read from its recorded plan)."""
        head = self._require(proposal_id, "promoted")
        plan = head["data"]["promotion_plan"]
        baseline, candidate = plan["rollback_release_ref"], plan["candidate_release_ref"]
        approval = self.local.approvals.issue(
            self.operator,
            "release.rollback",
            digest({"target_ref": baseline, "expected_active_ref": candidate}),
        )
        return self.local.meta.rollback(self.operator, proposal_id, baseline, candidate, approval)

    def report(self, proposal_id: str) -> dict[str, Any]:
        """Per arm: solved, verified-but-hidden-fail, test edits, tokens, time and API-equivalent
        cost per solved task (D-094). Descriptive; the verdict is the pre-registered one."""
        from ...meta_harness.trial_metrics import TrialMetrics

        head = self._head(proposal_id)
        report_ref = head["data"].get("report_ref")
        if not report_ref:
            raise Hold("META_STATE", "The candidate has no experiment report yet")
        report = self.store.get(self.scope, "eval-report", report_ref)
        metrics = TrialMetrics(self.dep.service)
        rows = [
            metrics.trial(self.store.get(self.scope, "eval-trial", ref))
            for ref in report["run_refs"]
        ]
        flagged = [
            {k: r[k] for k in ("task_id", "arm")} | {"why": why}
            for r in rows
            for why in (["verified_hidden_fail"] if r["verified_hidden_fail"] else [])
            + (["tests_changed"] if r.get("diff") and r["diff"]["tests_changed"] else [])
        ]
        return {
            "proposal_id": proposal_id,
            "report_id": report_ref["id"],
            "arms": TrialMetrics.summarize(rows),
            "flagged_trials": flagged,
            "note": (
                "Descriptive only. API-equivalent cost is an estimate at published API prices, "
                "not a subscription bill; upper_bound means no cache breakdown was recorded."
            ),
        }

    # -- 6. derived proposals (IC-02, IC-19 A) --------------------------------------------------
    def derived_proposal(self, parent_id: str, *, slot: str, reason: str) -> str:
        """The leave-one-out proposal of ``slot``: baseline = the parent's candidate, candidate =
        that candidate with ``slot`` back at the parent baseline's component, submitted by the
        proposer identity. It names the parent's stage plan as its experiment plan, is never
        screened and stays ``draft``. Hold DERIVED_PROPOSAL unless ``slot`` is a changed
        component of the parent and every slot of the variant equals the parent candidate's or
        the parent baseline's."""
        from ...meta_harness.stages import (
            PLAN_KIND,
            check_derived,
            component_name,
            is_derived,
            slot_of,
        )

        if not isinstance(reason, str) or not reason.strip():
            raise RuntimeFault("DERIVED_REASON", "A derived proposal states why it exists")
        proposer = self.local.proposer
        parent = self._proposal(parent_id)
        if is_derived(self.store, self.scope, parent):
            raise Hold("DERIVED_PROPOSAL", "A derived proposal has no derived proposals")
        plans = [
            ref for ref, _ in self.store.list_objects(self.scope, PLAN_KIND)
            if ref["id"] == "stageplan-" + parent_id
        ]  # fmt: skip
        if not plans:
            raise Hold("META_STATE", "The parent has no stage plan; amplai meta search first")
        plan_ref = max(plans, key=lambda r: r["revision"])
        manifests = self.manifests
        base_manifest = manifests.of_composition(parent["baseline_ref"])
        cand_manifest = manifests.of_composition(parent["candidate_ref"])
        target = slot_of(slot)
        changed = {c.slot: c for c in manifests.diff(base_manifest, cand_manifest)}
        if target not in changed:
            raise Hold(
                "DERIVED_PROPOSAL",
                "The slot is not a changed component of the parent",
                details={"slot": slot, "changed": sorted(component_name(s) for s in changed)},
            )
        change = changed[target]
        variant = manifests.materialize(
            proposer,
            base_composition_ref=parent["candidate_ref"],
            manifest=manifests.change(cand_manifest, **{target: change.before}),
            suffix="loo-" + target,
        )
        component_id, version = self._component_id(
            change.before if change.before is not None else change.after
        )
        proposal_id = new_id("harness-proposal")
        draft = {"baseline_ref": parent["candidate_ref"], "candidate_ref": variant}
        check_derived(self.store, self.scope, manifests, parent, draft)
        change_artifact = self.dep.artifacts.admit(
            self.scope,
            canonical(
                {
                    "changed_paths": [f"components/{change.kind}/{component_id}@{version}"],
                    **draft,
                    "component_changes": [
                        {
                            "kind": change.kind,
                            "component_id": component_id,
                            "from": change.after,
                            "to": change.before,
                        }
                    ],
                    "prediction_ref": None,
                    "proposer_run_ref": None,
                    "leak_scan": None,  # no new content: the parent's screen covered it
                    "origin": None,  # IC-19 (A): the parent's stage plan marks it derived
                }
            ),
            "application/json",
            trust="operator",
        )
        classification = self.local.meta.compositions.classify(
            self.scope, parent["candidate_ref"], variant
        )
        proposal = {
            "schema_version": "3.0.0",
            "proposal_id": proposal_id,
            "scope": self.scope.wire(),
            **draft,
            "surface_class": classification["surface_class"],
            "hypothesis": (
                f"Leave-one-out ablation of {component_name(target)} from {parent_id}: "
                f"{reason.strip()}"
            )[:4096],
            "observation_refs": list(parent["observation_refs"]),
            "change_artifact": change_artifact,
            "expected_benefit": (
                "Measures the contribution of one component (IC-02); never promoted (IC-19 A)"
            ),
            "risks": [
                "Descriptive ablation only; a removal is re-proposed as an ordinary proposal"
            ],
            "protected_surface_findings": [],
            "experiment_plan_ref": plan_ref,  # the parent's stage plan marks it derived
            "rollback_plan_ref": parent["rollback_plan_ref"],
            "proposer": proposer.wire(),
            "status": "draft",
        }
        self.local.meta.submit(proposer, proposal)
        return proposal_id

    # -- 7. calibration, stages and search (§3.9, §8.1, §8.2) ---------------------------------
    def _corpus_v2(self) -> CorpusV2:
        if not isinstance(self.corpus, CorpusV2):
            raise Hold("META_STATE", "Stages and calibration run on a corpus v2 (§10)")
        return self.corpus

    def set_corpus(self, set_name: str) -> CorpusV2:
        """One set of the loaded corpus v2 as its own corpus (``corpus_v2.for_set``): the main
        set keeps the loaded corpus (its id), the regression set is ``amplai-regression-v1``
        (§10.6). Hold META_STATE when the loaded corpus has no task of that set."""
        from ...meta_harness.corpus_v2 import for_set

        corpus = self._corpus_v2()
        if set_name == MAIN_SET:
            return corpus
        part = for_set(corpus, set_name)
        if not part.tasks:
            raise Hold("META_STATE", f"The loaded corpus has no {set_name} task",
                       details={"set": set_name})  # fmt: skip
        return part

    def set_app(self, set_name: str) -> InstalledApp:
        """The installed app the ``app``-environment tasks of one set name (the regression set
        names the Work 030 demo app, §10.6). Hold TARGET_UNKNOWN for none or several."""
        corpus = self.set_corpus(set_name)
        named = {
            str(corpus.bases.get(t.base_id, {}).get("app_id"))
            for t in corpus.tasks
            if t.environment_id == "app"
        }
        installed = sorted(named & set(self.dep.service.apps))
        if len(installed) != 1:
            raise Hold(
                "TARGET_UNKNOWN",
                f"The {set_name} set names no single installed app",
                details={"installed": installed, "named": sorted(named)},
            )
        return self.dep.service.apps[installed[0]]

    def frozen_corpus(
        self, corpus_ref: dict[str, Any] | None = None, *, corpus: CorpusV2 | None = None
    ) -> dict[str, Any]:
        """The frozen refs of this corpus v2 (``corpus_v2.freeze``): the latest task index of its
        version (or the one of ``corpus_ref``) and the leak index of the same corpus record.
        Hold CORPUS_CHANGED when the index of ``corpus_ref`` is of another version than the
        corpus loaded now. ``corpus`` is one set of it (``set_corpus``; default: the loaded
        corpus, whose id is the main set's)."""
        corpus = corpus if corpus is not None else self._corpus_v2()
        rows = [
            (ref, value)
            for ref, value in self.store.list_objects(self.scope, "corpus-task-index")
            if ref["id"] == "taskindex-" + corpus.corpus_id
            and (
                value.get("corpus_ref") == corpus_ref
                if corpus_ref is not None
                else value.get("corpus_version") == corpus.version
            )
        ]
        if not rows:
            raise Hold(
                "META_STATE",
                "This corpus version is not frozen; amplai meta corpus freeze first",
                details={"corpus_id": corpus.corpus_id, "version": corpus.version},
            )
        index_ref, index = max(rows, key=lambda row: row[0]["revision"])
        if index.get("corpus_version") != corpus.version:
            raise Hold(
                "CORPUS_CHANGED",
                "The corpus loaded now is another version than the frozen corpus",
                details={
                    "corpus_id": corpus.corpus_id,
                    "frozen_version": index.get("corpus_version"),
                    "loaded_version": corpus.version,
                },
            )
        leaks = [
            ref
            for ref, value in self.store.list_objects(self.scope, "leak-index")
            if ref["id"] == "leak-" + corpus.corpus_id
            and value.get("corpus_ref") == index["corpus_ref"]
        ]
        if not leaks:
            raise Hold("META_STATE", "The frozen corpus has no leak index")
        return {
            "corpus_ref": index["corpus_ref"],
            "task_index_ref": index_ref,
            "leak_index_ref": max(leaks, key=lambda r: r["revision"]),
        }

    def check_corpus(
        self,
        corpus_ref: dict[str, Any],
        case_ids: list[str] | None = None,
        *,
        corpus: CorpusV2 | None = None,
    ) -> None:
        """Hold CORPUS_CHANGED unless the corpus v2 loaded now is the frozen ``corpus_ref`` for
        ``case_ids`` (every case when None): its task index has the loaded version, and each case
        artifact is the §2.7 payload of the loaded task (``corpus_v2.case_payload``: contract
        text, hidden-test digest, base commit). The trial executor builds a trial from the loaded
        task of the case id (``LocalTrialExecutor._spec``), so a task edited or a version bumped
        after the freeze would otherwise run under the frozen case id."""
        from ...meta_harness.corpus_v2 import case_payload
        from ...meta_harness.local_corpus import CorpusError

        corpus = corpus if corpus is not None else self._corpus_v2()
        self.frozen_corpus(corpus_ref, corpus=corpus)  # the version of its task index
        frozen = self.store.get(self.scope, "eval-corpus", corpus_ref)
        wanted = None if case_ids is None else set(case_ids)
        cases = [c for c in frozen["cases"] if wanted is None or c["case_id"] in wanted]
        changed = sorted(wanted - {c["case_id"] for c in cases}) if wanted is not None else []
        for case in cases:
            try:
                payload = case_payload(corpus, corpus.task(case["case_id"]))
            except CorpusError:  # the task is gone or its base commit is unreadable
                changed.append(case["case_id"])
                continue
            if digest_bytes(canonical(payload)) != case["artifact_ref"]["digest"]:
                changed.append(case["case_id"])
        if changed:
            raise Hold(
                "CORPUS_CHANGED",
                "Tasks loaded now differ from the frozen corpus; freeze a new corpus version",
                details={"corpus_ref": corpus_ref, "cases": sorted(changed)},
            )

    def latest_summary(
        self, corpus_ref: dict[str, Any], cell_id: str, version_ref: dict[str, Any]
    ) -> dict[str, Any] | None:
        """The newest calibration summary of ``corpus_ref`` with a row for ``cell_id`` that pins
        ``version_ref`` (by ``summarized_at``, then id)."""
        found = []
        for ref, value in self.store.list_objects(self.scope, "calibration-summary"):
            if value.get("evaluator_version_ref") != version_ref or cell_id not in (
                value.get("cells") or {}
            ):
                continue
            plan = self.store.get(self.scope, "calibration-plan", value["plan_ref"])
            if plan.get("corpus_ref") == corpus_ref:
                found.append((str(value.get("summarized_at")), ref["id"], ref))
        return max(found, key=lambda row: row[:2])[2] if found else None

    def stage_runner(
        self,
        proposal_id: str,
        *,
        cell_id: str | None = None,
        parallel: int = 1,
        issuer: ApprovalIssuer | None = None,
    ) -> StageRunner:
        """A ``StageRunner`` on the frozen corpus v2. A proposal with a stage plan keeps the
        corpus, calibration summary and evaluator version its plan pins; a new one takes the
        evaluator version of the running code and the newest matching calibration summary.
        ``issuer`` (default: the human operator) freezes and runs its stage experiments (the
        nightly runner passes ``stages.standing_issuer``)."""
        from ...evaluation import versions
        from ...meta_harness.leak_gate import LeakGate
        from ...meta_harness.stages import PLAN_KIND, StageRunner
        from ...meta_harness.trial_metrics import TrialMetrics

        plans = [
            (ref, value) for ref, value in self.store.list_objects(self.scope, PLAN_KIND)
            if ref["id"] == "stageplan-" + proposal_id
        ]  # fmt: skip
        if plans:
            _, plan = max(plans, key=lambda row: row[0]["revision"])
            refs = self.frozen_corpus(plan["corpus_ref"])
            summary_ref, version_ref = (
                plan["calibration_summary_ref"],
                plan["evaluator_version_ref"],
            )
        else:
            if cell_id is None:
                raise Hold("META_STATE", "The proposal has no stage plan; amplai meta search")
            refs = self.frozen_corpus()
            current = versions.current_version_ref(self.store, self.scope)
            if current is None:
                raise Hold("EVALUATOR_UNQUALIFIED", "No evaluator version matches the running code")
            version_ref = current
            found = self.latest_summary(refs["corpus_ref"], cell_id, version_ref)
            if found is None:
                raise Hold(
                    "NO_INFORMATIVE_TASKS",
                    "No calibration summary of this corpus and cell; amplai meta calibrate first",
                    details={"cell_id": cell_id},
                )
            summary_ref = found
        return StageRunner(
            self,
            refs,
            calibration_summary_ref=summary_ref,
            evaluator_version_ref=version_ref,
            metrics=TrialMetrics(self.dep.service),
            leak_gate=LeakGate(self.operator, self.store, refs["leak_index_ref"]),
            parallel=parallel,
            issuer=issuer,
        )

    def search(
        self,
        proposal_id: str,
        *,
        cell_id: str,
        root_budget: dict[str, Any],
        parallel: int = 1,
    ) -> dict[str, Any]:
        """``StageRunner.plan`` (once per proposal) and ``advance``: the stages without an
        operator gate run; it stops at every operator gate (§8.1). ``findings`` holds each
        stage's guard findings (hack guards, ablation variants without a report)."""
        from ...meta_harness.stages import stage_findings

        runner = self.stage_runner(proposal_id, cell_id=cell_id, parallel=parallel)
        plan_ref = runner.plan(proposal_id, cell_id=cell_id, root_budget=root_budget)
        steps = runner.advance(proposal_id)
        return {
            "proposal_id": proposal_id,
            "plan_ref": plan_ref,
            "steps": [asdict(s) for s in steps],
            "stopped": runner.last_stop,
            "findings": stage_findings(self.store, self.scope, proposal_id),
        }

    def approve_stage(
        self,
        proposal_id: str,
        stage: str,
        *,
        parallel: int = 1,
        queue: bool = False,
        subject_digest: str | None = None,
    ) -> dict[str, Any]:
        """The operator's ``focused``/``holdout`` gate (§8.1): freeze, approve, run, record.

        ``queue`` (IC-10, ``--queue``): approve the exact experiment a night queued for the
        focused stage and freeze it; nothing runs here (the next night's confirmation phase runs
        it). ``subject_digest`` (``--digest``), when given, must be the queued digest."""
        runner = self.stage_runner(proposal_id, parallel=parallel)
        if queue:
            if stage != "focused":
                raise Hold("META_STATE", "Only the focused stage is queued (holdout stays here)")
            step = runner.approve_queued(proposal_id, stage, subject_digest=subject_digest)
            return {"proposal_id": proposal_id, "queued": True, **asdict(step)}
        if subject_digest is not None:
            raise RuntimeFault("LOCAL_INPUT", "--digest names a queued experiment (--queue)")
        step = runner.approve_stage(proposal_id, stage)
        return {"proposal_id": proposal_id, **asdict(step)}

    def stages(self, proposal_id: str) -> dict[str, Any]:
        from ...meta_harness.stages import stage_findings, stage_status

        return {
            "proposal_id": proposal_id,
            "state": self._head(proposal_id)["state"],
            "steps": [asdict(s) for s in stage_status(self.store, self.scope, proposal_id)],
            "findings": stage_findings(self.store, self.scope, proposal_id),
        }

    def calibrate(
        self,
        cell_ids: list[str],
        *,
        max_repeats: int,
        max_trials: int,
        parallel: int,
        max_tokens: int,
        max_wall_seconds: int,
        corpus_set: str = MAIN_SET,
    ) -> dict[str, Any]:
        """Freeze, approve (the operator, exact digest) and run a calibration of ``cell_ids``
        on every development and validation case of the frozen corpus (§8.2). The installed
        composition of each cell is its v1 composition. ``max_trials`` bounds the worst case
        (cells x cases x ``max_repeats``); a larger plan is refused before anything runs.

        ``corpus_set`` ``"regression"`` calibrates the frozen regression set
        (``amplai-regression-v1``, every case validation, §10.6) on the installed app its tasks
        name: the drift baseline of the nightly runner is the human operator's newest such
        calibration (clarification after S12: "the baseline is the operator's newest
        calibration on the regression set")."""
        from ...evaluation import calibration
        from ...meta_harness.trial_metrics import TrialMetrics

        cells = list(cell_ids)
        if not cells or len(set(cells)) != len(cells):
            raise RuntimeFault("CALIBRATION_PLAN", "Name each cell once")
        if corpus_set not in (MAIN_SET, REGRESSION_SET):
            raise RuntimeFault("CALIBRATION_PLAN", "The calibrated set is main or regression")
        loaded = self.set_corpus(corpus_set)
        app = self.app if corpus_set == MAIN_SET else self.set_app(corpus_set)
        compositions = app.compositions
        missing = [c for c in cells if c not in compositions]
        if missing:
            raise Hold("CELL_UNKNOWN", "Not an installed cell of the app", details=missing)
        policy = self.local.evaluation.executor_policy
        if policy is None:
            raise Hold(
                "QUALIFIED_EXECUTOR_REQUIRED",
                "Register a server-side qualified bounded executor first",
            )
        refs = self.frozen_corpus(corpus=loaded)
        corpus = self.store.get(self.scope, "eval-corpus", refs["corpus_ref"])
        case_ids = [c["case_id"] for c in corpus["cases"] if c["split"] in calibration.SPLITS]
        present = {c["split"] for c in corpus["cases"] if c["case_id"] in set(case_ids)}
        # the trials run the loaded tasks: they must be the frozen ones (freeze and run follow
        # in this call on this loaded corpus)
        self.check_corpus(refs["corpus_ref"], case_ids, corpus=loaded)
        worst = len(cells) * len(case_ids) * max_repeats
        if type(max_trials) is not int or max_trials < 1 or worst > max_trials:
            raise RuntimeFault(
                "CALIBRATION_PLAN",
                f"The plan may run up to {worst} trials (cells x cases x max_repeats), "
                f"above --max-trials {max_trials}",
                details={"worst_case_trials": worst, "max_trials": max_trials},
            )
        plan: dict[str, Any] = {
            "schema": calibration.PLAN_SCHEMA,
            "scope": self.scope.wire(),
            "cells": cells,
            "composition_refs": {c: [compositions[c]] for c in cells},
            "corpus_ref": refs["corpus_ref"],
            # the main set keeps both splits; the regression set is all validation (§10.6)
            "splits": list(calibration.SPLITS)
            if corpus_set == MAIN_SET
            else [x for x in calibration.SPLITS if x in present],
            "case_ids": case_ids,
            "initial_repeats": 1,
            "adaptive": {
                "max_repeats": max_repeats,
                "rule": calibration.RULE,
                "borderline": list(calibration.INFORMATIVE),
            },
            "budget": {
                "max_wall_seconds": max_wall_seconds,
                "max_attempts": 1,
                "max_tokens": max_tokens,
                "max_cost_microunits": 0,
                "currency": "USD",
                "max_parallel_works": parallel,
                "max_delegation_depth": 0,
            },
            "max_parallel": parallel,
            "frozen_at": now(),
        }
        plan["approval_ref"] = self.local.approvals.issue(
            self.operator, "experiment.execute", digest(plan)
        )
        service = calibration.CalibrationService(
            self.store,
            self.dep.contracts,
            self.dep.artifacts,
            approval_check=self.local.approvals.check,
            executor_id=EXECUTOR_ID,
            executor_policy=policy,
        )
        plan_ref = service.freeze(self.operator, plan)
        summary_ref = service.run(self.operator, plan_ref, self.executor, parallel=parallel)
        run = self.store.head(self.scope, calibration.RUN_KIND, plan_ref["id"])
        metrics = TrialMetrics(self.dep.service)
        for ref in run["data"]["trial_refs"]:
            metrics.record(ref)
        return {
            "plan_ref": plan_ref,
            "summary_ref": summary_ref,
            "state": run["state"],
            "trials": len(run["data"]["trial_refs"]),
            "stop_reason": run["data"]["stop_reason"],
        }

    def calibration_show(self, plan_id: str) -> dict[str, Any]:
        """The run head and the summary of one calibration plan (operator view)."""
        from ...evaluation import calibration

        run = self.store.head(self.scope, calibration.RUN_KIND, plan_id)
        summaries = [
            (ref, value)
            for ref, value in self.store.list_objects(self.scope, calibration.SUMMARY_KIND)
            if ref["id"] == "calsum-" + plan_id
        ]
        summary_ref, summary = (
            max(summaries, key=lambda row: row[0]["revision"]) if summaries else (None, None)
        )
        return {
            "plan_id": plan_id,
            "state": run["state"],
            "stop_reason": run["data"].get("stop_reason"),
            "trials": len(run["data"].get("trial_refs") or []),
            "repeats_added": run["data"].get("repeats_added"),
            "summary_ref": summary_ref,
            "summary": json.loads(json.dumps(summary)) if summary is not None else None,
        }

    # -- 8. reconcile (IC-18, provisional) ------------------------------------------------------
    def reconcile(
        self,
        proposal_id: str,
        *,
        stage: str | None = None,
        allocation_id: str | None = None,
        tokens: int | None = None,
        cost: int | None = None,
        receipt: Any = None,
    ) -> dict[str, Any]:
        """``reconcile`` as this product's human operator."""
        return reconcile(
            self.dep, self.operator, proposal_id, stage=stage, allocation_id=allocation_id,
            tokens=tokens, cost=cost, receipt=receipt,
        )  # fmt: skip

    # -- read ----------------------------------------------------------------------------------
    def status(self, proposal_id: str) -> dict[str, Any]:
        head = self._head(proposal_id)
        data = head["data"]
        return {
            "proposal_id": proposal_id,
            "state": head["state"],
            "classification": data.get("classification"),
            "rejection": data.get("rejection"),
            "canary_trials": len(data.get("canary_trials") or []),
            "canary_incidents": data.get("canary_incidents") or [],
            "active_release": self._active_release()["id"],
        }

    def _require(self, proposal_id: str, *states: str) -> dict[str, Any]:
        """The gate needs the candidate in one of ``states`` (MetaHarness enforces it as well)."""
        head = self._head(proposal_id)
        if head["state"] not in states:
            raise Hold(
                "META_STATE",
                f"The candidate is {head['state']}; this gate needs {' or '.join(states)}",
            )
        return head


# -- IC-29 (provisional): the stored executor qualification ---------------------------------------
def qualification_record(
    store: Any, scope: Any, cell_id: str
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """(ref, value) of the newest ``executor-qualification`` of ``cell_id`` that a human operator
    wrote with the IC-29 fields (``per_trial_tokens`` a nonnegative integer, ``basis`` a
    non-empty string, ``evidence`` a non-empty list of strings), ``status`` pass and this
    executor; newest by ``qualified_at``, then id. None when there is none."""
    from .meta_local import NIGHTLY_ID, PROPOSER_ID

    found = []
    for ref, value in store.list_objects(scope, "executor-qualification"):
        by = value.get("qualified_by")
        evidence = value.get("evidence")
        tokens = value.get("per_trial_tokens")
        if (
            value.get("cell_id") != cell_id
            or value.get("status") != "pass"
            or value.get("executor_id") != EXECUTOR_ID
            or not isinstance(by, dict)
            or by.get("kind") != "human"
            or by.get("subject_id") in (PROPOSER_ID, NIGHTLY_ID)
            or type(tokens) is not int
            or tokens < 0
            or not isinstance(value.get("basis"), str)
            or not value["basis"]
            or not isinstance(evidence, list)
            or not evidence
            or not all(isinstance(e, str) for e in evidence)
        ):
            continue
        found.append((str(value.get("qualified_at")), ref["id"], ref, value))
    if not found:
        return None
    _at, _id, ref, value = max(found, key=lambda row: row[:2])
    return dict(ref), dict(value)


# -- IC-18 (provisional): the human operator's reconcile path -------------------------------------
def _unresolved(store: Any, scope: Any, root_id: str) -> list[dict[str, Any]]:
    """The reserved or unknown allocations of a proposal root (none without a root)."""
    try:
        head = store.head(scope, "meta-budget", root_id)
    except RuntimeFault as exc:
        if exc.code != "NOT_FOUND":
            raise
        return []
    return [
        {"allocation_id": key, "status": value["status"], "tokens": value["tokens"],
         "owner_epoch": value.get("owner_epoch")}
        for key, value in sorted(head["data"]["allocations"].items())
        if value["status"] in ("reserved", "unknown")
    ]  # fmt: skip


def reconcile(
    dep: Any,
    actor: Any,
    proposal_id: str,
    *,
    stage: str | None = None,
    allocation_id: str | None = None,
    tokens: int | None = None,
    cost: int | None = None,
    receipt: Any = None,
) -> dict[str, Any]:
    """IC-18 (provisional): the human operator's reconcile path (``amplai meta reconcile``).

    ``stage``: the stage experiment of ``proposal_id``'s stage that is ``running`` from an
    earlier owner process becomes ``interrupted`` through ``EvaluationService.recover_interrupted``
    (never re-run; Hold EXPERIMENT_OWNER while its owner may still run) and the stage ``aborted``.
    A derived proposal's ablation is reconciled on the derived proposal id; the parent's ablation
    has no experiment of its own. For an interrupted holdout the evolution head stays
    ``offline_running`` until the operator's ``abort``.

    ``allocation_id``: one ``reserved`` or ``unknown`` allocation of ``proposal_id``'s root is
    settled through ``EvolutionBudget.reconcile`` with ``tokens``, ``cost`` and ``receipt`` (the
    operator's stopped-process usage receipt, admitted with operator trust: ``allocation_id``,
    ``tokens``, ``cost_microunits``, ``process_stopped`` true, ``unknown_effects`` 0).

    Only the human operator of this scope (Hold APPROVAL_HUMAN), never a proposer identity (Hold
    SELF_RECONCILE), with ``experiment.reconcile`` (FORBIDDEN otherwise); one target, either
    ``stage`` or ``allocation_id`` (RuntimeFault RECONCILE_TARGET). Holds the proposal's stage lock
    (Hold SEARCH_BUSY while a runner works on it)."""
    from ...meta_harness.stages import RUN_KIND, lock_root, stage_lock, update_stage
    from .meta_local import PROPOSER_ID

    store, scope = dep.store, dep.scope
    if actor.kind != "human" or actor.scope != scope:
        raise Hold("APPROVAL_HUMAN", "Only the human operator of this scope can reconcile")
    if actor.subject_id == PROPOSER_ID or "harness.propose" in actor.permissions:
        raise Hold("SELF_RECONCILE", "A proposer identity cannot reconcile")
    actor.require("experiment.reconcile")
    if (stage is None) == (allocation_id is None):
        raise RuntimeFault("RECONCILE_TARGET", "Name either a stage or an allocation")
    if allocation_id is not None and (tokens is None or cost is None or receipt is None):
        raise RuntimeFault(
            "RECONCILE_TARGET", "An allocation is reconciled with its tokens, cost and receipt"
        )
    evaluation = dep.meta_local.evaluation
    with stage_lock(store, scope, lock_root(store, scope, proposal_id)):
        if stage is not None:
            try:
                run = store.head(scope, RUN_KIND, "stagerun-" + proposal_id)
            except RuntimeFault as exc:
                if exc.code != "NOT_FOUND":
                    raise
                raise Hold("META_STATE", "The proposal has no stage run") from exc
            entry = run["data"]["stages"].get(stage)
            if entry is None or entry["state"] != "running" or entry["experiment_ref"] is None:
                raise Hold(
                    "META_STATE",
                    "Only a running stage with a stage experiment is reconciled",
                    details={"stage": stage, "state": entry["state"] if entry else None},
                )
            recovered = evaluation.recover_interrupted(actor, entry["experiment_ref"])
            update_stage(store, scope, proposal_id, stage, state="aborted")
            return {
                "proposal_id": proposal_id,
                "stage": stage,
                "state": "aborted",
                "experiment_ref": entry["experiment_ref"],
                "recovered": recovered,
                "unresolved_allocations": _unresolved(store, scope, proposal_id),
            }
        allocations = {a["allocation_id"]: a for a in _unresolved(store, scope, proposal_id)}
        found = allocations.get(str(allocation_id))
        if found is None:
            raise Hold(
                "RECONCILIATION_STATE",
                "No unresolved allocation of this proposal root",
                details={"unresolved": sorted(allocations)},
            )
        if found["status"] == "reserved" and found["owner_epoch"] == store.epoch:
            raise Hold(
                "EXPERIMENT_OWNER",
                "The process that reserved it may still be running; do not race its executor",
            )
        evidence = dep.artifacts.admit(
            scope, canonical(receipt), "application/json", trust="operator"
        )
        evaluation.budgets.reconcile(
            actor, proposal_id, str(allocation_id), tokens=tokens, cost=cost,
            evidence_ref=evidence, artifacts=dep.artifacts,
        )  # fmt: skip
        return {
            "proposal_id": proposal_id,
            "allocation_id": allocation_id,
            "status": "settled",
            "evidence_ref": evidence,
            "unresolved_allocations": _unresolved(store, scope, proposal_id),
        }
