"""An ``httpx`` transport answered by a Starlette ``TestClient`` (tests only).

Production clients (``AmplaiClient``, the intake MCP server) use ``httpx``. Starlette's
``TestClient`` uses ``httpx2`` when it is installed (the MCP SDK, Work 034 S1c, depends on it), and
its transport then no longer plugs into an ``httpx.Client``. This bridge forwards each request
through the ``TestClient`` instead, whichever of the two it uses.
"""

from __future__ import annotations

from typing import Any

import httpx


def forwarding_transport(client: Any) -> httpx.MockTransport:
    def forward(request: httpx.Request) -> httpx.Response:
        answer = client.request(
            request.method,
            request.url.raw_path.decode(),
            headers={k: v for k, v in request.headers.items() if k.lower() != "host"},
            content=request.content,
        )
        return httpx.Response(answer.status_code, headers=answer.headers, content=answer.content)

    return httpx.MockTransport(forward)
