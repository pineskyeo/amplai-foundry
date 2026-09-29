"""The meta-harness on the local product (Work 030 S3, D-089).

MetaHarness and EvaluationService share the product's store. The meta-proposer is a service
identity that can only propose; the human operator is the reviewer. Approvals of an experiment, a
canary, a promotion or a rollback are durable operator-issued objects that name their action and
the exact subject they approve, so nothing lives only in memory and a proposer can never issue one.
"""

from __future__ import annotations

from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ...evaluation.service import EvaluationService
from ...meta_harness.service import MetaHarness
from ..contracts.authority import Actor
from ..contracts.identity import new_id, now
from ..errors import Hold
from ..evidence.cas import ArtifactStore
from ..storage.store import Scope, Store

APPROVAL_KIND = "meta-approval"
PROPOSER_ID = "amplai-meta-proposer"
EXECUTOR_ID = "local-offline-executor"
RELEASE_KEY_ID = "local-authority"

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


class LocalMeta:
    def __init__(
        self,
        store: Store,
        contracts: Any,
        artifacts: ArtifactStore,
        scope: Scope,
        release_key: Ed25519PublicKey,
    ) -> None:
        self.approvals = LocalMetaApprovals(store, scope)
        self.proposer = Actor(PROPOSER_ID, scope, PROPOSER_PERMISSIONS, "service",
                              "local-meta-proposer")  # fmt: skip
        self.meta = MetaHarness(
            store,
            contracts,
            artifacts,
            approval_check=self.approvals.check,
            trusted_release_keys={RELEASE_KEY_ID: release_key},
        )
        # No executor policy yet: an experiment cannot run until S4 pins a qualified executor.
        self.evaluation = EvaluationService(
            store,
            contracts,
            artifacts,
            approval_check=self.approvals.check,
            executor_id=EXECUTOR_ID,
        )
