"""Small authenticated WSGI boundary for the Platform 0.4 service layer.

No web framework is required. Production deployments may wrap the same service
with FastAPI/ASGI later; the contract remains the service methods below.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import traceback
from collections.abc import Callable, Iterable
from dataclasses import asdict
from http import HTTPStatus
from typing import Any
from urllib.parse import unquote

from amplai_foundry.control_plane.errors import (
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    ControlPlaneError,
    NotFoundError,
    PayloadTooLargeError,
    ValidationError,
)
from amplai_foundry.control_plane.projections import ProjectionService
from amplai_foundry.control_plane.service import ControlPlaneService

StartResponse = Callable[[str, list[tuple[str, str]]], Any]

# 신뢰 경계다. 클라이언트가 선언한 길이를 그대로 읽지 않는다.
MAX_BODY_BYTES = 1 << 20


class ControlPlaneWSGIApp:
    def __init__(
        self, service: ControlPlaneService, projection: ProjectionService | None = None
    ) -> None:
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
        except PayloadTooLargeError as error:
            status, payload = 413, {"error": str(error)}
        except (ValidationError, ValueError, json.JSONDecodeError) as error:
            status, payload = 400, {"error": str(error)}
        except ControlPlaneError as error:
            status, payload = 500, {"error": str(error)}
        except sqlite3.IntegrityError:
            status, payload = 409, {"error": "STATE_CONFLICT"}
            self._log_unexpected()
        except sqlite3.Error:
            status, payload = 503, {"error": "STORE_UNAVAILABLE"}
            self._log_unexpected()
        except Exception:
            # A WSGI application must answer even when something below it is
            # broken.  The detail goes to the log, never to the client.
            status, payload = 500, {"error": "INTERNAL_ERROR"}
            self._log_unexpected()
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        phrase = HTTPStatus(status).phrase
        start_response(
            f"{status} {phrase}",
            [
                ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Length", str(len(body))),
            ],
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
        if method == "POST" and segments[3:] == ["orchestration-requests"]:
            return self.service.submit_orchestration_request(
                project_id=project_id,
                raw_token=raw_token,
                idempotency_key=str(environ.get("HTTP_IDEMPOTENCY_KEY") or ""),
                request=self._body(environ),
            )
        if method == "GET" and len(segments) == 5 and segments[3] == "orchestration-requests":
            request = self.service.get_orchestration_request(
                request_id=segments[4], project_id=project_id, raw_token=raw_token
            )
            return 200, asdict(request)
        if method == "GET" and len(segments) == 5 and segments[3] == "work":
            return 200, self.service.get_orchestration_work(
                work_id=segments[4], project_id=project_id, raw_token=raw_token
            )
        if method == "GET" and segments[3:] == ["pending-actions"]:
            return 200, self.service.list_pending_orchestration_actions(
                project_id=project_id, raw_token=raw_token
            )
        if method == "POST" and segments[3:] == ["knowledge-intake-requests"]:
            return self.service.request_knowledge_intake(
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
            job = self.service.get_job(
                job_id=segments[4], project_id=project_id, raw_token=raw_token
            )
            return 200, asdict(job)
        if method == "GET" and segments[3:] == ["projection"]:
            principal = self.service.auth.authenticate(
                raw_token, project_id=project_id, permission="projection:read"
            )
            state = self.projection.snapshot(tenant_id=principal.tenant_id, project_id=project_id)
            if state is None:
                state = self.projection.rebuild(
                    tenant_id=principal.tenant_id, project_id=project_id
                )
            return 200, state
        raise NotFoundError("ROUTE_NOT_FOUND")

    @staticmethod
    def _log_unexpected() -> None:
        print("control-plane: unhandled error", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)

    @staticmethod
    def _bearer(environ: dict[str, Any]) -> str:
        header = str(environ.get("HTTP_AUTHORIZATION") or "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            raise AuthenticationError("BEARER_TOKEN_REQUIRED")
        return header[len(prefix) :].strip()

    @staticmethod
    def _content_length(environ: dict[str, Any]) -> int:
        """Decide how much to read before touching the stream.

        The declared length is attacker-controlled, so it is validated and capped
        first.  Reading it unchecked let a client name an arbitrary size, and a
        negative value turned into `read(-1)` — read until EOF (`D-056`).
        """
        declared = str(environ.get("CONTENT_LENGTH") or "").strip()
        if not declared:
            return 0
        try:
            length = int(declared)
        except ValueError:
            raise ValidationError("CONTENT_LENGTH_INVALID") from None
        if length < 0:
            raise ValidationError("CONTENT_LENGTH_INVALID")
        if length > MAX_BODY_BYTES:
            raise PayloadTooLargeError("PAYLOAD_TOO_LARGE")
        return length

    @classmethod
    def _body(cls, environ: dict[str, Any]) -> dict[str, Any]:
        length = cls._content_length(environ)
        stream = environ.get("wsgi.input")
        raw = stream.read(length) if stream is not None and length else b"{}"
        if length and len(raw) != length:
            raise ValidationError("REQUEST_BODY_TRUNCATED")
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValidationError("JSON_OBJECT_REQUIRED")
        return value
