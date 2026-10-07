"""The meta-harness on the local product (Work 030 S3, D-089).

MetaHarness and EvaluationService share the product's store. The meta-proposer is a service
identity that can only propose; the human operator is the reviewer. Approvals of an experiment, a
canary, a promotion or a rollback are durable operator-issued objects that name their action and
the exact subject they approve, so nothing lives only in memory and a proposer can never issue one.

Work 033 S11 (interfaces.md §3.8, §3.9, §7.4, §10.4):

- ``MetaHarness.screen`` calls the leak-gate hook ``leak_findings``: the proposal, its change
  artifact and the content of every component the candidate brings in are scanned against every
  stored ``leak-index`` (latest revision of each corpus). The index is opened by a host-side reader
  identity without permissions (never ``harness.propose``, so ``LeakGate`` admits it); a store with
  no frozen corpus v2 has no index and nothing to find.
- ``EvaluationService`` gets the real ``reference_validator``: the declared reference composition
  is pinnable (``releases.pin_allowed``) for the baseline's cell and differs from the baseline only
  in ``attempt_policy``/``execution_strategy``, the latter a ``best_of_n`` with the declared n
  (``ManifestService.diff``). The installed compositions come from ``installed_compositions``,
  which ``LocalMetaOps`` sets from the product it opens.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ...evaluation.service import EvaluationService
from ...meta_harness.service import MetaHarness
from ..contracts.authority import Actor
from ..contracts.identity import digest, new_id, now
from ..contracts.semantics import resolve_ref
from ..errors import Hold, RuntimeFault
from ..evidence.cas import ArtifactStore
from ..storage.store import Scope, Store

APPROVAL_KIND = "meta-approval"
PROPOSER_ID = "amplai-meta-proposer"
EXECUTOR_ID = "local-offline-executor"
RELEASE_KEY_ID = "local-authority"
# Host-side reader of leak-index records (§3.11: built by host code, never a proposer identity).
LEAK_READER_ID = "amplai-meta-leak-reader"
LEAK_INDEX_KIND = "leak-index"
# §7.4: a reference composition differs from the baseline only in these budget-policy slots.
REFERENCE_SLOTS = frozenset({"attempt_policy", "execution_strategy"})
# composition fields a reference may differ in besides its carriers (the record's own identity)
IDENTITY_FIELDS = frozenset({"composition_id", "revision", "created_at"})
CARRIER_FIELDS = frozenset(
    {"prompt_bundle_ref", "context_policy_ref", "budget_policy_ref", "router_policy_ref"}
)

# the action a MetaHarness/EvaluationService approval_check names -> the permission that issues it
ACTION_PERMISSION = {
    "experiment.execute": "experiment.approve",
    "canary.execute": "canary.approve",
    "release.promote": "release.promote",
    "release.rollback": "release.rollback",
    # IC-17 (provisional): the standing approval of the nightly loop; issued only through
    # ``LocalMetaApprovals.issue_nightly`` (which keeps the policy), never through ``issue``
    "nightly.explore": "nightly.approve",
}
# What the human operator holds on top of the goal permissions. Never ``harness.propose``.
META_OPERATOR_PERMISSIONS = frozenset(
    {
        "harness.review",
        "experiment.approve",
        "experiment.run",
        "canary.approve",
        "canary.run",
        "release.promote",
        "release.rollback",
        "runtime.admin",
        "corpus.manage",
        "corpus.read",
        "corpus.holdout.evaluate",
        # IC-18 (provisional): the human operator's reconcile path (``amplai meta reconcile``,
        # ``LocalMetaOps.reconcile``, which also refuses a non-human or proposer actor)
        "experiment.reconcile",
        # IC-26 (provisional): evaluator changes and requalification (``evaluation/quality.py``,
        # which also refuses a non-human or proposer actor)
        "evaluator.approve",
        # IC-17 (provisional): the standing approval of the nightly loop (``issue_nightly``)
        "nightly.approve",
    }
)
PROPOSER_PERMISSIONS = frozenset({"harness.propose"})

# IC-17 (provisional, class C): the nightly loop's service identity and its standing approval.
NIGHTLY_ID = "amplai-meta-nightly"
NIGHTLY_ACTION = "nightly.explore"
NIGHTLY_PERMISSION = "nightly.approve"
# IC-30 (A, operator decision 2026-10-07): ``harness.screen`` lets the nightly identity run the
# mechanical screen (protected surfaces, leak gate) of a class A draft (``MetaHarness.screen``);
# class B, review, reject and every human gate stay out of reach (NIGHTLY_EXCLUDED)
NIGHTLY_PERMISSIONS = frozenset(
    {"experiment.approve", "experiment.run", "corpus.read", "execution.approve", "harness.screen"}
)
# never held by the nightly identity (IC-17); an actor holding any of them is not the nightly one
NIGHTLY_EXCLUDED = frozenset(
    {
        "corpus.holdout.evaluate", "canary.approve", "canary.run", "release.promote",
        "release.rollback", "harness.review", "harness.propose", "experiment.reconcile",
        NIGHTLY_PERMISSION,
    }
)  # fmt: skip
MAX_STANDING_SECONDS = 7 * 86400  # ≤ 7 nights (IC-13)
# §8.8: the only plans a derived approval may cover
STANDING_ALLOWED = (
    {"kind": "experiment", "split": "development", "purpose": "exploratory"},
    {"kind": "calibration"},
    {"kind": "drift", "corpus": "amplai-regression-v1"},
)
STANDING_POLICY_FIELDS = frozenset(
    {"cells", "budget_trials", "shares", "allowed", "max_budget", "valid_from", "valid_until"}
)
BUDGET_LIMITS = (
    "max_wall_seconds", "max_attempts", "max_tokens", "max_cost_microunits",
    "max_parallel_works", "max_delegation_depth",
)  # fmt: skip
CALIBRATION_PLAN_SCHEMA = "amplai.calibration-plan.v1"  # evaluation/calibration.py PLAN_SCHEMA


def nightly_actor(scope: Scope) -> Actor:
    """The service identity the nightly runner acts as (IC-17): exploratory approvals under a
    current standing approval, trial runs and trial-goal approvals, and the mechanical screen of
    class A drafts (IC-30); nothing else."""
    return Actor(NIGHTLY_ID, scope, NIGHTLY_PERMISSIONS, "service", "local-meta-nightly")


def regression_corpus_refs(store: Store, scope: Scope) -> list[dict[str, Any]]:
    """The eval-corpus refs of the frozen regression set ``amplai-regression-v1`` (§10.6), oldest
    first: the ``corpus_ref`` of each revision of its task index ``taskindex-amplai-regression-v1``
    (``corpus_v2.freeze``). The eval-corpus record itself is ``<corpus_id>-<version>``
    (``corpus_v2.py`` ``frozen_id``), so its ``corpus_id`` never equals the set's id."""
    from ...meta_harness.corpus_v2 import REGRESSION_CORPUS_ID

    rows = [
        (ref["revision"], value.get("corpus_ref"))
        for ref, value in store.list_objects(scope, "corpus-task-index")
        if ref["id"] == "taskindex-" + REGRESSION_CORPUS_ID
    ]
    return [dict(c) for _rev, c in sorted(rows, key=lambda r: r[0]) if isinstance(c, dict)]


def epoch_of(value: Any) -> float | None:
    """An ISO-8601 UTC time (``...Z`` or ``+00:00``) as epoch seconds; None when it is not one."""
    from datetime import UTC, datetime

    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC).timestamp()


def validate_nightly_policy(policy: Any) -> None:
    """§8.8: the nightly policy a standing approval binds (RuntimeFault NIGHTLY_POLICY)."""

    def bad(why: str) -> RuntimeFault:
        return RuntimeFault("NIGHTLY_POLICY", why)

    if not isinstance(policy, dict) or set(policy) != STANDING_POLICY_FIELDS:
        raise bad("A nightly policy has exactly the §8.8 fields")
    cells = policy["cells"]
    if not isinstance(cells, list) or not cells or not all(isinstance(c, str) and c for c in cells):
        raise bad("cells is a non-empty list of cell ids")
    if len(set(cells)) != len(cells):
        raise bad("Name each cell once")
    budget_trials = policy["budget_trials"]
    if type(budget_trials) is not int or budget_trials < 0:
        raise bad("budget_trials is a nonnegative integer")
    shares = policy["shares"]
    if (
        not isinstance(shares, dict)
        or set(shares) != {"drift", "screening_design", "search", "confirmation"}
        or not all(type(v) in (int, float) and 0 <= v <= 1 for v in shares.values())
        or abs(shares["drift"] + shares["search"] + shares["confirmation"] - 1) > 1e-9
        or shares["screening_design"] > shares["search"]
    ):
        raise bad("shares: drift + search + confirmation = 1, 0 <= screening_design <= search")
    if policy["allowed"] != [dict(a) for a in STANDING_ALLOWED]:
        raise bad("allowed is exactly the §8.8 list")
    from ...evaluation.calibration import validate_budget

    try:
        validate_budget(policy["max_budget"])
    except RuntimeFault as exc:
        raise bad("max_budget is a $defs.budget") from exc
    start, end = epoch_of(policy["valid_from"]), epoch_of(policy["valid_until"])
    if start is None or end is None or not 0 < end - start <= MAX_STANDING_SECONDS:
        raise bad("valid_from < valid_until, at most 7 nights apart (IC-13)")


class LocalMetaApprovals:
    """``approval_check`` over stored operator approvals (issue, check, revoke)."""

    def __init__(self, store: Store, scope: Scope) -> None:
        self.store, self.scope = store, scope

    def issue(self, operator: Actor, action: str, subject_digest: str) -> dict[str, Any]:
        permission = ACTION_PERMISSION.get(action)
        if permission is None:
            raise Hold("APPROVAL_ACTION", "Unknown meta-harness approval action")
        if action == NIGHTLY_ACTION:  # a standing approval binds its policy (issue_nightly)
            raise Hold("APPROVAL_ACTION", "Issue the nightly standing approval with its policy")
        if operator.kind != "human" or operator.scope != self.scope:
            raise Hold("APPROVAL_HUMAN", "Only the human operator of this scope can approve")
        if operator.subject_id == PROPOSER_ID or "harness.propose" in operator.permissions:
            raise Hold("SELF_APPROVAL", "A proposer identity cannot issue an approval")
        operator.require(permission)
        approval_id = new_id("meta-approval")
        value = {
            "approval_id": approval_id,
            "scope": self.scope.wire(),
            "action": action,
            "subject_digest": subject_digest,
            "approved_by": operator.wire(),
            "approved_at": now(),
            "revoked": False,
        }
        with self.store.tx() as db:
            ref: dict[str, Any] = self.store.put(
                db, self.scope, APPROVAL_KIND, approval_id, 1, value
            )
        return ref

    def revoke(self, operator: Actor, ref: dict[str, Any]) -> None:
        if operator.kind != "human":
            raise Hold("APPROVAL_HUMAN", "Only the human operator of this scope can revoke")
        value = self.store.get(self.scope, APPROVAL_KIND, ref)
        if value.get("action") == NIGHTLY_ACTION:  # IC-17: the standing approval's own permission
            operator.require(NIGHTLY_PERMISSION)
        state = {"revoked": True, "by": operator.wire(), "at": now()}
        with self.store.tx() as db:
            try:
                self.store.cas(
                    db, self.scope, APPROVAL_KIND + "-state", value["approval_id"], 0,
                    "revoked", state,
                )  # fmt: skip
            except Exception as exc:  # already revoked is not an error
                if getattr(exc, "code", "") != "STALE_VERSION":
                    raise

    def _revoked(self, scope: Scope, approval_id: str) -> bool:
        try:
            return bool(
                self.store.head(scope, APPROVAL_KIND + "-state", approval_id)["data"].get("revoked")
            )
        except Exception:
            return False

    def check(self, scope: Scope, ref: dict[str, Any], action: str, subject: str) -> dict[str, Any]:
        """An exact current approval: the operator's, or (IC-17) one the nightly identity derived
        under a standing approval that is still human-issued, unrevoked and inside its dates
        (re-checked here, so at every trial guard)."""
        try:
            value = dict(self.store.get(scope, APPROVAL_KIND, ref))
        except Exception as exc:
            raise Hold("META_APPROVAL", "No stored operator approval for this reference") from exc
        revoked = self._revoked(scope, value["approval_id"])
        approver = value["approved_by"]
        derived = approver.get("kind") != "human"
        if (
            scope != self.scope
            or value["scope"] != self.scope.wire()
            or value["action"] != action
            or value["subject_digest"] != subject
            or action == NIGHTLY_ACTION  # a standing approval is never an action's approval
            or (derived and not (approver.get("subject_id") == NIGHTLY_ID
                                 and approver.get("kind") == "service"
                                 and action == "experiment.execute"
                                 and isinstance(value.get("standing_ref"), dict)))
            or revoked
        ):  # fmt: skip
            raise Hold("META_APPROVAL", "No exact current operator approval for this action")
        if derived:
            self.standing(value["standing_ref"])  # Hold STANDING_APPROVAL
        return value

    # -- IC-17 (provisional): the nightly loop's standing approval -----------------------------
    def issue_nightly(self, operator: Actor, policy: dict[str, Any]) -> dict[str, Any]:
        """The human operator's standing approval (action ``nightly.explore``, permission
        ``nightly.approve``) bound to the digest of the nightly policy (§8.8, ≤ 7 nights). The
        policy itself is kept in the record so that ``issue_standing`` checks plans against it."""
        if operator.kind != "human" or operator.scope != self.scope:
            raise Hold("APPROVAL_HUMAN", "Only the human operator of this scope can approve")
        if operator.subject_id in (PROPOSER_ID, NIGHTLY_ID) or "harness.propose" in (
            operator.permissions
        ):
            raise Hold("SELF_APPROVAL", "A proposer or service identity cannot issue an approval")
        operator.require(NIGHTLY_PERMISSION)
        validate_nightly_policy(policy)
        approval_id = new_id("meta-approval")
        value = {
            "approval_id": approval_id,
            "scope": self.scope.wire(),
            "action": NIGHTLY_ACTION,
            "subject_digest": digest(policy),
            "policy": policy,
            "approved_by": operator.wire(),
            "approved_at": now(),
            "revoked": False,
        }
        with self.store.tx() as db:
            ref: dict[str, Any] = self.store.put(
                db, self.scope, APPROVAL_KIND, approval_id, 1, value
            )
        return ref

    def standing(self, ref: Any) -> dict[str, Any]:
        """The standing approval at ``ref`` when it is human-issued, a ``nightly.explore`` of this
        scope bound to its own policy, unrevoked and inside its dates (by the store clock);
        Hold STANDING_APPROVAL otherwise."""

        def refuse(why: str) -> Hold:
            return Hold("STANDING_APPROVAL", why)

        if not isinstance(ref, dict):
            raise refuse("No standing approval reference")
        try:
            value = dict(self.store.get(self.scope, APPROVAL_KIND, ref))
        except Exception as exc:
            raise refuse("No stored standing approval for this reference") from exc
        policy = value.get("policy")
        if (
            value.get("action") != NIGHTLY_ACTION
            or value.get("scope") != self.scope.wire()
            or (value.get("approved_by") or {}).get("kind") != "human"
            or not isinstance(policy, dict)
            or value.get("subject_digest") != digest(policy)
        ):
            raise refuse("Not a human-issued nightly standing approval of this scope")
        if self._revoked(self.scope, value["approval_id"]):
            raise refuse("The standing approval was revoked")
        start, end = epoch_of(policy.get("valid_from")), epoch_of(policy.get("valid_until"))
        clock = float(self.store.clock())
        if start is None or end is None or not start <= clock < end:
            raise refuse("The standing approval is outside its dates")
        return value

    def current_standing(self) -> dict[str, Any] | None:
        """The newest current standing approval (by ``approved_at``, then id), or None."""
        found = []
        for ref, value in self.store.list_objects(self.scope, APPROVAL_KIND):
            if value.get("action") != NIGHTLY_ACTION:
                continue
            try:
                self.standing(ref)
            except Hold:
                continue
            found.append((str(value.get("approved_at")), ref["id"], ref))
        return max(found, key=lambda row: row[:2])[2] if found else None

    def standing_kind(self, plan: dict[str, Any]) -> tuple[str, list[str]]:
        """(``experiment``|``calibration``|``drift``, the cells it runs) of a plan a derived
        approval may cover (§8.8 ``allowed``); Hold STANDING_APPROVAL for anything else
        (confirmatory, validation or holdout split, an unknown plan shape)."""

        def refuse(why: str, details: object = None) -> Hold:
            return Hold("STANDING_APPROVAL", why, details=details)

        if not isinstance(plan, dict):
            raise refuse("The plan is not a JSON object")
        if plan.get("schema") == CALIBRATION_PLAN_SCHEMA:
            try:
                self.store.get(self.scope, "eval-corpus", plan["corpus_ref"])
            except Exception as exc:
                raise refuse("The calibration plan names no stored corpus") from exc
            cells = plan.get("cells")
            if not isinstance(cells, list):
                raise refuse("The calibration plan names no cells")
            drift = plan["corpus_ref"] in regression_corpus_refs(self.store, self.scope)
            return ("drift" if drift else "calibration"), [str(c) for c in cells]
        if not {"analysis_plan_ref", "sampling_plan_ref", "proposal_ref"} <= set(plan):
            raise refuse("Not an experiment, calibration or drift plan")
        try:
            _, analysis = resolve_ref(self.store, self.scope, plan["analysis_plan_ref"])
            _, sampling = resolve_ref(self.store, self.scope, plan["sampling_plan_ref"])
        except RuntimeFault as exc:
            raise refuse("The experiment's analysis or sampling plan cannot be read") from exc
        purpose = (analysis.get("policy") or {}).get("purpose")
        split = sampling.get("split")
        if purpose != "exploratory" or split != "development":
            raise refuse(
                "Only exploratory experiments on the development split",
                {"purpose": purpose, "split": split},
            )
        cell = sampling.get("cell_id")
        return "experiment", [str(cell)] if isinstance(cell, str) else []

    def issue_standing(
        self, nightly: Actor, standing_ref: dict[str, Any], action: str, plan: dict[str, Any]
    ) -> dict[str, Any]:
        """A derived exact approval issued by the nightly identity (§8.8, IC-17): (1) the standing
        approval is current; (2) the plan is one ``allowed`` kind, on the policy's cells, with a
        budget at most ``max_budget`` field by field; (3) the subject digest is computed from the
        plan itself; (4) ``approved_by`` = the nightly identity and ``standing_ref`` is kept."""

        def refuse(why: str, details: object = None) -> Hold:
            return Hold("STANDING_APPROVAL", why, details=details)

        if (
            nightly.subject_id != NIGHTLY_ID
            or nightly.kind != "service"
            or nightly.scope != self.scope
            or nightly.permissions & NIGHTLY_EXCLUDED
        ):
            raise refuse("Only the nightly service identity derives approvals")
        if action != "experiment.execute":
            raise refuse("The nightly identity derives experiment approvals only",
                          {"action": action})  # fmt: skip
        nightly.require(ACTION_PERMISSION[action])
        standing = self.standing(standing_ref)
        policy = standing["policy"]
        kind, cells = self.standing_kind(plan)
        unknown = sorted(set(cells) - set(policy["cells"]))
        if not cells or unknown:
            raise refuse("The plan runs cells outside the standing policy", {"cells": unknown})
        budget, ceiling = plan.get("budget"), policy["max_budget"]
        if not isinstance(budget, dict):
            raise refuse("The plan has no budget")
        over = [
            k for k in BUDGET_LIMITS
            if type(budget.get(k)) is not int or budget[k] > ceiling[k]
        ]  # fmt: skip
        if over or budget.get("currency") != ceiling["currency"]:
            raise refuse("The plan's budget is above the standing max_budget",
                          {"fields": over or ["currency"]})  # fmt: skip
        subject = {k: v for k, v in plan.items() if k != "approval_ref"}
        approval_id = new_id("meta-approval")
        value = {
            "approval_id": approval_id,
            "scope": self.scope.wire(),
            "action": action,
            "subject_digest": digest(subject),
            "approved_by": nightly.wire(),
            "approved_at": now(),
            "revoked": False,
            "standing_ref": {k: standing_ref[k] for k in ("id", "revision", "digest")},
            "plan_kind": kind,
        }
        with self.store.tx() as db:
            ref: dict[str, Any] = self.store.put(
                db, self.scope, APPROVAL_KIND, approval_id, 1, value
            )
        return ref


def leak_subject(
    store: Store, artifacts: ArtifactStore, scope: Scope, proposal: dict[str, Any]
) -> dict[str, Any]:
    """What the leak gate scans for one proposal (§10.4): the proposal record, its change artifact
    and the content of every component the change brings in (``component_changes[].to``; a
    prompt bundle's role lines) and the ``proposal-prediction`` the change artifact names
    (``prediction_ref``: development task ids only, §9.5, IC-11). Values that cannot be read are
    scanned as absent: the screen's own checks refuse an unreadable change artifact."""
    subject: dict[str, Any] = {
        "proposal": proposal, "change_artifact": None, "components": [], "prediction": None,
    }  # fmt: skip
    try:
        change = json.loads(artifacts.read(scope, proposal["change_artifact"]))
    except (RuntimeFault, KeyError, TypeError, ValueError):
        return subject
    subject["change_artifact"] = change
    prediction_ref = change.get("prediction_ref") if isinstance(change, dict) else None
    if isinstance(prediction_ref, dict):
        try:
            kind, prediction = resolve_ref(store, scope, prediction_ref)
        except RuntimeFault:
            kind, prediction = "", {}
        if kind == "proposal-prediction":
            subject["prediction"] = {
                k: prediction.get(k)
                for k in ("improve_task_ids", "regress_task_ids", "improve_buckets",
                          "regress_buckets", "expected_delta", "risk")
            }  # fmt: skip
    changes = change.get("component_changes") if isinstance(change, dict) else None
    for item in changes if isinstance(changes, list) else []:
        to = item.get("to") if isinstance(item, dict) else None
        if not isinstance(to, dict):
            continue
        try:
            kind, value = resolve_ref(store, scope, to)
        except RuntimeFault:
            continue
        if kind == "harness-component":
            subject["components"].append({"component_id": value.get("component_id"),
                                          "content": value.get("content")})  # fmt: skip
        elif kind == "prompt-bundle":
            subject["components"].append({"bundle_id": value.get("bundle_id"),
                                          "implementer": value.get("implementer")})  # fmt: skip
    return subject


class LocalMeta:
    def __init__(
        self,
        store: Store,
        contracts: Any,
        artifacts: ArtifactStore,
        scope: Scope,
        release_key: Ed25519PublicKey,
    ) -> None:
        self.store, self.contracts, self.artifacts, self.scope = store, contracts, artifacts, scope
        self.approvals = LocalMetaApprovals(store, scope)
        self.proposer = Actor(PROPOSER_ID, scope, PROPOSER_PERMISSIONS, "service",
                              "local-meta-proposer")  # fmt: skip
        # §3.11: opens leak-index records; holds no permission at all
        self.leak_reader = Actor(LEAK_READER_ID, scope, frozenset(), "service",
                                 "local-meta-leak-reader")  # fmt: skip
        # per app: cell id -> installed composition ref (set by LocalMetaOps from its product)
        self.installed_compositions: Callable[[], list[dict[str, dict[str, Any]]]] | None = None
        self.meta = MetaHarness(
            store,
            contracts,
            artifacts,
            approval_check=self.approvals.check,
            trusted_release_keys={RELEASE_KEY_ID: release_key},
            leak_gate=self.leak_findings,
        )
        # No executor policy yet: an experiment cannot run until S4 pins a qualified executor.
        self.evaluation = EvaluationService(
            store,
            contracts,
            artifacts,
            approval_check=self.approvals.check,
            executor_id=EXECUTOR_ID,
            reference_validator=self.reference_validator,
        )

    # -- leak gate (§10.4, §3.9) ---------------------------------------------------------------
    def leak_index_refs(self, scope: Scope) -> list[dict[str, Any]]:
        """The latest revision of every stored leak index of ``scope``."""
        latest: dict[str, dict[str, Any]] = {}
        for ref, _value in self.store.list_objects(scope, LEAK_INDEX_KIND):
            if ref["id"] not in latest or ref["revision"] > latest[ref["id"]]["revision"]:
                latest[ref["id"]] = ref
        return [latest[k] for k in sorted(latest)]

    def leak_findings(self, scope: Scope, proposal: dict[str, Any]) -> list[dict[str, Any]]:
        """The ``leak_gate`` hook of ``MetaHarness.screen``: 3.0.0 findings (``LEAK_GATE``)."""
        from ...meta_harness.leak_gate import LeakGate

        if scope != self.scope:
            raise RuntimeFault("SCOPE_MISMATCH", "The leak gate serves one scope")
        refs = self.leak_index_refs(scope)
        if not refs:
            return []
        subject = leak_subject(self.store, self.artifacts, scope, proposal)
        findings: list[dict[str, Any]] = []
        for ref in refs:
            findings += LeakGate(self.leak_reader, self.store, ref).findings(scope, subject)
        return findings

    # -- the reference arm (§7.4) --------------------------------------------------------------
    def reference_validator(
        self, scope: Scope, plan: dict[str, Any], policy: dict[str, Any]
    ) -> None:
        """Hold REFERENCE_ARM unless the declared reference is a budget-matched best-of-n of the
        baseline: pinnable for the baseline's cell, no other component and no other composition
        field changed, and ``execution_strategy`` = ``best_of_n`` with the declared n (§7.4)."""
        from ...meta_harness.components import ComponentService
        from ...meta_harness.manifest import ManifestService
        from . import releases

        def refuse(why: str, details: object = None) -> Hold:
            return Hold("REFERENCE_ARM", why, details=details)

        arm = policy.get("reference_arm")
        if not isinstance(arm, dict) or not isinstance(arm.get("composition_ref"), dict):
            raise refuse("No reference arm is declared")
        n = arm.get("n")
        if type(n) is not int or not 1 <= n <= 3:
            raise refuse("A best-of-n reference has 1 <= n <= 3 (IC-06)")
        reference, baseline = arm["composition_ref"], plan["baseline_ref"]
        if scope != self.scope:
            raise refuse("The reference validator serves one scope")
        if self.installed_compositions is None:
            raise refuse("No installed compositions to pin the reference against")
        cells = None
        for compositions in self.installed_compositions():
            base_cell = releases.pin_allowed(self.store, scope, compositions, baseline)
            if base_cell is not None:
                cells = (
                    base_cell,
                    releases.pin_allowed(self.store, scope, compositions, reference),
                )
                break
        if cells is None:
            raise refuse("The baseline is not a pinnable composition of an installed cell")
        if cells[1] is None:
            raise refuse("The reference is not pinnable")
        if cells[1] != cells[0]:
            raise refuse("The reference belongs to another cell", {"cells": list(cells)})
        manifests = ManifestService(
            self.store, scope, self.contracts, ComponentService(self.store, scope)
        )
        try:
            base_manifest = manifests.of_composition(baseline)
            ref_manifest = manifests.of_composition(reference)
            base_value = self.store.get(scope, "harness-composition", baseline)
            ref_value = self.store.get(scope, "harness-composition", reference)
        except (RuntimeFault, KeyError, TypeError) as exc:
            code = getattr(exc, "code", type(exc).__name__)
            raise refuse("The compositions cannot be read", {"code": code}) from exc
        other = sorted(
            c.slot for c in manifests.diff(base_manifest, ref_manifest)
            if c.slot not in REFERENCE_SLOTS
        )  # fmt: skip
        if other:
            raise refuse("The reference changes other components", {"slots": other})
        fields = sorted(
            k for k in set(base_value) | set(ref_value)
            if k not in IDENTITY_FIELDS | CARRIER_FIELDS and base_value.get(k) != ref_value.get(k)
        )  # fmt: skip
        if fields:
            raise refuse("The reference changes other composition fields", {"fields": fields})
        strategy_ref = ref_manifest.budget.get("execution_strategy")
        try:
            content = (
                ComponentService(self.store, scope).get(strategy_ref)["content"]
                if strategy_ref is not None
                else None
            )
        except (RuntimeFault, KeyError, TypeError) as exc:
            raise refuse("The reference's execution strategy cannot be read") from exc
        params = (content or {}).get("params") or {}
        if (
            not isinstance(content, dict)
            or content.get("enabled") != ["best_of_n"]
            or (params.get("best_of_n") or {}).get("n") != n
        ):
            raise refuse(
                "The reference's execution strategy is not best_of_n with the declared n",
                {"n": n},
            )
