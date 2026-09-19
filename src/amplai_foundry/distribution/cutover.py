"""Authorized cutover and independent release receipts (design/19 §5-§6, design/20 §3, V3-062).

Cutover is fail-closed: without a signed, approved ReleaseSet plus an explicit human
release decision the operation returns HOLD and changes nothing. Platform and Kit keep
separate version truth; a target-by-target status is reported and partial success is
never rolled up to "complete". Rollback goes through the meta-harness path, which keeps
the latest revocations (T-090) and refuses while effects are unknown.
"""

from __future__ import annotations

from typing import Any

from amplai_foundry.runtime.contracts.authority import Actor
from amplai_foundry.runtime.contracts.identity import digest, new_id, now, verify_signature
from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.runtime.storage.store import Store

Ref = dict[str, Any]


class CutoverService:
    def __init__(
        self,
        store: Store,
        contracts: Any,
        *,
        approval_check: Any,
        trusted_release_keys: dict[str, Any],
        installer: Any | None = None,
    ) -> None:
        self.store, self.contracts = store, contracts
        self.approval_check, self.keys, self.installer = (
            approval_check,
            trusted_release_keys,
            installer,
        )

    # -- release set ------------------------------------------------------------------
    def _release(self, actor: Actor, ref: Ref) -> dict[str, Any]:
        release = self.store.get(actor.scope, "release-set", ref)
        self.contracts.validate("release-set", release)
        verify_signature(release, self.keys)
        if release["status"] != "approved":
            raise Hold("RELEASE_NOT_APPROVED", "A signature alone is not release approval")
        from amplai_foundry.runtime.contracts.semantics import check_refs, resolve_ref

        check_refs(self.store, actor.scope, release)
        _, matrix = resolve_ref(self.store, actor.scope, release["qualification_matrix_ref"])
        if matrix.get("status") != "pass":
            raise Hold("RELEASE_QUALIFICATION", "Release qualification has not passed")
        return release

    # -- version truth -------------------------------------------------------------------
    @staticmethod
    def version_truth(
        release: dict[str, Any], *, installed_platform: str | None, installed_kit: str | None
    ) -> dict[str, Any]:
        """Platform and Kit are independent trains; equal numbers imply nothing (design/01 §3)."""
        return {
            "platform": {
                "release": release["platform_version"],
                "installed": installed_platform,
                "matches": installed_platform == release["platform_version"],
            },
            "kit": {
                "release": release["kit_version"],
                "installed": installed_kit,
                "matches": installed_kit == release["kit_version"],
            },
            "protocol_major": release["protocol_major"],
            "coupled": False,
        }

    # -- cutover -----------------------------------------------------------------------
    def cutover(
        self,
        actor: Actor,
        release_ref: Ref,
        *,
        targets: list[dict[str, Any]],
        human_decision_ref: Ref | None,
        expected_active_ref: Ref | None,
    ) -> dict[str, Any]:
        actor.require("release.promote")
        if human_decision_ref is None:
            raise Hold(
                "CUTOVER_HUMAN_GATE",
                "Production cutover needs an explicit human release decision; nothing changed",
                details={"gate": "production_operation"},
            )
        release = self._release(actor, release_ref)
        subject = digest(
            {
                "release_ref": release_ref,
                "targets": targets,
                "expected_active_ref": expected_active_ref,
            }
        )
        self.approval_check(actor.scope, human_decision_ref, "release.cutover", subject)
        if not targets:
            raise RuntimeFault(
                "CUTOVER_TARGETS", "Cutover must name exactly the authorized target set"
            )
        with self.store.tx() as db:
            try:
                active = self.store.head(actor.scope, "release-pointer", "active", db=db)
            except RuntimeFault as exc:
                if exc.code != "NOT_FOUND":
                    raise
                active = {"row_version": 0, "data": {"release_ref": None}}
            if active["data"].get("release_ref") != expected_active_ref:
                from amplai_foundry.runtime.errors import Conflict

                raise Conflict("RELEASE_CAS", "Active release changed; re-evaluate cutover")
            unknown = db.execute(
                "SELECT COUNT(*) FROM heads WHERE tenant=? AND project=? AND kind='effect' "
                "AND state IN ('prepared','dispatched','unknown')",
                actor.scope.keys(),
            ).fetchone()[0]
            if unknown:
                raise Hold("CUTOVER_UNKNOWN_EFFECT", "Reconcile external effects before cutover")
            self.store.cas(
                db,
                actor.scope,
                "release-pointer",
                "active",
                active["row_version"],
                "active",
                {
                    "release_ref": release_ref,
                    "previous_ref": expected_active_ref,
                    "target_binding_refs": [t.get("binding_ref") for t in targets],
                },
            )
            self.store.event(
                db,
                actor.scope,
                "release",
                release["release_id"],
                "release.cutover",
                {"release_ref": release_ref, "decision_ref": human_decision_ref},
            )
            # R102: the receipt draft commits with the pointer switch, so a crash or IO error
            # during target installation leaves a visible "pointer_switched" partial receipt.
            receipt_id = new_id("cutover")
            draft = {
                "schema_version": "3.0.0",
                "receipt_id": receipt_id,
                "release_ref": release_ref,
                "previous_active_ref": expected_active_ref,
                "decision_ref": human_decision_ref,
                "targets": [{"target": t.get("id"), "status": "pending"} for t in targets],
                "overall": "pointer_switched",
                "issued_by": actor.subject_id,
                "issued_at": now(),
                "authority_changed": False,
                "rollback": "meta-harness rollback keeps latest revocations; not a backup restore",
            }
            self.store.put(db, actor.scope, "cutover-receipt", receipt_id, 1, draft)
        per_target: list[dict[str, Any]] = []
        for target in targets:
            item: dict[str, Any] = {"target": target.get("id"), "status": "pointer_switched"}
            if (
                self.installer is not None
                and target.get("root")
                and target.get("bundle") is not None
            ):
                try:
                    plan = self.installer.plan(target["root"], target["bundle"])
                    receipt = self.installer.apply(actor, target["root"], target["bundle"], plan)
                    item.update(status="installed", receipt_id=receipt["receipt_id"])
                except (Hold, RuntimeFault) as exc:
                    item.update(status="failed", code=exc.code, message=exc.message)
                except Exception as exc:  # OSError and friends: IO failure, never hidden
                    item.update(status="failed", code="INSTALL_IO", message=type(exc).__name__)
            per_target.append(item)
        failed = [t for t in per_target if t["status"] == "failed"]
        receipt = {
            **draft,
            "targets": per_target,
            "overall": "partial" if failed else "complete",
            "completed_at": now(),
        }
        with self.store.tx() as db:
            self.store.put(db, actor.scope, "cutover-receipt", receipt_id, 2, receipt)
        return receipt

    def status(self, actor: Actor) -> dict[str, Any]:
        actor.require("runtime.read")
        try:
            active = self.store.head(actor.scope, "release-pointer", "active")["data"]
        except RuntimeFault:
            active = {"release_ref": None}
        latest: dict[str, dict[str, Any]] = {}
        for ref, value in self.store.list_objects(actor.scope, "cutover-receipt"):
            latest[ref["id"]] = value  # ordered by id,revision: last write is the newest
        receipts = list(latest.values())
        return {
            "active_release_ref": active.get("release_ref"),
            "cutovers": len(receipts),
            "last": receipts[-1] if receipts else None,
        }
