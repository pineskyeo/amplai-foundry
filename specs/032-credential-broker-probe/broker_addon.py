"""mitmproxy addon for the Work 032 credential broker probe (not a product component).

The agent container holds only a placeholder. For an allowed provider host, the broker replaces
the placeholder inside any request header with the real value it read at start from the
operator's own credential file, mounted read-only into the broker container (never copied): an
``env:NAME`` line (the Claude token file) or a ``json:a.b`` path (the Codex scoped auth.json).
Nothing else changes. Hosts off the allowlist are refused. The addon logs one line per
request with host, path prefix, status and whether a placeholder was replaced; it never logs a
header value, a body or the real credential.

    mitmdump -s broker_addon.py --set allow=api.anthropic.com:443,... \
        --set placeholder=... --set secret_file=/broker/credential --set secret_format=env:NAME
"""

from __future__ import annotations

import json
import sys
import time

from mitmproxy import ctx, http


def read_secret(path: str, fmt: str) -> str:
    kind, _, key = fmt.partition(":")
    with open(path) as f:
        text = f.read()
    if kind == "env":
        for line in text.splitlines():
            name, _, value = line.partition("=")
            if name.strip() == key and value.strip():
                return value.strip()
    elif kind == "json":
        value: object = json.loads(text)
        for part in key.split("."):
            value = value[part] if isinstance(value, dict) else None  # type: ignore[index]
        if isinstance(value, str) and value:
            return value
    raise ValueError("the credential file has no value at " + fmt)


class Broker:
    def __init__(self) -> None:
        self.allow: set[str] = set()
        self.placeholder = ""
        self.secret = ""

    def load(self, loader) -> None:  # type: ignore[no-untyped-def]
        loader.add_option("allow", str, "", "comma-separated host:port allowlist")
        loader.add_option("placeholder", str, "", "the placeholder value the agent holds")
        loader.add_option("secret_file", str, "", "the operator's credential file (read-only)")
        loader.add_option("secret_format", str, "", "env:NAME or json:dotted.path")

    def configure(self, updated) -> None:  # type: ignore[no-untyped-def]
        if "allow" in updated:
            self.allow = {h.strip() for h in ctx.options.allow.split(",") if h.strip()}
        if "placeholder" in updated:
            self.placeholder = ctx.options.placeholder.strip()
        if ctx.options.secret_file and ctx.options.secret_format:
            self.secret = read_secret(ctx.options.secret_file, ctx.options.secret_format)
        if self.placeholder and self.placeholder == self.secret:
            raise ValueError("placeholder equals the secret")

    def _log(self, **fields: object) -> None:
        sys.stdout.write(json.dumps({"ts": round(time.time(), 3), **fields}) + "\n")
        sys.stdout.flush()

    def http_connect(self, flow: http.HTTPFlow) -> None:
        target = f"{flow.request.host}:{flow.request.port}"
        if target not in self.allow:
            flow.response = http.Response.make(403, b"not on the probe allowlist")
            self._log(event="deny", target=target)

    def request(self, flow: http.HTTPFlow) -> None:
        target = f"{flow.request.host}:{flow.request.port}"
        if target not in self.allow:
            flow.response = http.Response.make(403, b"not on the probe allowlist")
            self._log(event="deny", target=target)
            return
        replaced = []
        if self.placeholder:
            for name, value in list(flow.request.headers.items(multi=True)):
                if self.placeholder in value:
                    flow.request.headers[name] = value.replace(self.placeholder, self.secret)
                    replaced.append(name.lower())
        self._log(
            event="request",
            target=target,
            method=flow.request.method,
            path=flow.request.path.split("?")[0][:80],
            replaced_headers=replaced,
        )

    def response(self, flow: http.HTTPFlow) -> None:
        self._log(
            event="response",
            target=f"{flow.request.host}:{flow.request.port}",
            path=flow.request.path.split("?")[0][:80],
            status=flow.response.status_code if flow.response else None,
        )


addons = [Broker()]
