#!/usr/bin/env python3
"""Host-edge bridge from a Control Plane request to one Project Store Work.

This module deliberately owns the only dependency on the Loop Kit runtime.
It creates DRAFT Work only; human activation remains a separate authority.
"""

from __future__ import annotations

import hashlib
import subprocess
from typing import TYPE_CHECKING

from amplai_runtime import ConflictError, NotFoundError, ProjectStore

if TYPE_CHECKING:
    from amplai_foundry.control_plane.models import OrchestrationRequest


def _require_store_project(store: ProjectStore, project_id: str) -> None:
    """Reject composition when an authority scope names another Project Store."""
    if str(store.project.get("project_id")) != project_id:
        raise ValueError("PROJECT_STORE_PROJECT_MISMATCH")


class ProjectStoreOrchestrationGateway:
    """Resolve one durable request into one pinned, inactive Work graph."""

    def __init__(self, store: ProjectStore, *, source_app: str, contract_ref: str) -> None:
        self.store = store
        self.source_app = source_app
        self.contract_ref = contract_ref

    def ensure_draft_work(self, request: OrchestrationRequest) -> str:
        from amplai_foundry.control_plane.orchestration import ProjectStoreResolutionHold

        if str(self.store.project.get("project_id")) != request.project_id:
            # A Control Plane tenant/project boundary must never select an
            # arbitrary local Project Store merely because its host command was
            # configured with the wrong path.
            raise ProjectStoreResolutionHold("PROJECT_STORE_PROJECT_MISMATCH")
        for existing in self.store.list_work():
            if existing.get("request_ref") == request.request_id:
                return str(existing["work_id"])
        target_app = request.target_app_hint
        if not target_app:
            raise ProjectStoreResolutionHold("TARGET_APP_REQUIRED")
        binding = self.store.get_local_app(target_app, required=False)
        if binding is None:
            raise ProjectStoreResolutionHold("TARGET_APP_NOT_REGISTERED")
        runner_profile = request.runner_hint or binding.get("default_runner_profile")
        if not runner_profile:
            raise ProjectStoreResolutionHold("RUNNER_PROFILE_REQUIRED")
        if runner_profile not in (binding.get("runner_profiles") or {}):
            legacy = binding.get("runner") or {}
            if legacy.get("type") != runner_profile:
                raise ProjectStoreResolutionHold("RUNNER_PROFILE_UNRESOLVED")
        base_ref = self._base_ref(binding["repo_path"])
        change_id = "CR-ORCH-" + hashlib.sha256(request.request_id.encode("utf-8")).hexdigest()[:16]
        try:
            self.store.create_change(
                "Slack orchestration request " + request.request_id,
                request.goal,
                self.source_app,
                affected_apps=[self.source_app, target_app],
                contract_refs=[self.contract_ref],
                change_id=change_id,
                actor="orchestration-bridge",
            )
        except ConflictError:
            # The deterministic ID is the request's CR uniqueness key. A retry
            # must reuse it, never create a sibling graph.
            self.store.get_change(change_id)
        self.store.activate_change(change_id, actor="orchestration-bridge")
        try:
            work = self.store.create_work(
                change_id,
                target_app,
                request.goal,
                source_app=self.source_app,
                acceptance=["Human activation is required before execution."],
                contract_refs=[self.contract_ref],
                controller=request.controller,
                runner_profile=runner_profile,
                base_ref=base_ref,
                request_ref=request.request_id,
                actor="orchestration-bridge",
            )
        except NotFoundError as error:
            raise ProjectStoreResolutionHold("TARGET_APP_NOT_REGISTERED") from error
        return str(work["work_id"])

    @staticmethod
    def _base_ref(repo_path: str) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", repo_path, "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as error:
            from amplai_foundry.control_plane.orchestration import ProjectStoreResolutionHold

            raise ProjectStoreResolutionHold("TARGET_BASE_REF_UNAVAILABLE") from error
        base_ref = result.stdout.strip()
        if len(base_ref) != 40 or any(char not in "0123456789abcdef" for char in base_ref):
            from amplai_foundry.control_plane.orchestration import ProjectStoreResolutionHold

            raise ProjectStoreResolutionHold("TARGET_BASE_REF_UNAVAILABLE")
        return base_ref


class ProjectStoreWorkActivationGateway:
    """Host-edge CAS adapter for the activation domain port.

    The expected revision and sealed content digest travel to ``activate_work``;
    Project Store checks both while holding its lock.  Reading a card and
    activating it are therefore never a time-of-check/time-of-use mutation.
    """

    def __init__(self, store: ProjectStore, *, project_id: str | None = None) -> None:
        self.store = store
        self.project_id = project_id
        if project_id is not None:
            _require_store_project(store, project_id)

    def get(self, work_id: str):
        from amplai_foundry.governance.work_activation import WorkActivationSnapshot

        work = self.store.get_work(work_id)
        return WorkActivationSnapshot(
            work_id=str(work["work_id"]),
            state=str(work["status"]),
            revision=int(work.get("revision", 1)),
            digest=str(work["content_hash"]),
        )

    def activate(
        self,
        work_id: str,
        *,
        actor_id: str,
        expected_revision: int,
        expected_digest: str,
    ):
        from amplai_foundry.governance.work_activation import (
            WorkActivationError,
            WorkActivationSnapshot,
        )

        try:
            work = self.store.activate_work(
                work_id,
                actor="activation:" + actor_id,
                expected_revision=expected_revision,
                expected_digest=expected_digest,
            )
        except ConflictError as error:
            raise WorkActivationError("WORK_STALE") from error
        return WorkActivationSnapshot(
            work_id=str(work["work_id"]),
            state=str(work["status"]),
            revision=int(work.get("revision", 1)),
            digest=str(work["content_hash"]),
        )


class ProjectStoreActivationCardScheduler:
    """Queue a token-free card intent for an operator-configured Slack approver.

    The recipient is deployment configuration, not Hermes request data.  It is
    resolved through the normal Actor binding before a durable card intent is
    created; the later button click remains the only state-changing authority.
    """

    def __init__(
        self,
        store: ProjectStore,
        *,
        card_outbox,
        authority_service,
        project_ref,
        provider_installation_ref: str,
        recipient_external_actor_id: str,
        feature: str,
    ) -> None:
        self.store = store
        self.card_outbox = card_outbox
        self.authority_service = authority_service
        self.project_ref = project_ref
        self.provider_installation_ref = provider_installation_ref
        self.recipient_external_actor_id = recipient_external_actor_id
        self.feature = feature
        _require_store_project(store, project_ref.project_id)

    def schedule(self, *, request: OrchestrationRequest, work_ref: str) -> str:
        from amplai_foundry.governance.authority import DirectAuthorityRequest
        from amplai_foundry.governance.models import ChannelProvider, ChannelRef
        from amplai_foundry.governance.work_activation import WorkActivationScope

        if request.project_id != self.project_ref.project_id:
            raise ValueError("PROJECT_STORE_PROJECT_MISMATCH")
        route = request.reply_route
        channel = ChannelRef(
            provider=ChannelProvider.SLACK,
            workspace_id=str(route["workspace_id"]),
            channel_id=str(route["channel_id"]),
            thread_id=str(route["thread_id"]) if route.get("thread_id") else None,
            message_id=request.request_id,
        )
        authority = self.authority_service.authenticate(
            DirectAuthorityRequest(
                provider=ChannelProvider.SLACK,
                provider_installation_ref=self.provider_installation_ref,
                external_actor_id=self.recipient_external_actor_id,
                project_ref=self.project_ref,
                request_id=f"activation-card:{request.request_id}",
                channel=channel,
            )
        )
        intent = self.card_outbox.enqueue(
            snapshot=ProjectStoreWorkActivationGateway(
                self.store, project_id=self.project_ref.project_id
            ).get(work_ref),
            scope=WorkActivationScope(self.project_ref, ChannelProvider.SLACK, self.feature),
            authority=authority,
        )
        return intent.idempotency_key


class ProjectStoreWorkReader:
    """Read-only authoritative Work view used by the Control Plane HTTP edge."""

    def __init__(self, store: ProjectStore) -> None:
        self.store = store

    def get_work(self, work_id: str) -> dict[str, object]:
        work = self.store.get_work(work_id)
        result = work.get("result")
        if not isinstance(result, dict):
            result = {}
        return {
            "work_id": work["work_id"],
            "change_id": work["change_id"],
            "status": work["status"],
            "revision": int(work.get("revision", 1)),
            "request_ref": work.get("request_ref"),
            "attempt": int(work.get("attempts") or 0),
            "evidence_refs": list(result.get("evidence_refs") or work.get("evidence_refs") or []),
            "next_human_action": work.get("human_gate"),
        }
