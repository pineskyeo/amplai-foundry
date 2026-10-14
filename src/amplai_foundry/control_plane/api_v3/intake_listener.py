"""The intake-only listener (Work 034 S1g; spec H-12, OD-8, plan.md §3.2 item 11).

The front agent runs in a VM on this Mac (OD-8). The VM's ``127.0.0.1`` is the VM itself, so it
cannot reach the operator API, which stays on the host's loopback. It enters AMPLAI only through
this second listener: its own socket (``intake_listener.host``/``port`` in ``local.json``, no
default) in the same process as the operator API, serving the same intake routers (so one adapter
keeps one token, identity map and rate-limit window on both, H-7) and nothing else.

What it refuses:

- every path outside ``/api/v3/intake/<configured adapter id>`` (operator routes, meta, local
  product routes, ``/healthz``, OpenAPI and docs): ``404 INTAKE_LISTENER_ROUTE``, before the body
  is read and before any route or credential is looked at;
- a path segment ``.`` or ``..`` (the same code): no proxy in front can turn it into another route;
- a WebSocket: closed before it is accepted;
- a trailing slash or any other path under an adapter's root that is not one of its routes: the
  same ``404 INTAKE_LISTENER_ROUTE`` (no redirect, so no ``Location`` built from ``Host``);
- the operator token on an intake route: ``401 UNAUTHENTICATED``, as on the operator listener (the
  intake gate accepts only the adapter's own token, ``intake.py``);
- an unauthenticated request whose body or query does not validate: the credential is checked
  first, so it gets the gate's ``401`` and never the field names of a ``422``.

The uvicorn server behind it trusts no ``X-Forwarded-For``/``X-Forwarded-Proto`` (``proxy_headers``
off: the peer address is the TCP peer) and sends no ``Server`` header. On shutdown both servers wait
at most ``GRACEFUL_SECONDS`` for running requests. uvicorn 0.54 has no header or body read timeout
(its ``Config`` has ``timeout_keep_alive``, armed only after a response, and
``timeout_graceful_shutdown``); a connection that never finishes its headers is closed at shutdown.

H-12 also requires TLS or a source-pinned token on this listener. Which one is an operator decision
not taken yet, so neither is implemented, and :func:`check_protected` refuses to bind anything but a
loopback address (``INTAKE_LISTENER_UNPROTECTED``) until it is.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import signal
import socket
from collections.abc import Iterable, Iterator, Mapping
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from amplai_foundry.runtime.errors import Hold, RuntimeFault

from .intake import ROUTE_ROOT, IntakeGate
from .server import BoundedBody, fault_response

LISTENER_ROUTE = "INTAKE_LISTENER_ROUTE"
GRACEFUL_SECONDS = 10  # running requests at shutdown, both servers


def _refusal(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse({"code": code, "message": message, "outcome": "rejected"}, status)


def _route_refusal() -> JSONResponse:
    return _refusal(LISTENER_ROUTE, "Only the intake routes are served on this listener", 404)


class IntakeOnly:
    """ASGI guard in front of the intake app: only the configured adapters' route trees pass."""

    def __init__(self, app: Any, adapter_ids: Iterable[str]) -> None:
        self.app = app
        self.roots = tuple(ROUTE_ROOT + adapter_id for adapter_id in adapter_ids)

    def allowed(self, path: str) -> bool:
        if any(part in (".", "..") for part in path.split("/")):
            return False
        return any(path == root or path.startswith(root + "/") for root in self.roots)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":  # a WebSocket: refused before it is accepted
            await send({"type": "websocket.close", "code": 1008})
            return
        if not self.allowed(scope.get("path", "")):
            await _route_refusal()(scope, receive, send)
            return
        await self.app(scope, receive, send)


def create_intake_app(routers: Iterable[APIRouter], gates: Mapping[str, IntakeGate]) -> FastAPI:
    """An app with the given intake routers only (``gates``: adapter id -> its gate); no OpenAPI,
    docs, health route or slash redirect."""
    app = FastAPI(
        title="AMPLAI V3 intake",
        version="3.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
    )

    @app.exception_handler(RequestValidationError)
    async def invalid_handler(request: Request, exc: RequestValidationError) -> Any:
        """Authenticate before saying what is wrong with the request."""
        adapter_id = request.url.path[len(ROUTE_ROOT) :].split("/", 1)[0]
        gate = gates.get(adapter_id)
        if gate is None:  # unreachable behind IntakeOnly
            return _route_refusal()
        try:
            gate.authenticate(request.headers.get("authorization", ""))
        except RuntimeFault as fault:
            return fault_response(fault)
        return await request_validation_exception_handler(request, exc)

    @app.exception_handler(RuntimeFault)
    async def fault_handler(request: Request, exc: RuntimeFault) -> Any:
        return fault_response(exc)

    @app.exception_handler(HTTPException)
    async def http_handler(request: Request, exc: HTTPException) -> Any:
        if exc.status_code == 404:  # under an adapter's root but not one of its routes
            return _route_refusal()
        if exc.status_code == 405:
            return _refusal("METHOD_NOT_ALLOWED", "Method not allowed on this route", 405)
        # no intake route raises another HTTPException today (they raise RuntimeFault)
        return _refusal("HTTP_ERROR", str(exc.detail), exc.status_code)

    for router in routers:
        app.include_router(router)
    app.add_middleware(BoundedBody)
    app.add_middleware(IntakeOnly, adapter_ids=tuple(gates))  # outermost: before the body
    return app


def unmapped(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """``host`` as an address; an IPv4-mapped IPv6 address (``::ffff:a.b.c.d``) as its IPv4."""
    address = ipaddress.ip_address(host)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def check_protected(host: str) -> None:
    """H-12: a listener off loopback needs TLS or a source-pinned token. Neither is decided yet
    (Work 034 S1g open decision), so only a loopback address may be bound. An IPv4-mapped IPv6
    address is judged by its IPv4 address, so the answer does not depend on how the running
    CPython's ``ipaddress`` treats ``::ffff:127.0.0.1`` (that has changed between releases)."""
    if not unmapped(host).is_loopback:
        raise Hold(
            "INTAKE_LISTENER_UNPROTECTED",
            "The intake listener binds only a loopback address until H-12's TLS or "
            "source-pinned token is decided and configured",
            details={"host": host},
        )


def bind_socket(host: str, port: int) -> socket.socket:
    """Bind the listener's socket before anything is served, so a taken port is a coded Hold."""
    family = socket.AF_INET6 if ipaddress.ip_address(host).version == 6 else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
    except OSError as exc:
        sock.close()
        raise Hold(
            "INTAKE_LISTENER_BIND",
            "The intake listener could not bind its address",
            details={"host": host, "port": port, "error": type(exc).__name__},
        ) from exc
    return sock


def _quiet_server(config: Any) -> Any:
    """A uvicorn server that leaves the process signals to :func:`serve_with_listener`."""
    import uvicorn

    class QuietServer(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self) -> Iterator[None]:
            yield

    return QuietServer(config)


def _side_server(app: Any, graceful: int = GRACEFUL_SECONDS) -> Any:
    import uvicorn

    return _quiet_server(
        uvicorn.Config(
            app,
            lifespan="off",
            workers=1,
            access_log=False,
            proxy_headers=False,  # the peer is the TCP peer, whatever X-Forwarded-For says
            server_header=False,
            timeout_graceful_shutdown=graceful,
        )
    )


async def run_pair(
    operator: Any,
    side: Any,
    side_sockets: list[socket.socket],
    operator_sockets: list[socket.socket] | None = None,
) -> None:
    """Serve both uvicorn servers in one event loop. Either one ending stops the other."""
    side_task = asyncio.create_task(side.serve(sockets=side_sockets))
    side_task.add_done_callback(lambda _: setattr(operator, "should_exit", True))
    try:
        await operator.serve(sockets=operator_sockets)
    finally:
        side.should_exit = True
        await side_task


def serve_with_listener(
    operator_app: Any,
    operator_port: int,
    intake_app: Any,
    host: str,
    port: int,
    *,
    graceful: int = GRACEFUL_SECONDS,
) -> None:
    """``ops local-serve`` with an ``intake_listener``: the operator API on 127.0.0.1 as before,
    the intake listener on its own address, one process, one store owner.

    Ctrl-C or SIGTERM stops both servers gracefully, then is raised again as ``uvicorn.run``
    does (Ctrl-C ends quietly, SIGTERM ends the process with that signal)."""
    import uvicorn

    if intake_app is None:
        raise Hold(
            "INTAKE_LISTENER_NO_INTAKE",
            "intake_listener serves intake adapters; configure intake.adapters first",
        )
    if port == operator_port:
        raise Hold(
            "INTAKE_LISTENER_PORT",
            "The intake listener needs a port of its own",
            details={"port": port},
        )
    check_protected(host)
    sock = bind_socket(host, port)
    operator = _quiet_server(
        uvicorn.Config(
            operator_app,
            host="127.0.0.1",
            port=operator_port,
            workers=1,
            access_log=False,
            timeout_graceful_shutdown=graceful,
        )
    )
    side = _side_server(intake_app, graceful)
    captured: list[int] = []

    def stop(sig: int, frame: Any) -> None:
        if captured and sig == signal.SIGINT:  # a second Ctrl-C: do not wait for requests
            operator.force_exit = side.force_exit = True
        captured.append(sig)
        operator.should_exit = side.should_exit = True

    handled = (signal.SIGINT, signal.SIGTERM)
    originals = {sig: signal.signal(sig, stop) for sig in handled}
    try:
        asyncio.run(run_pair(operator, side, [sock]))
    finally:
        for sig, handler in originals.items():
            signal.signal(sig, handler)
        sock.close()
    try:
        for raised in reversed(captured):
            signal.raise_signal(raised)
    except KeyboardInterrupt:  # as ``uvicorn.run``: Ctrl-C ends quietly
        pass
