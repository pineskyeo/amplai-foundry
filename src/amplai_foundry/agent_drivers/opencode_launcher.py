"""``opencode serve`` in the qualified container, one per dispatch (specs/031-opencode-driver).

The server runs with the exact ``ContainerSandbox`` argv, detached, on the run's workspace and a
per-dispatch HOME that holds the seeded provider login. It listens on the container's loopback
only; the container is on the ``--internal`` egress network, so the host reaches it through
``docker exec curl``. The server password is new per run, passed to ``docker run`` by variable
name (never in an argv), and to curl as a config on stdin. Design 11:36 wants the password on;
D-091 records that the agent, running as the same uid, can read it from the server's
``/proc/<pid>/environ``. What the guard below removes is the variable in the agent's own shell
environment (measured in specs/031-opencode-driver/measurements.md M1: with both parts the bash
tool environment has no ``OPENCODE_SERVER_*`` name at all; the plugin alone leaves the names).

Server-sent events (``Accept: text/event-stream``) are streamed from a long-running ``curl -N``;
every other request is one ``curl`` call. A dropped stream ends the event iteration and the
driver reconciles through REST (http.py ``OpenCodeEvents``).
"""

from __future__ import annotations

import contextlib
import os
import secrets
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import IO, Any

import httpx

from ..runtime.errors import Hold
from ..sandbox.container import ContainerSandbox

PORT = 4096
USER = "opencode"
PASSWORD_ENV = "OPENCODE_SERVER_PASSWORD"
USERNAME_ENV = "OPENCODE_SERVER_USERNAME"
GUARD_DIR = "/amplai-input/oc-config"  # read-only config dir inside the container

# B: a shell.env plugin blanks the variables for every shell the server starts
GUARD_PLUGIN = """\
export default async () => ({
  "shell.env": async (_input, output) => {
    output.env.OPENCODE_SERVER_PASSWORD = "";
    output.env.OPENCODE_SERVER_USERNAME = "";
  },
});
"""
# C: the tool shell unsets the names before bash starts (the plugin can only blank a value)
GUARD_SHELL = """\
#!/bin/sh
unset OPENCODE_SERVER_PASSWORD OPENCODE_SERVER_USERNAME
exec /bin/bash "$@"
"""
# the server writes this file into its config dir when missing; read-only would fail (M1)
GUARD_GITIGNORE = "node_modules\npackage.json\npackage-lock.json\nbun.lock\n.gitignore"


def write_env_guard(directory: Path) -> Path:
    """The read-only config dir that keeps the server password out of the tool shell env."""
    directory = Path(directory)
    (directory / "plugin").mkdir(parents=True, exist_ok=True)
    (directory / "plugin" / "blank.js").write_text(GUARD_PLUGIN)
    (directory / ".gitignore").write_text(GUARD_GITIGNORE)
    (directory / "shell").write_text(GUARD_SHELL)
    (directory / "shell").chmod(0o755)
    return directory


def server_argv(
    sandbox: ContainerSandbox, workspace: Path, name: str, native_home: Path, guard: Path
) -> list[str]:
    """The exact sandbox argv for ``opencode serve``, detached, with the env guard mounted."""
    cmd = sandbox.command(
        ["opencode", "serve", "--port", str(PORT), "--hostname", "127.0.0.1"],
        workspace, name, env_names=[PASSWORD_ENV, USERNAME_ENV], native_home=native_home,
        readonly_mounts={GUARD_DIR: guard},
    )  # fmt: skip
    at = cmd.index(sandbox.profile.image)
    extra = ["--env", f"OPENCODE_CONFIG_DIR={GUARD_DIR}", "--env", f"SHELL={GUARD_DIR}/shell"]
    return [cmd[0], cmd[1], "-d", *cmd[2:at], *extra, *cmd[at:]]


HEALTH_SECONDS = 180  # the qualifier measured slow first starts behind the egress profile
REQUEST_SECONDS = 60


def _curl_quote(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _split_head(head: bytes) -> tuple[int, list[tuple[str, str]]]:
    status_line, *header_lines = head.decode("latin-1").split("\r\n")
    headers = [
        (k.strip(), v.strip())
        for k, _, v in (h.partition(":") for h in header_lines)
        if k.strip() and k.strip().lower() not in {"content-length", "transfer-encoding"}
    ]
    return int(status_line.split()[1]), headers


class _ProcessStream(httpx.SyncByteStream):
    """The body of a streamed response: curl's stdout until it ends or the stream is closed."""

    def __init__(self, process: subprocess.Popen[bytes], first: bytes) -> None:
        self.process, self.first = process, first

    def __iter__(self) -> Iterator[bytes]:
        if self.first:
            yield self.first
        out: IO[bytes] = self.process.stdout  # type: ignore[assignment]
        while chunk := out.read1(16384):  # type: ignore[attr-defined]
            yield chunk

    def close(self) -> None:
        with contextlib.suppress(ProcessLookupError):
            self.process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.process.wait(timeout=5)


class DockerExecTransport(httpx.BaseTransport):
    """HTTP to a server on the container's own loopback, through ``docker exec curl``."""

    def __init__(
        self, container: str, port: int, *, auth: tuple[str, str] | None, engine: str = "docker"
    ) -> None:
        self.container, self.port, self.auth, self.engine = container, port, auth, engine

    def _config(self, request: httpx.Request) -> bytes:
        url = f"http://127.0.0.1:{self.port}{request.url.raw_path.decode()}"
        lines = [f"url = {_curl_quote(url)}", f"request = {_curl_quote(request.method)}"]
        if self.auth:
            lines.append(f"user = {_curl_quote(self.auth[0] + ':' + self.auth[1])}")
        for key, value in request.headers.items():
            if key.lower() not in {"host", "authorization", "content-length", "accept-encoding"}:
                lines.append(f"header = {_curl_quote(key + ': ' + value)}")
        body = request.read()
        if body:
            lines.append(f"data-binary = {_curl_quote(body.decode())}")
        return "\n".join(lines).encode()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if "text/event-stream" in request.headers.get("accept", ""):
            return self._stream(request)
        run = subprocess.run(
            [self.engine, "exec", "-i", self.container, "curl", "-s", "-i",
             "--max-time", str(REQUEST_SECONDS), "-K", "-"],
            input=self._config(request), capture_output=True, timeout=REQUEST_SECONDS + 30,
            check=False,
        )  # fmt: skip
        if run.returncode != 0:
            raise httpx.ConnectError(f"docker exec curl rc={run.returncode}", request=request)
        head, _, content = run.stdout.partition(b"\r\n\r\n")
        while head.startswith(b"HTTP/1.1 100"):
            head, _, content = content.partition(b"\r\n\r\n")
        status, headers = _split_head(head)
        return httpx.Response(status, headers=headers, content=content, request=request)

    def _stream(self, request: httpx.Request) -> httpx.Response:
        process = subprocess.Popen(
            [self.engine, "exec", "-i", self.container, "curl", "-s", "-i", "-N", "-K", "-"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )  # fmt: skip
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(self._config(request))
        process.stdin.close()
        buffered = b""
        deadline = time.monotonic() + REQUEST_SECONDS
        while b"\r\n\r\n" not in buffered:
            chunk = process.stdout.read1(4096)  # type: ignore[attr-defined]
            if not chunk or time.monotonic() > deadline:
                _ProcessStream(process, b"").close()
                raise httpx.ConnectError("event stream did not answer", request=request)
            buffered += chunk
        head, _, rest = buffered.partition(b"\r\n\r\n")
        status, headers = _split_head(head)
        return httpx.Response(
            status, headers=headers, stream=_ProcessStream(process, rest), request=request
        )


class DockerOpenCodeServer:
    """One started ``opencode serve`` container (the ``OpenCodeServer`` of opencode_port)."""

    def __init__(
        self, sandbox: ContainerSandbox, name: str, workspace: Path, password: str
    ) -> None:
        self.sandbox, self.name, self._workspace = sandbox, name, workspace
        self.password: str | None = password
        self.base_url = f"http://127.0.0.1:{PORT}"
        self.engine = sandbox.engine
        self.transport: httpx.BaseTransport = DockerExecTransport(
            name, PORT, auth=(USER, password), engine=self.engine
        )
        self._lock = threading.Lock()

    def client(self) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, transport=self.transport, timeout=30)

    def _exec(self, script: str) -> str:
        run = subprocess.run(
            [self.engine, "exec", "-i", self.name, "sh", "-c", script],
            capture_output=True, timeout=60, check=False,
        )  # fmt: skip
        return run.stdout.decode(errors="replace")

    def running(self) -> bool:
        run = subprocess.run(
            [self.engine, "inspect", "-f", "{{.State.Running}}", self.name],
            capture_output=True, text=True, check=False,
        )  # fmt: skip
        return run.stdout.strip() == "true"

    def workspace(self) -> str:
        """The host directory actually mounted at /workspace in this container."""
        run = subprocess.run(
            [self.engine, "inspect", "-f",
             '{{range .Mounts}}{{if eq .Destination "/workspace"}}{{.Source}}{{end}}{{end}}',
             self.name],
            capture_output=True, text=True, check=False,
        )  # fmt: skip
        return run.stdout.strip()

    def children(self) -> list[str]:
        """Tool processes: descendants of the server that are not opencode itself."""
        if not self.running():
            return []
        table = self._exec(
            "for d in /proc/[0-9]*; do p=${d#/proc/};"
            " pp=$(awk '{print $4}' $d/stat 2>/dev/null);"
            ' c=$(tr "\\000" " " < $d/cmdline 2>/dev/null);'
            ' [ -n "$pp" ] && echo "$p $pp $c"; done'
        )
        rows = []
        for line in table.splitlines():
            parts = line.split(None, 2)
            if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
                rows.append((int(parts[0]), int(parts[1]), parts[2]))
        frontier = {p for p, _, c in rows if "opencode" in c and "serve" in c}
        found: list[str] = []
        while frontier:
            found += [c[:200] for p, pp, c in rows if pp in frontier and "opencode" not in c]
            frontier = {p for p, pp, _ in rows if pp in frontier}
        return found

    def boundary(self, session: str) -> bool:
        """The session is idle and no tool process outlives the turn."""
        with self.client() as client:
            statuses = client.get("/session/status").json()
        idle = statuses.get(session, {"type": "idle"}).get("type") == "idle"
        return bool(idle) and not self.children()

    def stop(self) -> None:
        with self._lock:
            subprocess.run([self.engine, "rm", "-f", self.name], capture_output=True, check=False)
            self.password = None


class DockerOpenCodeLauncher:
    """Starts ``opencode serve`` for one dispatch and waits until it answers healthy."""

    def __init__(
        self, sandbox: ContainerSandbox, guard: Path, *, prefix: str = "amplai-opencode"
    ) -> None:
        self.sandbox, self.prefix = sandbox, prefix
        self.guard = write_env_guard(guard)

    def launch(self, dispatch_id: str, workspace: Path, native_home: Path) -> DockerOpenCodeServer:
        name = f"{self.prefix}-{dispatch_id}"[:100]
        password = secrets.token_urlsafe(32)
        detached = server_argv(self.sandbox, workspace, name, native_home, self.guard)
        engine = self.sandbox.engine
        subprocess.run([engine, "rm", "-f", name], capture_output=True, check=False)
        env = {**os.environ, PASSWORD_ENV: password, USERNAME_ENV: USER}
        run = subprocess.run(detached, env=env, capture_output=True, check=False)
        if run.returncode != 0:
            raise Hold("OPENCODE_START", "The OpenCode container did not start",
                       details={"stderr": run.stderr.decode(errors="replace")[-300:]})  # fmt: skip
        server = DockerOpenCodeServer(self.sandbox, name, workspace, password)
        started = time.monotonic()
        with server.client() as client:
            while time.monotonic() - started < HEALTH_SECONDS:
                with contextlib.suppress(httpx.HTTPError, ValueError, IndexError):
                    if client.get("/global/health").status_code == 200:
                        server.startup_seconds = round(time.monotonic() - started, 1)  # type: ignore[attr-defined]
                        return server
                time.sleep(1)
        server.stop()
        raise Hold("OPENCODE_HEALTH", "opencode serve did not become healthy in time")

    def reconcile(self) -> list[str]:
        """Remove server containers left by a crashed owner (named by this launcher's prefix)."""
        engine = self.sandbox.engine
        run = subprocess.run(
            [engine, "ps", "-a", "--filter", f"name=^{self.prefix}-", "--format", "{{.Names}}"],
            capture_output=True, text=True, check=False,
        )  # fmt: skip
        names = [n for n in run.stdout.split() if n.startswith(self.prefix + "-")]
        for name in names:
            subprocess.run([engine, "rm", "-f", name], capture_output=True, check=False)
        return names


__all__: list[Any] = ["DockerExecTransport", "DockerOpenCodeLauncher", "DockerOpenCodeServer"]
