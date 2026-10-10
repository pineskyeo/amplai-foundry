"""Authenticated V3 API over the same deterministic services used by the CLI.

No HTTP body can supply an Actor, a permission set, process-stop evidence or an
approval result. Authentication and live Foundry authorization are server adapters.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from amplai_foundry.evaluation.observatory import Observatory
from amplai_foundry.runtime.contracts.authority import Actor
from amplai_foundry.runtime.contracts.identity import canonical, digest, now
from amplai_foundry.runtime.contracts.intake import is_intake
from amplai_foundry.runtime.contracts.semantics import check_refs
from amplai_foundry.runtime.errors import Conflict, Hold, RuntimeFault
from amplai_foundry.runtime.execution.steering import SteeringService
from amplai_foundry.runtime.storage.store import Store


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class IntentBody(Body):
    text: str = Field(min_length=1, max_length=65536)
    mode: Literal["work", "design"] = "work"
    target_hints: list[str] = Field(default_factory=list, max_length=64)
    attachment_refs: list[dict[str, Any]] = Field(default_factory=list, max_length=64)
    classification: Literal["public", "internal", "confidential", "restricted"] = "internal"
    external_message_id: str | None = None


class RefBody(Body):
    ref: dict[str, Any]


class ContractBody(Body):
    contract: dict[str, Any]


class GraphBody(Body):
    graph: dict[str, Any]
    contract_ref: dict[str, Any]


class ActivateBody(Body):
    contract_ref: dict[str, Any]
    graph_ref: dict[str, Any]
    grant_ref: dict[str, Any]
    profile: dict[str, Any]


class SteeringBody(Body):
    kind: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=65536)
    expected_contract_ref: dict[str, Any]
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list, max_length=64)
    priority: int | None = Field(default=None, ge=-100, le=100)


class ClaimBody(Body):
    goal_id: str | None = None


class LeaseBody(Body):
    lease_id: str
    fence: int = Field(ge=1)


class HeartbeatBody(LeaseBody):
    sequence: int = Field(ge=1)


class EventBody(HeartbeatBody):
    message_id: str
    event_type: str
    payload: dict[str, Any]


class StartBody(Body):
    dispatch: dict[str, Any]
    session_handle: str = Field(min_length=1, max_length=4096)


class VerifyBody(Body):
    acceptance_id: str
    subject: dict[str, Any]


class RegistryBody(Body):
    value: dict[str, Any]
    object_id: str
    revision: int = Field(ge=1)


class AnswerBody(Body):
    question_ref: dict[str, Any]
    text: str = Field(min_length=1, max_length=65536)
    contract_ref: dict[str, Any]


class MetaApprovalBody(Body):
    approval_ref: dict[str, Any]
    experiment_ref: dict[str, Any]


class CanaryApprovalBody(Body):
    approval_ref: dict[str, Any]
    policy_ref: dict[str, Any]


class PromotionBody(Body):
    plan: dict[str, Any]


class RollbackBody(Body):
    target_ref: dict[str, Any]
    expected_active_ref: dict[str, Any]
    approval_ref: dict[str, Any]


class ExperimentRunBody(RefBody):
    split: Literal["development", "validation", "holdout"] = "validation"


class CorpusBody(Body):
    corpus_id: str = Field(min_length=1, max_length=256)
    cases: list[dict[str, Any]] = Field(min_length=1, max_length=100000)
    holdout_use_limit: int = Field(ge=1, le=1000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReasonBody(Body):
    reason: str = Field(min_length=1, max_length=4096)


class CanaryTrialBody(Body):
    task_id: str = Field(min_length=1, max_length=256)


class KillBody(Body):
    enabled: bool
    reason: str = Field(min_length=1, max_length=4096)


class BearerAuthenticator:
    """Hash-only server token directory with live authorization on EVERY request."""

    def __init__(self, bindings: dict[str, Any], resolve_actor: Callable[[Any], Actor]):
        if not bindings or any(len(k) != 64 for k in bindings):
            raise ValueError("Nonempty SHA-256 token bindings are required")
        self.bindings, self.resolve_actor = dict(bindings), resolve_actor

    def __call__(self, authorization: str) -> Actor:
        if not authorization.startswith("Bearer ") or not 32 <= len(authorization[7:]) <= 4096:
            raise RuntimeFault("UNAUTHENTICATED", "A valid server-bound credential is required")
        hashed = hashlib.sha256(authorization[7:].encode()).hexdigest()
        # Constant-time comparison; permission/scope data comes from the server only.
        binding = next(
            (v for k, v in self.bindings.items() if hmac.compare_digest(k, hashed)), None
        )
        if binding is None:
            raise RuntimeFault("UNAUTHENTICATED", "A valid server-bound credential is required")
        actor = self.resolve_actor(binding)
        if not isinstance(actor, Actor):
            raise RuntimeFault("AUTHENTICATOR_ERROR", "Authenticator did not resolve an Actor")
        return actor


@dataclass
class ApiServices:
    runtime: Any
    goals: Any
    authenticate: Callable[[str], Actor]
    verification: Any = None
    meta: Any = None
    planning: Any = None
    planning_context: Callable[..., Any] | None = None
    # Collector is a local worker/qualified supervisor, not a request payload.
    worker_collector: Callable[..., Any] | None = None
    global_checker: Callable[..., Any] | None = None
    doctor: Callable[..., Any] | None = None
    object_read_guard: Callable[..., Any] | None = None
    artifact_read_guard: Callable[..., Any] | None = None
    evaluation: Any = None
    # Installed server executors only. Clients cannot send commands, counters,
    # callback code, approval results or fabricated trial outcomes.
    eval_executor: Callable[..., Any] | None = None
    canary_executor: Callable[..., Any] | None = None


class ApiCommands:
    """Durable ingress receipts, including the 'outcome unknown' crash window.

    A repeated in-flight command is not blindly re-executed. Domain commands are
    independently transactional/idempotent; operator reconciliation can inspect
    their receipts after an interrupted HTTP response.
    """

    def __init__(self, store: Store) -> None:
        self.store = store

    def run(
        self,
        actor: Actor,
        key: str,
        route: str,
        payload: Any,
        operation: Callable[[], Any],
    ) -> Any:
        if not key or len(key) > 256:
            raise RuntimeFault("IDEMPOTENCY_REQUIRED", "Idempotency-Key is required")
        identity = digest({"actor": actor.subject_id, "key": key})[7:]
        fingerprint = digest({"route": route, "payload": payload})
        with self.store.tx() as db:
            try:
                old = self.store.head(actor.scope, "api-command", identity, db=db)
            except RuntimeFault as exc:
                if exc.code != "NOT_FOUND":
                    raise
                old = None
            if old:
                if old["data"]["fingerprint"] != fingerprint:
                    raise Conflict("IDEMPOTENCY_CONFLICT", "Key was used with a different request")
                if old["state"] == "completed":
                    return old["data"]["result"]
                if old["state"] == "rejected":
                    err = old["data"]["error"]
                    raise RuntimeFault(err["code"], err["message"], outcome=err["outcome"])
                raise Hold(
                    "COMMAND_OUTCOME_UNKNOWN",
                    "The command is in flight or requires receipt reconciliation; "
                    "it was not repeated",
                )
            self.store.cas(
                db,
                actor.scope,
                "api-command",
                identity,
                0,
                "running",
                {
                    "fingerprint": fingerprint,
                    "route": route,
                    "actor": actor.subject_id,
                    "started_at": now(),
                    "owner_epoch": self.store.epoch,
                },
            )
        try:
            result = operation()
            canonical(result)
        except RuntimeFault as exc:
            with self.store.tx() as db:
                h = self.store.head(actor.scope, "api-command", identity, db=db)
                self.store.cas(
                    db,
                    actor.scope,
                    "api-command",
                    identity,
                    h["row_version"],
                    "rejected",
                    {
                        **h["data"],
                        "error": {"code": exc.code, "message": exc.message, "outcome": exc.outcome},
                    },
                )
            raise
        with self.store.tx() as db:
            h = self.store.head(actor.scope, "api-command", identity, db=db)
            self.store.cas(
                db,
                actor.scope,
                "api-command",
                identity,
                h["row_version"],
                "completed",
                {**h["data"], "result": result},
            )
        return result


class BoundedBody:
    """Bound memory before JSON parsing, including chunked requests."""

    def __init__(self, app: Any, maximum: int = 4 * 1024 * 1024) -> None:
        self.app, self.maximum = app, maximum

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        chunks, count = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            count += len(message.get("body", b""))
            if count > self.maximum:
                response = JSONResponse({"code": "BODY_TOO_LARGE"}, status_code=413)
                return await response(scope, receive, send)
            chunks.append(message)
            if not message.get("more_body", False):
                break

        async def replay() -> Any:
            return chunks.pop(0) if chunks else await receive()

        await self.app(scope, replay, send)


def create_app(services: ApiServices) -> FastAPI:
    runtime, goals, store = services.runtime, services.goals, services.runtime.store
    app = FastAPI(title="AMPLAI V3", version="3.0.0", docs_url=None, redoc_url=None)
    app.add_middleware(BoundedBody)
    commands, steering = ApiCommands(store), SteeringService(runtime)

    @app.exception_handler(RuntimeFault)
    async def fault_handler(request: Request, exc: RuntimeFault) -> Any:
        status = 409 if isinstance(exc, Conflict) else 423 if exc.outcome == "hold" else 400
        if exc.code == "UNAUTHENTICATED":
            status = 401
        if exc.code == "FORBIDDEN":
            status = 403
        if exc.code == "NOT_FOUND":
            status = 404
        return JSONResponse(
            {"code": exc.code, "message": exc.message, "outcome": exc.outcome}, status_code=status
        )

    def actor(authorization: str = Header(default="")) -> Actor:
        return services.authenticate(authorization)

    def key(idempotency_key: str = Header(default="", alias="Idempotency-Key")) -> str:
        if not idempotency_key or len(idempotency_key) > 256:
            raise RuntimeFault("IDEMPOTENCY_REQUIRED", "Idempotency-Key is required")
        return idempotency_key

    def version(if_match: str = Header(default="", alias="If-Match")) -> int:
        raw = if_match.strip('"')
        if not raw.isdigit() or int(raw) < 1:
            raise RuntimeFault(
                "IF_MATCH_REQUIRED", "Use the exact row_version from the latest scoped read"
            )
        return int(raw)

    def reader(a: Actor) -> None:
        a.require("runtime.read")
        if is_intake(a):  # these reads are not filtered to the linked operator's goals (AC-H3)
            raise RuntimeFault(
                "FORBIDDEN", "A front agent reads its operator's goals through the intake routes"
            )

    def require_service(value: Any, name: str) -> Any:
        if value is None:
            raise Hold(
                "SERVICE_NOT_CONFIGURED", name + " is not qualified/configured in this deployment"
            )
        return value

    def command(a: Actor, k: str, route: str, body: Any, operation: Callable[[], Any]) -> Any:
        return commands.run(
            a, k, route, body.model_dump() if isinstance(body, BaseModel) else body, operation
        )

    @app.get("/healthz")
    def health() -> Any:
        return {
            "service": "amplai-foundry",
            "version": "3.0.0",
            "liveness": "up",
            "deployment_qualified": False,
        }

    @app.get("/api/v3/readyz")
    def ready(a: Actor = Depends(actor)) -> Any:
        a.require("runtime.read")
        result = (
            services.doctor()
            if services.doctor
            else {"status": "hold", "reason": "No deployment qualification report configured"}
        )
        return JSONResponse(result, status_code=200 if result.get("status") == "pass" else 503)

    @app.post("/api/v3/intents", status_code=202)
    def submit(body: IntentBody, a: Actor = Depends(actor), k: str = Depends(key)) -> Any:
        a.require("goal.submit")
        return command(
            a,
            k,
            "intent.submit",
            body,
            lambda: goals.submit(
                a,
                text=body.text,
                mode=body.mode,
                target_hints=body.target_hints,
                attachment_refs=body.attachment_refs,
                classification=body.classification,
                channel="api",
                external_message_id=body.external_message_id,
                key="api:" + k,
            ),
        )

    @app.get("/api/v3/goals")
    def list_goals(a: Actor = Depends(actor)) -> Any:
        reader(a)
        with store._lock:
            rows = store.conn.execute(
                "SELECT id,state,row_version FROM heads WHERE tenant=? AND project=? "
                "AND kind='goal' ORDER BY id LIMIT 1000",
                a.scope.keys(),
            ).fetchall()
        return {"items": [dict(r) for r in rows], "limit": 1000, "scope": a.scope.wire()}

    @app.get("/api/v3/goals/{goal_id}")
    def get_goal(goal_id: str, a: Actor = Depends(actor)) -> Any:
        reader(a)
        value = store.head(a.scope, "goal", goal_id)
        return JSONResponse(
            {"goal_id": goal_id, **value, "budget": runtime.budgets.totals(a.scope, goal_id)},
            headers={"ETag": f'"{value["row_version"]}"'},
        )

    @app.post("/api/v3/goals/{goal_id}/plan")
    def plan(
        goal_id: str, a: Actor = Depends(actor), k: str = Depends(key), v: int = Depends(version)
    ) -> Any:
        a.require("goal.resolve")

        def execute() -> Any:
            if store.head(a.scope, "goal", goal_id)["row_version"] != v:
                raise Conflict("STALE_VERSION", "Goal changed before discovery")
            ctx = require_service(services.planning_context, "Context resolver")(a, goal_id)
            return require_service(services.planning, "Planning service").plan(a, goal_id, **ctx)

        return command(a, k, "goal.plan:" + goal_id, {"expected_version": v}, execute)

    @app.post("/api/v3/goals/{goal_id}/contracts")
    def freeze(
        goal_id: str,
        body: ContractBody,
        a: Actor = Depends(actor),
        k: str = Depends(key),
        v: int = Depends(version),
    ) -> Any:
        a.require("contract.propose")
        if body.contract.get("goal_id") != goal_id:
            raise RuntimeFault("GOAL_BINDING", "Contract target differs from URL")
        return command(
            a,
            k,
            "contract.freeze:" + goal_id,
            {**body.model_dump(), "expected_version": v},
            lambda: goals.freeze_contract(a, body.contract, expected_version=v),
        )

    @app.post("/api/v3/graphs")
    def graph(body: GraphBody, a: Actor = Depends(actor), k: str = Depends(key)) -> Any:
        a.require("graph.propose")
        return command(
            a,
            k,
            "graph.compile",
            body,
            lambda: runtime.save_graph(a, body.graph, body.contract_ref),
        )

    @app.post("/api/v3/goals/{goal_id}/activate")
    def activate(
        goal_id: str,
        body: ActivateBody,
        a: Actor = Depends(actor),
        k: str = Depends(key),
        v: int = Depends(version),
    ) -> Any:
        a.require("goal.activate")
        if body.contract_ref.get("id") != goal_id:
            raise RuntimeFault("GOAL_BINDING", "Contract differs from goal URL")
        return command(
            a,
            k,
            "goal.activate:" + goal_id,
            {**body.model_dump(), "expected_version": v},
            lambda: runtime.activate(
                a,
                body.contract_ref,
                body.graph_ref,
                body.grant_ref,
                body.profile,
                expected_version=v,
            ),
        )

    @app.post("/api/v3/goals/{goal_id}/steering", status_code=202)
    def steer(
        goal_id: str,
        body: SteeringBody,
        a: Actor = Depends(actor),
        k: str = Depends(key),
        v: int = Depends(version),
    ) -> Any:
        a.require("goal.steer")

        def execute() -> Any:
            if store.head(a.scope, "goal", goal_id)["row_version"] != v:
                raise Conflict("STALE_VERSION", "Goal changed before steering")
            return steering.receive(
                a,
                goal_id,
                body.kind,
                body.text,
                expected_contract_ref=body.expected_contract_ref,
                key="api:" + k,
                evidence_refs=body.evidence_refs,
                priority=body.priority,
            )

        return command(
            a, k, "goal.steer:" + goal_id, {**body.model_dump(), "expected_version": v}, execute
        )

    @app.post("/api/v3/questions/answer")
    def answer(body: AnswerBody, a: Actor = Depends(actor), k: str = Depends(key)) -> Any:
        a.require("question.answer")
        return command(
            a,
            k,
            "question.answer",
            body,
            lambda: goals.answer(a, body.question_ref, body.text, body.contract_ref),
        )

    @app.post("/api/v3/workers/claim")
    def claim(body: ClaimBody, a: Actor = Depends(actor), k: str = Depends(key)) -> Any:
        a.require("worker.execute")
        return command(a, k, "worker.claim", body, lambda: runtime.claim(a, goal_id=body.goal_id))

    @app.post("/api/v3/runs/start")
    def start(body: StartBody, a: Actor = Depends(actor), k: str = Depends(key)) -> Any:
        a.require("worker.execute")
        return command(
            a, k, "run.start", body, lambda: runtime.start(a, body.dispatch, body.session_handle)
        )

    @app.post("/api/v3/runs/{run_id}/heartbeat")
    def heartbeat(run_id: str, body: HeartbeatBody, a: Actor = Depends(actor)) -> Any:
        a.require("worker.execute")
        return runtime.heartbeat(a, run_id, body.lease_id, body.fence, body.sequence)

    @app.post("/api/v3/runs/{run_id}/events")
    def worker_event(run_id: str, body: EventBody, a: Actor = Depends(actor)) -> Any:
        a.require("worker.execute")
        return runtime.worker_event(
            a,
            run_id,
            body.lease_id,
            body.fence,
            message_id=body.message_id,
            sequence=body.sequence,
            event_type=body.event_type,
            payload=body.payload,
        )

    @app.post("/api/v3/runs/{run_id}/collect")
    def collect(
        run_id: str, body: LeaseBody, a: Actor = Depends(actor), k: str = Depends(key)
    ) -> Any:
        a.require("worker.execute")

        def execute() -> Any:
            runtime.lease(a, run_id, body.lease_id, body.fence)
            observed = require_service(services.worker_collector, "Observed worker collector")(
                a, run_id
            )
            return runtime.output_ready(
                a,
                run_id,
                body.lease_id,
                body.fence,
                observed["outputs"],
                usage=observed["usage"],
                process_stopped=observed["process_stopped"],
            )

        return command(a, k, "run.collect:" + run_id, body, execute)

    @app.post("/api/v3/verification/runs/{run_id}")
    def verify(
        run_id: str, body: VerifyBody, a: Actor = Depends(actor), k: str = Depends(key)
    ) -> Any:
        a.require("verifier.run")
        return command(
            a,
            k,
            "verification.run:" + run_id,
            body,
            lambda: require_service(services.verification, "Independent verifier").verify(
                a, run_id, body.acceptance_id, body.subject
            ),
        )

    @app.post("/api/v3/verification/runs/{run_id}/finish")
    def finish_work(run_id: str, a: Actor = Depends(actor), k: str = Depends(key)) -> Any:
        a.require("verifier.run")
        return command(
            a,
            k,
            "verification.work:" + run_id,
            {},
            lambda: require_service(services.verification, "Independent verifier").finish_work(
                a, run_id
            ),
        )

    @app.post("/api/v3/verification/goals/{goal_id}")
    def finish_goal(goal_id: str, a: Actor = Depends(actor), k: str = Depends(key)) -> Any:
        a.require("verifier.run")
        return command(
            a,
            k,
            "verification.goal:" + goal_id,
            {},
            lambda: require_service(services.verification, "Independent verifier").finish_goal(
                a, goal_id, services.global_checker
            ),
        )

    @app.post("/api/v3/registry/{kind}")
    def register(
        kind: str, body: RegistryBody, a: Actor = Depends(actor), k: str = Depends(key)
    ) -> Any:
        a.require("runtime.admin")
        allowed = {
            "app-binding",
            "model-profile",
            "driver-capabilities",
            "verifier-profile",
            "context-bundle",
            "harness-composition",
        }
        if kind not in allowed:
            raise RuntimeFault(
                "PROTECTED_REGISTRY",
                "This object requires its dedicated authority/evidence lifecycle",
            )

        def execute() -> Any:
            runtime.contracts.validate(kind, body.value)
            check_refs(store, a.scope, body.value)
            if kind == "app-binding":
                return goals.apps.register(a, body.value)
            with store.tx() as db:
                return store.put(db, a.scope, kind, body.object_id, body.revision, body.value)

        return command(a, k, "registry:" + kind, body, execute)

    @app.get("/api/v3/objects/{kind}/{object_id}")
    def read_object(
        kind: str, object_id: str, revision: int, digest: str, a: Actor = Depends(actor)
    ) -> Any:
        reader(a)
        if kind in {
            "eval-corpus",
            "corpus-case",
            "sealed-holdout",
            "demo-approval",
            "execution-grant",
        }:
            raise Hold(
                "DEDICATED_OBJECT_ACCESS", "Use the dedicated scoped authority/corpus service"
            )
        if kind in {
            "eval-experiment",
            "eval-trial",
            "eval-report",
            "sampling-plan",
            "analysis-plan",
        }:
            a.require("experiment.read")
            if "harness.propose" in a.permissions:
                raise Hold("EVAL_PROPOSER_READ", "Protected trial material is not proposer context")
        ref = {"id": object_id, "revision": revision, "digest": digest}
        if services.object_read_guard:
            services.object_read_guard(a, kind, ref)
        return store.get(a.scope, kind, ref)

    @app.post("/api/v3/authority/grants")
    def grant(body: RegistryBody, a: Actor = Depends(actor), k: str = Depends(key)) -> Any:
        a.require("grant.issue")
        return command(
            a, k, "authority.issue", body, lambda: runtime.authority.issue(a, body.value)
        )

    @app.get("/api/v3/metrics")
    def metrics(
        composition: str | None = None,
        model: str | None = None,
        driver: str | None = None,
        task_class: str | None = None,
        risk: str | None = None,
        repo: str | None = None,
        since: str | None = None,
        until: str | None = None,
        a: Actor = Depends(actor),
    ) -> Any:
        reader(a)
        filters = {
            k: v
            for k, v in {
                "composition": composition,
                "model": model,
                "driver": driver,
                "task_class": task_class,
                "risk": risk,
                "repo": repo,
            }.items()
            if v is not None
        }
        return Observatory(store).summary(a.scope, filters=filters, since=since, until=until)

    @app.get("/api/v3/telemetry/events")
    def telemetry_events(after: int = 0, limit: int = 100, a: Actor = Depends(actor)) -> Any:
        reader(a)
        safe: list[dict[str, Any]] = []

        def collect(batch: list[dict[str, Any]]) -> bool:
            safe.extend(batch)
            return True

        cursor = Observatory(store).export_batch(a.scope, after, collect, limit=limit)
        return {"events": safe, "next_cursor": cursor, "payload_exported": False}

    @app.post("/api/v3/evaluation/corpora")
    def corpus_freeze(body: CorpusBody, a: Actor = Depends(actor), k: str = Depends(key)) -> Any:
        a.require("corpus.manage")
        service = require_service(services.evaluation, "Evaluation service")
        return command(
            a,
            k,
            "evaluation.corpus",
            body,
            lambda: service.corpus.freeze(
                a,
                body.corpus_id,
                body.cases,
                holdout_use_limit=body.holdout_use_limit,
                metadata=body.metadata,
            ),
        )

    @app.post("/api/v3/evaluation/experiments")
    def experiment_freeze(
        body: PromotionBody, a: Actor = Depends(actor), k: str = Depends(key)
    ) -> Any:
        a.require("experiment.approve")
        return command(
            a,
            k,
            "evaluation.freeze",
            body,
            lambda: require_service(services.evaluation, "Evaluation service").freeze(a, body.plan),
        )

    @app.post("/api/v3/evaluation/run")
    def experiment_run(
        body: ExperimentRunBody, a: Actor = Depends(actor), k: str = Depends(key)
    ) -> Any:
        a.require("experiment.run")
        executor = require_service(services.eval_executor, "Qualified evaluation executor")
        return command(
            a,
            k,
            "evaluation.run",
            body,
            lambda: require_service(services.evaluation, "Evaluation service").run(
                a, body.ref, executor, split=body.split
            ),
        )

    @app.get("/api/v3/events")
    async def events(
        request: Request,
        after: int = 0,
        limit: int = 100,
        follow: bool = False,
        last_event_id: str = Header(default="", alias="Last-Event-ID"),
        a: Actor = Depends(actor),
    ) -> Any:
        reader(a)
        if after < 0 or not 1 <= limit <= 1000:
            raise RuntimeFault("EVENT_BOUNDS", "Invalid event cursor/limit")
        if last_event_id:
            if not last_event_id.isdigit():
                raise RuntimeFault("EVENT_CURSOR", "Event cursor must be numeric")
            after = max(after, int(last_event_id))
        authorization = request.headers.get("authorization", "")

        async def stream() -> Any:
            cursor = after
            for _ in range(60 if follow else 1):
                current_actor = services.authenticate(authorization)
                current_actor.require("runtime.read")
                if current_actor.scope != a.scope:
                    raise Hold("EVENT_SCOPE_CHANGED", "Reauthentication changed scope")
                batch = store.events(a.scope, after=cursor, limit=limit)
                for event in batch:
                    yield (
                        "id: "
                        + str(event["seq"])
                        + "\nevent: "
                        + event["event_type"]
                        + "\ndata: "
                        + canonical(event).decode()
                        + "\n\n"
                    )
                    cursor = event["seq"]
                if not follow or await request.is_disconnected():
                    break
                yield ": keep-alive\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/v3/meta/proposals")
    def meta_submit(body: RegistryBody, a: Actor = Depends(actor), k: str = Depends(key)) -> Any:
        a.require("harness.propose")
        return command(
            a,
            k,
            "meta.submit",
            body,
            lambda: require_service(services.meta, "Meta-harness").submit(a, body.value),
        )

    @app.post("/api/v3/meta/{proposal_id}/screen")
    def meta_screen(proposal_id: str, a: Actor = Depends(actor), k: str = Depends(key)) -> Any:
        a.require("harness.review")
        return command(
            a,
            k,
            "meta.screen:" + proposal_id,
            {},
            lambda: require_service(services.meta, "Meta-harness").screen(a, proposal_id),
        )

    @app.post("/api/v3/meta/{proposal_id}/review")
    def meta_review(
        proposal_id: str, body: RefBody, a: Actor = Depends(actor), k: str = Depends(key)
    ) -> Any:
        a.require("harness.review")
        return command(
            a,
            k,
            "meta.review:" + proposal_id,
            body,
            lambda: require_service(services.meta, "Meta-harness").record_review(
                a, proposal_id, body.ref
            ),
        )

    @app.post("/api/v3/meta/{proposal_id}/start-offline")
    def meta_offline(proposal_id: str, a: Actor = Depends(actor), k: str = Depends(key)) -> Any:
        a.require("experiment.run")
        return command(
            a,
            k,
            "meta.start-offline:" + proposal_id,
            {},
            lambda: require_service(services.meta, "Meta-harness").start_offline(a, proposal_id),
        )

    @app.post("/api/v3/meta/{proposal_id}/start-canary")
    def meta_start_canary(
        proposal_id: str, a: Actor = Depends(actor), k: str = Depends(key)
    ) -> Any:
        a.require("canary.run")
        return command(
            a,
            k,
            "meta.start-canary:" + proposal_id,
            {},
            lambda: require_service(services.meta, "Meta-harness").start_canary(a, proposal_id),
        )

    @app.post("/api/v3/meta/{proposal_id}/canary-trial")
    def meta_trial(
        proposal_id: str, body: CanaryTrialBody, a: Actor = Depends(actor), k: str = Depends(key)
    ) -> Any:
        a.require("canary.run")
        executor = require_service(services.canary_executor, "Qualified canary executor")
        return command(
            a,
            k,
            "meta.trial:" + proposal_id,
            body,
            lambda: require_service(services.meta, "Meta-harness").canary_trial(
                a, proposal_id, body.task_id, lambda task: executor(a.scope, proposal_id, task)
            ),
        )

    @app.post("/api/v3/meta/{proposal_id}/request-promotion")
    def meta_request_promotion(
        proposal_id: str, a: Actor = Depends(actor), k: str = Depends(key)
    ) -> Any:
        a.require("canary.run")
        return command(
            a,
            k,
            "meta.request-promotion:" + proposal_id,
            {},
            lambda: require_service(services.meta, "Meta-harness").request_promotion(
                a, proposal_id
            ),
        )

    @app.post("/api/v3/meta/{proposal_id}/abort")
    def meta_abort(
        proposal_id: str, body: ReasonBody, a: Actor = Depends(actor), k: str = Depends(key)
    ) -> Any:
        a.require("canary.run")
        return command(
            a,
            k,
            "meta.abort:" + proposal_id,
            body,
            lambda: require_service(services.meta, "Meta-harness").abort(
                a, proposal_id, body.reason
            ),
        )

    @app.get("/api/v3/meta/{proposal_id}/budget")
    def meta_budget(proposal_id: str, a: Actor = Depends(actor)) -> Any:
        a.require("experiment.read")
        return require_service(services.meta, "Meta-harness").budgets.totals(a.scope, proposal_id)

    @app.post("/api/v3/meta/{proposal_id}/approve-experiment")
    def meta_experiment(
        proposal_id: str, body: MetaApprovalBody, a: Actor = Depends(actor), k: str = Depends(key)
    ) -> Any:
        a.require("experiment.approve")
        return command(
            a,
            k,
            "meta.experiment:" + proposal_id,
            body,
            lambda: require_service(services.meta, "Meta-harness").approve_experiment(
                a, proposal_id, body.approval_ref, body.experiment_ref
            ),
        )

    @app.post("/api/v3/meta/{proposal_id}/evaluate")
    def meta_evaluate(
        proposal_id: str, body: RefBody, a: Actor = Depends(actor), k: str = Depends(key)
    ) -> Any:
        a.require("harness.review")
        return command(
            a,
            k,
            "meta.evaluate:" + proposal_id,
            body,
            lambda: require_service(services.meta, "Meta-harness").evaluate(
                a, proposal_id, body.ref
            ),
        )

    @app.post("/api/v3/meta/{proposal_id}/approve-canary")
    def meta_canary(
        proposal_id: str, body: CanaryApprovalBody, a: Actor = Depends(actor), k: str = Depends(key)
    ) -> Any:
        a.require("canary.approve")
        return command(
            a,
            k,
            "meta.canary:" + proposal_id,
            body,
            lambda: require_service(services.meta, "Meta-harness").approve_canary(
                a, proposal_id, body.policy_ref, body.approval_ref
            ),
        )

    @app.post("/api/v3/meta/{proposal_id}/promote")
    def meta_promote(
        proposal_id: str, body: PromotionBody, a: Actor = Depends(actor), k: str = Depends(key)
    ) -> Any:
        a.require("release.promote")
        return command(
            a,
            k,
            "meta.promote:" + proposal_id,
            body,
            lambda: require_service(services.meta, "Meta-harness").promote(
                a, proposal_id, body.plan
            ),
        )

    @app.post("/api/v3/meta/{proposal_id}/rollback")
    def meta_rollback(
        proposal_id: str, body: RollbackBody, a: Actor = Depends(actor), k: str = Depends(key)
    ) -> Any:
        a.require("release.rollback")
        return command(
            a,
            k,
            "meta.rollback:" + proposal_id,
            body,
            lambda: require_service(services.meta, "Meta-harness").rollback(
                a, proposal_id, body.target_ref, body.expected_active_ref, body.approval_ref
            ),
        )

    @app.post("/api/v3/runtime/kill")
    def kill(body: KillBody, a: Actor = Depends(actor), k: str = Depends(key)) -> Any:
        a.require("runtime.admin")
        return command(
            a,
            k,
            "runtime.kill",
            body,
            lambda: require_service(services.meta, "Meta-harness").kill_switch(
                a, body.enabled, body.reason
            ),
        )

    return app
