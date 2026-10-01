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
from ..contracts.identity import new_id, now
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
    }
)
PROPOSER_PERMISSIONS = frozenset({"harness.propose"})


class LocalMetaApprovals:
    """``approval_check`` over stored operator approvals (issue, check, revoke)."""

    def __init__(self, store: Store, scope: Scope) -> None:
        self.store, self.scope = store, scope

    def issue(self, operator: Actor, action: str, subject_digest: str) -> dict[str, Any]:
        permission = ACTION_PERMISSION.get(action)
        if permission is None:
            raise Hold("APPROVAL_ACTION", "Unknown meta-harness approval action")
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

    def check(self, scope: Scope, ref: dict[str, Any], action: str, subject: str) -> dict[str, Any]:
        try:
            value = dict(self.store.get(scope, APPROVAL_KIND, ref))
        except Exception as exc:
            raise Hold("META_APPROVAL", "No stored operator approval for this reference") from exc
        try:
            revoked = bool(
                self.store.head(scope, APPROVAL_KIND + "-state", value["approval_id"])["data"].get(
                    "revoked"
                )
            )
        except Exception:
            revoked = False
        if (
            scope != self.scope
            or value["scope"] != self.scope.wire()
            or value["action"] != action
            or value["subject_digest"] != subject
            or value["approved_by"]["kind"] != "human"
            or revoked
        ):
            raise Hold("META_APPROVAL", "No exact current operator approval for this action")
        return value


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
