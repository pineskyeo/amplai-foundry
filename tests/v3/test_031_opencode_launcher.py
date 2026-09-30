"""Work 031 — the exec transport to ``opencode serve`` in its container.

A stand-in ``docker`` records its argv and stdin and answers like ``curl -i``. The password must
travel only in the curl config on stdin, never in an argv; an event-stream request is streamed.
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import httpx

from amplai_foundry.agent_drivers.http import BoundHttp
from amplai_foundry.agent_drivers.opencode_launcher import DockerExecTransport

FAKE = r"""#!{python}
import json, sys, time
from pathlib import Path
log = Path({log!r})
config = sys.stdin.read()
entry = {{"argv": sys.argv[1:], "config": config}}
with log.open("a") as out:
    out.write(json.dumps(entry) + "\n")
stream = "-N" in sys.argv
out = sys.stdout.buffer
if stream:
    out.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n")
    out.flush()
    for i in range(3):
        out.write(b'data: {{"id": "e%d", "type": "server.heartbeat"}}\n\n' % i)
        out.flush()
        time.sleep(0.05)
else:
    out.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n")
    out.write(json.dumps({{"healthy": True, "version": "1.17.13"}}).encode())
"""


def fake_engine(tmp_path: Path) -> tuple[Path, Path]:
    log = tmp_path / "calls.jsonl"
    engine = tmp_path / "docker"
    engine.write_text(FAKE.format(python=sys.executable, log=str(log)))
    engine.chmod(engine.stat().st_mode | stat.S_IEXEC)
    return engine, log


def calls(log: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_a_request_carries_the_password_on_stdin_never_in_argv(tmp_path: Path) -> None:
    engine, log = fake_engine(tmp_path)
    secret = "per-run-password-value"
    transport = DockerExecTransport("srv", 4096, auth=("opencode", secret), engine=str(engine))
    with httpx.Client(base_url="http://127.0.0.1:4096", transport=transport) as client:
        response = client.post("/session", json={"title": "x"})
    assert response.status_code == 200 and response.json()["healthy"] is True
    (call,) = calls(log)
    argv = call["argv"]
    assert argv[:4] == ["exec", "-i", "srv", "curl"] and "-K" in argv  # type: ignore[index]
    assert all(secret not in a for a in argv)  # type: ignore[union-attr]
    config = str(call["config"])
    assert f'user = "opencode:{secret}"' in config
    assert 'url = "http://127.0.0.1:4096/session"' in config and 'request = "POST"' in config
    assert 'data-binary = "{\\"title\\":\\"x\\"}"' in config  # quotes escaped for curl


def test_an_event_stream_is_streamed_event_by_event(tmp_path: Path) -> None:
    engine, log = fake_engine(tmp_path)
    transport = DockerExecTransport("srv", 4096, auth=("opencode", "p"), engine=str(engine))
    http = BoundHttp(
        "http://127.0.0.1:4096",
        auth=httpx.BasicAuth("opencode", "p"),
        transport=transport,
        allow_local=True,
    )
    events = [json.loads(data) for data in http.sse("/event")]
    assert [e["id"] for e in events] == ["e0", "e1", "e2"]
    (call,) = calls(log)
    assert "-N" in call["argv"]  # type: ignore[operator]


def test_a_failed_exec_is_a_connection_error(tmp_path: Path) -> None:
    engine = tmp_path / "docker"
    engine.write_text(f"#!{sys.executable}\nimport sys\nsys.stdin.read()\nsys.exit(7)\n")
    engine.chmod(engine.stat().st_mode | stat.S_IEXEC)
    transport = DockerExecTransport("srv", 4096, auth=None, engine=str(engine))
    with httpx.Client(base_url="http://127.0.0.1:4096", transport=transport) as client:
        try:
            client.get("/global/health")
        except httpx.ConnectError:
            pass
        else:  # pragma: no cover
            raise AssertionError("a failed docker exec must not look like an answer")
