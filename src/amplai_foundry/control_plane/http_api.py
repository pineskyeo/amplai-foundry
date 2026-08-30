"""Small authenticated WSGI boundary for the Platform 0.4 service layer.

No web framework is required. Production deployments may wrap the same service
with FastAPI/ASGI later; the contract remains the service methods below.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from http import HTTPStatus
from typing import Any, Callable, Iterable
from urllib.parse import unquote

from amplai_foundry.control_plane.errors import (
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    ControlPlaneError,
    NotFoundError,
    ValidationError,
)
from amplai_foundry.control_plane.projections import ProjectionService
from amplai_foundry.control_plane.service import ControlPlaneService

StartResponse = Callable[[str, list[tuple[str, str]]], Any]


class ControlPlaneWSGIApp:
    def __init__(self, service: ControlPlaneService, projection: ProjectionService | None = None) -> None:
        self.service = service
        self.projection = projection or ProjectionService(service.store)

    def __call__(self, environ: dict[str, Any], start_response: StartResponse) -> Iterable[bytes]:
        try:
            status, payload = self._dispatch(environ)
        except AuthenticationError as error:
            status, payload = 401, {"error": str(error)}
        except AuthorizationError as error:
            status, payload = 403, {"error": str(error)}
        except NotFoundError as error:
            status, payload = 404, {"error": str(error)}
        except ConflictError as error:
            status, payload = 409, {"error": str(error)}
        except (ValidationError, ValueError, json.JSONDecodeError) as error:
            status, payload = 400, {"error": str(error)}
        except ControlPlaneError as error:
            status, payload = 500, {"error": str(error)}
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        phrase = HTTPStatus(status).phrase
        start_response(
            f"{status} {phrase}",
            [("Content-Type", "application/json; charset=utf-8"), ("Content-Length", str(len(body)))],
        )
        return [body]

    def _dispatch(self, environ: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        method = str(environ.get("REQUEST_METHOD") or "GET").upper()
        path = str(environ.get("PATH_INFO") or "/")
        segments = [unquote(item) for item in path.split("/") if item]
        if len(segments) < 3 or segments[:2] != ["v1", "projects"]:
            raise NotFoundError("ROUTE_NOT_FOUND")
        project_id = segments[2]
        raw_token = self._bearer(environ)
        if method == "POST" and len(segments) == 4 and segments[3] in {"decision", "evidence"}:
            body = self._body(environ)
            return self.service.publish_reference(
                kind=segments[3],
                project_id=project_id,
                raw_token=raw_token,
                idempotency_key=str(environ.get("HTTP_IDEMPOTENCY_KEY") or ""),
                envelope=body,
            )
        if method == "POST" and segments[3:] == ["context-requests"]:
            return self.service.request_context(
                project_id=project_id,
                raw_token=raw_token,
                idempotency_key=str(environ.get("HTTP_IDEMPOTENCY_KEY") or ""),
                request=self._body(environ),
            )
        if method == "GET" and len(segments) == 6 and segments[3] == "references":
            reference = self.service.get_reference(
                kind=segments[4],
                canonical_ref=segments[5],
                project_id=project_id,
                raw_token=raw_token,
            )
            return 200, asdict(reference)
        if method == "GET" and len(segments) == 5 and segments[3] == "jobs":
            job = self.service.get_job(job_id=segments[4], project_id=project_id, raw_token=raw_token)
            return 200, asdict(job)
        if method == "GET" and segments[3:] == ["projection"]:
            principal = self.service.auth.authenticate(
                raw_token, project_id=project_id, permission="projection:read"
            )
            state = self.projection.snapshot(tenant_id=principal.tenant_id, project_id=project_id)
            if state is None:
                state = self.projection.rebuild(tenant_id=principal.tenant_id, project_id=project_id)
            return 200, state
        raise NotFoundError("ROUTE_NOT_FOUND")

    @staticmethod
    def _bearer(environ: dict[str, Any]) -> str:
        header = str(environ.get("HTTP_AUTHORIZATION") or "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            raise AuthenticationError("BEARER_TOKEN_REQUIRED")
        return header[len(prefix) :].strip()

    @staticmethod
    def _body(environ: dict[str, Any]) -> dict[str, Any]:
        stream = environ.get("wsgi.input")
        length = int(environ.get("CONTENT_LENGTH") or 0)
        raw = stream.read(length) if stream is not None and length else b"{}"
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValidationError("JSON_OBJECT_REQUIRED")
        return value
